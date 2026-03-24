from flask import Flask, render_template, request, send_from_directory, url_for, redirect, abort
from flask import render_template_string
import json, os, datetime, uuid, pytz, smtplib, re, copy, unicodedata, tempfile
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

FORMATION_CENTRES = {
    "cote_azur": "Intégrale Academy Côte d’Azur",
    "auvergne": "Intégrale Academy Terres d’Auvergne",
    "paris": "Intégrale Academy Paris",
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

DEFAULT_FORMATION_SESSIONS = {
    "cote_azur": {
        "APS": [
            {"label": "Du 29 avril au 9 juin 2026 - examen le 10 juin 2026", "badge": ""},
            {"label": "Du 26 mai au 29 juin 2026 - examen le 30 juin 2026", "badge": ""},
            {"label": "Du 8 juillet au 12 août 2026 - examen le 13 août 2026", "badge": ""},
            {"label": "Du 7 septembre au 9 octobre 2026 - examen le 12 octobre 2026", "badge": ""},
            {"label": "Du 3 novembre au 8 décembre 2026 - examen le 9 décembre 2026", "badge": ""}
        ],
        "A3P": [
            {"label": "Du 30 mars au 2 juin 2026 - examen le 3 juin 2026", "badge": ""},
            {"label": "Du 8 juin au 4 août 2026 - examen le 5 août 2026", "badge": ""},
            {"label": "Du 1er septembre au 27 octobre 2026 - examen le 28 octobre 2026", "badge": ""},
            {"label": "Du 9 novembre 2026 au 19 janvier 2027 - examen le 20 janvier 2027", "badge": ""}
        ],
        "DESP_INIT": [
            {"label": "Du 27 avril au 15 juin 2026 (présentiel du 2 au 15/06) - examen le 16 juin 2026", "badge": ""},
            {"label": "Du 22 juin au 10 août 2026 (présentiel du 28/07 au 10/08) - examen le 11 août 2026", "badge": ""},
            {"label": "Du 7 septembre au 23 octobre 2026 (présentiel du 12 au 23/10) - examen le 26 octobre 2026", "badge": ""},
            {"label": "Du 2 novembre au 21 décembre 2026 (présentiel du 8 au 21/12) - examen le 22 décembre 2026", "badge": ""}
        ],
        "DESP_VAE": []
    },
    "auvergne": {
        "A3P": [
            {"label": "21 octobre au 17 décembre 2026", "badge": ""}
        ],
        "DESP_INIT": [
            {"label": "5 octobre au 19 novembre 2026 (A distance du 05/10 au 06/11/2026 – Présentiel du 09/11 au 19/11/2026)", "badge": ""}
        ],
        "DESP_VAE": []
    },
    "paris": {
        "DESP_INIT": [
            {"label": "Du 7 septembre au 23 octobre 2026 (présentiel à Paris) - examen le 26 octobre 2026", "badge": ""},
            {"label": "Du 2 novembre au 21 décembre 2026 (présentiel à Paris) - examen le 22 décembre 2026", "badge": ""}
        ],
        "DESP_VAE": []
    }
}

def get_formation_sessions(data_store=None):
    source = data_store if isinstance(data_store, dict) else load_data()
    sessions = source.get("formation_sessions")
    if isinstance(sessions, dict):
        merged = copy.deepcopy(DEFAULT_FORMATION_SESSIONS)
        for centre_code, formation_rows in sessions.items():
            if not isinstance(formation_rows, dict):
                continue
            merged.setdefault(centre_code, {})
            for formation_code, rows in formation_rows.items():
                if isinstance(rows, list):
                    merged[centre_code][formation_code] = rows
        return merged
    return copy.deepcopy(DEFAULT_FORMATION_SESSIONS)


def get_simulator_dates_options(data_store=None):
    sessions = get_formation_sessions(data_store)
    options = {}
    for centre_code in FORMATION_CENTRES:
        centre_sessions = sessions.get(centre_code, {})
        options[centre_code] = {}
        for formation_code, rows in centre_sessions.items():
            options[centre_code][formation_code] = [
                {
                    "label": (row.get("label") or "").strip(),
                    "badge": (row.get("badge") or "").strip(),
                    "date_examen": (row.get("date_examen") or "").strip(),
                }
                for row in rows
                if (row.get("label") or "").strip()
            ]
    return options


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

    match = re.search(r"examen le (\d{1,2}) ([a-zà-ÿ]+) (\d{4})", dates_txt, re.IGNORECASE)
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

def compute_plan_financement_simulation(formation, dates_txt, cpf_value, france_travail, date_examen_str, centre_code="cote_azur"):
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
    echeancier_message = ""
    if date_examen_str:
        try:
            date_examen = datetime.datetime.strptime(
                date_examen_str, "%Y-%m-%d"
            ).date()
        except ValueError:
            date_examen = None
            echeancier_message = "⚠️ Impossible de proposer un échéancier : la date d’examen est invalide."
    else:
        echeancier_message = "⚠️ Impossible de proposer un échéancier : la date d’examen est absente dans la session sélectionnée."

    echeances = build_echeances_mensuelles(
        reste=reste_sans_ft,
        date_devis=datetime.date.today(),
        date_examen=date_examen
    )

    if date_examen and not echeances and not echeancier_message:
        echeancier_message = "⚠️ Aucun échéancier possible : la formation doit être soldée avant l’examen."

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
        "centre": centre_code or "cote_azur",
        "dates": dates_txt or "",
        "date_examen": date_examen_str,
        "cpf": cpf,
        "tarif": tarif,
        "ft": ft,
        "france_travail": france_travail,
        "reste_avec_ft": reste_avec_ft,
        "reste_sans_ft": reste_sans_ft,
        "echeancier_message": echeancier_message,
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
def _data_file_has_content(path):
    """Retourne True si le JSON contient des données utiles (demandes/archives/hebergements)."""
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, list):
            return len(payload) > 0
        if isinstance(payload, dict):
            return any([
                bool(payload.get("demandes")),
                bool(payload.get("archives")),
                bool(payload.get("hebergements")),
            ])
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False
    return False


def _migrate_legacy_data_if_needed(legacy_file, target_file):
    """
    Copie l'ancien data.json du repo vers un disque persistant uniquement si:
    - la destination n'existe pas encore,
    - et le fichier legacy contient des données.
    """
    if not legacy_file or not target_file:
        return
    if not os.path.exists(legacy_file) or os.path.exists(target_file):
        return
    if not _data_file_has_content(legacy_file):
        return
    try:
        os.makedirs(os.path.dirname(target_file), exist_ok=True)
        with open(legacy_file, "r", encoding="utf-8") as src:
            payload = src.read()
        with open(target_file, "w", encoding="utf-8") as dst:
            dst.write(payload)
    except OSError:
        pass


def _resolve_data_file():
    """
    Résout le fichier data.json en privilégiant :
    1) les chemins explicites via variables d'environnement,
    2) les emplacements persistants Render connus,
    3) le fichier historique du repo (fallback local uniquement).
    """
    base_dir = os.path.dirname(__file__)
    legacy_file = os.path.join(base_dir, "data.json")

    explicit_file = os.getenv("DATA_FILE")
    if explicit_file:
        explicit_dir = os.path.dirname(explicit_file) or "."
        try:
            os.makedirs(explicit_dir, exist_ok=True)
            if os.access(explicit_dir, os.W_OK):
                return explicit_file
        except OSError:
            pass

    dir_candidates = [
        os.getenv("DATA_DIR"),
        os.getenv("RENDER_DISK_PATH"),
        os.getenv("RENDER_DISK_MOUNT_PATH"),
        "/var/data",
        "/mnt/data",
        os.path.join(base_dir, "data"),
    ]

    writable_files = []
    for candidate in dir_candidates:
        if not candidate:
            continue
        try:
            os.makedirs(candidate, exist_ok=True)
            if os.access(candidate, os.W_OK):
                data_file = os.path.join(candidate, "data.json")
                if os.path.exists(data_file):
                    return data_file
                writable_files.append(data_file)
        except OSError:
            continue

    if writable_files:
        preferred_file = writable_files[0]
        _migrate_legacy_data_if_needed(legacy_file, preferred_file)
        return preferred_file

    if os.path.exists(legacy_file):
        return legacy_file

    return legacy_file


