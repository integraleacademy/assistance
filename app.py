from flask import Flask, render_template, request, send_from_directory, url_for, redirect, abort, jsonify
from flask import render_template_string
import json, os, datetime, uuid, pytz, smtplib, re, copy, unicodedata, tempfile, traceback, html, base64
import html as html_module
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
import requests
from openai import OpenAI

SALESFORCE_URL = "https://webto.salesforce.com/servlet/servlet.WebToLead?encoding=UTF-8&orgId=00DJ9000000PT9F"
SALESFORCE_OID = "00DJ9000000PT9F"
SALESFORCE_LEAD_SOURCE_VALUE = "Google"
SALESFORCE_INFOS_COMPLEMENTAIRES_FIELD = "00NSa00000GcKVx"
SALESFORCE_ORIGINE_FIELD = "00NSa00000KPDmX"
SALESFORCE_CHOIX_DIRIGEANT_DESP_FIELD = "00NSa00000KDPJd"
ABANDONED_INFOS_COMPLEMENTAIRES_MESSAGE = "FORMULAIRE ABANDONNÉ - Prospect n’a pas terminé le formulaire complet."

def valeur_refus_ft(value):
    if value == "OUI":
        return "Si FT refuse le financement = financement personnel OK"
    if value == "NON":
        return "Si FT refuse le financement = pas de possibilité de financer personnellement"
    return ""

def _has_required_abandoned_form_contact_fields(fields):
    required_fields = ("nom", "prenom", "mail", "telephone")
    return all((fields.get(field) or "").strip() for field in required_fields)


def _is_abandoned_training_form_ready_for_salesforce(fields):
    return _has_required_abandoned_form_contact_fields(fields)


ABANDONED_FORM_LABEL = "Formulaire abandonné"
ABANDONED_DEMANDE_SOURCE = "formulaire_abandonne_demande_infos"


def _abandoned_training_form_salesforce_payload(fields):
    salesforce_fields = copy.deepcopy(fields)
    salesforce_fields["statut_formulaire"] = ABANDONED_FORM_LABEL
    salesforce_fields["source_formulaire"] = "demande-informations-formations"
    return salesforce_fields


def _est_payload_formulaire_abandonne(form):
    return (
        form.get("statut_formulaire") == ABANDONED_FORM_LABEL
        or form.get("infos_complementaires") == ABANDONED_FORM_LABEL
    )


def _infos_complementaires_salesforce(form, formulaire_abandonne):
    infos_existantes = str(
        form.get(SALESFORCE_INFOS_COMPLEMENTAIRES_FIELD)
        or form.get("infos_complementaires")
        or ""
    ).strip()

    if formulaire_abandonne:
        if infos_existantes and infos_existantes != ABANDONED_FORM_LABEL:
            return f"{ABANDONED_INFOS_COMPLEMENTAIRES_MESSAGE}\n\n{infos_existantes}"
        return ABANDONED_INFOS_COMPLEMENTAIRES_MESSAGE

    return infos_existantes


def _choix_dirigeant_desp_salesforce(form):
    formation_text = " ".join([
        str(form.get("formation", "")),
        str(form.get("type_formation", "")),
        str(form.get("choix_dirigeant", "")),
        str(form.get("desp", "")),
    ]).lower()

    if "vae" in formation_text:
        return "DESP VAE"
    if (
        "initial" in formation_text
        or "dirigeant" in formation_text
        or "desp" in formation_text
    ):
        return "DESP INITIAL"
    return ""


def _payload_salesforce_simulation_vae(nom, prenom, mail, telephone, reponses, score, resultat):
    reponses_salesforce = {
        question: str(reponses.get(question) or "").strip().upper()
        for question in ("q1", "q2", "q3", "q4", "q5")
    }
    resume_reponses = " | ".join(
        f"{question.upper()} : {reponse or 'NON RENSEIGNÉ'}"
        for question, reponse in reponses_salesforce.items()
    )

    return {
        "nom": nom,
        "prenom": prenom,
        "mail": mail,
        "telephone": telephone,
        "formation": "DESP_VAE",
        "type_formation": "VAE DESP",
        "choix_dirigeant": "DESP VAE",
        "source_formulaire": "simulateur-eligibilite-vae-desp",
        "cnaps_ok": reponses_salesforce["q1"],
        "score_eligibilite_vae": f"{score}%",
        "resultat_eligibilite_vae": resultat,
        "infos_complementaires": (
            "SIMULATEUR ÉLIGIBILITÉ VAE DESP COMPLÉTÉ\n"
            f"Score : {score}%\n"
            f"Résultat : {resultat}\n"
            f"Réponses : {resume_reponses}"
        ),
        **reponses_salesforce,
    }


def creer_piste_salesforce(form):
    print("FORMULAIRE RECU:", dict(form))
    formulaire_abandonne = _est_payload_formulaire_abandonne(form)
    description = "\n".join([
        f"{key} : {value}"
        for key, value in form.items()
    ])

    centre = form.get("centre", "")
    if centre == "cote_azur":
        lieu = "Côte d'Azur"
    elif centre == "paris":
        lieu = "Paris"
    elif centre == "aurillac" or centre == "auvergne":
        lieu = "Aurillac"
    else:
        lieu = ""

    formation_map = {
        "APS": "APS",
        "A3P": "A3P",
        "DESP_INIT": "DIRIGEANT",
        "DESP_VAE": "DIRIGEANT",
        "VTC": "CHAUFFEUR VTC",
        "BTS": "BTS",
        "SSIAP": "SSIAP",
    }
    formation_sf = formation_map.get(form.get("formation", ""), "")

    oui_non_map = {"OUI": "Oui", "NON": "Non"}
    cpf_sf = oui_non_map.get(form.get("cpf_consulte", ""), "")
    france_travail_sf = oui_non_map.get(form.get("france_travail", ""), "")

    choix_dirigeant_desp = _choix_dirigeant_desp_salesforce(form)

    data = {
        "oid": SALESFORCE_OID,
        "retURL": "https://assistance-alw9.onrender.com/confirmation-demande-informations",
        "first_name": form.get("prenom", ""),
        "last_name": form.get("nom", "Sans nom"),
        "email": form.get("mail", ""),
        "phone": form.get("telephone", ""),
        "mobile": form.get("telephone", ""),
        "company": "Particulier",
        # Origine personnalisée Salesforce
        "00NSa00000KPDmX": "Google",
        "industry": "Education",
        SALESFORCE_ORIGINE_FIELD: SALESFORCE_LEAD_SOURCE_VALUE,
        "00NSa00000G2PxB": formation_sf,
        "00NSa00000KDPOT": lieu,
        "00NSa00000GcJMz": cpf_sf,
        "00NSa00000GcJd7": form.get("cpf_montant", ""),
        "00NSa00000GcJlB": form.get("cnaps_ok", ""),
        "00NSa00000GcJtF": form.get("garde_vue", ""),
        "00NSa00000GcJzh": form.get("identite_numerique", ""),
        "00NSa00000GcK2v": form.get("identite_numerique", ""),
        "00NSa00000GcK9N": valeur_refus_ft(form.get("ft_refus_ok", "")),
        "00NSa00000GcKxN": form.get("dates", ""),
        "00NSa00000GcK4X": france_travail_sf,
        "00NSa00000GcQl3": form.get("financement_perso", ""),
        "00NSa00000GcKVx": _infos_complementaires_salesforce(
            form, formulaire_abandonne
        ),
        "description": description
    }
    if choix_dirigeant_desp:
        data[SALESFORCE_CHOIX_DIRIGEANT_DESP_FIELD] = choix_dirigeant_desp

    try:
        print("ENVOI SALESFORCE WEB-TO-LEAD:", SALESFORCE_URL)
        print("WEB TO LEAD ENDPOINT OK:", "/servlet/servlet.WebToLead" in SALESFORCE_URL)
        print("WEB TO LEAD DATA SENT:", data)
        print("LEAD SOURCE SENT:", data.get("lead_source"))
        print("INFOS COMPLEMENTAIRES SENT:", data.get("00NSa00000GcKVx"))
        print("CHOIX DIRIGEANT DESP SENT:", data.get("00NSa00000KDPJd"))
        print("WEB TO LEAD FIELDS SENT:", list(data.keys()))
        response = requests.post(SALESFORCE_URL, data=data, timeout=10)
        print("SALESFORCE STATUS:", response.status_code)
        print("SALESFORCE RESPONSE:", response.text)
    except Exception as e:
        print("Erreur envoi Salesforce:", e)

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

    # 1) Essai direct sur des dates numériques (ex: 22/06/2026 ... 10/08/2026)
    matches_numeric = re.findall(r"(\d{1,2})[/-](\d{1,2})[/-](20\d{2})", dates_txt)
    if len(matches_numeric) >= 2:
        try:
            d1 = _date(int(matches_numeric[0][2]), int(matches_numeric[0][1]), int(matches_numeric[0][0]))
            d2 = _date(int(matches_numeric[1][2]), int(matches_numeric[1][1]), int(matches_numeric[1][0]))
            return (d1, d2)
        except Exception:
            pass

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
        cleaned = re.sub(r"^[^0-9]*", "", p.strip(), flags=re.IGNORECASE)
        tokens = cleaned.replace(",", " ").split()
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

def get_formation_tarif(formation_code: str, formation_details=None) -> int:
    details = formation_details if isinstance(formation_details, dict) else {}
    if formation_code == "SSIAP" and details.get("ssiap_secourisme_valide") == "NON":
        return 1200
    return PLAN_TARIFS.get(formation_code, 0)


def build_devis_context(
    formation_code: str,
    formation_label: str,
    dates_txt: str,
    sequence: int = 1,
    formation_details=None,
):
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

    elif formation_code == "SSIAP":
        total = get_formation_tarif(formation_code, formation_details)
        intitule = "Agent de sécurité incendie SSIAP 1"
        if total == 1200:
            intitule += " (SST inclus)"
        lignes = [
            {"intitule": intitule, "prix_unitaire": _eur(total), "quantite": 1, "total": _eur(total)},
            {"intitule": "Frais de dossier", "prix_unitaire": "OFFERTS", "quantite": 1, "total": _eur(0)},
        ]

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
    "DESP_VAE": "DESP – Dirigeant d’entreprise de sécurité (VAE)",
    "SSIAP": "SSIAP 1 – Agent de sécurité incendie"
}

PLAN_TARIFS = {
    "A3P": 4200,
    "APS": 1650,
    "VTC": 1600,
    "DESP_INIT": 4300,
    "DESP_VAE": 3800,
    "SSIAP": 980
}

FORMATION_CENTRES = {
    "cote_azur": "Intégrale Academy Côte d’Azur",
    "auvergne": "Intégrale Academy Terres d’Auvergne",
    "paris": "Intégrale Academy Paris",
}

