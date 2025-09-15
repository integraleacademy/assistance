from flask import Flask, render_template, request
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
        contenu += f"\nJustificatif : {demande['justificatif']}"

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

# 📩 Mail au stagiaire quand traité
def envoyer_mail_confirmation(demande):
    sujet = "Votre demande a été traitée - Intégrale Academy"
    contenu = f"""
    Bonjour {demande['prenom']} {demande['nom']},

    Nous vous informons que votre demande a été traitée.

    📌 Motif : {demande['motif']}
    📝 Détails : {demande['details']}

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
    except Exception as e:
        print("❌ Erreur envoi mail confirmation :", e)

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        demandes = load_data()
        paris_tz = pytz.timezone("Europe/Paris")

        justificatif_path = ""
        if "justificatif" in request.files:
            f = request.files["justificatif"]
            if f and f.filename != "":
                filename = secure_filename(f.filename)
                justificatif_path = os.path.join(UPLOAD_FOLDER, filename)
                f.save(justificatif_path)

        new_demande = {
            "id": str(uuid.uuid4()),
            "nom": request.form["nom"],
            "prenom": request.form["prenom"],
            "telephone": request.form["telephone"],
            "mail": request.form["mail"],
            "motif": request.form["motif"],
            "details": request.form["details"],
            "justificatif": justificatif_path,
            "date": datetime.datetime.now(paris_tz).strftime("%d/%m/%Y %H:%M"),
            "attribution": "",
            "statut": "Non traité",
            "commentaire": ""
        }
        demandes.append(new_demande)
        save_data(demandes)

        envoyer_mail(new_demande)

        return render_template("confirmation.html")
    return render_template("index.html")

@app.route("/admin", methods=["GET", "POST"])
def admin():
    demandes = load_data()
    if request.method == "POST":
        action = request.form.get("action")
        demande_id = request.form.get("id")

        if action == "update":
            for d in demandes:
                if d["id"] == demande_id:
                    d["attribution"] = request.form.get("attribution")
                    nouveau_statut = request.form.get("statut")
                    d["commentaire"] = request.form.get("commentaire")

                    # Si statut passe à "Traité", envoi mail stagiaire
                    if d["statut"] != "Traité" and nouveau_statut == "Traité":
                        envoyer_mail_confirmation(d)

                    d["statut"] = nouveau_statut
            save_data(demandes)

        elif action == "delete":
            demandes = [d for d in demandes if d["id"] != demande_id]
            save_data(demandes)

    return render_template("admin.html", demandes=demandes)

@app.route("/imprimer/<demande_id>")
def imprimer(demande_id):
    demandes = load_data()
    demande = next((d for d in demandes if d["id"] == demande_id), None)
    return render_template("imprimer.html", demande=demande)

if __name__ == "__main__":
    app.run(debug=True)
