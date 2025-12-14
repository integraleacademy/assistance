from flask import Flask, render_template, request, send_from_directory, url_for, redirect, abort
from flask import render_template_string
import json, os, datetime, uuid, pytz, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email import encoders
from werkzeug.utils import secure_filename

from reportlab.platypus import Table, TableStyle
from reportlab.lib import colors

from functools import wraps
from werkzeug.security import check_password_hash, generate_password_hash
from flask import session, flash

# ---------- USERS (chargés depuis les variables d'environnement) ----------
# Si tu as mis les variables dans Render / .env : on les lit ici.
USERS = {
    os.getenv("USER_ELSA_EMAIL", "elsaduq83@gmail.com").lower(): {
        "name": "Elsa",
        "role": "user",
        # si tu veux stocker le mot de passe en clair (pas top), on lit USER_ELSA_PASS
        # Si tu préfères stocker un hash dans env, remplace par la valeur hashée
        "pass": os.getenv("USER_ELSA_PASS", "Lv15052021@")
    },
    os.getenv("USER_MOHAMED_EMAIL", "accueil@integraleacademy.com").lower(): {
        "name": "Mohamed",
        "role": "user",
        "pass": os.getenv("USER_MOHAMED_PASS", "Lv15052021@")
    },
    os.getenv("USER_CLEMENT_EMAIL", "clement@integraleacademy.com").lower(): {
        "name": "Clément",
        "role": "admin",   # Clément = super-admin (voit tout)
        "pass": os.getenv("USER_CLEMENT_PASS", "Lv15052021@")
    }
}


app = Flask(__name__)
app.secret_key = "cle-flask-secrete-2024"

from datetime import date

@app.context_processor
def inject_now():
    return {"now": date.today}


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
                data.setdefault("archives", [])
                data.setdefault("compteur_traitees", 0)
                data.setdefault("hebergements", [])
                return data
            else:
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
# Email helper
# -------------------------------------------------------------------
def _brand_header_table():
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
    return f"""
    <!DOCTYPE html>
    <html>
    <body style="margin:0;padding:0;background:#f7f7f7;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:#f7f7f7;">
        <tr>
          <td align="center" style="padding:24px;">
            <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="border-collapse:collapse;max-width:600px;width:100%; background:#ffffff;border:1px solid #eeeeee;border-radius:12px;overflow:hidden;">
              <tr>
                <td style="padding:0;">{_brand_header_table()}</td>
              </tr>
              <tr>
                <td style="padding:22px;">
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr><td style="font-family:Arial,Helvetica,sans-serif;">{title_html}</td></tr>
                  </table>
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                    <tr>
                      <td style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:#222;">
                        {body_html}
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
              <tr>
                <td style="padding:12px 22px;color:#777;font-size:12px;border-top:1px solid #f0f0f0; font-family:Arial,Helvetica,sans-serif;">
                  Merci de ne pas répondre directement à ce message automatique.
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </body>
    </html>
    """

def _attach_logo(related_part):
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

# --------------- Auth helpers ---------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_email"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper

def current_user():
    # retourne dict utilisateur (email, name, role) ou None
    ue = session.get("user_email")
    if not ue: 
        return None
    return USERS.get(ue.lower())


# -------------------------------------------------------------------
# Emails (admin, accusé, confirmation)
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
        f"Bonjour {demande['prenom']},\n\n"
        "📩 Nous avons bien reçu votre demande.\n"
        "⏳ Elle sera traitée dans les meilleurs délais.\n"
        "✅ Vous recevrez un mail lorsque votre demande aura été traitée.\n\n"
        "🙏 Merci de votre confiance,\n"
        "L'équipe Intégrale Academy\n"
    )
    html = _wrap_html(
        '<h1 style="margin:0 0 12px;font-size:20px;">📩 Accusé de réception</h1>',
        f"""
        <p>Bonjour <strong>{demande['prenom']}</strong>,</p>
        <p>📩 Nous avons bien reçu votre demande.</p>
        <p>⏳ Elle sera traitée dans les meilleurs délais.</p>
        <p style="margin:0">✅ Vous recevrez un mail lorsque votre demande aura été traitée.</p>
        <p style="margin:16px 0 0;">🙏 Merci de votre confiance,<br>L'équipe Intégrale Academy</p>
        """
    )
    send_email_html(demande["mail"], sujet, plain, html)