PLAN_DATES = {
    "A3P": [
        "30 juin au 2 septembre 2026 – examen le 3 septembre 2026",
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
            {"label": "Du 30 juin au 2 septembre 2026 - examen le 3 septembre 2026", "badge": ""},
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
        "DESP_VAE": [],
        "SSIAP": [
            {
                "label": "Du 12 au 27 octobre 2026 - examen le 28 octobre 2026",
                "badge": "",
                "date_examen": "2026-10-28",
            }
        ]
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

    match_numeric = re.search(r"examen le (\d{1,2})[/-](\d{1,2})[/-](20\d{2})", dates_txt, re.IGNORECASE)
    if match_numeric:
        return f"{match_numeric.group(3)}-{match_numeric.group(2).zfill(2)}-{match_numeric.group(1).zfill(2)}"

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


def supprimer_fichiers_demande(demande):
    """Supprime définitivement les fichiers liés à une demande.

    À utiliser uniquement quand la demande est supprimée définitivement
    (ex: vidage des archives), pas lors d'un simple archivage.
    """
    if not demande:
        return

    supprimer_fichier(demande.get("justificatif"))

    for pj in demande.get("pieces_jointes", []) or []:
        supprimer_fichier(pj)

    for reponse in demande.get("reponses", []) or []:
        for pj in reponse.get("pj", []) or []:
            supprimer_fichier(pj)

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

def _email_recipients(to_emails):
    if isinstance(to_emails, (list, tuple, set)):
        values = to_emails
    else:
        values = str(to_emails or "").split(",")
    return [str(value).strip() for value in values if str(value).strip()]


def _send_email_brevo(to_emails, subject, plain_text, html_body, attachments_paths=None):
    api_key = os.getenv("BREVO_API_KEY")
    sender_email = os.getenv("BREVO_SENDER_EMAIL") or os.getenv("SMTP_USER")
    recipients = _email_recipients(to_emails)
    if not api_key or not sender_email or not recipients:
        return False

    payload = {
        "sender": {
            "name": os.getenv("BREVO_SENDER_NAME", "Intégrale Academy"),
            "email": sender_email,
        },
        "to": [{"email": recipient} for recipient in recipients],
        "subject": subject,
        "textContent": plain_text,
        "htmlContent": html_body,
    }

    attachments = []
    for path in attachments_paths or []:
        if not path or not os.path.exists(path):
            continue
        with open(path, "rb") as attachment_file:
            attachments.append({
                "name": os.path.basename(path),
                "content": base64.b64encode(attachment_file.read()).decode("ascii"),
            })
    if attachments:
        payload["attachment"] = attachments

    try:
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            json=payload,
            headers={
                "accept": "application/json",
                "api-key": api_key,
                "content-type": "application/json",
            },
            timeout=10,
        )
        if 200 <= response.status_code < 300:
            print("✅ Email envoyé via le secours Brevo")
            return True
        print("❌ Erreur envoi email Brevo :", response.status_code, response.text)
    except Exception as e:
        print("❌ Erreur envoi email Brevo :", e)
    return False


def send_email_html(to_emails, subject, plain_text, html_body, attachments_paths=None):
    recipients = _email_recipients(to_emails)
    if not recipients:
        print("❌ Erreur envoi email : aucun destinataire")
        return False

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = os.getenv("SMTP_USER")
    msg["To"] = ", ".join(recipients)

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
        print("❌ Erreur envoi email SMTP :", e)
        return _send_email_brevo(
            recipients,
            subject,
            plain_text,
            html_body,
            attachments_paths=attachments_paths,
        )


# -------------------------------------------------------------------
# SMS helper
# -------------------------------------------------------------------
def _formation_sms_context(formation_code: str) -> dict:
    config = _abandoned_training_config(formation_code)
    return {
        "formation_name": config.get("formation_name") or PLAN_FORMATIONS.get(formation_code, formation_code or "Formation"),
        "calendly": config.get("calendly") or "https://calendly.com/integraleacademy/apr",
    }


def build_training_information_sms_text(formation_code: str) -> str:
    context = _formation_sms_context(formation_code)
    return (
        "Bonjour, \n"
        f"Je fais suite à votre demande d’informations concernant notre formation {context['formation_name']}. "
        "Je viens de vous adresser par mail toutes les informations utiles (pensez à vérifier vos courriers indésirables). \n"
        "Je vous invite à réserver un RDV téléphonique avec un membre de notre équipe qui pourra vous renseigner "
        f"et vous présenter en détails notre formation : {context['calendly']}\n"
        "Vous pouvez également nous contacter par téléphone du lundi au vendredi de 09h00 à 17h00 au 04 22 47 07 68. \n"
        "Je vous souhaite une bonne journée, \n"
        "Clément VAILLANT - Directeur Intégrale Academy"
    )


def _normaliser_telephone_sms(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""

    digits = re.sub(r"\D+", "", raw)
    if len(digits) < 8:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 10:
        digits = f"33{digits[1:]}"

    return digits


def send_sms(to_phone: str, body: str) -> bool:
    recipient = _normaliser_telephone_sms(to_phone)
    if not recipient:
        print("❌ Erreur envoi SMS Brevo : numéro invalide")
        return False

    api_key = os.getenv("BREVO_API_KEY")
    sender = os.getenv("BREVO_SMS_SENDER", "FORMATION")

    if not api_key or not sender:
        print("❌ Erreur envoi SMS Brevo : configuration incomplète")
        return False

    payload = {
        "sender": sender,
        "recipient": recipient,
        "content": body,
        "type": "transactional",
        "tag": "demande-informations-formations",
    }
    headers = {
        "accept": "application/json",
        "api-key": api_key,
        "content-type": "application/json",
    }

    try:
        response = requests.post(
            "https://api.brevo.com/v3/transactionalSMS/send",
            json=payload,
            headers=headers,
            timeout=10,
        )
        print("BREVO SMS STATUS:", response.status_code)
        print("BREVO SMS RESPONSE:", response.text)
        return 200 <= response.status_code < 300
    except Exception as e:
        print("❌ Erreur envoi SMS Brevo :", e)
        return False


def envoyer_sms_demande_infos_formation(record: dict, fields: dict) -> bool:
    body = build_training_information_sms_text(fields.get("formation", ""))
    ok = send_sms(fields.get("telephone", ""), body)
    now_str = datetime.datetime.now(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M")
    if ok:
        record["sms_sent_at"] = now_str
        record["sms_body"] = body
    else:
        record["sms_error"] = now_str
    return ok


def envoyer_sms_formulaire_formation_abandonne(draft_entry: dict, fields: dict) -> bool:
    body = build_training_information_sms_text(fields.get("formation", ""))
    ok = send_sms(fields.get("telephone", ""), body)
    now_str = datetime.datetime.now(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M")
    if ok:
        draft_entry["abandoned_sms_sent_at"] = now_str
        draft_entry["abandoned_sms_body"] = body
    else:
        draft_entry["abandoned_sms_error"] = now_str
    return ok





def _render_email_template(template_name: str, **kwargs) -> str:
    template_path = os.path.join(app.root_path, "templates", "emails", template_name)
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()

    for key, value in kwargs.items():
        html = html.replace("{" + key + "}", str(value))

    return html

def build_vae_desp_email_html(prenom, devis_url):
    return _render_email_template("vae_desp.html", prenom=prenom, devis_url=devis_url)


def build_vae_desp_email_plain(prenom, devis_url):
    return (
        f"Bonjour {prenom},\n\n"
        "Je fais suite à votre demande de renseignements concernant notre VAE Dirigeant d’Entreprise de Sécurité Privée (RNCP40385).\n"
        "Dossier de présentation : https://www.integraleacademy.com/dossiersfc\n"
        f"Télécharger votre devis détaillé : {devis_url}\n"
        "Démarrer votre VAE : https://gestionstagiaires-r5no.onrender.com/vae-desp\n"
        "Planifier un rendez-vous : https://calendly.com/integraleacademy/dirigeant\n\n"
        "Je reste à votre disposition pour tout renseignement complémentaire.\n\n"
        "Clément VAILLANT\nDirecteur – Intégrale Academy"
    )


def _is_vae_desp_formation(formation_code: str) -> bool:
    return str(formation_code or "").strip() == "DESP_VAE"



def _eur_display(amount: int) -> str:
    return f"{amount:,.0f} € TTC".replace(",", " ")


ABANDONED_TRAINING_EMAIL_CONFIG = {
    "A3P": {
        "short": "Formation A3P Bodyguard",
        "project_title": "de formation A3P Bodyguard",
        "formation_name": "Agent de Protection Physique des Personnes (A3P – Bodyguard)",
        "about_title": "La formation A3P en quelques mots",
        "about_text": "La formation <strong>A3P – Agent de Protection Physique des Personnes</strong> permet de se former aux métiers de la protection rapprochée, dans le respect de la réglementation française.",
        "about_extra": "Elle prépare à l’obtention de la carte professionnelle délivrée par le <strong>CNAPS</strong> pour exercer légalement dans le domaine de la protection physique des personnes.",
        "learning": [
            "Protection rapprochée et accompagnement de personnes exposées",
            "Préparation, anticipation et sécurisation des déplacements",
            "Gestion des situations sensibles et des comportements à risque",
            "Cadre légal de l’activité de sécurité privée",
            "Posture professionnelle, discrétion et communication",
            "Préparation à l’examen et à la demande de carte professionnelle CNAPS",
        ],
        "highlights": [
            ("✅", "Titre reconnu par l’État", "RNCP38002 — niveau 4"),
            ("👮", "Carte professionnelle CNAPS", "Protection Physique des Personnes"),
            ("🏨", "Hébergement possible", "Solution sur place selon disponibilités"),
        ],
        "calendly": "https://calendly.com/integraleacademy/apr",
    },
    "APS": {
        "short": "Formation APS",
        "project_title": "de formation APS",
        "formation_name": "Agent de Prévention et de Sécurité (APS)",
        "about_title": "La formation APS en quelques mots",
        "about_text": "La formation <strong>APS – Agent de Prévention et de Sécurité</strong> prépare aux missions de surveillance, de prévention et de protection des biens et des personnes.",
        "about_extra": "Elle permet de se préparer à l’exercice réglementé d’agent de sécurité privée et aux démarches liées à la carte professionnelle CNAPS.",
        "learning": [
            "Surveillance générale et prévention des actes de malveillance",
            "Accueil, contrôle d’accès et filtrage",
            "Gestion des conflits et des situations sensibles",
            "Bases juridiques de la sécurité privée",
            "Prévention incendie et secours à personne",
            "Préparation à l’examen APS et à la carte professionnelle CNAPS",
        ],
        "highlights": [
            ("✅", "Formation réglementée", "Accès au métier d’agent de sécurité"),
            ("👮", "Démarches CNAPS", "Accompagnement possible"),
            ("📍", "Sessions régulières", "Selon centres et disponibilités"),
        ],
        "calendly": "https://calendly.com/integraleacademy/aps",
    },
    "SSIAP": {
        "short": "Formation SSIAP 1",
        "project_title": "de formation SSIAP 1",
        "formation_name": "SSIAP 1 – Agent de Service de Sécurité Incendie et d’Assistance à Personnes",
        "about_title": "La formation SSIAP 1 en quelques mots",
        "about_text": "La formation <strong>SSIAP 1</strong> prépare au métier d’agent de sécurité incendie dans les établissements recevant du public et les immeubles de grande hauteur.",
        "about_extra": "Elle prépare au diplôme SSIAP 1 et aux missions de prévention incendie, d’alerte, d’évacuation et d’assistance à personnes.",
        "learning": [
            "Prévention des risques d’incendie",
            "Sensibilisation des occupants aux consignes de sécurité",
            "Intervention face à un début d’incendie",
            "Alerte et accueil des secours",
            "Évacuation du public et assistance aux personnes",
            "Préparation aux épreuves du diplôme SSIAP 1",
        ],
        "highlights": [
            ("🔥", "Diplôme SSIAP 1", "Sécurité incendie en ERP et IGH"),
            ("✅", "Programme réglementé", "67 heures hors examen"),
            ("📍", "Formation en présentiel", "Puget-sur-Argens"),
        ],
        "calendly": "https://calendly.com/integraleacademy/ssiap1",
    },
    "VTC": {
        "short": "Formation Chauffeur VTC",
        "project_title": "de formation Chauffeur VTC",
        "formation_name": "Chauffeur de transport avec chauffeur (VTC)",
        "about_title": "La formation VTC en quelques mots",
        "about_text": "La formation <strong>VTC</strong> permet de préparer votre projet de chauffeur professionnel avec une organisation flexible combinant théorie à distance et pratique.",
        "about_extra": "Elle vous accompagne sur les compétences attendues à l’examen et sur les démarches nécessaires pour lancer votre activité.",
        "learning": [
            "Réglementation du transport public particulier de personnes",
            "Sécurité routière, relation client et qualité de service",
            "Gestion, développement commercial et préparation d’activité",
            "Préparation à l’examen théorique VTC",
            "Mise en pratique de la conduite professionnelle",
            "Organisation des démarches administratives VTC",
        ],
        "highlights": [
            ("💻", "Théorie en ligne", "Accessible à distance"),
            ("🚗", "Pratique encadrée", "Préparation terrain"),
            ("📄", "Dossier complet", "Programme et démarches"),
        ],
        "calendly": "https://calendly.com/integraleacademy/chauffeurvtc",
    },
    "DESP_INIT": {
        "short": "Formation DESP initial",
        "project_title": "de formation DESP initial",
        "formation_name": "Dirigeant d’Entreprise de Sécurité Privée (DESP – initial)",
        "about_title": "La formation DESP initial en quelques mots",
        "about_text": "La formation <strong>DESP</strong> prépare les futurs dirigeants d’entreprise de sécurité privée à créer, piloter et gérer leur structure conformément à la réglementation.",
        "about_extra": "Elle prépare aux compétences nécessaires pour solliciter l’agrément dirigeant auprès du CNAPS.",
        "learning": [
            "Cadre juridique de la sécurité privée et obligations du dirigeant",
            "Création, gestion et pilotage d’une entreprise de sécurité",
            "Gestion administrative, commerciale et financière",
            "Management des équipes et organisation opérationnelle",
            "Déontologie, contrôle interne et conformité CNAPS",
            "Préparation à l’examen et à l’agrément dirigeant",
        ],
        "highlights": [
            ("✅", "Titre reconnu par l’État", "RNCP40385 — niveau 5"),
            ("🏢", "Projet dirigeant", "Créer ou gérer une société"),
            ("💻", "E-learning + présentiel", "Organisation mixte"),
        ],
        "calendly": "https://calendly.com/integraleacademy/dirigeant",
    },
    "DESP_VAE": {
        "short": "VAE DESP",
        "project_title": "de VAE DESP",
        "formation_name": "VAE Dirigeant d’Entreprise de Sécurité Privée (DESP)",
        "about_title": "La VAE DESP en quelques mots",
        "about_text": "La <strong>VAE DESP</strong> permet de valoriser votre expérience professionnelle pour viser la certification Dirigeant d’Entreprise de Sécurité Privée.",
        "about_extra": "Notre équipe peut vous accompagner dans la constitution du dossier, la formalisation de vos compétences et la préparation du passage devant le jury.",
        "learning": [
            "Analyse de votre expérience et de sa cohérence avec le référentiel",
            "Constitution et structuration du dossier VAE",
            "Mise en valeur des compétences de dirigeant sécurité privée",
            "Préparation à l’entretien avec le jury",
            "Compréhension des attendus réglementaires et CNAPS",
            "Accompagnement méthodologique jusqu’au dépôt du dossier",
        ],
        "highlights": [
            ("✅", "Certification visée", "RNCP40385 — niveau 5"),
            ("📝", "Accompagnement dossier", "Méthode et structuration"),
            ("📞", "Suivi personnalisé", "Échange avec notre équipe"),
        ],
        "calendly": "https://calendly.com/integraleacademy/dirigeant",
    },
}


def _abandoned_training_config(formation_code: str):
    default_label = PLAN_FORMATIONS.get(formation_code, formation_code or "Formation")
    return ABANDONED_TRAINING_EMAIL_CONFIG.get(formation_code) or {
        "short": default_label,
        "project_title": f"de formation {default_label}",
        "formation_name": default_label,
        "about_title": "La formation en quelques mots",
        "about_text": f"Cette formation <strong>{html.escape(default_label)}</strong> répond à un projet professionnel concret et peut faire l’objet d’un accompagnement par notre équipe.",
        "about_extra": "Nous pouvons vous expliquer les objectifs, les prérequis, les dates, le financement et les étapes d’inscription lors d’un échange téléphonique.",
        "learning": [
            "Objectifs et organisation de la formation",
            "Prérequis et conditions d’accès",
            "Dates, lieux et modalités pratiques",
            "Solutions de financement possibles",
            "Étapes d’inscription et documents utiles",
            "Réponses personnalisées à vos questions",
        ],
        "highlights": [
            ("📄", "Dossier complet", "Programme et informations pratiques"),
            ("💶", "Financement", "CPF, France Travail ou personnel"),
            ("📞", "Accompagnement", "Échange avec notre équipe"),
        ],
        "calendly": "https://calendly.com/integraleacademy/apr",
    }


def _highlights_html(items) -> str:
    blocks = []
    for idx, (emoji, title, subtitle) in enumerate(items):
        margin = " margin-top:10px;" if idx else ""
        blocks.append(
            f"""<table width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"border-collapse:collapse;{margin}\">
        <tr>
          <td style=\"background:#f8fafc; border:1px solid #e5e7eb; border-radius:14px; padding:14px; text-align:center;\">
            <div style=\"font-size:24px;\">{emoji}</div>
            <div style=\"font-weight:bold; margin-top:6px;\">{html.escape(title)}</div>
            <div style=\"font-size:13px; color:#64748b;\">{html.escape(subtitle)}</div>
          </td>
        </tr>
      </table>"""
        )
    return "\n".join(blocks)


def build_abandoned_training_email_html(prenom: str, formation_code: str, devis_url: str = "") -> str:
    config = _abandoned_training_config(formation_code)
    price = _eur_display(PLAN_TARIFS.get(formation_code, 0)) if PLAN_TARIFS.get(formation_code) else "Tarif transmis sur demande"
    learning_items_html = "".join(f"<li>{html.escape(item)}</li>" for item in config["learning"])
    return _render_email_template(
        "abandoned_training.html",
        prenom=html.escape(prenom or ""),
        formation_short=html.escape(config["short"]),
        project_title=html.escape(config["project_title"]),
        formation_name=html.escape(config["formation_name"]),
        calendly_url=config["calendly"],
        highlights_html=_highlights_html(config["highlights"]),
        about_title=html.escape(config["about_title"]),
        about_text=config["about_text"],
        about_extra=config["about_extra"],
        learning_items_html=learning_items_html,
        price=price,
    )


def build_abandoned_training_email_plain(prenom: str, formation_code: str, devis_url: str = "") -> str:
    config = _abandoned_training_config(formation_code)
    price = _eur_display(PLAN_TARIFS.get(formation_code, 0)) if PLAN_TARIFS.get(formation_code) else "Tarif transmis sur demande"
    return (
        f"Bonjour {prenom},\n\n"
        f"Vous aviez commencé une demande d’informations concernant notre formation {config['formation_name']}, mais votre demande n’a pas été finalisée.\n\n"
        "Aucun souci : je vous transmets les informations principales et vous propose un échange téléphonique si vous souhaitez avancer plus facilement.\n\n"
        f"Tarif : {price}\n"
        "Dossier de présentation : https://www.integraleacademy.com/dossiersfc\n"
        f"Réserver un rendez-vous : {config['calendly']}\n"
        "Identité Numérique La Poste : https://lidentitenumerique.laposte.fr\n\n"
        "Vous pouvez répondre directement à ce mail.\n\n"
        "Clément VAILLANT\nDirecteur Intégrale Group\n"
        "04 22 47 07 68\n"
    )



def _abandoned_training_email_content(prenom: str, formation_code: str, devis_url: str = ""):
    if _is_vae_desp_formation(formation_code):
        return (
            "📝 VAE – Dirigeant d’Entreprise de Sécurité Privée (RNCP40385)",
            build_vae_desp_email_plain(prenom, devis_url),
            build_vae_desp_email_html(prenom, devis_url),
        )

    config = _abandoned_training_config(formation_code)
    return (
        f"Votre demande d'informations - {config['short']}",
        build_abandoned_training_email_plain(prenom, formation_code, devis_url),
        build_abandoned_training_email_html(prenom, formation_code, devis_url),
    )


def envoyer_mail_formulaire_formation_abandonne(draft_entry: dict, fields: dict) -> bool:
    formation_code = fields.get("formation", "")
    prenom = fields.get("prenom", "")
    subject, plain, html_body = _abandoned_training_email_content(prenom, formation_code)
    ok = send_email_html(fields.get("mail"), subject, plain, html_body)
    now_str = datetime.datetime.now(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M")
    if ok:
        draft_entry["abandoned_mail_sent_at"] = now_str
        draft_entry["abandoned_mail_subject"] = subject
    else:
        draft_entry["abandoned_mail_error"] = now_str
    return ok


def _fields_formulaire_abandonne_depuis_demande(demande: dict) -> dict:
    try:
        parsed = json.loads(demande.get("details", "{}"))
        fields = parsed if isinstance(parsed, dict) else {}
    except Exception:
        fields = {}

    return {
        **fields,
        "nom": fields.get("nom") or demande.get("nom", ""),
        "prenom": fields.get("prenom") or demande.get("prenom", ""),
        "mail": fields.get("mail") or demande.get("mail", ""),
        "telephone": fields.get("telephone") or demande.get("telephone", ""),
    }


def _envoyer_mail_formulaire_abandonne_depuis_demande(demande: dict, fields: dict) -> bool:
    token_plan = demande.get("token_plan")
    if not token_plan:
        token_plan = uuid.uuid4().hex
        demande["token_plan"] = token_plan

    devis_url = url_for("plan_public", token=token_plan, _external=True)
    formation_code = fields.get("formation", "")
    prenom = fields.get("prenom", "")
    subject, plain, html_body = _abandoned_training_email_content(prenom, formation_code, devis_url)
    ok = send_email_html(fields.get("mail"), subject, plain, html_body)
    now_str = datetime.datetime.now(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M")
    if ok:
        demande["abandoned_mail_sent_at"] = now_str
        demande["abandoned_mail_subject"] = subject
    else:
        demande["abandoned_mail_error"] = now_str
    return ok


def _declencher_relance_formulaire_abandonne(record: dict, fields: dict, now_str: str) -> dict:
    result = {"salesforce": False, "mail": False, "sms": False, "skipped": False}
    if not _has_required_abandoned_form_contact_fields(fields):
        result["skipped"] = True
        record["abandoned_automation_skipped_at"] = now_str
        record["abandoned_automation_skip_reason"] = "Coordonnées incomplètes"
        return result

    if not record.get("salesforce_abandoned_sent_at"):
        creer_piste_salesforce(_abandoned_training_form_salesforce_payload(fields))
        record["salesforce_abandoned_sent_at"] = now_str
        record["salesforce_abandoned_status"] = ABANDONED_FORM_LABEL
        result["salesforce"] = True

    if not record.get("abandoned_mail_sent_at"):
        result["mail"] = envoyer_mail_formulaire_formation_abandonne(record, fields)

    if not record.get("abandoned_sms_sent_at"):
        result["sms"] = envoyer_sms_formulaire_formation_abandonne(record, fields)

    record["auto_abandoned_sent_at"] = record.get("auto_abandoned_sent_at") or now_str
    record["abandoned_automation_status"] = "Relance automatique déclenchée"
    return result


def _parse_datetime_paris(value: str):
    try:
        return datetime.datetime.strptime(str(value or ""), "%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return None


def _delai_relance_formulaire_abandonne_minutes() -> int:
    try:
        return max(1, int(os.getenv("ABANDONED_FORM_AUTO_RELAUNCH_DELAY_MINUTES", "15")))
    except (TypeError, ValueError):
        return 15


def _formulaire_abandonne_doit_declencher_relance(record: dict, now_dt=None) -> bool:
    if not isinstance(record, dict):
        return False

    fields = record.get("fields") or {}
    if not _has_required_abandoned_form_contact_fields(fields):
        return False

    if (
        record.get("salesforce_abandoned_sent_at")
        and record.get("abandoned_mail_sent_at")
        and record.get("abandoned_sms_sent_at")
    ):
        return False

    if record.get("abandoned_at") or record.get("abandoned_status") == ABANDONED_FORM_LABEL:
        return True

    last_update = _parse_datetime_paris(record.get("updated_at") or record.get("created_at"))
    if not last_update:
        return False

    now_dt = now_dt or datetime.datetime.now(pytz.timezone("Europe/Paris")).replace(tzinfo=None)
    age = now_dt - last_update
    return age >= datetime.timedelta(minutes=_delai_relance_formulaire_abandonne_minutes())


def _declencher_relances_formulaires_abandonnes_eligibles(data: dict) -> bool:
    now_dt = datetime.datetime.now(pytz.timezone("Europe/Paris")).replace(tzinfo=None)
    now_str = now_dt.strftime("%d/%m/%Y %H:%M")
    changed = False

    for draft in data.get("formulaires_abandonnes", []):
        if not _formulaire_abandonne_doit_declencher_relance(draft, now_dt):
            continue

        draft["abandoned_at"] = draft.get("abandoned_at") or now_str
        draft["abandoned_status"] = ABANDONED_FORM_LABEL
        draft["auto_abandoned_trigger_reason"] = (
            "Formulaire abandonné détecté automatiquement après inactivité"
        )
        _declencher_relance_formulaire_abandonne(draft, draft.get("fields") or {}, now_str)
        changed = True

    return changed

def _format_selected_session_date(dates_txt: str) -> str:
    if not dates_txt:
        return ""
    return dates_txt.strip().replace(" - examen le ", " — examen le ")

def _extract_exam_label_from_dates_txt(dates_txt: str) -> str:
    if not dates_txt:
        return ""
    match = re.search(r"examen le\s+(.+)$", dates_txt.strip(), re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).strip(" .)")


def _format_upcoming_sessions_for_email(centre_code: str, formation_code: str) -> str:
    sessions = get_formation_sessions()
    rows = sessions.get(_normalize_centre_code(centre_code), {}).get(formation_code, [])
    labels = [
        (row.get("label") or "").strip()
        for row in rows
        if isinstance(row, dict) and (row.get("label") or "").strip()
    ]
    if not labels:
        return '<p style="margin:0 0 6px 0;">📅 <strong>Dates à venir prochainement</strong></p>'
    items = "".join(
        f'<li style="margin:0 0 6px 0;"><strong>{label.replace(" - examen le ", " — examen le ")}</strong></li>'
        for label in labels
    )
    return f'<ul style="margin:0 0 8px 18px; padding:0;">{items}</ul>'


def _normalize_centre_code(centre_code: str) -> str:
    normalized = str(centre_code or "").strip().lower()
    aliases = {
        "cote_azur": "cote_azur",
        "cote d'azur": "cote_azur",
        "côte d'azur": "cote_azur",
        "paca": "cote_azur",
        "nice": "cote_azur",
        "auvergne": "auvergne",
        "clermont": "auvergne",
        "clermont-ferrand": "auvergne",
        "paris": "paris",
        "idf": "paris",
        "ile-de-france": "paris",
        "île-de-france": "paris",
    }
    return aliases.get(normalized, "cote_azur")


def _centre_label_and_address(centre_code: str):
    centre_code = _normalize_centre_code(centre_code)
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
            "142 rue de Rivoli — 75001 PARIS",
        ),
    }
    return centres.get(
        centre_code,
        (
            "Intégrale Academy Côte d’Azur",
            "54 chemin du Carreou — 83480 PUGET SUR ARGENS (Var)",
        ),
    )


def _centre_legal_block(centre_code: str) -> str:
    centre_code = _normalize_centre_code(centre_code)
    if centre_code == "paris":
        return (
            "SASU Intégrale Sécurité Formations\n"
            "142 rue de Rivoli\n"
            "75001 PARIS\n"
            "Immatriculée au Registre des commerces et des sociétés RCS 840899884\n"
            "NDA n°93830600283\n"
            "Autorisation CNAPS FOR-083-2027-02-08-20200755135\n"
            "Certification Nationale Qualité QUALIOPI n°03169 délivrée par SGS en date du 21/10/2024 - "
            "La certification qualité a été délivrée au titre de la ou des catégories d’actions suivantes : "
            "actions de formation, actions de formation en apprentissage."
        )

    return (
        "SASU Intégrale Sécurité Formations\n"
        "Siège social : 54 chemin du Carreou\n"
        "83480 PUGET SUR ARGENS\n"
        "Immatriculée au Registre du commerce et des sociétés de Fréjus RCS 840899884\n"
        "NDA n°93830600283\n"
        "Autorisation CNAPS FOR-083-2027-02-08-20200755135\n"
        "Certification Nationale Qualité QUALIOPI n°03169 délivrée par SGS en date du 21/10/2024 - "
        "La certification qualité a été délivrée au titre de la ou des catégories d’actions suivantes : "
        "actions de formation, actions de formation en apprentissage."
    )


def build_a3p_email_html(prenom: str, dates_txt: str, centre_code: str, devis_url: str):
    centre_label, _ = _centre_label_and_address(centre_code)
    centre_display = centre_label.replace("Intégrale Academy ", "")
    session_html = _format_upcoming_sessions_for_email(centre_code, "A3P")
    return _render_email_template("a3p.html", prenom=prenom, centre_display=centre_display, session_html=session_html, devis_url=devis_url)





def build_aps_email_html(prenom: str, dates_txt: str, centre_code: str, devis_url: str):
    session_date = _format_selected_session_date(dates_txt)
    centre_label, centre_address = _centre_label_and_address(centre_code)
    session_html = (
        f"<p style=\"margin:0; line-height:1.65;\">📅 <strong>{session_date}</strong></p>"
        if session_date
        else "<p style=\"margin:0; line-height:1.65;\">📅 <strong>Date transmise lors de notre échange téléphonique.</strong></p>"
    )
    return _render_email_template(
        "aps.html",
        prenom=prenom,
        session_html=session_html,
        centre_label=centre_label,
        centre_address=centre_address,
        devis_url=devis_url,
    )





def build_ssiap1_email_html(
    prenom: str,
    dates_txt: str,
    centre_code: str,
    devis_url: str,
    ssiap_secourisme_valide: str,
):
    session_date = _format_selected_session_date(dates_txt)
    centre_label, centre_address = _centre_label_and_address(centre_code)
    tarif = get_formation_tarif(
        "SSIAP",
        {"ssiap_secourisme_valide": ssiap_secourisme_valide},
    )
    session_html = (
        f'<p style="margin:0; line-height:1.65;">📅 <strong>{html_module.escape(session_date)}</strong></p>'
        if session_date
        else '<p style="margin:0; line-height:1.65;">📅 <strong>Date transmise lors de notre échange téléphonique.</strong></p>'
    )
    secourisme_info = (
        "La formation SST est incluse dans ce tarif."
        if tarif == 1200
        else "Tarif applicable avec un certificat SST valide ou un PSC1 de moins de 2 ans."
    )
    return _render_email_template(
        "ssiap1.html",
        prenom=html_module.escape(prenom or ""),
        session_html=session_html,
        centre_label=html_module.escape(centre_label),
        centre_address=html_module.escape(centre_address),
        tarif_display=_eur_display(tarif),
        secourisme_info=secourisme_info,
        devis_url=html_module.escape(devis_url, quote=True),
    )



def build_vtc_email_html(prenom: str, centre_code: str, devis_url: str):
    return _render_email_template("vtc.html", prenom=prenom, devis_url=devis_url)





def build_desp_init_email_html(prenom: str, dates_txt: str, centre_code: str, devis_url: str):
    centre_label, _ = _centre_label_and_address(centre_code)
    centre_display = centre_label.replace("Intégrale Academy ", "")
    session_html = _format_upcoming_sessions_for_email(centre_code, "DESP_INIT")
    return _render_email_template("desp_init.html", prenom=prenom, centre_display=centre_display, session_html=session_html, devis_url=devis_url)




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


def can_manage_admin_devis_rappels():
    """Autorise Clément et Mohamed à gérer les rappels Demoiselles du téléphone."""
    user = current_user()
    if not user:
        return False
    if user.get("role") == "admin":
        return True
    return (user.get("name") or "").strip().lower() == "mohamed"


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



def envoyer_mail_formulaire_rappel_admin(callback_data):
    sujet = "Formulaire Demoiselles du Téléphone"
    plain = (
        "Formulaire Demoiselles du Téléphone :\n\n"
        f"Nom: {callback_data.get('nom', '')}\n"
        f"Prénom: {callback_data.get('prenom', '')}\n"
        f"Email: {callback_data.get('mail', '')}\n"
        f"Téléphone: {callback_data.get('telephone', '')}\n"
        f"Objet de l'appel: {callback_data.get('objet_appel', '')}\n"
        f"Créneau de rappel: {callback_data.get('creneau_rappel', '')}\n"
    )

    rows = f"""
      <tr><td style="padding:6px 8px;color:#555;width:170px;">Nom</td><td style="padding:6px 8px;"><strong>{callback_data.get('nom', '')}</strong></td></tr>
      <tr><td style="padding:6px 8px;color:#555;">Prénom</td><td style="padding:6px 8px;"><strong>{callback_data.get('prenom', '')}</strong></td></tr>
      <tr><td style="padding:6px 8px;color:#555;">Email</td><td style="padding:6px 8px;">{callback_data.get('mail', '')}</td></tr>
      <tr><td style="padding:6px 8px;color:#555;">Téléphone</td><td style="padding:6px 8px;">{callback_data.get('telephone', '')}</td></tr>
      <tr><td style="padding:6px 8px;color:#555;">Objet de l'appel</td><td style="padding:6px 8px;white-space:pre-wrap;">{callback_data.get('objet_appel', '')}</td></tr>
      <tr><td style="padding:6px 8px;color:#555;">Créneau de rappel</td><td style="padding:6px 8px;">{callback_data.get('creneau_rappel', '')}</td></tr>
    """
    html = _wrap_html(
        '<h1 style="margin:0 0 12px;font-size:20px;">Formulaire Demoiselles du Téléphone</h1>',
        f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:14px;">
          {rows}
        </table>
        """
    )
    send_email_html("cassandre@integraleacademy.com", sujet, plain, html)
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





def _payload_salesforce_poei_cannes(demande, details):
    infos_complementaires = (
        "CANDIDATURE POEI SÉCURITÉ CANNES\n"
        "Formation : POEI Agent de sécurité privée + Agent de sécurité incendie SSIAP 1\n"
        "Dates : 23 septembre au 22 décembre 2026\n"
        "Lieu de formation : Intégrale Academy, Puget-sur-Argens\n"
        "Poste visé : Agent de sécurité / Agent de sécurité incendie à Cannes\n"
        "Contrat prévu : CDD minimum 6 mois\n"
        f"Ville de résidence : {details.get('Ville de résidence', '')}\n"
        f"Permis B : {details.get('Permis B', '')}\n"
        f"Disponible formation : {details.get('Disponible formation', '')}\n"
        f"Mobilité Cannes : {details.get('Mobilité Cannes', '')}\n"
        f"Inscrit France Travail : {details.get('Inscrit France Travail', '')}\n"
        f"Identifiant France Travail : {details.get('Identifiant France Travail', '')}\n"
        f"Message / motivation : {details.get('Message / motivation', '')}"
    )
    return {
        "nom": demande.get("nom", ""),
        "prenom": demande.get("prenom", ""),
        "mail": demande.get("mail", ""),
        "telephone": demande.get("telephone", ""),
        "formation": "SSIAP",
        "type_formation": "POEI Agent de sécurité privée + SSIAP 1",
        "source_formulaire": "poei-agent-securite-cannes",
        "centre": "cote_azur",
        "dates": "23 septembre au 22 décembre 2026",
        "france_travail": (
            "OUI"
            if details.get("Inscrit France Travail") == "Oui"
            else details.get("Inscrit France Travail", "")
        ),
        "ville": details.get("Ville de résidence", ""),
        "permis_b": details.get("Permis B", ""),
        "mobilite_cannes": details.get("Mobilité Cannes", ""),
        "identifiant_france_travail": details.get("Identifiant France Travail", ""),
        "infos_complementaires": infos_complementaires,
    }

def _poei_cannes_admin_email(demande):
    details = demande.get("details_data", {})
    rows = "".join(
        f"<tr><td style='padding:8px 10px;color:#64748b;border-bottom:1px solid #eef2f7;'>{html_module.escape(label)}</td>"
        f"<td style='padding:8px 10px;border-bottom:1px solid #eef2f7;'><strong>{html_module.escape(str(value or '—'))}</strong></td></tr>"
        for label, value in details.items()
    )
    plain = (
        "Nouvelle candidature POEI Sécurité Cannes\n\n"
        f"Nom : {demande.get('nom', '')}\n"
        f"Prénom : {demande.get('prenom', '')}\n"
        f"Email : {demande.get('mail', '')}\n"
        f"Téléphone : {demande.get('telephone', '')}\n"
        f"Ville : {details.get('Ville de résidence', '')}\n"
        f"Message : {details.get('Message / motivation', '')}"
    )
    html_body = _wrap_html(
        "<h1 style='margin:0;color:#123c2f;'>Nouvelle candidature POEI Sécurité Cannes</h1>",
        f"""
        <p>Une nouvelle candidature a été transmise depuis la landing page POEI Agent de sécurité + SSIAP 1.</p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;border:1px solid #eef2f7;border-radius:12px;overflow:hidden;">{rows}</table>
        """
    )
    return send_email_html("aurelie@integraleacademy.com", "Nouvelle candidature POEI Sécurité Cannes", plain, html_body)


@app.route("/poei-agent-securite-cannes", methods=["GET", "POST"])
def poei_agent_securite_cannes():
    success = request.args.get("success") == "1"
    if request.method == "POST":
        required_fields = [
            "nom", "prenom", "mail", "telephone", "ville", "permis_b",
            "disponible_formation", "mobilite_cannes", "france_travail", "identifiant_france_travail", "message",
        ]
        required_checks = ["confirm_disponibilite", "confirm_cannes", "confirm_cnaps", "consentement"]
        missing = [field for field in required_fields if not (request.form.get(field) or "").strip()]
        missing += [field for field in required_checks if request.form.get(field) != "on"]
        mail = (request.form.get("mail") or "").strip()
        if mail and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", mail):
            missing.append("mail")
        if request.form.get("france_travail", "").strip() != "Oui":
            missing.append("france_travail")
        if missing:
            return render_template("poei_agent_securite_cannes.html", success=False, error="missing_fields"), 400

        paris_tz = pytz.timezone("Europe/Paris")
        details_data = {
            "Formation": "POEI Agent de sécurité privée + Agent de sécurité incendie SSIAP 1",
            "Dates": "23 septembre au 22 décembre 2026",
            "Lieu de formation": "Intégrale Academy, Puget-sur-Argens",
            "Poste visé": "Agent de sécurité / Agent de sécurité incendie à Cannes",
            "Contrat prévu": "CDD minimum 6 mois",
            "Ville de résidence": request.form.get("ville", "").strip(),
            "Permis B": request.form.get("permis_b", "").strip(),
            "Disponible formation": request.form.get("disponible_formation", "").strip(),
            "Mobilité Cannes": request.form.get("mobilite_cannes", "").strip(),
            "Inscrit France Travail": request.form.get("france_travail", "").strip(),
            "Identifiant France Travail": request.form.get("identifiant_france_travail", "").strip(),
            "Confirmation CNAPS": "Oui",
            "Consentement recontact": "Oui",
            "Message / motivation": request.form.get("message", "").strip(),
        }
        demande = {
            "id": str(uuid.uuid4()),
            "nom": request.form.get("nom", "").strip(),
            "prenom": request.form.get("prenom", "").strip(),
            "telephone": request.form.get("telephone", "").strip(),
            "mail": mail,
            "motif": "Nouvelle candidature POEI Sécurité Cannes",
            "details": json.dumps(details_data, ensure_ascii=False),
            "details_data": details_data,
            "date": datetime.datetime.now(paris_tz).strftime("%d/%m/%Y %H:%M"),
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
            "source": "poei_agent_securite_cannes",
        }
        data = load_data()
        data.setdefault("demandes", []).append(demande)
        save_data(data)
        creer_piste_salesforce(_payload_salesforce_poei_cannes(demande, details_data))
        try:
            if _poei_cannes_admin_email(demande):
                demande["mail_confirme"] = datetime.datetime.now(paris_tz).strftime("%d/%m/%Y %H:%M")
            else:
                demande["mail_erreur"] = "Variables SMTP/Brevo manquantes ou envoi impossible : SMTP_USER/SMTP_PASS ou BREVO_API_KEY/BREVO_SENDER_EMAIL."
        except Exception as e:
            demande["mail_erreur"] = f"Erreur envoi email : {e}"
        data = load_data()
        for entry in data.get("demandes", []):
            if entry.get("id") == demande["id"]:
                entry.update({"mail_confirme": demande["mail_confirme"], "mail_erreur": demande["mail_erreur"]})
                break
        save_data(data)
        return redirect(url_for("poei_agent_securite_cannes", success="1") + "#candidature")
    return render_template("poei_agent_securite_cannes.html", success=success)


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
                data["demandes"].remove(to_remove)
                save_data(data)
            return redirect_with_query()

        # 🧹 Archivage de toutes les demandes traitées
        elif action == "delete_all_traitees":
            traitees = [d for d in demandes if d.get("statut") == "Traité"]
            for d in traitees:
                data["archives"].append(d)
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
                current_row = sessions[centre][formation][idx]
                for field in ("label", "badge", "date_examen"):
                    if field in request.form:
                        current_row[field] = (request.form.get(field) or "").strip()
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




def _normaliser_email_formulaire(value):
    return str(value or "").strip().lower()


def _normaliser_telephone_formulaire(value):
    return re.sub(r"\D+", "", str(value or ""))


def _est_demande_issue_formulaire_abandonne(demande):
    return (
        demande.get("source") == ABANDONED_DEMANDE_SOURCE
        or bool(demande.get("source_formulaire_abandonne_id"))
    )


def _est_formulaire_admin_devis(demande):
    if _est_demande_issue_formulaire_abandonne(demande):
        return False
    return (
        demande.get("motif") == "Demande de devis détaillé"
        or demande.get("source") == "demande_infos_formations"
    )


def _identifiants_formulaires_soumis(demandes):
    identifiants = {"draft_ids": set(), "emails": set(), "telephones": set()}
    for demande in demandes:
        if not _est_formulaire_admin_devis(demande):
            continue

        details = {}
        try:
            parsed = json.loads(demande.get("details", "{}"))
            if isinstance(parsed, dict):
                details = parsed
        except Exception:
            details = {}

        draft_id = str(details.get("draft_form_id") or demande.get("draft_form_id") or "").strip()
        if draft_id:
            identifiants["draft_ids"].add(draft_id)

        email = _normaliser_email_formulaire(demande.get("mail") or details.get("mail"))
        if email:
            identifiants["emails"].add(email)

        telephone = _normaliser_telephone_formulaire(demande.get("telephone") or details.get("telephone"))
        if len(telephone) >= 6:
            identifiants["telephones"].add(telephone)

    return identifiants


def _brouillon_correspond_a_formulaire_soumis(draft, identifiants):
    fields = draft.get("fields") or {}

    form_id = str(draft.get("form_id") or fields.get("draft_form_id") or "").strip()
    if form_id and form_id in identifiants["draft_ids"]:
        return True

    email = _normaliser_email_formulaire(fields.get("mail"))
    if email and email in identifiants["emails"]:
        return True

    telephone = _normaliser_telephone_formulaire(fields.get("telephone"))
    if len(telephone) >= 6 and telephone in identifiants["telephones"]:
        return True

    return False


def _nettoyer_formulaires_abandonnes_soumis(data):
    abandons = data.get("formulaires_abandonnes", [])
    if not abandons:
        return False

    identifiants = _identifiants_formulaires_soumis(data.get("demandes", []))
    abandons_filtres = [
        draft for draft in abandons
        if not _brouillon_correspond_a_formulaire_soumis(draft, identifiants)
    ]

    if len(abandons_filtres) == len(abandons):
        return False

    data["formulaires_abandonnes"] = abandons_filtres
    return True


def _supprimer_brouillon_formulaire_soumis(data, form_data):
    abandons = data.get("formulaires_abandonnes", [])
    if not abandons:
        return False

    submitted_draft_id = str(form_data.get("draft_form_id") or "").strip()
    submitted_email = _normaliser_email_formulaire(form_data.get("mail"))
    submitted_phone = _normaliser_telephone_formulaire(form_data.get("telephone"))

    def doit_garder(draft):
        fields = draft.get("fields") or {}
        if submitted_draft_id and draft.get("form_id") == submitted_draft_id:
            return False
        draft_email = _normaliser_email_formulaire(fields.get("mail"))
        if submitted_email and draft_email == submitted_email:
            return False
        draft_phone = _normaliser_telephone_formulaire(fields.get("telephone"))
        if len(submitted_phone) >= 6 and draft_phone == submitted_phone:
            return False
        return True

    abandons_filtres = [draft for draft in abandons if doit_garder(draft)]
    if len(abandons_filtres) == len(abandons):
        return False

    data["formulaires_abandonnes"] = abandons_filtres
    return True

@app.route("/demande-informations-formations", methods=["GET", "POST"])
def demande_informations_formations():
    data_store = load_data()
    sessions = get_formation_sessions(data_store)

    if request.method == "POST":
        form_data = request.form.to_dict()
        creer_piste_salesforce(request.form)
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
        _supprimer_brouillon_formulaire_soumis(data_store, form_data)

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
            "date_examen": form_data.get("date_examen", ""),
            "ssiap_secourisme_valide": form_data.get("ssiap_secourisme_valide", ""),
            "cpf_montant": form_data.get("cpf_montant", "0"),
            "france_travail": form_data.get("france_travail", "NON"),
            "identite_numerique": form_data.get("identite_numerique", "NON"),
        }
        data_store.setdefault("demandes", []).append({
            "id": devis_id,
            "token_plan": token_plan,
            "source_demande_infos_id": demande_id,
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
                f"Bonjour {prenom},\n\n"
                "Je fais suite à votre demande de renseignements concernant notre VAE Dirigeant d’Entreprise de Sécurité Privée (RNCP40385).\n"
                "Dossier de présentation : https://www.integraleacademy.com/dossiersfc\n"
                f"Télécharger votre devis détaillé : {devis_url}\n"
                "Démarrer votre VAE : https://gestionstagiaires-r5no.onrender.com/vae-desp\n"
                "Planifier un rendez-vous : https://calendly.com/integraleacademy/dirigeant\n\n"
                "Je reste à votre disposition pour tout renseignement complémentaire.\n\n"
                "Clément VAILLANT\nDirecteur – Intégrale Academy"
            )
            html = build_vae_desp_email_html(prenom, devis_url)
            email_subject = "📝 VAE – Dirigeant d’Entreprise de Sécurité Privée (RNCP40385)"
        elif form_data.get("formation") == "A3P":
            session_date = _format_selected_session_date(form_data.get("dates", ""))
            centre_label, centre_address = _centre_label_and_address(form_data.get("centre", ""))
            plain = (
                f"Bonjour {prenom},\n\n"
                "Je fais suite à votre demande de renseignements concernant notre formation Agent de Protection Physique des Personnes (A3P – Bodyguard), titre reconnu par l’État (RNCP38002 – niveau 4).\n"
                "Cette formation permet d’acquérir toutes les compétences nécessaires pour intervenir en tant que garde du corps, dans le respect strict de la réglementation française. Elle prépare également à l’obtention de la carte professionnelle Agent de protection physique des personnes délivrée par le CNAPS (Ministère de l'intérieur).\n\n"
                "Durée et organisation : 328 heures de formation.\n"
                + (f"Session : {session_date}\n" if session_date else "")
                + f"Lieu : {centre_label} — {centre_address}\n\n"
                "Tarif : 4200 € TTC (financement possible via CPF).\n"
                "Identité Numérique La Poste requise pour le CPF.\n"
                "Hébergement possible : 300 € TTC pour toute la formation.\n\n"
                "Dossier de présentation : https://www.integraleacademy.com/dossiersfc\n"
                "Planifier un rendez-vous : https://calendly.com/integraleacademy/apr\n\n"
                "Je reste à votre disposition pour toute information complémentaire.\n\n"
                "Clément VAILLANT\nDirecteur – Intégrale Academy"
            )
            html = build_a3p_email_html(prenom, form_data.get("dates", ""), form_data.get("centre", ""), devis_url)
            email_subject = "👮‍♂️ Formation Agent de Protection Physique des Personnes (A3P)"
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
            html = build_aps_email_html(prenom, form_data.get("dates", ""), form_data.get("centre", ""), devis_url)
            email_subject = "👮‍♂️ Formation Agent de Sécurité Privée (APS)"
        elif form_data.get("formation") == "SSIAP":
            session_date = _format_selected_session_date(form_data.get("dates", ""))
            tarif_ssiap = get_formation_tarif("SSIAP", form_data)
            secourisme_info = (
                "Le tarif comprend la formation SST."
                if tarif_ssiap == 1200
                else "Tarif applicable avec un certificat SST ou PSC1 de moins de 2 ans."
            )
            plain = (
                f"Bonjour {prenom},\n\n"
                "Voici les informations concernant notre formation Agent de sécurité incendie SSIAP 1.\n"
                + (f"Session : {session_date}\n" if session_date else "")
                + "Examen : 28 octobre 2026\n"
                "Lieu : Intégrale Academy Côte d’Azur — 54 chemin du Carreou, 83480 Puget-sur-Argens.\n"
                f"Tarif : {tarif_ssiap} € TTC. {secourisme_info}\n\n"
                f"Devis détaillé : {devis_url}\n"
                "Prendre rendez-vous : https://calendly.com/integraleacademy/ssiap1\n"
                "Contact : 04 22 47 07 68\n\n"
                "Clément VAILLANT\nDirecteur – Intégrale Academy"
            )
            html = build_ssiap1_email_html(
                prenom,
                form_data.get("dates", ""),
                form_data.get("centre", ""),
                devis_url,
                form_data.get("ssiap_secourisme_valide", ""),
            )
            email_subject = "🔥 Formation Agent de sécurité incendie SSIAP 1"
        elif form_data.get("formation") == "VTC":
            plain = (
                f"Bonjour {prenom},\n\n"
                "Pour faire suite à votre demande de renseignements, nous vous prions de bien vouloir trouver ci-dessous l’ensemble des informations détaillées concernant notre formation Chauffeur VTC.\n\n"
                "Organisation de la formation :\n"
                "- Théorie 100 % en ligne, accessible 7j/7.\n"
                "- Pratique : 1/2 journée à Puget-sur-Argens (83).\n\n"
                "Déroulement : accès e-learning à l'inscription, examen théorique Chambre des Métiers, puis pratique et examen pratique (La Valette-du-Var ou Nice selon résidence).\n\n"
                "Prérequis : permis B depuis plus de 3 ans et casier judiciaire vierge.\n"
                "Tarif : 1 650 € TTC (tout inclus).\n"
                "Durée : théorie ~100h + pratique 1/2 journée.\n\n"
                "Dossier : https://www.integraleacademy.com/dossiersfc\n"
                "Dates examens : https://www.cmar-paca.fr/galerie/1/f3ec5a86ea34eb95294dd770b94b8c23.pdf\n"
                "Programme : https://www.integraleacademy.com/dossiersfc\n"
                "Agrément VTC : https://www.integraleacademy.com/_files/ugd/008e7b_0e29b04a71dd4dcc9c0f266d28f0514b.pdf\n"
                "Prendre rendez-vous : https://calendly.com/integraleacademy/chauffeurvtc\n\n"
                "Clément VAILLANT\nDirecteur – Intégrale Academy"
            )
            html = build_vtc_email_html(prenom, form_data.get("centre", ""), devis_url)
            email_subject = "🚗 Formation Chauffeur VTC"
        elif form_data.get("formation") == "DESP_INIT":
            session_date = _format_selected_session_date(form_data.get("dates", ""))
            exam_label = _extract_exam_label_from_dates_txt(form_data.get("dates", ""))
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
                + f"Examen : {exam_label or 'XXXXX'}\n"
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

        demande_entry["mail_contenu"] = plain
        demande_entry["mail_html"] = html
        try:
            email_sent = send_email_html(form_data.get("mail"), email_subject, plain, html)
        except Exception as e:
            print("❌ Erreur inattendue envoi email demande informations formations :", e)
            email_sent = False

        if email_sent:
            demande_entry["mail_confirme"] = datetime.datetime.now(
                pytz.timezone("Europe/Paris")
            ).strftime("%d/%m/%Y %H:%M")
            demande_entry["mail_erreur"] = ""
        else:
            demande_entry["mail_erreur"] = "❌ Erreur lors de l'envoi automatique du mail"

        try:
            envoyer_sms_demande_infos_formation(demande_entry, form_data)
        except Exception as e:
            print("❌ Erreur envoi SMS demande informations formations :", e)
            demande_entry["sms_error"] = datetime.datetime.now(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M")
        save_data(data_store)

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




@app.route("/api/demande-informations-formations/autosave", methods=["POST"])
def autosave_demande_informations_formations():
    payload = request.get_json(silent=True) or {}
    form_id = (payload.get("form_id") or "").strip()
    if not form_id:
        return ("", 204)

    data = load_data()
    entries = data.setdefault("formulaires_abandonnes", [])

    status = (payload.get("status") or "draft").strip().lower()
    if status == "submitted":
        fields = payload.get("fields")
        if not isinstance(fields, dict):
            fields = {}

        submitted_email = _normaliser_email_formulaire(fields.get("mail"))
        submitted_phone = _normaliser_telephone_formulaire(fields.get("telephone"))

        data["formulaires_abandonnes"] = [
            e for e in entries
            if not (
                e.get("form_id") == form_id
                or (submitted_email and _normaliser_email_formulaire((e.get("fields") or {}).get("mail")) == submitted_email)
                or (len(submitted_phone) >= 6 and _normaliser_telephone_formulaire((e.get("fields") or {}).get("telephone")) == submitted_phone)
            )
        ]
        save_data(data)
        return ("", 204)

    fields = payload.get("fields")
    if not isinstance(fields, dict):
        fields = {}

    cleaned_fields = {}
    for k, v in fields.items():
        if isinstance(v, str):
            v = v.strip()
        if v not in ("", None):
            cleaned_fields[k] = v

    existing = next((e for e in entries if e.get("form_id") == form_id), None)
    now_str = datetime.datetime.now(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M")

    if existing:
        existing["fields"] = cleaned_fields
        existing["updated_at"] = now_str
        draft_entry = existing
    else:
        draft_entry = {
            "form_id": form_id,
            "fields": cleaned_fields,
            "created_at": now_str,
            "updated_at": now_str,
        }
        entries.append(draft_entry)

    if status == "abandoned":
        draft_entry["abandoned_at"] = draft_entry.get("abandoned_at") or now_str
        draft_entry["abandoned_status"] = ABANDONED_FORM_LABEL
        _declencher_relance_formulaire_abandonne(draft_entry, cleaned_fields, now_str)

    save_data(data)
    return ("", 204)

@app.route("/confirmation-demande-informations")
def confirmation_demande_infos():
    hot = request.args.get("hot") == "1"
    formation = request.args.get("formation") or ""
    calendly_map = {
        "DESP_INIT": "https://calendly.com/integraleacademy/dirigeant",
        "DESP_VAE": "https://calendly.com/integraleacademy/dirigeant",
        "A3P": "https://calendly.com/integraleacademy/apr",
        "APS": "https://calendly.com/integraleacademy/aps",
        "SSIAP": "https://calendly.com/integraleacademy/ssiap1",
        "VTC": "https://calendly.com/integraleacademy/chauffeurvtc"
    }
    return render_template("confirmation_demande_informations.html", hot=hot, calendly_url=calendly_map.get(formation))


@app.route("/api/formation-sessions")
def api_formation_sessions():
    data_store = load_data()
    return get_formation_sessions(data_store)


@app.route("/api/chat", methods=["POST"])
def api_chat():
    try:
        print("=== /api/chat appelé ===", flush=True)

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            print("ERREUR: OPENAI_API_KEY absente", flush=True)
            return jsonify({"reply": "Désolé, l’IA est momentanément indisponible."}), 200

        data = request.get_json(silent=True) or {}
        message = (data.get("message") or "").strip()
        print("Message reçu:", message, flush=True)

        if not message:
            return jsonify({"reply": "Veuillez écrire une question."}), 200

        from openai import OpenAI
        client = OpenAI(api_key=api_key, timeout=20)

        print("Appel OpenAI en cours...", flush=True)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "Tu es l'assistant officiel d’Intégrale Academy. Tu réponds aux prospects sur les formations APS, A3P, A3P garde du corps, VTC, Dirigeant, VAE, BTS, financement, devis, inscription, CNAPS et modalités. Tu réponds en français, de façon claire, rassurante et commerciale. Tu incites naturellement à compléter le formulaire de demande d’informations présent sur la page. Si on te demande une date précise que tu ne connais pas, tu invites à compléter le formulaire pour recevoir les prochaines dates."
                },
                {
                    "role": "user",
                    "content": message
                }
            ],
            max_tokens=400,
            temperature=0.4
        )
        print("Appel OpenAI terminé", flush=True)

        reply = ""
        if getattr(response, "choices", None) and response.choices[0].message:
            reply = response.choices[0].message.content or ""
        if not reply:
            reply = "Désolé, je n’ai pas pu générer de réponse."
        print("Réponse IA OK", flush=True)

        return jsonify({"reply": reply}), 200

    except Exception as e:
        print("ERREUR /api/chat:", str(e), flush=True)
        print(traceback.format_exc(), flush=True)
        return jsonify({"reply": "Désolé, l’IA est momentanément indisponible."}), 200


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
    simulations_vae = []
    for d in data.get("demandes", []):
        if d.get("motif") == "Demande de devis détaillé":

            # 🔧 Parsing sécurisé du JSON "details"
            infos = {}
            try:
                infos = json.loads(d.get("details", "{}"))
            except Exception:
                infos = {}

            d["infos"] = infos
            devis.append(d)
        elif d.get("source") == "simulateur_vae_desp":
            infos = {}
            try:
                infos = json.loads(d.get("details", "{}"))
            except Exception:
                infos = {}
            d["infos"] = infos
            simulations_vae.append(d)

    simulations_vae.reverse()
    return render_template("admin_devis.html", devis=devis, simulations_vae=simulations_vae)



@app.route("/admin-devis/poei")
@login_required
def admin_devis_poei():
    data = load_data()
    candidatures = []
    for demande in data.get("demandes", []):
        if demande.get("source") != "poei_agent_securite_cannes":
            continue
        details = demande.get("details_data")
        if not isinstance(details, dict):
            try:
                details = json.loads(demande.get("details", "{}"))
            except Exception:
                details = {}
        item = dict(demande)
        item["details"] = details
        candidatures.append(item)

    candidatures.reverse()
    stats = {
        "total": len(candidatures),
        "non_traites": sum(1 for c in candidatures if (c.get("statut") or "Non traité") == "Non traité"),
        "mails_confirmes": sum(1 for c in candidatures if c.get("mail_confirme")),
    }
    return render_template("admin_devis_poei.html", candidatures=candidatures, stats=stats)





@app.route("/formulaire-a-rappeler", methods=["GET", "POST"])
def formulaire_a_rappeler():
    success = False
    if request.method == "POST":
        payload = {
            "nom": (request.form.get("nom") or "").strip(),
            "prenom": (request.form.get("prenom") or "").strip(),
            "mail": (request.form.get("mail") or "").strip(),
            "telephone": (request.form.get("telephone") or "").strip(),
            "objet_appel": (request.form.get("objet_appel") or "").strip(),
            "creneau_rappel": (request.form.get("creneau_rappel") or "").strip(),
        }

        if all(payload.values()):
            data = load_data()
            paris_tz = pytz.timezone("Europe/Paris")
            entry = {
                "id": str(uuid.uuid4()),
                "date": datetime.datetime.now(paris_tz).strftime("%d/%m/%Y %H:%M"),
                "motif": "Formulaire à rappeler",
                "nom": payload["nom"],
                "prenom": payload["prenom"],
                "mail": payload["mail"],
                "telephone": payload["telephone"],
                "objet_appel": payload["objet_appel"],
                "creneau_rappel": payload["creneau_rappel"],
                "commentaire": "",
                "attribution": "Mohamed",
                "statut": "A rappeler",
                "traite": False,
            }
            data.setdefault("demandes", []).append(entry)
            save_data(data)
            try:
                envoyer_mail_formulaire_rappel_admin(entry)
            except Exception as e:
                print("⚠️ Erreur envoi mail formulaire rappel admin:", e)
            success = True

    return render_template("formulaire_a_rappeler.html", success=success)


def _rappel_est_traite(rappel):
    statut = str(rappel.get("statut") or "").strip().lower()
    return bool(rappel.get("traite")) or statut in {"traite", "traité"}


def _normaliser_rappel_telephone(rappel):
    """Garde le booléen `traite` et le libellé `statut` synchronisés."""
    rappel["traite"] = _rappel_est_traite(rappel)
    rappel["statut"] = "Traité" if rappel["traite"] else "A rappeler"
    return rappel


@app.route("/admin-devis/rappels", methods=["GET"])
@login_required
def admin_devis_rappels():
    if not can_manage_admin_devis_rappels():
        return jsonify({"ok": False, "error": "forbidden"}), 403

    data = load_data()
    rappels = [d for d in data.get("demandes", []) if d.get("motif") == "Formulaire à rappeler"]
    changed = False
    for rappel in rappels:
        previous_traite = rappel.get("traite")
        previous_statut = rappel.get("statut")
        _normaliser_rappel_telephone(rappel)
        if rappel.get("traite") != previous_traite or rappel.get("statut") != previous_statut:
            changed = True
    if changed:
        save_data(data)
    rappels.sort(key=lambda x: x.get("date", ""), reverse=True)
    return jsonify(rappels)


@app.route("/admin-devis/rappels", methods=["POST"])
@login_required
def create_admin_devis_rappel():
    if not can_manage_admin_devis_rappels():
        return jsonify({"ok": False, "error": "forbidden"}), 403

    data = load_data()
    payload = request.get_json(silent=True) or {}
    entry = {
        "id": str(uuid.uuid4()),
        "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "nom": (payload.get("nom") or "").strip(),
        "prenom": (payload.get("prenom") or "").strip(),
        "mail": (payload.get("mail") or "").strip(),
        "telephone": (payload.get("telephone") or "").strip(),
        "motif": "Formulaire à rappeler",
        "details": "Créé depuis l'admin devis",
        "objet_appel": (payload.get("objet_appel") or "").strip(),
        "creneau_rappel": (payload.get("creneau_rappel") or "").strip(),
        "statut": "A rappeler",
        "traite": False,
        "commentaire": "",
    }
    data.setdefault("demandes", []).append(entry)
    save_data(data)
    try:
        envoyer_mail_formulaire_rappel_admin(entry)
    except Exception as e:
        print("⚠️ Erreur envoi mail formulaire rappel admin:", e)
    return jsonify({"ok": True, "item": entry})


@app.route("/admin-devis/rappels/<rappel_id>", methods=["PATCH", "POST"])
@login_required
def update_admin_devis_rappel(rappel_id):
    if not can_manage_admin_devis_rappels():
        return jsonify({"ok": False, "error": "forbidden"}), 403

    data = load_data()
    payload = request.get_json(silent=True) or {}
    rappel = next((d for d in data.get("demandes", []) if d.get("id") == rappel_id and d.get("motif") == "Formulaire à rappeler"), None)
    if not rappel:
        return jsonify({"ok": False, "error": "not_found"}), 404

    if "traite" in payload:
        rappel["traite"] = bool(payload.get("traite"))
    if "commentaire" in payload:
        rappel["commentaire"] = str(payload.get("commentaire") or "")
    _normaliser_rappel_telephone(rappel)

    save_data(data)
    return jsonify({"ok": True, "item": rappel})


@app.route("/admin-devis/rappels/traites", methods=["DELETE"])
@login_required
def delete_admin_devis_rappels_traites():
    if not can_manage_admin_devis_rappels():
        return jsonify({"ok": False, "error": "forbidden"}), 403

    data = load_data()
    demandes = data.get("demandes", [])
    demandes_conservees = [
        demande
        for demande in demandes
        if not (
            demande.get("motif") == "Formulaire à rappeler"
            and _rappel_est_traite(demande)
        )
    ]
    deleted_count = len(demandes) - len(demandes_conservees)

    if deleted_count:
        data["demandes"] = demandes_conservees
        save_data(data)

    return jsonify({"ok": True, "deleted_count": deleted_count})


@app.route("/admin-devis/rappels/<rappel_id>", methods=["DELETE"])
@login_required
def delete_admin_devis_rappel(rappel_id):
    if not can_manage_admin_devis_rappels():
        return jsonify({"ok": False, "error": "forbidden"}), 403

    data = load_data()
    before = len(data.get("demandes", []))
    data["demandes"] = [d for d in data.get("demandes", []) if not (d.get("id") == rappel_id and d.get("motif") == "Formulaire à rappeler")]
    if len(data["demandes"]) == before:
        return jsonify({"ok": False, "error": "not_found"}), 404
    save_data(data)
    return jsonify({"ok": True})


@app.route("/admin-devis/formulaires")
@login_required
def admin_devis_formulaires():
    data = load_data()
    formulaires = []

    demandes = data.get("demandes", [])
    demande_infos_liees = {
        d.get("source_demande_infos_id")
        for d in demandes
        if d.get("motif") == "Demande de devis détaillé" and d.get("source_demande_infos_id")
    }

    for d in demandes:
        is_target = _est_formulaire_admin_devis(d)
        if not is_target:
            continue

        if d.get("source") == "demande_infos_formations" and d.get("id") in demande_infos_liees:
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
            "statut": d.get("statut", "Non traité"),
            "infos": infos,
            "sort_date": sort_date,
        })

    formulaires.sort(key=lambda formulaire: formulaire["sort_date"], reverse=True)

    now = datetime.datetime.now()
    today = now.date()
    yesterday = today - datetime.timedelta(days=1)
    current_iso_week = today.isocalendar()[:2]

    stats = {
        "today": 0,
        "yesterday": 0,
        "week": 0,
        "month": 0,
        "treated": 0,
        "to_process": 0,
        "total": len(formulaires),
    }

    for formulaire in formulaires:
        form_date = formulaire.get("sort_date")
        if form_date and form_date != datetime.datetime.min:
            form_day = form_date.date()
            if form_day == today:
                stats["today"] += 1
            if form_day == yesterday:
                stats["yesterday"] += 1
            if form_day.isocalendar()[:2] == current_iso_week:
                stats["week"] += 1
            if form_day.year == today.year and form_day.month == today.month:
                stats["month"] += 1

        statut = (formulaire.get("statut") or "").strip().lower()
        if statut == "traité":
            stats["treated"] += 1
        if statut in {"a traiter", "à traiter", "non traité", "non traite"}:
            stats["to_process"] += 1

    return render_template("admin_devis_formulaires.html", formulaires=formulaires, stats=stats)


@app.route("/admin-devis/formulaires/<formulaire_id>/statut", methods=["POST"])
@login_required
def modifier_statut_formulaire_admin_devis(formulaire_id):
    data = load_data()
    demande = next((d for d in data.get("demandes", []) if d.get("id") == formulaire_id), None)
    if not demande:
        return jsonify({"ok": False, "error": "not_found"}), 404

    is_target = _est_formulaire_admin_devis(demande)
    if not is_target:
        return jsonify({"ok": False, "error": "not_found"}), 404

    payload = request.get_json(silent=True) or {}
    raw_statut = str(payload.get("statut") or "").strip().lower()
    if raw_statut in {"traite", "traité"}:
        nouveau_statut = "Traité"
    elif raw_statut in {"a traiter", "à traiter", "non traite", "non traité"}:
        nouveau_statut = "Non traité"
    else:
        return jsonify({"ok": False, "error": "invalid_status"}), 400

    demande["statut"] = nouveau_statut
    save_data(data)
    return jsonify({"ok": True, "statut": nouveau_statut})


@app.route("/admin-devis/formulaires-abandonnes")
@login_required
def admin_devis_formulaires_abandonnes():
    data = load_data()
    data_changed = _nettoyer_formulaires_abandonnes_soumis(data)
    data_changed = _declencher_relances_formulaires_abandonnes_eligibles(data) or data_changed
    if data_changed:
        save_data(data)
    formulaires = []

    abandoned_devis_ids = set()
    abandoned_form_ids = set()

    def append_abandoned_formulaire(formulaire_id, date_value, fields, meta=None):
        if not _has_required_abandoned_form_contact_fields(fields):
            return

        meta = meta or {}
        try:
            sort_date = datetime.datetime.strptime(date_value, "%d/%m/%Y %H:%M")
        except ValueError:
            sort_date = datetime.datetime.min

        formulaires.append({
            "id": formulaire_id,
            "date": date_value,
            "nom": fields.get("nom", ""),
            "prenom": fields.get("prenom", ""),
            "mail": fields.get("mail", ""),
            "telephone": fields.get("telephone", ""),
            "source_label": "Formulaire abandonné",
            "statut": "Abandonné",
            "infos": fields,
            "sort_date": sort_date,
            "manual_abandoned_sent_at": meta.get("manual_abandoned_sent_at", ""),
            "abandoned_mail_sent_at": meta.get("abandoned_mail_sent_at", ""),
            "abandoned_sms_sent_at": meta.get("abandoned_sms_sent_at", ""),
        })

    for draft in data.get("formulaires_abandonnes", []):
        fields = draft.get("fields") or {}
        updated = draft.get("updated_at") or draft.get("created_at") or ""
        append_abandoned_formulaire(draft.get("form_id", ""), updated, fields, draft)
        if draft.get("abandoned_devis_id"):
            abandoned_devis_ids.add(draft.get("abandoned_devis_id"))
        if draft.get("form_id"):
            abandoned_form_ids.add(draft.get("form_id"))

    for demande in data.get("demandes", []):
        if not _est_demande_issue_formulaire_abandonne(demande):
            continue
        if demande.get("id") in abandoned_devis_ids:
            continue
        source_form_id = demande.get("source_formulaire_abandonne_id")
        if source_form_id and source_form_id in abandoned_form_ids:
            continue

        fields = _fields_formulaire_abandonne_depuis_demande(demande)
        append_abandoned_formulaire(demande.get("id", ""), demande.get("date", ""), fields, demande)

    formulaires.sort(key=lambda formulaire: formulaire["sort_date"], reverse=True)

    now = datetime.datetime.now()
    today = now.date()
    yesterday = today - datetime.timedelta(days=1)
    current_iso_week = today.isocalendar()[:2]

    stats = {
        "today": 0,
        "yesterday": 0,
        "week": 0,
        "month": 0,
        "treated": 0,
        "to_process": len(formulaires),
        "total": len(formulaires),
    }
    for formulaire in formulaires:
        form_date = formulaire.get("sort_date")
        if form_date and form_date != datetime.datetime.min:
            form_day = form_date.date()
            if form_day == today:
                stats["today"] += 1
            if form_day == yesterday:
                stats["yesterday"] += 1
            if form_day.isocalendar()[:2] == current_iso_week:
                stats["week"] += 1
            if form_day.year == today.year and form_day.month == today.month:
                stats["month"] += 1
    return render_template("admin_devis_formulaires_abandonnes.html", formulaires=formulaires, stats=stats)


@app.route("/admin-devis/formulaires-abandonnes/<formulaire_id>/relancer", methods=["POST"])
@login_required
def relancer_formulaire_abandonne_admin_devis(formulaire_id):
    data = load_data()
    now_str = datetime.datetime.now(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M")

    draft = next((d for d in data.get("formulaires_abandonnes", []) if d.get("form_id") == formulaire_id), None)
    demande = None
    fields = {}

    if draft:
        fields = draft.get("fields") or {}
    else:
        demande = next((d for d in data.get("demandes", []) if d.get("id") == formulaire_id), None)
        if not demande or not _est_demande_issue_formulaire_abandonne(demande):
            abort(404)
        fields = _fields_formulaire_abandonne_depuis_demande(demande)

    if not _has_required_abandoned_form_contact_fields(fields):
        flash("Impossible de relancer : nom, prénom, email et téléphone sont nécessaires.", "error")
        return redirect(url_for("admin_devis_formulaires_abandonnes"))

    if draft:
        draft["manual_abandoned_sent_at"] = now_str
        _declencher_relance_formulaire_abandonne(draft, fields, now_str)
        mail_ok = bool(draft.get("abandoned_mail_sent_at"))
        sms_ok = bool(draft.get("abandoned_sms_sent_at"))
    else:
        if not demande.get("salesforce_abandoned_sent_at"):
            creer_piste_salesforce(_abandoned_training_form_salesforce_payload(fields))
        demande["salesforce_abandoned_sent_at"] = demande.get("salesforce_abandoned_sent_at") or now_str
        demande["salesforce_abandoned_status"] = ABANDONED_FORM_LABEL
        demande["manual_abandoned_sent_at"] = now_str
        mail_ok = bool(demande.get("abandoned_mail_sent_at")) or _envoyer_mail_formulaire_abandonne_depuis_demande(demande, fields)
        sms_ok = bool(demande.get("abandoned_sms_sent_at")) or envoyer_sms_formulaire_formation_abandonne(demande, fields)

    save_data(data)

    nom_affiche = f"{fields.get('prenom', '')} {fields.get('nom', '')}".strip() or "ce contact"
    if mail_ok and sms_ok:
        flash(f"Mail et SMS formulaire abandonné envoyés, piste Salesforce créée pour {nom_affiche}.", "success")
    elif mail_ok:
        flash(f"Mail formulaire abandonné envoyé et piste Salesforce créée pour {nom_affiche}, mais l'envoi du SMS a échoué.", "error")
    elif sms_ok:
        flash(f"SMS formulaire abandonné envoyé et piste Salesforce créée pour {nom_affiche}, mais l'envoi du mail a échoué.", "error")
    else:
        flash(f"Piste Salesforce créée pour {nom_affiche}, mais l'envoi du mail et du SMS a échoué.", "error")
    return redirect(url_for("admin_devis_formulaires_abandonnes"))


@app.route("/admin-devis/formulaires/<formulaire_id>/supprimer", methods=["POST"])
@login_required
def supprimer_formulaire_admin_devis(formulaire_id):
    data = load_data()

    # 1) Formulaires "classiques" dans demandes
    demandes = data.get("demandes", [])
    to_remove = next((d for d in demandes if d.get("id") == formulaire_id), None)
    if to_remove:
        if _est_demande_issue_formulaire_abandonne(to_remove):
            demandes.remove(to_remove)
            save_data(data)
            return redirect(url_for("admin_devis_formulaires_abandonnes"))

        is_target = _est_formulaire_admin_devis(to_remove)
        if not is_target:
            abort(404)

        data.setdefault("archives", []).append(to_remove)
        demandes.remove(to_remove)
        save_data(data)
        return redirect(url_for("admin_devis_formulaires"))

    # 2) Formulaires abandonnés dans formulaires_abandonnes
    abandons = data.get("formulaires_abandonnes", [])
    abandon_to_remove = next((d for d in abandons if d.get("form_id") == formulaire_id), None)
    if abandon_to_remove:
        abandoned_devis_id = abandon_to_remove.get("abandoned_devis_id")
        demandes[:] = [
            d for d in demandes
            if not (
                (abandoned_devis_id and d.get("id") == abandoned_devis_id)
                or d.get("source_formulaire_abandonne_id") == formulaire_id
            )
        ]
        abandons.remove(abandon_to_remove)
        save_data(data)
        return redirect(url_for("admin_devis_formulaires_abandonnes"))

    abort(404)


@app.route("/admin-devis/formulaires/supprimer-tout", methods=["POST"])
@login_required
def supprimer_tous_formulaires_admin_devis():
    data = load_data()
    demandes = data.get("demandes", [])

    formulaires_cibles = [
        d for d in demandes
        if _est_formulaire_admin_devis(d)
    ]

    if not formulaires_cibles:
        return redirect(url_for("admin_devis_formulaires"))

    data.setdefault("archives", []).extend(formulaires_cibles)
    for demande in formulaires_cibles:
        demandes.remove(demande)

    save_data(data)
    return redirect(url_for("admin_devis_formulaires"))


@app.route("/admin-devis/formulaires-abandonnes/supprimer-tout", methods=["POST"])
@login_required
def supprimer_tous_formulaires_abandonnes_admin_devis():
    data = load_data()
    abandons = data.get("formulaires_abandonnes", [])
    demandes = data.get("demandes", [])
    demandes_sans_abandon = [d for d in demandes if not _est_demande_issue_formulaire_abandonne(d)]

    if not abandons and len(demandes_sans_abandon) == len(demandes):
        return redirect(url_for("admin_devis_formulaires_abandonnes"))

    data["formulaires_abandonnes"] = []
    demandes[:] = demandes_sans_abandon
    save_data(data)
    return redirect(url_for("admin_devis_formulaires_abandonnes"))


@app.route("/admin-devis/formulaires/<formulaire_id>/imprimer")
@login_required
def imprimer_formulaire_admin_devis(formulaire_id):
    data = load_data()
    demande = next((d for d in data.get("demandes", []) if d.get("id") == formulaire_id), None)
    is_abandoned = False

    if demande:
        if _est_demande_issue_formulaire_abandonne(demande):
            is_abandoned = True
        else:
            is_target = _est_formulaire_admin_devis(demande)
            if not is_target:
                abort(404)
    else:
        draft = next((d for d in data.get("formulaires_abandonnes", []) if d.get("form_id") == formulaire_id), None)
        if not draft:
            abort(404)
        is_abandoned = True

        draft_fields = draft.get("fields") or {}
        demande = {
            "id": draft.get("form_id", ""),
            "date": draft.get("updated_at") or draft.get("created_at") or "",
            "motif": "Demande de devis détaillé",
            "source": "demande_infos_formations",
            "details": json.dumps(draft_fields, ensure_ascii=False),
        }

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
    if is_abandoned:
        source_label = "Formulaire abandonné"
    elif demande.get("source") == "demande_infos_formations":
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

    badge_text = "INCONNU"
    campus_theme = "aurillac"
    centre_normalized = (centre_value or "").strip().lower()

    if formation_badge_theme == "vae":
        badge_text = "À DISTANCE"
        campus_theme = "distance"
    else:
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
        is_abandoned=is_abandoned,
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

@app.route("/simulateur-eligibilite-vae-desp", methods=["GET", "POST"])
def simulateur_vae_desp():
    if request.method == "GET":
        return render_template("simulateur_vae_desp.html")

    payload = request.get_json(silent=True) or {}
    nom = str(payload.get("nom") or "").strip()
    prenom = str(payload.get("prenom") or "").strip()
    mail = str(payload.get("mail") or "").strip()
    telephone = str(payload.get("telephone") or "").strip()
    reponses = payload.get("reponses") or {}

    if not nom or not prenom or not mail or not telephone:
        return jsonify({"ok": False, "error": "missing_contact_fields"}), 400
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", mail):
        return jsonify({"ok": False, "error": "invalid_email"}), 400
    telephone_digits = re.sub(r"\D", "", telephone)
    if len(telephone_digits) < 8 or len(telephone_digits) > 15:
        return jsonify({"ok": False, "error": "invalid_phone"}), 400
    if not isinstance(reponses, dict) or any(reponses.get(f"q{i}") not in {"oui", "non"} for i in range(1, 6)):
        return jsonify({"ok": False, "error": "incomplete_answers"}), 400

    score = sum(
        points for question, points in {"q1": 15, "q2": 25, "q3": 25, "q4": 15, "q5": 20}.items()
        if reponses.get(question) == "oui"
    )
    has_experience = any(reponses.get(question) == "oui" for question in ("q2", "q3", "q4"))
    if reponses.get("q5") == "non":
        resultat = "Documents manquants"
    elif has_experience:
        resultat = "Profil favorable"
    else:
        resultat = "Profil à étudier"

    details = {
        "formation": "VAE DESP",
        "score": score,
        "resultat": resultat,
        "reponses": {f"q{i}": reponses.get(f"q{i}") for i in range(1, 6)},
    }
    data = load_data()
    data.setdefault("demandes", []).append({
        "id": str(uuid.uuid4()),
        "nom": nom,
        "prenom": prenom,
        "mail": mail,
        "telephone": telephone,
        "motif": "Simulation éligibilité VAE DESP",
        "source": "simulateur_vae_desp",
        "details": json.dumps(details, ensure_ascii=False),
        "date": datetime.datetime.now(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M"),
        "statut": "Non traité",
    })
    save_data(data)

    creer_piste_salesforce(_payload_salesforce_simulation_vae(
        nom=nom,
        prenom=prenom,
        mail=mail,
        telephone=telephone,
        reponses=reponses,
        score=score,
        resultat=resultat,
    ))

    return jsonify({"ok": True, "score": score, "resultat": resultat})

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
            archive_to_delete = next((a for a in data["archives"] if a.get("id") == archive_id), None)
            if archive_to_delete:
                supprimer_fichiers_demande(archive_to_delete)
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
            for archive in data.get("archives", []):
                supprimer_fichiers_demande(archive)
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


def _supprimer_fichiers_devis(devis):
    """Supprime les fichiers générés associés à un devis."""
    for cle in ("pdf_path", "pdf_client_path"):
        chemin = devis.get(cle)
        if chemin and os.path.isfile(chemin):
            try:
                os.remove(chemin)
            except OSError:
                pass


@app.route("/admin-devis/simulations-vae/delete/<simulation_id>", methods=["POST"])
@login_required
def delete_simulation_vae(simulation_id):
    data = load_data()
    data["demandes"] = [
        demande for demande in data.get("demandes", [])
        if not (
            demande.get("id") == simulation_id
            and demande.get("source") == "simulateur_vae_desp"
        )
    ]
    save_data(data)
    return redirect(url_for("admin_devis"))


@app.route("/admin-devis/simulations-vae/delete-all", methods=["POST"])
@login_required
def delete_all_simulations_vae():
    data = load_data()
    data["demandes"] = [
        demande for demande in data.get("demandes", [])
        if demande.get("source") != "simulateur_vae_desp"
    ]
    save_data(data)
    return redirect(url_for("admin_devis"))


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
        _supprimer_fichiers_devis(devis)
        data["demandes"].remove(devis)
        save_data(data)

    return redirect(url_for("admin_devis"))


@app.route("/admin-devis/delete-all", methods=["POST"])
@login_required
def delete_all_devis():
    data = load_data()
    demandes_conservees = []

    for demande in data.get("demandes", []):
        if demande.get("motif") == "Demande de devis détaillé":
            _supprimer_fichiers_devis(demande)
        else:
            demandes_conservees.append(demande)

    data["demandes"] = demandes_conservees
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

    formation_label = PLAN_FORMATIONS.get(formation, formation)

    devis_ctx = build_devis_context(
        formation_code=formation,
        formation_label=formation_label,
        dates_txt=infos.get("dates", ""),
        sequence=1,
        formation_details=infos,
    )

    tarif = get_formation_tarif(formation, infos)

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

    centre_code = _normalize_centre_code(infos.get("centre"))
    centre_label, centre_address = _centre_label_and_address(centre_code)
    centre_legal = _centre_legal_block(centre_code)

    date_devis = datetime.date.today()
    date_devis_txt = (devis.get("date") or "").strip()
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            if date_devis_txt:
                date_devis = datetime.datetime.strptime(date_devis_txt, fmt).date()
                break
        except ValueError:
            continue

    # -------------------------------
    # Échéancier (si date examen)
    # -------------------------------
    date_examen_txt = (infos.get("date_examen") or "").strip()
    if not date_examen_txt:
        date_examen_txt = _parse_exam_date_from_dates_txt(infos.get("dates", ""))

    date_examen = None
    try:
        if date_examen_txt:
            date_examen = datetime.datetime.strptime(
                date_examen_txt, "%Y-%m-%d"
            ).date()
    except:
        date_examen = None

    # 🔁 Échéancier manuel prioritaire
    if devis.get("echeancier_manuel"):
        echeances = devis["echeancier_manuel"]
    else:
        echeances = build_echeances_mensuelles(
            reste=reste_sans_ft,
            date_devis=date_devis,
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
        centre_label=centre_label,
        centre_address=centre_address,
        centre_legal=centre_legal,
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
    
    formation_label = PLAN_FORMATIONS.get(formation, formation)


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

    formation_label = PLAN_FORMATIONS.get(formation, formation)

    devis_ctx = build_devis_context(
        formation_code=formation,
        formation_label=formation_label,
        dates_txt=infos.get("dates", ""),
        sequence=1,
        formation_details=infos,
    )

    tarif = get_formation_tarif(formation, infos)

    try:
        cpf = int(float(infos.get("cpf_montant", 0)))
    except:
        cpf = 0

    ft = max(tarif - cpf, 0) if infos.get("france_travail") == "OUI" else 0
    reste_avec_ft = max(tarif - cpf - ft, 0)
    reste_sans_ft = max(tarif - cpf, 0)
    centre_code = _normalize_centre_code(infos.get("centre"))
    centre_label, centre_address = _centre_label_and_address(centre_code)
    centre_legal = _centre_legal_block(centre_code)

    # 🔁 Échéancier : manuel PRIORITAIRE, sinon automatique
    if devis.get("echeancier_manuel") and len(devis["echeancier_manuel"]) > 0:
        echeances = devis["echeancier_manuel"]
    else:
        # date examen (champ dédié prioritaire, fallback via texte de session)
        date_examen = None
        date_examen_txt = (infos.get("date_examen") or "").strip()
        if not date_examen_txt:
            date_examen_txt = _parse_exam_date_from_dates_txt(infos.get("dates", ""))
        try:
            if date_examen_txt:
                date_examen = datetime.datetime.strptime(
                    date_examen_txt, "%Y-%m-%d"
                ).date()
        except:
            date_examen = None

        # date devis (évite de recalculer un échéancier différent selon la date de consultation)
        date_devis = datetime.date.today()
        date_devis_txt = (devis.get("date") or "").strip()
        for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
            try:
                if date_devis_txt:
                    date_devis = datetime.datetime.strptime(date_devis_txt, fmt).date()
                    break
            except ValueError:
                continue
    
        echeances = build_echeances_mensuelles(
            reste=reste_sans_ft,
            date_devis=date_devis,
            date_examen=date_examen
        )


    return render_template(
        "plan_financement.html",
        prenom=devis.get("prenom"),
        nom=devis.get("nom"),
        email=devis.get("mail"),
        formation_label=formation_label,
        dates=infos.get("dates"),
        centre_label=centre_label,
        centre_address=centre_address,
        centre_legal=centre_legal,
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
        sequence=1,
        formation_details=simulation,
    )
    centre_code = _normalize_centre_code(simulation.get("centre"))
    centre_label, centre_address = _centre_label_and_address(centre_code)
    centre_legal = _centre_legal_block(centre_code)

    return render_template(
        "plan_financement.html",
        prenom=plan.get("prenom"),
        nom=plan.get("nom"),
        email=plan.get("mail"),
        formation_label=formation_label,
        dates=simulation.get("dates", ""),
        centre_label=centre_label,
        centre_address=centre_address,
        centre_legal=centre_legal,
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
