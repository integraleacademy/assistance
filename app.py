from flask import Flask, render_template, request, send_from_directory, url_for
import json, os, datetime, uuid, pytz, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Dossier persistant Render
DATA_FILE = "/mnt/data/data.json"
UPLOAD_FOLDER = "/mnt/data/uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# -----------------------
# Fonctions utilitaires
# -----------------------
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):  # nouvelle structure
                if "demandes" not in data:
                    data["demandes"] = []
                if "compteur_traitees" not in data:
                    data["compteur_traitees"] = 0
                return data
            else:  # compatibilité ancien format (liste seule)
                return {"demandes": data, "compteur_traitees": 0}
    return {"demandes": [], "compteur_traitees": 0}

def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

# -----------------------
# Mails
# -----------------------
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

def envoyer_mail_confirmation(demande, fichiers_pj=None):
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

    msg = MIMEMultipart()
    msg.attach(MIMEText(contenu, "plain", "utf-8"))
    msg["Subject"] = sujet
    msg["From"] = os.getenv("SMTP_USER")
    msg["To"] = demande["mail"]

    # pièces jointes
    if fichiers_pj:
        for chemin in fichiers_pj:
            try:
                with open(chemin, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header("Content-Disposition", f"attachment; filename={os.path.basename(chemin)}")
                    msg.attach(part)
            except Exception as e:
                print("❌ Erreur ajout PJ :", e)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as serveur:
            serveur.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
            serveur.send_message(msg)
        print(f"✅ Mail de confirmation envoyé à {demande['mail']}")
        demande["mail_contenu"] = contenu
        return True
    except Exception as e:
        print("❌ Erreur envoi mail confirmation :", e)
        return False

# -----------------------
# Routes
# -----------------------
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        data = load_data()
        demandes = data["demandes"]
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
        save_data(data)

        envoyer_mail(new_demande)
        envoyer_mail_accuse(new_demande)

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
            paris_tz = pytz.timezone("Europe/Paris")
            for d in demandes:
                if d["id"] == demande_id:
                    d["attribution"] = request.form.get("attribution")
                    d["mail"] = request.form.get("mail") or d["mail"]
                    d["details"] = request.form.get("details")
                    d["commentaire"] = request.form.get("commentaire")
                    nouveau_statut = request.form.get("statut")

                    # récupération des PJ
                    fichiers_pj = []
                    if "pj" in request.files:
                        for f in request.files.getlist("pj"):
                            if f and f.filename != "":
                                filename = secure_filename(f.filename)
                                filepath = os.path.join(UPLOAD_FOLDER, filename)
                                f.save(filepath)
                                fichiers_pj.append(filepath)

                    if d["statut"] != "Traité" and nouveau_statut == "Traité":
                        if envoyer_mail_confirmation(d, fichiers_pj):
                            data["compteur_traitees"] += 1
                            d["mail_confirme"] = datetime.datetime.now(paris_tz).strftime("%d/%m/%Y %H:%M")
                            d["mail_erreur"] = ""
                        else:
                            d["mail_confirme"] = ""
                            d["mail_erreur"] = "❌ Erreur lors de l'envoi du mail"

                    d["statut"] = nouveau_statut

            save_data(data)

        elif action == "delete":
            data["demandes"] = [d for d in demandes if d["id"] != demande_id]
            save_data(data)

    return render_template("admin.html", demandes=demandes,
                           compteur_traitees=data["compteur_traitees"])

@app.route("/imprimer/<demande_id>")
def imprimer(demande_id):
    data = load_data()
    demande = next((d for d in data["demandes"] if d["id"] == demande_id), None)
    return render_template("imprimer.html", demande=demande)

@app.route("/uploads/<filename>")
def download_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route("/voir_mail/<demande_id>")
def voir_mail(demande_id):
    data = load_data()
    demande = next((d for d in data["demandes"] if d["id"] == demande_id), None)
    return render_template("voir_mail.html", demande=demande)

if __name__ == "__main__":
    app.run(debug=True)
