from flask import Flask, render_template, request, send_from_directory, url_for
import json, os, datetime, uuid, pytz, smtplib
from email.mime.text import MIMEText
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Dossier persistant Render
DATA_FILE = "/mnt/data/data.json"
UPLOAD_FOLDER = "/mnt/data/uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

# 📩 Mail à l’admin
def envoyer_mail(demande):
    sujet = f"Nouvelle demande stagiaire - {demande['motif']}"
    contenu = f"""
    Nouvelle demande reçue :

    Nom : {demande['nom']}
    Prénom : {demande['prenom']}
    Téléphone : {demande['telephone']}
    Email : {demande['mail']}
    Motif : {demande['motif']}
    Détails : {demande['details']}
    Date : {demande['date']}
    """

    if demande["justificatif"]:
        contenu += f"\nJustificatif : {url_for('download_file', filename=demande['justificatif'], _external=True)}"

    msg = MIMEText(contenu, "plain", "utf-8")
    msg["Subject"] = sujet
    msg["From"] = os.getenv("SMTP_USER")
    msg["To"] = "elsaduq83@gmail.com, ecole@integraleacademy.com"

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as serveur:
            serveur.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
            serveur.send_message(msg)
        print("✅ Mail envoyé aux admins")
    except Exception as e:
        print("❌ Erreur envoi mail admin :", e)

# 📩 Mail au stagiaire - accusé de réception
def envoyer_mail_accuse(demande):
    sujet = "Accusé de réception - Intégrale Academy"
    contenu = f"""
    Bonjour {demande['prenom']} {demande['nom']},

    📩 Nous avons bien reçu votre demande.  
    ⏳ Elle sera traitée dans les meilleurs délais.  

    ✅ Vous recevrez un mail lorsque votre demande aura été traitée.  

    Merci à vous,  
    L'équipe Intégrale Academy
    """

    msg = MIMEText(contenu, "plain", "utf-8")
    msg["Subject"] = sujet
    msg["From"] = os.getenv("SMTP_USER")
    msg["To"] = demande["mail"]

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as serveur:
            serveur.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
            serveur.send_message(msg)
        print(f"✅ Accusé de réception envoyé à {demande['mail']}")
    except Exception as e:
        print("❌ Erreur envoi mail accusé :", e)

# 📩 Mail au stagiaire quand traité (avec commentaire)
def envoyer_mail_confirmation(demande):
    sujet = "Votre demande a été traitée - Intégrale Academy"
    contenu = f"""
    Bonjour {demande['prenom']} {demande['nom']},

    ✅ Nous vous informons que votre demande a été traitée.

    📌 Motif : {demande['motif']}
    📝 Détails : {demande['details']}
    💬 Commentaire : {demande['commentaire'] if demande['commentaire'] else "Aucun commentaire ajouté."}

    Cordialement,  
    L'équipe Intégrale Academy
    """

    msg = MIMEText(contenu, "plain", "utf-8")
    msg["Subject"] = sujet
    msg["From"] = os.getenv("SMTP_USER")
    msg["To"] = demande["mail"]

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as serveur:
            serveur.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
            serveur.send_message(msg)
        print(f"✅ Mail de confirmation envoyé à {demande['mail']}")
        demande["mail_contenu"] = contenu  # ✅ stocke le contenu du mail
        return True
    except Exception as e:
        print("❌ Erreur envoi mail confirmation :", e)
        return False

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        demandes = load_data()
        paris_tz = pytz.timezone("Europe/Paris")

        justificatif_filename = ""
        if "justificatif" in request.files:
            f = request.files["justificatif"]
            if f and f.filename != "":
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
            "mail_contenu": ""
        }
        demandes.append(new_demande)
        save_data(demandes)

        envoyer_mail(new_demande)        # mail admin
        envoyer_mail_accuse(new_demande) # accusé stagiaire

        return render_template("confirmation.html")
    return render_template("index.html")

@app.route("/admin", methods=["GET", "POST"])
def admin():
    demandes = load_data()
    if request.method == "POST":
        action = request.form.get("action")
        demande_id = request.form.get("id")

        if action == "update":
            paris_tz = pytz.timezone("Europe/Paris")
            for d in demandes:
                if d["id"] == demande_id:
                    d["attribution"] = request.form.get("attribution")
                    d["mail"] = request.form.get("mail") or d["mail"]  # ✅ sauvegarde modif email
                    d["details"] = request.form.get("details")
                    d["commentaire"] = request.form.get("commentaire")
                    nouveau_statut = request.form.get("statut")

                    # Si statut passe à "Traité"
                    if d["statut"] != "Traité" and nouveau_statut == "Traité":
                        if envoyer_mail_confirmation(d):
                            d["mail_confirme"] = datetime.datetime.now(paris_tz).strftime("%d/%m/%Y %H:%M")
                            d["mail_erreur"] = ""
                        else:
                            d["mail_confirme"] = ""
                            d["mail_erreur"] = "❌ Erreur lors de l'envoi du mail"

                    d["statut"] = nouveau_statut

            save_data(demandes)  # ✅ sauvegarde bien après chaque modif

        elif action == "delete":
            demandes = [d for d in demandes if d["id"] != demande_id]
            save_data(demandes)

    return render_template("admin.html", demandes=demandes)

@app.route("/imprimer/<demande_id>")
def imprimer(demande_id):
    demandes = load_data()
    demande = next((d for d in demandes if d["id"] == demande_id), None)
    return render_template("imprimer.html", demande=demande)

@app.route("/uploads/<filename>")
def download_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

# 🔎 Voir le contenu du mail envoyé
@app.route("/voir_mail/<demande_id>")
def voir_mail(demande_id):
    demandes = load_data()
    demande = next((d for d in demandes if d["id"] == demande_id), None)
    return render_template("voir_mail.html", demande=demande)

# 🔄 API pour l’auto-refresh (avec anti-cache)
@app.route("/api/demandes")
def api_demandes():
    demandes = load_data()
    response = app.response_class(
        response=json.dumps(demandes, ensure_ascii=False),
        status=200,
        mimetype="application/json"
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

if __name__ == "__main__":
    app.run(debug=True)
