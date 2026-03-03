from flask import Flask, render_template, request, send_from_directory, url_for, redirect, abort
from flask import render_template_string
import json, os, datetime, uuid, pytz, smtplib, re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email import encoders
from werkzeug.utils import secure_filename

from reportlab.platypus import Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader

from functools import wraps
from werkzeug.security import check_password_hash, generate_password_hash
from flask import session, flash

import calendar
from datetime import date as _date

def _add_one_month(d: _date) -> _date:
    y = d.year + (1 if d.month == 12 else 0)
    m = 1 if d.month == 12 else d.month + 1
    last = calendar.monthrange(y, m)[1]
    return _date(y, m, min(d.day, last))

def _eur(v):
    # v peut être int/float/str (ex: "OFFERTS", "INCLUS")
    if isinstance(v, (int, float)):
        # pas de décimales
        return f"{int(v)} €"
    return str(v)

def _parse_dates_range(dates_txt: str):
    """
    Essaie d'extraire une date début/fin à partir d'un texte comme :
    '9 mars au 21 avril 2026' ou '09 mars 2026 au 21 avril 2026' etc.
    Si ça échoue, renvoie (None, None)
    """
    if not dates_txt:
        return (None, None)

    import re
    mois_fr = {
        "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
        "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
        "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12
    }

    # on récupère l'année (première année trouvée)
    m_annee = re.search(r"(20\d{2})", dates_txt)
    annee = int(m_annee.group(1)) if m_annee else None

    # split "au"
    parts = dates_txt.split("au")
    if len(parts) < 2:
        return (None, None)

    left = parts[0].strip()
    right = parts[1].strip()

    def parse_part(p: str):
        # attend "9 mars" ou "9 mars 2026"
        tokens = p.replace(",", " ").split()
        if len(tokens) < 2:
            return None
        jour = None
        mois = None
        an = None
        # jour
        try:
            jour = int(re.sub(r"\D", "", tokens[0]))
        except:
            return None
        # mois
        mois_name = tokens[1].lower()
        if mois_name not in mois_fr:
            return None
        mois = mois_fr[mois_name]
        # année (si présente)
        for t in tokens[2:]:
            if re.fullmatch(r"20\d{2}", t):
                an = int(t)
                break
        if an is None:
            an = annee
        if an is None:
            return None
        return _date(an, mois, jour)

    d1 = parse_part(left)
    d2 = parse_part(right)
    return (d1, d2)

def build_devis_context(formation_code: str, formation_label: str, dates_txt: str, sequence: int = 1):
    """
    Retourne un dict prêt à injecter dans plan_financement.html :
    devis_num, devis_date, devis_valid_until, date_debut, date_fin, devis_lignes, devis_total
    """
    today = _date.today()
    devis_num = f"{today.year}-{today.month:02d}{today.day:02d}{sequence:04d}"  # AAAA-MMDD0001
    devis_date = today.strftime("%d/%m/%Y")
    devis_valid_until = _add_one_month(today).strftime("%d/%m/%Y")

    d1, d2 = _parse_dates_range(dates_txt or "")
    date_debut = d1.strftime("%d/%m/%Y") if d1 else "—"
    date_fin = d2.strftime("%d/%m/%Y") if d2 else "—"

    # Lignes selon formation
    lignes = []
    total = 0

    if formation_code == "A3P":
        lignes = [
            {"intitule": "Agent de Protection Physique des Personnes (A3P)", "prix_unitaire": _eur(4200), "quantite": 1, "total": _eur(4200)},
            {"intitule": "Frais de dossier", "prix_unitaire": "OFFERTS", "quantite": 1, "total": _eur(0)},
        ]
        total = 4200

    elif formation_code == "APS":
        lignes = [
            {"intitule": "Agent de Prévention et de Sécurité (APS)", "prix_unitaire": _eur(1650), "quantite": 1, "total": _eur(1650)},
            {"intitule": "Frais de dossier", "prix_unitaire": "OFFERTS", "quantite": 1, "total": _eur(0)},
        ]
        total = 1650

    elif formation_code == "DESP_INIT":
        lignes = [
            {"intitule": "Formation initiale DSP Dirigeant d’entreprise de sécurité privée", "prix_unitaire": _eur(4300), "quantite": 1, "total": _eur(4300)},
            {"intitule": "Frais de dossier", "prix_unitaire": "OFFERTS", "quantite": 1, "total": _eur(0)},
            {"intitule": "Accès formation en ligne e-learning", "prix_unitaire": "INCLUS", "quantite": 1, "total": _eur(0)},
        ]
        total = 4300

    elif formation_code == "DESP_VAE":
        lignes = [
            {"intitule": "Etude de recevabilité (Livret 1)", "prix_unitaire": "OFFERTE", "quantite": 1, "total": _eur(0)},
            {"intitule": "Suivi de dossier, frais administratifs, présentation du dossier auprès du certificateur (acompte de 30%)", "prix_unitaire": _eur(1140), "quantite": 1, "total": _eur(1140)},
            {"intitule": "Passage devant le jury de certification (solde)", "prix_unitaire": _eur(2660), "quantite": 1, "total": _eur(2660)},
        ]
        total = 3800

    elif formation_code == "VTC":
        lignes = [
            {"intitule": "Formation Chauffeur VTC incluant :\nFormation théorique en ligne à distance\nFormation pratique sur véhicule à doubles commandes\nFrais d’examen Chambre des métiers\nPrêt du véhicule à doubles commandes le jour de l’examen pratique\nLe livre officiel Chauffeur VTC",
             "prix_unitaire": _eur(1600), "quantite": 1, "total": _eur(1600)},
            {"intitule": "Frais de dossier", "prix_unitaire": "OFFERTS", "quantite": 1, "total": _eur(0)},
        ]
        total = 1600

    else:
        # fallback si jamais
        lignes = [
            {"intitule": formation_label or formation_code or "Formation", "prix_unitaire": "—", "quantite": 1, "total": "—"},
        ]
        total = 0

    return {
        "devis_num": devis_num,
        "devis_date": devis_date,
        "devis_valid_until": devis_valid_until,
        "date_debut": date_debut,
        "date_fin": date_fin,
        "devis_lignes": lignes,
        "devis_total": _eur(total),
    }