def envoyer_mail_confirmation(demande):
    sujet = "✅ Votre demande a été traitée — Intégrale Academy"
    repondre_url = url_for("repondre", demande_id=demande["id"], _external=True)

    plain = (
        f"Bonjour {demande['prenom']},\n\n"
        "✅ Votre demande a été traitée.\n\n"
        f"Motif : {demande['motif']}\n"
        f"Détails : {demande['details']}\n"
        f"✍️ Notre réponse : {demande.get('commentaire') or 'Aucun commentaire ajouté.'}\n"
        f"{'Des pièces jointes sont incluses.' if demande.get('pieces_jointes') else ''}\n\n"
        f"👉 Pour répondre, cliquez ici : {repondre_url}\n\n"
        "Cordialement,\n"
        "L'équipe Intégrale Academy\n"
    )

    body_html = f"""
      <p>Bonjour <strong>{demande['prenom']}</strong>,</p>
      <p style="margin:0 0 8px;">✅ <strong>Votre demande a été traitée.</strong></p>

      <table role="presentation" cellpadding="0" cellspacing="0" width="100%" 
             style="border-collapse:collapse;background:#f9fafb;border:1px solid #eef0f2;
                    border-radius:8px;margin-top:16px;">
        <tr>
          <td style="padding:12px 14px;font-family:Arial,Helvetica,sans-serif;
                     font-size:14px;color:#222;">
            <div style="margin:4px 0;"><strong>Motif :</strong> {demande['motif']}</div>
            <div style="margin:4px 0;"><strong>Détails :</strong> {demande['details']}</div>
            <div style="margin:12px 0;padding:12px;background:#fff8e5;
                        border:1px solid #f0dca6;border-radius:6px;">
              <strong>✍️ Notre réponse :</strong><br>
              {demande.get('commentaire') or 'Aucun commentaire ajouté.'}
            </div>
          </td>
        </tr>
      </table>

      <table role="presentation" cellpadding="0" cellspacing="0" width="100%" 
             style="margin:20px 0; text-align:center;">
        <tr>
          <td align="center">
            <a href="{repondre_url}" 
               style="display:inline-block;padding:14px 28px;background:#0d6efd;color:white;
                      text-decoration:none;border-radius:8px;font-weight:bold;font-size:15px;">
              📩 Répondre à ce message
            </a>
          </td>
        </tr>
      </table>

      {"<p style='margin:8px 0;'>Des pièces jointes sont incluses avec ce message.</p>" if demande.get("pieces_jointes") else ""}
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

def envoyer_mail_attribution_mohamed(demande):
    """Envoie un mail à znaw83@gmail.com + copie à Clément quand la demande est attribuée à Mohamed"""
    sujet = f"📩 Nouvelle demande attribuée à Mohamed — {demande.get('motif','')}"
    lien_admin = "https://assistance-alw9.onrender.com/admin"

    plain = (
        f"Une demande vient d'être attribuée à Mohamed.\n\n"
        f"👤 {demande.get('prenom','')} {demande.get('nom','')}\n"
        f"📧 {demande.get('mail','')}\n"
        f"📅 {demande.get('date','')}\n"
        f"📌 Motif : {demande.get('motif','')}\n"
        f"📝 Détails : {demande.get('details','')}\n\n"
        f"➡️ Connectez-vous à la plateforme pour la traiter : {lien_admin}"
    )

    html = _wrap_html(
        '<h1 style="margin:0 0 12px;font-size:20px;">📩 Nouvelle demande attribuée à Mohamed</h1>',
        f"""
        <p>Une demande vient d'être <strong>attribuée à Mohamed</strong>.</p>
        <table role="presentation" cellpadding="0" cellspacing="0" width="100%" 
               style="border-collapse:collapse;font-size:14px;">
          <tr><td style="padding:6px 8px;">👤 Nom</td><td>{demande.get('nom','')}</td></tr>
          <tr><td style="padding:6px 8px;">👤 Prénom</td><td>{demande.get('prenom','')}</td></tr>
          <tr><td style="padding:6px 8px;">📧 Email</td><td>{demande.get('mail','')}</td></tr>
          <tr><td style="padding:6px 8px;">📅 Date</td><td>{demande.get('date','')}</td></tr>
          <tr><td style="padding:6px 8px;">📌 Motif</td><td>{demande.get('motif','')}</td></tr>
          <tr><td style="padding:6px 8px;">📝 Détails</td><td>{demande.get('details','')}</td></tr>
        </table>
        <p style="margin-top:16px;">
          ➡️ <a href="{lien_admin}" style="color:#0d6efd;text-decoration:none;font-weight:bold;">
          Se connecter à la plateforme pour traiter la demande</a>
        </p>
        """
    )

    send_email_html("znaw83@gmail.com, clement@integraleacademy.com", sujet, plain, html)




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
            "reponses": [],
            "is_doublon": is_doublon
        }
        demandes.append(new_demande)
        save_data(data)

        try: envoyer_mail_admin(new_demande)
        except: pass
        try: envoyer_mail_accuse(new_demande)
        except: pass

        return redirect(url_for("confirmation"))

    return render_template("index.html")

@app.route("/confirmation")
def confirmation():
    return render_template("confirmation.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = USERS.get(email)
        if not user:
            flash("Identifiants incorrects", "error")
            return redirect(url_for("login"))
        # Comparaison simple (si tu utilises des hashes, remplace par check_password_hash)
        expected = user.get("pass")
        if expected and password == expected:
            # ok
            session["user_email"] = email
            session["user_name"] = user.get("name")
            session["user_role"] = user.get("role")
            next_url = request.args.get("next") or url_for("admin")
            return redirect(next_url)
        else:
            flash("Identifiants incorrects", "error")
            return redirect(url_for("login"))
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("user_email", None)
    session.pop("user_name", None)
    session.pop("user_role", None)
    return redirect(url_for("login"))
    

@app.route("/admin", methods=["GET", "POST"])
@login_required
def admin():
    data = load_data()
    demandes = data["demandes"]

    # ❌ On exclut les demandes de devis de l'admin principal
    demandes = [
        d for d in demandes
        if d.get("motif") != "Demande de devis détaillé"
    ]


    # 🔐 Identifier l'utilisateur connecté
    user = current_user()  # récupère les infos de session
    user_name = user["name"] if user else None
    user_role = user["role"] if user else "user"

    if request.method == "POST":
        action = request.form.get("action")
        demande_id = request.form.get("id")

        # ➕ Ajout manuel
        if action == "add":
            paris_tz = pytz.timezone("Europe/Paris")
            new_demande = {
                "id": str(uuid.uuid4()),
                "nom": "Vaillant",
                "prenom": "Clément",
                "telephone": request.form.get("telephone", ""),
                "mail": request.form.get("mail", "ecole@integraleacademy.com"),
                "motif": request.form.get("motif", "Autre"),
                "details": request.form.get("details", ""),
                "justificatif": "",
                "date": datetime.datetime.now(paris_tz).strftime("%d/%m/%Y %H:%M"),
                "attribution": request.form.get("attribution", "Clément"),
                "statut": "Non traité",
                "commentaire": "",
                "mail_confirme": "",
                "mail_erreur": "",
                "mail_contenu": "",
                "mail_html": "",
                "pieces_jointes": [],
                "reponses": [],
                "is_doublon": False,
                "rappel_date": request.form.get("rappel_date", "")

            }

            data["demandes"].append(new_demande)
            save_data(data)

            # 📨 Si la nouvelle demande est attribuée à Mohamed → envoyer le mail
            if new_demande["attribution"].strip() == "Mohamed":
                try:
                    envoyer_mail_attribution_mohamed(new_demande)
                    print(f"📨 Notification envoyée (ajout manuel) pour {new_demande['prenom']} {new_demande['nom']}")
                except Exception as e:
                    print("⚠️ Erreur envoi mail attribution Mohamed (ajout manuel) :", e)

            return redirect(url_for("admin"))



        # ✏️ Mise à jour d'une demande existante
        elif action == "update":
            for d in demandes:
                if d["id"] == demande_id:
                    d["mail"] = request.form.get("mail") or d["mail"]
                    new_details = request.form.get("details")
                    if new_details is not None:
                        d["details"] = new_details
                    d["commentaire"] = request.form.get("commentaire")
                    d["rappel_date"] = request.form.get("rappel_date", d.get("rappel_date", ""))
                    ancienne_attribution = d.get("attribution", "").strip()
                    nouvelle_attribution = request.form.get("attribution", "").strip()
                    d["attribution"] = nouvelle_attribution or ancienne_attribution

                    # 🔔 Notification automatique si attribution = Mohamed
                    if nouvelle_attribution == "Mohamed" and ancienne_attribution != "Mohamed":
                        try:
                            envoyer_mail_attribution_mohamed(d)
                            print(f"📨 Notification envoyée (attribution Mohamed) pour {d.get('prenom')} {d.get('nom')}")
                        except Exception as e:
                            print("⚠️ Erreur envoi mail attribution Mohamed :", e)

                    # ✅ Gestion du statut
                    ancien_statut = d.get("statut", "Non traité")
                    nouveau_statut = request.form.get("statut") or ancien_statut

                    # 📎 Upload de pièces jointes
                    if "pj" in request.files:
                        for f in request.files.getlist("pj"):
                            if f and f.filename:
                                filename = secure_filename(f.filename)
                                filepath = os.path.join(UPLOAD_FOLDER, filename)
                                f.save(filepath)
                                d.setdefault("pieces_jointes", [])
                                if filename not in d["pieces_jointes"]:
                                    d["pieces_jointes"].append(filename)

                    # 📨 Passage à "Traité" → envoi mail auto
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

        # ❌ Suppression d'une pièce jointe
        elif action == "delete_pj":
            pj_name = request.form.get("pj_name")
            if demande_id and pj_name:
                for d in demandes:
                    if d["id"] == demande_id and pj_name in d.get("pieces_jointes", []):
                        d["pieces_jointes"].remove(pj_name)
                        supprimer_fichier(pj_name)
                        break
            save_data(data)
            return redirect(url_for("admin"))

        # 🗑️ Archiver une demande
        elif action == "delete":
            to_remove = next((d for d in demandes if d["id"] == demande_id), None)
            if to_remove:
                data["archives"].append(to_remove)
                supprimer_fichier(to_remove.get("justificatif"))
                for pj in to_remove.get("pieces_jointes", []):
                    supprimer_fichier(pj)
                data["demandes"].remove(to_remove)
                save_data(data)
            return redirect(url_for("admin"))

        # 🧹 Archiver toutes les demandes traitées
        elif action == "delete_all_traitees":
            traitees = [d for d in demandes if d.get("statut") == "Traité"]
            for d in traitees:
                data["archives"].append(d)
                supprimer_fichier(d.get("justificatif"))
                for pj in d.get("pieces_jointes", []):
                    supprimer_fichier(pj)
                data["demandes"].remove(d)
            save_data(data)
            return redirect(url_for("admin"))

    # 🔍 Recherche (GET)
    query = request.args.get("q", "").strip().lower()
    if query:
        demandes = [
            d for d in demandes if
            query in d.get("nom", "").lower()
            or query in d.get("prenom", "").lower()
            or query in d.get("mail", "").lower()
            or query in d.get("motif", "").lower()
            or query in d.get("details", "").lower()
            or query in d.get("attribution", "").lower()
        ]

    if user_role != "admin" and user_name:
        demandes = [
            d for d in demandes
            if (d.get("attribution") or "").strip().lower() == user_name.lower()
        ]



    return render_template(
        "admin.html",
        demandes=demandes,
        compteur_traitees=data["compteur_traitees"],
        query=query
    )

@app.route("/admin-devis")
@login_required
def admin_devis():
    data = load_data()

    devis = []
    for d in data.get("demandes", []):
        if d.get("motif") == "Demande de devis détaillé":

            # 🔧 Parsing sécurisé du JSON "details"
            infos = {}
            try:
                infos = json.loads(d.get("details", "{}"))
            except:
                infos = {}

            d["infos"] = infos
            devis.append(d)

    return render_template("admin_devis.html", devis=devis)


@app.route("/admin-devis/toggle/<devis_id>", methods=["POST"])
@login_required
def toggle_devis(devis_id):
    data = load_data()

    for d in data.get("demandes", []):
        if d.get("id") == devis_id and d.get("motif") == "Demande de devis détaillé":
            if d.get("statut_devis") == "Envoyé":
                d["statut_devis"] = "A envoyer"
            else:
                d["statut_devis"] = "Envoyé"
            break

    save_data(data)
    return redirect(url_for("admin_devis"))





@app.route("/archives", methods=["GET", "POST"], endpoint="archives")
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

    # ✅ Recherche dans archives
    query = request.args.get("q", "").strip().lower()
    if query:
        archives = [
            a for a in archives if
            query in str(a.get("nom", "")).lower()
            or query in str(a.get("prenom", "")).lower()
            or query in str(a.get("mail", "")).lower()
            or query in str(a.get("motif", "")).lower()
            or query in str(a.get("details", "")).lower()
        ]

    return render_template("archives.html", archives=archives, query=query)


@app.route("/admin-devis/delete/<devis_id>", methods=["POST"])
@login_required
def delete_devis(devis_id):
    data = load_data()

    devis = next(
        (d for d in data.get("demandes", [])
         if d.get("id") == devis_id and d.get("motif") == "Demande de devis détaillé"),
        None
    )

    if devis:
        # Suppression du PDF s'il existe
        pdf = devis.get("pdf_path")
        if pdf and os.path.exists(pdf):
            try:
                os.remove(pdf)
            except:
                pass

        data["demandes"].remove(devis)
        save_data(data)

    return redirect(url_for("admin_devis"))


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

@app.route("/repondre/<demande_id>", methods=["GET", "POST"])
def repondre(demande_id):
    data = load_data()
    demande = next((d for d in data["demandes"] if d["id"] == demande_id), None)

    # Si plus dans demandes, chercher dans archives
    if not demande:
        demande = next((a for a in data["archives"] if a["id"] == demande_id), None)
        if not demande:
            return "Demande introuvable", 404
        if request.method == "POST":
            data["archives"].remove(demande)
            data["demandes"].append(demande)

    if request.method == "POST":
        message = request.form.get("message", "").strip()
        paris_tz = pytz.timezone("Europe/Paris")

        pj_files = []
        if "pj" in request.files:
            for f in request.files.getlist("pj"):
                if f and f.filename:
                    filename = secure_filename(f.filename)
                    f.save(os.path.join(UPLOAD_FOLDER, filename))
                    pj_files.append(filename)

        nouvelle_reponse = {
            "date": datetime.datetime.now(paris_tz).strftime("%d/%m/%Y %H:%M"),
            "message": message,
            "pj": pj_files
        }
        demande.setdefault("reponses", []).append(nouvelle_reponse)
        demande["statut"] = "Non traité"

        save_data(data)
        return render_template("merci_reponse.html", demande=demande)

    return render_template("repondre.html", demande=demande)

@app.route("/uploads/<filename>")
def download_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

    # ------------------------------------------------------------
# ✅ Route publique pour la plateforme principale (suivi assistance)
# ------------------------------------------------------------
@app.route("/data.json")
def data_json():
    """
    Retourne le nombre de demandes à traiter (statut 'A TRAITER' ou 'Non traité')
    """
    try:
        data = load_data()
        demandes = data.get("demandes", [])
        a_traiter = [d for d in demandes if d.get("statut", "").strip().lower() in ["a traiter", "non traité"]]
        count = len(a_traiter)

        headers = {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*"
        }
        return {"a_traiter": count}, 200, headers

    except Exception as e:
        print("⚠️ Erreur /data.json :", e)
        return {"a_traiter": -1, "error": str(e)}, 500, {
            "Access-Control-Allow-Origin": "*"
        }
# ------------------------------------------------------------
# 🧩 PAGE : Demande de rappel téléphonique
# ------------------------------------------------------------
@app.route("/rappel", methods=["GET", "POST"])
def rappel():
    data = load_data()

    if request.method == "POST":
        paris_tz = pytz.timezone("Europe/Paris")

        nom = request.form.get("nom", "").strip()
        prenom = request.form.get("prenom", "").strip()
        mail = request.form.get("mail", "").strip()
        telephone = request.form.get("telephone", "").strip()
        formation = request.form.get("formation", "").strip()
        commentaire = request.form.get("commentaire", "").strip()
        plage = request.form.get("plage", "").strip()

        # 💾 Enregistrement dans data.json
        new_demande = {
            "id": str(uuid.uuid4()),
            "nom": nom,
            "prenom": prenom,
            "telephone": telephone,
            "mail": mail,
            "motif": f"Demande de rappel – {formation}",
            "details": f"Créée via le formulaire de rappel.\nPréférence horaire : {plage}\n{commentaire}",
            "justificatif": "",
            "date": datetime.datetime.now(paris_tz).strftime("%d/%m/%Y %H:%M"),
            "attribution": "Mohamed",
            "statut": "A rappeler",
            "commentaire": "",
            "mail_confirme": "",
            "mail_erreur": "",
            "mail_contenu": "",
            "mail_html": "",
            "pieces_jointes": [],
            "reponses": [],
            "is_doublon": False,
            "plage": plage
        }

        data["demandes"].append(new_demande)
        save_data(data)

        # 📨 Accusé de réception au candidat
        try:
            sujet = "📞 Nous avons bien reçu votre demande de rappel"
            plain = (
                f"Bonjour {prenom},\n\n"
                f"Nous avons bien reçu votre demande de rappel concernant la formation : {formation}.\n"
                f"Notre équipe vous contactera prochainement au {telephone}.\n\n"
                "Merci pour votre intérêt et à très bientôt !\n"
                "— L'équipe Intégrale Academy"
            )

            html = _wrap_html(
                '<h1 style="margin:0 0 12px;font-size:20px;">📞 Demande de rappel reçue</h1>',
                f"""
                <p>Bonjour <strong>{prenom}</strong>,</p>
                <p>Nous avons bien reçu votre demande de rappel concernant la formation :</p>
                <p><strong>{formation}</strong></p>
                <p>Notre équipe vous contactera prochainement au <strong>{telephone}</strong>.</p>
                <p style="margin-top:10px;">Merci pour votre intérêt et à très bientôt !<br>— L'équipe Intégrale Academy</p>
                """
            )
            send_email_html(mail, sujet, plain, html)
        except Exception as e:
            print("⚠️ Erreur envoi mail rappel :", e)

        # 📨 Notification interne à Mohamed
        try:
            envoyer_mail_attribution_mohamed(new_demande)
        except Exception as e:
            print("⚠️ Erreur envoi mail Mohamed :", e)

        # 🔁 Redirections automatiques selon la formation choisie
        if formation == "Chauffeur VTC":
            return redirect("https://www.integraleacademy.com/rdvvtc")
        elif formation == "Dirigeant d'entreprise de sécurité privée (DESP)":
            return redirect("https://www.integraleacademy.com/rdvconfirmedirigeant")
        else:
            return render_template("confirmation.html")

    return render_template("rappel.html")

@app.route("/hebergement", methods=["GET", "POST"])
def hebergement():
    data = load_data()
    paris_tz = pytz.timezone("Europe/Paris")

    if request.method == "POST":
        nom = request.form.get("nom", "").strip()
        prenom = request.form.get("prenom", "").strip()
        telephone = request.form.get("telephone", "").strip()
        mail = request.form.get("email", "").strip()
        session = request.form.get("session", "").strip()

        # 🚫 LIMITE DE 10 PLACES PAR SESSION (SÉCURISÉ CÔTÉ SERVEUR)
        nb_places_session = len([
            h for h in data.get("hebergements", [])
            if h.get("session") == session
        ])

        if nb_places_session >= 10:
            return render_template(
                "hebergement.html",
                erreur_session="❌ Notre hébergement est complet (10 places déjà réservées sur 10 places disponibles)."
            )

        # ✅ ENREGISTREMENT
        new_resa = {
            "id": str(uuid.uuid4()),
            "nom": nom,
            "prenom": prenom,
            "telephone": telephone,
            "session": session,
            "mail": mail,
            "cle_numero": "",
            "cle_etat": "A donner",
            "date": datetime.datetime.now(paris_tz).strftime("%d/%m/%Y %H:%M"),
            "paiement": "Non payé",
            "mode_paiement": "",
            "date_paiement": ""
        }

        data["hebergements"].append(new_resa)
        save_data(data)

        # --- Mail candidat ---
        subject = "🏨 Votre réservation d’hébergement a bien été enregistrée"
        plain = (
            f"Bonjour {prenom},\n\n"
            "Votre demande de réservation d’hébergement pour votre formation APR a bien été prise en compte.\n\n"
            f"📅 Dates sélectionnées : {session}\n"
            "💶 Le paiement de 300€ devra être effectué lors de votre arrivée (chèque ou espèces), à préparer dans une enveloppe portant votre nom et votre prénom.\n\n"
            "À très bientôt,\nIntégrale Academy"
        )

        html = _wrap_html(
            "<h2>🏨 Réservation d’hébergement confirmée</h2>",
            f"""
            <p>Bonjour <strong>{prenom}</strong>,</p>
            <p>Nous avons bien pris en compte votre réservation (hébergement formation Agent de Protection Physique des Personnes).</p>
            <p><strong>📅 Dates :</strong> {session}</p>
            <p><strong>💶 Le paiement de 300€ devra être effectué lors de votre arrivée (chèque ou espèces), à préparer dans une enveloppe portant votre nom et votre prénom.</p>
            <p>L’équipe Intégrale Academy</p>
            """
        )

        try:
            send_email_html(mail, subject, plain, html)
        except:
            pass

        # --- Mail admin ---
        try:
            send_email_html(
                "ecole@integraleacademy.com, clement@integraleacademy.com",
                f"🏨 Nouvelle réservation hébergement – {prenom} {nom}",
                plain,
                html
            )
        except:
            pass

        return redirect(url_for("hebergement_confirmation"))

    return render_template("hebergement.html")



@app.route("/hebergement_confirmation")
def hebergement_confirmation():
    return render_template("hebergement_confirmation.html")


@app.route("/admin_hebergement", methods=["GET", "POST"])
@login_required
def admin_hebergement():
    data = load_data()
    hebergements = data.get("hebergements", [])

    # 🔍 Recherche
    q = request.args.get("q", "").strip().lower()
    if q:
        hebergements = [
            h for h in hebergements
            if q in h.get("nom", "").lower()
            or q in h.get("prenom", "").lower()
            or q in h.get("mail", "").lower()
            or q in h.get("session", "").lower()
        ]

    # 🔽 Tri
    tri = request.args.get("tri")
    if tri == "session":
        hebergements = sorted(hebergements, key=lambda x: x.get("session", ""))

    # ------------------------------------------------------------------
    # 🟢 MISE À JOUR DES RÉSERVATIONS (POST)
    # ------------------------------------------------------------------
    if request.method == "POST":
        action = request.form.get("action")
        resa_id = request.form.get("id")

        # On parcourt TOUS les hébergements (pas filtrés)
        for h in data["hebergements"]:
            if h["id"] == resa_id:

                # 🔑 Numéro de clé
                if action == "cle_numero":
                    h["cle_numero"] = request.form.get("value", "")
                    save_data(data)
                    return "", 204

                # 🔑 État de clé
                if action == "cle_etat":
                    h["cle_etat"] = request.form.get("value", "")
                    save_data(data)
                    return "", 204

                # 🗑️ Supprimer
                if action == "delete":
                    data["hebergements"].remove(h)
                    save_data(data)
                    return redirect(url_for("admin_hebergement"))

                # 💵 Paiement (Payé / Non payé)
                if action == "paiement":
                    h["paiement"] = request.form.get("value")
                    if h["paiement"] == "Payé":
                        paris_tz = pytz.timezone("Europe/Paris")
                        h["date_paiement"] = datetime.datetime.now(paris_tz).strftime("%d/%m/%Y %H:%M")
                    else:
                        h["date_paiement"] = ""
                    save_data(data)
                    return "", 204

                # 💳 Mode de paiement
                if action == "mode":
                    h["mode_paiement"] = request.form.get("value")
                    save_data(data)
                    return "", 204

                # ✏️ Mise à jour générique (nom, prénom, téléphone, mail, session…)
                if action == "update_field":
                    field = request.form.get("field")
                    value = request.form.get("value", "").strip()

                    # Champs autorisés à être modifiés
                    allowed = {"nom", "prenom", "telephone", "mail", "session"}

                    if field not in allowed:
                        return "Champ non autorisé", 400

                    h[field] = value
                    save_data(data)
                    return "", 204


        save_data(data)



    # ------------------------------------------------------------------

    return render_template("admin_hebergement.html", hebergements=hebergements)


# ------------------------------------------------------------
# 🏨 API publique pour la plateforme principale : hébergement
# ------------------------------------------------------------
@app.route("/hebergement_data.json")
def hebergement_data():
    try:
        data = load_data()
        hebergements = data.get("hebergements", [])

        total = len(hebergements)
        non_payes = len([h for h in hebergements if h.get("paiement") != "Payé"])
        payes = total - non_payes

        headers = {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*"
        }

        return {
            "total": total,
            "payes": payes,
            "non_payes": non_payes
        }, 200, headers

    except Exception as e:
        return {
            "total": -1,
            "error": str(e)
        }, 500, {"Access-Control-Allow-Origin": "*"}


@app.route("/demande-devis", methods=["GET", "POST"])
def demande_devis():
    if request.method == "POST":
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.platypus import Table, TableStyle
        from reportlab.lib import colors
        from dateutil.relativedelta import relativedelta

        data = request.form.to_dict()

        # =========================
        # DATE DU DEVIS (AVANT TOUT)
        # =========================
        date_devis = datetime.date.today()

        # =========================
        # TARIFS
        # =========================
        TARIFS = {
            "A3P": 4200,
            "APS": 1650,
            "VTC": 1600,
            "DESP_INIT": 4300,
            "DESP_VAE": 3800
        }

        formation = data.get("formation")
        tarif = TARIFS.get(formation, 0)
        try:
            cpf = int(float((data.get("cpf_montant") or "0").replace(",", ".").replace(" ", "")))
        except:
            cpf = 0

        reste = max(tarif - cpf, 0)

        # =========================
        # PARSING DATE DÉBUT
        # =========================
        date_debut = None
        try:
            import re
            mois_fr = {
                "janvier": 1, "février": 2, "mars": 3, "avril": 4,
                "mai": 5, "juin": 6, "juillet": 7, "août": 8,
                "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12
            }
            txt = data.get("dates", "")
            debut = txt.split("au")[0].strip()
            jour, mois = debut.split()
            annee = int(re.search(r"(20\d{2})", txt).group(1))
            date_debut = datetime.date(annee, mois_fr[mois.lower()], int(jour))
        except:
            pass

        if not date_debut:
            date_debut = date_devis

        # =========================
        # PREMIER PRÉLÈVEMENT
        # - min 15 jours après devis
        # - toujours le 5
        # =========================
        date_min = date_devis + datetime.timedelta(days=15)

        if date_min.day <= 5:
            first_prelevement = date_min.replace(day=5)
        else:
            first_prelevement = (
                date_min.replace(day=1) + relativedelta(months=1)
            ).replace(day=5)

        # =========================
        # ÉCHÉANCIERS
        # =========================
        echeanciers = {}

        for n in [2, 3, 4, 5]:
            montant_base = round(reste / n, 2)
            montants = [montant_base] * n

            ecart = round(reste - sum(montants), 2)
            montants[-1] += ecart

            dates = []
            for i in range(n):
                d = first_prelevement + relativedelta(months=i)
                d = d.replace(day=5)
                dates.append({
                    "date": d,
                    "montant": montants[i]
                })

            echeanciers[n] = {
                "montants": montants,
                "echeances": dates
            }

        # =========================
        # PDF
        # =========================
        pdf_path = f"/mnt/data/devis_{uuid.uuid4().hex}.pdf"
        c = canvas.Canvas(pdf_path, pagesize=A4)
        width, height = A4
        y = height - 40

        logo = os.path.join(app.root_path, "static", "logo.png")
        if os.path.exists(logo):
            c.drawImage(logo, 40, y-60, width=140, mask="auto")
        y -= 90

        c.setFont("Helvetica-Bold", 20)
        c.drawCentredString(width/2, y, "DEVIS & PLAN DE FINANCEMENT")
        y -= 40

        c.setFont("Helvetica", 10)
        c.drawCentredString(
            width / 2,
            y,
            f"Date d’émission du devis : {date_devis.strftime('%d/%m/%Y')}"
        )
        y -= 25

        def v(key):
            val = (data.get(key) or "").strip()
            return val if val else "—"

        def yn(key):
            val = (data.get(key) or "").strip().upper()
            return val if val in ("OUI", "NON") else (val or "—")


        # =========================
        # INFOS STAGIAIRE – FORMULAIRE COMPLET
        # =========================
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, y, "Informations stagiaire")
        y -= 20
        c.setFont("Helvetica", 11)
        
        lignes = [
            ("Nom", v("nom")),
            ("Prénom", v("prenom")),
            ("Téléphone", v("telephone")),
            ("Email", v("mail")),
            ("Confirmation email", v("mail_confirm")),
        
            ("Formation", v("formation")),
            ("Session / Dates", v("dates")),
        
            ("CPF consulté", yn("cpf_consulte")),
            ("Montant CPF", f"{v('cpf_montant')} €"),
        
            ("France Travail", yn("france_travail")),
            ("Si refus France Travail : financement personnel", yn("ft_refus_ok")),
            ("Financement personnel / fonds disponibles", yn("financement_perso")),
        
            ("Identité Numérique La Poste", yn("identite_numerique")),
        ]
        
        for label, value in lignes:
            if y < 120:
                c.showPage()
                y = height - 60
                c.setFont("Helvetica", 11)
            c.drawString(40, y, f"{label} : {value}")
            y -= 14
        
        # =========================
        # CNAPS
        # =========================
        y -= 10
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, y, "Situation CNAPS")
        y -= 20
        c.setFont("Helvetica", 11)
        
        cnaps = [
            ("Carte professionnelle CNAPS valide", yn("cnaps_ok")),
            ("Garde à vue / prise d’empreintes", yn("garde_vue")),
            ("Titulaire d’un titre de séjour", yn("titre_sejour")),
        ]
        
        for label, value in cnaps:
            if y < 120:
                c.showPage()
                y = height - 60
                c.setFont("Helvetica", 11)
            c.drawString(40, y, f"{label} : {value}")
            y -= 14


        # =========================
        # RÉCAP FINANCIER
        # =========================
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, y, "Récapitulatif financier")
        y -= 20

        c.setFont("Helvetica", 11)
        c.drawString(40, y, f"Prix formation : {tarif} €")
        y -= 14
        c.drawString(40, y, f"Montant CPF : {cpf} €")
        y -= 14
        c.setFont("Helvetica-Bold", 11)
        c.drawString(40, y, f"Reste à charge : {reste} €")
        y -= 30

        c.setFont("Helvetica-Oblique", 9)
        c.drawString(
            40,
            y,
            "Prélèvements effectués le 5 de chaque mois. "
            "Premier prélèvement au minimum 15 jours après la date du devis."
        )
        y -= 30

        # =========================
        # TABLEAUX DE PAIEMENT
        # =========================
        for n in sorted(echeanciers.keys()):
            plan = echeanciers[n]

            if y < 300:
                c.showPage()
                y = height - 60

            c.setFont("Helvetica-Bold", 14)
            c.drawString(40, y, f"Option {n} paiements – prélèvements mensuels")
            y -= 20

            table_data = [["Date de prélèvement", "Montant"]]
            for e in plan["echeances"]:
                table_data.append([
                    e["date"].strftime("%d/%m/%Y"),
                    f"{e['montant']:.2f} €"
                ])

            table = Table(table_data, colWidths=[260, 120])
            table.setStyle(TableStyle([
                ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#f0f0f0")),
                ("GRID", (0,0), (-1,-1), 0.6, colors.grey),
                ("FONT", (0,0), (-1,0), "Helvetica-Bold"),
                ("ALIGN", (1,1), (-1,-1), "RIGHT"),
                ("PADDING", (0,0), (-1,-1), 8),
            ]))

            tw, th = table.wrap(0, 0)
            table.drawOn(c, 40, y - th)
            y -= th + 30

        c.save()

        # =========================
        # SAUVEGARDE + MAIL
        # =========================
        data_store = load_data()
        data_store["demandes"].append({
            "id": str(uuid.uuid4()),
            "nom": data.get("nom"),
            "prenom": data.get("prenom"),
            "telephone": data.get("telephone"),
            "mail": data.get("mail"),
            "motif": "Demande de devis détaillé",
            "details": json.dumps(data, ensure_ascii=False),
            "date": datetime.datetime.now(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M"),
            "statut": "Non traité",
            "attribution": "",
            "commentaire": "",
            "mail_confirme": "",
            "mail_erreur": "",
            "mail_contenu": "",
            "mail_html": "",
            "pieces_jointes": [],
            "reponses": [],
            "is_doublon": False,
            "rappel_date": "",
            "plage": "",
            "statut_devis": "A envoyer",
            "pdf_path": pdf_path
        })

        save_data(data_store)

        send_email_html(
            "clement@integraleacademy.com",
            "Demande de devis – Intégrale Academy",
            "PDF en pièce jointe",
            _wrap_html("<h2>Nouvelle demande de devis</h2>", "<p>Devis en pièce jointe.</p>"),
            attachments_paths=[pdf_path]
        )

        ultra = (
            data.get("cpf_consulte") == "OUI" and
            data.get("france_travail") == "NON" and
            data.get("financement_perso") == "OUI" and
            data.get("identite_numerique") == "OUI"
        )

        return redirect(url_for("confirmation_devis", ultra="1" if ultra else "0"))

    return render_template("demande_devis.html")





@app.route("/confirmation-devis")
def confirmation_devis():
    ultra = request.args.get("ultra") == "1"
    return render_template("confirmation_devis.html", ultra=ultra)

@app.route("/admin-devis/pdf/<devis_id>")
@login_required
def voir_pdf_devis(devis_id):
    data = load_data()

    devis = next(
        (d for d in data.get("demandes", [])
         if d.get("id") == devis_id and d.get("motif") == "Demande de devis détaillé"),
        None
    )

    if not devis:
        abort(404)

    pdf_path = devis.get("pdf_path")
    if not pdf_path or not os.path.exists(pdf_path):
        abort(404)

    return send_from_directory(
        os.path.dirname(pdf_path),
        os.path.basename(pdf_path)
    )

@app.route("/devis_data.json")
def devis_data():
    try:
        data = load_data()
        demandes = data.get("demandes", [])

        devis_a_envoyer = [
            d for d in demandes
            if d.get("motif") == "Demande de devis détaillé"
            and d.get("statut_devis") == "A envoyer"
        ]

        return {
            "a_envoyer": len(devis_a_envoyer)
        }, 200, {
            "Access-Control-Allow-Origin": "*"
        }

    except Exception as e:
        return {
            "a_envoyer": -1,
            "error": str(e)
        }, 500, {
            "Access-Control-Allow-Origin": "*"
        }




        



if __name__ == "__main__":
    app.run(debug=True)
