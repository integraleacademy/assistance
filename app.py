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
# Email helper
# -------------------------------------------------------------------
def _brand_header_table():
    return """
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
        <tr>
          <td align="center" style="padding:16px 16px 8px 16px;margin:0;">
            <img src="cid:logo_cid" alt="Intégrale Academy"
                 height="56"
                 style="display:block;height:56px;width:auto;max-width:220px;">
          </td>
        </tr>
        <tr>
          <td align="center" style="padding:0 16px 10px 16px;margin:0;
                                    font-weight:700;font-size:16px;color:#111;">
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
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="border-collapse:collapse;background:#f7f7f7;">
      <tr>
        <td align="center" style="padding:24px;">
          <table role="presentation" cellpadding="0" cellspacing="0" width="100%"
                 style="border-collapse:collapse;max-width:600px;width:100%;
                        background:#ffffff;border:1px solid #eeeeee;border-radius:12px;overflow:hidden;">
            <tr><td style="padding:0;">{_brand_header_table()}</td></tr>
            <tr>
              <td style="padding:22px;">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                  <tr><td style="font-family:Arial,Helvetica,sans-serif;">{title_html}</td></tr>
                </table>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                  <tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:#222;">
                      {body_html}
                  </td></tr>
                </table>
              </td>
            </tr>
            <tr>
              <td style="padding:12px 22px;color:#777;font-size:12px;border-top:1px solid #f0f0f0;
                         font-family:Arial,Helvetica,sans-serif;">
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

# -------------------------------------------------------------------
# Emails
# -------------------------------------------------------------------
def envoyer_mail_admin(demande):
    sujet = f"🆕 Nouvelle demande stagiaire — {demande['motif']}"
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

    rows = f"""
      <tr><td style="padding:6px 0;width:120px;color:#555;">👤 Nom</td>
          <td style="padding:6px 0;"><strong>{demande['nom']}</strong></td></tr>
      <tr><td style="padding:6px 0;width:120px;color:#555;">👤 Prénom</td>
          <td style="padding:6px 0;"><strong>{demande['prenom']}</strong></td></tr>
      <tr><td style="padding:6px 0;width:120px;color:#555;">📞 Téléphone</td>
          <td style="padding:6px 0;">{demande['telephone']}</td></tr>
      <tr><td style="padding:6px 0;width:120px;color:#555;">✉️ Email</td>
          <td style="padding:6px 0;">{demande['mail']}</td></tr>
      <tr><td style="padding:6px 0;width:120px;color:#555;">📌 Motif</td>
          <td style="padding:6px 0;">{demande['motif']}</td></tr>
      <tr><td style="padding:6px 0;width:120px;color:#555;">📝 Détails</td>
          <td style="padding:6px 0;">{demande['details']}</td></tr>
      <tr><td style="padding:6px 0;width:120px;color:#555;">📅 Date</td>
          <td style="padding:6px 0;">{demande['date']}</td></tr>
    """
    if demande.get("justificatif"):
        link = url_for('download_file', filename=demande['justificatif'], _external=True)
        rows += f"""<tr><td style="padding:6px 0;width:120px;color:#555;">📎 Justificatif</td>
                    <td style="padding:6px 0;"><a href="{link}" style="color:#0d6efd;text-decoration:none;">Télécharger</a></td></tr>"""

    html = _wrap_html(
        '<h1 style="margin:0 0 12px;font-size:20px;">🆕 Nouvelle demande stagiaire</h1>',
        f"<p>Une nouvelle demande a été soumise sur le site.</p><table width='100%'>{rows}</table>"
    )
    send_email_html("elsaduq83@gmail.com, ecole@integraleacademy.com", sujet, plain, html)

def envoyer_mail_accuse(demande):
    sujet = "📩 Accusé de réception — Intégrale Academy"
    plain = f"Bonjour {demande['prenom']} {demande['nom']},\n\nNous avons bien reçu votre demande."
    html = _wrap_html(
        '<h1 style="margin:0 0 12px;font-size:20px;">📩 Accusé de réception</h1>',
        f"<p>Bonjour <strong>{demande['prenom']} {demande['nom']}</strong>,</p><p>Nous avons bien reçu votre demande.</p>"
    )
    send_email_html(demande["mail"], sujet, plain, html)

def envoyer_mail_confirmation(demande):
    sujet = "✅ Votre demande a été traitée — Intégrale Academy"
    plain = (
        f"Bonjour {demande['prenom']} {demande['nom']},\n\n"
        "Votre demande a été traitée.\n\n"
        f"Motif : {demande['motif']}\n"
        f"Détails : {demande['details']}\n"
        f"Notre réponse : {demande.get('commentaire') or 'Aucune réponse ajoutée.'}\n"
    )

    commentaire_html = f"""
      <div style="margin:12px 0;padding:12px;background:#fff8e5;
                  border:1px solid #f0dca6;border-radius:6px;font-size:14px;color:#333;">
        <strong>✍️ Notre réponse :</strong><br>{demande.get('commentaire') or 'Aucune réponse ajoutée.'}
      </div>
    """

    body_html = f"""
      <p>Bonjour <strong>{demande['prenom']} {demande['nom']}</strong>,</p>
      <p>✅ Votre demande a été traitée.</p>
      <table style="background:#f9fafb;border:1px solid #eef0f2;border-radius:8px;width:100%;">
        <tr><td style="padding:12px;"><strong>📌 Motif :</strong> {demande['motif']}<br>
                <strong>📝 Détails :</strong> {demande['details']}</td></tr>
      </table>
      {commentaire_html}
      <p>Cordialement,<br>L'équipe Intégrale Academy</p>
    """
    html = _wrap_html('<h1 style="margin:0 0 12px;font-size:20px;">✅ Demande traitée</h1>', body_html)
    pj_paths = []
    for pj in demande.get("pieces_jointes", []):
        chemin = os.path.join(UPLOAD_FOLDER, pj)
        if os.path.exists(chemin):
            pj_paths.append(chemin)
    send_email_html(demande["mail"], sujet, plain, html, attachments_paths=pj_paths)

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
            "statut": "Non traité",
            "commentaire": "",
            "pieces_jointes": []
        }
        demandes.append(new_demande)
        save_data(data)
        envoyer_mail_admin(new_demande)
        envoyer_mail_accuse(new_demande)
        return render_template("confirmation.html")
    return render_template("index.html")

@app.route("/uploads/<filename>")
def download_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

if __name__ == "__main__":
    app.run(debug=True)