DATA_FILE = _resolve_data_file()
DATA_DIR = os.path.dirname(DATA_FILE)
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

DEFAULT_DATA = {
    "demandes": [],
    "archives": [],
    "compteur_traitees": 0,
    "hebergements": [],
    "formation_sessions": {},
    "plans_simulation": {},
}

# -------------------------------------------------------------------
# Utils
# -------------------------------------------------------------------
def load_data():
    def _normalize_payload(payload):
        if isinstance(payload, dict):
            normalized = dict(payload)
            for key, default in DEFAULT_DATA.items():
                if key not in normalized:
                    normalized[key] = default.copy() if isinstance(default, (list, dict)) else default
            return normalized
        if isinstance(payload, list):
            normalized = dict(DEFAULT_DATA)
            normalized["demandes"] = payload
            normalized["archives"] = []
            normalized["compteur_traitees"] = 0
            return normalized
        return None

    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = _normalize_payload(json.load(f))
                if data is not None:
                    return data
        except (json.JSONDecodeError, OSError):
            # fichier corrompu/inaccessible : on tente d'abord une restauration auto via backup
            backup_path = f"{DATA_FILE}.bak"
            if os.path.exists(backup_path):
                try:
                    with open(backup_path, "r", encoding="utf-8") as backup:
                        backup_data = _normalize_payload(json.load(backup))
                    if backup_data is not None:
                        return backup_data
                except (json.JSONDecodeError, OSError):
                    pass

            # sinon on garde une copie puis on repart proprement
            try:
                backup_path = f"{DATA_FILE}.corrupted"
                os.replace(DATA_FILE, backup_path)
            except OSError:
                pass
    return {
        "demandes": [],
        "archives": [],
        "compteur_traitees": 0,
        "hebergements": [],
        "formation_sessions": {},
        "plans_simulation": {},
    }

def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)

    # Sauvegarde de sécurité (anti-perte en cas de mauvaise manip)
    if os.path.exists(DATA_FILE):
        backup_file = f"{DATA_FILE}.bak"
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as src:
                previous = src.read()
            with open(backup_file, "w", encoding="utf-8") as dst:
                dst.write(previous)
        except OSError:
            pass

    temp_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=os.path.dirname(DATA_FILE),
            prefix=f"{os.path.basename(DATA_FILE)}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            temp_file = f.name
            json.dump(data, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_file, DATA_FILE)
    finally:
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass

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
          <img src="https://integraleacademy.file.force.com/file-asset-public/Logo_Integrale_Academy_officielpdf?oid=00DJ9000000PT9F" alt="Intégrale Academy" height="56" style="display:block;height:56px;width:auto;max-width:220px;">
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


def build_vae_desp_email_html():
    return """<html style="overflow-y: hidden;">
<head>
    <title></title>
</head>
<body style="height: auto; min-height: auto;">
<div style="font-family: Arial, sans-serif; max-width:650px; margin:auto; background:#f9f9f9; padding:20px;">
<div style="background:#ffffff; border-radius:12px; box-shadow:0 2px 8px rgba(0,0,0,0.08); overflow:hidden;">
<div style="text-align:center; padding:25px 20px 10px 20px;"><img alt="Intégrale Academy" src="https://integraleacademy.file.force.com/file-asset-public/Logo_Integrale_Academy_officielpdf?oid=00DJ9000000PT9F" style="max-width:110px; height:auto; display:block; margin:auto;" />
<h2 style="color:#000; font-size:20px; margin:12px 0 0 0;">Intégrale Academy</h2>
</div>

<div style="background:#F4C45A; padding:14px; text-align:center;">
<h3 style="margin:0; font-size:18px; color:#000;">📝 VAE – Dirigeant d’Entreprise de Sécurité Privée (RNCP40385)</h3>
</div>

<div style="padding:25px; font-size:15px; color:#333; line-height:1.7;">
<p>Bonjour,</p>

<p>Je fais suite à notre échange concernant la <strong>Validation des Acquis de l’Expérience (VAE)</strong> du titre <strong>RNCP40385 – Dirigeant d’Entreprise de Sécurité Privée</strong>, délivré par notre centre de formation Intégrale Academy.</p>

<p>La VAE vous permet d’obtenir un <strong>titre reconnu par l’État (niveau 5 – Bac+2)</strong> sur la base de votre expérience professionnelle dans la sécurité privée ainsi que de vos fonctions de direction, de management et de gestion d’entreprise.</p>

<p><strong>📄 Dossier de présentation :</strong></p>

<div style="text-align:center; margin:20px 0;"><a href="https://www.integraleacademy.com/dossiersfc" style="display:inline-block; padding:12px 22px; background:#0f1f33; color:#ffffff; text-decoration:none; border-radius:8px; font-weight:bold;">📄 Télécharger le dossier de présentation </a></div>

<hr style="border:none; border-top:1px solid #eee; margin:30px 0;" />
<div style="margin:30px 0; text-align:center;"><a href="https://gestionstagiaires-r5no.onrender.com/vae-desp" style="display:inline-block; padding:16px 28px; background:#F4C45A; color:#000; text-decoration:none; border-radius:10px; font-weight:bold; font-size:16px;">🚀 Démarrer ma VAE maintenant </a></div>

<p style="text-align:center; font-size:14px; color:#666;">👉 Vous pouvez commencer immédiatement votre Livret 1 en ligne en cliquant ici.</p>

<hr style="border:none; border-top:1px solid #eee; margin:30px 0;" />
<h3 style="color:#000;">🧭 Les étapes de votre parcours VAE</h3>

<h4 style="margin-top:20px;">1️⃣ Rédaction du Livret 1 (dossier de faisabilité)</h4>

<p>Vous complétez votre dossier de faisabilité en ligne. Ce document permet de présenter votre parcours professionnel, vos fonctions exercées et vos responsabilités. Il s’agit de la « photographie » de votre expérience afin de vérifier l’adéquation avec le référentiel du titre.</p>

<p><strong>⏳ Durée estimée : environ 30 minutes.</strong></p>

<h4 style="margin-top:20px;">2️⃣ Étude du Livret 1 et attestation de recevabilité</h4>

<p>La commission pédagogique étudie votre dossier. Si les éléments sont conformes et suffisants, une <strong>attestation de recevabilité</strong> vous est délivrée.</p>

<p>À cette étape, nous mettons en place la convention de VAE et procédons au règlement de l’<strong>acompte de 30 % (1 140 €)</strong>.</p>

<h4 style="margin-top:20px;">3️⃣ Constitution du Livret 2</h4>

<p>Le Livret 2 constitue le cœur de votre démarche. Vous y détaillez précisément :</p>

<ul style="margin-left:20px;">
    <li>Vos activités professionnelles</li>
    <li>Vos missions de direction</li>
    <li>Vos responsabilités managériales</li>
    <li>Vos compétences réglementaires et opérationnelles</li>
    <li>Les situations professionnelles rencontrées</li>
</ul>

<p>Ce dossier servira de base à l’évaluation par le jury de certification.</p>

<h4 style="margin-top:20px;">4️⃣ Étude du Livret 2</h4>

<p>La commission analyse votre dossier. Si l’ensemble est conforme et complet, une date de passage devant le jury est programmée.</p>

<h4 style="margin-top:20px;">5️⃣ Passage devant le jury de certification</h4>

<p>Avant le jury, le solde de la formation est à régler (<strong>2 520 €</strong>).</p>

<p>L’entretien dure environ <strong>45 minutes à 1 heure</strong> et peut se dérouler :</p>

<ul style="margin-left:20px;">
    <li>En présentiel à Nice (06)</li>
    <li>En visioconférence</li>
</ul>

<p>Le jury échange avec vous sur votre parcours et vérifie la maîtrise des compétences attendues à travers des questions concrètes liées à votre expérience.</p>

<h4 style="margin-top:20px;">6️⃣ Obtention de votre certification</h4>

<p>Après validation par le jury, vous obtenez officiellement le titre RNCP40385 – Dirigeant d’Entreprise de Sécurité Privée.</p>

<hr style="border:none; border-top:1px solid #eee; margin:30px 0;" />
<h3 style="color:#000;">💶 Tarif et financement</h3>

<p><strong>Tarif global : 3 800 € TTC</strong></p>

<p>Ce tarif comprend :</p>

<ul style="margin-left:20px;">
    <li>L’étude de recevabilité</li>
    <li>L’accompagnement méthodologique Livret 2</li>
    <li>La préparation au jury</li>
    <li>Les frais de certification</li>
</ul>

<p>👉 Financement possible via votre <strong>Compte Personnel de Formation (CPF)</strong>.</p>

<div style="margin:25px 0; padding:18px; background:#f5f5f5; border-radius:10px; text-align:center; border:1px solid #e4e4e4;">
<p style="margin:0 0 12px 0; font-size:15px; color:#333;">Besoin d’un <strong>devis personnalisé</strong> avec plan de financement ?</p>
<a href="https://assistance-alw9.onrender.com/demande-devis" style="display:inline-block; padding:12px 22px; background:#0f1f33; color:#fff; text-decoration:none; border-radius:8px; font-weight:bold;">🧾 Demander un devis personnalisé </a></div>

<hr style="border:none; border-top:1px solid #eee; margin:30px 0;" />
<h3 style="color:#000;">📞 Échanger avec nous</h3>

<p>Vous pouvez planifier un rendez-vous téléphonique pour faire le point sur votre situation :</p>

<div style="text-align:center; margin:20px 0;"><a href="https://calendly.com/integraleacademy/dirigeant" style="display:inline-block; padding:12px 22px; background:#F4C45A; color:#000; text-decoration:none; border-radius:8px; font-weight:bold;">📞 Planifier un rendez-vous </a></div>

<p>Je reste bien entendu à votre disposition pour tout renseignement complémentaire.</p>

<p>Bien cordialement,<br />
<br />
<strong>Clément VAILLANT</strong><br />
Directeur – Intégrale Academy<br />
ecole@integraleacademy.com<br />
📍 54 chemin du Carreou – 83480 Puget-sur-Argens</p>
</div>

<div style="padding:20px; font-size:12px; color:#555; text-align:center; border-top:1px solid #eee; line-height:1.5;">© Intégrale Academy — Merci de votre confiance 💛<br />
SIREN 840 899 884 – NDA 93830600283 – Certification QUALIOPI n°03169<br />
integraleacademy.com</div>
</div>
</div>
</body>
</html>
"""


