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
                data.setdefault("archives", [])  # ✅ ajouté, tout le reste inchangé
                data.setdefault("compteur_traitees", 0)
                return data
            else:
                # compat ancien format (liste brute)
                return {"demandes": data, "archives": [], "compteur_traitees": 0}
    return {"demandes": [], "archives": [], "compteur_traitees": 0}

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
# Email helper: HTML responsive (tables) + texte + logo inline + PJ
# -------------------------------------------------------------------
def _brand_header_table():
    """Header en <table> (compatible email) avec logo compact centré."""
    return """
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
      <tr>
        <td align="center" style="padding:16px 16px 8px 16px;margin:0;">
          <img src="cid:logo_cid" alt="Intégrale Academy" height="56" style="display:block;height:56px;width:auto;max-width:220px;">
        </td>
      </tr>
      <tr>
        <td align="center" style="padding:0 16px 10px 16px;margin:0; font-weight:700;font-size:16px;color:#111;">
          Intégrale Academy
        </td>
      </tr>
      <tr><td style="border-bottom:1px solid #f0f0f0;"></td></tr>
    </table>
    """

def _wrap_html(title_html, body_html):
    """Gabarit responsive basé sur tables (600px max, 100% mobile)."""
    return f"""
    <!DOCTYPE html>
    <html>
    <body style="margin:0;padding:0;background:#f7f7f7;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:#f7f7f7;">
        <tr>
          <td align="center" style="padding:24px;">
            <!-- Carte -->
            <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="border-collapse:collapse;max-width:600px;width:100%; background:#ffffff;border:1px solid #eeeeee;border-radius:12px;overflow:hidden;">
              <tr>
                <td style="padding:0;">{_brand_header_table()}</td>
              </tr>
              <!-- Contenu -->
              <tr>
                <td style="padding:22px;">
                  <!-- Titre -->
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr><td style="font-family:Arial,Helvetica,sans-serif;">{title_html}</td></tr>
                  </table>
                  <!-- Corps -->
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr>
                      <td style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:#222;">
                        {body_html}
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
              <!-- Footer -->
              <tr>
                <td style="padding:12px 22px;color:#777;font-size:12px;border-top:1px solid #f0f0f0; font-family:Arial,Helvetica,sans-serif;">
                  Merci de ne pas répondre directement à ce message automatique.
                </td>
              </tr>
            </table>
            <!-- /Carte -->
          </td>
        </tr>
      </table>
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
    """ Structure email: mixed
        └─ related
           └─ alternative (text/plain + text/html)
        + attachments
    """
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = os.getenv("SMTP_USER")
    msg["To"] = to_emails

    related = MIMEMultipart("related")
    msg.attach(related)
    alt = MIMEMultipart("alternative")
    related.attach(alt)
    alt.attach(MIMEText(plain_text, "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html", "utf-8"))

    _attach_logo(related)

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

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as serveur:
            serveur.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
            serveur.send_message(msg)
        return True
    except Exception as e:
        print("❌ Erreur envoi email :", e)
        return False

# -------------------------------------------------------------------
# Emails (admin, accusé, confirmation) — CONTENU COMPLET
# -------------------------------------------------------------------
def envoyer_mail_admin(demande):
    sujet = f"🆕 Nouvelle demande stagiaire — {demande['motif']}"
    plain = (
        "🆕 Nouvelle demande reçue :\n\n"
        f"👤 Nom: {demande['nom']}\n"
        f"👤 Prénom: {demande['prenom']}\n"
        f"📞 Téléphone: {demande['telephone']}\n"
        f"✉️ Email: {demande['mail']}\n"
        f"📌 Motif: {demande['motif']}\n"
        f"📝 Détails: {demande['details']}\n"
        f"📅 Date: {demande['date']}\n"
    )
    if demande.get("justificatif"):
        plain += f"📎 Justificatif: {url_for('download_file', filename=demande['justificatif'], _external=True)}\n"

    # ✅ Tableau aligné : 2 colonnes (label fixe)
    rows = f"""
      <tr><td style="padding:6px 8px;color:#555;width:150px;">👤 Nom</td>
          <td style="padding:6px 8px;"><strong>{demande['nom']}</strong></td></tr>
      <tr><td style="padding:6px 8px;color:#555;width:150px;">👤 Prénom</td>
          <td style="padding:6px 8px;"><strong>{demande['prenom']}</strong></td></tr>
      <tr><td style="padding:6px 8px;color:#555;width:150px;">📞 Téléphone</td>
          <td style="padding:6px 8px;">{demande['telephone']}</td></tr>
      <tr><td style="padding:6px 8px;color:#555;width:150px;">✉️ Email</td>
          <td style="padding:6px 8px;">{demande['mail']}</td></tr>
      <tr><td style="padding:6px 8px;color:#555;width:150px;">📌 Motif</td>
          <td style="padding:6px 8px;">{demande['motif']}</td></tr>
      <tr><td style="padding:6px 8px;color:#555;width:150px;">📝 Détails</td>
          <td style="padding:6px 8px;">{demande['details']}</td></tr>
      <tr><td style="padding:6px 8px;color:#555;width:150px;">📅 Date</td>
          <td style="padding:6px 8px;">{demande['date']}</td></tr>
    """
    if demande.get("justificatif"):
        link = url_for('download_file', filename=demande['justificatif'], _external=True)
        rows += f"""<tr><td style="padding:6px 8px;color:#555;width:150px;">📎 Justificatif</td>
                      <td style="padding:6px 8px;">
                        <a href="{link}" style="color:#0d6efd;text-decoration:none;">Télécharger</a>
                      </td></tr>"""

    html = _wrap_html(
        '<h1 style="margin:0 0 12px;font-size:20px;">🆕 Nouvelle demande stagiaire</h1>',
        f"""
        <p style="margin:0 0 12px;">Une nouvelle demande a été soumise sur le site.</p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:14px;">
          {rows}
        </table>
        """
    )
    send_email_html("elsaduq83@gmail.com, ecole@integraleacademy.com", sujet, plain, html)

def envoyer_mail_accuse(demande):
    sujet = "📩 Accusé de réception — Intégrale Academy"
    plain = (
        f"Bonjour {demande['prenom']} {demande['nom']},\n\n"
        "📩 Nous avons bien reçu votre demande.\n"
        "⏳ Elle sera traitée dans les meilleurs délais.\n"
        "✅ Vous recevrez un mail lorsque votre demande aura été traitée.\n\n"
        "🙏 Merci de votre confiance,\n"
        "L'équipe Intégrale Academy\n"
    )
    html = _wrap_html(
        '<h1 style="margin:0 0 12px;font-size:20px;">📩 Accusé de réception</h1>',
        f"""
        <p>Bonjour <strong>{demande['prenom']} {demande['nom']}</strong>,</p>
        <p>📩 Nous avons bien reçu votre demande.</p>
        <p>⏳ Elle sera traitée dans les meilleurs délais.</p>
        <p style="margin:0">✅ Vous recevrez un mail lorsque votre demande aura été traitée.</p>
        <p style="margin:16px 0 0;">🙏 Merci de votre confiance,<br>L'équipe Intégrale Academy</p>
        """
    )
    send_email_html(demande["mail"], sujet, plain, html)

def envoyer_mail_confirmation(demande):
    sujet = "✅ Votre demande a été traitée — Intégrale Academy"
    plain = (
        f"Bonjour {demande['prenom']} {demande['nom']},\n\n"
        "✅ Votre demande a été traitée.\n\n"
        f"📌 Motif : {demande['motif']}\n"
        f"📝 Détails : {demande['details']}\n"
        f"✍️ Notre réponse : {demande.get('commentaire') or 'Aucun commentaire ajouté.'}\n"
        f"{'📎 Des pièces jointes sont incluses.' if demande.get('pieces_jointes') else ''}\n\n"
        "Cordialement,\n"
        "L'équipe Intégrale Academy\n"
    )
    body_html = f"""
      <p>Bonjour <strong>{demande['prenom']} {demande['nom']}</strong>,</p>
      <p style="margin:0 0 8px;">✅ <strong>Votre demande a été traitée.</strong></p>
      <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="border-collapse:collapse;background:#f9fafb;border:1px solid #eef0f2;border-radius:8px;">
        <tr>
          <td style="padding:12px 14px;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222;">
            <div style="margin:4px 0;"><strong>📌 Motif :</strong> {demande['motif']}</div>
            <div style="margin:4px 0;"><strong>📝 Détails :</strong> {demande['details']}</div>
            <div style="margin:12px 0;padding:12px;background:#fff8e5; border:1px solid #f0dca6;border-radius:6px;">
              <strong>✍️ Notre réponse :</strong><br>
              {demande.get('commentaire') or 'Aucun commentaire ajouté.'}
            </div>
          </td>
        </tr>
      </table>
      {"<p style='margin:8px 0;'>📎 Des pièces jointes sont incluses avec ce message.</p>" if demande.get("pieces_jointes") else ""}
      <p style="margin:16px 0 0;">Cordialement,<br>L'équipe Intégrale Academy</p>
    """
    html = _wrap_html('<h1 style="margin:0 0 12px;font-size:20px;">✅ Demande traitée</h1>', body_html)

    pj_paths = []
    for pj in demande.get("pieces_jointes", []):
        chemin = os.path.join(UPLOAD_FOLDER, pj)
        if os.path.exists(chemin):
            pj_paths.append(chemin)

    ok = send_email_html(demande["mail"], sujet, plain, html, attachments_paths=pj_paths)
    if ok:
        demande["mail_contenu"] = f"Sujet : {sujet}\n\n{plain}"
        demande["mail_html"] = html
    return ok

# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    data = load_data()
    if request.method == "POST":
        demandes = data["demandes"]
        paris_tz = pytz.timezone("Europe/Paris")

        justificatif_filename = ""
        if "justificatif" in request.files:
            f = request.files["justificatif"]
            if f and f.filename:
                filename = secure_filename(f.filename)
                f.save(os.path.join(UPLOAD_FOLDER, filename))
                justificatif_filename = filename

        nom_in = request.form["nom"].strip()
        prenom_in = request.form["prenom"].strip()
        mail_in = request.form["mail"].strip()
        motif_in = request.form["motif"].strip()
        details_in = request.form["details"].strip()

        # ✅ Détection doublon (nom+prenom+mail+motif+details)
        is_doublon = any(
            d.get("nom","").strip().lower() == nom_in.lower() and
            d.get("prenom","").strip().lower() == prenom_in.lower() and
            d.get("mail","").strip().lower() == mail_in.lower() and
            d.get("motif","").strip().lower() == motif_in.lower() and
            d.get("details","").strip().lower() == details_in.lower()
            for d in demandes
        )

        new_demande = {
            "id": str(uuid.uuid4()),
            "nom": nom_in,
            "prenom": prenom_in,
            "telephone": request.form["telephone"],
            "mail": mail_in,
            "motif": motif_in,
            "details": details_in,
            "justificatif": justificatif_filename,
            "date": datetime.datetime.now(paris_tz).strftime("%d/%m/%Y %H:%M"),
            "attribution": "",
            "statut": "Non traité",
            "commentaire": "",
            "mail_confirme": "",
            "mail_erreur": "",
            "mail_contenu": "",
            "mail_html": "",
            "pieces_jointes": [],
            "is_doublon": is_doublon
        }
        demandes.append(new_demande)
        save_data(data)

        try: envoyer_mail_admin(new_demande)
        except: pass
        try: envoyer_mail_accuse(new_demande)
        except: pass

        return render_template("confirmation.html")

    return render_template("index.html")

@app.route("/admin", methods=["GET", "POST"])
def admin():
    data = load_data()
    demandes = data["demandes"]

    if request.method == "POST":
        action = request.form.get("action")
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

                    # Upload nouvelles pièces jointes
                    if "pj" in request.files:
                        for f in request.files.getlist("pj"):
                            if f and f.filename:
                                filename = secure_filename(f.filename)
                                filepath = os.path.join(UPLOAD_FOLDER, filename)
                                f.save(filepath)
                                d.setdefault("pieces_jointes", [])
                                if filename not in d["pieces_jointes"]:
                                    d["pieces_jointes"].append(filename)

                    # Passage à Traité => envoi confirmation
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
            to_remove = None
            for d in demandes:
                if d["id"] == demande_id:
                    to_remove = d
                    break
            if to_remove:
                # Archiver la demande (au lieu de la perdre)
                data["archives"].append(to_remove)
                # supprimer les fichiers associés
                supprimer_fichier(to_remove.get("justificatif"))
                for pj in to_remove.get("pieces_jointes", []):
                    supprimer_fichier(pj)
                data["demandes"].remove(to_remove)
                save_data(data)
            return redirect(url_for("admin"))

    return render_template("admin.html", demandes=demandes, compteur_traitees=data["compteur_traitees"])

@app.route("/archives", methods=["GET", "POST"])
def archives():
    data = load_data()

    if request.method == "POST":
        action = request.form.get("action")
        if action == "delete_one":
            archive_id = request.form.get("id")
            data["archives"] = [a for a in data["archives"] if a["id"] != archive_id]
            save_data(data)
        elif action == "clear":
            data["archives"] = []
            save_data(data)
        return redirect(url_for("archives"))

    archives = data["archives"]

    query = request.args.get("q", "").strip().lower()
    if query:
        archives = [
            a for a in archives if
            query in a.get("nom","").lower()
            or query in a.get("prenom","").lower()
            or query in a.get("mail","").lower()
            or query in a.get("motif","").lower()
            or query in a.get("details","").lower()
        ]

    return render_template("archives.html", archives=archives, query=query)

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
