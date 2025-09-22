from flask import Flask, render_template, request, send_from_directory, url_for, redirect
import json, os, datetime, uuid, pytz, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email import encoders
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Fichiers persistants (Render)
DATA_FILE = "/mnt/data/data.json"
UPLOAD_FOLDER = "/mnt/data/uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# -------------------------------------------------------------------
# Utils
# -------------------------------------------------------------------
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("demandes", [])
                data.setdefault("compteur_traitees", 0)
                return data
            else:
                # compat ancien format (liste brute)
                return {"demandes": data, "compteur_traitees": 0}
    return {"demandes": [], "compteur_traitees": 0}

def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def supprimer_fichier(filename):
    if not filename:
        return
    chemin = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(chemin):
        os.remove(chemin)

# -------------------------------------------------------------------
# Email helper: HTML + texte + logo inline + PJ
# -------------------------------------------------------------------
def _build_brand_header():
    # Couleurs en cohérence avec ton admin (jaune #f4c45a, bleu liens)
    return """
    <div style="padding:16px 20px;border-bottom:1px solid #f0f0f0;display:flex;align-items:center;gap:12px;">
      <img src="cid:logo_cid" alt="Intégrale Academy" style="height:40px;display:block;">
      <div style="font-weight:700;font-size:16px;color:#111;">Intégrale Academy</div>
    </div>
    """

def _wrap_html(title_html, body_html):
    return f"""
    <html>
      <body style="background:#f7f7f7;margin:0;padding:24px;font-family:Arial,Helvetica,sans-serif;">
        <div style="max-width:680px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;border:1px solid #eee;">
          {_build_brand_header()}
          <div style="padding:22px;">
            {title_html}
            <div style="font-size:14px;line-height:1.6;color:#222;">
              {body_html}
            </div>
          </div>
          <div style="padding:12px 22px;color:#777;font-size:12px;border-top:1px solid #f0f0f0;">
            Merci de ne pas répondre directement à ce message automatique.
          </div>
        </div>
      </body>
    </html>
    """

def _attach_logo(related_part):
    """Attache le logo static/logo.png si présent, sous CID logo_cid."""
    try:
        logo_path = os.path.join(app.root_path, "static", "logo.png")
        if os.path.exists(logo_path):
            with open(logo_path, "rb") as f:
                img = MIMEImage(f.read())
                img.add_header("Content-ID", "<logo_cid>")
                img.add_header("Content-Disposition", "inline", filename="logo.png")
                related_part.attach(img)
    except Exception as e:
        print("⚠️ Impossible d’attacher le logo :", e)

def send_email_html(to_emails, subject, plain_text, html_body, attachments_paths=None):
    """
    Construit un email multipart correct :
      mixed
        └─ related
            └─ alternative (text/plain + text/html)
        + pièces jointes
    + logo inline (cid:logo_cid) si dispo.
    """
    # Top-level: mixed
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = os.getenv("SMTP_USER")
    msg["To"] = to_emails

    # related (pour inline images)
    related = MIMEMultipart("related")
    msg.attach(related)

    # alternative (texte + html)
    alt = MIMEMultipart("alternative")
    related.attach(alt)

    alt.attach(MIMEText(plain_text, "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html", "utf-8"))

    # logo inline
    _attach_logo(related)

    # pièces jointes (optionnel)
    if attachments_paths:
        for chemin in attachments_paths:
            if not chemin or not os.path.exists(chemin):
                continue
            with open(chemin, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f"attachment; filename={os.path.basename(chemin)}")
                msg.attach(part)

    # envoi
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as serveur:
            serveur.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
            serveur.send_message(msg)
        return True
    except Exception as e:
        print("❌ Erreur envoi email :", e)
        return False