def _format_selected_session_date(dates_txt: str) -> str:
    if not dates_txt:
        return ""
    return dates_txt.strip().replace(" - examen le ", " — examen le ")


def _centre_label_and_address(centre_code: str):
    centres = {
        "cote_azur": (
            "Intégrale Academy Côte d’Azur",
            "54 chemin du Carreou — 83480 PUGET SUR ARGENS (Var)",
        ),
        "auvergne": (
            "Intégrale Academy Terres d’Auvergne",
            "650 route d'Aumont — 15130 Arpajon-sur-Cère",
        ),
        "paris": (
            "Intégrale Academy Paris",
            "Paris et Île-de-France (adresse communiquée lors de l’inscription)",
        ),
    }
    return centres.get(
        centre_code,
        (
            "Intégrale Academy Côte d’Azur",
            "54 chemin du Carreou — 83480 PUGET SUR ARGENS (Var)",
        ),
    )


def build_a3p_email_html(prenom: str, dates_txt: str, centre_code: str):
    session_date = _format_selected_session_date(dates_txt)
    centre_label, centre_address = _centre_label_and_address(centre_code)
    session_html = (
        f"<p>📅 <strong>{session_date}</strong></p>"
        if session_date
        else "<p>📅 <strong>Dates communiquées lors de notre échange.</strong></p>"
    )

    return f"""<html style="overflow-y: hidden;">
<head>
    <title></title>
</head>
<body style="height: auto; min-height: auto;">
<div style="font-family: Arial, sans-serif; max-width:600px; margin:auto; background:#f9f9f9; padding:20px;">
<div style="background:#fff; border-radius:12px; box-shadow:0 2px 8px rgba(0,0,0,0.1); overflow:hidden;">
<div style="text-align:center; padding:20px 20px 10px 20px;"><img alt="Intégrale Academy" src="https://integraleacademy.file.force.com/file-asset-public/Logo_Integrale_Academy_officielpdf?oid=00DJ9000000PT9F" style="max-width:100px; height:auto; display:block; margin:auto;" />
<h2 style="color:#000; font-size:18px; margin:10px 0 0 0;">Intégrale Academy</h2>
</div>

<div style="background:#F4C45A; padding:12px; text-align:center;">
<h3 style="margin:0; font-size:18px; color:#000;">🛡️ Formation Agent de Protection Physique des Personnes (A3P)</h3>
</div>

<div style="padding:20px; font-size:15px; color:#333; line-height:1.6;">
<p>Bonjour {prenom},</p>

<p>Je fais suite à notre conversation téléphonique concernant notre formation <strong>Agent de Protection Physique des Personnes (A3P – Bodyguard)</strong>, titre reconnu par l’État (<strong>RNCP38002 – niveau 4</strong>).</p>

<p>Cette formation permet d’acquérir toutes les compétences nécessaires pour intervenir en tant que <strong>garde du corps</strong>, dans le respect strict de la réglementation française. Elle prépare également à l’obtention de la <strong>carte professionnelle Agent de protection physique des personnes</strong> délivrée par le CNAPS (Ministère de l’Intérieur).</p>

<p><strong>📄 Dossier de présentation :</strong></p>

<p style="text-align:center; margin:20px 0;"><a href="https://www.integraleacademy.com/dossiersfc" style="display:inline-block; padding:12px 22px; background:#0f1f33; color:#fff;
                  text-decoration:none; border-radius:8px; font-weight:bold;">📄 Télécharger le Dossier de présentation </a></p>

<h3 style="margin-top:25px; font-size:17px; color:#000;">🎓 Durée et organisation</h3>

<p><strong>328 heures de formation </strong>, conformément à la réglementation.</p>

<h3 style="margin-top:25px; font-size:17px; color:#000;">🏫 Votre session</h3>

{session_html}

<p>Lieu : <strong>{centre_label}</strong><br />
{centre_address}</p>

<h3 style="margin-top:25px; font-size:17px; color:#000;">💶 Tarif & financement</h3>

<p>Tarif : <strong>4200 € TTC</strong><br />
Formation finançable via votre <strong>Compte Personnel de Formation (CPF)</strong>.</p>

<p>👉 Vous devrez activer votre <strong>Identité Numérique La Poste</strong> pour valider le dossier CPF.</p>

<h3 style="margin-top:25px; font-size:17px; color:#000;">🛏️ Hébergement</h3>

<p>Nous proposons une solution d'hébergement au sein du centre de formation au tarif de :<br />
<strong>300 € TTC pour la durée totale de la formation</strong></p>

<p>Dortoir collectif, salle de bain, douche, cuisine équipée, machine à laver et sèche-linge. 👉 Paiement sur place (chèque ou espèces), réservation à effectuer lors de votre inscription.</p>

<div style="margin:25px 0; padding:18px; background:#f5f5f5; border-radius:10px; text-align:center; border:1px solid #e4e4e4;">
<p style="margin:0 0 12px 0; font-size:15px; color:#333;">Si vous souhaitez un <strong>devis personnalisé</strong> avec un plan de financement détaillé :</p>
<a href="https://assistance-alw9.onrender.com/demande-devis" style="display:inline-block; padding:12px 22px; background:#F4C45A; color:#000;
                  text-decoration:none; border-radius:8px; font-weight:bold;">🧾 Demander un devis personnalisé </a></div>

<h3 style="margin-top:25px; font-size:17px; color:#000;">📞 Prochaine étape</h3>

<p>Pour réserver votre place ou poser vos questions, vous pouvez planifier un rendez-vous téléphonique ici :</p>

<p style="text-align:center; margin:20px 0;"><a href="https://calendly.com/integraleacademy/apr" style="display:inline-block; padding:12px 22px; background:#0f1f33; color:#fff;
                  text-decoration:none; border-radius:8px; font-weight:bold;">📞 Planifier un rendez-vous </a></p>

<p>Je reste à votre disposition pour toute information complémentaire.</p>

<p>Je vous souhaite une excellente journée,<br />
<br />
<strong>Clément VAILLANT</strong><br />
Directeur – Intégrale Academy<br />
ecole@integraleacademy.com – integraleacademy.com<br />
📍 54 chemin du Carreou – 83480 Puget-sur-Argens</p>
</div>

<div style="padding:20px; font-size:12px; color:#555; text-align:center; border-top:1px solid #eee; line-height:1.5;">© Intégrale Academy — Merci de votre confiance 💛<br />
54 chemin du Carreou 83480 PUGET SUR ARGENS / 142 rue de Rivoli 75001 PARIS<br />
SIREN 840 899 884 - NDA 93830600283 - Certification Nationale QUALIOPI : n°03169 en date du 21/10/2024<br />
UAI Côte d'Azur 0831774C - UAI Paris 0756548K<br />
<a href="https://www.integraleacademy.com" style="color:#0f1f33; text-decoration:none;">integraleacademy.com</a></div>
</div>
</div>
</body>
</html>
"""