PLAN_FORMATIONS = {
    "A3P": "A3P – Agent de Protection Physique des Personnes",
    "APS": "APS – Agent de Prévention et de Sécurité",
    "VTC": "VTC – Chauffeur de transport avec chauffeur",
    "DESP_INIT": "DESP – Dirigeant d’entreprise de sécurité (initial)",
    "DESP_VAE": "DESP – Dirigeant d’entreprise de sécurité (VAE)"
}

PLAN_TARIFS = {
    "A3P": 4200,
    "APS": 1650,
    "VTC": 1600,
    "DESP_INIT": 4300,
    "DESP_VAE": 3800
}

PLAN_DATES = {
    "A3P": [
        "30 mars au 2 juin 2026 – examen le 3 juin 2026",
        "8 juin au 4 août 2026 – examen le 5 août 2026",
        "1 septembre au 27 octobre 2026 – examen le 28 octobre 2026",
        "9 novembre 2026 au 19 janvier 2027 – examen le 20 janvier 2027"
    ],
    "APS": [
        "23 mars au 27 avril 2026 – examen le 28 avril 2026",
        "26 mai au 29 juin 2026 – examen le 30 juin 2026",
        "8 juillet au 12 août 2026 – examen le 13 août 2026",
        "7 septembre au 9 octobre 2026 – examen le 12 octobre 2026",
        "3 novembre au 8 décembre 2026 – examen le 9 décembre 2026"
    ],
    "DESP_INIT": [
        "19 janvier au 2 mars 2026 – examen le 3 mars 2026",
        "9 mars au 21 avril 2026 – examen le 22 avril 2026",
        "27 avril au 15 juin 2026 – examen le 16 juin 2026"
    ]
}

def _parse_cpf_value(value):
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(float(value))
    cleaned = str(value).replace(",", ".").replace(" ", "")
    try:
        return int(float(cleaned))
    except:
        return 0

def _parse_exam_date_from_dates_txt(dates_txt):
    if not dates_txt:
        return ""

    match = re.search(r"examen le (\\d{1,2}) ([a-zà-ÿ]+) (\\d{4})", dates_txt, re.IGNORECASE)
    if not match:
        return ""

    jour = match.group(1).zfill(2)
    mois_texte = match.group(2).lower()
    annee = match.group(3)
    mois_map = {
        "janvier": "01",
        "février": "02",
        "fevrier": "02",
        "mars": "03",
        "avril": "04",
        "mai": "05",
        "juin": "06",
        "juillet": "07",
        "août": "08",
        "aout": "08",
        "septembre": "09",
        "octobre": "10",
        "novembre": "11",
        "décembre": "12",
        "decembre": "12"
    }
    mois = mois_map.get(mois_texte)
    if not mois:
        return ""
    return f"{annee}-{mois}-{jour}"

def compute_plan_financement_simulation(formation, dates_txt, cpf_value, france_travail, date_examen_str):
    formation_code = formation or "APS"
    formation_label = PLAN_FORMATIONS.get(formation_code, formation_code)
    tarif = PLAN_TARIFS.get(formation_code, 0)
    cpf = _parse_cpf_value(cpf_value)
    ft = max(tarif - cpf, 0) if france_travail == "OUI" else 0
    reste_avec_ft = max(tarif - cpf - ft, 0)
    reste_sans_ft = max(tarif - cpf, 0)

    date_examen_str = (date_examen_str or "").strip()
    if not date_examen_str:
        date_examen_str = _parse_exam_date_from_dates_txt(dates_txt or "")

    date_examen = None
    if date_examen_str:
        try:
            date_examen = datetime.datetime.strptime(
                date_examen_str, "%Y-%m-%d"
            ).date()
        except ValueError:
            date_examen = None

    echeances = build_echeances_mensuelles(
        reste=reste_sans_ft,
        date_devis=datetime.date.today(),
        date_examen=date_examen
    )

    echeances_payload = [
        {
            "date": e["date"].strftime("%d/%m/%Y"),
            "montant": f"{e['montant']:.2f}"
        }
        for e in echeances
    ]

    return {
        "formation": formation_code,
        "formation_label": formation_label,
        "dates": dates_txt or "",
        "date_examen": date_examen_str,
        "cpf": cpf,
        "tarif": tarif,
        "ft": ft,
        "france_travail": france_travail,
        "reste_avec_ft": reste_avec_ft,
        "reste_sans_ft": reste_sans_ft,
        "echeances": echeances_payload
    }


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


app = Flask(__name__, static_folder="static", static_url_path="/static")
from datetime import timedelta

app.secret_key = os.environ.get("SECRET_KEY", "CHANGE_ME_LONG_RANDOM")

