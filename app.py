from flask import Flask, render_template, request, send_from_directory, url_for, redirect
import json, os, datetime, uuid, pytz, smtplib, hashlib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email import encoders
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Fichiers persistants (Render)
DATA_FILE = "/mnt/data/data.json"
ARCHIVE_FILE = "/mnt/data/archive.json"
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


def load_archive():
    if os.path.exists(ARCHIVE_FILE):
        with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_archive(archive):
    with open(ARCHIVE_FILE, "w", encoding="utf-8") as f:
        json.dump(archive, f, indent=4, ensure_ascii=False)


def hash_demande(d):
    contenu = f"{d['nom']}|{d['prenom']}|{d['mail']}|{d['motif']}|{d['details']}"
    return hashlib.sha256(contenu.encode("utf-8")).hexdigest()


def supprimer_fichier(filename):
    if not filename:
        return
    chemin = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(chemin):
        os.remove(chemin)


# -------------------------------------------------------------------
# Emails (inchangés, je n’ai pas recopié ici pour alléger)
# -------------------------------------------------------------------
# ⚠️ Garde exactement tes fonctions d’envoi de mail :
# send_email_html, envoyer_mail_admin, envoyer_mail_accuse, envoyer_mail_confirmation
# (elles ne changent pas du tout)


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
            "nom": request.form["nom"].strip(),
            "prenom": request.form["prenom"].strip(),
            "telephone": request.form["telephone"].strip(),
            "mail": request.form["mail"].strip().lower(),
            "motif": request.form["motif"],
            "details": request.form["details"].strip(),
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

        # Vérifier si cette demande existe déjà dans l’archive
        archive = load_archive()
        h = hash_demande(new_demande)
        if any(a["hash"] == h for a in archive):
            print("⚠️ Demande ignorée (doublon détecté dans archive)")
            return render_template("confirmation.html", doublon=True)

        # Sinon → on enregistre et on archive
        demandes.append(new_demande)
        save_data(data)

        archive.append({
            "hash": h,
            "nom": new_demande["nom"],
            "prenom": new_demande["prenom"],
            "mail": new_demande["mail"],
            "motif": new_demande["motif"],
            "details": new_demande["details"],
            "date": new_demande["date"]
        })
        save_archive(archive)

        envoyer_mail_admin(new_demande)
        envoyer_mail_accuse(new_demande)

        return render_template("confirmation.html", doublon=False)
    return render_template("index.html")


@app.route("/admin", methods=["GET", "POST"])
def admin():
    data = load_data()
    demandes = data["demandes"]
    # ... ⚠️ garde le reste de ton code admin identique
    return render_template("admin.html",
                           demandes=demandes,
                           compteur_traitees=data["compteur_traitees"])


@app.route("/archive")
def archive_page():
    archive = load_archive()
    return render_template("archive.html", archive=archive)


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