def build_aps_email_html(prenom: str, dates_txt: str, centre_code: str):
    session_date = _format_selected_session_date(dates_txt)
    centre_label, centre_address = _centre_label_and_address(centre_code)
    session_html = (
        f"<p style=\"margin:0; line-height:1.65;\">📅 <strong>{session_date}</strong></p>"
        if session_date
        else "<p style=\"margin:0; line-height:1.65;\">📅 <strong>Date transmise lors de notre échange téléphonique.</strong></p>"
    )

    return f"""<html style="overflow-y: hidden;">
<head>
    <title></title>
</head>
<body style="height: auto; min-height: auto;">
<div style="font-family: Arial, sans-serif; max-width:600px; margin:auto; background:#f9f9f9; padding:20px;">
<div style="background:#fff; border-radius:12px; box-shadow:0 2px 8px rgba(0,0,0,0.1); overflow:hidden;">
<div style="text-align:center; padding:20px 20px 10px 20px;"><img alt="Intégrale Academy" src="https://integraleacademy.file.force.com/file-asset-public/Logo_Integrale_Academy_officielpdf?oid=00DJ9000000PT9F" style="max-width:100px; height:auto; display:block; margin:auto;" />
<h2 style="color:#000; font-size:18px; margin:10px 0 0 0;">Intégrale Academy</h2>
</div>

<div style="background:#F4C45A; padding:12px; text-align:center;">
<h3 style="margin:0; font-size:18px; color:#000;">🛡️ Formation Agent de Sécurité Privée (APS)</h3>
</div>

<div style="padding:20px; font-size:15px; color:#333; line-height:1.65;">
<p>Bonjour {prenom},</p>
<p>Pour faire suite à votre demande de renseignements, nous vous prions de bien vouloir trouver ci-dessous <strong>l’ensemble des informations détaillées</strong> concernant notre formation <strong>Agent de Sécurité Privée (APS)</strong>.</p>
<h3 style="margin-top:22px; font-size:17px; color:#000;">📄 Dossier de présentation 2026</h3>
<p style="text-align:center; margin:18px 0;"><a href="https://www.integraleacademy.com/dossiersfc" style="display:inline-block; padding:12px 22px; background:#0f1f33; color:#fff; text-decoration:none; border-radius:8px; font-weight:bold;">📄 Télécharger le Dossier de présentation </a></p>
<h3 style="margin-top:25px; font-size:17px; color:#000;">📅 Votre session</h3>
<div style="margin:12px 0; padding:14px; background:#f5f5f5; border-radius:10px; border:1px solid #e8e8e8;">{session_html}</div>
<p style="margin-top:10px;"><strong>⚠️ Places limitées :</strong> formation limitée à <strong>12 personnes</strong> par session.</p>
<h3 style="margin-top:25px; font-size:17px; color:#000;">💶 Tarifs</h3>
<p><strong>Formation : 1 650 € TTC</strong></p>
<h3 style="margin-top:25px; font-size:17px; color:#000;">⏱️ Durée & rythme</h3>
<p><strong>175 heures de formation</strong><br />Du <strong>lundi au vendredi</strong><br />Durée : <strong>5 semaines</strong></p>
<h3 style="margin-top:25px; font-size:17px; color:#000;">📍 Lieu</h3>
<p><strong>{centre_label}</strong><br /><strong>{centre_address}</strong></p>
<h3 style="margin-top:25px; font-size:17px; color:#000;">💳 Financement</h3>
<ul style="padding-left:20px;">
<li><strong>Compte Personnel de Formation (CPF)</strong></li>
<li><strong>Demande de financement</strong> auprès de <strong>France Travail</strong></li>
<li><strong>Financement personnel</strong> : acompte de 30 % + paiement en plusieurs fois</li>
</ul>
<p style="text-align:center; margin:20px 0;"><a href="https://lidentitenumerique.laposte.fr" style="display:inline-block; padding:12px 22px; background:#F4C45A; color:#000; text-decoration:none; border-radius:8px; font-weight:bold;">🔐 Identité numérique La Poste </a></p>
<p style="text-align:center; margin:20px 0;"><a href="https://www.integraleacademy.com/dossiersfc" style="display:inline-block; padding:12px 22px; background:#0f1f33; color:#fff; text-decoration:none; border-radius:8px; font-weight:bold;">📄 Télécharger le Dossier APS </a></p>
<p>Je reste à votre disposition pour toute information complémentaire.<br /><br /><strong>Clément VAILLANT</strong><br />Directeur – Intégrale Academy<br />ecole@integraleacademy.com – integraleacademy.com</p>
</div>
</div>
</div>
</body>
</html>
"""


def build_vtc_email_html(prenom: str, centre_code: str):
    centre_label, centre_address = _centre_label_and_address(centre_code)
    return f"""<html style="overflow-y: hidden;">
<head><title></title></head>
<body style="height: auto; min-height: auto;">
<div style="font-family: Arial, sans-serif; max-width:600px; margin:auto; background:#f9f9f9; padding:20px;">
<div style="background:#fff; border-radius:12px; box-shadow:0 2px 8px rgba(0,0,0,0.1); overflow:hidden;">
<div style="text-align:center; padding:20px 20px 10px 20px;"><img alt="Intégrale Academy" src="https://integraleacademy.file.force.com/file-asset-public/Logo_Integrale_Academy_officielpdf?oid=00DJ9000000PT9F" style="max-width:100px; height:auto; display:block; margin:auto;" />
<h2 style="color:#000; font-size:18px; margin:10px 0 0 0;">Intégrale Academy</h2></div>
<div style="background:#F4C45A; padding:12px; text-align:center;"><h3 style="margin:0; font-size:18px; color:#000;">🚗 Formation Chauffeur VTC</h3></div>
<div style="padding:20px; font-size:15px; color:#333; line-height:1.65;">
<p>Bonjour {prenom},</p>
<p>Pour faire suite à votre demande de renseignements, voici les informations détaillées concernant notre <strong>formation Chauffeur VTC</strong>.</p>
<p style="text-align:center; margin:18px 0;"><a href="https://www.integraleacademy.com/dossiersfc" style="display:inline-block; padding:12px 22px; background:#0f1f33; color:#fff; text-decoration:none; border-radius:8px; font-weight:bold;">📄 Télécharger le dossier de présentation </a></p>
<h3 style="margin-top:25px; font-size:17px; color:#000;">📅 Organisation de la formation</h3>
<p><strong>Théorie :</strong> 100 % en ligne à distance, accessible 7j/7, démarrage immédiat après inscription.<br />
<strong>Pratique :</strong> 1/2 journée de mise en situation dans nos locaux <strong>{centre_label}</strong> ({centre_address}).</p>
<p style="text-align:center; margin:16px 0;"><a href="https://www.cmar-paca.fr/galerie/1/f3ec5a86ea34eb95294dd770b94b8c23.pdf" style="display:inline-block; padding:12px 22px; background:#F4C45A; color:#000; text-decoration:none; border-radius:8px; font-weight:bold;">📅 Consulter les dates des examens </a></p>
<h3 style="margin-top:25px; font-size:17px; color:#000;">💶 Tarif</h3>
<p><strong>1 650 € TTC – FORMULE TOUT INCLUS</strong></p>
<h3 style="margin-top:25px; font-size:17px; color:#000;">💳 Financement</h3>
<ul style="padding-left:20px;"><li>CPF</li><li>France Travail</li><li>Financement personnel</li></ul>
<p style="text-align:center;"><a href="tel:0422470768" style="display:inline-block; padding:12px 22px; background:#0f1f33; color:#fff; text-decoration:none; border-radius:8px; font-weight:bold;">📞 04 22 47 07 68 </a></p>
<p>Je reste à votre disposition pour toute information complémentaire.<br /><br /><strong>Clément VAILLANT</strong><br />Directeur – Intégrale Academy<br />ecole@integraleacademy.com – integraleacademy.com</p>
</div></div></div></body></html>"""