# -------------------------------------------------------------------
# Emails (admin, accusé de réception, confirmation traité avec PJ)
# -------------------------------------------------------------------
def envoyer_mail_admin(demande):
    sujet = f"Nouvelle demande stagiaire — {demande['motif']}"
    # Texte brut (fallback)
    plain = (
        "Nouvelle demande reçue :\n\n"
        f"Nom: {demande['nom']}\n"
        f"Prénom: {demande['prenom']}\n"
        f"Téléphone: {demande['telephone']}\n"
        f"Email: {demande['mail']}\n"
        f"Motif: {demande['motif']}\n"
        f"Détails: {demande['details']}\n"
        f"Date: {demande['date']}\n"
    )
    if demande.get("justificatif"):
        plain += f"Justificatif: {url_for('download_file', filename=demande['justificatif'], _external=True)}\n"

    # HTML
    details_rows = f"""
      <tr><td style="padding:6px 0;color:#555;">Nom</td><td style="padding:6px 0;"><strong>{demande['nom']}</strong></td></tr>
      <tr><td style="padding:6px 0;color:#555;">Prénom</td><td style="padding:6px 0;"><strong>{demande['prenom']}</strong></td></tr>
      <tr><td style="padding:6px 0;color:#555;">Téléphone</td><td style="padding:6px 0;">{demande['telephone']}</td></tr>
      <tr><td style="padding:6px 0;color:#555;">Email</td><td style="padding:6px 0;">{demande['mail']}</td></tr>
      <tr><td style="padding:6px 0;color:#555;">Motif</td><td style="padding:6px 0;">{demande['motif']}</td></tr>
      <tr><td style="padding:6px 0;color:#555;">Détails</td><td style="padding:6px 0;">{demande['details']}</td></tr>
      <tr><td style="padding:6px 0;color:#555;">Date</td><td style="padding:6px 0;">{demande['date']}</td></tr>
    """
    if demande.get("justificatif"):
        link = url_for('download_file', filename=demande['justificatif'], _external=True)
        details_rows += f"""<tr><td style="padding:6px 0;color:#555;">Justificatif</td>
                            <td style="padding:6px 0;"><a href="{link}" style="color:#0d6efd;text-decoration:none;">📎 Télécharger</a></td></tr>"""

    html = _wrap_html(
        '<h1 style="margin:0 0 12px;font-size:20px;">Nouvelle demande stagiaire</h1>',
        f"""
        <p style="margin:0 0 12px;">Une nouvelle demande a été soumise sur le site.</p>
        <table style="width:100%;border-collapse:collapse;font-size:14px;">{details_rows}</table>
        """
    )

    ok = send_email_html(
        to_emails="elsaduq83@gmail.com, ecole@integraleacademy.com",
        subject=sujet,
        plain_text=plain,
        html_body=html,
        attachments_paths=None
    )
    print("✅ Mail admin envoyé" if ok else "❌ Échec mail admin")

def envoyer_mail_accuse(demande):
    sujet = "Accusé de réception — Intégrale Academy"
    plain = (
        f"Bonjour {demande['prenom']} {demande['nom']},\n\n"
        "Nous avons bien reçu votre demande.\n"
        "Elle sera traitée dans les meilleurs délais.\n"
        "Vous recevrez un mail lorsque votre demande aura été traitée.\n\n"
        "L'équipe Intégrale Academy\n"
    )
    html = _wrap_html(
        '<h1 style="margin:0 0 12px;font-size:20px;">Accusé de réception</h1>',
        f"""
        <p>Bonjour <strong>{demande['prenom']} {demande['nom']}</strong>,</p>
        <p>Nous avons bien reçu votre demande. Elle sera traitée dans les meilleurs délais.</p>
        <p style="margin:0">Vous recevrez un mail lorsque votre demande aura été traitée.</p>
        """
    )

    ok = send_email_html(
        to_emails=demande["mail"],
        subject=sujet,
        plain_text=plain,
        html_body=html,
        attachments_paths=None
    )
    print(f"✅ Accusé envoyé à {demande['mail']}" if ok else "❌ Échec accusé")

def envoyer_mail_confirmation(demande):
    """Mail 'Traité' avec toutes les PJ sauvegardées + stockage du HTML pour /voir_mail"""
    sujet = "Votre demande a été traitée — Intégrale Academy"

    # Fallback texte
    plain = (
        f"Bonjour {demande['prenom']} {demande['nom']},\n\n"
        "Votre demande a été traitée.\n\n"
        f"Motif : {demande['motif']}\n"
        f"Détails : {demande['details']}\n"
        f"Commentaire : {demande.get('commentaire') or 'Aucun commentaire ajouté.'}\n\n"
        "L'équipe Intégrale Academy\n"
    )

    # HTML
    body_html = f"""
      <p>Bonjour <strong>{demande['prenom']} {demande['nom']}</strong>,</p>
      <p style="margin:0 0 8px;">✅ Votre demande a été traitée.</p>
      <div style="background:#f9fafb;border:1px solid #eef0f2;border-radius:8px;padding:12px 14px;margin:12px 0;">
        <div style="margin:4px 0;"><strong>Motif :</strong> {demande['motif']}</div>
        <div style="margin:4px 0;"><strong>Détails :</strong> {demande['details']}</div>
        <div style="margin:4px 0;"><strong>Commentaire :</strong> {demande.get('commentaire') or 'Aucun commentaire ajouté.'}</div>
      </div>
      {"<p style='margin:8px 0;'>📎 Des pièces jointes sont incluses avec ce message.</p>" if demande.get("pieces_jointes") else ""}
      <p style="margin:16px 0 0;">Cordialement,<br>L'équipe Intégrale Academy</p>
    """
    html = _wrap_html('<h1 style="margin:0 0 12px;font-size:20px;">Demande traitée</h1>', body_html)

    # Prépare les chemins de PJ
    pj_paths = []
    for pj in demande.get("pieces_jointes", []):
        chemin = os.path.join(UPLOAD_FOLDER, pj)
        if os.path.exists(chemin):
            pj_paths.append(chemin)

    ok = send_email_html(
        to_emails=demande["mail"],
        subject=sujet,
        plain_text=plain,
        html_body=html,
        attachments_paths=pj_paths
    )

    if ok:
        # Conserver un aperçu pour /voir_mail
        demande["mail_contenu"] = f"Sujet : {sujet}\n\n{plain}"
        demande["mail_html"] = html
    return ok

# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        data = load_data()
        demandes = data["demandes"]
        paris_tz = pytz.timezone("Europe/Paris")

        justificatif_filename = ""
        if "justificatif" in request.files:
            f = request.files["justificatif"]
            if f and f.filename:
                filename = secure_filename(f.filename)
                f.save(os.path.join(UPLOAD_FOLDER, filename))
                justificatif_filename = filename

        new_demande = {
            "id": str(uuid.uuid4()),
            "nom": request.form["nom"],
            "prenom": request.form["prenom"],
            "telephone": request.form["telephone"],
            "mail": request.form["mail"],
            "motif": request.form["motif"],
            "details": request.form["details"],
            "justificatif": justificatif_filename,
            "date": datetime.datetime.now(paris_tz).strftime("%d/%m/%Y %H:%M"),
            "attribution": "",
            "statut": "Non traité",
            "commentaire": "",
            "mail_confirme": "",
            "mail_erreur": "",
            "mail_contenu": "",
            "mail_html": "",
            "pieces_jointes": []
        }
        demandes.append(new_demande)
        save_data(data)

        # Mails : admin + accusé
        envoyer_mail_admin(new_demande)
        envoyer_mail_accuse(new_demande)

        return render_template("confirmation.html")
    return render_template("index.html")

@app.route("/admin", methods=["GET", "POST"])
def admin():
    data = load_data()
    demandes = data["demandes"]

    if request.method == "POST":
        action = request.form.get("action")
        # si clic sur le bouton de suppression PJ (name="delete_pj" value="nom.ext")
        if not action and request.form.get("delete_pj"):
            action = "delete_pj"

        demande_id = request.form.get("id")

        if action == "update":
            for d in demandes:
                if d["id"] == demande_id:
                    d["mail"] = request.form.get("mail") or d["mail"]
                    d["details"] = request.form.get("details")
                    d["commentaire"] = request.form.get("commentaire")
                    d["attribution"] = request.form.get("attribution", d.get("attribution", ""))
                    ancien_statut = d.get("statut", "Non traité")
                    nouveau_statut = request.form.get("statut") or ancien_statut

                    # Ajout de PJ persistantes
                    if "pj" in request.files:
                        for f in request.files.getlist("pj"):
                            if f and f.filename:
                                filename = secure_filename(f.filename)
                                filepath = os.path.join(UPLOAD_FOLDER, filename)
                                f.save(filepath)
                                d.setdefault("pieces_jointes", [])
                                if filename not in d["pieces_jointes"]:
                                    d["pieces_jointes"].append(filename)

                    # Envoi du mail si changement vers "Traité"
                    if ancien_statut != "Traité" and nouveau_statut == "Traité":
                        if envoyer_mail_confirmation(d):
                            data["compteur_traitees"] += 1
                            paris_tz = pytz.timezone("Europe/Paris")
                            d["mail_confirme"] = datetime.datetime.now(paris_tz).strftime("%d/%m/%Y %H:%M")
                            d["mail_erreur"] = ""
                        else:
                            d["mail_erreur"] = "❌ Erreur lors de l'envoi du mail"

                    d["statut"] = nouveau_statut

            save_data(data)
            return redirect(url_for("admin"))

        elif action == "delete_pj":
            pj_name = request.form.get("pj_name") or request.form.get("delete_pj")
            for d in demandes:
                if d["id"] == demande_id and pj_name in d.get("pieces_jointes", []):
                    d["pieces_jointes"].remove(pj_name)
                    supprimer_fichier(pj_name)
            save_data(data)
            return redirect(url_for("admin"))

        elif action == "delete":
            # Supprime justificatif + toutes les PJ, puis la demande
            to_remove = None
            for d in demandes:
                if d["id"] == demande_id:
                    to_remove = d
                    break
            if to_remove:
                supprimer_fichier(to_remove.get("justificatif"))
                for pj in to_remove.get("pieces_jointes", []):
                    supprimer_fichier(pj)
                data["demandes"].remove(to_remove)
                save_data(data)
            return redirect(url_for("admin"))

    return render_template("admin.html",
                           demandes=demandes,
                           compteur_traitees=data["compteur_traitees"])

@app.route("/imprimer/<demande_id>")
def imprimer(demande_id):
    data = load_data()
    demande = next((d for d in data["demandes"] if d["id"] == demande_id), None)
    return render_template("imprimer.html", demande=demande)

@app.route("/voir_mail/<demande_id>")
def voir_mail(demande_id):
    data = load_data()
    demande = next((d for d in data["demandes"] if d["id"] == demande_id), None)
    return render_template("voir_mail.html", demande=demande)

@app.route("/uploads/<filename>")
def download_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

if __name__ == "__main__":
    app.run(debug=True)