app.config.update(
    SESSION_COOKIE_NAME="integrale_assistance_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,  # Render = https
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

@app.before_request
def refresh_session():
    session.permanent = True



from datetime import date

@app.context_processor
def inject_now():
    return {"now": date.today}


# Fichiers persistants (Render)
def _resolve_data_dir():
    """
    Utilise DATA_DIR/RENDER_DISK_PATH puis /mnt/data si disponibles,
    sinon fallback local. On évite toute écriture de test au boot
    pour ne pas bloquer l'initialisation des workers.
    """
    candidates = [
        os.getenv("DATA_DIR"),
        os.getenv("RENDER_DISK_PATH"),
        "/mnt/data",
        os.path.join(os.path.dirname(__file__), "data"),
    ]

    for candidate in candidates:
        if not candidate:
            continue
        try:
            os.makedirs(candidate, exist_ok=True)
            return candidate
        except OSError:
            continue
    # dernier recours : dossier courant
    return os.getcwd()


DATA_DIR = _resolve_data_dir()
DATA_FILE = os.path.join(DATA_DIR, "data.json")
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# -------------------------------------------------------------------
# Utils
# -------------------------------------------------------------------
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("demandes", [])
                    data.setdefault("archives", [])
                    data.setdefault("compteur_traitees", 0)
                    data.setdefault("hebergements", [])
                    return data
                return {"demandes": data, "archives": [], "compteur_traitees": 0}
        except (json.JSONDecodeError, OSError):
            # fichier corrompu/inaccessible : on garde une copie puis on repart proprement
            try:
                backup_path = f"{DATA_FILE}.corrupted"
                os.replace(DATA_FILE, backup_path)
            except OSError:
                pass
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
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as serveur:
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
    """Envoie un mail à znaw83@gmail.com quand la demande est attribuée à Mohamed"""
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

    send_email_html("znaw83@gmail.com", sujet, plain, html)




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
            "commentaire_admin": "",
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

        expected = user.get("pass")

        if expected and password == expected:
            session["user_email"] = email
            session["user_name"] = user.get("name")
            session["user_role"] = user.get("role")

            session.permanent = True  # ✅ cookie persistant (30 jours)

            next_url = request.args.get("next") or url_for("admin")
            return redirect(next_url)

        flash("Identifiants incorrects", "error")
        return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("user_email", None)
    session.pop("user_name", None)
    session.pop("user_role", None)
    return redirect(url_for("login"))
    

def update_demande_fields(demande, form_data, data, files=None):
    demande["mail"] = form_data.get("mail") or demande.get("mail")
    if form_data.get("details") is not None:
        demande["details"] = form_data.get("details")
    demande["commentaire"] = form_data.get("commentaire", demande.get("commentaire", ""))
    demande["commentaire_admin"] = form_data.get("commentaire_admin", demande.get("commentaire_admin", ""))
    demande["rappel_date"] = form_data.get("rappel_date", demande.get("rappel_date", ""))

    ancienne_attribution = demande.get("attribution", "").strip()
    nouvelle_attribution = (form_data.get("attribution") or "").strip()
    demande["attribution"] = nouvelle_attribution or ancienne_attribution

    # 🔔 Notification attribution Mohamed
    if nouvelle_attribution == "Mohamed" and ancienne_attribution != "Mohamed":
        try:
            envoyer_mail_attribution_mohamed(demande)
        except:
            pass

    ancien_statut = demande.get("statut", "Non traité")
    nouveau_statut = form_data.get("statut") or ancien_statut

    # 📎 Upload pièces jointes
    if files and "pj" in files:
        for f in files.getlist("pj"):
            if f and f.filename:
                filename = secure_filename(f.filename)
                filepath = os.path.join(UPLOAD_FOLDER, filename)
                f.save(filepath)
                demande.setdefault("pieces_jointes", [])
                if filename not in demande["pieces_jointes"]:
                    demande["pieces_jointes"].append(filename)

    # 📨 Passage à Traité
    if ancien_statut != "Traité" and nouveau_statut == "Traité":
        if envoyer_mail_confirmation(demande):
            data["compteur_traitees"] += 1
            paris_tz = pytz.timezone("Europe/Paris")
            demande["mail_confirme"] = datetime.datetime.now(paris_tz).strftime("%d/%m/%Y %H:%M")
            demande["mail_erreur"] = ""
        else:
            demande["mail_erreur"] = "❌ Erreur lors de l'envoi du mail"

    demande["statut"] = nouveau_statut


@app.route("/admin", methods=["GET", "POST"])
@login_required
def admin():
    data = load_data()
    demandes = data["demandes"]

    # ❌ Exclure les demandes de devis détaillé de l'admin principal
    demandes = [
        d for d in demandes
        if d.get("motif") != "Demande de devis détaillé"
    ]

    # 🔐 Identifier l'utilisateur connecté
    user = current_user()
    user_name = user["name"] if user else None
    user_role = user["role"] if user else "user"

    # ✅ Fonction de parsing de date (AFFICHAGE UNIQUEMENT)
    def parse_date(d):
        try:
            return datetime.datetime.strptime(d.get("date", ""), "%d/%m/%Y %H:%M")

        except:
            return datetime.datetime.min

    # =====================================================
    # 🟢 TRAITEMENTS POST (AJOUT / UPDATE / DELETE / ARCHIVE)
    # =====================================================
    raw_query = (request.args.get("q") or request.form.get("q") or "").strip()
    query = raw_query.lower()

    def redirect_with_query():
        if raw_query:
            return redirect(url_for("admin", q=raw_query))
        return redirect(url_for("admin"))

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
                "commentaire_admin": "",
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

            # 📧 Notification si attribuée à Mohamed
            if new_demande["attribution"].strip() == "Mohamed":
                try:
                    envoyer_mail_attribution_mohamed(new_demande)
                except:
                    pass

            return redirect_with_query()

        # ✏️ Mise à jour d'une demande existante
        elif action == "update":
            for d in demandes:
                if d["id"] == demande_id:
                    update_demande_fields(d, request.form, data, request.files)

            save_data(data)
            return redirect_with_query()

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
            return redirect_with_query()

        # 🗑️ Archivage d'une demande
        elif action == "delete":
            to_remove = next((d for d in demandes if d["id"] == demande_id), None)
            if to_remove:
                data["archives"].append(to_remove)
                supprimer_fichier(to_remove.get("justificatif"))
                for pj in to_remove.get("pieces_jointes", []):
                    supprimer_fichier(pj)
                data["demandes"].remove(to_remove)
                save_data(data)
            return redirect_with_query()

        # 🧹 Archivage de toutes les demandes traitées
        elif action == "delete_all_traitees":
            traitees = [d for d in demandes if d.get("statut") == "Traité"]
            for d in traitees:
                data["archives"].append(d)
                supprimer_fichier(d.get("justificatif"))
                for pj in d.get("pieces_jointes", []):
                    supprimer_fichier(pj)
                data["demandes"].remove(d)
            save_data(data)
            return redirect_with_query()

    # =========================
    # 🔍 Recherche (GET)
    # =========================
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

    # 👤 Filtre utilisateur (non admin)
    if user_role != "admin" and user_name:
        demandes = [
            d for d in demandes
            if (d.get("attribution") or "").strip().lower() == user_name.lower()
        ]

    # 🔽 TRI GLOBAL (plus récentes en premier)
    demandes = sorted(
        demandes,
        key=parse_date,
        reverse=True
    )

    return render_template(
        "admin.html",
        demandes=demandes,
        compteur_traitees=data["compteur_traitees"],
        query=raw_query
    )


@app.route("/admin/choisir-centre-formation")
@login_required
def choisir_centre_formation():
    return render_template("choisir_centre_formation.html")


@app.route("/admin/autosave", methods=["POST"])
@login_required
def admin_autosave():
    data = load_data()
    demandes = data["demandes"]
    form_data = request.form or (request.get_json(silent=True) or {})
    demande_id = form_data.get("id")
    if not demande_id:
        return ("", 204)

    for d in demandes:
        if d["id"] == demande_id:
            update_demande_fields(d, form_data, data)
            save_data(data)
            break

    return ("", 204)


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

@app.route("/admin-devis/simulateur")
@login_required
def simulateur_plan_financement():
    simulation = compute_plan_financement_simulation(
        formation=request.args.get("formation", "APS"),
        dates_txt=request.args.get("dates", ""),
        cpf_value=request.args.get("cpf", 0),
        france_travail=request.args.get("france_travail", "NON"),
        date_examen_str=request.args.get("date_examen", "")
    )

    return render_template(
        "simulateur_plan_financement.html",
        formations=PLAN_FORMATIONS,
        dates_options=PLAN_DATES,
        simulation=simulation
    )

@app.route("/admin-devis/simulateur/data", methods=["POST"])
@login_required
def simulateur_plan_financement_data():
    payload = request.get_json(silent=True) or {}

    simulation = compute_plan_financement_simulation(
        formation=payload.get("formation", "APS"),
        dates_txt=payload.get("dates", ""),
        cpf_value=payload.get("cpf", 0),
        france_travail=payload.get("france_travail", "NON"),
        date_examen_str=payload.get("date_examen", "")
    )

    return simulation


@app.route("/admin-devis/simulateur/envoyer-plan", methods=["POST"])
@login_required
def simulateur_plan_financement_envoyer_plan():
    payload = request.get_json(silent=True) or {}

    simulation = compute_plan_financement_simulation(
        formation=payload.get("formation", "APS"),
        dates_txt=payload.get("dates", ""),
        cpf_value=payload.get("cpf", 0),
        france_travail=payload.get("france_travail", "NON"),
        date_examen_str=payload.get("date_examen", "")
    )

    destinataire = (payload.get("mail") or "").strip()
    if not destinataire:
        return {"ok": False, "message": "Un email est nécessaire pour l'envoi."}, 400

    prenom = (payload.get("prenom") or "").strip()
    nom = (payload.get("nom") or "").strip()

    echeances_recue = payload.get("echeances") or []
    echeances = []
    for e in echeances_recue:
        date_txt = str(e.get("date") or "").strip()
        montant_txt = str(e.get("montant") or "").strip()
        if not date_txt or not montant_txt:
            continue
        try:
            montant = float(montant_txt)
        except:
            continue
        echeances.append({"date": date_txt, "montant": round(montant, 2)})

    if not echeances:
        for e in simulation.get("echeances", []):
            try:
                echeances.append({
                    "date": datetime.datetime.strptime(e.get("date", ""), "%d/%m/%Y").strftime("%Y-%m-%d"),
                    "montant": round(float(e.get("montant", 0)), 2)
                })
            except:
                pass

    token = uuid.uuid4().hex
    data = load_data()
    data.setdefault("plans_simulation", [])
    data["plans_simulation"].append({
        "id": str(uuid.uuid4()),
        "token": token,
        "created_at": datetime.datetime.now(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M"),
        "nom": nom,
        "prenom": prenom,
        "mail": destinataire,
        "simulation": simulation,
        "echeances": echeances
    })
    save_data(data)

    plan_url = url_for("plan_simulation_public", token=token, _external=True)
    nom_affiche = (prenom + " " + nom).strip() or "Madame, Monsieur"
    subject = "📄 Votre plan de financement détaillé — Intégrale Academy"
    plain = (
        f"Bonjour {nom_affiche},\n\n"
        "Voici votre plan de financement détaillé en consultation :\n"
        f"{plan_url}\n\n"
        "Ce lien est consultatif (aucune modification n'est possible).\n\n"
        "Bien cordialement,\n"
        "Intégrale Academy"
    )
    html = _wrap_html(
        "<h1>📄 Votre plan de financement détaillé</h1>",
        f"""
        <p>Bonjour <strong>{nom_affiche}</strong>,</p>
        <p>Vous trouverez ci-dessous votre plan de financement détaillé en <strong>consultation uniquement</strong>.</p>
        <p style=\"text-align:center;margin:24px 0;\">
          <a href=\"{plan_url}\" style=\"display:inline-block;padding:14px 26px;background:#0d6efd;color:white;text-decoration:none;border-radius:8px;font-weight:700;\">
            👉 Consulter le plan de financement
          </a>
        </p>
        <p style=\"font-size:13px;color:#666;margin:0;\">Ce lien est personnel et ne permet aucune modification.</p>
        """
    )

    send_email_html(destinataire, subject, plain, html)
    return {"ok": True, "message": "Plan envoyé avec succès."}


@app.route("/admin-devis/toggle/<devis_id>", methods=["POST"])
@login_required
def toggle_devis(devis_id):
    data = load_data()

    for d in data.get("demandes", []):
        if d.get("id") == devis_id and d.get("motif") == "Demande de devis détaillé":
            ancien_statut = d.get("statut_devis") or "A envoyer"

            if ancien_statut == "Envoyé":
                d["statut_devis"] = "A envoyer"
            else:
                d["statut_devis"] = "Envoyé"

                # 📞 Notification Mohamed uniquement au clic sur "Changer le statut"
                # depuis un devis "À envoyer" => "Envoyé"
                rappel_existant = next(
                    (
                        x for x in data.get("demandes", [])
                        if x.get("source_devis_id") == devis_id
                        and x.get("motif") == "Rappel suite devis envoyé"
                    ),
                    None
                )

                if not rappel_existant:
                    try:
                        infos = json.loads(d.get("details", "{}"))
                    except:
                        infos = {}

                    formation = (infos.get("formation") or "").strip()
                    formation_label = {
                        "A3P": "A3P – Agent de Protection Physique des Personnes",
                        "APS": "APS – Agent de Prévention et de Sécurité",
                        "VTC": "VTC – Chauffeur de transport avec chauffeur",
                        "DESP_INIT": "DESP – Dirigeant d’entreprise de sécurité (initial)",
                        "DESP_VAE": "DESP – Dirigeant d’entreprise de sécurité (VAE)"
                    }.get(formation, formation)

                    demande_rappel_devis = {
                        "id": str(uuid.uuid4()),
                        "source_devis_id": devis_id,
                        "nom": d.get("nom"),
                        "prenom": d.get("prenom"),
                        "telephone": d.get("telephone"),
                        "mail": d.get("mail"),
                        "motif": "Rappel suite devis envoyé",
                        "details": (
                            "Créée automatiquement après clic sur ‘Changer le statut’ (À envoyer → Envoyé).\n"
                            f"Formation : {formation_label or 'Non précisée'}\n"
                            f"Session : {(infos.get('dates') or 'Non précisée')}"
                        ),
                        "date": datetime.datetime.now(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M"),
                        "statut": "A rappeler",
                        "attribution": "Mohamed",
                        "commentaire": "",
                        "commentaire_admin": "",
                        "mail_confirme": "",
                        "mail_erreur": "",
                        "mail_contenu": "",
                        "mail_html": "",
                        "pieces_jointes": [],
                        "reponses": [],
                        "is_doublon": False,
                        "rappel_date": "",
                        "plage": ""
                    }
                    data["demandes"].append(demande_rappel_devis)

                    try:
                        envoyer_mail_attribution_mohamed(demande_rappel_devis)
                    except:
                        pass
            break

    save_data(data)
    return redirect(url_for("admin_devis"))

@app.route("/admin-devis/dossier/<devis_id>")
@login_required
def voir_dossier_devis(devis_id):
    data = load_data()

    devis = next(
        (d for d in data.get("demandes", [])
         if d.get("id") == devis_id
         and d.get("motif") == "Demande de devis détaillé"),
        None
    )

    if not devis:
        abort(404)

    try:
        infos = json.loads(devis.get("details", "{}"))
    except:
        infos = {}

    return render_template(
        "voir_dossier_devis.html",
        devis=devis,
        infos=infos
    )


@app.route("/admin-devis/dossier/<devis_id>/update-vtc-dates", methods=["POST"])
@login_required
def update_vtc_dates_devis(devis_id):
    data = load_data()

    devis = next(
        (d for d in data.get("demandes", [])
         if d.get("id") == devis_id
         and d.get("motif") == "Demande de devis détaillé"),
        None
    )

    if not devis:
        abort(404)

    try:
        infos = json.loads(devis.get("details", "{}"))
    except:
        infos = {}

    if infos.get("formation") == "VTC":
        infos["dates_reelles_formation_vtc"] = request.form.get("dates_reelles_formation_vtc", "").strip()
        devis["details"] = json.dumps(infos, ensure_ascii=False)
        save_data(data)

    return redirect(url_for("voir_dossier_devis", devis_id=devis_id))
    





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
            "commentaire_admin": "",
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

from dateutil.relativedelta import relativedelta

def build_echeances_mensuelles(reste: float, date_devis: datetime.date, date_examen: datetime.date):
    """
    Retourne une liste [{"date": date, "montant": float}, ...]
    Règle: prélèvement le 5 du mois suivant la date du devis, puis chaque 5.
    Le total doit être soldé au plus tard J-7 avant l'examen.
    """
    if not date_examen:
        return []

    date_limite = date_examen - datetime.timedelta(days=7)

    # 1er prélèvement = 5 du mois suivant (quoi qu'il arrive)
    first = (date_devis.replace(day=1) + relativedelta(months=1)).replace(day=5)

    # Si c'est déjà après la date limite -> impossible de proposer des prélèvements
    if first > date_limite:
        return []

    # Liste des dates 5/5/5... jusqu'à la date limite
    dates = []
    d = first
    while d <= date_limite:
        dates.append(d)
        d = (d + relativedelta(months=1)).replace(day=5)

    n = len(dates)
    if n == 0:
        return []

    # Répartition du reste sur n échéances
    montant_base = round(float(reste) / n, 2)
    montants = [montant_base] * n
    ecart = round(float(reste) - sum(montants), 2)
    montants[-1] += ecart

    return [{"date": dates[i], "montant": montants[i]} for i in range(n)]



@app.route("/demande-devis", methods=["GET", "POST"])
def demande_devis():
    if request.method == "POST":
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.platypus import Table, TableStyle
        from reportlab.lib import colors
        from dateutil.relativedelta import relativedelta

        data = request.form.to_dict()
        data["gclid"] = (data.get("gclid") or "").strip()

        # -------------------------
        # 🔥 NOTATION INTERNE AUTO
        # -------------------------
        notation_interne = ""
        
        if (
            data.get("cpf_consulte") == "OUI"
            and data.get("france_travail") == "NON"
            and data.get("financement_perso") == "OUI"
            and data.get("identite_numerique") == "OUI"
        ):
            notation_interne = "CHAUD"

        # =========================
        # DATE D'EXAMEN (OBLIGATOIRE POUR CERTAINES FORMATIONS)
        # =========================
        date_examen = None
        date_examen_str = data.get("date_examen", "").strip()
        
        if date_examen_str:
            try:
                date_examen = datetime.datetime.strptime(
                    date_examen_str, "%Y-%m-%d"
                ).date()
            except ValueError:
                date_examen = None

        # =========================
        # DATE LIMITE DE PAIEMENT
        # (formation soldée 7 jours avant l’examen)
        # =========================
        date_limite_paiement = None
        
        if date_examen:
            date_limite_paiement = date_examen - datetime.timedelta(days=7)
        
        print("DATE EXAMEN =", date_examen)
        print("DATE LIMITE PAIEMENT =", date_limite_paiement)



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
        # MONTANT FRANCE TRAVAIL
        # =========================
        if data.get("france_travail") == "OUI":
            montant_ft = max(tarif - cpf, 0)
        else:
            montant_ft = 0


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
        # PDF
        # =========================
        pdf_path = f"/mnt/data/devis_{uuid.uuid4().hex}.pdf"
        c = canvas.Canvas(pdf_path, pagesize=A4)
        width, height = A4
        y = height - 40

        logo_path = os.path.join(app.root_path, "static", "logo.png")
        if os.path.exists(logo_path):
            c.drawImage(
                ImageReader(logo_path),
                40,
                height - 120,
                width=160,
                preserveAspectRatio=True,
                mask="auto"
            )

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
            ("Dates réelles de formation VTC", v("dates_reelles_formation_vtc")),
        
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
            "Premier prélèvement le 5 du mois suivant l’inscription. "
            "La formation doit être intégralement réglée au plus tard 7 jours avant l’examen."
        )

        y -= 30



        c.save()

        # =========================
        # SAUVEGARDE + MAIL
        # =========================
        data_store = load_data()
        data_store["demandes"].append({
            "id": str(uuid.uuid4()),
            "token_plan": uuid.uuid4().hex,
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
            "commentaire_admin": "",
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
            "notation_interne": notation_interne,
            "echeancier_manuel": [],
            "pdf_path": pdf_path
        })

        # 📞 Créer automatiquement une demande de rappel dans l'admin (attribuée à Mohamed)
        demande_rappel_devis = {
            "id": str(uuid.uuid4()),
            "nom": data.get("nom"),
            "prenom": data.get("prenom"),
            "telephone": data.get("telephone"),
            "mail": data.get("mail"),
            "motif": "Rappel suite dépôt devis",
            "details": (
                "Créée automatiquement après une demande de devis détaillé.\n"
                f"Formation : {formation or 'Non précisée'}\n"
                f"Session : {data.get('dates', 'Non précisée')}"
            ),
            "date": datetime.datetime.now(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M"),
            "statut": "A rappeler",
            "attribution": "Mohamed",
            "commentaire": "",
            "commentaire_admin": "",
            "mail_confirme": "",
            "mail_erreur": "",
            "mail_contenu": "",
            "mail_html": "",
            "pieces_jointes": [],
            "reponses": [],
            "is_doublon": False,
            "rappel_date": "",
            "plage": ""
        }
        data_store["demandes"].append(demande_rappel_devis)

        save_data(data_store)

        try:
            envoyer_mail_attribution_mohamed(demande_rappel_devis)
        except:
            pass

        ultra = (
            data.get("cpf_consulte") == "OUI" and
            data.get("france_travail") == "NON" and
            data.get("financement_perso") == "OUI" and
            data.get("identite_numerique") == "OUI"
        )

        return redirect(url_for(
            "confirmation_devis",
            ultra="1" if ultra else "0",
            formation=formation
        ))


    gclid = (request.args.get("gclid") or "").strip()
    return render_template(
        "demande_devis.html",
        dates_options=PLAN_DATES,
        gclid=gclid
    )





@app.route("/confirmation-devis")
def confirmation_devis():
    ultra = request.args.get("ultra") == "1"
    formation = request.args.get("formation")
    return render_template(
        "confirmation_devis.html",
        ultra=ultra,
        formation=formation
    )


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

@app.route("/admin-devis/pdf-client/<devis_id>")
@login_required
def voir_pdf_client(devis_id):
    data = load_data()

    devis = next(
        (d for d in data.get("demandes", [])
         if d.get("id") == devis_id
         and d.get("motif") == "Demande de devis détaillé"),
        None
    )

    if not devis:
        abort(404)

    pdf_client_path = devis.get("pdf_client_path")
    if not pdf_client_path or not os.path.exists(pdf_client_path):
        abort(404)

    return send_from_directory(
        os.path.dirname(pdf_client_path),
        os.path.basename(pdf_client_path)
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

@app.route("/admin-devis/plan-financement/<devis_id>")
@login_required
def plan_financement_devis(devis_id):
    data = load_data()

    devis = next(
        (d for d in data.get("demandes", [])
         if d.get("id") == devis_id
         and d.get("motif") == "Demande de devis détaillé"),
        None
    )

    if not devis:
        abort(404)

    # -------------------------------
    # Infos formulaire
    # -------------------------------
    try:
        infos = json.loads(devis.get("details", "{}"))
    except:
        infos = {}

    formation = infos.get("formation")

    formation_label = {
        "A3P": "A3P – Agent de Protection Physique des Personnes",
        "APS": "APS – Agent de Prévention et de Sécurité",
        "VTC": "VTC – Chauffeur de transport avec chauffeur",
        "DESP_INIT": "DESP – Dirigeant d’entreprise de sécurité (initial)",
        "DESP_VAE": "DESP – Dirigeant d’entreprise de sécurité (VAE)"
    }.get(formation, formation)

    devis_ctx = build_devis_context(
        formation_code=formation,
        formation_label=formation_label,
        dates_txt=infos.get("dates", ""),
        sequence=1
    )



    TARIFS = {
        "A3P": 4200,
        "APS": 1650,
        "VTC": 1600,
        "DESP_INIT": 4300,
        "DESP_VAE": 3800
    }

    tarif = TARIFS.get(formation, 0)

    try:
        cpf = int(float(infos.get("cpf_montant", 0)))
    except:
        cpf = 0

    # France Travail
    if infos.get("france_travail") == "OUI":
        ft = max(tarif - cpf, 0)
    else:
        ft = 0

    reste_avec_ft = max(tarif - cpf - ft, 0)
    reste_sans_ft = max(tarif - cpf, 0)

    # -------------------------------
    # Échéancier (si date examen)
    # -------------------------------
    date_examen = None
    try:
        if infos.get("date_examen"):
            date_examen = datetime.datetime.strptime(
                infos["date_examen"], "%Y-%m-%d"
            ).date()
    except:
        date_examen = None

    # 🔁 Échéancier manuel prioritaire
    if devis.get("echeancier_manuel"):
        echeances = devis["echeancier_manuel"]
    else:
        echeances = build_echeances_mensuelles(
            reste=reste_sans_ft,
            date_devis=datetime.date.today(),
            date_examen=date_examen
        )


    return render_template(
        "plan_financement.html",
        devis=devis,
        prenom=devis.get("prenom"),
        nom=devis.get("nom"),
        email=devis.get("mail"),
        formation_label=formation_label,
        dates=infos.get("dates"),
        cpf=cpf,
        ft=ft,
        reste_avec_ft=reste_avec_ft,
        reste_sans_ft=reste_sans_ft,
        echeances=echeances,
        **devis_ctx
    )




@app.route("/test-plan-financement")
def test_plan_financement():
    echeances = [
        {"date": "05/04/2026", "montant": "525"},
        {"date": "05/05/2026", "montant": "525"},
        {"date": "05/06/2026", "montant": "525"},
        {"date": "05/07/2026", "montant": "525"},
        {"date": "05/08/2026", "montant": "525"},
        {"date": "05/09/2026", "montant": "525"},
        {"date": "05/10/2026", "montant": "525"},
        {"date": "05/11/2026", "montant": "525"},
    ]

    return render_template(
        "plan_financement.html",
        prenom="Clément",
        nom="VAILLANT",
        formation_label="A3P – Agent de Protection Physique des Personnes",
        dates="Mars 2026 → Novembre 2026",
        cpf=3000,
        ft=1200,
        reste_avec_ft=0,
        reste_sans_ft=1200,
        echeances=echeances
    )

@app.route("/admin-devis/echeancier/<devis_id>", methods=["POST"])
@login_required
def save_echeancier(devis_id):
    data = load_data()

    devis = next(
        (d for d in data.get("demandes", [])
         if d.get("id") == devis_id
         and d.get("motif") == "Demande de devis détaillé"),
        None
    )

    if not devis:
        abort(404)

    dates = request.form.getlist("date[]")
    montants = request.form.getlist("montant[]")

    echeancier = []
    for d, m in zip(dates, montants):
        if d and m:
            try:
                echeancier.append({
                    "date": d,
                    "montant": float(m)
                })
            except:
                pass

    # 💾 Sauvegarde de l’échéancier manuel (sans contrôle du reste)
    devis["echeancier_manuel"] = echeancier
    
    # Total informatif uniquement
    devis["echeancier_total"] = round(
        sum(e["montant"] for e in echeancier), 2
    )
    
    save_data(data)


    return redirect(url_for("plan_financement_devis", devis_id=devis_id))


@app.route("/admin-devis/envoyer-plan/<devis_id>", methods=["POST"])
@login_required
def envoyer_plan_financement(devis_id):
    data = load_data()

    devis = next(
        (d for d in data.get("demandes", [])
         if d.get("id") == devis_id
         and d.get("motif") == "Demande de devis détaillé"),
        None
    )
    if not devis:
        abort(404)

    plan_url = url_for("plan_public", token=devis.get("token_plan"), _external=True)

    email = devis.get("mail")
    prenom = devis.get("prenom", "").strip()
    
    # 🔎 Formation = dans devis["details"] (JSON)
    try:
        infos = json.loads(devis.get("details", "{}"))
    except:
        infos = {}
    
    formation = (infos.get("formation") or "").strip()
    
    formation_label = {
        "A3P": "A3P – Agent de Protection Physique des Personnes",
        "APS": "APS – Agent de Prévention et de Sécurité",
        "VTC": "VTC – Chauffeur de transport avec chauffeur",
        "DESP_INIT": "DESP – Dirigeant d’entreprise de sécurité (initial)",
        "DESP_VAE": "DESP – Dirigeant d’entreprise de sécurité (VAE)"
    }.get(formation, formation)


    subject = "📄 Votre devis détaillé — Intégrale Academy"

    # ✅ TEXTE BRUT aligné avec le HTML
    plain = (
        f"Bonjour {prenom},\n\n"
        f"Je fais suite à votre demande de devis concernant notre formation {formation_label}.\n"
        "Je vous prie de bien vouloir trouver ci-dessous votre devis détaillé :\n\n"
        f"{plan_url}\n\n"
        "Vous pouvez également télécharger le dossier de présentation de notre formation :\n"
        "https://www.integraleacademy.com/dossiersfc\n\n"
        "Si vous avez la moindre question, n'hésitez pas à nous contacter au 04 22 47 07 68.\n\n"
        "Bien cordialement,\n"
        "Clément VAILLANT - Directeur Intégrale Academy\n"
        "Ce lien est personnel et sécurisé."
    )

    # ✅ HTML = ton “2e texte”, mais avec la vraie variable Python
    html = _wrap_html(
        "<h1>📄 Votre devis détaillé</h1>",
        f"""
        <p>Bonjour <strong>{prenom}</strong>,</p>

        <p>
          Je fais suite à votre demande de devis concernant notre formation <strong>{formation_label}</strong>.
          <br>
          Je vous prie de bien vouloir trouver ci-dessous votre <strong>devis détaillé</strong> :
        </p>

        <p style="text-align:center;margin:24px 0 10px;">
          <a href="{plan_url}"
             style="display:inline-block;
                    padding:14px 26px;
                    background:#0d6efd;
                    color:white;
                    text-decoration:none;
                    border-radius:8px;
                    font-weight:700;">
            👉 Consulter mon devis détaillé
          </a>
        </p>

        <p style="text-align:center;margin:10px 0 24px;">
          <a href="https://www.integraleacademy.com/dossiersfc"
             style="display:inline-block;
                    padding:14px 26px;
                    background:#0f1f33;
                    color:white;
                    text-decoration:none;
                    border-radius:8px;
                    font-weight:700;">
            📎 Télécharger le dossier de présentation de notre formation
          </a>
        </p>

        <p style="margin:0 0 10px;">
          Si vous avez la moindre question, n'hésitez pas à nous contacter au
          <strong>04 22 47 07 68</strong>.
        </p>

        <p style="font-size:13px;color:#666;margin:0;">
          Ce lien est personnel et sécurisé.
        </p>

        <p style="margin-top:16px;">
          Bien cordialement,<br>
          <strong>Clément VAILLANT - Directeur Intégrale Academy</strong>
        </p>
        """
    )

    # ... ensuite ton envoi email (smtp/brevo/etc.) avec plain+html


    # Envoi email
    send_email_html(
        to_emails=email,
        subject=subject,
        plain_text=plain,
        html_body=html
    )

    # ---------------------------------
    # Statut + sauvegarde
    # ---------------------------------
    devis["statut_devis"] = "Envoyé"
    devis["date_envoi_plan"] = datetime.datetime.now(
        pytz.timezone("Europe/Paris")
    ).strftime("%d/%m/%Y %H:%M")

    # 📞 Créer la demande de rappel dans l'admin uniquement quand le devis est envoyé
    rappel_existant = next(
        (
            d for d in data.get("demandes", [])
            if d.get("source_devis_id") == devis_id
            and d.get("motif") == "Rappel suite devis envoyé"
        ),
        None
    )

    if not rappel_existant:
        demande_rappel_devis = {
            "id": str(uuid.uuid4()),
            "source_devis_id": devis_id,
            "nom": devis.get("nom"),
            "prenom": devis.get("prenom"),
            "telephone": devis.get("telephone"),
            "mail": devis.get("mail"),
            "motif": "Rappel suite devis envoyé",
            "details": (
                "Créée automatiquement après clic sur ‘Devis envoyé’.\n"
                f"Formation : {formation_label or 'Non précisée'}\n"
                f"Session : {(infos.get('dates') or 'Non précisée')}"
            ),
            "date": datetime.datetime.now(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M"),
            "statut": "A rappeler",
            "attribution": "Mohamed",
            "commentaire": "",
            "commentaire_admin": "",
            "mail_confirme": "",
            "mail_erreur": "",
            "mail_contenu": "",
            "mail_html": "",
            "pieces_jointes": [],
            "reponses": [],
            "is_doublon": False,
            "rappel_date": "",
            "plage": ""
        }
        data["demandes"].append(demande_rappel_devis)

        try:
            envoyer_mail_attribution_mohamed(demande_rappel_devis)
        except:
            pass

    save_data(data)

    return redirect(url_for("admin_devis"))



@app.route("/plan/<token>")
def plan_public(token):
    data = load_data()

    devis = next(
        (d for d in data.get("demandes", [])
         if d.get("motif") == "Demande de devis détaillé"
         and d.get("token_plan") == token),
        None
    )

    if not devis:
        return "Lien invalide ou expiré", 404

    try:
        infos = json.loads(devis.get("details", "{}"))
    except:
        infos = {}

    formation = infos.get("formation")

    formation_label = {
        "A3P": "A3P – Agent de Protection Physique des Personnes",
        "APS": "APS – Agent de Prévention et de Sécurité",
        "VTC": "VTC – Chauffeur de transport avec chauffeur",
        "DESP_INIT": "DESP – Dirigeant d’entreprise de sécurité (initial)",
        "DESP_VAE": "DESP – Dirigeant d’entreprise de sécurité (VAE)"
    }.get(formation, formation)

    devis_ctx = build_devis_context(
        formation_code=formation,
        formation_label=formation_label,
        dates_txt=infos.get("dates", ""),
        sequence=1
    )


    TARIFS = {
        "A3P": 4200,
        "APS": 1650,
        "VTC": 1600,
        "DESP_INIT": 4300,
        "DESP_VAE": 3800
    }

    tarif = TARIFS.get(formation, 0)

    try:
        cpf = int(float(infos.get("cpf_montant", 0)))
    except:
        cpf = 0

    ft = max(tarif - cpf, 0) if infos.get("france_travail") == "OUI" else 0
    reste_avec_ft = max(tarif - cpf - ft, 0)
    reste_sans_ft = max(tarif - cpf, 0)

    # 🔁 Échéancier : manuel PRIORITAIRE, sinon automatique
    if devis.get("echeancier_manuel") and len(devis["echeancier_manuel"]) > 0:
        echeances = devis["echeancier_manuel"]
    else:
        # date examen
        date_examen = None
        try:
            if infos.get("date_examen"):
                date_examen = datetime.datetime.strptime(
                    infos["date_examen"], "%Y-%m-%d"
                ).date()
        except:
            date_examen = None
    
        echeances = build_echeances_mensuelles(
            reste=reste_sans_ft,
            date_devis=datetime.date.today(),
            date_examen=date_examen
        )


    return render_template(
        "plan_financement.html",
        prenom=devis.get("prenom"),
        nom=devis.get("nom"),
        email=devis.get("mail"),
        formation_label=formation_label,
        dates=infos.get("dates"),
        cpf=cpf,
        ft=ft,
        reste_avec_ft=reste_avec_ft,
        reste_sans_ft=reste_sans_ft,
        echeances=echeances,
        readonly=True,
        **devis_ctx
    )


@app.route("/plan-simulation/<token>")
def plan_simulation_public(token):
    data = load_data()
    plans = data.get("plans_simulation", [])
    plan = next((p for p in plans if p.get("token") == token), None)
    if not plan:
        return "Lien invalide ou expiré", 404

    simulation = plan.get("simulation") or {}
    formation_code = simulation.get("formation")
    formation_label = PLAN_FORMATIONS.get(formation_code, formation_code)
    devis_ctx = build_devis_context(
        formation_code=formation_code,
        formation_label=formation_label,
        dates_txt=simulation.get("dates", ""),
        sequence=1
    )

    return render_template(
        "plan_financement.html",
        prenom=plan.get("prenom"),
        nom=plan.get("nom"),
        email=plan.get("mail"),
        formation_label=formation_label,
        dates=simulation.get("dates", ""),
        cpf=simulation.get("cpf", 0),
        ft=simulation.get("ft", 0),
        reste_avec_ft=simulation.get("reste_avec_ft", 0),
        reste_sans_ft=simulation.get("reste_sans_ft", 0),
        echeances=plan.get("echeances") or [],
        readonly=True,
        **devis_ctx
    )



@app.route("/lookup_hebergement.json")
def lookup_hebergement():
    email = (request.args.get("email") or request.args.get("mail") or "").strip().lower()
    nom = (request.args.get("nom") or "").strip().lower()
    prenom = (request.args.get("prenom") or "").strip().lower()
    session_txt = " ".join((request.args.get("session") or "").split())

    if not email and not (nom and prenom):
        return {"ok": False, "error": "missing email or (nom+prenom)"}, 400, {
            "Access-Control-Allow-Origin": "*"
        }

    data = load_data()
    hebergements = data.get("hebergements", [])

    def norm(s: str) -> str:
        return " ".join((s or "").strip().lower().split())

    def match(h):
        h_mail = norm(h.get("mail"))  # ou: norm(h.get("mail") or h.get("email"))
        h_nom = norm(h.get("nom"))
        h_prenom = norm(h.get("prenom"))

        in_mail = norm(email)
        in_nom = norm(nom)
        in_prenom = norm(prenom)

        # 1) Email si possible
        email_ok = False
        if in_mail and h_mail:
            email_ok = (h_mail == in_mail)

        # 2) Fallback nom/prenom si possible
        name_ok = False
        if in_nom and in_prenom and h_nom and h_prenom:
            name_ok = (h_nom == in_nom and h_prenom == in_prenom)

        if not (email_ok or name_ok):
            return False

        # 3) session optionnelle
        if session_txt:
            hs = norm(h.get("session"))
            return hs == norm(session_txt)

        return True

    reserved = any(match(h) for h in hebergements)

    return {
        "ok": True,
        "reserved": reserved,
        "status": "réservé" if reserved else "inconnu"
    }, 200, {"Access-Control-Allow-Origin": "*"}











        



if __name__ == "__main__":
    app.run(debug=True)
