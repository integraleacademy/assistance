from flask import Flask, render_template, request, send_from_directory, url_for, redirect
import json, os, datetime, uuid, pytz, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from werkzeug.utils import secure_filename

app = Flask(__name__)

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
    chemin = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(chemin):
        os.remove(chemin)


# -----------------------
# Mails
# -----------------------
def envoyer_mail_confirmation(demande):
    sujet = "Votre demande a été traitée - Intégrale Academy"
    contenu = f"""
    Bonjour {demande['prenom']} {demande['nom']},

    ✅ Votre demande a été traitée.

    📌 Motif : {demande['motif']}
    📝 Détails : {demande['details']}
    💬 Commentaire : {demande['commentaire'] if demande.get('commentaire') else "Aucun commentaire ajouté."}

    Cordialement,  
    L'équipe Intégrale Academy
    """

    msg = MIMEMultipart()
    msg.attach(MIMEText(contenu, "plain", "utf-8"))
    msg["Subject"] = sujet
    msg["From"] = os.getenv("SMTP_USER")
    msg["To"] = demande["mail"]

    # joindre toutes les PJ enregistrées
    if demande.get("pieces_jointes"):
        for pj in demande["pieces_jointes"]:
            chemin = os.path.join(UPLOAD_FOLDER, pj)
            if os.path.exists(chemin):
                with open(chemin, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header("Content-Disposition", f"attachment; filename={pj}")
                    msg.attach(part)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as serveur:
            serveur.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
            serveur.send_message(msg)
        print(f"✅ Mail envoyé à {demande['mail']}")
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
            "pieces_jointes": []
        }
        demandes.append(new_demande)
        save_data(data)

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
                    nouveau_statut = request.form.get("statut") or d["statut"]

                    # ajout de PJ
                    if "pj" in request.files:
                        for f in request.files.getlist("pj"):
                            if f and f.filename:
                                filename = secure_filename(f.filename)
                                filepath = os.path.join(UPLOAD_FOLDER, filename)
                                f.save(filepath)
                                if filename not in d["pieces_jointes"]:
                                    d["pieces_jointes"].append(filename)

                    # envoi mail si traité
                    if d["statut"] != "Traité" and nouveau_statut == "Traité":
                        if envoyer_mail_confirmation(d):
                            data["compteur_traitees"] += 1
                            d["mail_confirme"] = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
                        else:
                            d["mail_erreur"] = "❌ Erreur lors de l'envoi du mail"

                    d["statut"] = nouveau_statut

            save_data(data)
            return redirect(url_for("admin"))

        elif action == "delete_pj":
            pj_name = request.form.get("pj_name")
            for d in demandes:
                if d["id"] == demande_id and pj_name in d.get("pieces_jointes", []):
                    d["pieces_jointes"].remove(pj_name)
                    supprimer_fichier(pj_name)
            save_data(data)
            return redirect(url_for("admin"))

        elif action == "delete":
            demande_to_delete = None
            for d in demandes:
                if d["id"] == demande_id:
                    demande_to_delete = d
                    break
            if demande_to_delete:
                if demande_to_delete.get("justificatif"):
                    supprimer_fichier(demande_to_delete["justificatif"])
                for pj in demande_to_delete.get("pieces_jointes", []):
                    supprimer_fichier(pj)
                data["demandes"].remove(demande_to_delete)
                save_data(data)
            return redirect(url_for("admin"))

    return render_template("admin.html", demandes=demandes,
                           compteur_traitees=data["compteur_traitees"])


@app.route("/uploads/<filename>")
def download_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


if __name__ == "__main__":
    app.run(debug=True)