def build_desp_init_email_html(prenom: str, dates_txt: str, centre_code: str, devis_url: str):
    session_date = _format_selected_session_date(dates_txt)
    centre_label, _ = _centre_label_and_address(centre_code)
    centre_display = centre_label.replace("Intégrale Academy ", "")
    session_html = (
        f"""
        <p style="margin:0 0 6px 0;">📅 <strong>{session_date}</strong></p>
        <p style="margin:0 0 6px 0;">Présentiel : <strong>{session_date}</strong></p>
        """
        if session_date
        else """
        <p style="margin:0 0 6px 0;">📅 <strong>XXXX</strong></p>
        <p style="margin:0 0 6px 0;">Présentiel : <strong>XXXXX</strong></p>
        """
    )

    return f"""<html style="overflow-y:hidden;">
<head><title></title></head>
<body style="height:auto; min-height:auto;">
<div style="font-family:Arial,sans-serif; max-width:640px; margin:auto; background:#f9f9f9; padding:20px;">
  <div style="background:#fff; border-radius:12px; box-shadow:0 2px 8px rgba(0,0,0,.1); overflow:hidden;">
    <div style="text-align:center; padding:20px 20px 10px 20px;">
      <img alt="Intégrale Academy" src="https://integraleacademy.file.force.com/file-asset-public/Logo_Integrale_Academy_officielpdf?oid=00DJ9000000PT9F" style="max-width:100px; height:auto; display:block; margin:auto;" />
      <h2 style="color:#000; font-size:18px; margin:10px 0 0 0;">Intégrale Academy</h2>
    </div>
    <div style="padding:20px; font-size:15px; color:#333; line-height:1.6;">
      <p>Bonjour {prenom},</p>
      <p>Je fais suite à votre demande de renseignements concernant notre formation <strong>Dirigeant d’Entreprise de Sécurité Privée (DESP)</strong>, titre reconnu par l’État (<strong>RNCP40385 – niveau 5, équivalent Bac+2</strong>).</p>
      <p>Cette formation permet d’obtenir les compétences indispensables pour créer, diriger et gérer une entreprise de sécurité privée et vous permet de demander votre agrément dirigeant auprès du CNAPS conformément à la réglementation.</p>
      <p style="margin:18px 0 8px 0;"><strong>📄 Dossier de présentation</strong></p>
      <p style="margin:0 0 16px 0; text-align:center;"><a href="https://www.integraleacademy.com/dossiersfc" style="display:inline-block;padding:12px 20px;background:#0f1f33;color:#fff;text-decoration:none;border-radius:8px;font-weight:bold;transition:all .2s ease;box-shadow:0 2px 0 rgba(0,0,0,.15);">Télécharger le dossier de présentation</a></p>
      <p style="margin:18px 0 8px 0;"><strong>🎓 Durée et organisation</strong></p>
      <p>La formation complète se déroule sur <strong>245 heures</strong>, réparties ainsi :</p>
      <ul style="margin:0 0 16px 18px; padding:0;"><li>175 heures de e-learning à distance</li><li>70 heures de présentiel (2 semaines)</li></ul>
      <p>Le e-learning est accessible 24h/24, depuis un ordinateur, une tablette ou un smartphone. Chaque module comprend des vidéos, des supports interactifs, des quiz et des exercices pratiques.</p>
      <p>💡 Pas d’inquiétude : vous êtes accompagné tout au long du parcours, et une assistance pédagogique reste disponible en cas de besoin.</p>
      <p style="margin:18px 0 8px 0;"><strong>🏫 Prochaines formations</strong></p>
      {session_html}
      <p style="margin:0 0 6px 0;">Examen : <strong>27 avril 2026</strong></p>
      <p style="margin:0 0 16px 0;">Dans notre centre de formation : <strong>{centre_display}</strong></p>
      <p style="margin:18px 0 8px 0;"><strong>💶 Tarif et financement</strong></p>
      <p>Le tarif de la formation est de <strong>4 300 € TTC</strong>. Elle est finançable via votre Compte Personnel de Formation (CPF).</p>
      <p>👉 Pour cela, vous devrez créer ou activer votre <strong>Identité Numérique La Poste</strong>, nécessaire à la validation du dossier CPF.</p>
      <p style="margin:0 0 16px 0; text-align:center;"><a href="{devis_url}" style="display:inline-block;padding:12px 20px;background:#0f1f33;color:#fff;text-decoration:none;border-radius:8px;font-weight:bold;transition:all .2s ease;box-shadow:0 2px 0 rgba(0,0,0,.15);">Télécharger votre devis détaillé</a></p>
      <p style="margin:18px 0 8px 0;"><strong>📞 Prochaine étape</strong></p>
      <p>Si vous souhaitez réserver votre place ou poser vos questions, vous pouvez planifier un rendez-vous téléphonique directement :</p>
      <p style="margin:0 0 16px 0; text-align:center;"><a href="https://calendly.com/integraleacademy/dirigeant" style="display:inline-block;padding:12px 20px;background:#F4C45A;color:#000;text-decoration:none;border-radius:8px;font-weight:bold;transition:all .2s ease;box-shadow:0 2px 0 rgba(0,0,0,.15);">Planifier un rendez-vous</a></p>
      <p>Je reste à votre disposition pour tous renseignements complémentaires,<br />Je vous souhaite une excellente journée,</p>
      <p><strong>Clément VAILLANT</strong><br />Directeur Intégrale Group<br />ecole@integraleacademy.com – integraleacademy.com<br />📞 04 22 47 07 68<br />📍 Paris - Aurillac - Côte d'Azur</p>
    </div>
    <div style="padding:20px; font-size:12px; color:#555; text-align:center; border-top:1px solid #eee; line-height:1.5;">© Intégrale Academy — Merci de votre confiance 💛<br />SIREN 840 899 884 - NDA 93830600283 - Certification Nationale QUALIOPI : n°03169 en date du 21/10/2024<br />UAI Côte d'Azur 0831774C - UAI Paris 0756548K<br />integraleacademy.com</div>
  </div>
</div>
</body>
</html>"""

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

        if user_name.lower() == "mohamed":
            demandes_by_id = {d.get("id"): d for d in data.get("demandes", []) if d.get("id")}

            def _visible_for_mohamed(demande):
                source_id = demande.get("source_devis_id")
                if not source_id:
                    return True

                source = demandes_by_id.get(source_id)
                if not source:
                    return True

                if source.get("motif") != "Demande de devis détaillé":
                    return True

                return (source.get("statut_devis") or "A envoyer") == "Envoyé"

            demandes = [d for d in demandes if _visible_for_mohamed(d)]

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
def choisir_centre_formation():
    return render_template("choisir_centre_formation.html")


@app.route("/admin/formation-sessions", methods=["GET", "POST"])
@login_required
def admin_formation_sessions():
    data = load_data()
    sessions = get_formation_sessions(data)

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        centre = (request.form.get("centre") or "").strip()
        formation = (request.form.get("formation") or "").strip()
        idx_raw = request.form.get("idx", "")

        sessions.setdefault(centre, {})
        sessions[centre].setdefault(formation, [])

        if action == "add":
            label = (request.form.get("label") or "").strip()
            badge = (request.form.get("badge") or "").strip()
            date_examen = (request.form.get("date_examen") or "").strip()
            if label:
                sessions[centre][formation].append({
                    "label": label,
                    "badge": badge,
                    "date_examen": date_examen,
                })
        elif action == "update":
            try:
                idx = int(idx_raw)
            except:
                idx = -1
            if 0 <= idx < len(sessions[centre][formation]):
                sessions[centre][formation][idx]["label"] = (request.form.get("label") or "").strip()
                sessions[centre][formation][idx]["badge"] = (request.form.get("badge") or "").strip()
                sessions[centre][formation][idx]["date_examen"] = (request.form.get("date_examen") or "").strip()
        elif action == "delete":
            try:
                idx = int(idx_raw)
            except:
                idx = -1
            if 0 <= idx < len(sessions[centre][formation]):
                sessions[centre][formation].pop(idx)

        data["formation_sessions"] = sessions
        save_data(data)
        return redirect(url_for("admin_formation_sessions"))

    return render_template("admin_formation_sessions.html", sessions=sessions, formations=PLAN_FORMATIONS, centres=FORMATION_CENTRES)


