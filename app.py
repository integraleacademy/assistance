from flask import Flask, render_template, request
import json, os, datetime, uuid

app = Flask(__name__)

# Emplacement du fichier sur le disque persistant Render
DATA_FILE = "/mnt/data/data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)  # s'assurer que le dossier existe
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        demandes = load_data()
        new_demande = {
            "id": str(uuid.uuid4()),
            "nom": request.form["nom"],
            "prenom": request.form["prenom"],
            "telephone": request.form["telephone"],
            "mail": request.form["mail"],
            "motif": request.form["motif"],
            "details": request.form["details"],
            "date": datetime.datetime.now().strftime("%d/%m/%Y %H:%M"),
            "attribution": "",
            "statut": "Non traité",
            "commentaire": ""
        }
        demandes.append(new_demande)
        save_data(demandes)
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
                    d["statut"] = request.form.get("statut")
                    d["commentaire"] = request.form.get("commentaire")
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