@app.route("/demande-informations-formations", methods=["GET", "POST"])
def demande_informations_formations():
    data_store = load_data()
    sessions = get_formation_sessions(data_store)

    if request.method == "POST":
        form_data = request.form.to_dict()
        form_data["gclid"] = (form_data.get("gclid") or "").strip()

        prospect_chaud = (
            form_data.get("cpf_consulte") == "OUI"
            and form_data.get("france_travail") == "NON"
            and form_data.get("financement_perso") == "OUI"
            and form_data.get("identite_numerique") == "OUI"
        )

        demande_id = str(uuid.uuid4())
        demande_entry = {
            "id": demande_id,
            "nom": form_data.get("nom", "").strip(),
            "prenom": form_data.get("prenom", "").strip(),
            "telephone": form_data.get("telephone", "").strip(),
            "mail": form_data.get("mail", "").strip(),
            "motif": "Demande d’informations formations Intégrale Academy",
            "details": json.dumps(form_data, ensure_ascii=False),
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
            "notation_interne": "CHAUD" if prospect_chaud else "",
            "source": "demande_infos_formations"
        }
        data_store.setdefault("demandes", []).append(demande_entry)

        if form_data.get("souhaite_devis") != "OUI":
            rappel = {
                "id": str(uuid.uuid4()),
                "source_devis_id": demande_id,
                "nom": demande_entry["nom"],
                "prenom": demande_entry["prenom"],
                "telephone": demande_entry["telephone"],
                "mail": demande_entry["mail"],
                "motif": "personne à rappeler",
                "details": (
                    "Demande d’informations formations soumise.\n"
                    f"Formation : {form_data.get('formation', 'Non précisée')}\n"
                    f"Lieu : {form_data.get('centre', 'Non précisé')}\n"
                    f"Date : {form_data.get('dates', 'Non précisée')}"
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
            data_store["demandes"].append(rappel)

            try:
                envoyer_mail_attribution_mohamed(rappel)
            except:
                pass

        formation_label = PLAN_FORMATIONS.get(form_data.get("formation"), form_data.get("formation", "Formation"))
        prenom = form_data.get("prenom", "")

        devis_id = str(uuid.uuid4())
        token_plan = uuid.uuid4().hex
        devis_payload = {
            "nom": form_data.get("nom", "").strip(),
            "prenom": form_data.get("prenom", "").strip(),
            "telephone": form_data.get("telephone", "").strip(),
            "mail": form_data.get("mail", "").strip(),
            "formation": form_data.get("formation", ""),
            "dates": form_data.get("dates", ""),
            "centre": form_data.get("centre", ""),
            "cpf_montant": form_data.get("cpf_montant", "0"),
            "france_travail": form_data.get("france_travail", "NON"),
            "identite_numerique": form_data.get("identite_numerique", "NON"),
        }
        data_store.setdefault("demandes", []).append({
            "id": devis_id,
            "token_plan": token_plan,
            "nom": devis_payload["nom"],
            "prenom": devis_payload["prenom"],
            "telephone": devis_payload["telephone"],
            "mail": devis_payload["mail"],
            "motif": "Demande de devis détaillé",
            "details": json.dumps(devis_payload, ensure_ascii=False),
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
            "notation_interne": "CHAUD" if prospect_chaud else "",
            "echeancier_manuel": [],
            "pdf_path": ""
        })
        devis_url = url_for("plan_public", token=token_plan, _external=True)
        extra_devis = f"""
        <p style="margin-top:16px;">Vous pouvez télécharger votre devis détaillé en cliquant ici :</p>
        <p style="text-align:center;"><a href="{devis_url}" style="display:inline-block;padding:12px 18px;background:#0d6efd;color:#fff;border-radius:10px;text-decoration:none;font-weight:700;">Je télécharge mon devis détaillé</a></p>
        """

        extra_identite = ""
        if form_data.get("identite_numerique") == "NON":
            extra_identite = """
            <p>Pour utiliser votre Compte Personnel de Formation, vous devez créer votre « Identité Numérique la Poste » (FranceConnect+). Vous pouvez créer votre Identité Numérique la Poste directement dans un bureau de Poste ou sur le site internet officiel <a href="https://lidentitenumerique.laposte.fr/">https://lidentitenumerique.laposte.fr/</a>.</p>
            """

        save_data(data_store)

        if form_data.get("formation") == "DESP_VAE":
            plain = (
                "Bonjour,\n\n"
                "Merci pour votre intérêt concernant la VAE Dirigeant d’Entreprise de Sécurité Privée (RNCP40385).\n"
                "Vous pouvez démarrer votre parcours ici : https://gestionstagiaires-r5no.onrender.com/vae-desp\n\n"
                "Bien cordialement,\n"
                "Intégrale Academy"
            )
            html = build_vae_desp_email_html()
            email_subject = "📝 VAE – Dirigeant d’Entreprise de Sécurité Privée (RNCP40385)"
        elif form_data.get("formation") == "A3P":
            session_date = _format_selected_session_date(form_data.get("dates", ""))
            centre_label, centre_address = _centre_label_and_address(form_data.get("centre", ""))
            plain = (
                f"Bonjour {prenom},\n\n"
                "Je fais suite à notre conversation téléphonique concernant notre formation Agent de Protection Physique des Personnes (A3P – Bodyguard), titre reconnu par l’État (RNCP38002 – niveau 4).\n\n"
                "Durée : 328 heures de formation.\n"
                + (f"Session : {session_date}\n" if session_date else "")
                + f"Lieu : {centre_label} — {centre_address}\n\n"
                "Tarif : 4200 € TTC (financement possible via CPF).\n"
                "Hébergement possible : 300 € TTC pour toute la formation.\n\n"
                "Dossier de présentation : https://www.integraleacademy.com/dossiersfc\n"
                f"Devis détaillé : {devis_url}\n"
                "Planifier un rendez-vous : https://calendly.com/integraleacademy/apr\n\n"
                "Je reste à votre disposition pour toute information complémentaire.\n\n"
                "Clément VAILLANT\nDirecteur – Intégrale Academy"
            )
            html = build_a3p_email_html(prenom, form_data.get("dates", ""), form_data.get("centre", ""))
            email_subject = "🛡️ Formation Agent de Protection Physique des Personnes (A3P)"
        elif form_data.get("formation") == "APS":
            session_date = _format_selected_session_date(form_data.get("dates", ""))
            centre_label, centre_address = _centre_label_and_address(form_data.get("centre", ""))
            plain = (
                f"Bonjour {prenom},\n\n"
                "Voici les informations détaillées concernant notre formation Agent de Sécurité Privée (APS).\n"
                + (f"Session : {session_date}\n" if session_date else "")
                + f"Lieu : {centre_label} — {centre_address}\n"
                "Tarif : 1 650 € TTC.\n"
                "Durée : 175 heures sur 5 semaines.\n\n"
                "Dossier de présentation : https://www.integraleacademy.com/dossiersfc\n"
                f"Devis détaillé : {devis_url}\n"
                "Identité numérique La Poste : https://lidentitenumerique.laposte.fr\n"
                "Contact : 04 22 47 07 68\n\n"
                "Clément VAILLANT\nDirecteur – Intégrale Academy"
            )
            html = build_aps_email_html(prenom, form_data.get("dates", ""), form_data.get("centre", ""))
            email_subject = "🛡️ Formation Agent de Sécurité Privée (APS)"
        elif form_data.get("formation") == "VTC":
            centre_label, centre_address = _centre_label_and_address(form_data.get("centre", ""))
            plain = (
                f"Bonjour {prenom},\n\n"
                "Voici les informations détaillées concernant notre formation Chauffeur VTC.\n"
                f"Centre : {centre_label} — {centre_address}\n"
                "Tarif : 1 650 € TTC (tout inclus).\n"
                "Théorie : 100 % en ligne. Pratique : 1/2 journée en centre.\n\n"
                "Dossier : https://www.integraleacademy.com/dossiersfc\n"
                f"Devis détaillé : {devis_url}\n"
                "Dates examens : https://www.cmar-paca.fr/galerie/1/f3ec5a86ea34eb95294dd770b94b8c23.pdf\n"
                "Contact : 04 22 47 07 68\n\n"
                "Clément VAILLANT\nDirecteur – Intégrale Academy"
            )
            html = build_vtc_email_html(prenom, form_data.get("centre", ""))
            email_subject = "🚗 Formation Chauffeur VTC"
        elif form_data.get("formation") == "DESP_INIT":
            session_date = _format_selected_session_date(form_data.get("dates", ""))
            centre_label, _ = _centre_label_and_address(form_data.get("centre", ""))
            plain = (
                f"Bonjour {prenom},\n\n"
                "Je fais suite à votre demande de renseignements concernant notre formation Dirigeant d’Entreprise de Sécurité Privée (DESP), titre reconnu par l’État (RNCP40385 – niveau 5, équivalent Bac+2).\n"
                "Cette formation permet d’obtenir les compétences indispensables pour créer, diriger et gérer une entreprise de sécurité privée et vous permet de demander votre agrément dirigeant auprès du CNAPS conformément à la réglementation.\n\n"
                "Dossier de présentation : https://www.integraleacademy.com/dossiersfc\n\n"
                "Durée et organisation : 245 heures (175 heures de e-learning à distance + 70 heures de présentiel sur 2 semaines).\n"
                "Le e-learning est accessible 24h/24 sur ordinateur, tablette ou smartphone.\n\n"
                "Prochaines formations :\n"
                + (f"- {session_date}\n" if session_date else "- XXXX\n")
                + (f"Présentiel : {session_date}\n" if session_date else "Présentiel : XXXXX\n")
                + "Examen : 27 avril 2026\n"
                + f"Centre : {centre_label}\n\n"
                "Tarif : 4 300 € TTC (finançable via CPF).\n"
                "Identité Numérique La Poste (obligatoire CPF) : https://lidentitenumerique.laposte.fr/\n"
                f"Devis détaillé : {devis_url}\n\n"
                "Planifier un rendez-vous : https://calendly.com/integraleacademy/dirigeant\n\n"
                "Je reste à votre disposition pour tous renseignements complémentaires.\n"
                "Je vous souhaite une excellente journée.\n\n"
                "Clément VAILLANT\nDirecteur Intégrale Group"
            )
            html = build_desp_init_email_html(prenom, form_data.get("dates", ""), form_data.get("centre", ""), devis_url)
            email_subject = "Votre demande de renseignements – Formation DESP initial"
        else:
            plain = (
                f"Bonjour {prenom},\n\n"
                f"Je fais suite à votre demande de renseignements concernant notre formation {formation_label}. Nous vous remercions de nous avoir contacté !\n\n"
                "Un conseiller formation reviendra vers vous prochainement pour vous accompagner dans votre projet de formation.\n\n"
                "Vous pouvez également nous contacter au 04 22 47 07 68 pour échanger avec notre équipe.\n\n"
                + f"Vous pouvez télécharger votre devis détaillé ici : {devis_url}\n\n"
                + ("Pour utiliser votre Compte Personnel de Formation, vous devez créer votre Identité Numérique La Poste : https://lidentitenumerique.laposte.fr/\n\n" if form_data.get("identite_numerique") == "NON" else "")
                + "Je vous souhaite une bonne journée,\n\n"
                "Clément VAILLANT\nDirecteur Intégrale Academy"
            )

            html = _wrap_html(
                "<h1>✨ Merci pour votre demande</h1>",
                f"""
                <p>Bonjour <strong>{prenom}</strong>,</p>
                <p>Je fais suite à votre demande de renseignements concernant notre formation <strong>{formation_label}</strong>. Nous vous remercions de nous avoir contacté !</p>
                <p>Un conseiller formation reviendra vers vous prochainement pour vous accompagner dans votre projet de formation.</p>
                <p>Vous pouvez également nous contacter au <strong>04 22 47 07 68</strong> pour échanger avec notre équipe.</p>
                {extra_devis}
                {extra_identite}
                <p>Je vous souhaite une bonne journée,</p>
                <p><strong>Clément VAILLANT</strong><br>Directeur Intégrale Academy</p>
                """
            )
            email_subject = "Votre demande de renseignements – Intégrale Academy"

        try:
            send_email_html(form_data.get("mail"), email_subject, plain, html)
        except:
            pass

        if prospect_chaud:
            try:
                send_email_html(
                    "clement@integraleacademy.com",
                    "🔥 Prospect CHAUD — Demande d’informations formations",
                    f"Prospect chaud: {demande_entry['prenom']} {demande_entry['nom']} ({demande_entry['mail']})",
                    _wrap_html("<h1>🔥 Prospect CHAUD</h1>", f"<p>{demande_entry['prenom']} {demande_entry['nom']} — {formation_label}</p><p>Email : {demande_entry['mail']}</p>")
                )
            except:
                pass

        if form_data.get("formation") == "DESP_VAE":
            return redirect("https://gestionstagiaires-r5no.onrender.com/vae-desp")

        return redirect(url_for("confirmation_demande_infos", hot="1" if prospect_chaud else "0", formation=form_data.get("formation", "")))

    gclid = (request.args.get("gclid") or "").strip()
    return render_template("demande_informations_formations.html", sessions=sessions, formations=PLAN_FORMATIONS, gclid=gclid)


@app.route("/confirmation-demande-informations")
def confirmation_demande_infos():
    hot = request.args.get("hot") == "1"
    formation = request.args.get("formation") or ""
    calendly_map = {
        "DESP_INIT": "https://calendly.com/integraleacademy/dirigeant",
        "DESP_VAE": "https://calendly.com/integraleacademy/dirigeant",
        "A3P": "https://calendly.com/integraleacademy/apr",
        "APS": "https://calendly.com/integraleacademy/aps",
        "VTC": "https://calendly.com/integraleacademy/chauffeurvtc"
    }
    return render_template("confirmation_demande_informations.html", hot=hot, calendly_url=calendly_map.get(formation))


@app.route("/api/formation-sessions")
def api_formation_sessions():
    data_store = load_data()
    return get_formation_sessions(data_store)


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


@app.route("/admin-devis/formulaires")
@login_required
def admin_devis_formulaires():
    data = load_data()
    formulaires = []

    for d in data.get("demandes", []):
        is_target = (
            d.get("motif") == "Demande de devis détaillé"
            or d.get("source") == "demande_infos_formations"
        )
        if not is_target:
            continue

        infos = {}
        try:
            parsed = json.loads(d.get("details", "{}"))
            if isinstance(parsed, dict):
                infos = parsed
        except:
            infos = {}

        source_label = "Devis détaillé"
        if d.get("source") == "demande_infos_formations":
            source_label = "Infos formations"

        formulaire_date = d.get("date", "")
        try:
            sort_date = datetime.datetime.strptime(formulaire_date, "%d/%m/%Y %H:%M")
        except ValueError:
            sort_date = datetime.datetime.min

        formulaires.append({
            "id": d.get("id"),
            "date": formulaire_date,
            "nom": d.get("nom", ""),
            "prenom": d.get("prenom", ""),
            "mail": d.get("mail", ""),
            "telephone": d.get("telephone", ""),
            "source_label": source_label,
            "infos": infos,
            "sort_date": sort_date,
        })

    formulaires.sort(key=lambda formulaire: formulaire["sort_date"], reverse=True)

    return render_template("admin_devis_formulaires.html", formulaires=formulaires)


@app.route("/admin-devis/formulaires/<formulaire_id>/imprimer")
@login_required
def imprimer_formulaire_admin_devis(formulaire_id):
    data = load_data()
    demande = next((d for d in data.get("demandes", []) if d.get("id") == formulaire_id), None)
    if not demande:
        abort(404)

    is_target = (
        demande.get("motif") == "Demande de devis détaillé"
        or demande.get("source") == "demande_infos_formations"
    )
    if not is_target:
        abort(404)

    infos = {}
    try:
        parsed = json.loads(demande.get("details", "{}"))
        if isinstance(parsed, dict):
            infos = parsed
    except:
        infos = {}

    def normalize_key(raw_key):
        text = str(raw_key or "").strip().lower()
        text = unicodedata.normalize("NFD", text)
        text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
        text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
        return text

    label_overrides = {
        "gclid": "Gclid",
        "nom": "Nom",
        "prenom": "Prénom",
        "mail": "Mail",
        "mail_confirm": "Mail confirm",
        "telephone": "Téléphone",
        "formation": "Formation souhaitée",
        "centre": "Lieu de formation souhaité",
        "centre_formation": "Lieu de formation souhaité",
        "dates": "Dates de formation souhaitées",
        "cpf_consulte": "CPF consulté",
        "cpf_montant": "Montant CPF",
        "france_travail": "Inscrit France Travail",
        "ft_refus_ok": "Refus France Travail",
        "financement_perso": "Financement personnel",
        "identite_numerique": "Identité numérique",
        "cnaps_ok": "CNAPS validé",
        "garde_vue": "Garde à vue",
        "titre_sejour": "Titre de séjour",
    }

    categories = {
        "infos_generales": {"title": "Informations générales", "rows": []},
        "formation_souhaitee": {"title": "Formation souhaitée", "rows": []},
        "lieu_formation": {"title": "Lieu de formation souhaité", "rows": []},
        "dates_formation": {"title": "Dates de formation souhaitées", "rows": []},
        "financement": {"title": "Financement de votre formation", "rows": []},
        "situation": {"title": "Votre situation", "rows": []},
        "autres": {"title": "Autres informations", "rows": []},
    }

    def category_for_key(normalized_key):
        if normalized_key in {"nom", "prenom", "mail", "mail_confirm", "telephone", "gclid"}:
            return "infos_generales"
        if normalized_key in {"formation"}:
            return "formation_souhaitee"
        if normalized_key in {"centre", "centre_formation", "centre_formation_souhaite", "lieu_formation"}:
            return "lieu_formation"
        if normalized_key in {"dates", "date", "dates_formation", "dates_formation_souhaitees"}:
            return "dates_formation"
        if normalized_key in {"cpf_consulte", "cpf_montant", "france_travail", "ft_refus_ok", "financement_perso"}:
            return "financement"
        if normalized_key in {"identite_numerique", "cnaps_ok", "garde_vue", "titre_sejour"}:
            return "situation"
        return "autres"

    infos_rows = []
    centre_value = ""
    for key, value in infos.items():
        if isinstance(value, list):
            display_value = ", ".join(str(v) for v in value)
        elif isinstance(value, dict):
            display_value = json.dumps(value, ensure_ascii=False)
        else:
            display_value = str(value)

        normalized_key = normalize_key(key)
        if normalized_key in {"centre", "centre_formation", "centre formation"}:
            centre_value = display_value

        row = {
            "label": label_overrides.get(normalized_key, key.replace("_", " ").capitalize()),
            "value": display_value
        }
        infos_rows.append(row)
        categories[category_for_key(normalized_key)]["rows"].append(row)

    categorized_infos = [block for block in categories.values() if block["rows"]]

    source_label = "Devis détaillé"
    if demande.get("source") == "demande_infos_formations":
        source_label = "Infos formations"

    formation_value = str(
        infos.get("formation")
        or infos.get("Formation")
        or infos.get("formation_souhaitee")
        or infos.get("formation souhaitée")
        or ""
    ).strip()
    formation_normalized = formation_value.lower()
    formation_badge_text = ""
    formation_badge_theme = ""

    if formation_normalized in {"a3p", "agent de protection physique des personnes (a3p)", "agent de protection physique des personnes"}:
        formation_badge_text = "A3P"
        formation_badge_theme = "a3p"
    elif formation_normalized in {
        "desp_init",
        "dirigeant",
        "desp",
        "dirigeant d'entreprise de sécurité",
        "dirigeant d’entreprise de sécurité",
        "dirigeant d'entreprise de sécurité privée (desp)",
        "dirigeant d’entreprise de sécurité privée (desp)"
    }:
        formation_badge_text = "DIRIGEANT"
        formation_badge_theme = "dirigeant"
    elif formation_normalized in {
        "desp_vae",
        "vae",
        "dirigeant d'entreprise de sécurité (desp) – vae",
        "dirigeant d’entreprise de sécurité (desp) – vae",
        "dirigeant d’entreprise de sécurité privée (desp) en vae",
        "dirigeant d'entreprise de sécurité privée (desp) en vae"
    }:
        formation_badge_text = "VAE"
        formation_badge_theme = "vae"
    elif formation_normalized in {"vtc", "chauffeur vtc"}:
        formation_badge_text = "VTC"
        formation_badge_theme = "vtc"
    elif formation_normalized in {"aps", "agent de prévention et de sécurité (aps)", "agent de prévention et de sécurité"}:
        formation_badge_text = "APS"
        formation_badge_theme = "aps"

    badge_text = "AURILLAC"
    campus_theme = "aurillac"
    centre_normalized = (centre_value or "").strip().lower()

    if centre_normalized in {"cote d'azur", "côte d'azur", "paca", "nice"}:
        badge_text = "CÔTE D'AZUR"
        campus_theme = "cote-azur"
    elif centre_normalized in {"auvergne", "clermont", "clermont-ferrand"}:
        badge_text = "AUVERGNE"
        campus_theme = "auvergne"
    elif centre_normalized in {"ile-de-france", "île-de-france", "idf", "paris"}:
        badge_text = "ÎLE-DE-FRANCE"
        campus_theme = "idf"

    return render_template(
        "admin_devis_formulaire_imprimable.html",
        demande=demande,
        source_label=source_label,
        infos_rows=infos_rows,
        categorized_infos=categorized_infos,
        badge_text=badge_text,
        campus_theme=campus_theme,
        formation_badge_text=formation_badge_text,
        formation_badge_theme=formation_badge_theme
    )

@app.route("/admin-devis/simulateur")
@login_required
def simulateur_plan_financement():
    data_store = load_data()
    centre_code = request.args.get("centre", "cote_azur")
    simulation = compute_plan_financement_simulation(
        formation=request.args.get("formation", "APS"),
        dates_txt=request.args.get("dates", ""),
        cpf_value=request.args.get("cpf", 0),
        france_travail=request.args.get("france_travail", "NON"),
        date_examen_str=request.args.get("date_examen", ""),
        centre_code=centre_code,
    )

    return render_template(
        "simulateur_plan_financement.html",
        formations=PLAN_FORMATIONS,
        centres=FORMATION_CENTRES,
        dates_options=get_simulator_dates_options(data_store),
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
        date_examen_str=payload.get("date_examen", ""),
        centre_code=payload.get("centre", "cote_azur"),
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
        date_examen_str=payload.get("date_examen", ""),
        centre_code=payload.get("centre", "cote_azur"),
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
        elif action == "restore_one":
            archive_id = request.form.get("id")
            to_restore = next((a for a in data["archives"] if a.get("id") == archive_id), None)
            if to_restore:
                data.setdefault("demandes", []).append(to_restore)
                data["archives"] = [a for a in data["archives"] if a.get("id") != archive_id]
                save_data(data)
        elif action == "restore_all":
            if data.get("archives"):
                data.setdefault("demandes", []).extend(data["archives"])
                data["archives"] = []
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
    all_hebergements = data.get("hebergements", [])
    hebergements = list(all_hebergements)

    sessions_disponibles = sorted({
        (h.get("session") or "").strip()
        for h in all_hebergements
        if (h.get("session") or "").strip()
    })

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

    session_filter = " ".join((request.args.get("session_filter") or "").split())
    if session_filter:
        session_filter_lower = session_filter.lower()
        hebergements = [
            h for h in hebergements
            if " ".join((h.get("session") or "").split()).lower() == session_filter_lower
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

    return render_template(
        "admin_hebergement.html",
        hebergements=hebergements,
        sessions_disponibles=sessions_disponibles,
        selected_session=session_filter,
        search_query=request.args.get("q", ""),
        tri=tri,
    )


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

        # ✅ Ne pas créer de rappel Mohamed au dépôt du dossier devis.
        # Le rappel doit être créé uniquement quand le statut passe à "Envoyé"
        # via le bouton "Changer le statut" dans l'admin devis.
        save_data(data_store)

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
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
