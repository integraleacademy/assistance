from flask import Flask, render_template, request, send_file, send_from_directory, url_for, redirect, abort, jsonify, has_app_context
from flask import render_template_string
import json, os, datetime, uuid, pytz, smtplib, re, copy, unicodedata, tempfile, traceback, html, base64, hashlib, hmac, time, sqlite3, threading, shutil, gzip, mimetypes, io, zipfile
import html as html_module
from html.parser import HTMLParser
from urllib.parse import quote, urlparse
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
from flask import session, flash

import calendar
from datetime import date as _date
import requests
from openai import OpenAI
from candidate_scoring import (
    TRAINING_PRICES_CENTS,
    calculate_candidate_integration_score,
    normalize_cpf_amount,
)
from candidate_ai_analysis import (
    AI_CANDIDATE_ANALYSIS_VERSION, AI_CANDIDATE_PROMPT_VERSION,
    AI_CANDIDATE_SYSTEM_PROMPT, CANDIDATE_AI_RESPONSE_SCHEMA, CandidateAIResponseError,
    build_candidate_ai_context as _build_candidate_ai_context,
    build_candidate_ai_fallback,
    classify_calendly_appointment, compute_candidate_ai_source_hash,
    finalize_candidate_ai_analysis, validate_candidate_ai_analysis,
)
from crm_exports import (
    CRM_EXPORT_DEFINITIONS,
    build_crm_export_workbook,
    crm_export_filename,
)
from wedof_governor_client import (
    WedofGovernorError,
    WedofQuotaExceeded,
    acquire_wedof_lock,
    release_wedof_lock,
    reserve_wedof_request,
)
from yousign_service import (
    YousignClient,
    YousignError,
    get_yousign_config,
    is_yousign_configured,
    is_yousign_sandbox,
    normalize_french_mobile,
    sanitize_yousign_external_id,
    yousign_service_access_message,
)

SALESFORCE_URL = "https://webto.salesforce.com/servlet/servlet.WebToLead?encoding=UTF-8&orgId=00DJ9000000PT9F"
SALESFORCE_OID = "00DJ9000000PT9F"
SALESFORCE_LEAD_SOURCE_VALUE = "Google"
SALESFORCE_INFOS_COMPLEMENTAIRES_FIELD = "00NSa00000GcKVx"
SALESFORCE_ORIGINE_FIELD = "00NSa00000KPDmX"
SALESFORCE_CHOIX_DIRIGEANT_DESP_FIELD = "00NSa00000KDPJd"
ABANDONED_INFOS_COMPLEMENTAIRES_MESSAGE = "FORMULAIRE ABANDONN√â - Prospect n‚Äôa pas termin√© le formulaire complet."

def valeur_refus_ft(value):
    if value == "OUI":
        return "Si FT refuse le financement = financement personnel OK"
    if value == "NON":
        return "Si FT refuse le financement = pas de possibilit√© de financer personnellement"
    return ""

def _has_required_abandoned_form_contact_fields(fields):
    required_fields = ("nom", "prenom", "mail", "telephone")
    return all((fields.get(field) or "").strip() for field in required_fields)


def _is_abandoned_training_form_ready_for_salesforce(fields):
    return _has_required_abandoned_form_contact_fields(fields)


ABANDONED_FORM_LABEL = "Formulaire abandonn√©"
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
        f"{question.upper()} : {reponse or 'NON RENSEIGN√â'}"
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
            "SIMULATEUR √âLIGIBILIT√â VAE DESP COMPL√âT√â\n"
            f"Score : {score}%\n"
            f"R√©sultat : {resultat}\n"
            f"R√©ponses : {resume_reponses}"
        ),
        **reponses_salesforce,
    }


def _crm_create_contact_from_vae_simulation(data, nom, prenom, mail, telephone,
                                            reponses, score, resultat):
    """Cr√©e une piste CRM tra√ßable √† partir du test public VAE DESP."""
    now = _crm_now()
    answers = {f"q{i}": str(reponses.get(f"q{i}") or "").strip().lower()
               for i in range(1, 6)}
    contact = {
        "id": str(uuid.uuid4()),
        "prenom": _crm_format_first_name(prenom),
        "nom": _crm_format_last_name(nom),
        "telephone": telephone, "mail": mail,
        "formation": "DESP", "desp_type": "VAE", "lieu": "",
        "statut": "Nouveaux", "dates_formation": "",
        "cpf": "", "cpf_montant": "", "carte_pro": "",
        "antecedents": "", "garde_vue": "", "titre_sejour": "",
        "titre_sejour_cnaps": "", "compte_cnaps": "", "cnaps_username": "",
        "cnaps_birth_year": "", "cnaps_password": "", "integration_dracar": "",
        "identite_creation": "",
        "identite_ok": "", "financement_ft": "", "statut_demande_financement_ft": "",
        "montant_accorde_ft": "", "financement_perso_possible": "",
        "refus_ft_perso": "", "reste_a_charge_perso": "", "inscrit_ft": "",
        "relance_date": "", "origine": "Simulateur VAE",
        "commentaires": f"Test d‚Äô√©ligibilit√© VAE DESP r√©alis√© ‚Äî score : {score}% ‚Äî {resultat}.",
        "created_at": now, "updated_at": now, "activities": [],
        "source": "simulateur_vae_desp",
        "vae_eligibility": {
            "completed_at": now, "score": score, "resultat": resultat,
            "reponses": answers,
        },
    }
    _crm_activity(contact, "creation", "Test d‚Äô√©ligibilit√© VAE DESP compl√©t√©",
                  f"Score : {score}% ¬∑ R√©sultat : {resultat}")
    matched, _, _ = find_or_create_crm_contact(
        data, {"nom": nom, "prenom": prenom, "mail": mail,
               "telephone": telephone, "formation": "DESP"},
        "simulateur_vae_desp", proposed_contact=contact,
    )
    return matched


def creer_piste_salesforce(form):
    print("FORMULAIRE RECU:", dict(form))
    formulaire_abandonne = _est_payload_formulaire_abandonne(form)
    description = "\n".join([
        f"{key} : {value}"
        for key, value in form.items()
    ])

    centre = form.get("centre", "")
    if centre == "cote_azur":
        lieu = "C√¥te d'Azur"
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
        "POEI": "POEI",
    }
    formation_sf = formation_map.get(form.get("formation", ""), "")

    oui_non_map = {"OUI": "Oui", "NON": "Non"}
    cpf_sf = oui_non_map.get(form.get("cpf_consulte", ""), "")
    france_travail_sf = oui_non_map.get(form.get("france_travail", ""), "")

    choix_dirigeant_desp = _choix_dirigeant_desp_salesforce(form)
    origine_salesforce = (
        form.get(SALESFORCE_ORIGINE_FIELD)
        or form.get("origine")
        or SALESFORCE_LEAD_SOURCE_VALUE
    )

    data = {
        "oid": SALESFORCE_OID,
        "retURL": "https://assistance-alw9.onrender.com/confirmation-demande-informations",
        "first_name": form.get("prenom", ""),
        "last_name": form.get("nom", "Sans nom"),
        "email": form.get("mail", ""),
        "phone": form.get("telephone", ""),
        "mobile": form.get("telephone", ""),
        "company": "Particulier",
        # Origine personnalis√©e Salesforce
        SALESFORCE_ORIGINE_FIELD: origine_salesforce,
        "industry": "Education",
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
    # v peut √™tre int/float/str (ex: "OFFERTS", "INCLUS")
    if isinstance(v, (int, float)):
        # pas de d√©cimales
        return f"{int(v)} ‚Ç¨"
    return str(v)

def _parse_dates_range(dates_txt: str):
    """
    Essaie d'extraire une date d√©but/fin √† partir d'un texte comme :
    '9 mars au 21 avril 2026' ou '09 mars 2026 au 21 avril 2026' etc.
    Si √ßa √©choue, renvoie (None, None)
    """
    if not dates_txt:
        return (None, None)

    import re
    mois_fr = {
        "janvier": 1, "f√©vrier": 2, "fevrier": 2, "mars": 3, "avril": 4,
        "mai": 5, "juin": 6, "juillet": 7, "ao√ªt": 8, "aout": 8,
        "septembre": 9, "octobre": 10, "novembre": 11, "d√©cembre": 12, "decembre": 12
    }

    # 1) Essai direct sur des dates num√©riques (ex: 22/06/2026 ... 10/08/2026)
    matches_numeric = re.findall(r"(\d{1,2})[/-](\d{1,2})[/-](20\d{2})", dates_txt)
    if len(matches_numeric) >= 2:
        try:
            d1 = _date(int(matches_numeric[0][2]), int(matches_numeric[0][1]), int(matches_numeric[0][0]))
            d2 = _date(int(matches_numeric[1][2]), int(matches_numeric[1][1]), int(matches_numeric[1][0]))
            return (d1, d2)
        except Exception:
            pass

    # on r√©cup√®re l'ann√©e (premi√®re ann√©e trouv√©e)
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
        # ann√©e (si pr√©sente)
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
    return PLAN_TARIFS.get(formation_code, 0)


def build_devis_context(
    formation_code: str,
    formation_label: str,
    dates_txt: str,
    sequence: int = 1,
    formation_details=None,
):
    """
    Retourne un dict pr√™t √† injecter dans plan_financement.html :
    devis_num, devis_date, devis_valid_until, date_debut, date_fin, devis_lignes, devis_total
    """
    today = _date.today()
    devis_num = f"{today.year}-{today.month:02d}{today.day:02d}{sequence:04d}"  # AAAA-MMDD0001
    devis_date = today.strftime("%d/%m/%Y")
    devis_valid_until = _add_one_month(today).strftime("%d/%m/%Y")

    d1, d2 = _parse_dates_range(dates_txt or "")
    date_debut = d1.strftime("%d/%m/%Y") if d1 else "‚Äî"
    date_fin = d2.strftime("%d/%m/%Y") if d2 else "‚Äî"

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
            {"intitule": "Agent de Pr√©vention et de S√©curit√© (APS)", "prix_unitaire": _eur(1650), "quantite": 1, "total": _eur(1650)},
            {"intitule": "Frais de dossier", "prix_unitaire": "OFFERTS", "quantite": 1, "total": _eur(0)},
        ]
        total = 1650

    elif formation_code == "DESP_INIT":
        lignes = [
            {"intitule": "Formation initiale DSP Dirigeant d‚Äôentreprise de s√©curit√© priv√©e", "prix_unitaire": _eur(4300), "quantite": 1, "total": _eur(4300)},
            {"intitule": "Frais de dossier", "prix_unitaire": "OFFERTS", "quantite": 1, "total": _eur(0)},
            {"intitule": "Acc√®s formation en ligne e-learning", "prix_unitaire": "INCLUS", "quantite": 1, "total": _eur(0)},
        ]
        total = 4300

    elif formation_code == "DESP_VAE":
        lignes = [
            {"intitule": "Etude de recevabilit√© (Livret 1)", "prix_unitaire": "OFFERTE", "quantite": 1, "total": _eur(0)},
            {"intitule": "Suivi de dossier, frais administratifs, pr√©sentation du dossier aupr√®s du certificateur (acompte de 30%)", "prix_unitaire": _eur(1140), "quantite": 1, "total": _eur(1140)},
            {"intitule": "Passage devant le jury de certification (solde)", "prix_unitaire": _eur(2660), "quantite": 1, "total": _eur(2660)},
        ]
        total = 3800

    elif formation_code == "VTC":
        lignes = [
            {"intitule": "Formation Chauffeur VTC incluant :\nFormation th√©orique en ligne √† distance\nFormation pratique sur v√©hicule √† doubles commandes\nFrais d‚Äôexamen Chambre des m√©tiers\nPr√™t du v√©hicule √† doubles commandes le jour de l‚Äôexamen pratique\nLe livre officiel Chauffeur VTC",
             "prix_unitaire": _eur(1500), "quantite": 1, "total": _eur(1500)},
            {"intitule": "Frais de dossier", "prix_unitaire": "OFFERTS", "quantite": 1, "total": _eur(0)},
        ]
        total = 1500

    elif formation_code == "SSIAP":
        total = get_formation_tarif(formation_code, formation_details)
        intitule = "Agent de s√©curit√© incendie SSIAP 1"
        lignes = [
            {"intitule": intitule, "prix_unitaire": _eur(total), "quantite": 1, "total": _eur(total)},
            {"intitule": "Frais de dossier", "prix_unitaire": "OFFERTS", "quantite": 1, "total": _eur(0)},
        ]

    else:
        # fallback si jamais
        lignes = [
            {"intitule": formation_label or formation_code or "Formation", "prix_unitaire": "‚Äî", "quantite": 1, "total": "‚Äî"},
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
    "A3P": "A3P ‚Äì Agent de Protection Physique des Personnes",
    "APS": "APS ‚Äì Agent de Pr√©vention et de S√©curit√©",
    "VTC": "VTC ‚Äì Chauffeur de transport avec chauffeur",
    "DESP_INIT": "DESP ‚Äì Dirigeant d‚Äôentreprise de s√©curit√© (initial)",
    "DESP_VAE": "DESP ‚Äì Dirigeant d‚Äôentreprise de s√©curit√© (VAE)",
    "SSIAP": "SSIAP 1 ‚Äì Agent de s√©curit√© incendie"
}

VTC_CPF_REGISTRATION_URL = (
    "https://www.moncompteformation.gouv.fr/espace-prive/html/#/formation/"
    "recherche/84089988400026_vtc2022/84089988400026_vtc2022mixte"
    "?contexteFormation=ACTIVITE_PROFESSIONNELLE"
)

PLAN_TARIFS = {
    "A3P": TRAINING_PRICES_CENTS["A3P"] // 100,
    "APS": TRAINING_PRICES_CENTS["APS"] // 100,
    "VTC": TRAINING_PRICES_CENTS["VTC"] // 100,
    "DESP_INIT": TRAINING_PRICES_CENTS["DESP_INITIAL"] // 100,
    "DESP_VAE": TRAINING_PRICES_CENTS["DESP_VAE"] // 100,
    "SSIAP": TRAINING_PRICES_CENTS["SSIAP_1"] // 100,
}

FORMATION_CENTRES = {
    "cote_azur": "Int√©grale Academy C√¥te d‚ÄôAzur",
    "auvergne": "Int√©grale Academy Terres d‚ÄôAuvergne",
    "paris": "Int√©grale Academy Paris",
}

SECRETARIAT_FORMATIONS = {
    "A3P": {"short": "A3P", "duration": "328 h", "price": "4 200 ‚Ç¨ TTC", "format": "Pr√©sentiel", "purpose": "Se former √† la protection rapproch√©e des personnes et exercer comme agent de protection physique.", "funding": "CPF, France Travail, financement employeur ou personnel (selon √©ligibilit√©).", "calendly": "https://calendly.com/integraleacademy/apr"},
    "APS": {"short": "APS", "duration": "175 h ¬∑ 5 semaines", "price": "1 650 ‚Ç¨ TTC", "format": "Pr√©sentiel", "purpose": "Obtenir les comp√©tences n√©cessaires aux missions de surveillance, de pr√©vention et de s√©curit√© priv√©e.", "funding": "CPF, France Travail, financement employeur ou personnel (selon √©ligibilit√©).", "calendly": "https://calendly.com/integraleacademy/aps"},
    "SSIAP": {"short": "SSIAP 1", "duration": "Formation + examen", "price": "1 230 ‚Ç¨ TTC", "format": "Pr√©sentiel", "purpose": "Devenir agent de s√©curit√© incendie dans les ERP et les immeubles de grande hauteur.", "funding": "France Travail, employeur ou financement personnel (selon √©ligibilit√©).", "calendly": "https://calendly.com/integraleacademy/ssiap1"},
    "DESP_INIT": {"short": "DESP initial", "duration": "245 h", "price": "4 300 ‚Ç¨ TTC", "format": "175 h √† distance + 70 h en pr√©sentiel", "purpose": "Acqu√©rir l'aptitude professionnelle permettant de cr√©er et diriger une entreprise de s√©curit√© priv√©e.", "funding": "CPF, France Travail, financement employeur ou personnel (selon √©ligibilit√©).", "calendly": "https://calendly.com/integraleacademy/dirigeant"},
    "DESP_VAE": {"short": "VAE DESP", "duration": "Accompagnement individualis√©", "price": "3 800 ‚Ç¨ TTC", "format": "100 % √† distance", "purpose": "Faire reconna√Ætre son exp√©rience afin d'obtenir la certification de dirigeant d'entreprise de s√©curit√© priv√©e.", "funding": "CPF, employeur ou financement personnel (selon √©ligibilit√©).", "calendly": "https://calendly.com/integraleacademy/dirigeant"},
    "VTC": {"short": "Chauffeur VTC", "duration": "Th√©orie en ligne + pratique", "price": "1 500 ‚Ç¨ TTC", "format": "Hybride", "purpose": "Pr√©parer l'examen VTC et ma√Ætriser les bases n√©cessaires √† l'activit√© de chauffeur professionnel.", "funding": "CPF, France Travail ou financement personnel (selon √©ligibilit√©).", "calendly": "https://calendly.com/integraleacademy/chauffeurvtc"},
    "BTS_MOS": {"short": "BTS MOS", "label": "BTS Management Op√©rationnel de la S√©curit√© (MOS)", "duration": "2 ans", "price": "Formation prise en charge en alternance", "format": "Alternance", "purpose": "Piloter des prestations de s√©curit√© et encadrer des √©quipes op√©rationnelles.", "funding": "Prise en charge par l'OPCO de l'employeur.", "calendly": "https://calendly.com/integraleacademy/formation"},
    "BTS_MCO": {"short": "BTS MCO", "label": "BTS Management Commercial Op√©rationnel (MCO)", "duration": "2 ans", "price": "Formation prise en charge en alternance", "format": "Alternance", "purpose": "G√©rer une unit√© commerciale et d√©velopper la relation client et les ventes.", "funding": "Prise en charge par l'OPCO de l'employeur.", "calendly": "https://calendly.com/integraleacademy/formation"},
    "BTS_CI": {"short": "BTS CI", "label": "BTS Commerce International (CI)", "duration": "2 ans", "price": "Formation prise en charge en alternance", "format": "Alternance", "purpose": "D√©velopper et suivre les activit√©s commerciales d'une entreprise √† l'international.", "funding": "Prise en charge par l'OPCO de l'employeur.", "calendly": "https://calendly.com/integraleacademy/formation"},
    "BTS_NDRC": {"short": "BTS NDRC", "label": "BTS N√©gociation et Digitalisation de la Relation Client (NDRC)", "duration": "2 ans", "price": "Formation prise en charge en alternance", "format": "Alternance", "purpose": "D√©velopper la client√®le, n√©gocier et piloter la relation client sur tous les canaux.", "funding": "Prise en charge par l'OPCO de l'employeur.", "calendly": "https://calendly.com/integraleacademy/formation"},
    "BTS_PI": {"short": "BTS PI", "label": "BTS Professions Immobili√®res (PI)", "duration": "2 ans", "price": "Formation prise en charge en alternance", "format": "Alternance", "purpose": "Se pr√©parer aux m√©tiers de la transaction et de la gestion immobili√®res.", "funding": "Prise en charge par l'OPCO de l'employeur.", "calendly": "https://calendly.com/integraleacademy/formation"},
    "BTS_CG": {"short": "BTS CG", "label": "BTS Comptabilit√© Gestion (CG)", "duration": "2 ans", "price": "Formation prise en charge en alternance", "format": "Alternance", "purpose": "Ma√Ætriser les op√©rations comptables, fiscales et sociales et contribuer au pilotage de l'organisation.", "funding": "Prise en charge par l'OPCO de l'employeur.", "calendly": "https://calendly.com/integraleacademy/formation"},
}

SECRETARIAT_WEBSITE_URL = "https://www.integraleacademy.com/"
SECRETARIAT_DOSSIER_URL = "https://www.integraleacademy.com/dossiersfc"
SECRETARIAT_PLANNING_URL = "https://www.integraleacademy.com/calendrier-formations"
SECRETARIAT_AI_URL = "https://chatgpt.com/g/g-69cb47858f948191b7daabca5892786d-infos-formations-integrale-academy"

_SECRETARIAT_WEBSITE_CACHE = {"sitemap": (0, []), "pages": {}}
_SECRETARIAT_WEBSITE_CACHE_LOCK = threading.Lock()
_SECRETARIAT_WEBSITE_CACHE_SECONDS = 15 * 60
_SECRETARIAT_WEBSITE_MAX_PAGES = 8
_SECRETARIAT_WEBSITE_ALIASES = {
    "A3P": ("a3p", "apr", "protection", "garde du corps"),
    "APS": ("aps", "agent de prevention", "agent de s√©curit√©"),
    "SSIAP": ("ssiap", "incendie"),
    "DESP_INIT": ("desp", "dirigeant", "entreprise de s√©curit√©"),
    "DESP_VAE": ("desp", "vae", "dirigeant"),
    "VTC": ("vtc", "chauffeur"),
    "BTS_MOS": ("bts mos", "management op√©rationnel"),
    "BTS_MCO": ("bts mco", "management commercial"),
    "BTS_CI": ("bts ci", "commerce international"),
    "BTS_NDRC": ("bts ndrc", "n√©gociation"),
    "BTS_PI": ("bts pi", "professions immobili√®res"),
    "BTS_CG": ("bts cg", "comptabilit√© gestion"),
}


class _SecretariatWebsiteTextParser(HTMLParser):
    """Extract readable copy from an official website page without extra dependencies."""

    _ignored_tags = {"script", "style", "noscript", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self._ignored_tags:
            self._ignored_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in self._ignored_tags and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data):
        if not self._ignored_depth and data.strip():
            self.parts.append(data.strip())


def _secretariat_official_url(url):
    parsed = urlparse(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in {
        "integraleacademy.com", "www.integraleacademy.com"
    }


def _secretariat_fetch_official_text(url):
    """Fetch one allow-listed official page and cache its visible text."""
    now = time.monotonic()
    with _SECRETARIAT_WEBSITE_CACHE_LOCK:
        cached = _SECRETARIAT_WEBSITE_CACHE["pages"].get(url)
    if cached and now - cached[0] < _SECRETARIAT_WEBSITE_CACHE_SECONDS:
        return cached[1]
    if not _secretariat_official_url(url):
        return ""
    response = requests.get(url, timeout=8, headers={"User-Agent": "IntegraleAcademy-Secretariat/1.0"})
    response.raise_for_status()
    if "text/html" not in response.headers.get("Content-Type", "text/html").lower():
        return ""
    parser = _SecretariatWebsiteTextParser()
    parser.feed(response.text[:2_000_000])
    text_content = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()[:16_000]
    with _SECRETARIAT_WEBSITE_CACHE_LOCK:
        _SECRETARIAT_WEBSITE_CACHE["pages"][url] = (now, text_content)
    return text_content


def _secretariat_sitemap_urls():
    now = time.monotonic()
    with _SECRETARIAT_WEBSITE_CACHE_LOCK:
        cached_at, cached_urls = _SECRETARIAT_WEBSITE_CACHE["sitemap"]
    if cached_urls and now - cached_at < _SECRETARIAT_WEBSITE_CACHE_SECONDS:
        return cached_urls
    response = requests.get(f"{SECRETARIAT_WEBSITE_URL}sitemap.xml", timeout=8,
                            headers={"User-Agent": "IntegraleAcademy-Secretariat/1.0"})
    response.raise_for_status()
    urls = []
    sitemap_urls = re.findall(r"<loc>\s*(.*?)\s*</loc>", response.text, flags=re.I)
    # Wix may expose a sitemap index. Read its small child sitemaps before selecting pages.
    for location in sitemap_urls[:20]:
        location = html_module.unescape(location)
        if location.lower().endswith(".xml") and _secretariat_official_url(location):
            child = requests.get(location, timeout=8,
                                 headers={"User-Agent": "IntegraleAcademy-Secretariat/1.0"})
            child.raise_for_status()
            urls.extend(re.findall(r"<loc>\s*(.*?)\s*</loc>", child.text, flags=re.I))
        else:
            urls.append(location)
    urls = list(dict.fromkeys(html_module.unescape(url) for url in urls
                              if _secretariat_official_url(html_module.unescape(url))))
    with _SECRETARIAT_WEBSITE_CACHE_LOCK:
        _SECRETARIAT_WEBSITE_CACHE["sitemap"] = (now, urls)
    return urls


def _secretariat_website_context(formation_code, question):
    """Retrieve the most relevant official pages for a training question."""
    try:
        try:
            urls = _secretariat_sitemap_urls()
        except Exception:
            app.logger.warning("Sitemap Int√©grale Academy indisponible", exc_info=True)
            urls = []
        terms = (*_SECRETARIAT_WEBSITE_ALIASES.get(formation_code, ()),
                 *re.findall(r"[a-z√†-√ø0-9]{4,}", question.lower()))
        def relevance(url):
            normalized = unicodedata.normalize("NFKD", url).encode("ascii", "ignore").decode().lower()
            return sum(3 if term in _SECRETARIAT_WEBSITE_ALIASES.get(formation_code, ()) else 1
                       for term in terms
                       if unicodedata.normalize("NFKD", term).encode("ascii", "ignore").decode() in normalized)
        ranked = sorted(urls, key=lambda url: (relevance(url), url == SECRETARIAT_WEBSITE_URL), reverse=True)
        selected = [SECRETARIAT_WEBSITE_URL, *[url for url in ranked if relevance(url) > 0]]
        excerpts = []
        for url in list(dict.fromkeys(selected))[:_SECRETARIAT_WEBSITE_MAX_PAGES]:
            try:
                content = _secretariat_fetch_official_text(url)
            except Exception:
                app.logger.warning("Page Int√©grale Academy indisponible : %s", url, exc_info=True)
                continue
            if content:
                excerpts.append(f"SOURCE : {url}\n{content}")
        return "\n\n".join(excerpts)[:40_000]
    except Exception:
        app.logger.exception("Lecture du site Int√©grale Academy impossible")
        return ""

# Rep√®res volontairement r√©dig√©s pour une personne qui ne conna√Æt pas encore les
# formations. Les conditions d√©finitives restent valid√©es par l'√©quipe admissions.
_SECURITY_TRAINING_GUIDANCE = {
    "A3P": {
        "audience": "Personnes souhaitant travailler dans la protection rapproch√©e.",
        "prerequisites": "Projet compatible avec les conditions d'acc√®s aux m√©tiers de la s√©curit√© priv√©e ; l'√©quipe v√©rifie la situation et les d√©marches CNAPS.",
        "certification": "Titre A3P reconnu par l'√âtat (niveau 4) et pr√©paration aux d√©marches de carte professionnelle CNAPS.",
        "topics": ["Cadre l√©gal de la s√©curit√© priv√©e", "Pr√©paration et s√©curisation des d√©placements", "Gestion des risques et situations sensibles", "Posture, discr√©tion et communication professionnelles"],
    },
    "APS": {
        "audience": "Personnes souhaitant devenir agent de s√©curit√© priv√©e.",
        "prerequisites": "Projet compatible avec les conditions d'acc√®s √† la s√©curit√© priv√©e ; l'√©quipe accompagne la v√©rification des d√©marches CNAPS.",
        "certification": "Formation r√©glement√©e pr√©parant √† l'exercice du m√©tier et aux d√©marches de carte professionnelle CNAPS.",
        "topics": ["Surveillance et pr√©vention", "Accueil, filtrage et contr√¥le d'acc√®s", "Gestion des conflits", "Incendie et secours √† personne"],
    },
    "SSIAP": {
        "audience": "Personnes visant un poste d'agent de s√©curit√© incendie en ERP ou IGH.",
        "prerequisites": "Des pr√©requis r√©glementaires s'appliquent, notamment en secourisme et aptitude m√©dicale ; faire valider le dossier par l'√©quipe.",
        "certification": "Pr√©paration au dipl√¥me SSIAP 1, sous r√©serve de r√©ussite aux √©preuves.",
        "topics": ["Pr√©vention incendie", "Installations techniques", "R√¥les et missions de l'agent", "Mises en situation et pr√©paration √† l'examen"],
    },
    "DESP_INIT": {
        "audience": "Cr√©ateurs, repreneurs ou futurs dirigeants d'une entreprise de s√©curit√© priv√©e.",
        "prerequisites": "Le projet et les conditions r√©glementaires d'acc√®s √† la direction d'une activit√© de s√©curit√© priv√©e doivent √™tre v√©rifi√©s.",
        "certification": "Pr√©paration √† l'aptitude professionnelle de dirigeant d'entreprise de s√©curit√© priv√©e.",
        "topics": ["Cadre juridique et r√©glementaire", "Gestion administrative et financi√®re", "Management et ressources humaines", "D√©veloppement commercial"],
    },
    "DESP_VAE": {
        "audience": "Professionnels exp√©riment√©s souhaitant faire reconna√Ætre leur exp√©rience de direction en s√©curit√© priv√©e.",
        "prerequisites": "L'exp√©rience doit correspondre aux comp√©tences de la certification ; un entretien permet de confirmer la recevabilit√© du projet.",
        "certification": "Accompagnement √† la VAE du titre de dirigeant d'entreprise de s√©curit√© priv√©e.",
        "topics": ["Diagnostic de l'exp√©rience", "Constitution du dossier", "Explicitation des comp√©tences", "Pr√©paration au jury"],
    },
    "VTC": {
        "audience": "Personnes ayant un projet de chauffeur VTC salari√© ou ind√©pendant.",
        "prerequisites": "Le candidat doit faire v√©rifier les conditions r√©glementaires applicables √† l'examen et √† la carte professionnelle VTC.",
        "certification": "Pr√©paration √† l'examen VTC et aux d√©marches n√©cessaires au lancement de l'activit√©.",
        "topics": ["R√©glementation VTC", "Gestion et d√©veloppement commercial", "S√©curit√© routi√®re", "Pr√©paration th√©orique et pratique √† l'examen"],
    },
}
for _code, _details in SECRETARIAT_FORMATIONS.items():
    if _code.startswith("BTS_"):
        _details.update({
            "audience": "Candidats souhaitant pr√©parer un dipl√¥me Bac+2 en alternance.",
            "prerequisites": "√ätre titulaire du baccalaur√©at ou d'un titre √©quivalent et faire valider sa candidature.",
            "certification": "Dipl√¥me d'√âtat de niveau 5 (Bac+2), sous r√©serve de r√©ussite √† l'examen.",
            "topics": ["Enseignements professionnels du BTS", "Culture g√©n√©rale et langues", "Mise en pratique en entreprise", "Pr√©paration aux √©preuves nationales"],
        })
    else:
        _details.update(_SECURITY_TRAINING_GUIDANCE.get(_code, {}))
    # Source unique des faits affich√©s dans le compte rendu. Les dates restent
    # toujours celles de la session choisie et ne figurent donc pas ici.
    _details.update({
        "label": _details.get("label") or PLAN_FORMATIONS.get(_code, _details.get("short", _code)),
        "source_url": SECRETARIAT_WEBSITE_URL,
        "assistant_url": SECRETARIAT_AI_URL,
        "dossier_url": SECRETARIAT_DOSSIER_URL,
        "planning_url": SECRETARIAT_PLANNING_URL,
    })

SECRETARIAT_FORMATIONS["APS"].update({
    "format": "Du lundi au vendredi",
    "location": "Puget-sur-Argens, entre Cannes et Saint-Tropez",
    "capacity": "Groupe limit√© √† 12 personnes",
    "certification": "TFP APS, puis demande de carte professionnelle CNAPS",
    "prerequisites": "Autorisation d‚Äôentr√©e en formation CNAPS obligatoire, avec accompagnement de notre √©quipe.",
})

PLAN_DATES = {
    "A3P": [
        "30 juin au 2 septembre 2026 ‚Äì examen le 3 septembre 2026",
        "8 juin au 4 ao√ªt 2026 ‚Äì examen le 5 ao√ªt 2026",
        "1 septembre au 27 octobre 2026 ‚Äì examen le 28 octobre 2026",
        "9 novembre 2026 au 19 janvier 2027 ‚Äì examen le 20 janvier 2027"
    ],
    "APS": [
        "23 mars au 27 avril 2026 ‚Äì examen le 28 avril 2026",
        "26 mai au 29 juin 2026 ‚Äì examen le 30 juin 2026",
        "8 juillet au 12 ao√ªt 2026 ‚Äì examen le 13 ao√ªt 2026",
        "7 septembre au 9 octobre 2026 ‚Äì examen le 12 octobre 2026",
        "3 novembre au 8 d√©cembre 2026 ‚Äì examen le 9 d√©cembre 2026"
    ],
    "DESP_INIT": [
        "19 janvier au 2 mars 2026 ‚Äì examen le 3 mars 2026",
        "9 mars au 21 avril 2026 ‚Äì examen le 22 avril 2026",
        "27 avril au 15 juin 2026 ‚Äì examen le 16 juin 2026"
    ]
}

DEFAULT_FORMATION_SESSIONS = {
    "cote_azur": {
        "APS": [
            {"label": "Du 29 avril au 9 juin 2026 - examen le 10 juin 2026", "badge": ""},
            {"label": "Du 26 mai au 29 juin 2026 - examen le 30 juin 2026", "badge": ""},
            {"label": "Du 8 juillet au 12 ao√ªt 2026 - examen le 13 ao√ªt 2026", "badge": ""},
            {"label": "Du 7 septembre au 9 octobre 2026 - examen le 12 octobre 2026", "badge": ""},
            {"label": "Du 3 novembre au 8 d√©cembre 2026 - examen le 9 d√©cembre 2026", "badge": ""}
        ],
        "A3P": [
            {"label": "Du 30 juin au 2 septembre 2026 - examen le 3 septembre 2026", "badge": ""},
            {"label": "Du 8 juin au 4 ao√ªt 2026 - examen le 5 ao√ªt 2026", "badge": ""},
            {"label": "Du 1er septembre au 27 octobre 2026 - examen le 28 octobre 2026", "badge": ""},
            {"label": "Du 9 novembre 2026 au 19 janvier 2027 - examen le 20 janvier 2027", "badge": ""}
        ],
        "DESP_INIT": [
            {"label": "Du 27 avril au 15 juin 2026 (pr√©sentiel du 2 au 15/06) - examen le 16 juin 2026", "badge": ""},
            {"label": "Du 22 juin au 10 ao√ªt 2026 (pr√©sentiel du 28/07 au 10/08) - examen le 11 ao√ªt 2026", "badge": ""},
            {"label": "Du 7 septembre au 23 octobre 2026 (pr√©sentiel du 12 au 23/10) - examen le 26 octobre 2026", "badge": ""},
            {"label": "Du 2 novembre au 21 d√©cembre 2026 (pr√©sentiel du 8 au 21/12) - examen le 22 d√©cembre 2026", "badge": ""}
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
            {"label": "21 octobre au 17 d√©cembre 2026", "badge": ""}
        ],
        "DESP_INIT": [
            {"label": "5 octobre au 19 novembre 2026 (A distance du 05/10 au 06/11/2026 ‚Äì Pr√©sentiel du 09/11 au 19/11/2026)", "badge": ""}
        ],
        "DESP_VAE": []
    },
    "paris": {
        "DESP_INIT": [
            {"label": "Du 7 septembre au 23 octobre 2026 (pr√©sentiel √† Paris) - examen le 26 octobre 2026", "badge": ""},
            {"label": "Du 2 novembre au 21 d√©cembre 2026 (pr√©sentiel √† Paris) - examen le 22 d√©cembre 2026", "badge": ""}
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


_FRENCH_MONTH_NUMBERS = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
}


def _session_date_range(label):
    """Extract the training range from the French label used by the admin."""
    normalized = (
        unicodedata.normalize("NFKD", str(label or ""))
        .encode("ascii", "ignore")
        .decode()
        .lower()
    )
    start = re.match(
        r"^\s*(?:du\s+)?(?P<day>\d{1,2})(?:er)?"
        r"(?:\s+(?P<month>[a-z]+))?(?:\s+(?P<year>\d{4}))?\s+au\s+",
        normalized,
    )
    if not start:
        return None, None

    start_day = int(start.group("day"))
    month_name = start.group("month")
    start_year = start.group("year")
    remainder = normalized[start.end():]
    end = re.match(
        r"(?P<day>\d{1,2})(?:er)?\s+(?P<month>[a-z]+)"
        r"(?:\s+(?P<year>\d{4}))?",
        remainder,
    )
    if not end:
        return None, None
    if not month_name and end:
        month_name = end.group("month")
    start_month = _FRENCH_MONTH_NUMBERS.get(month_name or "")
    end_month = _FRENCH_MONTH_NUMBERS.get(end.group("month") or "")
    end_year = end.group("year")
    if not start_year:
        start_year = end_year
        if start_year and start_month and end_month and start_month > end_month:
            start_year = str(int(start_year) - 1)
    if not end_year:
        end_year = start_year
        if end_year and start_month and end_month and end_month < start_month:
            end_year = str(int(end_year) + 1)
    if not start_month or not end_month or not start_year or not end_year:
        return None, None
    try:
        return (
            datetime.date(int(start_year), start_month, start_day),
            datetime.date(int(end_year), end_month, int(end.group("day"))),
        )
    except ValueError:
        return None, None


def _session_start_date(label):
    """Extract a session's first day from the French label used by the admin."""
    return _session_date_range(label)[0]


def get_upcoming_formation_sessions(data_store=None, today=None):
    """Return form sessions whose start date has not passed yet."""
    today = today or datetime.datetime.now(
        pytz.timezone("Europe/Paris")
    ).date()
    sessions = get_formation_sessions(data_store)
    for formations in sessions.values():
        for formation_code, rows in formations.items():
            formations[formation_code] = [
                row for row in rows
                if (start := _session_start_date(row.get("label"))) is None or start >= today
            ]
    return sessions


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

    mois_map = {
        "janvier": "01",
        "f√©vrier": "02",
        "fevrier": "02",
        "mars": "03",
        "avril": "04",
        "mai": "05",
        "juin": "06",
        "juillet": "07",
        "ao√ªt": "08",
        "aout": "08",
        "septembre": "09",
        "octobre": "10",
        "novembre": "11",
        "d√©cembre": "12",
        "decembre": "12"
    }

    def format_french_date(match):
        mois = mois_map.get(match.group(2).lower())
        if not mois:
            return ""
        return f"{match.group(3)}-{mois}-{match.group(1).zfill(2)}"

    match = re.search(r"examen le (\d{1,2}) ([a-z√†-√ø]+) (\d{4})", dates_txt, re.IGNORECASE)
    if match:
        return format_french_date(match)

    # Les devis personnalis√©s peuvent ne contenir que la p√©riode de formation
    # (par exemple ¬´ Du 9 novembre 2026 au 19 janvier 2027 ¬ª). Dans ce cas, la
    # fin de session est la meilleure date limite disponible pour l'√©ch√©ancier.
    range_numeric = re.search(
        r"\bau\s+(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b",
        dates_txt,
        re.IGNORECASE,
    )
    if range_numeric:
        return (
            f"{range_numeric.group(3)}-{range_numeric.group(2).zfill(2)}-"
            f"{range_numeric.group(1).zfill(2)}"
        )

    range_text = re.search(
        r"\bau\s+(\d{1,2})\s+([a-z√†-√ø]+)\s+(20\d{2})\b",
        dates_txt,
        re.IGNORECASE,
    )
    return format_french_date(range_text) if range_text else ""

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
            echeancier_message = "‚ö†Ô∏è Impossible de proposer un √©ch√©ancier : la date d‚Äôexamen est invalide."
    else:
        echeancier_message = "‚ö†Ô∏è Impossible de proposer un √©ch√©ancier : la date d‚Äôexamen est absente dans la session s√©lectionn√©e."

    echeances = build_echeances_mensuelles(
        reste=reste_sans_ft,
        date_devis=datetime.date.today(),
        date_examen=date_examen
    )

    if date_examen and not echeances and not echeancier_message:
        echeancier_message = "‚ö†Ô∏è Aucun √©ch√©ancier possible : la formation doit √™tre sold√©e avant l‚Äôexamen."

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


# ---------- USERS (charg√©s depuis les variables d'environnement) ----------
# Si tu as mis les variables dans Render / .env : on les lit ici.
_CRM_ACCOUNTS = (
    ("clement@integraleacademy.com", "Cl√©ment VAILLANT", "admin", "CRM_CLEMENT_PASSWORD"),
    ("cassandre@integraleacademy.com", "Cassandre MENARD", "user", "CRM_CASSANDRE_PASSWORD"),
    ("aurelie@integraleacademy.com", "Aur√©lie CHAUSSEZ", "user", "CRM_AURELIE_PASSWORD"),
    ("elsa@integraleacademy.com", "Elsa DUQUESNE", "user", "CRM_ELSA_PASSWORD"),
)

USERS = {
    email: {
        "email": email,
        "name": name,
        "first_name": name.split(maxsplit=1)[0],
        "role": role,
        "password_env": password_env,
    }
    for email, name, role, password_env in _CRM_ACCOUNTS
}


app = Flask(__name__, static_folder="static", static_url_path="/static")
from datetime import timedelta

_secret_key = os.environ.get("SECRET_KEY")
if not _secret_key and (os.environ.get("RENDER") or os.environ.get("FLASK_ENV") == "production"):
    raise RuntimeError("SECRET_KEY doit √™tre configur√©e en production")
app.secret_key = _secret_key or os.urandom(32)

app.config.update(
    SESSION_COOKIE_NAME="integrale_assistance_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,  # Render = https
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
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
    """Retourne True si le JSON contient des donn√©es utiles (demandes/archives/hebergements)."""
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
    - et le fichier legacy contient des donn√©es.
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
    R√©sout le fichier data.json en privil√©giant :
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

CRM_DEFAULT_CALL_NOTE_PRESETS = [
    "Doit cr√©er son Identit√© Num√©rique la Poste.",
    "Un nouveau RDV t√©l√©phonique a √©t√© fix√©",
    "Une relance a √©t√© programm√©e",
    "Est assez motiv√©",
    "N‚Äôest pas tr√®s motiv√©",
    "A d√©j√† la carte professionnelle",
    "N‚Äôa pas de carte professionnelle",
]
CRM_DEFAULT_RELANCE_MOTIF_PRESETS = [
    "Suivi VAE",
    "En attente RDV France Travail",
    "Doit revenir vers nous",
]
CRM_DEFAULT_MANUAL_NEXT_ACTION_PRESETS = [
    "Le candidat doit consulter le montant de son CPF",
    "Le candidat doit se rapprocher de son conseiller France Travail",
    "Le candidat doit cr√©er son Identit√© Num√©rique la Poste",
]

DEFAULT_DATA = {
    "demandes": [],
    "archives": [],
    "compteur_traitees": 0,
    "hebergements": [],
    "formation_sessions": {},
    "plans_simulation": {},
    "secretariat_demandes": [],
    "crm_contacts": [],
    # Journal additif des nouvelles sollicitations. Il est volontairement
    # distinct des fiches afin qu'une personne puisse avoir plusieurs demandes.
    "crm_inbound_requests": [],
    # Une ligne immuable par soumission Meta (l'identifiant externe est unique).
    "crm_meta_lead_submissions": [],
    "crm_email_templates": [],
    "crm_sms_templates": [],
    "crm_calendly_appointments": [],
    "crm_calendly": {},
    "crm_settings": {
        "calendar_default_view": "week",
        "calendar_workday_start": "08:00",
        "calendar_workday_end": "19:00",
        "notification_mentions": True,
        "notification_system": True,
        "direction_costs": {},
        "call_note_presets": CRM_DEFAULT_CALL_NOTE_PRESETS.copy(),
        "relance_motif_presets": CRM_DEFAULT_RELANCE_MOTIF_PRESETS.copy(),
        "manual_next_action_presets": (
            CRM_DEFAULT_MANUAL_NEXT_ACTION_PRESETS.copy()
        ),
    },
    "crm_ai_candidate_analyses": {},
    "crm_cnaps_scoring_snapshots": {},
}

# -------------------------------------------------------------------
# Utils
# -------------------------------------------------------------------
_DATA_CACHE_LOCK = threading.RLock()
_DATA_CACHE_SIGNATURE = None
_DATA_CACHE_PAYLOAD = None
_DATA_BACKUP_TIMES = {}
_DATA_BACKUP_INTERVAL_SECONDS = 300
_DATA_WRITE_LOCK = threading.RLock()

_CALLBACK_LIFECYCLE_FIELDS = (
    "callback_status",
    "callback_status_updated_at",
    "callback_processed_at",
    "callback_processed_by",
    "statut",
)
_CALLBACK_COMMENT_FIELDS = (
    "callback_comment",
    "callback_comment_updated_at",
    "callback_comment_updated_by",
)


def _normalize_data_payload(payload):
    if isinstance(payload, dict):
        normalized = dict(payload)
        for key, default in DEFAULT_DATA.items():
            if key not in normalized:
                normalized[key] = (
                    default.copy() if isinstance(default, (list, dict)) else default
                )
        return normalized
    if isinstance(payload, list):
        normalized = dict(DEFAULT_DATA)
        normalized["demandes"] = payload
        normalized["archives"] = []
        normalized["compteur_traitees"] = 0
        return normalized
    return None


def _data_file_signature():
    """Cheaply identify the active JSON snapshot, including test path swaps."""
    path = os.path.abspath(DATA_FILE)
    try:
        stat = os.stat(path)
        return path, stat.st_ino, stat.st_size, stat.st_mtime_ns
    except OSError:
        return path, None, 0, 0


def _load_data_snapshot():
    """Return a process-local immutable-by-convention snapshot.

    Render runs a single Gunicorn process for Socket.IO. Keeping the decoded
    JSON in that process avoids reparsing several megabytes for every CRM poll
    and every contact sheet. Callers that mutate data must keep using
    ``load_data()``, which returns a defensive copy.
    """
    global _DATA_CACHE_PAYLOAD, _DATA_CACHE_SIGNATURE
    signature = _data_file_signature()
    with _DATA_CACHE_LOCK:
        if (_DATA_CACHE_PAYLOAD is not None
                and _DATA_CACHE_SIGNATURE == signature):
            return _DATA_CACHE_PAYLOAD

        data = None
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    data = _normalize_data_payload(json.load(f))
            except (json.JSONDecodeError, OSError):
                # fichier corrompu/inaccessible : on tente d'abord une restauration auto via backup
                backup_path = f"{DATA_FILE}.bak"
                if os.path.exists(backup_path):
                    try:
                        with open(backup_path, "r", encoding="utf-8") as backup:
                            data = _normalize_data_payload(json.load(backup))
                    except (json.JSONDecodeError, OSError):
                        data = None

                # sinon on garde une copie puis on repart proprement
                if data is None:
                    try:
                        os.replace(DATA_FILE, f"{DATA_FILE}.corrupted")
                    except OSError:
                        pass
        if data is None:
            data = {
                key: value.copy() if isinstance(value, (list, dict)) else value
                for key, value in DEFAULT_DATA.items()
            }
        _DATA_CACHE_PAYLOAD = data
        _DATA_CACHE_SIGNATURE = _data_file_signature()
        return _DATA_CACHE_PAYLOAD


def load_data():
    """Return an isolated mutable copy for legacy read/modify/write callers."""
    return copy.deepcopy(_load_data_snapshot())


def _callback_lifecycle_revision(entry):
    """Return the newest durable timestamp carried by a callback request."""
    for key in (
        "callback_status_updated_at", "callback_processed_at", "created_at",
    ):
        raw_value = str(entry.get(key) or "").strip()
        if not raw_value:
            continue
        try:
            return datetime.datetime.fromisoformat(raw_value).timestamp()
        except ValueError:
            continue
    return 0.0


def _callback_comment_revision(entry):
    """Return the durable revision of a callback team's internal comment."""
    raw_value = str(entry.get("callback_comment_updated_at") or "").strip()
    if not raw_value:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(raw_value).timestamp()
    except ValueError:
        return 0.0


def _preserve_newer_callback_lifecycle(data, persisted):
    """Protect callback status/audit data from an older concurrent snapshot."""
    if not isinstance(data, dict) or not isinstance(persisted, dict):
        return
    persisted_entries = [
        entry
        for entry in (persisted.get("secretariat_demandes") or [])
        if isinstance(entry, dict) and entry.get("type") == "autre"
        and entry.get("id")
    ]
    if not persisted_entries:
        return
    outgoing_entries = data.get("secretariat_demandes")
    if outgoing_entries is None:
        outgoing_entries = []
        data["secretariat_demandes"] = outgoing_entries
    elif not isinstance(outgoing_entries, list):
        return
    outgoing_by_id = {
        str(entry.get("id") or ""): entry
        for entry in outgoing_entries
        if isinstance(entry, dict) and entry.get("type") == "autre"
        and entry.get("id")
    }
    protected_request_ids = set()
    for current in persisted_entries:
        request_id = str(current["id"])
        outgoing = outgoing_by_id.get(request_id)
        if outgoing is None:
            outgoing_entries.append(copy.deepcopy(current))
            outgoing_by_id[request_id] = outgoing_entries[-1]
            protected_request_ids.add(request_id)
            continue
        protected = False
        if (_callback_lifecycle_revision(current)
                > _callback_lifecycle_revision(outgoing)):
            for field in _CALLBACK_LIFECYCLE_FIELDS:
                if field in current:
                    outgoing[field] = copy.deepcopy(current[field])
                else:
                    outgoing.pop(field, None)
            protected = True
        if (_callback_comment_revision(current)
                > _callback_comment_revision(outgoing)):
            for field in _CALLBACK_COMMENT_FIELDS:
                if field in current:
                    outgoing[field] = copy.deepcopy(current[field])
                else:
                    outgoing.pop(field, None)
            protected = True
        if protected:
            protected_request_ids.add(request_id)

    if not protected_request_ids:
        return
    persisted_callback_activities = {}
    for contact in (persisted.get("crm_contacts") or []):
        if not isinstance(contact, dict):
            continue
        contact_id = str(contact.get("id") or "")
        for activity in (contact.get("activities") or []):
            if not isinstance(activity, dict):
                continue
            request_id = str(activity.get("callback_request_id") or "")
            if request_id in protected_request_ids:
                persisted_callback_activities.setdefault(contact_id, []).append(
                    copy.deepcopy(activity)
                )

    for contact in (data.get("crm_contacts") or []):
        if not isinstance(contact, dict):
            continue
        authoritative = persisted_callback_activities.get(
            str(contact.get("id") or ""),
        )
        if not authoritative:
            continue
        activities = [
            activity
            for activity in (contact.get("activities") or [])
            if not (
                isinstance(activity, dict)
                and str(activity.get("callback_request_id") or "")
                in protected_request_ids
            )
        ]
        contact["activities"] = sorted(
            [*authoritative, *activities],
            key=lambda activity: str(activity.get("date") or "")
            if isinstance(activity, dict) else "",
            reverse=True,
        )


def _save_data_unlocked(data):
    global _DATA_CACHE_PAYLOAD, _DATA_CACHE_SIGNATURE
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)

    # Conserver une sauvegarde de s√©curit√© sans recopier plusieurs m√©gaoctets √†
    # chaque frappe dans une fiche CRM. Une sauvegarde toutes les cinq minutes
    # prot√®ge les donn√©es tout en divisant fortement les √©critures sur le disque.
    data_path = os.path.abspath(DATA_FILE)
    now = time.monotonic()
    last_backup = _DATA_BACKUP_TIMES.get(data_path, 0)
    if (os.path.exists(DATA_FILE)
            and (not last_backup
                 or now - last_backup >= _DATA_BACKUP_INTERVAL_SECONDS)):
        backup_file = f"{DATA_FILE}.bak"
        try:
            # Une copie flux-√†-flux √©vite de conserver une deuxi√®me version du
            # gros fichier JSON en m√©moire pendant chaque √©criture CRM.
            shutil.copyfile(DATA_FILE, backup_file)
            _DATA_BACKUP_TIMES[data_path] = now
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
            # Le fichier est une base applicative, pas un document destin√© √†
            # √™tre √©dit√© √† la main. Le JSON compact r√©duit le volume, le temps
            # CPU et la dur√©e du fsync sur le disque persistant Render.
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_file, DATA_FILE)
        with _DATA_CACHE_LOCK:
            _DATA_CACHE_PAYLOAD = copy.deepcopy(data)
            _DATA_CACHE_SIGNATURE = _data_file_signature()
    finally:
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass


def save_data(data):
    """Persist one snapshot without losing newer callback lifecycle changes."""
    with _DATA_WRITE_LOCK:
        persisted = _load_data_snapshot()
        _preserve_newer_callback_lifecycle(data, persisted)
        return _save_data_unlocked(data)


def supprimer_fichier(filename):
    if not filename:
        return
    chemin = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(chemin):
        os.remove(chemin)


def supprimer_fichiers_demande(demande):
    """Supprime d√©finitivement les fichiers li√©s √† une demande.

    √Ä utiliser uniquement quand la demande est supprim√©e d√©finitivement
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
          <img src="https://integraleacademy.file.force.com/file-asset-public/Logo_Integrale_Academy_officielpdf?oid=00DJ9000000PT9F" alt="Int√©grale Academy" height="56" style="display:block;height:56px;width:auto;max-width:220px;">
        </td>
      </tr>
      <tr>
        <td align="center" style="padding:0 16px 10px 16px;margin:0; font-weight:700;font-size:16px;color:#111;">
          Int√©grale Academy
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
                  Merci de ne pas r√©pondre directement √† ce message automatique.
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
        print("‚ö†Ô∏è Impossible d‚Äôattacher le logo :", e)

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
            "name": os.getenv("BREVO_SENDER_NAME", "Int√©grale Academy"),
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
    if "cid:integrale-academy-logo" in html_body:
        logo_path = os.path.join(app.root_path, "static", "logo.png")
        if os.path.exists(logo_path):
            with open(logo_path, "rb") as logo_file:
                payload.setdefault("attachment", []).append({
                    "name": "integrale-academy-logo.png",
                    "content": base64.b64encode(logo_file.read()).decode("ascii"),
                    "contentId": "integrale-academy-logo",
                })

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
            print("‚úÖ Email envoy√© via le secours Brevo")
            return True
        print("‚ùå Erreur envoi email Brevo :", response.status_code, response.text)
    except Exception as e:
        print("‚ùå Erreur envoi email Brevo :", e)
    return False


def send_email_html(to_emails, subject, plain_text, html_body, attachments_paths=None):
    recipients = _email_recipients(to_emails)
    if not recipients:
        print("‚ùå Erreur envoi email : aucun destinataire")
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

    if "cid:integrale-academy-logo" in html_body:
        logo_path = os.path.join(app.root_path, "static", "logo.png")
        if os.path.exists(logo_path):
            with open(logo_path, "rb") as logo_file:
                logo = MIMEImage(logo_file.read())
            logo.add_header("Content-ID", "<integrale-academy-logo>")
            logo.add_header("Content-Disposition", "inline", filename="integrale-academy-logo.png")
            related.attach(logo)

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
        print("‚ùå Erreur envoi email SMTP :", e)
        return _send_email_brevo(
            recipients,
            subject,
            plain_text,
            html_body,
            attachments_paths=attachments_paths,
        )


CRM_EMAIL_ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024
CRM_EMAIL_ATTACHMENT_EXTENSIONS = frozenset({
    ".csv", ".doc", ".docx", ".gif", ".heic", ".jpeg", ".jpg", ".pdf",
    ".png", ".ppt", ".pptx", ".txt", ".webp", ".xls", ".xlsx",
})


def _crm_email_attachment_root():
    """Return the private, persistent folder used by CRM e-mail templates."""
    root = os.path.abspath(os.path.join(
        os.path.dirname(DATA_FILE), "uploads", "crm-email-attachments",
    ))
    os.makedirs(root, exist_ok=True)
    return root


def _crm_read_email_attachment(uploaded):
    """Validate an uploaded e-mail attachment and return safe in-memory data."""
    if not uploaded or not uploaded.filename:
        return None
    filename = secure_filename(uploaded.filename)
    extension = os.path.splitext(filename)[1].lower()
    if not filename or extension not in CRM_EMAIL_ATTACHMENT_EXTENSIONS:
        raise ValueError(
            "Format de pi√®ce jointe non accept√©. Utilisez une image, un PDF, "
            "un document Word, Excel ou PowerPoint, un CSV ou un fichier texte."
        )
    if len(filename.encode("utf-8")) > 240:
        raise ValueError("Le nom de la pi√®ce jointe est trop long.")
    content = uploaded.stream.read(CRM_EMAIL_ATTACHMENT_MAX_BYTES + 1)
    if not content:
        raise ValueError("La pi√®ce jointe est vide.")
    if len(content) > CRM_EMAIL_ATTACHMENT_MAX_BYTES:
        raise OverflowError("La pi√®ce jointe ne doit pas d√©passer 20 Mo.")
    return {
        "filename": filename,
        "content": content,
        "content_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
    }


def _crm_read_email_attachments(uploaded_files):
    """Validate manual files and enforce the 20 MB limit across the whole e-mail."""
    attachments = []
    total_bytes = 0
    for uploaded in uploaded_files or []:
        attachment = _crm_read_email_attachment(uploaded)
        if not attachment:
            continue
        total_bytes += len(attachment["content"])
        if total_bytes > CRM_EMAIL_ATTACHMENT_MAX_BYTES:
            raise OverflowError(
                "L‚Äôensemble des pi√®ces jointes ne doit pas d√©passer 20 Mo."
            )
        attachments.append(attachment)
    return attachments


def _crm_store_email_attachment(uploaded):
    attachment = _crm_read_email_attachment(uploaded)
    if not attachment:
        return None
    attachment_id = str(uuid.uuid4())
    attachment_dir = os.path.join(_crm_email_attachment_root(), attachment_id)
    os.makedirs(attachment_dir, exist_ok=False)
    path = os.path.join(attachment_dir, attachment["filename"])
    try:
        with open(path, "xb") as attachment_file:
            attachment_file.write(attachment["content"])
    except Exception:
        shutil.rmtree(attachment_dir, ignore_errors=True)
        raise
    return {
        "id": attachment_id,
        "nom": attachment["filename"],
        "taille": len(attachment["content"]),
        "type": attachment["content_type"],
    }


def _crm_email_attachment_path(metadata):
    """Resolve stored metadata without allowing a path to escape its private root."""
    if not isinstance(metadata, dict):
        return None
    try:
        attachment_id = str(uuid.UUID(str(metadata.get("id") or "")))
    except (ValueError, TypeError, AttributeError):
        return None
    filename = str(metadata.get("nom") or "")
    if not filename or secure_filename(filename) != filename:
        return None
    root = _crm_email_attachment_root()
    path = os.path.abspath(os.path.join(root, attachment_id, filename))
    if os.path.commonpath((root, path)) != root or not os.path.isfile(path):
        return None
    return path


def _crm_delete_email_attachment(metadata):
    if not isinstance(metadata, dict):
        return
    try:
        attachment_id = str(uuid.UUID(str(metadata.get("id") or "")))
    except (ValueError, TypeError, AttributeError):
        return
    root = _crm_email_attachment_root()
    attachment_dir = os.path.abspath(os.path.join(root, attachment_id))
    if os.path.commonpath((root, attachment_dir)) == root:
        shutil.rmtree(attachment_dir, ignore_errors=True)


def _crm_send_email_html(to_emails, subject, plain_text, html_body,
                         template=None, attachments_paths=None):
    """Send an e-mail with explicit files or the attachment saved on its template."""
    paths = attachments_paths
    if paths is None and isinstance(template, dict):
        stored_path = _crm_email_attachment_path(template.get("piece_jointe"))
        paths = [stored_path] if stored_path else []
    paths = [path for path in (paths or []) if path]
    # Keep the historical four-argument call when there is no attachment. It
    # remains compatible with existing providers, tests and local overrides.
    if not paths:
        return send_email_html(to_emails, subject, plain_text, html_body)
    return send_email_html(
        to_emails, subject, plain_text, html_body, attachments_paths=paths,
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


def build_training_information_sms_text(formation_code: str, *, include_phone_booking=True) -> str:
    context = _formation_sms_context(formation_code)
    booking_text = (
        "Je vous invite √† r√©server un RDV t√©l√©phonique avec un membre de notre √©quipe qui pourra vous renseigner "
        f"et vous pr√©senter en d√©tails notre formation : {context['calendly']}\n"
    ) if include_phone_booking else ""
    return (
        "Bonjour, \n"
        f"Je fais suite √† votre demande d‚Äôinformations concernant notre formation {context['formation_name']}. "
        "Je viens de vous adresser par mail toutes les informations utiles (pensez √† v√©rifier vos courriers ind√©sirables). \n"
        + booking_text
        + "Vous pouvez √©galement nous contacter par t√©l√©phone du lundi au vendredi de 09h00 √† 17h00 au 04 22 47 07 68. \n"
        "Je vous souhaite une bonne journ√©e, \n"
        "Cl√©ment VAILLANT - Directeur Int√©grale Academy"
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


_GSM_7_BASIC_CHARACTERS = frozenset(
    "@\u00a3$\u00a5\u00e8\u00e9\u00f9\u00ec\u00f2\u00c7\n\u00d8\u00f8\r\u00c5\u00e5"
    "\u0394_\u03a6\u0393\u039b\u03a9\u03a0\u03a8\u03a3\u0398\u039e"
    "\u00c6\u00e6\u00df\u00c9 !\"#\u00a4%&'()*+,-./"
    "0123456789:;<=>?\u00a1ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "\u00c4\u00d6\u00d1\u00dc\u00a7\u00bfabcdefghijklmnopqrstuvwxyz\u00e4\u00f6\u00f1\u00fc\u00e0"
)
_GSM_7_EXTENSION_CHARACTERS = frozenset("\f^{}\\[~]|\u20ac")


def _sms_requires_unicode(body: str) -> bool:
    """Return whether Brevo must encode the SMS as Unicode rather than GSM-7."""
    gsm_characters = _GSM_7_BASIC_CHARACTERS | _GSM_7_EXTENSION_CHARACTERS
    return any(character not in gsm_characters for character in str(body or ""))


def send_sms(to_phone: str, body: str) -> bool:
    recipient = _normaliser_telephone_sms(to_phone)
    if not recipient:
        print("‚ùå Erreur envoi SMS Brevo : num√©ro invalide")
        return False

    api_key = os.getenv("BREVO_API_KEY")
    sender = os.getenv("BREVO_SMS_SENDER", "FORMATION")

    if not api_key or not sender:
        print("‚ùå Erreur envoi SMS Brevo : configuration incompl√®te")
        return False

    payload = {
        "sender": sender,
        "recipient": recipient,
        "content": body,
        "type": "transactional",
        "tag": "demande-informations-formations",
        # Brevo otherwise replaces characters outside GSM-7 (notably emojis)
        # with question marks instead of switching encodings automatically.
        "unicodeEnabled": _sms_requires_unicode(body),
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
        print("‚ùå Erreur envoi SMS Brevo :", e)
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
        "Je fais suite √† votre demande de renseignements concernant notre VAE Dirigeant d‚ÄôEntreprise de S√©curit√© Priv√©e (RNCP40385).\n"
        "Dossier de pr√©sentation : https://www.integraleacademy.com/dossiersfc\n"
        f"T√©l√©charger votre devis d√©taill√© : {devis_url}\n"
        "D√©marrer votre VAE : https://gestionstagiaires-r5no.onrender.com/vae-desp\n"
        "Planifier un rendez-vous : https://calendly.com/integraleacademy/dirigeant\n\n"
        "Je reste √† votre disposition pour tout renseignement compl√©mentaire.\n\n"
        "Cl√©ment VAILLANT\nDirecteur ‚Äì Int√©grale Academy"
    )


def _is_vae_desp_formation(formation_code: str) -> bool:
    return str(formation_code or "").strip() == "DESP_VAE"



def _eur_display(amount: int) -> str:
    return f"{amount:,.0f} ‚Ç¨ TTC".replace(",", " ")


ABANDONED_TRAINING_EMAIL_CONFIG = {
    "A3P": {
        "short": "Formation A3P Bodyguard",
        "project_title": "de formation A3P Bodyguard",
        "formation_name": "Agent de Protection Physique des Personnes (A3P ‚Äì Bodyguard)",
        "about_title": "La formation A3P en quelques mots",
        "about_text": "La formation <strong>A3P ‚Äì Agent de Protection Physique des Personnes</strong> permet de se former aux m√©tiers de la protection rapproch√©e, dans le respect de la r√©glementation fran√ßaise.",
        "about_extra": "Elle pr√©pare √† l‚Äôobtention de la carte professionnelle d√©livr√©e par le <strong>CNAPS</strong> pour exercer l√©galement dans le domaine de la protection physique des personnes.",
        "learning": [
            "Protection rapproch√©e et accompagnement de personnes expos√©es",
            "Pr√©paration, anticipation et s√©curisation des d√©placements",
            "Gestion des situations sensibles et des comportements √† risque",
            "Cadre l√©gal de l‚Äôactivit√© de s√©curit√© priv√©e",
            "Posture professionnelle, discr√©tion et communication",
            "Pr√©paration √† l‚Äôexamen et √† la demande de carte professionnelle CNAPS",
        ],
        "highlights": [
            ("‚úÖ", "Titre reconnu par l‚Äô√âtat", "RNCP38002 ‚Äî niveau 4"),
            ("üëÆ", "Carte professionnelle CNAPS", "Protection Physique des Personnes"),
            ("üè®", "H√©bergement possible", "Solution sur place selon disponibilit√©s"),
        ],
        "calendly": "https://calendly.com/integraleacademy/apr",
    },
    "APS": {
        "short": "Formation APS",
        "project_title": "de formation APS",
        "formation_name": "Agent de Pr√©vention et de S√©curit√© (APS)",
        "about_title": "La formation APS en quelques mots",
        "about_text": "La formation <strong>APS ‚Äì Agent de Pr√©vention et de S√©curit√©</strong> pr√©pare aux missions de surveillance, de pr√©vention et de protection des biens et des personnes.",
        "about_extra": "Elle permet de se pr√©parer √† l‚Äôexercice r√©glement√© d‚Äôagent de s√©curit√© priv√©e et aux d√©marches li√©es √† la carte professionnelle CNAPS.",
        "learning": [
            "Surveillance g√©n√©rale et pr√©vention des actes de malveillance",
            "Accueil, contr√¥le d‚Äôacc√®s et filtrage",
            "Gestion des conflits et des situations sensibles",
            "Bases juridiques de la s√©curit√© priv√©e",
            "Pr√©vention incendie et secours √† personne",
            "Pr√©paration √† l‚Äôexamen APS et √† la carte professionnelle CNAPS",
        ],
        "highlights": [
            ("‚úÖ", "Formation r√©glement√©e", "Acc√®s au m√©tier d‚Äôagent de s√©curit√©"),
            ("üëÆ", "D√©marches CNAPS", "Accompagnement possible"),
            ("üìç", "Sessions r√©guli√®res", "Selon centres et disponibilit√©s"),
        ],
        "calendly": "https://calendly.com/integraleacademy/aps",
    },
    "SSIAP": {
        "short": "Formation SSIAP 1",
        "project_title": "de formation SSIAP 1",
        "formation_name": "SSIAP 1 ‚Äì Agent de Service de S√©curit√© Incendie et d‚ÄôAssistance √† Personnes",
        "about_title": "La formation SSIAP 1 en quelques mots",
        "about_text": "La formation <strong>SSIAP 1</strong> pr√©pare au m√©tier d‚Äôagent de s√©curit√© incendie dans les √©tablissements recevant du public et les immeubles de grande hauteur.",
        "about_extra": "Elle pr√©pare au dipl√¥me SSIAP 1 et aux missions de pr√©vention incendie, d‚Äôalerte, d‚Äô√©vacuation et d‚Äôassistance √† personnes.",
        "learning": [
            "Pr√©vention des risques d‚Äôincendie",
            "Sensibilisation des occupants aux consignes de s√©curit√©",
            "Intervention face √† un d√©but d‚Äôincendie",
            "Alerte et accueil des secours",
            "√âvacuation du public et assistance aux personnes",
            "Pr√©paration aux √©preuves du dipl√¥me SSIAP 1",
        ],
        "highlights": [
            ("üî•", "Dipl√¥me SSIAP 1", "S√©curit√© incendie en ERP et IGH"),
            ("‚úÖ", "Programme r√©glement√©", "67 heures hors examen"),
            ("üìç", "Formation en pr√©sentiel", "Puget-sur-Argens"),
        ],
        "calendly": "https://calendly.com/integraleacademy/ssiap1",
    },
    "VTC": {
        "short": "Formation Chauffeur VTC",
        "project_title": "de formation Chauffeur VTC",
        "formation_name": "Chauffeur de transport avec chauffeur (VTC)",
        "about_title": "La formation VTC en quelques mots",
        "about_text": "La formation <strong>VTC</strong> permet de pr√©parer votre projet de chauffeur professionnel avec une organisation flexible combinant th√©orie √† distance et pratique.",
        "about_extra": "Elle vous accompagne sur les comp√©tences attendues √† l‚Äôexamen et sur les d√©marches n√©cessaires pour lancer votre activit√©.",
        "learning": [
            "R√©glementation du transport public particulier de personnes",
            "S√©curit√© routi√®re, relation client et qualit√© de service",
            "Gestion, d√©veloppement commercial et pr√©paration d‚Äôactivit√©",
            "Pr√©paration √† l‚Äôexamen th√©orique VTC",
            "Mise en pratique de la conduite professionnelle",
            "Organisation des d√©marches administratives VTC",
        ],
        "highlights": [
            ("üíª", "Th√©orie en ligne", "Accessible √† distance"),
            ("üöó", "Pratique encadr√©e", "Pr√©paration terrain"),
            ("üìÑ", "Dossier complet", "Programme et d√©marches"),
        ],
        "calendly": "https://calendly.com/integraleacademy/chauffeurvtc",
    },
    "DESP_INIT": {
        "short": "Formation DESP initial",
        "project_title": "de formation DESP initial",
        "formation_name": "Dirigeant d‚ÄôEntreprise de S√©curit√© Priv√©e (DESP ‚Äì initial)",
        "about_title": "La formation DESP initial en quelques mots",
        "about_text": "La formation <strong>DESP</strong> pr√©pare les futurs dirigeants d‚Äôentreprise de s√©curit√© priv√©e √† cr√©er, piloter et g√©rer leur structure conform√©ment √† la r√©glementation.",
        "about_extra": "Elle pr√©pare aux comp√©tences n√©cessaires pour solliciter l‚Äôagr√©ment dirigeant aupr√®s du CNAPS.",
        "learning": [
            "Cadre juridique de la s√©curit√© priv√©e et obligations du dirigeant",
            "Cr√©ation, gestion et pilotage d‚Äôune entreprise de s√©curit√©",
            "Gestion administrative, commerciale et financi√®re",
            "Management des √©quipes et organisation op√©rationnelle",
            "D√©ontologie, contr√¥le interne et conformit√© CNAPS",
            "Pr√©paration √† l‚Äôexamen et √† l‚Äôagr√©ment dirigeant",
        ],
        "highlights": [
            ("‚úÖ", "Titre reconnu par l‚Äô√âtat", "RNCP40385 ‚Äî niveau 5"),
            ("üè¢", "Projet dirigeant", "Cr√©er ou g√©rer une soci√©t√©"),
            ("üíª", "E-learning + pr√©sentiel", "Organisation mixte"),
        ],
        "calendly": "https://calendly.com/integraleacademy/dirigeant",
    },
    "DESP_VAE": {
        "short": "VAE DESP",
        "project_title": "de VAE DESP",
        "formation_name": "VAE Dirigeant d‚ÄôEntreprise de S√©curit√© Priv√©e (DESP)",
        "about_title": "La VAE DESP en quelques mots",
        "about_text": "La <strong>VAE DESP</strong> permet de valoriser votre exp√©rience professionnelle pour viser la certification Dirigeant d‚ÄôEntreprise de S√©curit√© Priv√©e.",
        "about_extra": "Notre √©quipe peut vous accompagner dans la constitution du dossier, la formalisation de vos comp√©tences et la pr√©paration du passage devant le jury.",
        "learning": [
            "Analyse de votre exp√©rience et de sa coh√©rence avec le r√©f√©rentiel",
            "Constitution et structuration du dossier VAE",
            "Mise en valeur des comp√©tences de dirigeant s√©curit√© priv√©e",
            "Pr√©paration √† l‚Äôentretien avec le jury",
            "Compr√©hension des attendus r√©glementaires et CNAPS",
            "Accompagnement m√©thodologique jusqu‚Äôau d√©p√¥t du dossier",
        ],
        "highlights": [
            ("‚úÖ", "Certification vis√©e", "RNCP40385 ‚Äî niveau 5"),
            ("üìù", "Accompagnement dossier", "M√©thode et structuration"),
            ("üìû", "Suivi personnalis√©", "√âchange avec notre √©quipe"),
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
        "about_text": f"Cette formation <strong>{html.escape(default_label)}</strong> r√©pond √† un projet professionnel concret et peut faire l‚Äôobjet d‚Äôun accompagnement par notre √©quipe.",
        "about_extra": "Nous pouvons vous expliquer les objectifs, les pr√©requis, les dates, le financement et les √©tapes d‚Äôinscription lors d‚Äôun √©change t√©l√©phonique.",
        "learning": [
            "Objectifs et organisation de la formation",
            "Pr√©requis et conditions d‚Äôacc√®s",
            "Dates, lieux et modalit√©s pratiques",
            "Solutions de financement possibles",
            "√âtapes d‚Äôinscription et documents utiles",
            "R√©ponses personnalis√©es √† vos questions",
        ],
        "highlights": [
            ("üìÑ", "Dossier complet", "Programme et informations pratiques"),
            ("üí∂", "Financement", "CPF, France Travail ou personnel"),
            ("üìû", "Accompagnement", "√âchange avec notre √©quipe"),
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
        f"Vous aviez commenc√© une demande d‚Äôinformations concernant notre formation {config['formation_name']}, mais votre demande n‚Äôa pas √©t√© finalis√©e.\n\n"
        "Aucun souci : je vous transmets les informations principales et vous propose un √©change t√©l√©phonique si vous souhaitez avancer plus facilement.\n\n"
        f"Tarif : {price}\n"
        "Dossier de pr√©sentation : https://www.integraleacademy.com/dossiersfc\n"
        f"R√©server un rendez-vous : {config['calendly']}\n"
        "Identit√© Num√©rique La Poste : https://lidentitenumerique.laposte.fr\n\n"
        "Vous pouvez r√©pondre directement √† ce mail.\n\n"
        "Cl√©ment VAILLANT\nDirecteur Int√©grale Group\n"
        "04 22 47 07 68\n"
    )



def _abandoned_training_email_content(prenom: str, formation_code: str, devis_url: str = ""):
    if _is_vae_desp_formation(formation_code):
        return (
            "üìù VAE ‚Äì Dirigeant d‚ÄôEntreprise de S√©curit√© Priv√©e (RNCP40385)",
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


def _crm_create_or_match_abandoned_form_contact(data, record, fields, now_str):
    """Create one internal CRM lead for an eligible abandoned public form."""
    external_id = str(record.get("form_id") or record.get("id") or "").strip()
    if not external_id or not _has_required_abandoned_form_contact_fields(fields):
        return None

    existing_request = next((
        item for item in data.get("crm_inbound_requests", [])
        if item.get("source") == ABANDONED_DEMANDE_SOURCE
        and item.get("external_id") == external_id
    ), None)
    formation_key = str(fields.get("formation") or "").strip()
    formation = {
        "DESP_INIT": "DESP", "DESP_VAE": "DESP", "SSIAP": "SSIAP 1",
        "VTC": "Chauffeur VTC",
    }.get(formation_key, formation_key)
    centre_key = str(fields.get("centre") or "").strip()
    lieu = {
        "paris": "Paris", "cote_azur": "C√¥te d‚ÄôAzur",
        "auvergne": "Auvergne", "aurillac": "Auvergne",
    }.get(centre_key, centre_key)
    tracking = _crm_google_ads_tracking_fields(fields)
    identifier_type, identifier = _crm_information_request_google_ads_identifier(fields)
    now = _crm_now()
    contact = {
        "id": str(uuid.uuid4()),
        "prenom": _crm_format_first_name(fields.get("prenom")),
        "nom": _crm_format_last_name(fields.get("nom")),
        "telephone": str(fields.get("telephone") or "").strip(),
        "mail": str(fields.get("mail") or "").strip(),
        "formation": formation, "lieu": lieu, "statut": "Nouveaux",
        "dates_formation": str(fields.get("dates") or "").strip(),
        "cpf": str(fields.get("cpf_consulte") or "").strip(),
        "cpf_montant": normalize_cpf_amount(fields.get("cpf_montant")),
        "carte_pro": str(fields.get("cnaps_ok") or "").strip(),
        "titre_sejour": str(fields.get("titre_sejour") or "").strip(),
        "garde_vue": str(fields.get("garde_vue") or "").strip(),
        "antecedents": str(fields.get("garde_vue") or "").strip(),
        "desp_type": "VAE" if formation_key == "DESP_VAE" else (
            "INITIAL" if formation_key == "DESP_INIT" else ""
        ),
        "identite_creation": str(fields.get("identite_numerique") or "").strip(),
        "identite_ok": "",
        "financement_ft": str(fields.get("france_travail") or "").strip(),
        "statut_demande_financement_ft": "", "montant_accorde_ft": "",
        "financement_perso_possible": str(fields.get("ft_refus_ok") or "").strip(),
        "refus_ft_perso": str(fields.get("ft_refus_ok") or "").strip(),
        "reste_a_charge_perso": str(fields.get("financement_perso") or "").strip(),
        "inscrit_ft": "", "commentaires": "", "relance_date": "",
        "origine": ABANDONED_FORM_LABEL,
        "gclid": tracking["gclid"], "wbraid": tracking["wbraid"],
        "gbraid": tracking["gbraid"],
        "google_ads_identifier": identifier,
        "google_ads_identifier_type": identifier_type,
        "created_at": now, "updated_at": now, "activities": [],
        "source": ABANDONED_DEMANDE_SOURCE,
        "source_form_id": external_id,
        "formulaire": dict(fields),
        "source_history": [{
            "origin": ABANDONED_FORM_LABEL,
            "source": ABANDONED_DEMANDE_SOURCE,
            "external_id": external_id,
            "date": now,
        }],
    }
    detail = " ¬∑ ".join(filter(None, [
        f"Formation : {formation}" if formation else "",
        f"Lieu : {lieu}" if lieu else "",
        f"Session : {contact['dates_formation']}" if contact["dates_formation"] else "",
    ])) or "Coordonn√©es compl√®tes enregistr√©es."
    _crm_activity(
        contact, "creation", "Formulaire abandonn√© d√©tect√©", detail,
    )
    matched, inbound, created = find_or_create_crm_contact(
        data, fields, ABANDONED_DEMANDE_SOURCE,
        proposed_contact=contact, external_id=external_id,
    )
    if matched and not created and not existing_request:
        _crm_activity(
            matched, "inbound_request", "Formulaire abandonn√© d√©tect√©", detail,
        )
        _crm_record_origin(
            matched,
            ABANDONED_FORM_LABEL,
            source=ABANDONED_DEMANDE_SOURCE,
            external_id=external_id,
            date=now,
        )
    if matched:
        record["crm_abandoned_contact_id"] = matched.get("id")
        record["crm_abandoned_created_at"] = (
            record.get("crm_abandoned_created_at") or now_str
        )
    elif inbound:
        record["crm_abandoned_review_id"] = inbound.get("id")
        record["crm_abandoned_review_at"] = (
            record.get("crm_abandoned_review_at") or now_str
        )
    return matched


def _declencher_relance_formulaire_abandonne(
    data: dict, record: dict, fields: dict, now_str: str,
) -> dict:
    result = {"crm": False, "salesforce": False, "mail": False, "sms": False, "skipped": False}
    if not _has_required_abandoned_form_contact_fields(fields):
        result["skipped"] = True
        record["abandoned_automation_skipped_at"] = now_str
        record["abandoned_automation_skip_reason"] = "Coordonn√©es incompl√®tes"
        return result

    try:
        result["crm"] = bool(
            _crm_create_or_match_abandoned_form_contact(data, record, fields, now_str)
        )
        record.pop("crm_abandoned_error", None)
    except Exception as exc:
        record["crm_abandoned_error"] = f"{now_str} ¬∑ {exc}"

    if not record.get("salesforce_abandoned_sent_at"):
        try:
            creer_piste_salesforce(_abandoned_training_form_salesforce_payload(fields))
            record["salesforce_abandoned_sent_at"] = now_str
            record["salesforce_abandoned_status"] = ABANDONED_FORM_LABEL
            result["salesforce"] = True
        except Exception as exc:
            record["salesforce_abandoned_error"] = f"{now_str} ¬∑ {exc}"

    if not record.get("abandoned_mail_sent_at"):
        try:
            result["mail"] = envoyer_mail_formulaire_formation_abandonne(record, fields)
        except Exception as exc:
            record["abandoned_mail_error"] = f"{now_str} ¬∑ {exc}"

    if not record.get("abandoned_sms_sent_at"):
        try:
            result["sms"] = envoyer_sms_formulaire_formation_abandonne(record, fields)
        except Exception as exc:
            record["abandoned_sms_error"] = f"{now_str} ¬∑ {exc}"

    record["auto_abandoned_sent_at"] = record.get("auto_abandoned_sent_at") or now_str
    record["abandoned_automation_status"] = "Relance automatique d√©clench√©e"
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
        (record.get("crm_abandoned_contact_id") or record.get("crm_abandoned_review_id"))
        and record.get("salesforce_abandoned_sent_at")
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
            "Formulaire abandonn√© d√©tect√© automatiquement apr√®s inactivit√©"
        )
        _declencher_relance_formulaire_abandonne(data, draft, draft.get("fields") or {}, now_str)
        changed = True

    return changed

def _format_selected_session_date(dates_txt: str) -> str:
    if not dates_txt:
        return ""
    return dates_txt.strip().replace(" - examen le ", " ‚Äî examen le ")

def _extract_exam_label_from_dates_txt(dates_txt: str) -> str:
    if not dates_txt:
        return ""
    match = re.search(r"examen le\s+(.+)$", dates_txt.strip(), re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).strip(" .)")


def _format_upcoming_sessions_for_email(
    centre_code: str, formation_code: str, data_store=None, today=None,
) -> str:
    sessions = get_upcoming_formation_sessions(data_store, today=today)
    rows = sessions.get(_normalize_centre_code(centre_code), {}).get(formation_code, [])
    labels = [
        (row.get("label") or "").strip()
        for row in rows
        if isinstance(row, dict) and (row.get("label") or "").strip()
    ]
    if not labels:
        return '<p style="margin:0 0 6px 0;">üìÖ <strong>Dates √† venir prochainement</strong></p>'
    items = "".join(
        f'<li style="margin:0 0 6px 0;"><strong>{label.replace(" - examen le ", " ‚Äî examen le ")}</strong></li>'
        for label in labels
    )
    return f'<ul style="margin:0 0 8px 18px; padding:0;">{items}</ul>'


def _normalize_centre_code(centre_code: str) -> str:
    normalized = str(centre_code or "").strip().lower()
    aliases = {
        "cote_azur": "cote_azur",
        "cote d'azur": "cote_azur",
        "c√¥te d'azur": "cote_azur",
        "paca": "cote_azur",
        "nice": "cote_azur",
        "auvergne": "auvergne",
        "clermont": "auvergne",
        "clermont-ferrand": "auvergne",
        "paris": "paris",
        "idf": "paris",
        "ile-de-france": "paris",
        "√Æle-de-france": "paris",
    }
    return aliases.get(normalized, "cote_azur")


def _centre_label_and_address(centre_code: str):
    centre_code = _normalize_centre_code(centre_code)
    centres = {
        "cote_azur": (
            "Int√©grale Academy C√¥te d‚ÄôAzur",
            "54 chemin du Carreou ‚Äî 83480 PUGET SUR ARGENS (Var)",
        ),
        "auvergne": (
            "Int√©grale Academy Terres d‚ÄôAuvergne",
            "650 route d'Aumont ‚Äî 15130 Arpajon-sur-C√®re",
        ),
        "paris": (
            "Int√©grale Academy Paris",
            "142 rue de Rivoli ‚Äî 75001 PARIS",
        ),
    }
    return centres.get(
        centre_code,
        (
            "Int√©grale Academy C√¥te d‚ÄôAzur",
            "54 chemin du Carreou ‚Äî 83480 PUGET SUR ARGENS (Var)",
        ),
    )


def _centre_legal_block(centre_code: str) -> str:
    centre_code = _normalize_centre_code(centre_code)
    if centre_code == "paris":
        return (
            "SASU Int√©grale S√©curit√© Formations\n"
            "142 rue de Rivoli\n"
            "75001 PARIS\n"
            "Immatricul√©e au Registre des commerces et des soci√©t√©s RCS 840899884\n"
            "NDA n¬∞93830600283\n"
            "Autorisation CNAPS FOR-083-2027-02-08-20200755135\n"
            "Certification Nationale Qualit√© QUALIOPI n¬∞03169 d√©livr√©e par SGS en date du 21/10/2024 - "
            "La certification qualit√© a √©t√© d√©livr√©e au titre de la ou des cat√©gories d‚Äôactions suivantes : "
            "actions de formation, actions de formation en apprentissage."
        )

    return (
        "SASU Int√©grale S√©curit√© Formations\n"
        "Si√®ge social : 54 chemin du Carreou\n"
        "83480 PUGET SUR ARGENS\n"
        "Immatricul√©e au Registre du commerce et des soci√©t√©s de Fr√©jus RCS 840899884\n"
        "NDA n¬∞93830600283\n"
        "Autorisation CNAPS FOR-083-2027-02-08-20200755135\n"
        "Certification Nationale Qualit√© QUALIOPI n¬∞03169 d√©livr√©e par SGS en date du 21/10/2024 - "
        "La certification qualit√© a √©t√© d√©livr√©e au titre de la ou des cat√©gories d‚Äôactions suivantes : "
        "actions de formation, actions de formation en apprentissage."
    )


def build_a3p_email_html(
    prenom: str, dates_txt: str, centre_code: str, devis_url: str, data_store=None,
    *, include_phone_booking=True, prominent_phone_booking=False,
):
    centre_label, _ = _centre_label_and_address(centre_code)
    centre_display = centre_label.replace("Int√©grale Academy ", "")
    session_html = _format_upcoming_sessions_for_email(centre_code, "A3P", data_store)
    devis_button_html = ""
    if devis_url:
        devis_button_html = (
            '<p style="margin:0; text-align:center;">'
            f'<a href="{devis_url}" style="display:inline-block; background:#F4C45A; color:#111827; '
            'text-decoration:none; padding:13px 22px; border-radius:10px; font-weight:bold;">'
            "T√©l√©charger mon devis d√©taill√©</a></p>"
        )
    return _render_email_template(
        "a3p.html", prenom=prenom, centre_display=centre_display,
        session_html=session_html, devis_button_html=devis_button_html,
        phone_booking_html=(_render_email_template("a3p_phone_booking.html")
                            if include_phone_booking and not prominent_phone_booking else ""),
        prominent_phone_booking_html=(
            _render_email_template("a3p_meta_phone_booking.html")
            if include_phone_booking and prominent_phone_booking else ""
        ),
    )


def _a3p_information_email_content(
    prenom: str, dates_txt: str, centre_code: str, devis_url: str, data_store=None,
    *, include_phone_booking=True, prominent_phone_booking=False,
):
    """Return the A3P message shared by the public form and META leads."""
    session_date = _format_selected_session_date(dates_txt)
    centre_label, centre_address = _centre_label_and_address(centre_code)
    booking_text = (
        "Planifier un rendez-vous : https://calendly.com/integraleacademy/apr\n\n"
    ) if include_phone_booking else ""
    prominent_booking_text = (
        "\nVOTRE RENDEZ-VOUS T√âL√âPHONIQUE\n"
        "Nous vous invitons √† prendre un rendez-vous t√©l√©phonique avec un membre de notre √©quipe. "
        "Il vous pr√©sentera en d√©tail notre formation et r√©pondra √† toutes vos questions.\n"
        "R√©server mon rendez-vous t√©l√©phonique : https://calendly.com/integraleacademy/apr\n"
        "Choisissez le cr√©neau qui vous convient.\n\n"
    ) if include_phone_booking and prominent_phone_booking else ""
    plain = (
        f"Bonjour {prenom},\n\n"
        "Je fais suite √† votre demande de renseignements concernant notre formation Agent de Protection Physique des Personnes (A3P ‚Äì Bodyguard), titre reconnu par l‚Äô√âtat (RNCP38002 ‚Äì niveau 4).\n"
        + prominent_booking_text
        + "Cette formation permet d‚Äôacqu√©rir toutes les comp√©tences n√©cessaires pour intervenir en tant que garde du corps, dans le respect strict de la r√©glementation fran√ßaise. Elle pr√©pare √©galement √† l‚Äôobtention de la carte professionnelle Agent de protection physique des personnes d√©livr√©e par le CNAPS (Minist√®re de l'int√©rieur).\n\n"
        "Dur√©e et organisation : 328 heures de formation.\n"
        + (f"Session : {session_date}\n" if session_date else "")
        + f"Lieu : {centre_label} ‚Äî {centre_address}\n\n"
        "Tarif : 4200 ‚Ç¨ TTC (financement possible via CPF).\n"
        "Identit√© Num√©rique La Poste requise pour le CPF.\n"
        "H√©bergement possible : 300 ‚Ç¨ TTC pour toute la formation.\n\n"
        "Dossier de pr√©sentation : https://www.integraleacademy.com/dossiersfc\n"
        + (booking_text if not prominent_phone_booking else "")
        + "Je reste √† votre disposition pour toute information compl√©mentaire.\n\n"
        "Cl√©ment VAILLANT\nDirecteur ‚Äì Int√©grale Academy"
    )
    return (
        "üëÆ‚Äç‚ôÇÔ∏è Formation Agent de Protection Physique des Personnes (A3P)",
        plain,
        build_a3p_email_html(
            prenom, dates_txt, centre_code, devis_url, data_store,
            include_phone_booking=include_phone_booking,
            prominent_phone_booking=prominent_phone_booking,
        ),
    )





def build_aps_email_html(prenom: str, dates_txt: str, centre_code: str, devis_url: str):
    session_date = _format_selected_session_date(dates_txt)
    centre_label, centre_address = _centre_label_and_address(centre_code)
    session_html = (
        f"<p style=\"margin:0; line-height:1.65;\">üìÖ <strong>{session_date}</strong></p>"
        if session_date
        else "<p style=\"margin:0; line-height:1.65;\">üìÖ <strong>Date transmise lors de notre √©change t√©l√©phonique.</strong></p>"
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
        f'<p style="margin:0; line-height:1.65;">üìÖ <strong>{html_module.escape(session_date)}</strong></p>'
        if session_date
        else '<p style="margin:0; line-height:1.65;">üìÖ <strong>Date transmise lors de notre √©change t√©l√©phonique.</strong></p>'
    )
    secourisme_info = (
        "Un certificat SST valide ou un PSC1 de moins de 2 ans reste requis."
        if ssiap_secourisme_valide == "NON"
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





def build_desp_init_email_html(
    prenom: str, dates_txt: str, centre_code: str, devis_url: str, data_store=None,
):
    centre_label, _ = _centre_label_and_address(centre_code)
    centre_display = centre_label.replace("Int√©grale Academy ", "")
    session_html = _format_upcoming_sessions_for_email(
        centre_code, "DESP_INIT", data_store,
    )
    return _render_email_template("desp_init.html", prenom=prenom, centre_display=centre_display, session_html=session_html, devis_url=devis_url)




# --------------- Auth helpers ---------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_email"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    wrapper._login_required = True
    return wrapper

def current_user():
    # retourne dict utilisateur (email, name, role) ou None
    ue = session.get("user_email")
    if not ue: 
        return None
    return USERS.get(ue.lower())


def can_manage_admin_devis_rappels():
    """R√©serve la gestion des rappels √† l'administrateur."""
    user = current_user()
    if not user:
        return False
    return user.get("role") == "admin"


# -------------------------------------------------------------------
# Emails (admin, accus√©, confirmation)
# -------------------------------------------------------------------
def envoyer_mail_admin(demande):
    sujet = f"üÜï Nouvelle demande stagiaire ‚Äî {demande['motif']}"
    plain = (
        "üÜï Nouvelle demande re√ßue :\n\n"
        f"üë§ Nom: {demande['nom']}\n"
        f"üë§ Pr√©nom: {demande['prenom']}\n"
        f"üìû T√©l√©phone: {demande['telephone']}\n"
        f"‚úâÔ∏è Email: {demande['mail']}\n"
        f"üìå Motif: {demande['motif']}\n"
        f"üìù D√©tails: {demande['details']}\n"
        f"üìÖ Date: {demande['date']}\n"
    )
    if demande.get("justificatif"):
        plain += f"üìé Justificatif: {url_for('download_file', filename=demande['justificatif'], _external=True)}\n"

    rows = f"""
      <tr><td style="padding:6px 8px;color:#555;width:150px;">üë§ Nom</td>
          <td style="padding:6px 8px;"><strong>{demande['nom']}</strong></td></tr>
      <tr><td style="padding:6px 8px;color:#555;width:150px;">üë§ Pr√©nom</td>
          <td style="padding:6px 8px;"><strong>{demande['prenom']}</strong></td></tr>
      <tr><td style="padding:6px 8px;color:#555;width:150px;">üìû T√©l√©phone</td>
          <td style="padding:6px 8px;">{demande['telephone']}</td></tr>
      <tr><td style="padding:6px 8px;color:#555;width:150px;">‚úâÔ∏è Email</td>
          <td style="padding:6px 8px;">{demande['mail']}</td></tr>
      <tr><td style="padding:6px 8px;color:#555;width:150px;">üìå Motif</td>
          <td style="padding:6px 8px;">{demande['motif']}</td></tr>
      <tr><td style="padding:6px 8px;color:#555;width:150px;">üìù D√©tails</td>
          <td style="padding:6px 8px;">{demande['details']}</td></tr>
      <tr><td style="padding:6px 8px;color:#555;width:150px;">üìÖ Date</td>
          <td style="padding:6px 8px;">{demande['date']}</td></tr>
    """
    if demande.get("justificatif"):
        link = url_for('download_file', filename=demande['justificatif'], _external=True)
        rows += f"""<tr><td style="padding:6px 8px;color:#555;width:150px;">üìé Justificatif</td>
                      <td style="padding:6px 8px;">
                        <a href="{link}" style="color:#0d6efd;text-decoration:none;">T√©l√©charger</a>
                      </td></tr>"""

    html = _wrap_html(
        '<h1 style="margin:0 0 12px;font-size:20px;">üÜï Nouvelle demande stagiaire</h1>',
        f"""
        <p style="margin:0 0 12px;">Une nouvelle demande a √©t√© soumise sur le site.</p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:14px;">
          {rows}
        </table>
        """
    )
    send_email_html("elsaduq83@gmail.com, ecole@integraleacademy.com", sujet, plain, html)



def envoyer_mail_formulaire_rappel_admin(callback_data):
    sujet = "Formulaire Demoiselles du T√©l√©phone"
    plain = (
        "Formulaire Demoiselles du T√©l√©phone :\n\n"
        f"Nom: {callback_data.get('nom', '')}\n"
        f"Pr√©nom: {callback_data.get('prenom', '')}\n"
        f"Email: {callback_data.get('mail', '')}\n"
        f"T√©l√©phone: {callback_data.get('telephone', '')}\n"
        f"Objet de l'appel: {callback_data.get('objet_appel', '')}\n"
        f"Cr√©neau de rappel: {callback_data.get('creneau_rappel', '')}\n"
    )

    rows = f"""
      <tr><td style="padding:6px 8px;color:#555;width:170px;">Nom</td><td style="padding:6px 8px;"><strong>{callback_data.get('nom', '')}</strong></td></tr>
      <tr><td style="padding:6px 8px;color:#555;">Pr√©nom</td><td style="padding:6px 8px;"><strong>{callback_data.get('prenom', '')}</strong></td></tr>
      <tr><td style="padding:6px 8px;color:#555;">Email</td><td style="padding:6px 8px;">{callback_data.get('mail', '')}</td></tr>
      <tr><td style="padding:6px 8px;color:#555;">T√©l√©phone</td><td style="padding:6px 8px;">{callback_data.get('telephone', '')}</td></tr>
      <tr><td style="padding:6px 8px;color:#555;">Objet de l'appel</td><td style="padding:6px 8px;white-space:pre-wrap;">{callback_data.get('objet_appel', '')}</td></tr>
      <tr><td style="padding:6px 8px;color:#555;">Cr√©neau de rappel</td><td style="padding:6px 8px;">{callback_data.get('creneau_rappel', '')}</td></tr>
    """
    html = _wrap_html(
        '<h1 style="margin:0 0 12px;font-size:20px;">Formulaire Demoiselles du T√©l√©phone</h1>',
        f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:14px;">
          {rows}
        </table>
        """
    )
    send_email_html("cassandre@integraleacademy.com", sujet, plain, html)
def envoyer_mail_accuse(demande):
    sujet = "üì© Accus√© de r√©ception ‚Äî Int√©grale Academy"
    plain = (
        f"Bonjour {demande['prenom']},\n\n"
        "üì© Nous avons bien re√ßu votre demande.\n"
        "‚è≥ Elle sera trait√©e dans les meilleurs d√©lais.\n"
        "‚úÖ Vous recevrez un mail lorsque votre demande aura √©t√© trait√©e.\n\n"
        "üôè Merci de votre confiance,\n"
        "L'√©quipe Int√©grale Academy\n"
    )
    html = _wrap_html(
        '<h1 style="margin:0 0 12px;font-size:20px;">üì© Accus√© de r√©ception</h1>',
        f"""
        <p>Bonjour <strong>{demande['prenom']}</strong>,</p>
        <p>üì© Nous avons bien re√ßu votre demande.</p>
        <p>‚è≥ Elle sera trait√©e dans les meilleurs d√©lais.</p>
        <p style="margin:0">‚úÖ Vous recevrez un mail lorsque votre demande aura √©t√© trait√©e.</p>
        <p style="margin:16px 0 0;">üôè Merci de votre confiance,<br>L'√©quipe Int√©grale Academy</p>
        """
    )
    send_email_html(demande["mail"], sujet, plain, html)

def envoyer_mail_confirmation(demande):
    sujet = "‚úÖ Votre demande a √©t√© trait√©e ‚Äî Int√©grale Academy"
    repondre_url = url_for("repondre", demande_id=demande["id"], _external=True)

    plain = (
        f"Bonjour {demande['prenom']},\n\n"
        "‚úÖ Votre demande a √©t√© trait√©e.\n\n"
        f"Motif : {demande['motif']}\n"
        f"D√©tails : {demande['details']}\n"
        f"‚úçÔ∏è Notre r√©ponse : {demande.get('commentaire') or 'Aucun commentaire ajout√©.'}\n"
        f"{'Des pi√®ces jointes sont incluses.' if demande.get('pieces_jointes') else ''}\n\n"
        f"üëâ Pour r√©pondre, cliquez ici : {repondre_url}\n\n"
        "Cordialement,\n"
        "L'√©quipe Int√©grale Academy\n"
    )

    body_html = f"""
      <p>Bonjour <strong>{demande['prenom']}</strong>,</p>
      <p style="margin:0 0 8px;">‚úÖ <strong>Votre demande a √©t√© trait√©e.</strong></p>

      <table role="presentation" cellpadding="0" cellspacing="0" width="100%" 
             style="border-collapse:collapse;background:#f9fafb;border:1px solid #eef0f2;
                    border-radius:8px;margin-top:16px;">
        <tr>
          <td style="padding:12px 14px;font-family:Arial,Helvetica,sans-serif;
                     font-size:14px;color:#222;">
            <div style="margin:4px 0;"><strong>Motif :</strong> {demande['motif']}</div>
            <div style="margin:4px 0;"><strong>D√©tails :</strong> {demande['details']}</div>
            <div style="margin:12px 0;padding:12px;background:#fff8e5;
                        border:1px solid #f0dca6;border-radius:6px;">
              <strong>‚úçÔ∏è Notre r√©ponse :</strong><br>
              {demande.get('commentaire') or 'Aucun commentaire ajout√©.'}
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
              üì© R√©pondre √† ce message
            </a>
          </td>
        </tr>
      </table>

      {"<p style='margin:8px 0;'>Des pi√®ces jointes sont incluses avec ce message.</p>" if demande.get("pieces_jointes") else ""}
      <p style="margin:16px 0 0;">Cordialement,<br>L'√©quipe Int√©grale Academy</p>
    """
    html = _wrap_html('<h1 style="margin:0 0 12px;font-size:20px;">‚úÖ Demande trait√©e</h1>', body_html)

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
    """Envoie un mail √† znaw83@gmail.com quand la demande est attribu√©e √† Mohamed"""
    sujet = f"üì© Nouvelle demande attribu√©e √† Mohamed ‚Äî {demande.get('motif','')}"
    lien_admin = "https://assistance-alw9.onrender.com/admin"

    plain = (
        f"Une demande vient d'√™tre attribu√©e √† Mohamed.\n\n"
        f"üë§ {demande.get('prenom','')} {demande.get('nom','')}\n"
        f"üìß {demande.get('mail','')}\n"
        f"üìÖ {demande.get('date','')}\n"
        f"üìå Motif : {demande.get('motif','')}\n"
        f"üìù D√©tails : {demande.get('details','')}\n\n"
        f"‚û°Ô∏è Connectez-vous √† la plateforme pour la traiter : {lien_admin}"
    )

    html = _wrap_html(
        '<h1 style="margin:0 0 12px;font-size:20px;">üì© Nouvelle demande attribu√©e √† Mohamed</h1>',
        f"""
        <p>Une demande vient d'√™tre <strong>attribu√©e √† Mohamed</strong>.</p>
        <table role="presentation" cellpadding="0" cellspacing="0" width="100%" 
               style="border-collapse:collapse;font-size:14px;">
          <tr><td style="padding:6px 8px;">üë§ Nom</td><td>{demande.get('nom','')}</td></tr>
          <tr><td style="padding:6px 8px;">üë§ Pr√©nom</td><td>{demande.get('prenom','')}</td></tr>
          <tr><td style="padding:6px 8px;">üìß Email</td><td>{demande.get('mail','')}</td></tr>
          <tr><td style="padding:6px 8px;">üìÖ Date</td><td>{demande.get('date','')}</td></tr>
          <tr><td style="padding:6px 8px;">üìå Motif</td><td>{demande.get('motif','')}</td></tr>
          <tr><td style="padding:6px 8px;">üìù D√©tails</td><td>{demande.get('details','')}</td></tr>
        </table>
        <p style="margin-top:16px;">
          ‚û°Ô∏è <a href="{lien_admin}" style="color:#0d6efd;text-decoration:none;font-weight:bold;">
          Se connecter √† la plateforme pour traiter la demande</a>
        </p>
        """
    )

    send_email_html("znaw83@gmail.com", sujet, plain, html)





def _payload_salesforce_poei_cannes(demande, details):
    infos_complementaires = (
        "CANDIDATURE POEI S√âCURIT√â CANNES\n"
        "Formation : POEI Agent de s√©curit√© priv√©e + Agent de s√©curit√© incendie SSIAP 1\n"
        "Dates : 23 septembre au 22 d√©cembre 2026\n"
        "Lieu de formation : Int√©grale Academy, Puget-sur-Argens\n"
        "Poste vis√© : Agent de s√©curit√© / Agent de s√©curit√© incendie √† Cannes\n"
        "Contrat pr√©vu : CDD minimum 6 mois\n"
        f"Ville de r√©sidence : {details.get('Ville de r√©sidence', '')}\n"
        f"Permis B : {details.get('Permis B', '')}\n"
        f"Disponible formation : {details.get('Disponible formation', '')}\n"
        f"Mobilit√© Cannes : {details.get('Mobilit√© Cannes', '')}\n"
        f"Inscrit France Travail : {details.get('Inscrit France Travail', '')}\n"
        f"Identifiant France Travail : {details.get('Identifiant France Travail', '')}\n"
        f"Message / motivation : {details.get('Message / motivation', '')}"
    )
    return {
        "nom": demande.get("nom", ""),
        "prenom": demande.get("prenom", ""),
        "mail": demande.get("mail", ""),
        "telephone": demande.get("telephone", ""),
        "formation": "POEI",
        "type_formation": "POEI Agent de s√©curit√© priv√©e + SSIAP 1",
        "source_formulaire": "poei-agent-securite-cannes",
        "origine": "POEI",
        "centre": "cote_azur",
        "dates": "23 septembre au 22 d√©cembre 2026",
        "france_travail": (
            "OUI"
            if details.get("Inscrit France Travail") == "Oui"
            else details.get("Inscrit France Travail", "")
        ),
        "ville": details.get("Ville de r√©sidence", ""),
        "permis_b": details.get("Permis B", ""),
        "mobilite_cannes": details.get("Mobilit√© Cannes", ""),
        "identifiant_france_travail": details.get("Identifiant France Travail", ""),
        "infos_complementaires": infos_complementaires,
    }

def _poei_cannes_admin_email(demande):
    details = demande.get("details_data", {})
    rows = "".join(
        f"<tr><td style='padding:8px 10px;color:#64748b;border-bottom:1px solid #eef2f7;'>{html_module.escape(label)}</td>"
        f"<td style='padding:8px 10px;border-bottom:1px solid #eef2f7;'><strong>{html_module.escape(str(value or '‚Äî'))}</strong></td></tr>"
        for label, value in details.items()
    )
    plain = (
        "Nouvelle candidature POEI S√©curit√© Cannes\n\n"
        f"Nom : {demande.get('nom', '')}\n"
        f"Pr√©nom : {demande.get('prenom', '')}\n"
        f"Email : {demande.get('mail', '')}\n"
        f"T√©l√©phone : {demande.get('telephone', '')}\n"
        f"Ville : {details.get('Ville de r√©sidence', '')}\n"
        f"Message : {details.get('Message / motivation', '')}"
    )
    html_body = _wrap_html(
        "<h1 style='margin:0;color:#123c2f;'>Nouvelle candidature POEI S√©curit√© Cannes</h1>",
        f"""
        <p>Une nouvelle candidature a √©t√© transmise depuis la landing page POEI Agent de s√©curit√© + SSIAP 1.</p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;border:1px solid #eef2f7;border-radius:12px;overflow:hidden;">{rows}</table>
        """
    )
    return send_email_html("aurelie@integraleacademy.com", "Nouvelle candidature POEI S√©curit√© Cannes", plain, html_body)


def _poei_cannes_candidate_email(demande):
    prenom = html_module.escape(demande.get("prenom", "") or "")
    plain = (
        f"Bonjour {demande.get('prenom', '')},\n\n"
        "Nous avons bien re√ßu votre candidature pour la formation POEI Agent de s√©curit√© + SSIAP 1.\n"
        "Notre √©quipe va l'√©tudier avec attention et reviendra vers vous tr√®s prochainement.\n\n"
        "√Ä tr√®s vite,\n"
        "L'√©quipe Int√©grale Academy"
    )
    html_body = f"""
    <div style="text-align:center;padding:8px 0 18px;">
      <span style="display:inline-block;padding:8px 14px;border-radius:999px;background:#ecfdf5;color:#047857;font-weight:800;font-size:13px;letter-spacing:.02em;">Candidature re√ßue</span>
    </div>
    <div style="background:linear-gradient(135deg,#0f172a,#14532d);border-radius:22px;padding:28px 24px;color:#fff;text-align:center;box-shadow:0 18px 38px rgba(15,23,42,.18);">
      <div style="font-size:42px;line-height:1;margin-bottom:12px;">‚úÖ</div>
      <h1 style="margin:0;font-size:26px;line-height:1.2;color:#fff;">Nous avons bien re√ßu votre candidature</h1>
      <p style="margin:14px 0 0;font-size:16px;line-height:1.6;color:#dcfce7;">Formation POEI Agent de s√©curit√© + SSIAP 1</p>
    </div>
    <div style="padding:24px 4px 4px;">
      <p style="font-size:16px;margin:0 0 14px;">Bonjour <strong>{prenom}</strong>,</p>
      <p style="font-size:16px;margin:0 0 14px;">Merci pour votre candidature. Elle a bien √©t√© transmise √† notre √©quipe.</p>
      <p style="font-size:16px;margin:0 0 18px;">Nous allons l'√©tudier avec attention et nous reviendrons vers vous <strong>tr√®s prochainement</strong> pour les prochaines √©tapes.</p>
      <div style="border:1px solid #d1fae5;background:#f0fdf4;border-radius:16px;padding:16px 18px;margin:20px 0;">
        <p style="margin:0;color:#14532d;font-weight:800;">Votre parcours en bref</p>
        <p style="margin:8px 0 0;color:#166534;">Formation financ√©e du 23 septembre au 22 d√©cembre 2026, puis opportunit√© d'emploi si votre candidature est retenue.</p>
      </div>
      <p style="margin:18px 0 0;">√Ä tr√®s vite,<br><strong>L'√©quipe Int√©grale Academy</strong></p>
    </div>
    """
    html = _wrap_html("", html_body)
    return send_email_html(
        demande.get("mail", ""),
        "‚úÖ Nous avons bien re√ßu votre candidature ‚Äî Int√©grale Academy",
        plain,
        html,
    )


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
            "Formation": "POEI Agent de s√©curit√© priv√©e + Agent de s√©curit√© incendie SSIAP 1",
            "Dates": "23 septembre au 22 d√©cembre 2026",
            "Lieu de formation": "Int√©grale Academy, Puget-sur-Argens",
            "Poste vis√©": "Agent de s√©curit√© / Agent de s√©curit√© incendie √† Cannes",
            "Contrat pr√©vu": "CDD minimum 6 mois",
            "Ville de r√©sidence": request.form.get("ville", "").strip(),
            "Permis B": request.form.get("permis_b", "").strip(),
            "Disponible formation": request.form.get("disponible_formation", "").strip(),
            "Mobilit√© Cannes": request.form.get("mobilite_cannes", "").strip(),
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
            "motif": "Nouvelle candidature POEI S√©curit√© Cannes",
            "details": json.dumps(details_data, ensure_ascii=False),
            "details_data": details_data,
            "date": datetime.datetime.now(paris_tz).strftime("%d/%m/%Y %H:%M"),
            "statut": "Non trait√©",
            "attribution": "",
            "commentaire": "",
            "commentaire_admin": "",
            "mail_confirme": "",
            "mail_erreur": "",
            "mail_contenu": "",
            "mail_html": "",
            "candidat_mail_confirme": "",
            "candidat_mail_erreur": "",
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
            demande["mail_erreur"] = f"Erreur envoi email admin : {e}"
        try:
            if _poei_cannes_candidate_email(demande):
                demande["candidat_mail_confirme"] = datetime.datetime.now(paris_tz).strftime("%d/%m/%Y %H:%M")
            else:
                demande["candidat_mail_erreur"] = "Variables SMTP/Brevo manquantes ou envoi impossible : SMTP_USER/SMTP_PASS ou BREVO_API_KEY/BREVO_SENDER_EMAIL."
        except Exception as e:
            demande["candidat_mail_erreur"] = f"Erreur envoi email candidat : {e}"
        data = load_data()
        for entry in data.get("demandes", []):
            if entry.get("id") == demande["id"]:
                entry.update({
                    "mail_confirme": demande["mail_confirme"],
                    "mail_erreur": demande["mail_erreur"],
                    "candidat_mail_confirme": demande["candidat_mail_confirme"],
                    "candidat_mail_erreur": demande["candidat_mail_erreur"],
                })
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
            "statut": "Non trait√©",
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
    if session.get("user_email"):
        return redirect(url_for("crm"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = USERS.get(email)

        client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
        if not _login_attempt_allowed(client_ip):
            flash("Trop de tentatives. Veuillez patienter quelques minutes.", "error")
            return render_template("login.html"), 429

        configured_password = os.environ.get(user["password_env"]) if user else None
        if user and not configured_password:
            app.logger.warning("Compte CRM d√©sactiv√© : variable %s absente", user["password_env"])

        if user and configured_password and hmac.compare_digest(configured_password, password):
            _clear_login_attempts(client_ip)
            session.clear()  # renouvelle les donn√©es et l'identit√© de la session
            session["user_email"] = email
            session["user_name"] = user["name"]
            session["user_role"] = user["role"]
            session.permanent = True

            next_url = _safe_next_url(request.args.get("next")) or url_for("crm")
            return redirect(next_url)

        _record_login_failure(client_ip)

        flash("Identifiants incorrects", "error")
        return render_template("login.html"), 401

    return render_template("login.html")


_LOGIN_ATTEMPTS = {}
_LOGIN_ATTEMPTS_LOCK = threading.Lock()
_LOGIN_WINDOW_SECONDS = 5 * 60
_LOGIN_MAX_ATTEMPTS = 5


def _safe_next_url(value):
    """Accepte uniquement un chemin absolu interne, jamais une URL externe."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return None


def _login_attempt_allowed(client_ip):
    now = time.monotonic()
    with _LOGIN_ATTEMPTS_LOCK:
        failures = [stamp for stamp in _LOGIN_ATTEMPTS.get(client_ip, []) if now - stamp < _LOGIN_WINDOW_SECONDS]
        _LOGIN_ATTEMPTS[client_ip] = failures
        return len(failures) < _LOGIN_MAX_ATTEMPTS


def _record_login_failure(client_ip):
    with _LOGIN_ATTEMPTS_LOCK:
        _LOGIN_ATTEMPTS.setdefault(client_ip, []).append(time.monotonic())


def _clear_login_attempts(client_ip):
    with _LOGIN_ATTEMPTS_LOCK:
        _LOGIN_ATTEMPTS.pop(client_ip, None)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
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

    # üîî Notification attribution Mohamed
    if nouvelle_attribution == "Mohamed" and ancienne_attribution != "Mohamed":
        try:
            envoyer_mail_attribution_mohamed(demande)
        except:
            pass

    ancien_statut = demande.get("statut", "Non trait√©")
    nouveau_statut = form_data.get("statut") or ancien_statut

    # üìé Upload pi√®ces jointes
    if files and "pj" in files:
        for f in files.getlist("pj"):
            if f and f.filename:
                filename = secure_filename(f.filename)
                filepath = os.path.join(UPLOAD_FOLDER, filename)
                f.save(filepath)
                demande.setdefault("pieces_jointes", [])
                if filename not in demande["pieces_jointes"]:
                    demande["pieces_jointes"].append(filename)

    # üì® Passage √† Trait√©
    if ancien_statut != "Trait√©" and nouveau_statut == "Trait√©":
        if envoyer_mail_confirmation(demande):
            data["compteur_traitees"] += 1
            paris_tz = pytz.timezone("Europe/Paris")
            demande["mail_confirme"] = datetime.datetime.now(paris_tz).strftime("%d/%m/%Y %H:%M")
            demande["mail_erreur"] = ""
        else:
            demande["mail_erreur"] = "‚ùå Erreur lors de l'envoi du mail"

    demande["statut"] = nouveau_statut


@app.route("/admin", methods=["GET", "POST"])
@login_required
def admin():
    data = load_data()
    demandes = data["demandes"]

    # ‚ùå Exclure les demandes de devis d√©taill√© de l'admin principal
    demandes = [
        d for d in demandes
        if d.get("motif") != "Demande de devis d√©taill√©"
    ]

    # üîê Identifier l'utilisateur connect√©
    user = current_user()
    user_name = user["name"] if user else None
    user_role = user["role"] if user else "user"

    # ‚úÖ Fonction de parsing de date (AFFICHAGE UNIQUEMENT)
    def parse_date(d):
        try:
            return datetime.datetime.strptime(d.get("date", ""), "%d/%m/%Y %H:%M")

        except:
            return datetime.datetime.min

    # =====================================================
    # üü¢ TRAITEMENTS POST (AJOUT / UPDATE / DELETE / ARCHIVE)
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

        # ‚ûï Ajout manuel
        if action == "add":
            paris_tz = pytz.timezone("Europe/Paris")
            new_demande = {
                "id": str(uuid.uuid4()),
                "nom": "Vaillant",
                "prenom": "Cl√©ment",
                "telephone": request.form.get("telephone", ""),
                "mail": request.form.get("mail", "ecole@integraleacademy.com"),
                "motif": request.form.get("motif", "Autre"),
                "details": request.form.get("details", ""),
                "justificatif": "",
                "date": datetime.datetime.now(paris_tz).strftime("%d/%m/%Y %H:%M"),
                "attribution": request.form.get("attribution", "Cl√©ment"),
                "statut": "Non trait√©",
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

            # üìß Notification si attribu√©e √† Mohamed
            if new_demande["attribution"].strip() == "Mohamed":
                try:
                    envoyer_mail_attribution_mohamed(new_demande)
                except:
                    pass

            return redirect_with_query()

        # ‚úèÔ∏è Mise √† jour d'une demande existante
        elif action == "update":
            for d in demandes:
                if d["id"] == demande_id:
                    update_demande_fields(d, request.form, data, request.files)

            save_data(data)
            return redirect_with_query()

        # ‚ùå Suppression d'une pi√®ce jointe
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

        # üóëÔ∏è Archivage d'une demande
        elif action == "delete":
            to_remove = next((d for d in demandes if d["id"] == demande_id), None)
            if to_remove:
                data["archives"].append(to_remove)
                data["demandes"].remove(to_remove)
                save_data(data)
            return redirect_with_query()

        # üßπ Archivage de toutes les demandes trait√©es
        elif action == "delete_all_traitees":
            traitees = [d for d in demandes if d.get("statut") == "Trait√©"]
            for d in traitees:
                data["archives"].append(d)
                data["demandes"].remove(d)
            save_data(data)
            return redirect_with_query()

    # =========================
    # üîç Recherche (GET)
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

    # üë§ Filtre utilisateur (non admin)
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

                if source.get("motif") != "Demande de devis d√©taill√©":
                    return True

                return (source.get("statut_devis") or "A envoyer") == "Envoy√©"

            demandes = [d for d in demandes if _visible_for_mohamed(d)]

    # üîΩ TRI GLOBAL (plus r√©centes en premier)
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
        demande.get("motif") == "Demande de devis d√©taill√©"
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


def _brevo_sms_credits() -> float:
    """Return the SMS credits currently available on the configured Brevo account."""
    api_key = os.getenv("BREVO_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("La cl√© API Brevo n‚Äôest pas configur√©e.")

    response = requests.get(
        "https://api.brevo.com/v3/account",
        headers={"accept": "application/json", "api-key": api_key},
        timeout=10,
    )
    if not 200 <= response.status_code < 300:
        raise RuntimeError("Brevo n‚Äôa pas pu communiquer le solde SMS.")

    payload = response.json()
    plans = payload.get("plan", []) if isinstance(payload, dict) else []
    sms_plans = [
        plan for plan in plans
        if isinstance(plan, dict) and str(plan.get("type", "")).casefold() == "sms"
    ]
    if not sms_plans:
        raise RuntimeError("Aucun cr√©dit SMS n‚Äôa √©t√© trouv√© sur le compte Brevo.")
    try:
        return sum(float(plan.get("credits", 0)) for plan in sms_plans)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Brevo a renvoy√© un solde SMS invalide.") from exc


BREVO_SMS_CREDITS_PER_MESSAGE = 4.5
BREVO_SMS_LOW_BALANCE_THRESHOLD = 50
BREVO_SMS_LOW_BALANCE_RECIPIENTS = (
    "clement@integraleacademy.com",
    "cassandre@integraleacademy.com",
)


def _brevo_sms_remaining(credits: float) -> int:
    """Convert Brevo credits to the same estimated SMS count shown in the CRM."""
    return max(0, int(float(credits) // BREVO_SMS_CREDITS_PER_MESSAGE))


def _notify_brevo_sms_low_balance(credits: float) -> bool:
    """Send one alert per low-balance period and re-arm it after a top-up."""
    sms_remaining = _brevo_sms_remaining(credits)
    data = load_data()
    alert_key = "crm_brevo_sms_low_balance_alerted"

    if sms_remaining >= BREVO_SMS_LOW_BALANCE_THRESHOLD:
        if data.get(alert_key):
            data[alert_key] = False
            save_data(data)
        return False
    if data.get(alert_key):
        return False

    subject = f"Alerte Brevo : moins de {BREVO_SMS_LOW_BALANCE_THRESHOLD} SMS restants"
    plain_text = (
        "Le solde SMS Brevo est bient√¥t √©puis√©.\n\n"
        f"Solde estim√© : {sms_remaining} SMS restants ({credits:g} cr√©dits Brevo).\n"
        "Merci de recharger le compte Brevo afin d‚Äô√©viter une interruption des envois."
    )
    html_body = (
        "<p>Le solde SMS Brevo est bient√¥t √©puis√©.</p>"
        f"<p><strong>Solde estim√© : {sms_remaining} SMS restants</strong> "
        f"({credits:g} cr√©dits Brevo).</p>"
        "<p>Merci de recharger le compte Brevo afin d‚Äô√©viter une interruption des envois.</p>"
    )
    if not send_email_html(
        BREVO_SMS_LOW_BALANCE_RECIPIENTS,
        subject,
        plain_text,
        html_body,
    ):
        return False

    data[alert_key] = True
    save_data(data)
    return True


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

@app.route("/secretariat")
def secretariat():
    data_store = load_data()
    formations = []
    formation_icons = {
        "A3P": "üß≠",
        "APS": "üëÆ",
        "SSIAP": "üî•",
        "DESP_INIT": "üíº",
        "DESP_VAE": "üìã",
        "VTC": "üöò",
    }
    sessions = get_upcoming_formation_sessions(data_store)
    for code, details in SECRETARIAT_FORMATIONS.items():
        centres = []
        for centre_code, centre_label in FORMATION_CENTRES.items():
            rows = sessions.get(centre_code, {}).get(code, [])
            if rows:
                centres.append({"code": centre_code, "label": centre_label, "sessions": rows})
        formations.append({
            "code": code,
            "label": details.get("label", PLAN_FORMATIONS.get(code, code)),
            "centres": centres,
            "category": "bts" if code.startswith("BTS_") else "security",
            "icon": "üéì" if code.startswith("BTS_") else formation_icons.get(code, "üìò"),
            **details,
        })
    journal = sorted(
        data_store.get("secretariat_demandes", []),
        key=lambda row: row.get("created_at", ""),
        reverse=True,
    )
    return render_template("secretariat.html", formations=formations, journal=journal)


_SECRETARIAT_DELIVERY_LOCK = threading.Lock()


def _serialize_secretariat_delivery(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        with _SECRETARIAT_DELIVERY_LOCK, _CRM_RECONCILIATION_LOCK:
            return view(*args, **kwargs)
    return wrapped


def _secretariat_existing_crm_contact(data, payload):
    """Return a matching existing CRM contact without ever creating a lead."""
    contacts = data.get("crm_contacts", [])
    email = _crm_normalize_email(payload.get("email") or payload.get("mail"))
    phone = _crm_normalize_phone(payload.get("telephone"))
    if not email and not phone:
        return None

    def matches(contact):
        return bool(
            (email and _crm_normalize_email(contact.get("mail")) == email)
            or (phone and _crm_normalize_phone(contact.get("telephone")) == phone)
        )

    selected_id = str(payload.get("crm_contact_id") or "").strip()
    if selected_id:
        selected = next(
            (contact for contact in contacts
             if str(contact.get("id") or "") == selected_id),
            None,
        )
        if selected and matches(selected):
            return selected

    return next((contact for contact in contacts if matches(contact)), None)


@app.route("/api/secretariat/crm-contact", methods=["POST"])
def api_secretariat_crm_contact():
    """Find an existing CRM record without exposing the contact list."""
    payload = request.get_json(silent=True) or {}
    contact = _secretariat_existing_crm_contact(load_data(), payload)
    return jsonify({"contact_id": contact.get("id") if contact else None})


@app.route("/api/secretariat/demandes", methods=["POST"])
@_serialize_secretariat_delivery
def api_secretariat_demandes():
    payload = request.get_json(silent=True) or {}
    if payload.get("type") not in {"formation", "autre"}:
        return jsonify({"ok": False, "error": "Type de demande invalide"}), 400
    now = datetime.datetime.now(pytz.timezone("Europe/Paris"))
    entry = {
        "id": str(uuid.uuid4()),
        "type": payload.get("type"),
        "formation": payload.get("formation", ""),
        "formation_date_souhaitee": str(payload.get("formation_date_souhaitee", "")).strip(),
        "nom": str(payload.get("nom", "")).strip(),
        "prenom": str(payload.get("prenom", "")).strip(),
        "nom_famille": str(payload.get("nom_famille", "")).strip(),
        "formation_centre": str(payload.get("formation_centre", "")).strip(),
        "formation_session_label": str(payload.get("formation_session_label", "")).strip(),
        "formation_date_examen": str(payload.get("formation_date_examen", "")).strip(),
        "telephone": str(payload.get("telephone", "")).strip(),
        "email": str(payload.get("email", "")).strip(),
        "crm_contact_id": str(payload.get("crm_contact_id", "")).strip(),
        "notes": str(payload.get("notes", "")).strip(),
        "devis": str(payload.get("devis", "")).strip(),
        "rdv": str(payload.get("rdv", "")).strip(),
        "rdv_status": str(payload.get("rdv_status", "")).strip(),
        "rdv_date": str(payload.get("rdv_date", "")).strip(),
        "rdv_time": str(payload.get("rdv_time", "")).strip(),
        "rdv_mode": str(payload.get("rdv_mode", "")).strip(),
        "rdv_url": str(payload.get("rdv_url", "")).strip(),
        "calendly_url": str(payload.get("calendly_url", "")).strip(),
        "cpf_consulte": str(payload.get("cpf_consulte", "")).strip(),
        "cpf_montant": str(payload.get("cpf_montant", "")).strip(),
        "france_travail": str(payload.get("france_travail", "")).strip(),
        "france_travail_status": str(payload.get("france_travail_status", "")).strip(),
        "ft_refus_ok": str(payload.get("ft_refus_ok", "")).strip(),
        "financement_perso": str(payload.get("financement_perso", "")).strip(),
        "identite_numerique": str(payload.get("identite_numerique", "")).strip(),
        "ssiap_secourisme_valide": str(payload.get("ssiap_secourisme_valide", "")).strip(),
        "cnaps_ok": str(payload.get("cnaps_ok", "")).strip(),
        "cnaps_status": str(payload.get("cnaps_status", "")).strip(),
        "garde_vue": str(payload.get("garde_vue", "")).strip(),
        "titre_sejour": str(payload.get("titre_sejour", "")).strip(),
        "statut": payload.get("statut", "Trait√©"),
        "created_at": now.isoformat(),
        "date": now.strftime("%d/%m/%Y %H:%M"),
    }
    data_store = load_data()
    entries = data_store.setdefault("secretariat_demandes", [])
    # Le formulaire formation cr√©e d√©j√† une ligne ¬´ RDV √† prendre ¬ª. La derni√®re
    # √©tape du parcours l'enrichit au lieu de cr√©er un doublon dans le journal.
    def is_same_recent_submission(row):
        if payload.get("type") != "formation" or row.get("formation") != entry["formation"] or row.get("telephone") != entry["telephone"]:
            return False
        if row.get("statut") == "RDV √† prendre":
            return True
        try:
            created = datetime.datetime.fromisoformat(str(row.get("created_at")))
            return abs((now - created).total_seconds()) < 300 and row.get("email", "") == entry["email"]
        except (TypeError, ValueError):
            return False
    existing = next((row for row in reversed(entries) if is_same_recent_submission(row)), None)
    if existing:
        created_at, date_label, entry_id = existing.get("created_at"), existing.get("date"), existing.get("id")
        existing.update(entry)
        existing.update({"created_at": created_at, "date": date_label, "id": entry_id})
        entry = existing
    else:
        entries.append(entry)

    # Une ¬´ autre demande ¬ª reste une demande de rappel ind√©pendante. Elle ne
    # doit jamais cr√©er de piste ni √™tre envoy√©e √† Salesforce. Lorsqu'une fiche
    # existe d√©j√†, la demande est seulement li√©e et consign√©e dans son journal.
    if entry["type"] == "autre":
        entry.update({
            "callback_status": "pending",
            "callback_status_updated_at": now.isoformat(),
            "callback_processed_at": "",
            "callback_processed_by": "",
            "statut": "√Ä traiter",
        })
        _, crm_contact = _crm_prepare_callback_request(data_store, entry)
        if crm_contact:
            _crm_ensure_secretariat_publication(crm_contact, entry)
            crm_contact["updated_at"] = _crm_now()
        save_data(data_store)
        return jsonify({
            "ok": True,
            "demande": entry,
            "crm_contact_id": entry.get("crm_contact_id") or None,
            "messages": {},
        }), 201

    # Les demandes de renseignements formation conservent leur fonctionnement :
    # piste CRM, rapprochement non destructif et envoi Salesforce si elle est neuve.
    contact_count_before = len(data_store.get("crm_contacts", []))
    name_parts = entry["nom"].split(None, 1)
    crm_payload = {
        "prenom": str(entry.get("prenom") or (name_parts[0] if name_parts else "")).strip(),
        "nom": str(entry.get("nom_famille") or (name_parts[1] if len(name_parts) > 1 else (name_parts[0] if name_parts else "Sans nom"))).strip(),
        "mail": entry["email"],
        "telephone": entry["telephone"],
        "formation": entry["formation"],
        "source_formulaire": "assistant-secretariat",
        "origine": "Secr√©tariat",
        "cpf_consulte": entry["cpf_consulte"],
        "cpf_montant": entry["cpf_montant"],
        "france_travail": entry["france_travail"],
        "ft_refus_ok": entry["ft_refus_ok"],
        "financement_perso": entry["financement_perso"],
        "identite_numerique": entry["identite_numerique"],
        "cnaps_ok": entry["cnaps_ok"],
        "garde_vue": entry["garde_vue"],
        "titre_sejour": entry["titre_sejour"],
        "infos_complementaires": "\n".join(filter(None, [
            "Appel trait√© par le secr√©tariat",
            f"Type de demande : {entry['type']}",
            f"Session souhait√©e : {entry['formation_date_souhaitee']}" if entry["formation_date_souhaitee"] else "",
            f"Notes : {entry['notes']}" if entry["notes"] else "",
            f"Rendez-vous : {entry['rdv']}" if entry["rdv"] else "",
            f"Devis demand√© : {entry['devis']}" if entry["devis"] else "",
        ])),
    }
    centre_code, session_label = _secretariat_session_details(entry)
    # Salesforce expects these exact generic form keys. Without them, its lead
    # received neither the selected campus nor the requested training dates.
    crm_payload.update({"centre": centre_code, "dates": session_label})
    crm_contact = _crm_create_contact_from_secretariat(data_store, entry, crm_payload)
    if crm_contact:
        _crm_ensure_secretariat_publication(crm_contact, entry)
    creates_new_lead = len(data_store.get("crm_contacts", [])) > contact_count_before
    message_results = {}
    if entry["type"] == "formation" and crm_contact:
        _secretariat_refresh_calendly_appointments(data_store, entry, crm_contact)
        _secretariat_hydrate_appointment_from_crm(data_store, entry, crm_contact)
        _ensure_secretariat_quote(data_store, entry, crm_contact)
        message_results = _send_secretariat_information_messages(data_store, entry, crm_contact)
    save_data(data_store)
    if creates_new_lead:
        creer_piste_salesforce(crm_payload)
    return jsonify({
        "ok": True,
        "demande": entry,
        "crm_contact_id": crm_contact.get("id") if creates_new_lead and crm_contact else None,
        "messages": message_results,
    }), 201


def _secretariat_ai_context(formation_code):
    """Build the trusted training context; no training facts come from the browser."""
    details = SECRETARIAT_FORMATIONS.get(formation_code)
    if not details:
        return None
    sessions = get_formation_sessions(load_data())
    centres = []
    for centre_code, centre_label in FORMATION_CENTRES.items():
        rows = sessions.get(centre_code, {}).get(formation_code, [])
        if rows:
            centres.append({"centre": centre_label, "sessions": rows})
    return {"code": formation_code,
            "nom": details.get("label", PLAN_FORMATIONS.get(formation_code, formation_code)),
            **details, "centres_et_sessions": centres}


def _call_secretariat_ai(formation_code, task, conversation=None):
    context = _secretariat_ai_context(formation_code)
    if context is None:
        return None
    website_context = _secretariat_website_context(formation_code, task)
    started = time.monotonic()
    system = (
        "Tu aides le secr√©tariat d'Int√©grale Academy pendant un appel. R√©ponds en fran√ßais, "
        "de fa√ßon claire et directement exploitable. Utilise EXCLUSIVEMENT la fiche fiable et les "
        "extraits du site officiel integraleacademy.com fournis ci-dessous, y compris pour les dates, "
        "tarifs et r√®gles. Cherche d'abord la r√©ponse dans ces deux sources. N'invente jamais et ne "
        "compl√®te pas avec tes connaissances g√©n√©rales. Si la r√©ponse n'est explicitement pr√©sente "
        "dans aucune des deux sources, r√©ponds exactement : ¬´ Cette information n‚Äôest pas disponible "
        "dans la fiche de la formation ni sur le site officiel. ¬ª"
    )
    history = conversation or []
    user = (f"FICHE FIABLE :\n{json.dumps(context, ensure_ascii=False)}\n\n"
            f"EXTRAITS ACTUELS DU SITE OFFICIEL :\n{website_context or 'Site officiel temporairement inaccessible.'}\n\n"
            f"HISTORIQUE SUR CETTE FORMATION :\n{json.dumps(history, ensure_ascii=False)}\n\n"
            f"T√ÇCHE :\n{task}")
    try:
        return _crm_ai(system, user, max_tokens=900)
    finally:
        app.logger.info("Appel IA secr√©tariat formation=%s dur√©e_ms=%d",
                        formation_code, round((time.monotonic() - started) * 1000))


@app.route("/api/secretariat/formations/<formation_code>/ai/question", methods=["POST"])
def api_secretariat_ai_question(formation_code):
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question", "")).strip()
    if not question:
        return jsonify({"ok": False, "error": "√âcrivez une question."}), 400
    if len(question) > 500:
        return jsonify({"ok": False, "error": "La question est trop longue (500 caract√®res maximum)."}), 400
    conversation = payload.get("conversation", [])
    if not isinstance(conversation, list):
        return jsonify({"ok": False, "error": "Historique de conversation invalide."}), 400
    clean_history = []
    for item in conversation[-8:]:
        if not isinstance(item, dict):
            continue
        clean_history.append({"question": str(item.get("question", ""))[:500],
                              "answer": str(item.get("answer", ""))[:2000]})
    if formation_code not in SECRETARIAT_FORMATIONS:
        return jsonify({"ok": False, "error": "Formation inconnue."}), 404
    try:
        reply = _call_secretariat_ai(formation_code,
            f"R√©ponds √† cette question en 2 √† 5 phrases : {question}", clean_history)
        return jsonify({"ok": True, "reply": reply})
    except Exception:
        app.logger.exception("Assistant IA du secr√©tariat indisponible")
        return jsonify({"ok": False, "error": "L‚Äôassistant IA est momentan√©ment indisponible. R√©essayez plus tard."}), 503


@app.route("/api/secretariat/formations/<formation_code>/ai/key-information", methods=["POST"])
def api_secretariat_ai_key_information(formation_code):
    if formation_code not in SECRETARIAT_FORMATIONS:
        return jsonify({"ok": False, "error": "Formation inconnue."}), 404
    task = ("Cr√©e une synth√®se structur√©e et facile √† lire : nom, objectif, d√©bouch√©s, public, pr√©requis, "
            "conditions CNAPS, tarif, dur√©e, modalit√©, centres et prochaines dates, financements, d√©roulement, "
            "examen, certification et points importants. Termine par ¬´ R√©sum√© oral (30 √† 45 secondes) ¬ª avec "
            "un texte naturel √† lire. Omet les rubriques sans donn√©e plut√¥t que de les inventer.")
    try:
        return jsonify({"ok": True, "summary": _call_secretariat_ai(formation_code, task)})
    except Exception:
        app.logger.exception("Synth√®se IA du secr√©tariat indisponible")
        return jsonify({"ok": False, "error": "La g√©n√©ration IA est momentan√©ment indisponible. R√©essayez plus tard."}), 503


@app.route("/api/secretariat/ai/request-summary", methods=["POST"])
def api_secretariat_ai_request_summary():
    """Turn the optional call qualification fields into a CRM-ready note."""
    payload = request.get_json(silent=True) or {}
    formation_code = str(payload.get("formation", "")).strip()
    if formation_code and formation_code not in SECRETARIAT_FORMATIONS:
        return jsonify({"ok": False, "error": "Formation inconnue."}), 400

    allowed_fields = {
        "type": "Type de demande", "nom": "Appelant", "telephone": "T√©l√©phone",
        "email": "E-mail", "rdv": "Rendez-vous t√©l√©phonique",
        "cpf_consulte": "Compte CPF consult√©", "cpf_montant": "Montant CPF disponible",
        "france_travail": "Financement France Travail souhait√©",
        "ft_refus_ok": "Financement personnel en cas de refus France Travail",
        "financement_perso": "Financement personnel ou reste √† charge possible",
        "identite_numerique": "Identit√© Num√©rique La Poste cr√©√©e",
        "cnaps_ok": "Carte professionnelle CNAPS valide",
        "garde_vue": "Garde √† vue ou prise d‚Äôempreintes",
        "titre_sejour": "Titre de s√©jour", "devis": "Devis d√©taill√©",
        "formation_date_souhaitee": "Session de formation souhait√©e",
    }
    facts = {label: str(payload.get(key, "")).strip()[:1000]
             for key, label in allowed_fields.items() if str(payload.get(key, "")).strip()}
    precision = str(payload.get("precision", "")).strip()[:4000]
    current_summary = str(payload.get("summary", "")).strip()[:6000]
    context = _secretariat_ai_context(formation_code) if formation_code else None
    system = (
        "Tu r√©diges la note transmise au CRM d'Int√©grale Academy apr√®s un appel t√©l√©phonique. "
        "√âcris en fran√ßais professionnel, clair, concis et factuel. N'invente aucune information. "
        "Mentionne la formation souhait√©e, les prochaines dates disponibles, le financement, les "
        "conditions r√©glementaires et le rendez-vous uniquement lorsque ces √©l√©ments sont fournis. "
        "Omet toute donn√©e non renseign√©e."
    )
    user = (
        f"DONN√âES DE L'APPEL :\n{json.dumps(facts, ensure_ascii=False)}\n\n"
        f"FICHE FIABLE DE LA FORMATION :\n{json.dumps(context, ensure_ascii=False)}\n\n"
        f"R√âSUM√â D√âJ√Ä PROPOS√â :\n{current_summary}\n\n"
        f"PR√âCISIONS LIBRES DE LA SECR√âTAIRE :\n{precision}\n\n"
        "R√©dige le r√©sum√© final √† envoyer au CRM en un court paragraphe structur√©."
    )
    try:
        summary = _crm_ai(system, user, max_tokens=700)
        return jsonify({"ok": True, "summary": summary})
    except Exception:
        app.logger.exception("R√©sum√© IA de la demande du secr√©tariat indisponible")
        return jsonify({"ok": False, "error": "La reformulation IA est momentan√©ment indisponible. R√©essayez plus tard."}), 503


@app.route("/api/secretariat/calendly/appointment", methods=["POST"])
def api_secretariat_calendly_appointment():
    """Find the telephone booking made while the secretariat form is open."""
    payload = request.get_json(silent=True) or {}
    email = _crm_normalize_email(payload.get("email"))
    telephone = str(payload.get("telephone") or "").strip()
    if not email and not _crm_normalize_phone(telephone):
        return jsonify({"appointment": None})

    # Preview on a copy. The definitive CRM link is made only when the request
    # is submitted, so merely opening the summary never creates CRM records.
    data = copy.deepcopy(load_data())
    entry = {"email": email, "telephone": telephone}
    # Reuse the existing CRM contact when the caller is already known.  A
    # Calendly appointment can legitimately be linked only by ``contact_id``
    # (for example after a targeted CRM refresh), while its cached invitee
    # e-mail/phone is missing or differs from the values collected during the
    # call.  Using a synthetic id unconditionally made that valid link
    # invisible to the secretariat preview.
    contact = (
        _crm_calendly_contact_by_email(data, email)
        or _crm_calendly_contact_by_phone(data, telephone)
    )
    if not contact:
        contact = {
            "id": "secretariat-calendly-preview",
            "mail": email,
            "telephone": telephone,
            "formulaire": {},
        }
    _secretariat_refresh_calendly_appointments(data, entry, contact)
    appointment = _secretariat_hydrate_appointment_from_crm(data, entry, contact)
    if not appointment:
        return jsonify({"appointment": None})
    return jsonify({"appointment": {
        "date": entry.get("rdv_date"),
        "time": entry.get("rdv_time"),
        "mode": entry.get("rdv_mode"),
        "label": entry.get("rdv"),
        "name": appointment.get("name") or "Rendez-vous t√©l√©phonique",
    }})


@app.route("/api/secretariat/assistant", methods=["POST"])
def api_secretariat_assistant():
    """Compatibility endpoint for existing clients."""
    payload = request.get_json(silent=True) or {}
    formation_code = str(payload.get("formation", "")).strip()
    if formation_code not in SECRETARIAT_FORMATIONS:
        return jsonify({"ok": False, "error": "Formation inconnue."}), 400
    payload["question"] = payload.get("message", "")
    with app.test_request_context(json=payload):
        return api_secretariat_ai_question(formation_code)


@app.get("/inscriptions")
def inscriptions_vtc():
    """Parcours public d'inscription CPF r√©serv√© √† la formation VTC."""
    return render_template(
        "inscriptions_vtc.html",
        cpf_registration_url=VTC_CPF_REGISTRATION_URL,
    )


@app.route("/demande-informations-formations", methods=["GET", "POST"])
def demande_informations_formations():
    data_store = load_data()
    sessions = get_upcoming_formation_sessions(data_store)

    if request.method == "POST":
        form_data = request.form.to_dict()
        creer_piste_salesforce(request.form)
        form_data.update(_crm_google_ads_tracking_fields(form_data))

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
            "motif": "Demande d‚Äôinformations formations Int√©grale Academy",
            "details": json.dumps(form_data, ensure_ascii=False),
            "date": datetime.datetime.now(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M"),
            "statut": "Non trait√©",
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
        if form_data.get("source_secretariat") == "1":
            now_secretariat = datetime.datetime.now(pytz.timezone("Europe/Paris"))
            data_store.setdefault("secretariat_demandes", []).append({
                "id": str(uuid.uuid4()), "type": "formation",
                "formation": form_data.get("formation", ""),
                "nom": " ".join(filter(None, [demande_entry["prenom"], demande_entry["nom"]])),
                "telephone": demande_entry["telephone"], "email": demande_entry["mail"],
                "notes": form_data.get("commentaires_secretariat", "").strip(),
                "rdv": form_data.get("rdv_telephonique", "").strip(),
                "statut": "Trait√©" if form_data.get("rdv_telephonique") else "RDV √† prendre",
                "created_at": now_secretariat.isoformat(),
                "date": now_secretariat.strftime("%d/%m/%Y %H:%M"),
            })
        _supprimer_brouillon_formulaire_soumis(data_store, form_data)

        if form_data.get("souhaite_devis") != "OUI":
            rappel = {
                "id": str(uuid.uuid4()),
                "source_devis_id": demande_id,
                "nom": demande_entry["nom"],
                "prenom": demande_entry["prenom"],
                "telephone": demande_entry["telephone"],
                "mail": demande_entry["mail"],
                "motif": "personne √† rappeler",
                "details": (
                    "Demande d‚Äôinformations formations soumise.\n"
                    f"Formation : {form_data.get('formation', 'Non pr√©cis√©e')}\n"
                    f"Lieu : {form_data.get('centre', 'Non pr√©cis√©')}\n"
                    f"Date : {form_data.get('dates', 'Non pr√©cis√©e')}"
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
            "motif": "Demande de devis d√©taill√©",
            "details": json.dumps(devis_payload, ensure_ascii=False),
            "date": datetime.datetime.now(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M"),
            "statut": "Non trait√©",
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
        crm_contact = _crm_create_contact_from_information_request(
            data_store, form_data, demande_id, devis_id, devis_url
        )
        extra_devis = f"""
        <p style="margin-top:16px;">Vous pouvez t√©l√©charger votre devis d√©taill√© en cliquant ici :</p>
        <p style="text-align:center;"><a href="{devis_url}" style="display:inline-block;padding:12px 18px;background:#0d6efd;color:#fff;border-radius:10px;text-decoration:none;font-weight:700;">Je t√©l√©charge mon devis d√©taill√©</a></p>
        """

        extra_identite = ""
        if form_data.get("identite_numerique") == "NON":
            extra_identite = """
            <p>Pour utiliser votre Compte Personnel de Formation, vous devez cr√©er votre ¬´ Identit√© Num√©rique la Poste ¬ª (FranceConnect+). Vous pouvez cr√©er votre Identit√© Num√©rique la Poste directement dans un bureau de Poste ou sur le site internet officiel <a href="https://lidentitenumerique.laposte.fr/">https://lidentitenumerique.laposte.fr/</a>.</p>
            """

        save_data(data_store)

        if form_data.get("formation") == "DESP_VAE":
            plain = (
                f"Bonjour {prenom},\n\n"
                "Je fais suite √† votre demande de renseignements concernant notre VAE Dirigeant d‚ÄôEntreprise de S√©curit√© Priv√©e (RNCP40385).\n"
                "Dossier de pr√©sentation : https://www.integraleacademy.com/dossiersfc\n"
                f"T√©l√©charger votre devis d√©taill√© : {devis_url}\n"
                "D√©marrer votre VAE : https://gestionstagiaires-r5no.onrender.com/vae-desp\n"
                "Planifier un rendez-vous : https://calendly.com/integraleacademy/dirigeant\n\n"
                "Je reste √† votre disposition pour tout renseignement compl√©mentaire.\n\n"
                "Cl√©ment VAILLANT\nDirecteur ‚Äì Int√©grale Academy"
            )
            html = build_vae_desp_email_html(prenom, devis_url)
            email_subject = "üìù VAE ‚Äì Dirigeant d‚ÄôEntreprise de S√©curit√© Priv√©e (RNCP40385)"
        elif form_data.get("formation") == "A3P":
            email_subject, plain, html = _a3p_information_email_content(
                prenom, form_data.get("dates", ""), form_data.get("centre", ""), devis_url,
            )
        elif form_data.get("formation") == "APS":
            session_date = _format_selected_session_date(form_data.get("dates", ""))
            centre_label, centre_address = _centre_label_and_address(form_data.get("centre", ""))
            plain = (
                f"Bonjour {prenom},\n\n"
                "Voici les informations d√©taill√©es concernant notre formation Agent de S√©curit√© Priv√©e (APS).\n"
                + (f"Session : {session_date}\n" if session_date else "")
                + f"Lieu : {centre_label} ‚Äî {centre_address}\n"
                "Tarif : 1 650 ‚Ç¨ TTC.\n"
                "Dur√©e : 175 heures sur 5 semaines.\n\n"
                "Dossier de pr√©sentation : https://www.integraleacademy.com/dossiersfc\n"
                f"Devis d√©taill√© : {devis_url}\n"
                "Identit√© num√©rique La Poste : https://lidentitenumerique.laposte.fr\n"
                "Contact : 04 22 47 07 68\n\n"
                "Cl√©ment VAILLANT\nDirecteur ‚Äì Int√©grale Academy"
            )
            html = build_aps_email_html(prenom, form_data.get("dates", ""), form_data.get("centre", ""), devis_url)
            email_subject = "üëÆ‚Äç‚ôÇÔ∏è Formation Agent de S√©curit√© Priv√©e (APS)"
        elif form_data.get("formation") == "SSIAP":
            session_date = _format_selected_session_date(form_data.get("dates", ""))
            tarif_ssiap = get_formation_tarif("SSIAP", form_data)
            secourisme_info = (
                "Un certificat SST valide ou un PSC1 de moins de 2 ans reste requis."
                if form_data.get("ssiap_secourisme_valide") == "NON"
                else "Tarif applicable avec un certificat SST ou PSC1 de moins de 2 ans."
            )
            plain = (
                f"Bonjour {prenom},\n\n"
                "Voici les informations concernant notre formation Agent de s√©curit√© incendie SSIAP 1.\n"
                + (f"Session : {session_date}\n" if session_date else "")
                + "Examen : 28 octobre 2026\n"
                "Lieu : Int√©grale Academy C√¥te d‚ÄôAzur ‚Äî 54 chemin du Carreou, 83480 Puget-sur-Argens.\n"
                f"Tarif : {tarif_ssiap} ‚Ç¨ TTC. {secourisme_info}\n\n"
                f"Devis d√©taill√© : {devis_url}\n"
                "Prendre rendez-vous : https://calendly.com/integraleacademy/ssiap1\n"
                "Contact : 04 22 47 07 68\n\n"
                "Cl√©ment VAILLANT\nDirecteur ‚Äì Int√©grale Academy"
            )
            html = build_ssiap1_email_html(
                prenom,
                form_data.get("dates", ""),
                form_data.get("centre", ""),
                devis_url,
                form_data.get("ssiap_secourisme_valide", ""),
            )
            email_subject = "üî• Formation Agent de s√©curit√© incendie SSIAP 1"
        elif form_data.get("formation") == "VTC":
            plain = (
                f"Bonjour {prenom},\n\n"
                "Pour faire suite √† votre demande de renseignements, nous vous prions de bien vouloir trouver ci-dessous l‚Äôensemble des informations d√©taill√©es concernant notre formation Chauffeur VTC.\n\n"
                "Organisation de la formation :\n"
                "- Th√©orie 100 % en ligne, accessible 7j/7.\n"
                "- Pratique : 1/2 journ√©e √† Puget-sur-Argens (83).\n\n"
                "D√©roulement : acc√®s e-learning √† l'inscription, examen th√©orique Chambre des M√©tiers, puis pratique et examen pratique (La Valette-du-Var ou Nice selon r√©sidence).\n\n"
                "Pr√©requis : permis B depuis plus de 3 ans et casier judiciaire vierge.\n"
                "Tarif : 1 650 ‚Ç¨ TTC (tout inclus).\n"
                "Dur√©e : th√©orie ~100h + pratique 1/2 journ√©e.\n\n"
                "Dossier : https://www.integraleacademy.com/dossiersfc\n"
                "Dates examens : https://www.cmar-paca.fr/galerie/1/f3ec5a86ea34eb95294dd770b94b8c23.pdf\n"
                "Programme : https://www.integraleacademy.com/dossiersfc\n"
                "Agr√©ment VTC : https://www.integraleacademy.com/_files/ugd/008e7b_0e29b04a71dd4dcc9c0f266d28f0514b.pdf\n"
                "Prendre rendez-vous : https://calendly.com/integraleacademy/chauffeurvtc\n\n"
                "Cl√©ment VAILLANT\nDirecteur ‚Äì Int√©grale Academy"
            )
            html = build_vtc_email_html(prenom, form_data.get("centre", ""), devis_url)
            email_subject = "üöó Formation Chauffeur VTC"
        elif form_data.get("formation") == "DESP_INIT":
            session_date = _format_selected_session_date(form_data.get("dates", ""))
            exam_label = _extract_exam_label_from_dates_txt(form_data.get("dates", ""))
            centre_label, _ = _centre_label_and_address(form_data.get("centre", ""))
            plain = (
                f"Bonjour {prenom},\n\n"
                "Je fais suite √† votre demande de renseignements concernant notre formation Dirigeant d‚ÄôEntreprise de S√©curit√© Priv√©e (DESP), titre reconnu par l‚Äô√âtat (RNCP40385 ‚Äì niveau 5, √©quivalent Bac+2).\n"
                "Cette formation permet d‚Äôobtenir les comp√©tences indispensables pour cr√©er, diriger et g√©rer une entreprise de s√©curit√© priv√©e et vous permet de demander votre agr√©ment dirigeant aupr√®s du CNAPS conform√©ment √† la r√©glementation.\n\n"
                "Dossier de pr√©sentation : https://www.integraleacademy.com/dossiersfc\n\n"
                "Dur√©e et organisation : 245 heures (175 heures de e-learning √† distance + 70 heures de pr√©sentiel sur 2 semaines).\n"
                "Le e-learning est accessible 24h/24 sur ordinateur, tablette ou smartphone.\n\n"
                "Prochaines formations :\n"
                + (f"- {session_date}\n" if session_date else "- XXXX\n")
                + f"Examen : {exam_label or 'XXXXX'}\n"
                + f"Centre : {centre_label}\n\n"
                "Tarif : 4 300 ‚Ç¨ TTC (finan√ßable via CPF).\n"
                "Identit√© Num√©rique La Poste (obligatoire CPF) : https://lidentitenumerique.laposte.fr/\n"
                f"Devis d√©taill√© : {devis_url}\n\n"
                "Planifier un rendez-vous : https://calendly.com/integraleacademy/dirigeant\n\n"
                "Je reste √† votre disposition pour tous renseignements compl√©mentaires.\n"
                "Je vous souhaite une excellente journ√©e.\n\n"
                "Cl√©ment VAILLANT\nDirecteur Int√©grale Group"
            )
            html = build_desp_init_email_html(prenom, form_data.get("dates", ""), form_data.get("centre", ""), devis_url)
            email_subject = "Votre demande de renseignements ‚Äì Formation DESP initial"
        else:
            plain = (
                f"Bonjour {prenom},\n\n"
                f"Je fais suite √† votre demande de renseignements concernant notre formation {formation_label}. Nous vous remercions de nous avoir contact√© !\n\n"
                "Un conseiller formation reviendra vers vous prochainement pour vous accompagner dans votre projet de formation.\n\n"
                "Vous pouvez √©galement nous contacter au 04 22 47 07 68 pour √©changer avec notre √©quipe.\n\n"
                + f"Vous pouvez t√©l√©charger votre devis d√©taill√© ici : {devis_url}\n\n"
                + ("Pour utiliser votre Compte Personnel de Formation, vous devez cr√©er votre Identit√© Num√©rique La Poste : https://lidentitenumerique.laposte.fr/\n\n" if form_data.get("identite_numerique") == "NON" else "")
                + "Je vous souhaite une bonne journ√©e,\n\n"
                "Cl√©ment VAILLANT\nDirecteur Int√©grale Academy"
            )

            html = _wrap_html(
                "<h1>‚ú® Merci pour votre demande</h1>",
                f"""
                <p>Bonjour <strong>{prenom}</strong>,</p>
                <p>Je fais suite √† votre demande de renseignements concernant notre formation <strong>{formation_label}</strong>. Nous vous remercions de nous avoir contact√© !</p>
                <p>Un conseiller formation reviendra vers vous prochainement pour vous accompagner dans votre projet de formation.</p>
                <p>Vous pouvez √©galement nous contacter au <strong>04 22 47 07 68</strong> pour √©changer avec notre √©quipe.</p>
                {extra_devis}
                {extra_identite}
                <p>Je vous souhaite une bonne journ√©e,</p>
                <p><strong>Cl√©ment VAILLANT</strong><br>Directeur Int√©grale Academy</p>
                """
            )
            email_subject = "Votre demande de renseignements ‚Äì Int√©grale Academy"

        demande_entry["mail_contenu"] = plain
        demande_entry["mail_html"] = html
        try:
            email_sent = send_email_html(form_data.get("mail"), email_subject, plain, html)
        except Exception as e:
            print("‚ùå Erreur inattendue envoi email demande informations formations :", e)
            email_sent = False

        if email_sent:
            demande_entry["mail_confirme"] = datetime.datetime.now(
                pytz.timezone("Europe/Paris")
            ).strftime("%d/%m/%Y %H:%M")
            demande_entry["mail_erreur"] = ""
        else:
            demande_entry["mail_erreur"] = "‚ùå Erreur lors de l'envoi automatique du mail"

        _crm_activity(
            crm_contact,
            "email" if email_sent else "erreur",
            "E-mail automatique envoy√©" if email_sent else "√âchec de l‚Äôe-mail automatique",
            email_subject,
            html,
        )

        try:
            sms_sent = envoyer_sms_demande_infos_formation(demande_entry, form_data)
            _crm_activity(
                crm_contact,
                "sms" if sms_sent else "erreur",
                "SMS automatique envoy√©" if sms_sent else "√âchec du SMS automatique",
                build_training_information_sms_text(form_data.get("formation")),
            )
        except Exception as e:
            print("‚ùå Erreur envoi SMS demande informations formations :", e)
            demande_entry["sms_error"] = datetime.datetime.now(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M")
            _crm_activity(crm_contact, "erreur", "√âchec du SMS automatique", str(e))
        crm_contact["updated_at"] = _crm_now()
        save_data(data_store)

        if prospect_chaud:
            try:
                send_email_html(
                    "clement@integraleacademy.com",
                    "üî• Prospect CHAUD ‚Äî Demande d‚Äôinformations formations",
                    f"Prospect chaud: {demande_entry['prenom']} {demande_entry['nom']} ({demande_entry['mail']})",
                    _wrap_html("<h1>üî• Prospect CHAUD</h1>", f"<p>{demande_entry['prenom']} {demande_entry['nom']} ‚Äî {formation_label}</p><p>Email : {demande_entry['mail']}</p>")
                )
            except:
                pass

        if form_data.get("formation") == "DESP_VAE":
            return redirect("https://gestionstagiaires-r5no.onrender.com/vae-desp")

        return redirect(url_for("confirmation_demande_infos", hot="1" if prospect_chaud else "0", formation=form_data.get("formation", "")))

    google_ads_attribution = _crm_google_ads_tracking_fields(request.args)
    return render_template(
        "demande_informations_formations.html",
        sessions=sessions,
        formations=PLAN_FORMATIONS,
        secretariat=request.args.get("secretariat") == "1",
        **google_ads_attribution,
    )




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
        _declencher_relance_formulaire_abandonne(data, draft_entry, cleaned_fields, now_str)

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
        print("=== /api/chat appel√© ===", flush=True)

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            print("ERREUR: OPENAI_API_KEY absente", flush=True)
            return jsonify({"reply": "D√©sol√©, l‚ÄôIA est momentan√©ment indisponible."}), 200

        data = request.get_json(silent=True) or {}
        message = (data.get("message") or "").strip()
        print("Message re√ßu:", message, flush=True)

        if not message:
            return jsonify({"reply": "Veuillez √©crire une question."}), 200

        from openai import OpenAI
        client = OpenAI(api_key=api_key, timeout=20)

        print("Appel OpenAI en cours...", flush=True)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "Tu es l'assistant officiel d‚ÄôInt√©grale Academy. Tu r√©ponds aux prospects sur les formations APS, A3P, A3P garde du corps, VTC, Dirigeant, VAE, BTS, financement, devis, inscription, CNAPS et modalit√©s. Tu r√©ponds en fran√ßais, de fa√ßon claire, rassurante et commerciale. Tu incites naturellement √† compl√©ter le formulaire de demande d‚Äôinformations pr√©sent sur la page. Si on te demande une date pr√©cise que tu ne connais pas, tu invites √† compl√©ter le formulaire pour recevoir les prochaines dates."
                },
                {
                    "role": "user",
                    "content": message
                }
            ],
            max_tokens=400,
            temperature=0.4
        )
        print("Appel OpenAI termin√©", flush=True)

        reply = ""
        if getattr(response, "choices", None) and response.choices[0].message:
            reply = response.choices[0].message.content or ""
        if not reply:
            reply = "D√©sol√©, je n‚Äôai pas pu g√©n√©rer de r√©ponse."
        print("R√©ponse IA OK", flush=True)

        return jsonify({"reply": reply}), 200

    except Exception as e:
        print("ERREUR /api/chat:", str(e), flush=True)
        print(traceback.format_exc(), flush=True)
        return jsonify({"reply": "D√©sol√©, l‚ÄôIA est momentan√©ment indisponible."}), 200


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
        if d.get("motif") == "Demande de devis d√©taill√©":

            # üîß Parsing s√©curis√© du JSON "details"
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



POEI_DETAIL_QUESTIONS = [
    ("Formation", "Formation demand√©e"),
    ("Dates", "Quelles sont les dates de la formation ?"),
    ("Lieu de formation", "O√π se d√©roule la formation ?"),
    ("Poste vis√©", "Quel est le poste vis√© √† l‚Äôissue de la formation ?"),
    ("Contrat pr√©vu", "Quel contrat est pr√©vu apr√®s la formation ?"),
    ("Ville de r√©sidence", "Quelle est votre ville de r√©sidence ?"),
    ("Permis B", "Avez-vous le permis B ?"),
    ("Disponible formation", "√ätes-vous disponible du 23/09 au 22/12/2026 ?"),
    ("Mobilit√© Cannes", "Pouvez-vous travailler √† Cannes ensuite ?"),
    ("Inscrit France Travail", "√ätes-vous inscrit √† France Travail ?"),
    ("Identifiant France Travail", "Quel est votre identifiant France Travail ?"),
    (
        "Confirmation CNAPS",
        "Confirmez-vous ne pas avoir d‚Äôant√©c√©dents judiciaires incompatibles avec le m√©tier d‚Äôagent de s√©curit√© ?",
    ),
    (
        "Consentement recontact",
        "Confirmez-vous √™tre inscrit √† France Travail et accepter d‚Äô√™tre recontact√© dans le cadre de votre candidature ?",
    ),
    ("Message / motivation", "Quel est votre message / votre motivation ?"),
]


def _poei_detail_question_rows(details):
    rows = []
    used_keys = set()
    for key, question in POEI_DETAIL_QUESTIONS:
        rows.append({"question": question, "value": details.get(key, "‚Äî") or "‚Äî"})
        used_keys.add(key)
    for key in sorted(k for k in details if k not in used_keys):
        rows.append({"question": key, "value": details.get(key, "‚Äî") or "‚Äî"})
    return rows


def _poei_candidature_details(demande):
    details = demande.get("details_data")
    if not isinstance(details, dict):
        try:
            details = json.loads(demande.get("details", "{}"))
        except Exception:
            details = {}
    return details


def _poei_find_candidature(data, candidature_id):
    for demande in data.get("demandes", []):
        if demande.get("id") == candidature_id and demande.get("source") == "poei_agent_securite_cannes":
            return demande
    return None


def _poei_now():
    return datetime.datetime.now(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M")


@app.route("/admin-devis/poei")
@login_required
def admin_devis_poei():
    data = load_data()
    candidatures = []
    for demande in data.get("demandes", []):
        if demande.get("source") != "poei_agent_securite_cannes":
            continue
        item = dict(demande)
        item["details"] = _poei_candidature_details(demande)
        item["detail_questions"] = _poei_detail_question_rows(item["details"])
        candidatures.append(item)

    candidatures.reverse()
    stats = {
        "total": len(candidatures),
        "non_traites": sum(1 for c in candidatures if (c.get("statut") or "Non trait√©") == "Non trait√©"),
        "appeles": sum(1 for c in candidatures if c.get("appel_effectue")),
    }
    return render_template("admin_devis_poei.html", candidatures=candidatures, stats=stats)


@app.route("/admin-devis/poei/<candidature_id>", methods=["PATCH", "POST"])
@login_required
def admin_devis_poei_update(candidature_id):
    data = load_data()
    candidature = _poei_find_candidature(data, candidature_id)
    if candidature is None:
        abort(404)

    form_data = request.form or (request.get_json(silent=True) or {})
    previous_appel = bool(candidature.get("appel_effectue"))
    appel_value = form_data.get("appel_effectue")

    if "statut" in form_data:
        candidature["statut"] = (form_data.get("statut") or "Non trait√©").strip() or "Non trait√©"
    if "commentaire_suivi" in form_data:
        candidature["commentaire_suivi"] = (form_data.get("commentaire_suivi") or "").strip()
    if "prochaine_action" in form_data:
        candidature["prochaine_action"] = (form_data.get("prochaine_action") or "").strip()
    if "rappel_date" in form_data:
        candidature["rappel_date"] = (form_data.get("rappel_date") or "").strip()
    if appel_value is not None:
        candidature["appel_effectue"] = str(appel_value).lower() in {"1", "true", "on", "oui", "yes"}
        if candidature["appel_effectue"] and not previous_appel:
            candidature["date_appel"] = _poei_now()
        elif not candidature["appel_effectue"]:
            candidature["date_appel"] = ""

    candidature["suivi_modifie"] = _poei_now()
    save_data(data)
    return jsonify({
        "ok": True,
        "date_appel": candidature.get("date_appel", ""),
        "suivi_modifie": candidature.get("suivi_modifie", ""),
    })


@app.route("/admin-devis/poei/<candidature_id>/supprimer", methods=["POST", "DELETE"])
@login_required
def admin_devis_poei_delete(candidature_id):
    data = load_data()
    demandes = data.get("demandes", [])
    candidature = _poei_find_candidature(data, candidature_id)
    if candidature is None:
        abort(404)
    data["demandes"] = [d for d in demandes if d.get("id") != candidature_id]
    supprimer_fichiers_demande(candidature)
    save_data(data)
    if request.method == "DELETE" or request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True})
    return redirect(url_for("admin_devis_poei"))


@app.route("/formulaire-a-rappeler", methods=["GET", "POST"])
def formulaire_a_rappeler():
    return redirect(url_for("secretariat"))


def _rappel_est_traite(rappel):
    statut = str(rappel.get("statut") or "").strip().lower()
    return bool(rappel.get("traite")) or statut in {"traite", "trait√©"}


def _normaliser_rappel_telephone(rappel):
    """Garde le bool√©en `traite` et le libell√© `statut` synchronis√©s."""
    rappel["traite"] = _rappel_est_traite(rappel)
    rappel["statut"] = "Trait√©" if rappel["traite"] else "A rappeler"
    return rappel


@app.route("/admin-devis/rappels", methods=["GET"])
@login_required
def admin_devis_rappels():
    if not can_manage_admin_devis_rappels():
        return jsonify({"ok": False, "error": "forbidden"}), 403

    data = load_data()
    rappels = [d for d in data.get("demandes", []) if d.get("motif") == "Formulaire √† rappeler"]
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
        "motif": "Formulaire √† rappeler",
        "details": "Cr√©√© depuis l'admin devis",
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
        print("‚ö†Ô∏è Erreur envoi mail formulaire rappel admin:", e)
    return jsonify({"ok": True, "item": entry})


@app.route("/admin-devis/rappels/<rappel_id>", methods=["PATCH", "POST"])
@login_required
def update_admin_devis_rappel(rappel_id):
    if not can_manage_admin_devis_rappels():
        return jsonify({"ok": False, "error": "forbidden"}), 403

    data = load_data()
    payload = request.get_json(silent=True) or {}
    rappel = next((d for d in data.get("demandes", []) if d.get("id") == rappel_id and d.get("motif") == "Formulaire √† rappeler"), None)
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
            demande.get("motif") == "Formulaire √† rappeler"
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
    data["demandes"] = [d for d in data.get("demandes", []) if not (d.get("id") == rappel_id and d.get("motif") == "Formulaire √† rappeler")]
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
        if d.get("motif") == "Demande de devis d√©taill√©" and d.get("source_demande_infos_id")
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

        source_label = "Devis d√©taill√©"
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
            "statut": d.get("statut", "Non trait√©"),
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
        if statut == "trait√©":
            stats["treated"] += 1
        if statut in {"a traiter", "√† traiter", "non trait√©", "non traite"}:
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
    if raw_statut in {"traite", "trait√©"}:
        nouveau_statut = "Trait√©"
    elif raw_statut in {"a traiter", "√† traiter", "non traite", "non trait√©"}:
        nouveau_statut = "Non trait√©"
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
            "source_label": "Formulaire abandonn√©",
            "statut": "Abandonn√©",
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
        flash("Impossible de relancer : nom, pr√©nom, email et t√©l√©phone sont n√©cessaires.", "error")
        return redirect(url_for("admin_devis_formulaires_abandonnes"))

    if draft:
        draft["manual_abandoned_sent_at"] = now_str
        _declencher_relance_formulaire_abandonne(data, draft, fields, now_str)
        mail_ok = bool(draft.get("abandoned_mail_sent_at"))
        sms_ok = bool(draft.get("abandoned_sms_sent_at"))
    else:
        _crm_create_or_match_abandoned_form_contact(data, demande, fields, now_str)
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
        flash(f"Mail et SMS formulaire abandonn√© envoy√©s, piste Salesforce cr√©√©e pour {nom_affiche}.", "success")
    elif mail_ok:
        flash(f"Mail formulaire abandonn√© envoy√© et piste Salesforce cr√©√©e pour {nom_affiche}, mais l'envoi du SMS a √©chou√©.", "error")
    elif sms_ok:
        flash(f"SMS formulaire abandonn√© envoy√© et piste Salesforce cr√©√©e pour {nom_affiche}, mais l'envoi du mail a √©chou√©.", "error")
    else:
        flash(f"Piste Salesforce cr√©√©e pour {nom_affiche}, mais l'envoi du mail et du SMS a √©chou√©.", "error")
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

    # 2) Formulaires abandonn√©s dans formulaires_abandonnes
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
            "motif": "Demande de devis d√©taill√©",
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
        "wbraid": "WBRAID Google Ads",
        "gbraid": "GBRAID Google Ads",
        "gad_source": "Source Google Ads",
        "gad_campaignid": "Campagne Google Ads",
        "utm_source": "Source UTM",
        "utm_medium": "Support UTM",
        "utm_campaign": "Campagne UTM",
        "nom": "Nom",
        "prenom": "Pr√©nom",
        "mail": "Mail",
        "mail_confirm": "Mail confirm",
        "telephone": "T√©l√©phone",
        "formation": "Formation souhait√©e",
        "centre": "Lieu de formation souhait√©",
        "centre_formation": "Lieu de formation souhait√©",
        "dates": "Dates de formation souhait√©es",
        "cpf_consulte": "CPF consult√©",
        "cpf_montant": "Montant CPF",
        "france_travail": "Inscrit France Travail",
        "ft_refus_ok": "Refus France Travail",
        "financement_perso": "Financement personnel",
        "identite_numerique": "Identit√© num√©rique",
        "cnaps_ok": "CNAPS valid√©",
        "garde_vue": "Garde √† vue",
        "titre_sejour": "Titre de s√©jour",
    }

    categories = {
        "infos_generales": {"title": "Informations g√©n√©rales", "rows": []},
        "formation_souhaitee": {"title": "Formation souhait√©e", "rows": []},
        "lieu_formation": {"title": "Lieu de formation souhait√©", "rows": []},
        "dates_formation": {"title": "Dates de formation souhait√©es", "rows": []},
        "financement": {"title": "Financement de votre formation", "rows": []},
        "situation": {"title": "Votre situation", "rows": []},
        "autres": {"title": "Autres informations", "rows": []},
    }

    def category_for_key(normalized_key):
        if normalized_key in {
            "nom", "prenom", "mail", "mail_confirm", "telephone", "gclid",
            "wbraid", "gbraid", "gad_source", "gad_campaignid", "utm_source",
            "utm_medium", "utm_campaign",
        }:
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

    source_label = "Devis d√©taill√©"
    if is_abandoned:
        source_label = "Formulaire abandonn√©"
    elif demande.get("source") == "demande_infos_formations":
        source_label = "Infos formations"

    formation_value = str(
        infos.get("formation")
        or infos.get("Formation")
        or infos.get("formation_souhaitee")
        or infos.get("formation souhait√©e")
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
        "dirigeant d'entreprise de s√©curit√©",
        "dirigeant d‚Äôentreprise de s√©curit√©",
        "dirigeant d'entreprise de s√©curit√© priv√©e (desp)",
        "dirigeant d‚Äôentreprise de s√©curit√© priv√©e (desp)"
    }:
        formation_badge_text = "DIRIGEANT"
        formation_badge_theme = "dirigeant"
    elif formation_normalized in {
        "desp_vae",
        "vae",
        "dirigeant d'entreprise de s√©curit√© (desp) ‚Äì vae",
        "dirigeant d‚Äôentreprise de s√©curit√© (desp) ‚Äì vae",
        "dirigeant d‚Äôentreprise de s√©curit√© priv√©e (desp) en vae",
        "dirigeant d'entreprise de s√©curit√© priv√©e (desp) en vae"
    }:
        formation_badge_text = "VAE"
        formation_badge_theme = "vae"
    elif formation_normalized in {"vtc", "chauffeur vtc"}:
        formation_badge_text = "VTC"
        formation_badge_theme = "vtc"
    elif formation_normalized in {"aps", "agent de pr√©vention et de s√©curit√© (aps)", "agent de pr√©vention et de s√©curit√©"}:
        formation_badge_text = "APS"
        formation_badge_theme = "aps"

    badge_text = "INCONNU"
    campus_theme = "aurillac"
    centre_normalized = (centre_value or "").strip().lower()

    if formation_badge_theme == "vae":
        badge_text = "√Ä DISTANCE"
        campus_theme = "distance"
    else:
        if centre_normalized in {"cote d'azur", "c√¥te d'azur", "paca", "nice"}:
            badge_text = "C√îTE D'AZUR"
            campus_theme = "cote-azur"
        elif centre_normalized in {"auvergne", "clermont", "clermont-ferrand"}:
            badge_text = "AUVERGNE"
            campus_theme = "auvergne"
        elif centre_normalized in {"ile-de-france", "√Æle-de-france", "idf", "paris"}:
            badge_text = "√éLE-DE-FRANCE"
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
        resultat = "Profil √† √©tudier"

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
        "motif": "Simulation √©ligibilit√© VAE DESP",
        "source": "simulateur_vae_desp",
        "details": json.dumps(details, ensure_ascii=False),
        "date": datetime.datetime.now(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M"),
        "statut": "Non trait√©",
    })
    contact = _crm_create_contact_from_vae_simulation(
        data, nom, prenom, mail, telephone, reponses, score, resultat,
    )
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

    return jsonify({"ok": True, "score": score, "resultat": resultat,
                    "crm_contact_id": contact.get("id") if contact else None})

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
        return {"ok": False, "message": "Un email est n√©cessaire pour l'envoi."}, 400

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
    subject = "üìÑ Votre plan de financement d√©taill√© ‚Äî Int√©grale Academy"
    plain = (
        f"Bonjour {nom_affiche},\n\n"
        "Voici votre plan de financement d√©taill√© en consultation :\n"
        f"{plan_url}\n\n"
        "Ce lien est consultatif (aucune modification n'est possible).\n\n"
        "Bien cordialement,\n"
        "Int√©grale Academy"
    )
    html = _wrap_html(
        "<h1>üìÑ Votre plan de financement d√©taill√©</h1>",
        f"""
        <p>Bonjour <strong>{nom_affiche}</strong>,</p>
        <p>Vous trouverez ci-dessous votre plan de financement d√©taill√© en <strong>consultation uniquement</strong>.</p>
        <p style=\"text-align:center;margin:24px 0;\">
          <a href=\"{plan_url}\" style=\"display:inline-block;padding:14px 26px;background:#0d6efd;color:white;text-decoration:none;border-radius:8px;font-weight:700;\">
            üëâ Consulter le plan de financement
          </a>
        </p>
        <p style=\"font-size:13px;color:#666;margin:0;\">Ce lien est personnel et ne permet aucune modification.</p>
        """
    )

    send_email_html(destinataire, subject, plain, html)
    return {"ok": True, "message": "Plan envoy√© avec succ√®s."}


@app.route("/admin-devis/toggle/<devis_id>", methods=["POST"])
@login_required
def toggle_devis(devis_id):
    data = load_data()

    for d in data.get("demandes", []):
        if d.get("id") == devis_id and d.get("motif") == "Demande de devis d√©taill√©":
            ancien_statut = d.get("statut_devis") or "A envoyer"

            if ancien_statut == "Envoy√©":
                d["statut_devis"] = "A envoyer"
            else:
                d["statut_devis"] = "Envoy√©"

                # üìû Notification Mohamed uniquement au clic sur "Changer le statut"
                # depuis un devis "√Ä envoyer" => "Envoy√©"
                rappel_existant = next(
                    (
                        x for x in data.get("demandes", [])
                        if x.get("source_devis_id") == devis_id
                        and x.get("motif") == "Rappel suite devis envoy√©"
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
                        "A3P": "A3P ‚Äì Agent de Protection Physique des Personnes",
                        "APS": "APS ‚Äì Agent de Pr√©vention et de S√©curit√©",
                        "VTC": "VTC ‚Äì Chauffeur de transport avec chauffeur",
                        "DESP_INIT": "DESP ‚Äì Dirigeant d‚Äôentreprise de s√©curit√© (initial)",
                        "DESP_VAE": "DESP ‚Äì Dirigeant d‚Äôentreprise de s√©curit√© (VAE)"
                    }.get(formation, formation)

                    demande_rappel_devis = {
                        "id": str(uuid.uuid4()),
                        "source_devis_id": devis_id,
                        "nom": d.get("nom"),
                        "prenom": d.get("prenom"),
                        "telephone": d.get("telephone"),
                        "mail": d.get("mail"),
                        "motif": "Rappel suite devis envoy√©",
                        "details": (
                            "Cr√©√©e automatiquement apr√®s clic sur ‚ÄòChanger le statut‚Äô (√Ä envoyer ‚Üí Envoy√©).\n"
                            f"Formation : {formation_label or 'Non pr√©cis√©e'}\n"
                            f"Session : {(infos.get('dates') or 'Non pr√©cis√©e')}"
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
         and d.get("motif") == "Demande de devis d√©taill√©"),
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
         and d.get("motif") == "Demande de devis d√©taill√©"),
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

    # ‚úÖ Recherche dans archives
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
    """Supprime les fichiers g√©n√©r√©s associ√©s √† un devis."""
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
         if d.get("id") == devis_id and d.get("motif") == "Demande de devis d√©taill√©"),
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
        if demande.get("motif") == "Demande de devis d√©taill√©":
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
        demande["statut"] = "Non trait√©"

        save_data(data)
        return render_template("merci_reponse.html", demande=demande)

    return render_template("repondre.html", demande=demande)

@app.route("/uploads/<filename>")
def download_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

# ------------------------------------------------------------
# ‚úÖ Route publique pour la plateforme principale (suivi assistance)
# ------------------------------------------------------------
@app.route("/data.json")
def data_json():
    """
    Retourne le nombre de demandes √† traiter (statut 'A TRAITER' ou 'Non trait√©')
    """
    try:
        data = load_data()
        demandes = data.get("demandes", [])
        a_traiter = [d for d in demandes if d.get("statut", "").strip().lower() in ["a traiter", "non trait√©"]]
        count = len(a_traiter)

        headers = {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*"
        }
        return {"a_traiter": count}, 200, headers

    except Exception as e:
        print("‚ö†Ô∏è Erreur /data.json :", e)
        return {"a_traiter": -1, "error": str(e)}, 500, {
            "Access-Control-Allow-Origin": "*"
        }
# ------------------------------------------------------------
# üß© PAGE : Demande de rappel t√©l√©phonique
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

        # üíæ Enregistrement dans data.json
        new_demande = {
            "id": str(uuid.uuid4()),
            "nom": nom,
            "prenom": prenom,
            "telephone": telephone,
            "mail": mail,
            "motif": f"Demande de rappel ‚Äì {formation}",
            "details": f"Cr√©√©e via le formulaire de rappel.\nPr√©f√©rence horaire : {plage}\n{commentaire}",
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

        # üì® Accus√© de r√©ception au candidat
        try:
            sujet = "üìû Nous avons bien re√ßu votre demande de rappel"
            plain = (
                f"Bonjour {prenom},\n\n"
                f"Nous avons bien re√ßu votre demande de rappel concernant la formation : {formation}.\n"
                f"Notre √©quipe vous contactera prochainement au {telephone}.\n\n"
                "Merci pour votre int√©r√™t et √† tr√®s bient√¥t !\n"
                "‚Äî L'√©quipe Int√©grale Academy"
            )

            html = _wrap_html(
                '<h1 style="margin:0 0 12px;font-size:20px;">üìû Demande de rappel re√ßue</h1>',
                f"""
                <p>Bonjour <strong>{prenom}</strong>,</p>
                <p>Nous avons bien re√ßu votre demande de rappel concernant la formation :</p>
                <p><strong>{formation}</strong></p>
                <p>Notre √©quipe vous contactera prochainement au <strong>{telephone}</strong>.</p>
                <p style="margin-top:10px;">Merci pour votre int√©r√™t et √† tr√®s bient√¥t !<br>‚Äî L'√©quipe Int√©grale Academy</p>
                """
            )
            send_email_html(mail, sujet, plain, html)
        except Exception as e:
            print("‚ö†Ô∏è Erreur envoi mail rappel :", e)

        # üì® Notification interne √† Mohamed
        try:
            envoyer_mail_attribution_mohamed(new_demande)
        except Exception as e:
            print("‚ö†Ô∏è Erreur envoi mail Mohamed :", e)

        # üîÅ Redirections automatiques selon la formation choisie
        if formation == "Chauffeur VTC":
            return redirect("https://www.integraleacademy.com/rdvvtc")
        elif formation == "Dirigeant d'entreprise de s√©curit√© priv√©e (DESP)":
            return redirect("https://www.integraleacademy.com/rdvconfirmedirigeant")
        else:
            return render_template("confirmation.html")

    return render_template("rappel.html")

HEBERGEMENT_ADDRESS = "Int√©grale Academy, 54 chemin du Carreou, 83480 Puget-sur-Argens"
_HEBERGEMENT_WEEKDAYS = (
    "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche",
)
_HEBERGEMENT_MONTHS = (
    "janvier", "f√©vrier", "mars", "avril", "mai", "juin",
    "juillet", "ao√ªt", "septembre", "octobre", "novembre", "d√©cembre",
)
HEBERGEMENT_CONVENTION_VERSION = 2
HEBERGEMENT_YOUSIGN_SIGNATURE_FIELD = {
    "page": 11,
    "x": 352,
    "y": 655,
    "width": 168,
    "height": 42,
}
HEBERGEMENT_YOUSIGN_ACTIVE_STATUSES = {"draft", "approval", "ongoing"}
HEBERGEMENT_YOUSIGN_SIGNED_STATUSES = {"done", "signed"}
HEBERGEMENT_YOUSIGN_STATUS_LABELS = {
    "draft": "√Ä pr√©parer",
    "approval": "En pr√©paration",
    "ongoing": "En attente de signature",
    "done": "Sign√©e",
    "signed": "Sign√©e",
    "declined": "Refus√©e",
    "expired": "Expir√©e",
    "canceled": "Annul√©e",
    "rejected": "Rejet√©e",
    "error": "Erreur d'envoi",
}
HEBERGEMENT_INVENTORY_ITEMS = (
    ("access", "Cl√©, badge et moyen d'acc√®s"),
    ("door", "Porte, serrure et poign√©e"),
    ("walls", "Murs et plafond de l'espace de couchage"),
    ("floor", "Sol et plinthes"),
    ("window", "Fen√™tre, vitrage, fermeture et occultation"),
    ("bed_frame", "Lit, sommier et structure"),
    ("mattress", "Matelas"),
    ("sheet", "Protection / drap-housse fourni"),
    ("storage", "Rangement et mobilier attribu√©s"),
    ("electricity", "√âclairage, interrupteurs et prises visibles"),
    ("heating", "Chauffage et ventilation visibles"),
    ("safety", "D√©tecteur / √©quipement de s√©curit√© visible"),
    ("bathroom", "Douche et salle de bain"),
    ("toilets", "Toilettes"),
    ("kitchen", "Cuisine et √©quipements communs"),
    ("laundry", "Machine √† laver et s√®che-linge"),
    ("common_areas", "Espaces communs"),
    ("other", "Autre √©l√©ment"),
)
HEBERGEMENT_HANDOVER_CHECKLIST = (
    ("convention_reviewed", "Convention relue avec le stagiaire"),
    ("copy_delivery_planned", "Copie de la convention pr√©vue pour le stagiaire"),
    ("inventory_completed", "√âtat des lieux d'entr√©e compl√©t√©"),
    ("rules_explained", "R√®gles essentielles expliqu√©es"),
    ("key_handed_over", "Cl√© ou badge remis"),
    ("safety_explained", "Consignes de s√©curit√© et issues indiqu√©es"),
)
HEBERGEMENT_CONVENTION_FIELD_NAMES = (
    "nom", "prenom", "telephone", "mail", "session",
    "personal_address", "postal_code", "city",
    "contract_date", "contract_time", "arrival_date", "arrival_time",
    "departure_date", "departure_time", "room", "bed", "key_number",
    "center_representative", "center_role", "payment_status",
    "payment_method", "payment_date", "payment_cheque_number",
    "payment_bank", "payment_cheque_date", "receipt_issued",
    "receipt_reference", "deposit_received", "deposit_holder",
    "deposit_bank", "deposit_cheque_number", "deposit_cheque_date",
    "entry_photos_count", "entry_observations",
)
HEBERGEMENT_CONVENTION_FIELD_LIMITS = {
    "nom": 80,
    "prenom": 80,
    "telephone": 30,
    "mail": 160,
    "session": 160,
    "personal_address": 180,
    "postal_code": 12,
    "city": 80,
    "room": 80,
    "bed": 40,
    "key_number": 40,
    "center_representative": 120,
    "center_role": 120,
    "payment_cheque_number": 60,
    "payment_bank": 100,
    "receipt_reference": 80,
    "deposit_holder": 120,
    "deposit_bank": 100,
    "deposit_cheque_number": 60,
    "entry_photos_count": 4,
    "entry_observations": 600,
}


def _format_hebergement_date_fr(value):
    return (
        f"{_HEBERGEMENT_WEEKDAYS[value.weekday()]} {value.day} "
        f"{_HEBERGEMENT_MONTHS[value.month - 1]} {value.year}"
    )


def _hebergement_sessions_for_template(data_store=None, today=None, *, include_past=False):
    """Use the on-site A3P calendar, keeping booking labels without exam details."""
    sessions = (
        get_formation_sessions(data_store)
        if include_past
        else get_upcoming_formation_sessions(data_store, today=today)
    )
    options = {}
    for row in sessions.get("cote_azur", {}).get("A3P", []):
        label = re.split(
            r"\s*[-‚Äì‚Äî]\s*examen\b",
            str(row.get("label") or ""),
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()
        start_date, end_date = _hebergement_session_dates(label)
        if not start_date or not end_date:
            continue
        options[label] = {
            "label": label,
            "start_date": start_date,
            "end_date": end_date,
            "arrival_label": _format_hebergement_date_fr(
                start_date - datetime.timedelta(days=1)
            ),
        }
    return sorted(options.values(), key=lambda option: (option["start_date"], option["label"]))


def _hebergement_arrival_label(session):
    start_date, _end_date = _hebergement_session_dates(session)
    if not start_date:
        return "la veille du premier jour de formation"
    return _format_hebergement_date_fr(start_date - datetime.timedelta(days=1))


def _hebergement_session_dates(session):
    # Read the saved period too, so old bookings survive calendar edits/deletions.
    start_date, end_date = _session_date_range(session)
    if not start_date or not end_date or end_date < start_date:
        return None, None
    return start_date, end_date


def _hebergement_now_iso():
    return datetime.datetime.now(
        pytz.timezone("Europe/Paris")
    ).isoformat(timespec="seconds")


def _hebergement_date_input(value):
    """Convertit une date enregistr√©e en valeur ISO utilisable par input[type=date]."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _hebergement_date_label(value, fallback="√Ä compl√©ter"):
    iso_value = _hebergement_date_input(value)
    if not iso_value:
        return fallback
    return _format_hebergement_date_fr(
        datetime.datetime.strptime(iso_value, "%Y-%m-%d").date()
    )


def _find_hebergement_reservation(data, reservation_id):
    return next((
        item
        for item in data.get("hebergements", [])
        if str(item.get("id") or "") == str(reservation_id)
    ), None)


def _hebergement_yousign_status_label(status):
    return HEBERGEMENT_YOUSIGN_STATUS_LABELS.get(
        str(status or "draft").strip(), "Statut inconnu"
    )


def _normalize_hebergement_yousign_state(state=None):
    normalized = {
        "signatureRequestId": "",
        "documentId": "",
        "signerId": "",
        "fieldId": "",
        "externalId": "",
        "status": "draft",
        "statusLabel": "√Ä pr√©parer",
        "sentAt": "",
        "lastReminderAt": "",
        "signedAt": "",
        "declinedAt": "",
        "expiredAt": "",
        "canceledAt": "",
        "lastEvent": "",
        "lastEventAt": "",
        "lastSyncedAt": "",
        "lastWebhookAt": "",
        "recipientEmail": "",
        "recipientPhone": "",
        "signedDocumentFilename": "",
        "error": "",
        "errorPayload": None,
    }
    if isinstance(state, dict):
        normalized.update({
            key: value for key, value in state.items() if key in normalized
        })
    normalized["statusLabel"] = _hebergement_yousign_status_label(
        normalized.get("status")
    )
    return normalized


def _hebergement_yousign_is_locked(state):
    normalized = _normalize_hebergement_yousign_state(state)
    return bool(normalized.get("signatureRequestId")) and (
        normalized.get("status") in HEBERGEMENT_YOUSIGN_ACTIVE_STATUSES
        or normalized.get("status") in HEBERGEMENT_YOUSIGN_SIGNED_STATUSES
    )


def _hebergement_convention_record(reservation, current_user=""):
    stored = reservation.get("convention_hebergement")
    if not isinstance(stored, dict):
        stored = {}
    stored_fields = stored.get("fields")
    if not isinstance(stored_fields, dict):
        stored_fields = {}

    session_label = str(
        stored_fields.get("session") or reservation.get("session") or ""
    ).strip()
    start_date, end_date = _hebergement_session_dates(session_label)
    arrival_date = start_date - datetime.timedelta(days=1) if start_date else None
    payment_date = _hebergement_date_input(reservation.get("date_paiement"))

    defaults = {
        "nom": str(reservation.get("nom") or "").strip().upper(),
        "prenom": str(reservation.get("prenom") or "").strip().title(),
        "telephone": str(reservation.get("telephone") or "").strip(),
        "mail": str(reservation.get("mail") or "").strip(),
        "session": session_label,
        "personal_address": "",
        "postal_code": "",
        "city": "",
        "contract_date": start_date.isoformat() if start_date else "",
        "contract_time": "",
        "arrival_date": arrival_date.isoformat() if arrival_date else "",
        "arrival_time": "",
        "departure_date": end_date.isoformat() if end_date else "",
        "departure_time": "",
        "room": "",
        "bed": "",
        "key_number": str(reservation.get("cle_numero") or "").strip(),
        "center_representative": str(current_user or "").strip(),
        "center_role": "Direction Int√©grale Academy",
        "payment_status": str(
            reservation.get("paiement") or "Non pay√©"
        ).strip(),
        "payment_method": str(
            reservation.get("mode_paiement") or ""
        ).strip(),
        "payment_date": payment_date,
        "payment_cheque_number": "",
        "payment_bank": "",
        "payment_cheque_date": "",
        "receipt_issued": "",
        "receipt_reference": "",
        "deposit_received": "",
        "deposit_holder": "",
        "deposit_bank": "",
        "deposit_cheque_number": "",
        "deposit_cheque_date": "",
        "entry_photos_count": "0",
        "entry_observations": "N√©ant",
    }
    fields = {
        key: str(stored_fields.get(key, default) or "").strip()[
            :HEBERGEMENT_CONVENTION_FIELD_LIMITS.get(key, 40)
        ]
        for key, default in defaults.items()
    }

    stored_inventory = stored.get("inventory")
    if not isinstance(stored_inventory, dict):
        stored_inventory = {}
    inventory = {}
    for item_key, item_label in HEBERGEMENT_INVENTORY_ITEMS:
        saved_item = stored_inventory.get(item_key)
        if not isinstance(saved_item, dict):
            saved_item = {}
        state = str(saved_item.get("entry_state") or "B").strip().upper()
        if state not in {"B", "U", "A", "D", "M", "N/A"}:
            state = "B"
        inventory[item_key] = {
            "label": item_label,
            "entry_state": state,
            "observations": str(
                saved_item.get("observations") or ""
            ).strip()[:80],
        }

    stored_checklist = stored.get("checklist")
    if not isinstance(stored_checklist, dict):
        stored_checklist = {}
    checklist = {
        key: bool(stored_checklist.get(key))
        for key, _label in HEBERGEMENT_HANDOVER_CHECKLIST
    }
    return {
        "version": HEBERGEMENT_CONVENTION_VERSION,
        "fields": fields,
        "inventory": inventory,
        "checklist": checklist,
        "updated_at": str(stored.get("updated_at") or ""),
        "yousign": _normalize_hebergement_yousign_state(
            stored.get("yousign")
        ),
    }


def _hebergement_convention_from_form(reservation, form, current_user=""):
    record = _hebergement_convention_record(reservation, current_user)
    fields = record["fields"]
    for field_name in HEBERGEMENT_CONVENTION_FIELD_NAMES:
        if field_name in form:
            value = str(form.get(field_name) or "").strip()
            fields[field_name] = value[
                :HEBERGEMENT_CONVENTION_FIELD_LIMITS.get(field_name, 40)
            ]

    if fields["payment_status"] not in {"Pay√©", "Non pay√©"}:
        fields["payment_status"] = "Non pay√©"
    if fields["payment_method"] not in {"", "Ch√®que", "Esp√®ces"}:
        fields["payment_method"] = ""
    if fields["receipt_issued"] not in {"", "Oui", "Non"}:
        fields["receipt_issued"] = ""
    if fields["deposit_received"] not in {"", "Oui", "Non"}:
        fields["deposit_received"] = ""

    record["checklist"] = {
        key: form.get(f"checklist_{key}") == "on"
        for key, _label in HEBERGEMENT_HANDOVER_CHECKLIST
    }
    for item_key, item_label in HEBERGEMENT_INVENTORY_ITEMS:
        state = str(
            form.get(f"inventory_{item_key}_state") or "B"
        ).strip().upper()
        if state not in {"B", "U", "A", "D", "M", "N/A"}:
            state = "B"
        record["inventory"][item_key] = {
            "label": item_label,
            "entry_state": state,
            "observations": str(
                form.get(f"inventory_{item_key}_observations") or ""
            ).strip()[:80],
        }
    record["updated_at"] = _hebergement_now_iso()
    return record


def _hebergement_signature_validation_errors(record):
    fields = record.get("fields") or {}
    required = {
        "nom": "nom",
        "prenom": "pr√©nom",
        "telephone": "t√©l√©phone portable",
        "mail": "adresse e-mail",
        "session": "session de formation",
        "personal_address": "adresse personnelle",
        "postal_code": "code postal",
        "city": "ville",
        "contract_date": "date de signature",
        "contract_time": "heure de signature",
        "arrival_date": "date d'arriv√©e",
        "arrival_time": "heure d'arriv√©e",
        "departure_date": "date de d√©part pr√©vue",
        "departure_time": "heure de d√©part pr√©vue",
        "room": "dortoir ou chambre",
        "bed": "num√©ro de lit",
        "key_number": "num√©ro de cl√© ou badge",
        "center_representative": "repr√©sentant du centre",
        "center_role": "fonction du repr√©sentant",
        "payment_method": "mode de r√®glement",
        "payment_date": "date du r√®glement",
        "receipt_issued": "indication sur le re√ßu",
        "deposit_holder": "titulaire du ch√®que de caution",
        "deposit_bank": "banque du ch√®que de caution",
        "deposit_cheque_number": "num√©ro du ch√®que de caution",
        "deposit_cheque_date": "date du ch√®que de caution",
    }
    errors = [
        f"Renseignez le champ ¬´ {label} ¬ª."
        for key, label in required.items()
        if not str(fields.get(key) or "").strip()
    ]
    if not all(_hebergement_session_dates(fields.get("session"))):
        errors.append("S√©lectionnez une session de formation valide.")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", fields.get("mail", "")):
        errors.append("L'adresse e-mail du signataire est invalide.")
    try:
        normalize_french_mobile(fields.get("telephone", ""))
    except YousignError as exc:
        errors.append(str(exc))

    arrival_date = _hebergement_date_input(fields.get("arrival_date"))
    departure_date = _hebergement_date_input(fields.get("departure_date"))
    payment_date = _hebergement_date_input(fields.get("payment_date"))
    contract_date = _hebergement_date_input(fields.get("contract_date"))
    date_fields = {
        "arrival_date": (arrival_date, "La date d'arriv√©e est invalide."),
        "departure_date": (departure_date, "La date de d√©part est invalide."),
        "payment_date": (payment_date, "La date de remise des 300 ‚Ç¨ est invalide."),
        "payment_cheque_date": (
            _hebergement_date_input(fields.get("payment_cheque_date")),
            "La date du ch√®que de r√®glement est invalide.",
        ),
        "contract_date": (contract_date, "La date de signature est invalide."),
        "deposit_cheque_date": (
            _hebergement_date_input(fields.get("deposit_cheque_date")),
            "La date du ch√®que de caution est invalide.",
        ),
    }
    for field_name, (parsed_value, error_message) in date_fields.items():
        if fields.get(field_name) and not parsed_value:
            errors.append(error_message)
    if arrival_date and departure_date and departure_date < arrival_date:
        errors.append("La date de d√©part doit √™tre post√©rieure √† la date d'arriv√©e.")
    if (
        contract_date and arrival_date and departure_date
        and not arrival_date <= contract_date <= departure_date
    ):
        errors.append(
            "La date de signature doit √™tre comprise dans la p√©riode "
            "d'h√©bergement."
        )
    arrival_time = str(fields.get("arrival_time") or "").strip()
    if arrival_time:
        try:
            parsed_arrival_time = datetime.time.fromisoformat(arrival_time)
            if not (
                datetime.time(8, 0)
                <= parsed_arrival_time
                <= datetime.time(17, 0)
            ):
                errors.append(
                    "L'heure d'arriv√©e doit imp√©rativement √™tre comprise entre "
                    "08h00 et 17h00."
                )
        except ValueError:
            errors.append("L'heure d'arriv√©e est invalide.")
    for field_name, label in (
        ("contract_time", "L'heure de signature est invalide."),
        ("departure_time", "L'heure de d√©part est invalide."),
    ):
        raw_time = str(fields.get(field_name) or "").strip()
        if raw_time:
            try:
                datetime.time.fromisoformat(raw_time)
            except ValueError:
                errors.append(label)
    if fields.get("payment_status") != "Pay√©":
        errors.append("La participation de 300 ‚Ç¨ doit √™tre enregistr√©e comme pay√©e.")
    elif arrival_date and payment_date and payment_date != arrival_date:
        errors.append(
            "La date de remise des 300 ‚Ç¨ doit correspondre √† la date d'arriv√©e."
        )
    if fields.get("deposit_received") != "Oui":
        errors.append("Le ch√®que de caution de 200 ‚Ç¨ doit √™tre enregistr√© comme re√ßu.")
    if fields.get("payment_method") == "Ch√®que":
        cheque_required = {
            "payment_cheque_number": "num√©ro du ch√®que de r√®glement",
            "payment_bank": "banque du ch√®que de r√®glement",
            "payment_cheque_date": "date du ch√®que de r√®glement",
        }
        errors.extend(
            f"Renseignez le champ ¬´ {label} ¬ª."
            for key, label in cheque_required.items()
            if not str(fields.get(key) or "").strip()
        )
    missing_checks = [
        label for key, label in HEBERGEMENT_HANDOVER_CHECKLIST
        if not (record.get("checklist") or {}).get(key)
    ]
    if missing_checks:
        errors.append(
            "Validez toutes les v√©rifications de remise avant l'envoi Yousign."
        )
    return list(dict.fromkeys(errors))


app.jinja_env.globals["hebergement_yousign_status_label"] = (
    _hebergement_yousign_status_label
)


def _hebergement_convention_context(reservation):
    convention = _hebergement_convention_record(reservation)
    fields = convention["fields"]
    session_label = str(
        fields.get("session") or reservation.get("session") or ""
    ).strip()
    start_date, end_date = _hebergement_session_dates(session_label)
    reservation_id = re.sub(
        r"[^A-Za-z0-9]", "", str(reservation.get("id") or "")
    )
    reference_suffix = (reservation_id[:8] or "ACOMPLETER").upper()
    paris_tz = pytz.timezone("Europe/Paris")
    generated_on = datetime.datetime.now(paris_tz).date()

    return {
        "contract_reference": f"HEB-CDA-{reference_suffix}",
        "contract_version": "3 septembre 2026",
        "generated_on": _format_hebergement_date_fr(generated_on),
        "formation_label": "Agent de protection physique des personnes (A3P)",
        "session_label": session_label or "√Ä compl√©ter",
        "formation_start_label": (
            _format_hebergement_date_fr(start_date) if start_date else "√Ä compl√©ter"
        ),
        "formation_end_label": (
            _format_hebergement_date_fr(end_date) if end_date else "√Ä compl√©ter"
        ),
        "contract_date_label": _hebergement_date_label(
            fields.get("contract_date")
        ),
        "contract_time": fields.get("contract_time") or "√Ä compl√©ter",
        "arrival_label": _hebergement_date_label(
            fields.get("arrival_date"),
            _hebergement_arrival_label(session_label),
        ),
        "arrival_time": fields.get("arrival_time") or "√Ä compl√©ter",
        "departure_label": _hebergement_date_label(
            fields.get("departure_date"),
            _format_hebergement_date_fr(end_date) if end_date else "√Ä compl√©ter",
        ),
        "departure_time": fields.get("departure_time") or "√Ä compl√©ter",
        "occupant": {
            "nom": fields.get("nom", "").upper(),
            "prenom": fields.get("prenom", "").title(),
            "telephone": fields.get("telephone", ""),
            "mail": fields.get("mail", ""),
            "address": fields.get("personal_address", ""),
            "postal_code": fields.get("postal_code", ""),
            "city": fields.get("city", ""),
        },
        "room": fields.get("room", ""),
        "bed": fields.get("bed", ""),
        "key_number": fields.get("key_number", ""),
        "center_representative": fields.get("center_representative", ""),
        "center_role": fields.get("center_role", ""),
        "payment_status": fields.get("payment_status", "Non pay√©"),
        "payment_method": fields.get("payment_method", ""),
        "payment_date": _hebergement_date_label(
            fields.get("payment_date"), "Non renseign√©e"
        ),
        "payment_cheque_number": fields.get("payment_cheque_number", ""),
        "payment_bank": fields.get("payment_bank", ""),
        "payment_cheque_date": _hebergement_date_label(
            fields.get("payment_cheque_date"), "Non renseign√©e"
        ),
        "receipt_issued": fields.get("receipt_issued", ""),
        "receipt_reference": fields.get("receipt_reference", ""),
        "deposit_received": fields.get("deposit_received", ""),
        "deposit_holder": fields.get("deposit_holder", ""),
        "deposit_bank": fields.get("deposit_bank", ""),
        "deposit_cheque_number": fields.get("deposit_cheque_number", ""),
        "deposit_cheque_date": _hebergement_date_label(
            fields.get("deposit_cheque_date"), "Non renseign√©e"
        ),
        "entry_photos_count": fields.get("entry_photos_count", "0"),
        "entry_observations": fields.get("entry_observations", "N√©ant"),
        "inventory": convention.get("inventory") or {},
        "handover_checklist": convention.get("checklist") or {},
        "electronically_prepared": bool(
            convention.get("yousign", {}).get("signatureRequestId")
        ),
    }


def _build_hebergement_convention_pdf(reservation):
    from hebergement_contract import build_hebergement_contract_pdf

    return build_hebergement_contract_pdf(
        _hebergement_convention_context(reservation),
        logo_path=os.path.join(app.static_folder, "logo.png"),
    )


def _hebergement_confirmation_email(prenom, session):
    arrival_label = _hebergement_arrival_label(session)
    safe_prenom = html_module.escape(prenom or "")
    safe_session = html_module.escape(session or "")
    safe_arrival = html_module.escape(arrival_label)
    safe_address = html_module.escape(HEBERGEMENT_ADDRESS)

    subject = "Confirmation de votre h√©bergement ‚Äì Int√©grale Academy"
    plain = (
        f"Bonjour {prenom},\n\n"
        "Votre r√©servation d‚Äôh√©bergement sur place pour la formation Agent de Protection "
        "Physique des Personnes (A3P) est confirm√©e.\n\n"
        "R√âCAPITULATIF DE VOTRE R√âSERVATION\n"
        f"- P√©riode de formation : {session}\n"
        f"- Adresse : {HEBERGEMENT_ADDRESS}\n"
        "- H√©bergement collectif sur place pendant toute la dur√©e de la formation, week-ends "
        "et jours f√©ri√©s inclus.\n\n"
        "ARRIV√âE ET REMISE DES CL√âS\n"
        f"La remise des cl√©s peut avoir lieu le {arrival_label}, veille du d√©but de votre "
        "formation, imp√©rativement entre 08h00 et 17h00. Aucune remise de cl√©s ne pourra "
        "√™tre effectu√©e apr√®s 17h00.\n"
        "Si vous ne pouvez pas vous pr√©senter avant 17h00, vous devrez pr√©voir par vos "
        "propres moyens une solution d‚Äôh√©bergement pour cette nuit et vous pr√©senter au "
        "centre le lendemain matin, √† l‚Äôheure indiqu√©e sur votre convocation.\n\n"
        "√Ä REMETTRE IMP√âRATIVEMENT D√àS VOTRE ARRIV√âE\n"
        "- Participation financi√®re : 300 ‚Ç¨, √† verser imp√©rativement d√®s votre arriv√©e, "
        "lors de la remise des cl√©s et de la signature du contrat d‚Äôh√©bergement. Le r√®glement "
        "s‚Äôeffectue par ch√®que ou en esp√®ces, dans une enveloppe portant vos nom et pr√©nom.\n"
        "- D√©p√¥t de garantie : un ch√®que de caution distinct de 200 ‚Ç¨, √† remettre "
        "imp√©rativement d√®s votre arriv√©e, lors de la remise des cl√©s et de la signature du "
        "contrat d‚Äôh√©bergement. Il est destin√© √† couvrir notamment toute d√©gradation, perte "
        "de cl√© ou mat√©riel manquant et sera restitu√© en l‚Äôabsence de dommage apr√®s "
        "v√©rification.\n\n"
        "CE QUE VOUS DEVEZ APPORTER\n"
        "- Un sac de couchage ou une couverture (un drap-housse est fourni) ;\n"
        "- Un oreiller ;\n"
        "- Gel douche, savon et shampoing ;\n"
        "- Lessive ;\n"
        "- Des sacs-poubelle et du papier toilette compl√©mentaires si n√©cessaire entre deux "
        "passages de la soci√©t√© de nettoyage.\n\n"
        "R√àGLES ESSENTIELLES DE L‚ÄôH√âBERGEMENT\n"
        "- Maintenir les locaux propres, rang√©s et respecter les espaces communs, le mat√©riel "
        "et les √©quipements ;\n"
        "- Adopter une tenue correcte et ne pas circuler torse nu ou en sous-v√™tements ;\n"
        "- Ne pas fumer ni vapoter √† l‚Äôint√©rieur du centre ;\n"
        "- Respecter le voisinage : aucun bruit apr√®s 22h00, aucune f√™te et discr√©tion "
        "obligatoire ;\n"
        "- Ne pas introduire ni consommer d‚Äôalcool ou de drogue et ne pas p√©n√©trer dans le "
        "centre sous leur emprise ;\n"
        "- Ne faire entrer aucune personne ext√©rieure. L‚Äôacc√®s est strictement r√©serv√© aux "
        "stagiaires h√©berg√©s.\n\n"
        "Tout manquement √† ces r√®gles peut entra√Æner des sanctions et la fin de la mise √† "
        "disposition de l‚Äôh√©bergement.\n\n"
        "Nous vous remercions de prendre vos dispositions avant votre arriv√©e.\n\n"
        "L‚Äô√©quipe Int√©grale Academy"
    )

    html_body = _wrap_html(
        '<p style="margin:0 0 6px;color:#b27a12;font-size:12px;font-weight:700;'
        'letter-spacing:.08em;text-transform:uppercase;">H√©bergement sur place</p>'
        '<h1 style="margin:0 0 18px;color:#172033;font-size:25px;line-height:1.25;">'
        "Votre r√©servation est confirm√©e</h1>",
        f"""
        <p style="margin:0 0 16px;">Bonjour <strong>{safe_prenom}</strong>,</p>
        <p style="margin:0 0 20px;">Votre place dans notre h√©bergement collectif est confirm√©e pour votre formation <strong>Agent de Protection Physique des Personnes (A3P)</strong>.</p>

        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 20px;border-collapse:separate;border-spacing:0;background:#f7f8fb;border:1px solid #e5e8ef;border-radius:10px;overflow:hidden;">
          <tr><td style="padding:14px 16px;border-bottom:1px solid #e5e8ef;color:#667085;width:32%;">Formation</td><td style="padding:14px 16px;border-bottom:1px solid #e5e8ef;color:#172033;font-weight:700;">A3P</td></tr>
          <tr><td style="padding:14px 16px;border-bottom:1px solid #e5e8ef;color:#667085;">P√©riode</td><td style="padding:14px 16px;border-bottom:1px solid #e5e8ef;color:#172033;font-weight:700;">{safe_session}</td></tr>
          <tr><td style="padding:14px 16px;color:#667085;">Adresse</td><td style="padding:14px 16px;color:#172033;font-weight:700;">{safe_address}</td></tr>
        </table>

        <div style="margin:0 0 20px;padding:18px;background:#fff7e6;border:1px solid #f0c36d;border-left:5px solid #b27a12;border-radius:10px;">
          <p style="margin:0 0 6px;color:#7a4d00;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;">Arriv√©e et remise des cl√©s</p>
          <p style="margin:0 0 8px;color:#172033;font-size:18px;line-height:1.4;"><strong>{safe_arrival}, entre 08h00 et 17h00 imp√©rativement</strong></p>
          <p style="margin:0;color:#5f430d;">Aucune remise de cl√©s ne pourra √™tre effectu√©e apr√®s 17h00. Si vous ne pouvez pas √™tre pr√©sent avant cet horaire, vous devrez pr√©voir par vos propres moyens un h√©bergement pour cette nuit, puis vous pr√©senter au centre le lendemain matin √† l‚Äôheure indiqu√©e sur votre convocation.</p>
        </div>

        <h2 style="margin:24px 0 10px;color:#172033;font-size:18px;">Paiement et caution √† remettre d√®s l‚Äôarriv√©e</h2>
        <div style="margin:0 0 10px;padding:14px 16px;border:1px solid #dce3ef;border-radius:9px;">
          <p style="margin:0 0 4px;color:#172033;font-weight:700;">Participation financi√®re ‚Äî 300 ‚Ç¨</p>
          <p style="margin:0;color:#475467;"><strong>√Ä verser imp√©rativement d√®s votre arriv√©e, lors de la remise des cl√©s et de la signature du contrat d‚Äôh√©bergement.</strong> Le r√®glement s‚Äôeffectue par ch√®que ou en esp√®ces, dans une enveloppe portant vos nom et pr√©nom. Le montant couvre toute la dur√©e de la formation, week-ends et jours f√©ri√©s inclus.</p>
        </div>
        <div style="margin:0 0 20px;padding:14px 16px;border:1px solid #dce3ef;border-radius:9px;">
          <p style="margin:0 0 4px;color:#172033;font-weight:700;">Ch√®que de caution ‚Äî 200 ‚Ç¨</p>
          <p style="margin:0;color:#475467;"><strong>√Ä remettre imp√©rativement d√®s votre arriv√©e, lors de la remise des cl√©s et de la signature du contrat d‚Äôh√©bergement.</strong> Ce ch√®que distinct est destin√© √† couvrir notamment toute d√©gradation, perte de cl√© ou mat√©riel manquant. Il sera restitu√© en l‚Äôabsence de dommage apr√®s v√©rification.</p>
        </div>

        <h2 style="margin:24px 0 10px;color:#172033;font-size:18px;">√Ä apporter</h2>
        <ul style="margin:0 0 20px;padding-left:20px;color:#344054;">
          <li style="margin-bottom:5px;">Sac de couchage ou couverture ‚Äî un drap-housse est fourni ;</li>
          <li style="margin-bottom:5px;">Oreiller, gel douche, savon et shampoing ;</li>
          <li style="margin-bottom:5px;">Lessive ;</li>
          <li>Sacs-poubelle et papier toilette compl√©mentaires si n√©cessaire entre deux passages de la soci√©t√© de nettoyage.</li>
        </ul>

        <h2 style="margin:24px 0 10px;color:#172033;font-size:18px;">R√®gles essentielles</h2>
        <ul style="margin:0 0 18px;padding-left:20px;color:#344054;">
          <li style="margin-bottom:5px;">Maintenir les locaux propres et rang√©s, et respecter les espaces communs, le mat√©riel et les √©quipements ;</li>
          <li style="margin-bottom:5px;">Adopter une tenue correcte et ne pas circuler torse nu ou en sous-v√™tements ;</li>
          <li style="margin-bottom:5px;">Ne pas fumer ni vapoter √† l‚Äôint√©rieur du centre ;</li>
          <li style="margin-bottom:5px;">Respecter le voisinage : aucun bruit apr√®s 22h00, aucune f√™te et discr√©tion obligatoire ;</li>
          <li style="margin-bottom:5px;">Ne pas introduire ni consommer d‚Äôalcool ou de drogue et ne pas p√©n√©trer dans le centre sous leur emprise ;</li>
          <li>Ne faire entrer aucune personne ext√©rieure : l‚Äôacc√®s est strictement r√©serv√© aux stagiaires h√©berg√©s.</li>
        </ul>
        <div style="margin:0 0 20px;padding:14px 16px;background:#fff1f0;border:1px solid #f4b4ae;border-radius:9px;color:#8a1c13;">
          <strong>Important :</strong> tout manquement √† ces r√®gles peut entra√Æner des sanctions et la fin de la mise √† disposition de l‚Äôh√©bergement.
        </div>
        <p style="margin:0;color:#344054;">Nous vous remercions de prendre vos dispositions avant votre arriv√©e.<br><br><strong>L‚Äô√©quipe Int√©grale Academy</strong></p>
        """
    )
    return subject, plain, html_body


@app.route("/hebergement", methods=["GET", "POST"])
def hebergement():
    data = load_data()
    paris_tz = pytz.timezone("Europe/Paris")
    hebergement_sessions = _hebergement_sessions_for_template(data)

    if request.method == "POST":
        nom = request.form.get("nom", "").strip()
        prenom = request.form.get("prenom", "").strip()
        telephone = request.form.get("telephone", "").strip()
        mail = request.form.get("email", "").strip()
        session = request.form.get("session", "").strip()

        if session not in {option["label"] for option in hebergement_sessions}:
            return render_template(
                "hebergement.html",
                erreur_session="Cette session n‚Äôest plus disponible. S√©lectionnez une session √† venir.",
                hebergement_sessions=hebergement_sessions,
                form_values=request.form,
            )

        # üö´ LIMITE DE 10 PLACES PAR SESSION (S√âCURIS√â C√îT√â SERVEUR)
        nb_places_session = len([
            h for h in data.get("hebergements", [])
            if h.get("session") == session
        ])

        if nb_places_session >= 10:
            return render_template(
                "hebergement.html",
                erreur_session="Notre h√©bergement est complet pour cette session (10 places r√©serv√©es sur 10).",
                hebergement_sessions=hebergement_sessions,
                form_values=request.form,
            )

        # ‚úÖ ENREGISTREMENT
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
            "paiement": "Non pay√©",
            "mode_paiement": "",
            "date_paiement": ""
        }

        data["hebergements"].append(new_resa)
        save_data(data)

        # --- Mail candidat ---
        subject, plain, html = _hebergement_confirmation_email(prenom, session)

        try:
            send_email_html(mail, subject, plain, html)
        except:
            pass

        # --- Mail admin ---
        try:
            send_email_html(
                "ecole@integraleacademy.com, clement@integraleacademy.com",
                f"üè® Nouvelle r√©servation h√©bergement ‚Äì {prenom} {nom}",
                plain,
                html
            )
        except:
            pass

        return redirect(url_for("hebergement_confirmation", session=session))

    return render_template(
        "hebergement.html",
        hebergement_sessions=hebergement_sessions,
        form_values={},
    )



@app.route("/hebergement_confirmation")
def hebergement_confirmation():
    session = request.args.get("session", "").strip()
    return render_template(
        "hebergement_confirmation.html",
        session=session,
        arrival_label=_hebergement_arrival_label(session),
    )


def _hebergement_sync_yousign_state(client, state):
    signature_request_id = state.get("signatureRequestId")
    payload = client.get_signature_request(signature_request_id)
    signers_payload = client.get_signature_request_signers(
        signature_request_id
    )
    if isinstance(signers_payload, list):
        signers = signers_payload
    elif isinstance(signers_payload, dict):
        signers = (
            signers_payload.get("data")
            if isinstance(signers_payload.get("data"), list)
            else signers_payload.get("signers", [])
        )
    else:
        signers = []

    api_status = str(payload.get("status") or "").strip()
    signer_statuses = [
        str(signer.get("status") or "").strip()
        for signer in signers if isinstance(signer, dict)
    ]
    if api_status == "done" or (
        signer_statuses
        and all(status in {"done", "signed"} for status in signer_statuses)
    ):
        status = "done"
    elif any(status == "declined" for status in signer_statuses):
        status = "declined"
    elif api_status in HEBERGEMENT_YOUSIGN_STATUS_LABELS:
        status = api_status
    else:
        status = state.get("status") or "ongoing"

    now = _hebergement_now_iso()
    updates = {
        **state,
        "status": status,
        "lastSyncedAt": now,
        "lastEvent": "manual.sync",
        "lastEventAt": now,
        "error": "",
    }
    if status in HEBERGEMENT_YOUSIGN_SIGNED_STATUSES:
        updates["signedAt"] = (
            payload.get("done_at") or payload.get("updated_at") or now
        )
    elif status == "declined":
        updates["declinedAt"] = now
    elif status == "expired":
        updates["expiredAt"] = now
    elif status == "canceled":
        updates["canceledAt"] = now
    return _normalize_hebergement_yousign_state(updates)


def _hebergement_pdf_page_count(pdf_content):
    counts = [
        int(value)
        for value in re.findall(rb"/Count\s+(\d+)\s+/Kids", pdf_content)
    ]
    return max(counts) if counts else 0


def _send_hebergement_convention_yousign(data, reservation):
    record = _hebergement_convention_record(reservation)
    state = record["yousign"]
    if state.get("signatureRequestId") and (
        state.get("status") in HEBERGEMENT_YOUSIGN_ACTIVE_STATUSES
    ):
        return "Une demande de signature Yousign est d√©j√† en cours."
    if not is_yousign_configured():
        return (
            "Yousign n'est pas configur√© sur Assistance : ajoutez la variable "
            "YOUSIGN_API_KEY sur le service Render."
        )

    fields = record["fields"]
    pdf_content = _build_hebergement_convention_pdf(reservation)
    page_count = _hebergement_pdf_page_count(pdf_content)
    expected_page = HEBERGEMENT_YOUSIGN_SIGNATURE_FIELD["page"]
    if page_count != expected_page:
        return (
            "Le nombre de pages de la convention a chang√© "
            f"({page_count or 'inconnu'} au lieu de {expected_page}). "
            "L'envoi est bloqu√© pour √©viter une signature mal positionn√©e."
        )

    client = YousignClient()
    now = _hebergement_now_iso()
    request_id = ""
    document_id = ""
    signer_id = ""
    field_id = ""
    external_id = sanitize_yousign_external_id(
        "hebergement-"
        f"{reservation.get('id')}-{uuid.uuid4().hex[:8]}"
    )
    occupant_name = " ".join(filter(None, [
        fields.get("prenom"), fields.get("nom")
    ]))
    try:
        signature_request = client.create_signature_request(
            f"Convention d'h√©bergement - {occupant_name}",
            external_id=external_id,
        )
        request_id = str(signature_request.get("id") or "")
        if not request_id:
            raise YousignError(
                "Yousign n'a pas retourn√© d'identifiant de demande."
            )

        document = client.upload_file(
            request_id,
            pdf_content,
            f"Convention_hebergement_{secure_filename(occupant_name)}.pdf",
            parse_anchors=False,
        )
        document_id = str(document.get("id") or "")
        if not document_id:
            raise YousignError(
                "Yousign n'a pas retourn√© d'identifiant de document."
            )

        signer = client.add_signer(
            request_id,
            fields.get("prenom", ""),
            fields.get("nom", ""),
            fields.get("mail", ""),
            fields.get("telephone", ""),
        )
        signer_id = str(signer.get("id") or "")
        if not signer_id:
            raise YousignError(
                "Yousign n'a pas retourn√© d'identifiant de signataire."
            )

        signature_field = client.add_signature_field(
            request_id,
            document_id,
            signer_id,
            **HEBERGEMENT_YOUSIGN_SIGNATURE_FIELD,
        )
        field_id = str(signature_field.get("id") or "")
        if not field_id:
            raise YousignError(
                "Le champ de signature n'a pas pu √™tre ajout√© √† la convention."
            )

        activated = client.activate_signature_request(request_id)
        status = str(activated.get("status") or "ongoing").strip()
        if status not in HEBERGEMENT_YOUSIGN_STATUS_LABELS:
            status = "ongoing"
        record["yousign"] = _normalize_hebergement_yousign_state({
            "signatureRequestId": request_id,
            "documentId": document_id,
            "signerId": signer_id,
            "fieldId": field_id,
            "externalId": external_id,
            "status": status,
            "sentAt": now,
            "lastSyncedAt": now,
            "lastEvent": "signature_request.activated",
            "lastEventAt": now,
            "recipientEmail": fields.get("mail", ""),
            "recipientPhone": normalize_french_mobile(
                fields.get("telephone", "")
            ),
            "error": "",
        })
        reservation["convention_hebergement"] = record
        save_data(data)
        return ""
    except YousignError as exc:
        if request_id:
            try:
                client.cancel_signature_request(
                    request_id,
                    "Envoi de la convention d'h√©bergement interrompu.",
                )
            except YousignError:
                app.logger.warning(
                    "Annulation de la demande Yousign incompl√®te impossible "
                    "reservation=%s request=%s",
                    reservation.get("id"), request_id,
                )
        user_error = yousign_service_access_message(
            exc.status_code, exc.payload
        )
        record["yousign"] = _normalize_hebergement_yousign_state({
            **state,
            "signatureRequestId": request_id or state.get(
                "signatureRequestId", ""
            ),
            "documentId": document_id,
            "signerId": signer_id,
            "fieldId": field_id,
            "externalId": external_id,
            "status": "error",
            "lastSyncedAt": now,
            "lastEvent": "api.error",
            "lastEventAt": now,
            "recipientEmail": fields.get("mail", ""),
            "error": user_error,
            "errorPayload": exc.payload,
        })
        reservation["convention_hebergement"] = record
        save_data(data)
        app.logger.warning(
            "Envoi Yousign h√©bergement impossible reservation=%s status=%s "
            "error=%s",
            reservation.get("id"), exc.status_code, user_error,
        )
        return user_error


def _sync_hebergement_reservation_from_convention(reservation, record):
    fields = record["fields"]
    for field in ("nom", "prenom", "telephone", "mail", "session"):
        reservation[field] = fields.get(field, "")
    reservation["cle_numero"] = fields.get("key_number", "")
    reservation["paiement"] = fields.get("payment_status", "Non pay√©")
    reservation["mode_paiement"] = fields.get("payment_method", "")
    payment_date = _hebergement_date_input(fields.get("payment_date"))
    if payment_date:
        formatted = datetime.datetime.strptime(
            payment_date, "%Y-%m-%d"
        ).strftime("%d/%m/%Y")
        previous = str(reservation.get("date_paiement") or "")
        reservation["date_paiement"] = (
            previous if previous.startswith(formatted) else formatted
        )
    elif reservation["paiement"] != "Pay√©":
        reservation["date_paiement"] = ""
    if record.get("checklist", {}).get("key_handed_over"):
        reservation["cle_etat"] = "Donnee"
    reservation["convention_hebergement"] = record


@app.route(
    "/admin_hebergement/<reservation_id>/convention/preparer",
    methods=["GET", "POST"],
)
@login_required
def admin_hebergement_convention_editor(reservation_id):
    data = load_data()
    reservation = _find_hebergement_reservation(data, reservation_id)
    if reservation is None:
        abort(404)

    current_user = session.get("user_name") or session.get("user_email") or ""
    record = _hebergement_convention_record(reservation, current_user)
    validation_errors = []

    if request.method == "POST":
        if _hebergement_yousign_is_locked(record.get("yousign")):
            flash(
                "La convention est verrouill√©e car elle a d√©j√† √©t√© envoy√©e en "
                "signature. Actualisez son statut avant toute modification.",
                "error",
            )
            return redirect(url_for(
                "admin_hebergement_convention_editor",
                reservation_id=reservation_id,
            ))

        record = _hebergement_convention_from_form(
            reservation, request.form, current_user
        )
        _sync_hebergement_reservation_from_convention(reservation, record)
        save_data(data)
        action = request.form.get("editor_action") or "save"

        if action == "preview":
            return redirect(url_for(
                "admin_hebergement_convention",
                reservation_id=reservation_id,
                inline="1",
            ))
        if action == "send_yousign":
            validation_errors = _hebergement_signature_validation_errors(record)
            if not validation_errors:
                error = _send_hebergement_convention_yousign(data, reservation)
                if not error:
                    flash(
                        "Convention envoy√©e en signature Yousign. Le stagiaire "
                        "recevra l'invitation par e-mail et le code par SMS.",
                        "success",
                    )
                    return redirect(url_for(
                        "admin_hebergement_convention_editor",
                        reservation_id=reservation_id,
                    ))
                validation_errors = [error]
        else:
            flash("Convention enregistr√©e.", "success")
            return redirect(url_for(
                "admin_hebergement_convention_editor",
                reservation_id=reservation_id,
            ))

    session_options = [
        option["label"]
        for option in _hebergement_sessions_for_template(data, include_past=True)
    ]
    for saved_label in (reservation.get("session"), record["fields"].get("session")):
        if saved_label and saved_label not in session_options:
            session_options.append(saved_label)
    session_schedule = {}
    for label in session_options:
        start_date, end_date = _hebergement_session_dates(label)
        if start_date and end_date:
            session_schedule[label] = {
                "contract_date": start_date.isoformat(),
                "arrival_date": (start_date - datetime.timedelta(days=1)).isoformat(),
                "departure_date": end_date.isoformat(),
            }

    return render_template(
        "admin_hebergement_convention_editor.html",
        reservation=reservation,
        convention=record,
        fields=record["fields"],
        inventory_items=HEBERGEMENT_INVENTORY_ITEMS,
        handover_checklist=HEBERGEMENT_HANDOVER_CHECKLIST,
        validation_errors=validation_errors,
        yousign_state=record["yousign"],
        form_locked=_hebergement_yousign_is_locked(record["yousign"]),
        yousign_configured=is_yousign_configured(),
        yousign_sandbox=is_yousign_sandbox(),
        session_options=session_options,
        session_schedule=session_schedule,
    )


@app.post("/admin_hebergement/<reservation_id>/convention/yousign/sync")
@login_required
def admin_hebergement_convention_yousign_sync(reservation_id):
    data = load_data()
    reservation = _find_hebergement_reservation(data, reservation_id)
    if reservation is None:
        abort(404)
    record = _hebergement_convention_record(reservation)
    state = record["yousign"]
    if not state.get("signatureRequestId"):
        flash("Aucune demande Yousign √† actualiser.", "error")
    elif not is_yousign_configured():
        flash("Yousign n'est pas configur√© sur Assistance.", "error")
    else:
        try:
            record["yousign"] = _hebergement_sync_yousign_state(
                YousignClient(), state
            )
            reservation["convention_hebergement"] = record
            save_data(data)
            flash(
                "Statut Yousign actualis√© : "
                f"{record['yousign']['statusLabel']}.",
                "success",
            )
        except YousignError as exc:
            flash(
                "Actualisation Yousign impossible : "
                f"{yousign_service_access_message(exc.status_code, exc.payload)}",
                "error",
            )
    return redirect(url_for(
        "admin_hebergement_convention_editor",
        reservation_id=reservation_id,
    ))


@app.post("/admin_hebergement/<reservation_id>/convention/yousign/remind")
@login_required
def admin_hebergement_convention_yousign_remind(reservation_id):
    data = load_data()
    reservation = _find_hebergement_reservation(data, reservation_id)
    if reservation is None:
        abort(404)
    record = _hebergement_convention_record(reservation)
    state = record["yousign"]
    if (
        not state.get("signatureRequestId")
        or not state.get("signerId")
        or state.get("status") != "ongoing"
    ):
        flash("Aucune signature en attente ne peut √™tre relanc√©e.", "error")
    else:
        try:
            YousignClient().send_signer_reminder(
                state["signatureRequestId"], state["signerId"]
            )
            state["lastReminderAt"] = _hebergement_now_iso()
            state["lastEvent"] = "signer.reminder_sent"
            state["lastEventAt"] = state["lastReminderAt"]
            record["yousign"] = _normalize_hebergement_yousign_state(state)
            reservation["convention_hebergement"] = record
            save_data(data)
            flash("Relance Yousign envoy√©e au stagiaire.", "success")
        except YousignError as exc:
            flash(
                "Relance Yousign impossible : "
                f"{yousign_service_access_message(exc.status_code, exc.payload)}",
                "error",
            )
    return redirect(url_for(
        "admin_hebergement_convention_editor",
        reservation_id=reservation_id,
    ))


@app.post("/admin_hebergement/<reservation_id>/convention/yousign/cancel")
@login_required
def admin_hebergement_convention_yousign_cancel(reservation_id):
    data = load_data()
    reservation = _find_hebergement_reservation(data, reservation_id)
    if reservation is None:
        abort(404)
    record = _hebergement_convention_record(reservation)
    state = record["yousign"]
    if (
        not state.get("signatureRequestId")
        or state.get("status") not in HEBERGEMENT_YOUSIGN_ACTIVE_STATUSES
    ):
        flash("Aucune demande Yousign active ne peut √™tre annul√©e.", "error")
    elif not is_yousign_configured():
        flash("Yousign n'est pas configur√© sur Assistance.", "error")
    else:
        try:
            YousignClient().cancel_signature_request(
                state["signatureRequestId"],
                "Convention d'h√©bergement annul√©e par Int√©grale Academy.",
            )
            now = _hebergement_now_iso()
            state.update({
                "status": "canceled",
                "canceledAt": now,
                "lastEvent": "signature_request.canceled_by_admin",
                "lastEventAt": now,
                "lastSyncedAt": now,
                "error": "",
            })
            record["yousign"] = _normalize_hebergement_yousign_state(state)
            reservation["convention_hebergement"] = record
            save_data(data)
            flash(
                "Demande Yousign annul√©e. La convention peut de nouveau √™tre "
                "modifi√©e puis renvoy√©e.",
                "success",
            )
        except YousignError as exc:
            flash(
                "Annulation Yousign impossible : "
                f"{yousign_service_access_message(exc.status_code, exc.payload)}",
                "error",
            )
    return redirect(url_for(
        "admin_hebergement_convention_editor",
        reservation_id=reservation_id,
    ))


def _extract_yousign_signed_pdf(content):
    if content.startswith(b"%PDF"):
        return content
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            pdf_names = sorted(
                name for name in archive.namelist()
                if name.lower().endswith(".pdf") and not name.endswith("/")
            )
            if pdf_names:
                return archive.read(pdf_names[0])
    except zipfile.BadZipFile:
        pass
    raise YousignError(
        "Yousign n'a retourn√© aucun document PDF sign√© exploitable."
    )


@app.get("/admin_hebergement/<reservation_id>/convention/yousign/download")
@login_required
def admin_hebergement_convention_yousign_download(reservation_id):
    data = load_data()
    reservation = _find_hebergement_reservation(data, reservation_id)
    if reservation is None:
        abort(404)
    record = _hebergement_convention_record(reservation)
    state = record["yousign"]
    if state.get("status") not in HEBERGEMENT_YOUSIGN_SIGNED_STATUSES:
        flash("La convention n'est pas encore sign√©e.", "error")
        return redirect(url_for(
            "admin_hebergement_convention_editor",
            reservation_id=reservation_id,
        ))
    if not state.get("signatureRequestId"):
        abort(404)

    try:
        pdf_content = _extract_yousign_signed_pdf(
            YousignClient().download_signed_documents(
                state["signatureRequestId"]
            )
        )
    except YousignError as exc:
        flash(
            "T√©l√©chargement Yousign impossible : "
            f"{yousign_service_access_message(exc.status_code, exc.payload)}",
            "error",
        )
        return redirect(url_for(
            "admin_hebergement_convention_editor",
            reservation_id=reservation_id,
        ))

    occupant_filename = secure_filename(
        f"{record['fields'].get('nom', '')}_"
        f"{record['fields'].get('prenom', '')}"
    ).strip("_") or "stagiaire"
    response = send_file(
        io.BytesIO(pdf_content),
        as_attachment=True,
        download_name=(
            f"Convention_hebergement_signee_{occupant_filename}.pdf"
        ),
        mimetype="application/pdf",
        max_age=0,
    )
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.post("/webhooks/yousign/hebergement")
def hebergement_yousign_webhook():
    raw_body = request.get_data()
    webhook_secret = get_yousign_config().webhook_secret
    signature_header_names = (
        "X-Yousign-Signature-256",
        "X-Yousign-Signature",
        "Yousign-Signature",
        "X-Hub-Signature-256",
    )
    signature_header = next((
        request.headers.get(name)
        for name in signature_header_names
        if request.headers.get(name)
    ), "")
    if webhook_secret:
        if not signature_header:
            return {"ok": True, "ignored": True}
        expected = hmac.new(
            webhook_secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        provided = signature_header.split("=", 1)[-1].strip()
        if not hmac.compare_digest(expected, provided):
            return {"ok": True, "ignored": True}

    payload = request.get_json(silent=True) or {}
    event_name = (
        payload.get("event_name") or payload.get("event")
        or payload.get("type") or ""
    )
    event_statuses = {
        "signature_request.activated": "ongoing",
        "signer.done": "done",
        "signature_request.done": "done",
        "signer.declined": "declined",
        "signature_request.declined": "declined",
        "signature_request.expired": "expired",
        "signature_request.canceled": "canceled",
        "signature_request.rejected": "rejected",
        "signer.notification_delivery_failed": "error",
        "signer.error": "error",
    }
    if event_name not in event_statuses:
        return {"ok": True, "ignored": True}

    data_payload = (
        payload.get("data") if isinstance(payload.get("data"), dict) else {}
    )
    signature_request = (
        data_payload.get("signature_request")
        if isinstance(data_payload.get("signature_request"), dict) else {}
    )
    signer = (
        data_payload.get("signer")
        if isinstance(data_payload.get("signer"), dict) else {}
    )
    signature_request_id = (
        signature_request.get("id")
        or data_payload.get("signature_request_id")
        or payload.get("signature_request_id")
        or (
            signer.get("signature_request", {}).get("id")
            if isinstance(signer.get("signature_request"), dict) else ""
        )
    )
    external_id = (
        signature_request.get("external_id")
        or data_payload.get("external_id")
        or payload.get("external_id")
    )

    data = load_data()
    for reservation in data.get("hebergements", []):
        record = _hebergement_convention_record(reservation)
        state = record["yousign"]
        if not (
            signature_request_id
            and state.get("signatureRequestId") == signature_request_id
        ) and not (
            external_id and state.get("externalId") == external_id
        ):
            continue
        now = _hebergement_now_iso()
        status = event_statuses[event_name]
        state.update({
            "status": status,
            "lastWebhookAt": now,
            "lastEvent": event_name,
            "lastEventAt": now,
            "error": (
                "Yousign n'a pas pu notifier le stagiaire. V√©rifiez ses "
                "coordonn√©es."
                if status == "error" else ""
            ),
        })
        if status == "done":
            state["signedAt"] = now
        elif status == "declined":
            state["declinedAt"] = now
        elif status == "expired":
            state["expiredAt"] = now
        elif status == "canceled":
            state["canceledAt"] = now
        record["yousign"] = _normalize_hebergement_yousign_state(state)
        reservation["convention_hebergement"] = record
        save_data(data)
        return {"ok": True, "target": "hebergement"}
    return {"ok": True, "ignored": True}


@app.get("/admin_hebergement/<reservation_id>/convention")
@login_required
def admin_hebergement_convention(reservation_id):
    reservation = _find_hebergement_reservation(load_data(), reservation_id)
    if reservation is None:
        abort(404)

    pdf_content = _build_hebergement_convention_pdf(reservation)
    occupant_filename = secure_filename(
        f"{reservation.get('nom') or ''}_{reservation.get('prenom') or ''}"
    ).strip("_")
    if not occupant_filename:
        occupant_filename = secure_filename(str(reservation_id)) or "stagiaire"

    response = send_file(
        io.BytesIO(pdf_content),
        as_attachment=request.args.get("inline") != "1",
        download_name=f"Convention_hebergement_{occupant_filename}.pdf",
        mimetype="application/pdf",
        max_age=0,
    )
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


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

    # üîç Recherche
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

    # üîΩ Tri
    tri = request.args.get("tri")
    if tri == "session":
        hebergements = sorted(hebergements, key=lambda x: x.get("session", ""))

    # ------------------------------------------------------------------
    # üü¢ MISE √Ä JOUR DES R√âSERVATIONS (POST)
    # ------------------------------------------------------------------
    if request.method == "POST":
        action = request.form.get("action")
        resa_id = request.form.get("id")

        # On parcourt TOUS les h√©bergements (pas filtr√©s)
        for h in data["hebergements"]:
            if h["id"] == resa_id:

                # üîë Num√©ro de cl√©
                if action == "cle_numero":
                    h["cle_numero"] = request.form.get("value", "")
                    save_data(data)
                    return "", 204

                # üîë √âtat de cl√©
                if action == "cle_etat":
                    h["cle_etat"] = request.form.get("value", "")
                    save_data(data)
                    return "", 204

                # üóëÔ∏è Supprimer
                if action == "delete":
                    data["hebergements"].remove(h)
                    save_data(data)
                    return redirect(url_for("admin_hebergement"))

                # üíµ Paiement (Pay√© / Non pay√©)
                if action == "paiement":
                    h["paiement"] = request.form.get("value")
                    if h["paiement"] == "Pay√©":
                        paris_tz = pytz.timezone("Europe/Paris")
                        h["date_paiement"] = datetime.datetime.now(paris_tz).strftime("%d/%m/%Y %H:%M")
                    else:
                        h["date_paiement"] = ""
                    save_data(data)
                    return "", 204

                # üí≥ Mode de paiement
                if action == "mode":
                    h["mode_paiement"] = request.form.get("value")
                    save_data(data)
                    return "", 204

                # ‚úèÔ∏è Mise √† jour g√©n√©rique (nom, pr√©nom, t√©l√©phone, mail, session‚Ä¶)
                if action == "update_field":
                    field = request.form.get("field")
                    value = request.form.get("value", "").strip()

                    # Champs autoris√©s √† √™tre modifi√©s
                    allowed = {"nom", "prenom", "telephone", "mail", "session"}

                    if field not in allowed:
                        return "Champ non autoris√©", 400

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
# üè® API publique pour la plateforme principale : h√©bergement
# ------------------------------------------------------------
@app.route("/hebergement_data.json")
def hebergement_data():
    try:
        data = load_data()
        hebergements = data.get("hebergements", [])

        total = len(hebergements)
        non_payes = len([h for h in hebergements if h.get("paiement") != "Pay√©"])
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
    R√®gle: pr√©l√®vement le 5 du mois suivant la date du devis, puis chaque 5.
    Le total doit √™tre sold√© au plus tard J-7 avant l'examen.
    """
    if not date_examen:
        return []

    date_limite = date_examen - datetime.timedelta(days=7)

    # 1er pr√©l√®vement = 5 du mois suivant (quoi qu'il arrive)
    first = (date_devis.replace(day=1) + relativedelta(months=1)).replace(day=5)

    # Si c'est d√©j√† apr√®s la date limite -> impossible de proposer des pr√©l√®vements
    if first > date_limite:
        return []

    # Liste des dates 5/5/5... jusqu'√† la date limite
    dates = []
    d = first
    while d <= date_limite:
        dates.append(d)
        d = (d + relativedelta(months=1)).replace(day=5)

    n = len(dates)
    if n == 0:
        return []

    # R√©partition du reste sur n √©ch√©ances
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
        # üî• NOTATION INTERNE AUTO
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
        # (formation sold√©e 7 jours avant l‚Äôexamen)
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
            "VTC": 1500,
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
        # PARSING DATE D√âBUT
        # =========================
        date_debut = None
        try:
            import re
            mois_fr = {
                "janvier": 1, "f√©vrier": 2, "mars": 3, "avril": 4,
                "mai": 5, "juin": 6, "juillet": 7, "ao√ªt": 8,
                "septembre": 9, "octobre": 10, "novembre": 11, "d√©cembre": 12
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
            f"Date d‚Äô√©mission du devis : {date_devis.strftime('%d/%m/%Y')}"
        )
        y -= 25

        def v(key):
            val = (data.get(key) or "").strip()
            return val if val else "‚Äî"

        def yn(key):
            val = (data.get(key) or "").strip().upper()
            return val if val in ("OUI", "NON") else (val or "‚Äî")


        # =========================
        # INFOS STAGIAIRE ‚Äì FORMULAIRE COMPLET
        # =========================
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, y, "Informations stagiaire")
        y -= 20
        c.setFont("Helvetica", 11)
        
        lignes = [
            ("Nom", v("nom")),
            ("Pr√©nom", v("prenom")),
            ("T√©l√©phone", v("telephone")),
            ("Email", v("mail")),
            ("Confirmation email", v("mail_confirm")),
        
            ("Formation", v("formation")),
            ("Session / Dates", v("dates")),
            ("Dates r√©elles de formation VTC", v("dates_reelles_formation_vtc")),
        
            ("CPF consult√©", yn("cpf_consulte")),
            ("Montant CPF", f"{v('cpf_montant')} ‚Ç¨"),
        
            ("France Travail", yn("france_travail")),
            ("Si refus France Travail : financement personnel", yn("ft_refus_ok")),
            ("Financement personnel / fonds disponibles", yn("financement_perso")),
        
            ("Identit√© Num√©rique La Poste", yn("identite_numerique")),
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
            ("Garde √† vue / prise d‚Äôempreintes", yn("garde_vue")),
            ("Titulaire d‚Äôun titre de s√©jour", yn("titre_sejour")),
        ]
        
        for label, value in cnaps:
            if y < 120:
                c.showPage()
                y = height - 60
                c.setFont("Helvetica", 11)
            c.drawString(40, y, f"{label} : {value}")
            y -= 14


        # =========================
        # R√âCAP FINANCIER
        # =========================
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, y, "R√©capitulatif financier")
        y -= 20

        c.setFont("Helvetica", 11)
        c.drawString(40, y, f"Prix formation : {tarif} ‚Ç¨")
        y -= 14
        c.drawString(40, y, f"Montant CPF : {cpf} ‚Ç¨")
        y -= 14
        c.setFont("Helvetica-Bold", 11)
        c.drawString(40, y, f"Reste √† charge : {reste} ‚Ç¨")
        y -= 30

        c.setFont("Helvetica-Oblique", 9)
        c.drawString(
            40,
            y,
            "Pr√©l√®vements effectu√©s le 5 de chaque mois. "
            "Premier pr√©l√®vement le 5 du mois suivant l‚Äôinscription. "
            "La formation doit √™tre int√©gralement r√©gl√©e au plus tard 7 jours avant l‚Äôexamen."
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
            "motif": "Demande de devis d√©taill√©",
            "details": json.dumps(data, ensure_ascii=False),
            "date": datetime.datetime.now(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M"),
            "statut": "Non trait√©",
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

        # ‚úÖ Ne pas cr√©er de rappel Mohamed au d√©p√¥t du dossier devis.
        # Le rappel doit √™tre cr√©√© uniquement quand le statut passe √† "Envoy√©"
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
         if d.get("id") == devis_id and d.get("motif") == "Demande de devis d√©taill√©"),
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
         and d.get("motif") == "Demande de devis d√©taill√©"),
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
            if d.get("motif") == "Demande de devis d√©taill√©"
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
         and d.get("motif") == "Demande de devis d√©taill√©"),
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
    # √âch√©ancier (si date examen)
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

    # üîÅ √âch√©ancier manuel prioritaire
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
        prenom="Cl√©ment",
        nom="VAILLANT",
        formation_label="A3P ‚Äì Agent de Protection Physique des Personnes",
        dates="Mars 2026 ‚Üí Novembre 2026",
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
         and d.get("motif") == "Demande de devis d√©taill√©"),
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

    # üíæ Sauvegarde de l‚Äô√©ch√©ancier manuel (sans contr√¥le du reste)
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
         and d.get("motif") == "Demande de devis d√©taill√©"),
        None
    )
    if not devis:
        abort(404)

    plan_url = url_for("plan_public", token=devis.get("token_plan"), _external=True)

    email = devis.get("mail")
    prenom = devis.get("prenom", "").strip()
    
    # üîé Formation = dans devis["details"] (JSON)
    try:
        infos = json.loads(devis.get("details", "{}"))
    except:
        infos = {}
    
    formation = (infos.get("formation") or "").strip()
    
    formation_label = PLAN_FORMATIONS.get(formation, formation)


    subject = "üìÑ Votre devis d√©taill√© ‚Äî Int√©grale Academy"

    # ‚úÖ TEXTE BRUT align√© avec le HTML
    plain = (
        f"Bonjour {prenom},\n\n"
        f"Je fais suite √† votre demande de devis concernant notre formation {formation_label}.\n"
        "Je vous prie de bien vouloir trouver ci-dessous votre devis d√©taill√© :\n\n"
        f"{plan_url}\n\n"
        "Vous pouvez √©galement t√©l√©charger le dossier de pr√©sentation de notre formation :\n"
        "https://www.integraleacademy.com/dossiersfc\n\n"
        "Si vous avez la moindre question, n'h√©sitez pas √† nous contacter au 04 22 47 07 68.\n\n"
        "Bien cordialement,\n"
        "Cl√©ment VAILLANT - Directeur Int√©grale Academy\n"
        "Ce lien est personnel et s√©curis√©."
    )

    # ‚úÖ HTML = ton ‚Äú2e texte‚Äù, mais avec la vraie variable Python
    html = _wrap_html(
        "<h1>üìÑ Votre devis d√©taill√©</h1>",
        f"""
        <p>Bonjour <strong>{prenom}</strong>,</p>

        <p>
          Je fais suite √† votre demande de devis concernant notre formation <strong>{formation_label}</strong>.
          <br>
          Je vous prie de bien vouloir trouver ci-dessous votre <strong>devis d√©taill√©</strong> :
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
            üëâ Consulter mon devis d√©taill√©
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
            üìé T√©l√©charger le dossier de pr√©sentation de notre formation
          </a>
        </p>

        <p style="margin:0 0 10px;">
          Si vous avez la moindre question, n'h√©sitez pas √† nous contacter au
          <strong>04 22 47 07 68</strong>.
        </p>

        <p style="font-size:13px;color:#666;margin:0;">
          Ce lien est personnel et s√©curis√©.
        </p>

        <p style="margin-top:16px;">
          Bien cordialement,<br>
          <strong>Cl√©ment VAILLANT - Directeur Int√©grale Academy</strong>
        </p>
        """
    )

    # ... ensuite ton envoi email (smtp/brevo/etc.) avec plain+html


    # Envoi email
    email_sent = bool(send_email_html(
        to_emails=email,
        subject=subject,
        plain_text=plain,
        html_body=html
    ))
    if email_sent:
        _crm_record_quote_email_sent(data, devis, subject, html)

    # ---------------------------------
    # Statut + sauvegarde
    # ---------------------------------
    devis["statut_devis"] = "Envoy√©"
    devis["date_envoi_plan"] = datetime.datetime.now(
        pytz.timezone("Europe/Paris")
    ).strftime("%d/%m/%Y %H:%M")

    # üìû Cr√©er la demande de rappel dans l'admin uniquement quand le devis est envoy√©
    rappel_existant = next(
        (
            d for d in data.get("demandes", [])
            if d.get("source_devis_id") == devis_id
            and d.get("motif") == "Rappel suite devis envoy√©"
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
            "motif": "Rappel suite devis envoy√©",
            "details": (
                "Cr√©√©e automatiquement apr√®s clic sur ‚ÄòDevis envoy√©‚Äô.\n"
                f"Formation : {formation_label or 'Non pr√©cis√©e'}\n"
                f"Session : {(infos.get('dates') or 'Non pr√©cis√©e')}"
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
         if d.get("motif") == "Demande de devis d√©taill√©"
         and d.get("token_plan") == token),
        None
    )

    if not devis:
        return "Lien invalide ou expir√©", 404

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

    # üîÅ √âch√©ancier : manuel PRIORITAIRE, sinon automatique
    if devis.get("echeancier_manuel") and len(devis["echeancier_manuel"]) > 0:
        echeances = devis["echeancier_manuel"]
    else:
        # date examen (champ d√©di√© prioritaire, fallback via texte de session)
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

        # date devis (√©vite de recalculer un √©ch√©ancier diff√©rent selon la date de consultation)
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
        return "Lien invalide ou expir√©", 404

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
        "status": "r√©serv√©" if reserved else "inconnu"
    }, 200, {"Access-Control-Allow-Origin": "*"}











        



CRM_STATUSES = [
    "Nouveaux", "Blocage", "RDV programm√©",
    "RDV programm√© sans rendez-vous", "En cours",
    "A relancer", "Disqualifi√©", "Converti",
]
CRM_RESERVED_STATUSES = {"A relancer", "Disqualifi√©", "Converti"}
CRM_SECONDARY_STATUSES = (
    "Prochain RDV inscription", "Financement FT en cours",
    "Financement FT refus√©", "Def MOB", "POEI", "C2P en cours",
    "Transition pro", "March√© FT",
)
CRM_SECONDARY_ONLY_STATUSES = set(CRM_SECONDARY_STATUSES) | {"Session FT"}
CRM_FT_SECONDARY_BY_STATUS = {
    "en_cours_instruction": "Financement FT en cours",
    "refusee": "Financement FT refus√©",
}
CRM_FT_STATUS_BY_SECONDARY = {
    secondary: funding_status
    for funding_status, secondary in CRM_FT_SECONDARY_BY_STATUS.items()
}
CRM_MANUAL_STATUS_SOURCE = "manual"
CRM_ASSET_VERSION = "20260923-activity-navigation-1"
CRM_PAGE_LABELS = {
    "accueil": "Accueil",
    "fil-actu": "Fil d‚Äôactualit√©",
    "calendrier": "Calendrier",
    "contacts": "Contacts",
    "pistes": "Pistes",
    "relances": "Relances",
    "demandes-rappel": "Demande de rappel",
    "inscrits": "Inscrits",
    "disqualifies": "Disqualifi√©s",
    "notifications": "Notifications",
    "modeles": "Mod√®les",
    "exports": "Exports",
}


def _crm_migrate_registration_appointment_status(contact):
    """D√©place sans perte l'ancienne √©tape principale vers la seconde timeline."""
    if contact.get("statut") != "Prochain RDV inscription":
        return False
    contact["statut"] = "En cours"
    if not contact.get("statut_secondaire"):
        contact["statut_secondaire"] = "Prochain RDV inscription"
    return True


def _crm_statuses(data=None):
    """Retourne le pipeline personnalisable, compl√©t√© des statuts syst√®me."""
    manual_appointment_status = "RDV programm√© sans rendez-vous"
    configured = (data or {}).get("crm_statuses")
    if not isinstance(configured, list):
        return list(CRM_STATUSES)
    clean = []
    for value in configured:
        label = str(value or "").strip()
        if (label and label not in clean and label not in CRM_RESERVED_STATUSES
                and label not in CRM_SECONDARY_ONLY_STATUSES):
            clean.append(label)
    for required_status in (manual_appointment_status, "En cours"):
        if required_status in clean:
            clean.remove(required_status)
    insertion_index = (
        clean.index("RDV programm√©") + 1
        if "RDV programm√©" in clean else len(clean)
    )
    clean.insert(insertion_index, manual_appointment_status)
    clean.insert(insertion_index + 1, "En cours")
    clean.extend(status for status in CRM_STATUSES if status in CRM_RESERVED_STATUSES)
    return clean

CALENDLY_API_BASE = "https://api.calendly.com"
CALENDLY_WEBHOOK_EVENTS = ("invitee.created", "invitee.canceled")


class CalendlyAPIError(RuntimeError):
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.payload = payload if isinstance(payload, dict) else {}
        title = str(self.payload.get("title") or "Erreur Calendly").strip()
        message = str(self.payload.get("message") or "La requ√™te Calendly a √©chou√©.").strip()
        required = self.payload.get("required_scopes") or []
        if required:
            message = f"{message} Permissions requises : {', '.join(required)}."
        super().__init__(f"{title} : {message}")

    @property
    def insufficient_scope(self):
        return str(self.payload.get("title") or "").lower() == "insufficient scope"


def _calendly_token():
    return (
        os.getenv("CALENDLY_ACCESS_TOKEN")
        or os.getenv("CALENDLY_API_TOKEN")
        or os.getenv("CALENDLY_TOKEN")
        or ""
    ).strip()


def _calendly_signing_key():
    return (os.getenv("CALENDLY_WEBHOOK_SIGNING_KEY") or "").strip()


def _calendly_request(method, path, *, params=None, json_body=None, timeout=25):
    token = _calendly_token()
    if not token:
        raise RuntimeError("CALENDLY_ACCESS_TOKEN n'est pas configur√© dans Render.")
    response = requests.request(
        method,
        f"{CALENDLY_API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        params=params,
        json=json_body,
        timeout=timeout,
    )
    if response.status_code >= 400:
        try:
            payload = response.json()
        except ValueError:
            payload = {"message": response.text[:500] or "R√©ponse Calendly invalide."}
        raise CalendlyAPIError(response.status_code, payload)
    if response.status_code == 204 or not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("Calendly a renvoy√© une r√©ponse illisible.") from exc


def _calendly_paginated_collection(path, params=None, max_pages=100):
    params = dict(params or {})
    collection = []
    for _ in range(max_pages):
        page = _calendly_request("GET", path, params=params)
        collection.extend(page.get("collection") or [])
        token = (page.get("pagination") or {}).get("next_page_token")
        if not token:
            break
        params["page_token"] = token
    return collection


def _calendly_resource_uuid(uri, resource):
    value = str(uri or "").strip()
    match = re.fullmatch(
        rf"https://api\.calendly\.com/{re.escape(resource)}/([A-Za-z0-9_-]+)",
        value,
    )
    return match.group(1) if match else ""


def _calendly_callback_url():
    explicit = (os.getenv("CALENDLY_WEBHOOK_URL") or "").strip()
    if explicit:
        return explicit
    base_url = (
        os.getenv("PUBLIC_BASE_URL")
        or os.getenv("RENDER_EXTERNAL_URL")
        or request.url_root
    ).rstrip("/")
    if request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https":
        base_url = re.sub(r"^http://", "https://", base_url, count=1)
    return f"{base_url}/api/crm/calendly/webhook"


def _calendly_user_context():
    resource = (_calendly_request("GET", "/users/me").get("resource") or {})
    user_uri = resource.get("uri")
    organization_uri = resource.get("current_organization")
    if not user_uri or not organization_uri:
        raise RuntimeError("Calendly n'a pas renvoy√© l'utilisateur ou l'organisation associ√©e au jeton.")
    return {
        "user": user_uri,
        "organization": organization_uri,
        "account_name": resource.get("name") or "",
        "account_email": resource.get("email") or "",
    }


def _calendly_context_from_data(data):
    state = data.get("crm_calendly") or {}
    if state.get("user") and state.get("organization"):
        return {
            "user": state["user"],
            "organization": state["organization"],
            "scope": state.get("scope") or "organization",
        }
    context = _calendly_user_context()
    context["scope"] = "organization"
    return context


def _calendly_route_error(exc, *, action=""):
    if isinstance(exc, RuntimeError) and not isinstance(exc, CalendlyAPIError):
        payload = {"error": str(exc)}
        if action:
            payload["stage"] = action
        return jsonify(payload), 503
    message = str(exc)
    if action and isinstance(exc, CalendlyAPIError):
        details = []
        for detail in exc.payload.get("details") or []:
            if not isinstance(detail, dict):
                continue
            parameter = str(detail.get("parameter") or "").strip()
            detail_message = str(detail.get("message") or detail.get("code") or "").strip()
            if parameter and detail_message:
                details.append(f"{parameter} : {detail_message}")
            elif detail_message:
                details.append(detail_message)
        if details:
            message = f"{message} D√©tail Calendly : {' ; '.join(details)}"
        guidance = ""
        if "invalid argument" in message.casefold():
            guidance = (
                " V√©rifiez le type de rendez-vous, le cr√©neau, le fuseau horaire, "
                "le num√©ro de t√©l√©phone et les r√©ponses obligatoires."
            )
        message = f"Calendly a refus√© {action} : {message}.{guidance}".replace("..", ".")
    payload = {"error": message}
    if action:
        payload["stage"] = action
    return jsonify(payload), 502


def _crm_normalize_email(value):
    return str(value or "").strip().casefold()


def _crm_normalize_phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("0033"):
        digits = digits[2:]
    if len(digits) == 10 and digits.startswith("0") and digits[1] in "123456789":
        digits = f"33{digits[1:]}"
    return digits


def _crm_normalize_name(value):
    text = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text)


def _crm_is_empty(value):
    return value is None or (isinstance(value, str) and value.strip().casefold() in {"", "non renseign√©", "non renseigne"})


def _crm_names_compatible(contact, payload):
    supplied = [payload.get("prenom"), payload.get("nom")]
    stored = [contact.get("prenom"), contact.get("nom")]
    compared = [(a, b) for a, b in zip(stored, supplied) if not _crm_is_empty(a) and not _crm_is_empty(b)]
    return bool(compared) and all(_crm_normalize_name(a) == _crm_normalize_name(b) for a, b in compared)


_CRM_RECONCILIATION_LOCK = threading.RLock()


def _crm_serialized(view):
    """Keep JSON-file CRM read/modify/write transactions from overlapping."""
    @wraps(view)
    def serialized_view(*args, **kwargs):
        with _CRM_RECONCILIATION_LOCK:
            return view(*args, **kwargs)
    return serialized_view


def _find_or_create_crm_contact(data, payload, source, *, proposed_contact=None,
                                external_id=None, selected_contact_id=None,
                                force_create=False, ordered_coordinates=False,
                                record_activity=True,
                                create_on_ambiguity=False):
    """Point d'entr√©e unique, non destructif, pour toute nouvelle sollicitation CRM.

    Les coordonn√©es normalis√©es ne sont utilis√©es que pour chercher. Une valeur
    existante n'est jamais remplac√©e; seuls les champs explicitement autoris√©s et
    r√©ellement vides peuvent √™tre compl√©t√©s.
    """
    requests = data.setdefault("crm_inbound_requests", [])
    if external_id:
        duplicate = next((row for row in requests if row.get("source") == source and
                          row.get("external_id") == str(external_id)), None)
        if duplicate:
            return _crm_contact(data, duplicate.get("contact_id")), duplicate, False

    email = _crm_normalize_email(payload.get("mail") or payload.get("email"))
    phone = _crm_normalize_phone(payload.get("telephone"))
    contacts = data.setdefault("crm_contacts", [])
    email_matches = [c for c in contacts if email and _crm_normalize_email(c.get("mail")) == email]
    phone_matches = [c for c in contacts if phone and _crm_normalize_phone(c.get("telephone")) == phone]
    selected = _crm_contact(data, selected_contact_id) if selected_contact_id else None
    ambiguous_reasons = []
    contact = selected
    if not contact and not force_create:
        email_ids, phone_ids = {c.get("id") for c in email_matches}, {c.get("id") for c in phone_matches}
        if len(email_matches) > 1 or len(phone_matches) > 1:
            ambiguous_reasons.append("coordonn√©e associ√©e √† plusieurs fiches")
        elif email_matches and phone_matches and email_ids != phone_ids and not ordered_coordinates:
            ambiguous_reasons.append("e-mail et t√©l√©phone associ√©s √† des fiches diff√©rentes")
        elif email_matches and phone_matches and email_ids == phone_ids:
            contact = email_matches[0]
        elif ordered_coordinates and len(email_matches) == 1:
            contact = email_matches[0]
        elif ordered_coordinates and len(phone_matches) == 1:
            contact = phone_matches[0]
        else:
            candidate = (email_matches or phone_matches)
            if len(candidate) == 1:
                if ordered_coordinates or _crm_names_compatible(candidate[0], payload):
                    contact = candidate[0]
                else:
                    ambiguous_reasons.append("coordonn√©e concordante mais identit√© incompatible")
            elif any(_crm_names_compatible(c, payload) for c in contacts):
                ambiguous_reasons.append("nom et pr√©nom seuls concordants")

    now = _crm_now()
    inbound = {
        "id": str(uuid.uuid4()), "contact_id": None, "created_at": now,
        "source": source, "external_id": str(external_id) if external_id else "",
        "formation": str(payload.get("formation") or "").strip(),
        "commentaire": str(payload.get("commentaire") or payload.get("notes") or "").strip(),
        "coordinates": {"nom": payload.get("nom", ""), "prenom": payload.get("prenom", ""),
                        "mail": payload.get("mail") or payload.get("email", ""),
                        "telephone": payload.get("telephone", "")},
        "raw_payload": copy.deepcopy(dict(payload)), "differences": [],
        "status": "pending_review" if ambiguous_reasons and not force_create else "matched",
        "review_reasons": ambiguous_reasons, "resolved_at": None, "resolved_by": None,
    }
    created = False
    if not contact and (not ambiguous_reasons or create_on_ambiguity):
        contact = proposed_contact or {
            "id": str(uuid.uuid4()), "prenom": _crm_format_first_name(payload.get("prenom")),
            "nom": _crm_format_last_name(payload.get("nom")),
            "mail": str(payload.get("mail") or payload.get("email") or "").strip(),
            "telephone": str(payload.get("telephone") or "").strip(),
            "formation": str(payload.get("formation") or "").strip(), "statut": "Nouveaux",
            "origine": source, "created_at": now, "updated_at": now, "activities": [],
        }
        contacts.insert(0, contact); created = True; inbound["status"] = "created"
    if contact:
        inbound["contact_id"] = contact.get("id")
        proposed = proposed_contact if isinstance(proposed_contact, dict) else {}
        incoming_origin = (
            proposed.get("origine") or payload.get("origine")
            or CRM_ORIGIN_SOURCE_LABELS.get(source) or source
        )
        known_origin_keys = {
            _crm_origin_key(_crm_canonical_origin(item.get("origin")))
            for item in contact.get("source_history", [])
            if isinstance(item, dict)
        }
        known_origin_keys.add(
            _crm_origin_key(_crm_canonical_origin(contact.get("origine")))
        )
        meta_context = proposed.get("meta_source") or {}
        origin_context = {
            "campaign": meta_context.get("campaign_name", ""),
            "ad": meta_context.get("ad_name", ""),
            "form": meta_context.get("form_name", ""),
        }
        origin_added = _crm_record_origin(
            contact,
            incoming_origin,
            source=source,
            external_id=external_id,
            context=origin_context,
            date=now,
        )
        incoming_key = _crm_origin_key(_crm_canonical_origin(incoming_origin))
        if origin_added and not created and incoming_key not in known_origin_keys:
            _crm_activity(
                contact,
                "origine",
                f"Origine secondaire : {_crm_canonical_origin(incoming_origin)}",
                f"Origine principale conserv√©e : {contact.get('origine') or 'Non renseign√©e'}",
            )
        normalizers = {"mail": _crm_normalize_email, "telephone": _crm_normalize_phone,
                       "nom": _crm_normalize_name, "prenom": _crm_normalize_name}
        values = {"mail": payload.get("mail") or payload.get("email"), "telephone": payload.get("telephone"),
                  "nom": payload.get("nom"), "prenom": payload.get("prenom"),
                  "formation": payload.get("formation"), "lieu": payload.get("lieu") or payload.get("centre")}
        completed = []
        if not created:
            for field, incoming in values.items():
                if _crm_is_empty(incoming): continue
                if _crm_is_empty(contact.get(field)):
                    contact[field] = incoming; completed.append(field)
                else:
                    normalize = normalizers.get(field, lambda value: str(value or "").strip().casefold())
                    if normalize(contact.get(field)) != normalize(incoming): inbound["differences"].append(field)
            contact["updated_at"] = now
            detail = f"Source : {source}."
            if completed: detail += " Champs compl√©t√©s : " + ", ".join(completed) + "."
            if inbound["differences"]: detail += " Informations diff√©rentes √† v√©rifier : " + ", ".join(inbound["differences"]) + "."
            if record_activity:
                _crm_activity(contact, "inbound_request", "Nouvelle demande re√ßue", detail)
    requests.insert(0, inbound)
    return contact, inbound, created


def find_or_create_crm_contact(data, payload, source, **options):
    """S√©rialise le rapprochement dans un processus applicatif."""
    with _CRM_RECONCILIATION_LOCK:
        return _find_or_create_crm_contact(data, payload, source, **options)


_META_LEAD_FIELDS = (
    "id", "lead_id", "leadgen_id", "created_time", "page_id", "form_id",
    "form_name", "ad_id", "ad_name", "adset_id", "adset_name", "campaign_id",
    "campaign_name", "platform", "full_name", "first_name", "last_name", "email",
    "phone_number", "city", "zip_code", "postal_code", "formation", "centre",
    "lieu", "dates", "dates_formation",
)

_META_DEFAULT_FORMATION = "A3P"
_META_DEFAULT_LOCATION = "C√¥te d‚ÄôAzur"

_META_ANSWER_CONTAINERS = {
    "fielddata", "fields", "answers", "questionsandanswers", "questionanswers",
}
_META_PAYLOAD_WRAPPERS = {"data", "lead", "payload", "body"}

_META_DIRECT_CRM_QUESTION_ALIASES = {
    "formation": "formation",
    "formationsouhaitee": "formation",
    "formationdesiree": "formation",
    "formationchoisie": "formation",
    "typedeformation": "formation",
    "centre": "lieu",
    "formationcentre": "lieu",
    "centreformation": "lieu",
    "lieu": "lieu",
    "lieudeformation": "lieu",
    "lieuformation": "lieu",
    "villedeformation": "lieu",
    "dates": "dates_formation",
    "date": "dates_formation",
    "session": "dates_formation",
    "datesformation": "dates_formation",
    "datesdeformation": "dates_formation",
    "datessouhaitees": "dates_formation",
    "sessionformation": "dates_formation",
    "cpf": "cpf",
    "comptecpf": "cpf",
    "cpfconsulte": "cpf",
    "montantcpf": "cpf_montant",
    "cpfmontant": "cpf_montant",
    "montantcpfdisponible": "cpf_montant",
    "paliercpf": "cpf_palier",
    "cpfpalier": "cpf_palier",
    "tranchecpf": "cpf_palier",
    "cpftranche": "cpf_palier",
    "cartepro": "carte_pro",
    "carteprofessionnelle": "carte_pro",
    "cnapsok": "carte_pro",
    "titredesejour": "titre_sejour",
    "gardevue": "garde_vue",
    "antecedents": "antecedents",
    "antecedentsjudiciaires": "antecedents",
    "comptecnaps": "compte_cnaps",
    "parcoursdesp": "desp_type",
    "typedesp": "desp_type",
    "identitenumerique": "identite_creation",
    "identitenumeriquelaposte": "identite_creation",
    "identitecreation": "identite_creation",
    "identiteok": "identite_ok",
    "francetravail": "financement_ft",
    "financementfrancetravail": "financement_ft",
    "financementft": "financement_ft",
    "statutdemandefinancementft": "statut_demande_financement_ft",
    "statutdemandefrancetravail": "statut_demande_financement_ft",
    "montantaccordeft": "montant_accorde_ft",
    "montantaccordefrancetravail": "montant_accorde_ft",
    "financementpersopossible": "financement_perso_possible",
    "financementpersonnelpossible": "financement_perso_possible",
    "ftrefusok": "refus_ft_perso",
    "refusftperso": "refus_ft_perso",
    "financementperso": "reste_a_charge_perso",
    "resteachargeperso": "reste_a_charge_perso",
    "inscritft": "inscrit_ft",
    "inscritfrancetravail": "inscrit_ft",
    "commentaire": "commentaires",
    "commentaires": "commentaires",
    "notes": "commentaires",
}

_META_CRM_COMPLETION_FIELDS = (
    "formation", "lieu", "dates_formation", "cpf", "cpf_montant", "cpf_palier",
    "carte_pro",
    "titre_sejour", "garde_vue", "antecedents", "compte_cnaps", "desp_type",
    "identite_creation", "identite_ok", "financement_ft",
    "statut_demande_financement_ft", "montant_accorde_ft",
    "financement_perso_possible", "refus_ft_perso", "reste_a_charge_perso",
    "inscrit_ft", "commentaires",
)


def _meta_key(value):
    text = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    return "".join(char for char in text if char.isalnum() and not unicodedata.combining(char))


def _meta_scalar(value):
    if isinstance(value, list):
        return ", ".join(
            scalar for item in value if (scalar := _meta_scalar(item))
        ).strip()
    if isinstance(value, dict):
        for key in ("values", "value", "answer", "answers", "response"):
            if key in value:
                return _meta_scalar(value.get(key))
        return ""
    if isinstance(value, tuple):
        return ""
    return str(value or "").strip()


def _meta_words(value):
    text = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _meta_is_cpf_tier(value):
    """Distingue une tranche CPF d'un montant exact exploitable par le score."""
    text = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    if any(term in text for term in (
        "plus de", "moins de", "entre ", "jusqu", "au moins", "et plus",
    )):
        return True
    return bool(re.search(
        r"\d[\d\s\u00a0]*(?:-|‚Äì|‚Äî|\s+a\s+|\s+au\s+)\d",
        text,
    ))


def _crm_is_meta_contact(contact):
    """Identifie une piste META m√™me si son origine a √©t√© effac√©e par l'ancien formulaire."""
    return any(
        str(contact.get(field) or "").strip().casefold() in {"meta", "meta_zapier"}
        for field in ("origine", "source")
    )


def _crm_enforce_meta_defaults(contact):
    """Conserve la provenance et le lieu communs aux pistes META."""
    if not _crm_is_meta_contact(contact):
        return False
    expected = {
        "origine": "META",
        "lieu": _META_DEFAULT_LOCATION,
    }
    changed = False
    for field, value in expected.items():
        if contact.get(field) != value:
            contact[field] = value
            changed = True
    if changed:
        contact["updated_at"] = _crm_now()
    return changed


def _parse_meta_lead_payload(payload):
    """Tol√®re les libell√©s Zapier/Meta usuels sans √©liminer les questions inconnues."""
    aliases = {_meta_key(field): field for field in _META_LEAD_FIELDS}
    aliases.update({
        "leadgenid": "leadgen_id", "leadid": "lead_id", "fullname": "full_name",
        "firstname": "first_name", "lastname": "last_name", "mail": "email",
        "emailaddress": "email", "phone": "phone_number", "telephone": "phone_number",
        "mobile": "phone_number", "zipcode": "zip_code", "postalcode": "postal_code",
    })
    fields, custom = {}, {}

    def consume(name, value):
        canonical = aliases.get(_meta_key(name))
        scalar = _meta_scalar(value)
        if canonical:
            if scalar and not fields.get(canonical):
                fields[canonical] = scalar
        elif _meta_key(name) not in _META_ANSWER_CONTAINERS | _META_PAYLOAD_WRAPPERS:
            custom[str(name)] = copy.deepcopy(value)

    def consume_answers(value):
        if isinstance(value, list):
            for item in value:
                consume_answers(item)
            return
        if not isinstance(value, dict):
            return
        question = (
            value.get("name") or value.get("question") or value.get("label")
            or value.get("key") or value.get("title")
        )
        if question:
            answer = value.get(
                "values",
                value.get("value", value.get("answer", value.get("response"))),
            )
            consume(question, answer)
            if not aliases.get(_meta_key(question)):
                custom[str(question)] = copy.deepcopy(answer)
            return
        for name, answer in value.items():
            consume(name, answer)

    def consume_mapping(mapping):
        if not isinstance(mapping, dict):
            return
        for name, value in mapping.items():
            key = _meta_key(name)
            if key in _META_ANSWER_CONTAINERS:
                consume_answers(value)
            elif key in _META_PAYLOAD_WRAPPERS:
                if isinstance(value, dict):
                    consume_mapping(value)
                elif isinstance(value, list):
                    for item in value:
                        consume_mapping(item)
            else:
                consume(name, value)

    consume_mapping(payload)
    full_name = fields.get("full_name", "")
    if full_name and not fields.get("first_name") and not fields.get("last_name"):
        parts = full_name.split(None, 1)
        fields["first_name"] = parts[0]
        fields["last_name"] = parts[1] if len(parts) > 1 else ""
    return fields, custom


def _meta_yes_no(value):
    normalized = _meta_words(value)
    if not normalized:
        return ""
    negative = (
        normalized in {"non", "no", "false", "0", "aucun", "aucune", "pas encore"}
        or normalized.startswith(("non ", "pas "))
        or any(phrase in normalized for phrase in (
            "je n ai pas", "je ne suis pas", "non inscrit", "non cree", "pas cree",
            "sans compte", "aucune demande",
        ))
    )
    if negative:
        return "NON"
    positive = (
        normalized in {"oui", "yes", "true", "1"}
        or normalized.startswith(("oui ", "yes "))
        or any(phrase in normalized for phrase in (
            "deja cree", "deja inscrit", "je suis inscrit", "j ai un", "je dispose",
        ))
    )
    return "OUI" if positive else ""


def _meta_formation(value):
    normalized = _meta_words(value)
    if not normalized:
        return "", ""
    formation, desp_type = "", ""
    if (re.search(r"\ba3p\b", normalized) or re.search(r"\bapr\b", normalized)
            or "protection physique" in normalized or "protection rapprochee" in normalized
            or "garde du corps" in normalized):
        formation = "A3P"
    elif ("desp" in normalized
          or ("dirigeant" in normalized and "securite" in normalized)):
        formation = "DESP"
        if "vae" in normalized or "validation des acquis" in normalized:
            desp_type = "VAE"
        elif "initial" in normalized:
            desp_type = "INITIAL"
    elif "ssiap" in normalized or "securite incendie" in normalized:
        formation = "SSIAP 1"
    elif "vtc" in normalized or "chauffeur" in normalized:
        formation = "Chauffeur VTC"
    elif (re.search(r"\baps\b", normalized) or "agent de prevention" in normalized
          or "agent de securite privee" in normalized):
        formation = "APS"
    return formation, desp_type


def _meta_place(value):
    normalized = _meta_words(value)
    if not normalized:
        return ""
    if "paris" in normalized or "ile de france" in normalized:
        return "Paris"
    if any(term in normalized for term in (
        "cote d azur", "puget", "frejus", "saint raphael", "var", "paca",
    )):
        return "C√¥te d‚ÄôAzur"
    if any(term in normalized for term in ("auvergne", "aurillac", "cantal")):
        return "Auvergne"
    return ""


def _meta_funding_status(value):
    normalized = _meta_words(value)
    if not normalized:
        return ""
    if "refus" in normalized or "rejete" in normalized:
        return "refusee"
    if "annul" in normalized or "abandon" in normalized:
        return "annulee"
    if any(term in normalized for term in ("accepte", "accorde", "valide")):
        return "acceptee"
    if any(term in normalized for term in ("instruction", "en cours", "en attente")):
        return "en_cours_instruction"
    if any(term in normalized for term in ("transmis", "envoye", "depose")):
        return "transmise"
    if any(term in normalized for term in ("a preparer", "pas encore depose")):
        return "a_preparer"
    if any(term in normalized for term in ("aucune demande", "pas de demande")):
        return "aucune_demande"
    return ""


def _meta_question_excluded_from_scoring(question):
    """Garde Q1 √† Q4 visibles sans les projeter dans les faits m√©tier du CRM."""
    normalized = _meta_words(question)
    if re.match(r"^q\s*[1-4]\b", normalized):
        return True
    excluded_signatures = (
        ("formation officielle", "agent de protection physique"),
        ("cours", "examen", "francais"),
        ("9 semaines", "presentiel", "puget"),
        ("pris connaissance", "tarif", "4 200"),
    )
    return any(
        all(term in normalized for term in signature)
        for signature in excluded_signatures
    )


def _meta_crm_question_field(question):
    key = _meta_key(question)
    direct = _META_DIRECT_CRM_QUESTION_ALIASES.get(key)
    if direct:
        return direct
    if "montant" in key and "cpf" in key:
        return "cpf_montant"
    if "identitenumerique" in key or "franceconnect" in key:
        return "identite_ok" if any(term in key for term in ("fonction", "active", "valide")) else "identite_creation"
    if "inscrit" in key and ("francetravail" in key or "poleemploi" in key):
        return "inscrit_ft"
    if "demandeuremploi" in key:
        return "inscrit_ft"
    if "statut" in key and ("francetravail" in key or "financementft" in key):
        return "statut_demande_financement_ft"
    if "montant" in key and ("accord" in key or "accepte" in key) and "francetravail" in key:
        return "montant_accorde_ft"
    if any(term in key for term in ("payerpersonnellement", "financerpersonnellement", "financementpersonnel")):
        return "financement_perso_possible"
    if "refus" in key and ("francetravail" in key or "ft" in key) and "financ" in key:
        return "refus_ft_perso"
    if "resteacharge" in key or ("financementpersonnel" in key and "refus" not in key):
        return "reste_a_charge_perso"
    if "financement" in key and ("francetravail" in key or key.endswith("ft")):
        return "financement_ft"
    if "cpf" in key and any(term in key for term in ("compte", "consulte", "dispose", "avezvous")):
        return "cpf"
    if "carteprofessionnelle" in key or "cartepro" in key:
        return "carte_pro"
    if "comptecnaps" in key:
        return "compte_cnaps"
    if "titredesejour" in key:
        return "titre_sejour"
    if "gardevue" in key or "empreinte" in key:
        return "garde_vue"
    if "antecedent" in key or "casierjudiciaire" in key:
        return "antecedents"
    if "parcours" in key and "desp" in key:
        return "desp_type"
    if ("date" in key or "session" in key) and "naissance" not in key:
        return "dates_formation"
    if any(term in key for term in ("lieu", "centre", "villeformation")) and "domicile" not in key:
        return "lieu"
    if "formation" in key and not any(term in key for term in ("date", "lieu", "centre")):
        return "formation"
    if any(term in key for term in ("commentaire", "precision", "projetformation", "besoinparticulier")):
    „Ω|·º≠z &ä€^u}çÖπçï±±Ö—•Ωπ}ëÖ—î°¡ÖÂ±ΩÖê∞ÅπΩ‹ı9Ωπî§Ë(ÄÄÄÄààâIï—’…∏Å—°îÅçÖπçï±±Ö—•Ω∏ÅëÖ‰Å•∏ÅAÖ…•Ã∞Å¡…ïôï……•πúÅÖ±ïπë±‰ùÃÅ—•µïÕ—Öµ¿∏ààà(ÄÄÄÅçÖπçï±±Ö—•Ω∏ÄÙÅ¡ÖÂ±ΩÖêπùï–†âçÖπçï±±Ö—•Ω∏à§ÅΩ»ÅÌÙ(ÄÄÄÅ…Ö›}—•µïÕ—Öµ¿ÄÙÄ†(ÄÄÄÄÄÄÄÅçÖπçï±±Ö—•Ω∏πùï–†âç…ïÖ—ïë}Ö–à§(ÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°çÖπçï±±Ö—•Ω∏∞Åë•ç–§(ÄÄÄÄÄÄÄÅï±ÕîÄàà(ÄÄÄÄ§ÅΩ»Å¡ÖÂ±ΩÖêπùï–†â’¡ëÖ—ïë}Ö–à§(ÄÄÄÅ¡Ö…•ÃÄÙÅ¡Â—Ëπ—•µïÈΩπî†â’…Ω¡îΩAÖ…•Ãà§(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅçÖπçï±±ïë}Ö–ÄÙÅëÖ—ï—•µîπëÖ—ï—•µîπô…Ωµ•ÕΩôΩ…µÖ–†(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—»°…Ö›}—•µïÕ—Öµ¿ÅΩ»Äàà§π…ï¡±Öçî†âhà∞Äà¨¿¿Ë¿¿à§(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ•òÅçÖπçï±±ïë}Ö–π—È•πôºÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÄÄÄÄÅçÖπçï±±ïë}Ö–ÄÙÅ¡Â—ËπUQπ±ΩçÖ±•Èî°çÖπçï±±ïë}Ö–§(ÄÄÄÄÄÄÄÅçÖπçï±±ïë}Ö–ÄÙÅçÖπçï±±ïë}Ö–πÖÕ—•µïÈΩπî°¡Ö…•Ã§(ÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»§Ë(ÄÄÄÄÄÄÄÅçÖπçï±±ïë}Ö–ÄÙÅπΩ‹ÅΩ»ÅëÖ—ï—•µîπëÖ—ï—•µîππΩ‹°¡Ö…•Ã§(ÄÄÄÄÄÄÄÅ•òÅçÖπçï±±ïë}Ö–π—È•πôºÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÄÄÄÄÅçÖπçï±±ïë}Ö–ÄÙÅ¡Ö…•Ãπ±ΩçÖ±•Èî°çÖπçï±±ïë}Ö–§(ÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÅçÖπçï±±ïë}Ö–ÄÙÅçÖπçï±±ïë}Ö–πÖÕ—•µïÈΩπî°¡Ö…•Ã§(ÄÄÄÅ…ï—’…∏ÅçÖπçï±±ïë}Ö–πëÖ—î†§π•ÕΩôΩ…µÖ–†§(()ëïòÅ}ç…µ}çÖ±ïπë±Â}âΩΩ≠•πù}Õ’¡ï…ÕïëïÕ}Öç—•Ÿï}…ï±Öπçî°çΩπ—Öç–∞ÅÖ¡¡Ω•π—µïπ–§Ë(ÄÄÄÄààâIï—’…∏Å›°ï—°ï»ÅÖ∏Åï·•Õ—•πúÅâΩΩ≠•πúÅ•ÃÅπï›ï»Å—°Ö∏Å—°îÅΩ¡ï∏ÅôΩ±±Ω‹µ’¿∏((ÄÄÄÅ’±∞ÅÕÂπç°…Ωπ•ÈÖ—•ΩπÃÅ…ï¡±Ö‰ÅÖ¡¡Ω•π—µïπ—ÃÅ—°Ö–ÅµÖ‰ÅÖ±…ïÖë‰ÅâîÅ•∏Å—°îÅ±ΩçÖ∞(ÄÄÄÅçÖç°î∏ÄÅΩµ¡Ö…•πúÅç…ïÖ—•Ω∏Å—•µïÕ—Öµ¡ÃÅ…ï¡Ö•…ÃÅ¡…îµï·•Õ—•πúÅ•πçΩπÕ•Õ—ïπ–(ÄÄÄÅëÖ—ÑÅ›•—°Ω’–ÅçÖπçï±±•πúÅÑÅôΩ±±Ω‹µ’¿Åëï±•âï…Ö—ï±‰Åç…ïÖ—ïêÅÖô—ï»ÅâΩΩ≠•πú∏(ÄÄÄÅ5•ÕÕ•πúÅΩ»ÅµÖ±ôΩ…µïêÅ—•µïÕ—Öµ¡ÃÅÖ…îÅ±ïô–Å’π—Ω’ç°ïêÅ…Ö—°ï»Å—°Ö∏Å…•Õ≠•πú(ÄÄÄÅëïÕ—…’ç—•ŸîÅ°•Õ—Ω…‰Åç°ÖπùïÃ∏(ÄÄÄÄààà(ÄÄÄÅâΩΩ≠ïë}Ö–ÄÙÄ†(ÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ–πùï–†âç…ïÖ—ïë}Ö–à§(ÄÄÄÄÄÄÄÅΩ»ÅÖ¡¡Ω•π—µïπ–πùï–†âçÖ±ïπë±Â}ç…ïÖ—ïë}Ö–à§(ÄÄÄÄÄÄÄÅΩ»Äàà(ÄÄÄÄ§(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅâΩΩ≠ïë}Ö–ÄÙÅëÖ—ï—•µîπëÖ—ï—•µîπô…Ωµ•ÕΩôΩ…µÖ–†(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—»°âΩΩ≠ïë}Ö–§π…ï¡±Öçî†âhà∞Äà¨¿¿Ë¿¿à§(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ•òÅâΩΩ≠ïë}Ö–π—È•πôºÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÄÄÄÄÅâΩΩ≠ïë}Ö–ÄÙÅ¡Â—ËπUQπ±ΩçÖ±•Èî°âΩΩ≠ïë}Ö–§(ÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÅâΩΩ≠ïë}Ö–ÄÙÅâΩΩ≠ïë}Ö–πÖÕ—•µïÈΩπî°¡Â—ËπUQ§(ÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî((ÄÄÄÅ}ç…µ}ïπÕ’…ï}…ï±ÖπçïÃ°çΩπ—Öç–§(ÄÄÄÅôΩ»Å…ï±ÖπçîÅ•∏ÅçΩπ—Öç–πùï–†â…ï±ÖπçïÃà∞Åmt§Ë(ÄÄÄÄÄÄÄÅ•òÅ…ï±Öπçîπùï–†âÕ—Ö—’Ãà§ÄÑÙÄâÕç°ïë’±ïêàË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅç…ïÖ—ïë}Ö–ÄÙÅëÖ—ï—•µîπëÖ—ï—•µîπô…Ωµ•ÕΩôΩ…µÖ–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—»°…ï±Öπçîπùï–†âç…ïÖ—ïë}Ö–à§ÅΩ»Äàà§π…ï¡±Öçî†âhà∞Äà¨¿¿Ë¿¿à§(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅç…ïÖ—ïë}Ö–π—È•πôºÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅç…ïÖ—ïë}Ö–ÄÙÅ¡Â—ËπUQπ±ΩçÖ±•Èî°ç…ïÖ—ïë}Ö–§(ÄÄÄÄÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅç…ïÖ—ïë}Ö–ÄÙÅç…ïÖ—ïë}Ö–πÖÕ—•µïÈΩπî°¡Â—ËπUQ§(ÄÄÄÄÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÄåÅ≈’Ö∞ÅÕïçΩπêµ…ïÕΩ±’—•Ω∏Å—•µïÕ—Öµ¡ÃÅÖ…îÅÖµâ•ù’Ω’ÃÏÅ¡…ïÕï…Ÿ•πúÅ—°î(ÄÄÄÄÄÄÄÄåÅôΩ±±Ω‹µ’¿Å•ÃÅÕÖôï»Å—°Ö∏ÅçÖπçï±±•πúÅÑÅ¡Ω—ïπ—•Ö±±‰Å±Ö—ï»ÅµÖπ’Ö∞ÅÖç—•Ω∏∏(ÄÄÄÄÄÄÄÅ•òÅç…ïÖ—ïë}Ö–ÄÅâΩΩ≠ïë}Ö–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅQ…’î(ÄÄÄÅ…ï—’…∏ÅÖ±Õî(()ëïòÅ}ç…µ}’¡Õï…—}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ–†(ÄÄÄÅëÖ—Ñ∞(ÄÄÄÅ¡ÖÂ±ΩÖê∞(ÄÄÄÄ®∞(ÄÄÄÅ›ïâ°ΩΩ≠}ïŸïπ–Ùàà∞(ÄÄÄÅÕΩ’…çîÙâ›ïâ°ΩΩ¨à∞(ÄÄÄÅçΩπ—Öç—}•êı9Ωπî∞(ÄÄÄÅ…ïçΩ…ë}Öç—•Ÿ•—‰ıQ…’î∞(§Ë(ÄÄÄÅÕç°ïë’±ïë}ïŸïπ–ÄÙÅ¡ÖÂ±ΩÖêπùï–†âÕç°ïë’±ïë}ïŸïπ–à§ÅΩ»ÅÌÙ(ÄÄÄÅ•πŸ•—ïï}’…§ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†â’…§à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅïŸïπ—}’…§ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âïŸïπ–à§ÅΩ»ÅÕç°ïë’±ïë}ïŸïπ–πùï–†â’…§à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅïµÖ•∞ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âïµÖ•∞à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅÖ¡¡Ω•π—µïπ—ÃÄÙÅëÖ—ÑπÕï—ëïôÖ’±–†âç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Ãà∞Åmt§(ÄÄÄÅï·•Õ—•πúÄÙÅπï·–†(ÄÄÄÄÄÄÄÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅÖ¡¡Ω•π—µïπ—Ã(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ•πŸ•—ïï}’…§ÅÖπêÅ•—ï¥πùï–†â•πŸ•—ïï}’…§à§ÄÙÙÅ•πŸ•—ïï}’…§(ÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÅ9Ωπî∞(ÄÄÄÄ§(ÄÄÄÅ•òÅπΩ–Åï·•Õ—•πúÅÖπêÅïŸïπ—}’…§ÅÖπêÅïµÖ•∞Ë(ÄÄÄÄÄÄÄÅï·•Õ—•πúÄÙÅπï·–†(ÄÄÄÄÄÄÄÄÄÄÄÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅÖ¡¡Ω•π—µïπ—Ã(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ•—ï¥πùï–†âïŸïπ—}’…§à§ÄÙÙÅïŸïπ—}’…§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅ}ç…µ}πΩ…µÖ±•Èï}ïµÖ•∞°•—ï¥πùï–†â•πŸ•—ïï}ïµÖ•∞à§§ÄÙÙÅ}ç…µ}πΩ…µÖ±•Èï}ïµÖ•∞°ïµÖ•∞§(ÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ9Ωπî∞(ÄÄÄÄÄÄÄÄ§((ÄÄÄÅ¡…ïŸ•Ω’Õ}Õ—Ö—’ÃÄÙÅï·•Õ—•πúπùï–†âÕ—Ö—’Ãà§Å•òÅï·•Õ—•πúÅï±ÕîÅ9Ωπî(ÄÄÄÅ¡…ïŸ•Ω’Õ}Õ—Ö…–ÄÙÅï·•Õ—•πúπùï–†âÕ—Ö…—}—•µîà§Å•òÅï·•Õ—•πúÅï±ÕîÅ9Ωπî(ÄÄÄÅ•πŸ•—ïï}¡°ΩπîÄÙÅ}ç…µ}çÖ±ïπë±Â}¡ÖÂ±ΩÖë}¡°Ωπî°¡ÖÂ±ΩÖê§(ÄÄÄÅÕ—Ö—’ÃÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âÕ—Ö—’Ãà§ÅΩ»ÅÕç°ïë’±ïë}ïŸïπ–πùï–†âÕ—Ö—’Ãà§ÅΩ»ÄâÖç—•Ÿîà§(ÄÄÄÅ•òÅ›ïâ°ΩΩ≠}ïŸïπ–ÄÙÙÄâ•πŸ•—ïîπçÖπçï±ïêàÅΩ»ÅÕç°ïë’±ïë}ïŸïπ–πùï–†âÕ—Ö—’Ãà§ÄÙÙÄâçÖπçï±ïêàË(ÄÄÄÄÄÄÄÅÕ—Ö—’ÃÄÙÄâçÖπçï±ïêà(ÄÄÄÅµïµâï…Õ°•¡ÃÄÙÅÕç°ïë’±ïë}ïŸïπ–πùï–†âïŸïπ—}µïµâï…Õ°•¡Ãà§ÅΩ»Åmt(ÄÄÄÅ°ΩÕ–ÄÙÅµïµâï…Õ°•¡Õl¡tÅ•òÅµïµâï…Õ°•¡ÃÅï±ÕîÅÌÙ(ÄÄÄÅπΩ‹ÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅÖ¡¡Ω•π—µïπ–ÄÙÅï·•Õ—•πúÅΩ»ÅÏ(ÄÄÄÄÄÄÄÄâ•êàËÅÕ—»°’’•êπ’’•ê–†§§∞(ÄÄÄÄÄÄÄÄâç…ïÖ—ïë}Ö–àËÅπΩ‹∞(ÄÄÄÅÙ(ÄÄÄÅÖ¡¡Ω•π—µïπ–π’¡ëÖ—î°Ï(ÄÄÄÄÄÄÄÄâ•πŸ•—ïï}’…§àËÅ•πŸ•—ïï}’…§∞(ÄÄÄÄÄÄÄÄâïŸïπ—}’…§àËÅïŸïπ—}’…§∞(ÄÄÄÄÄÄÄÄâïŸïπ—}—Â¡ï}’…§àËÅÕç°ïë’±ïë}ïŸïπ–πùï–†âïŸïπ—}—Â¡îà§ÅΩ»ÅÖ¡¡Ω•π—µïπ–πùï–†âïŸïπ—}—Â¡ï}’…§à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄâπÖµîàËÅÕç°ïë’±ïë}ïŸïπ–πùï–†âπÖµîà§ÅΩ»ÅÖ¡¡Ω•π—µïπ–πùï–†âπÖµîà§ÅΩ»ÄâIïπëïËµŸΩ’ÃÅÖ±ïπë±‰à∞(ÄÄÄÄÄÄÄÄâÕ—Ö…—}—•µîàËÅÕç°ïë’±ïë}ïŸïπ–πùï–†âÕ—Ö…—}—•µîà§ÅΩ»ÅÖ¡¡Ω•π—µïπ–πùï–†âÕ—Ö…—}—•µîà§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄâïπë}—•µîàËÅÕç°ïë’±ïë}ïŸïπ–πùï–†âïπë}—•µîà§ÅΩ»ÅÖ¡¡Ω•π—µïπ–πùï–†âïπë}—•µîà§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄâÕ—Ö—’ÃàËÅÕ—Ö—’Ã∞(ÄÄÄÄÄÄÄÄâ•πŸ•—ïï}πÖµîàËÅ¡ÖÂ±ΩÖêπùï–†âπÖµîà§ÅΩ»ÅÖ¡¡Ω•π—µïπ–πùï–†â•πŸ•—ïï}πÖµîà§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄâ•πŸ•—ïï}ïµÖ•∞àËÅïµÖ•∞ÅΩ»ÅÖ¡¡Ω•π—µïπ–πùï–†â•πŸ•—ïï}ïµÖ•∞à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄâ•πŸ•—ïï}¡°ΩπîàËÅ•πŸ•—ïï}¡°ΩπîÅΩ»ÅÖ¡¡Ω•π—µïπ–πùï–†â•πŸ•—ïï}¡°Ωπîà§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄâ•πŸ•—ïï}—•µïÈΩπîàËÅ¡ÖÂ±ΩÖêπùï–†â—•µïÈΩπîà§ÅΩ»ÅÖ¡¡Ω•π—µïπ–πùï–†â•πŸ•—ïï}—•µïÈΩπîà§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄâ°ΩÕ—}πÖµîàËÅ°ΩÕ–πùï–†â’Õï…}πÖµîà§ÅΩ»ÅÖ¡¡Ω•π—µïπ–πùï–†â°ΩÕ—}πÖµîà§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄâ°ΩÕ—}ïµÖ•∞àËÅ°ΩÕ–πùï–†â’Õï…}ïµÖ•∞à§ÅΩ»ÅÖ¡¡Ω•π—µïπ–πùï–†â°ΩÕ—}ïµÖ•∞à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄâ±ΩçÖ—•Ω∏àËÅÕç°ïë’±ïë}ïŸïπ–πùï–†â±ΩçÖ—•Ω∏à§ÅΩ»ÅÖ¡¡Ω•π—µïπ–πùï–†â±ΩçÖ—•Ω∏à§∞(ÄÄÄÄÄÄÄÄâçÖπçï±}’…∞àËÅ¡ÖÂ±ΩÖêπùï–†âçÖπçï±}’…∞à§ÅΩ»ÅÖ¡¡Ω•π—µïπ–πùï–†âçÖπçï±}’…∞à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄâ…ïÕç°ïë’±ï}’…∞àËÅ¡ÖÂ±ΩÖêπùï–†â…ïÕç°ïë’±ï}’…∞à§ÅΩ»ÅÖ¡¡Ω•π—µïπ–πùï–†â…ïÕç°ïë’±ï}’…∞à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄâ…ïÕç°ïë’±ïêàËÅâΩΩ∞°¡ÖÂ±ΩÖêπùï–†â…ïÕç°ïë’±ïêà§§∞(ÄÄÄÄÄÄÄÄâΩ±ë}•πŸ•—ïîàËÅ¡ÖÂ±ΩÖêπùï–†âΩ±ë}•πŸ•—ïîà§∞(ÄÄÄÄÄÄÄÄâπï›}•πŸ•—ïîàËÅ¡ÖÂ±ΩÖêπùï–†âπï›}•πŸ•—ïîà§∞(ÄÄÄÄÄÄÄÄâçÖπçï±±Ö—•Ω∏àËÅ¡ÖÂ±ΩÖêπùï–†âçÖπçï±±Ö—•Ω∏à§∞(ÄÄÄÄÄÄÄÄâçÖ±ïπë±Â}ç…ïÖ—ïë}Ö–àËÅ¡ÖÂ±ΩÖêπùï–†âç…ïÖ—ïë}Ö–à§ÅΩ»ÅÖ¡¡Ω•π—µïπ–πùï–†âçÖ±ïπë±Â}ç…ïÖ—ïë}Ö–à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄâçÖ±ïπë±Â}’¡ëÖ—ïë}Ö–àËÅ¡ÖÂ±ΩÖêπùï–†â’¡ëÖ—ïë}Ö–à§ÅΩ»ÅÖ¡¡Ω•π—µïπ–πùï–†âçÖ±ïπë±Â}’¡ëÖ—ïë}Ö–à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄâÕΩ’…çîàËÅÕΩ’…çî∞(ÄÄÄÄÄÄÄÄâ’¡ëÖ—ïë}Ö–àËÅπΩ‹∞(ÄÄÄÅÙ§((ÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§Å•òÅçΩπ—Öç—}•êÅï±ÕîÅ9Ωπî(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–ÅÖπêÅÖ¡¡Ω•π—µïπ–πùï–†âçΩπ—Öç—}•êà§Ë(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅÖ¡¡Ω•π—µïπ–πùï–†âçΩπ—Öç—}•êà§§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çÖ±ïπë±Â}çΩπ—Öç—}âÂ}ïµÖ•∞°ëÖ—Ñ∞ÅÖ¡¡Ω•π—µïπ–πùï–†â•πŸ•—ïï}ïµÖ•∞à§§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çÖ±ïπë±Â}çΩπ—Öç—}âÂ}¡°Ωπî°ëÖ—Ñ∞ÅÖ¡¡Ω•π—µïπ–πùï–†â•πŸ•—ïï}¡°Ωπîà§§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çÖ±ïπë±Â}ë’¡±•çÖ—ï}âΩΩ≠•πù}çΩπ—Öç–°ëÖ—Ñ∞ÅÖ¡¡Ω•π—µïπ–§(ÄÄÄÅçΩπ—Öç—}ç…ïÖ—ïêÄÙÅÖ±Õî(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–ÅÖπêÅ}ç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—}•Õ}—ΩëÖÂ}Ω…}ô’—’…î°Ö¡¡Ω•π—µïπ–§Ë(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çÖ±ïπë±Â}πï›}çΩπ—Öç–°ëÖ—Ñ∞Å¡ÖÂ±ΩÖê∞ÅÖ¡¡Ω•π—µïπ–§(ÄÄÄÄÄÄÄÅçΩπ—Öç—}ç…ïÖ—ïêÄÙÅçΩπ—Öç–Å•ÃÅπΩ–Å9Ωπî(ÄÄÄÅÖ¡¡Ω•π—µïπ—lâçΩπ—Öç—}•êâtÄÙÅçΩπ—Öç–πùï–†â•êà§Å•òÅçΩπ—Öç–Åï±ÕîÅ9Ωπî((ÄÄÄÅ•πôï……ïë}ôΩ…µÖ—•Ω∏∞Å•πôï……ïë}ëïÕ¡}—Â¡îÄÙÅ}ç…µ}çÖ±ïπë±Â}ôΩ…µÖ—•Ω∏°¡ÖÂ±ΩÖê§(ÄÄÄÅ•òÅçΩπ—Öç–ÅÖπêÅ•πôï……ïë}ôΩ…µÖ—•Ω∏ÅÖπêÅπΩ–ÅÕ—»°çΩπ—Öç–πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§Ë(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâôΩ…µÖ—•Ω∏âtÄÙÅ•πôï……ïë}ôΩ…µÖ—•Ω∏(ÄÄÄÄÄÄÄÅ•òÅ•πôï……ïë}ëïÕ¡}—Â¡îË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâëïÕ¡}—Â¡îâtÄÙÅ•πôï……ïë}ëïÕ¡}—Â¡î((ÄÄÄÅ•òÅπΩ–Åï·•Õ—•πúË(ÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—ÃπÖ¡¡ïπê°Ö¡¡Ω•π—µïπ–§(ÄÄÄÅ•òÅçΩπ—Öç—}ç…ïÖ—ïêË(ÄÄÄÄÄÄÄÅ}ç…µ}çÖ±ïπë±Â}…ï±•π≠}Ö¡¡Ω•π—µïπ—Ã°ëÖ—Ñ∞ÅçΩπ—Öç–§((ÄÄÄÅç°ÖπùïêÄÙÄ†(ÄÄÄÄÄÄÄÅπΩ–Åï·•Õ—•πú(ÄÄÄÄÄÄÄÅΩ»Å¡…ïŸ•Ω’Õ}Õ—Ö—’ÃÄÑÙÅÖ¡¡Ω•π—µïπ–πùï–†âÕ—Ö—’Ãà§(ÄÄÄÄÄÄÄÅΩ»Å¡…ïŸ•Ω’Õ}Õ—Ö…–ÄÑÙÅÖ¡¡Ω•π—µïπ–πùï–†âÕ—Ö…—}—•µîà§(ÄÄÄÄ§(ÄÄÄÅÖ¡¡Ω•π—µïπ—}âïçÖµï}Öç—•ŸîÄÙÅâΩΩ∞†(ÄÄÄÄÄÄÄÅçΩπ—Öç–(ÄÄÄÄÄÄÄÅÖπêÄ°çΩπ—Öç–πùï–†âÕ—Ö—’–à§ÅΩ»Äâ9Ω’ŸïÖ’‡à§(ÄÄÄÄÄÄÄÅπΩ–Å•∏ÅÏâΩπŸï…—§à∞Äâ•Õ≈’Ö±•ôß§âÙ(ÄÄÄÄÄÄÄÅÖπêÅÕ—»°Ö¡¡Ω•π—µïπ–πùï–†âÕ—Ö—’Ãà§ÅΩ»ÄâÖç—•Ÿîà§π±Ω›ï»†§(ÄÄÄÄÄÄÄÅπΩ–Å•∏ÅÏâçÖπçï±ïêà∞ÄâçÖπçï±±ïêâÙ(ÄÄÄÄÄÄÄÅÖπêÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅπΩ–Åï·•Õ—•πú(ÄÄÄÄÄÄÄÄÄÄÄÅΩ»ÅÕ—»°¡…ïŸ•Ω’Õ}Õ—Ö—’ÃÅΩ»Äàà§π±Ω›ï»†§Å•∏ÅÏâçÖπçï±ïêà∞ÄâçÖπçï±±ïêâÙ(ÄÄÄÄÄÄÄÄÄÄÄÅΩ»Å¡…ïŸ•Ω’Õ}Õ—Ö…–ÄÑÙÅÖ¡¡Ω•π—µïπ–πùï–†âÕ—Ö…—}—•µîà§(ÄÄÄÄÄÄÄÄÄÄÄÅΩ»Å}ç…µ}çÖ±ïπë±Â}âΩΩ≠•πù}Õ’¡ï…ÕïëïÕ}Öç—•Ÿï}…ï±Öπçî†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ–∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅÖπêÅ}ç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—}•Õ}—ΩëÖÂ}Ω…}ô’—’…î°Ö¡¡Ω•π—µïπ–§(ÄÄÄÄ§(ÄÄÄÅ…ï±Öπçï}ç°ÖπùïêÄÙÅÖ±Õî(ÄÄÄÅ•òÅÖ¡¡Ω•π—µïπ—}âïçÖµï}Öç—•ŸîË(ÄÄÄÄÄÄÄÅ|∞Å…ï±Öπçï}ç°ÖπùïêÄÙÅ}ç…µ}Õç°ïë’±ï}…ï±Öπçî†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÄàà∞(ÄÄÄÄÄÄÄÄÄÄÄÅÕΩ’…çîÙâçÖ±ïπë±Â}Ö¡¡Ω•π—µïπ–à∞(ÄÄÄÄÄÄÄÄÄÄÄÅÖç—Ω…}πÖµîÙâÖ±ïπë±‰à∞(ÄÄÄÄÄÄÄÄ§((ÄÄÄÅçÖπçï±±Ö—•Ωπ}…ï±Öπçï}ç°ÖπùïêÄÙÅÖ±Õî(ÄÄÄÅçÖπçï±±Ö—•Ωπ}°ÖÕ}Öç—•Ÿï}…ï¡±Öçïµïπ–ÄÙÅÖ±Õî(ÄÄÄÅçÖπçï±±Ö—•Ωπ}Ö’—ΩµÖ—•Ωπ}¡ïπë•πúÄÙÅâΩΩ∞†(ÄÄÄÄÄÄÄÅçΩπ—Öç–(ÄÄÄÄÄÄÄÅÖπêÅ›ïâ°ΩΩ≠}ïŸïπ–ÄÙÙÄâ•πŸ•—ïîπçÖπçï±ïêà(ÄÄÄÄÄÄÄÅÖπêÅπΩ–ÅÖ¡¡Ω•π—µïπ–πùï–†âçÖπçï±±Ö—•Ωπ}ôΩ±±Ω›’¡}¡…ΩçïÕÕïë}Ö–à§(ÄÄÄÄ§(ÄÄÄÅ•òÅçÖπçï±±Ö—•Ωπ}Ö’—ΩµÖ—•Ωπ}¡ïπë•πúË(ÄÄÄÄÄÄÄÅçÖπçï±±Ö—•Ωπ}°ÖÕ}Öç—•Ÿï}…ï¡±Öçïµïπ–ÄÙÅÖπ‰†(ÄÄÄÄÄÄÄÄÄÄÄÅ•—ï¥Å•ÃÅπΩ–ÅÖ¡¡Ω•π—µïπ–(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅ•—ï¥πùï–†âçΩπ—Öç—}•êà§ÄÙÙÅçΩπ—Öç–πùï–†â•êà§(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅ}ç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—}•Õ}—ΩëÖÂ}Ω…}ô’—’…î°•—ï¥§(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»Å•—ï¥Å•∏ÅÖ¡¡Ω•π—µïπ—Ã(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ•òÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅπΩ–ÅçÖπçï±±Ö—•Ωπ}°ÖÕ}Öç—•Ÿï}…ï¡±Öçïµïπ–(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÄ°çΩπ—Öç–πùï–†âÕ—Ö—’–à§ÅΩ»Äâ9Ω’ŸïÖ’‡à§(ÄÄÄÄÄÄÄÄÄÄÄÅπΩ–Å•∏ÅÏâΩπŸï…—§à∞Äâ•Õ≈’Ö±•ôß§âÙ(ÄÄÄÄÄÄÄÄ§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçÖπçï±±Ö—•Ωπ}ëÖ—îÄÙÅ}ç…µ}çÖ±ïπë±Â}çÖπçï±±Ö—•Ωπ}ëÖ—î°¡ÖÂ±ΩÖê§(ÄÄÄÄÄÄÄÄÄÄÄÅ|∞ÅçÖπçï±±Ö—•Ωπ}…ï±Öπçï}ç°ÖπùïêÄÙÅ}ç…µ}Õç°ïë’±ï}…ï±Öπçî†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçÖπçï±±Ö—•Ωπ}ëÖ—î∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕΩ’…çîÙâçÖ±ïπë±Â}çÖπçï±±Ö—•Ω∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖç—Ω…}πÖµîÙâÖ±ïπë±‰à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµΩ—•òÙâM’•—îÅÖππ’±Ö—•Ω∏Åë‘Å…ïπëïËµŸΩ’Ãà∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—lâçÖπçï±±Ö—•Ωπ}ôΩ±±Ω›’¡}ëÖ—îâtÄÙÅçÖπçï±±Ö—•Ωπ}ëÖ—î(ÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—lâçÖπçï±±Ö—•Ωπ}ôΩ±±Ω›’¡}¡…ΩçïÕÕïë}Ö–âtÄÙÅπΩ‹(ÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—lâ’¡ëÖ—ïë}Ö–âtÄÙÅπΩ‹(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î((ÄÄÄÅΩ±ë}Õ—Ö—’ÃÄÙÄ°çΩπ—Öç–πùï–†âÕ—Ö—’–à§ÅΩ»Äâ9Ω’ŸïÖ’‡à§Å•òÅçΩπ—Öç–Åï±ÕîÄàà(ÄÄÄÅÕ—Ö—’Õ}ç°ÖπùïêÄÙÅâΩΩ∞†(ÄÄÄÄÄÄÄÅçΩπ—Öç–(ÄÄÄÄÄÄÄÅÖπêÅ}ç…µ}ÕÂπç}çΩπ—Öç—}çÖ±ïπë±Â}Õ—Ö—’Ã†(ÄÄÄÄÄÄÄÄÄÄÄÅëÖ—Ñ∞(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÅ¡…ïôï…}Ö¡¡Ω•π—µïπ–Ù†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—}âïçÖµï}Öç—•Ÿî(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ»ÅçÖπçï±±Ö—•Ωπ}°ÖÕ}Öç—•Ÿï}…ï¡±Öçïµïπ–(ÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄ§(ÄÄÄÅ¡•¡ï±•πï}ç°ÖπùïêÄÙÄ†(ÄÄÄÄÄÄÄÅ…ï±Öπçï}ç°Öπùïê(ÄÄÄÄÄÄÄÅΩ»ÅçÖπçï±±Ö—•Ωπ}…ï±Öπçï}ç°Öπùïê(ÄÄÄÄÄÄÄÅΩ»ÅÕ—Ö—’Õ}ç°Öπùïê(ÄÄÄÄ§(ÄÄÄÅ•òÄ†(ÄÄÄÄÄÄÄÅçΩπ—Öç–(ÄÄÄÄÄÄÄÅÖπêÅ¡•¡ï±•πï}ç°Öπùïê(ÄÄÄÄÄÄÄÅÖπêÅ…ïçΩ…ë}Öç—•Ÿ•—‰(ÄÄÄÄÄÄÄÅÖπêÅΩ±ë}Õ—Ö—’ÃÄÑÙÅçΩπ—Öç–πùï–†âÕ—Ö—’–à§(ÄÄÄÄ§Ë(ÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö—’–à∞(ÄÄÄÄÄÄÄÄÄÄÄÅòâM—Ö—’–ÄËÅÌçΩπ—Öç—lùÕ—Ö—’–ùuÙà∞(ÄÄÄÄÄÄÄÄÄÄÄÅòâπç•ï∏ÅÕ—Ö—’–ÄËÅÌΩ±ë}Õ—Ö—’ÕÙà∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅ•òÅçΩπ—Öç–ÅÖπêÅ…ïçΩ…ë}Öç—•Ÿ•—‰ÅÖπêÅç°ÖπùïêË(ÄÄÄÄÄÄÄÅ•òÅÖ¡¡Ω•π—µïπ–πùï–†âÕ—Ö—’Ãà§ÄÙÙÄâçÖπçï±ïêàË(ÄÄÄÄÄÄÄÄÄÄÄÅ—•—±îÄÙÄâIïπëïËµŸΩ’ÃÅÖ±ïπë±‰ÅÖππ’≥§à(ÄÄÄÄÄÄÄÅï±•òÅÖ¡¡Ω•π—µïπ–πùï–†â…ïÕç°ïë’±ïêà§ÅΩ»ÅÖ¡¡Ω•π—µïπ–πùï–†âΩ±ë}•πŸ•—ïîà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ—•—±îÄÙÄâIïπëïËµŸΩ’ÃÅÖ±ïπë±‰Å…ï¡…Ωù…Öµ∑§à(ÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÅ—•—±îÄÙÄâIïπëïËµŸΩ’ÃÅÖ±ïπë±‰Å¡±Öπ•ôß§à(ÄÄÄÄÄÄÄÅëï—Ö•∞ÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅòâÌÖ¡¡Ω•π—µïπ–πùï–†ùπÖµîú§ÅΩ»ÄùIïπëïËµŸΩ’ÃùÙÉäPÄà(ÄÄÄÄÄÄÄÄÄÄÄÅòâÌ}ç…µ}çÖ±ïπë±Â}ëÖ—ï—•µï}±Öâï∞°Ö¡¡Ω•π—µïπ–πùï–†ùÕ—Ö…—}—•µîú§•Ùà(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞ÄâçÖ±ïπë±‰à∞Å—•—±î∞Åëï—Ö•∞§(ÄÄÄÅ•òÅçΩπ—Öç–ÅÖπêÄ°¡•¡ï±•πï}ç°ÖπùïêÅΩ»Ä°…ïçΩ…ë}Öç—•Ÿ•—‰ÅÖπêÅç°Öπùïê§§Ë(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅπΩ‹(ÄÄÄÅ…ï—’…∏ÅÖ¡¡Ω•π—µïπ–∞ÅçΩπ—Öç–(()ëïòÅ}ç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—}•Õ}—ΩëÖÂ}Ω…}ô’—’…î°Ö¡¡Ω•π—µïπ–∞ÅπΩ‹ı9Ωπî§Ë(ÄÄÄÄààâIï—’…∏Å›°ï—°ï»ÅÑÅπΩ∏µçÖπçï±±ïêÅÖ¡¡Ω•π—µïπ–Å•ÃÅΩ∏ΩÖô—ï»Å—ΩëÖ‰Å•∏ÅAÖ…•Ã∏ààà(ÄÄÄÅ•òÅÕ—»°Ö¡¡Ω•π—µïπ–πùï–†âÕ—Ö—’Ãà§ÅΩ»ÄâÖç—•Ÿîà§π±Ω›ï»†§Å•∏ÅÏâçÖπçï±ïêà∞ÄâçÖπçï±±ïêâÙË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅ•òÅÕ—»°Ö¡¡Ω•π—µïπ–πùï–†â…ïÕ¡ΩπÕï}Õ—Ö—’Ãà§ÅΩ»Äàà§π±Ω›ï»†§ÄÙÙÄâπΩ}ÖπÕ›ï»àË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅÕ—Ö…—}—•µîÄÙÅÕ—»°Ö¡¡Ω•π—µïπ–πùï–†âÕ—Ö…—}—•µîà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–ÅÕ—Ö…—}—•µîË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—}Ö–ÄÙÅëÖ—ï—•µîπëÖ—ï—•µîπô…Ωµ•ÕΩôΩ…µÖ–†(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö…—}—•µîπ…ï¡±Öçî†âhà∞Äà¨¿¿Ë¿¿à§(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî((ÄÄÄÅ¡Ö…•ÃÄÙÅ¡Â—Ëπ—•µïÈΩπî†â’…Ω¡îΩAÖ…•Ãà§(ÄÄÄÅ•òÅÖ¡¡Ω•π—µïπ—}Ö–π—È•πôºÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—}Ö–ÄÙÅ¡Ö…•Ãπ±ΩçÖ±•Èî°Ö¡¡Ω•π—µïπ—}Ö–§(ÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—}Ö–ÄÙÅÖ¡¡Ω•π—µïπ—}Ö–πÖÕ—•µïÈΩπî°¡Ö…•Ã§((ÄÄÄÅ…ïôï…ïπçîÄÙÅπΩ‹ÅΩ»ÅëÖ—ï—•µîπëÖ—ï—•µîππΩ‹°¡Ö…•Ã§(ÄÄÄÅ•òÅ…ïôï…ïπçîπ—È•πôºÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅ…ïôï…ïπçîÄÙÅ¡Ö…•Ãπ±ΩçÖ±•Èî°…ïôï…ïπçî§(ÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÅ…ïôï…ïπçîÄÙÅ…ïôï…ïπçîπÖÕ—•µïÈΩπî°¡Ö…•Ã§(ÄÄÄÅ…ï—’…∏ÅÖ¡¡Ω•π—µïπ—}Ö–πëÖ—î†§Ä¯ÙÅ…ïôï…ïπçîπëÖ—î†§(()ëïòÅ}ç…µ}ÕÂπç}çΩπ—Öç—}çÖ±ïπë±Â}Õ—Ö—’Ã†(ÄÄÄÄÄÄÄÅëÖ—Ñ∞ÅçΩπ—Öç–∞ÅπΩ‹ı9Ωπî∞Ä®∞Å¡…ïôï…}Ö¡¡Ω•π—µïπ–ıÖ±Õî∞ÅÖ¡¡Ω•π—µïπ—Ãı9Ωπî§Ë(ÄÄÄÄààâ±•ù∏Å—°îÅ¡•¡ï±•πîÅ›•—†Åç’……ïπ–Ωô’—’…îÅÖ¡¡Ω•π—µïπ—ÃÅÖπêÅΩ¡ï∏ÅôΩ±±Ω‹µ’¡Ã∏((ÄÄÄÅQ°îÅÖ¡¡Ω•π—µïπ–Å…’±îÅ•ÃÅëï±•âï…Ö—ï±‰ÅâÖÕïêÅΩ∏Å—°îÅçÖ±ïπëÖ»ÅëÖ‰Å•∏ÅAÖ…•ÃË(ÄÄÄÅÖ∏ÅÖ¡¡Ω•π—µïπ–ÅïÖ…±•ï»Å—ΩëÖ‰Å…ïµÖ•πÃÅŸ•Õ•â±î∞Åâ’–ÅΩπîÅô…Ω¥ÅÑÅ¡…ïŸ•Ω’ÃÅëÖ‰(ÄÄÄÅëΩïÃÅπΩ–∏Å•πÖ∞ÅÕ—Ö—’ÕïÃÅÖ±›ÖÂÃÅ›•∏∏ÅQ°îÅ•πùïÕ–Å¡Ö—†ÅçÖπçï±ÃÅ—°îÅôΩ±±Ω‹µ’¡Ã(ÄÄÄÅ—°Ö–Åï·•Õ—ïêÅ›°ï∏ÅÑÅâΩΩ≠•πúÅâïçΩµïÃÅÖç—•ŸîÏÅ—°•ÃÅ…ïçΩπç•±ï»ÅπïŸï»Å…ï¡ïÖ—Ã(ÄÄÄÅ—°Ö–ÅÕ•ëîÅïôôïç–ÅΩ∏Å±Ö—ï»Å…ïÖëÃÅΩ»ÅÕÂπç°…Ωπ•ÈÖ—•ΩπÃ∏Å1Ö—ï»ÅôΩ±±Ω‹µ’¡ÃÅ≠ïï¿(ÄÄÄÅ—°ï•»Å°•Õ—Ω…•çÖ∞Å¡…•Ω…•—‰Å’π±ïÕÃÅ—°îÅÖ¡¡Ω•π—µïπ–Å°ÖÃÅ©’Õ–ÅâïçΩµîÅÖç—•Ÿî∏(ÄÄÄÅ∏ÅÖ¡¡Ω•π—µïπ–ÅµÖ…≠ïêÅÅÅπΩ}ÖπÕ›ï…ÅÄÅπºÅ±Ωπùï»Å›•πÃÅΩŸï»Å•—ÃÅ(¨»ÅôΩ±±Ω‹µ’¿∏ÅÅÕ—Ö±î(ÄÄÄÅÅÅIXÅ¡…Ωù…Öµ∑•ÅÄÅ›•—°Ω’–ÅÖ∏Åï±•ù•â±îÅÖ¡¡Ω•π—µïπ–ÅΩ»ÅôΩ±±Ω‹µ’¿Å•ÃÅ…ï¡Ö•…ïê(ÄÄÄÅ—ºÅÅÅ∏ÅçΩ’…ÕÅÄ∏(ÄÄÄÄààà(ÄÄÄÅçÖπë•ëÖ—ïÃÄÙÄ°ëÖ—Ñπùï–†âç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Ãà∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅÖ¡¡Ω•π—µïπ—ÃÅ•ÃÅ9ΩπîÅï±ÕîÅÖ¡¡Ω•π—µïπ—Ã§(ÄÄÄÅ°ÖÕ}Öç—•Ÿï}Ö¡¡Ω•π—µïπ–ÄÙÅÖπ‰†(ÄÄÄÄÄÄÄÅ•—ï¥πùï–†âçΩπ—Öç—}•êà§ÄÙÙÅçΩπ—Öç–πùï–†â•êà§(ÄÄÄÄÄÄÄÅÖπêÅ}ç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—}•Õ}—ΩëÖÂ}Ω…}ô’—’…î°•—ï¥∞ÅπΩ‹§(ÄÄÄÄÄÄÄÅôΩ»Å•—ï¥Å•∏ÅçÖπë•ëÖ—ïÃ(ÄÄÄÄ§(ÄÄÄÅç’……ïπ—}Õ—Ö—’ÃÄÙÅçΩπ—Öç–πùï–†âÕ—Ö—’–à§ÅΩ»Äâ9Ω’ŸïÖ’‡à(ÄÄÄÅ•òÅç’……ïπ—}Õ—Ö—’ÃÅ•∏ÅÏâ•Õ≈’Ö±•ôß§à∞ÄâΩπŸï…—§âÙË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅ•òÅ°ÖÕ}Öç—•Ÿï}Ö¡¡Ω•π—µïπ–Ë(ÄÄÄÄÄÄÄÅ•òÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅç’……ïπ—}Õ—Ö—’ÃÄÙÙÄâIXÅ¡…Ωù…Öµ∑§à(ÄÄÄÄÄÄÄÄÄÄÄÅΩ»Ä°ç’……ïπ—}Õ—Ö—’ÃÄÙÙÄâÅ…ï±Öπçï»àÅÖπêÅπΩ–Å¡…ïôï…}Ö¡¡Ω•π—µïπ–§(ÄÄÄÄÄÄÄÄ§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÄÄÄÄÅπï·—}Õ—Ö—’ÃÄÙÄâIXÅ¡…Ωù…Öµ∑§à(ÄÄÄÅï±•òÅçΩπ—Öç–πùï–†â…ï±Öπçï}ëÖ—îà§Ë(ÄÄÄÄÄÄÄÅ•òÅç’……ïπ—}Õ—Ö—’ÃÄÙÙÄâÅ…ï±Öπçï»àË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÄÄÄÄÅπï·—}Õ—Ö—’ÃÄÙÄâÅ…ï±Öπçï»à(ÄÄÄÅï±•òÅç’……ïπ—}Õ—Ö—’ÃÄÙÙÄâIXÅ¡…Ωù…Öµ∑§àË(ÄÄÄÄÄÄÄÅπï·—}Õ—Ö—’ÃÄÙÄâ∏ÅçΩ’…Ãà(ÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅçΩπ—Öç—lâÕ—Ö—’–âtÄÙÅπï·—}Õ—Ö—’Ã(ÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅ…ï—’…∏ÅQ…’î(()ëïòÅ}ç…µ}çÖ±ïπë±Â}ôï—ç°}çΩπ—Öç—}Ö¡¡Ω•π—µïπ—Ã°ëÖ—Ñ∞ÅçΩπ—Öç–§Ë(ÄÄÄÄààâï—ç†Å—°îÅïŸïπ—ÃÅÖ±ïπë±‰ÅÖÕÕΩç•Ö—ïÃÅ›•—†Å—°•ÃÅçΩπ—Öç–∏((ÄÄÄÅQ°îÅîµµÖ•∞Åïπ—ï…ïêÅ•∏ÅÖ±ïπë±‰Å•ÃÅπΩ–ÅπïçïÕÕÖ…•±‰Å—°îÅΩπîÅçΩ±±ïç—ïêÅâ‰Å—°î(ÄÄÄÅÕïç…ï—Ö…‰Ä°ÑÅÕ°Ö…ïêΩçΩµ¡Öπ‰ÅÖëë…ïÕÃÅ•ÃÅÕΩµï—•µïÃÅ’Õïê§∏ÄÅQ…‰ÅÖ±ïπë±‰ùÃ(ÄÄÄÅïôô•ç•ïπ–ÅîµµÖ•∞Åô•±—ï»Åô•…Õ–∞Å—°ï∏ÅôÖ±∞ÅâÖç¨Å—ºÅ—°îÅ¡°ΩπîÅπ’µâï»ÅçΩπ—Ö•πïê(ÄÄÄÅ•∏Å—°îÅ•πŸ•—ïîÅÖπÕ›ï…ÃÅ›°ï∏Å•–Åë•êÅπΩ–Åô•πêÅÖπÂ—°•πú∏(ÄÄÄÄààà(ÄÄÄÅïµÖ•∞ÄÙÅ}ç…µ}πΩ…µÖ±•Èï}ïµÖ•∞°çΩπ—Öç–πùï–†âµÖ•∞à§§(ÄÄÄÅ¡°ΩπîÄÙÅ}ç…µ}πΩ…µÖ±•Èï}¡°Ωπî°çΩπ—Öç–πùï–†â—ï±ï¡°Ωπîà§§(ÄÄÄÅ•òÅπΩ–ÅïµÖ•∞ÅÖπêÅπΩ–Å¡°ΩπîË(ÄÄÄÄÄÄÄÅ…ï—’…∏Åmt∞ÅÏâµï—°ΩêàËÄâ¡°Ωπï}çÖç°îà∞Äâ¡…ΩçïÕÕïë}ïŸïπ—ÃàËÄ¡Ù((ÄÄÄÅçΩπ—ï·–ÄÙÅ}çÖ±ïπë±Â}çΩπ—ï·—}ô…Ωµ}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ¡Ö…ÖµÃÄÙÅÏ(ÄÄÄÄÄÄÄÄâçΩ’π–àËÄƒ¿¿∞(ÄÄÄÄÄÄÄÄâÕΩ…–àËÄâÕ—Ö…—}—•µîÈëïÕåà∞(ÄÄÄÅÙ(ÄÄÄÅ•òÅïµÖ•∞Ë(ÄÄÄÄÄÄÄÅ¡Ö…ÖµÕlâ•πŸ•—ïï}ïµÖ•∞âtÄÙÅïµÖ•∞(ÄÄÄÅ•òÅçΩπ—ï·–πùï–†âÕçΩ¡îà§ÄÙÙÄâ’Õï»àË(ÄÄÄÄÄÄÄÅ¡Ö…ÖµÕlâ’Õï»âtÄÙÅçΩπ—ï·—lâ’Õï»ât(ÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÅ¡Ö…ÖµÕlâΩ…ùÖπ•ÈÖ—•Ω∏âtÄÙÅçΩπ—ï·—lâΩ…ùÖπ•ÈÖ—•Ω∏ât((ÄÄÄÅÕç°ïë’±ïë}ïŸïπ—ÃÄÙÅ}çÖ±ïπë±Â}¡Öù•πÖ—ïë}çΩ±±ïç—•Ω∏†(ÄÄÄÄÄÄÄÄàΩÕç°ïë’±ïë}ïŸïπ—Ãà∞(ÄÄÄÄÄÄÄÅ¡Ö…ÖµÃı¡Ö…ÖµÃ∞(ÄÄÄÄÄÄÄÅµÖ·}¡ÖùïÃÙƒ¿¿∞(ÄÄÄÄ§(ÄÄÄÅ¡ÖÂ±ΩÖëÃÄÙÅmt(ÄÄÄÅôΩ»ÅÕç°ïë’±ïë}ïŸïπ–Å•∏ÅÕç°ïë’±ïë}ïŸïπ—ÃË(ÄÄÄÄÄÄÄÅïŸïπ—}’’•êÄÙÅ}çÖ±ïπë±Â}…ïÕΩ’…çï}’’•ê†(ÄÄÄÄÄÄÄÄÄÄÄÅÕç°ïë’±ïë}ïŸïπ–πùï–†â’…§à§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕç°ïë’±ïë}ïŸïπ—Ãà∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅïŸïπ—}’’•êË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ•πŸ•—ïï}¡Ö…ÖµÃÄÙÅÏâçΩ’π–àËÄƒ¿¡Ù(ÄÄÄÄÄÄÄÅ•òÅïµÖ•∞Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ•πŸ•—ïï}¡Ö…ÖµÕlâïµÖ•∞âtÄÙÅïµÖ•∞(ÄÄÄÄÄÄÄÅ•πŸ•—ïïÃÄÙÅ}çÖ±ïπë±Â}¡Öù•πÖ—ïë}çΩ±±ïç—•Ω∏†(ÄÄÄÄÄÄÄÄÄÄÄÅòàΩÕç°ïë’±ïë}ïŸïπ—ÃΩÌïŸïπ—}’’•ëÙΩ•πŸ•—ïïÃà∞(ÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö…ÖµÃı•πŸ•—ïï}¡Ö…ÖµÃ∞(ÄÄÄÄÄÄÄÄÄÄÄÅµÖ·}¡ÖùïÃÙƒ¿¿∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅôΩ»Å•πŸ•—ïîÅ•∏Å•πŸ•—ïïÃË(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÄ°ïµÖ•∞ÅÖπêÅ}ç…µ}πΩ…µÖ±•Èï}ïµÖ•∞°•πŸ•—ïîπùï–†âïµÖ•∞à§§ÄÙÙÅïµÖ•∞§ÅΩ»Ä†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡°ΩπîÅÖπêÅ}ç…µ}πΩ…µÖ±•Èï}¡°Ωπî°}ç…µ}çÖ±ïπë±Â}¡ÖÂ±ΩÖë}¡°Ωπî°•πŸ•—ïî§§ÄÙÙÅ¡°Ωπî(ÄÄÄÄÄÄÄÄÄÄÄÄ§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëÃπÖ¡¡ïπê°Ï®©•πŸ•—ïî∞ÄâÕç°ïë’±ïë}ïŸïπ–àËÅÕç°ïë’±ïë}ïŸïπ—Ù§((ÄÄÄÄåÅ∏ÅîµµÖ•∞µô•±—ï…ïêÅ≈’ï…‰ÅçÖππΩ–Å…ï—’…∏ÅÑÅâΩΩ≠•πúÅµÖëîÅ›•—†ÅÑÅë•ôôï…ïπ–(ÄÄÄÄåÅÖëë…ïÕÃ∏ÄÅMçÖ∏Å—°îÅÖç—•ŸîÅïŸïπ—ÃÅΩπ±‰Å›°ï∏ÅπïïëïêÅÖπêÅ•ëïπ—•ô‰Å—°î(ÄÄÄÄåÅ•πŸ•—ïîÅâ‰Å—°îÅ—ï±ï¡°ΩπîÅÖπÕ›ï»ÅçΩ±±ïç—ïêÅâ‰ÅÖ±ïπë±‰∏(ÄÄÄÅµÖ—ç°ïë}âÂ}¡°ΩπîÄÙÅÖ±Õî(ÄÄÄÅ•òÅπΩ–Å¡ÖÂ±ΩÖëÃÅÖπêÅ¡°ΩπîÅÖπêÅïµÖ•∞Ë(ÄÄÄÄÄÄÄÅ¡°Ωπï}¡Ö…ÖµÃÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩ’π–àËÄƒ¿¿∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕΩ…–àËÄâÕ—Ö…—}—•µîÈÖÕåà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö—’ÃàËÄâÖç—•Ÿîà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâµ•π}Õ—Ö…—}—•µîàËÅëÖ—ï—•µîπëÖ—ï—•µîππΩ‹°ëÖ—ï—•µîπ—•µïÈΩπîπ’—å§π•ÕΩôΩ…µÖ–†§∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÅ•òÅçΩπ—ï·–πùï–†âÕçΩ¡îà§ÄÙÙÄâ’Õï»àË(ÄÄÄÄÄÄÄÄÄÄÄÅ¡°Ωπï}¡Ö…ÖµÕlâ’Õï»âtÄÙÅçΩπ—ï·—lâ’Õï»ât(ÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÅ¡°Ωπï}¡Ö…ÖµÕlâΩ…ùÖπ•ÈÖ—•Ω∏âtÄÙÅçΩπ—ï·—lâΩ…ùÖπ•ÈÖ—•Ω∏ât(ÄÄÄÄÄÄÄÅ¡°Ωπï}ïŸïπ—ÃÄÙÅ}çÖ±ïπë±Â}¡Öù•πÖ—ïë}çΩ±±ïç—•Ω∏†(ÄÄÄÄÄÄÄÄÄÄÄÄàΩÕç°ïë’±ïë}ïŸïπ—Ãà∞Å¡Ö…ÖµÃı¡°Ωπï}¡Ö…ÖµÃ∞ÅµÖ·}¡ÖùïÃÙƒ¿¿(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅôΩ»ÅÕç°ïë’±ïë}ïŸïπ–Å•∏Å¡°Ωπï}ïŸïπ—ÃË(ÄÄÄÄÄÄÄÄÄÄÄÅïŸïπ—}’’•êÄÙÅ}çÖ±ïπë±Â}…ïÕΩ’…çï}’’•ê°Õç°ïë’±ïë}ïŸïπ–πùï–†â’…§à§∞ÄâÕç°ïë’±ïë}ïŸïπ—Ãà§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–ÅïŸïπ—}’’•êË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÄÄÄÄÅ•πŸ•—ïïÃÄÙÅ}çÖ±ïπë±Â}¡Öù•πÖ—ïë}çΩ±±ïç—•Ω∏†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòàΩÕç°ïë’±ïë}ïŸïπ—ÃΩÌïŸïπ—}’’•ëÙΩ•πŸ•—ïïÃà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö…ÖµÃıÏâçΩ’π–àËÄƒ¿¡Ù∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµÖ·}¡ÖùïÃÙƒ¿¿∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»Å•πŸ•—ïîÅ•∏Å•πŸ•—ïïÃË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ}ç…µ}πΩ…µÖ±•Èï}¡°Ωπî°}ç…µ}çÖ±ïπë±Â}¡ÖÂ±ΩÖë}¡°Ωπî°•πŸ•—ïî§§ÄÙÙÅ¡°ΩπîË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëÃπÖ¡¡ïπê°Ï®©•πŸ•—ïî∞ÄâÕç°ïë’±ïë}ïŸïπ–àËÅÕç°ïë’±ïë}ïŸïπ—Ù§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµÖ—ç°ïë}âÂ}¡°ΩπîÄÙÅQ…’î(ÄÄÄÅ…ï—’…∏Å¡ÖÂ±ΩÖëÃ∞ÅÏ(ÄÄÄÄÄÄÄÄâµï—°ΩêàËÄâ¡°ΩπîàÅ•òÅµÖ—ç°ïë}âÂ}¡°ΩπîÅΩ»ÅπΩ–ÅïµÖ•∞Åï±ÕîÄâïµÖ•∞à∞(ÄÄÄÄÄÄÄÄâ¡…ΩçïÕÕïë}ïŸïπ—ÃàËÅ±ï∏°Õç°ïë’±ïë}ïŸïπ—Ã§∞(ÄÄÄÅÙ(()ëïòÅ}çÖ±ïπë±Â}¡°Ωπï}π’µâï»°ŸÖ±’î§Ë(ÄÄÄÅ…Ö‹ÄÙÅÕ—»°ŸÖ±’îÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–Å…Ö‹Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Äàà(ÄÄÄÅë•ù•—ÃÄÙÅ…îπÕ’à°»âqà∞Äàà∞Å…Ö‹§(ÄÄÄÅ•òÅë•ù•—ÃπÕ—Ö…—Õ›•—††à¿¿à§Ë(ÄÄÄÄÄÄÄÅë•ù•—ÃÄÙÅë•ù•—Õl»Èt(ÄÄÄÅ•òÅ±ï∏°ë•ù•—Ã§ÄÙÙÄƒ¿ÅÖπêÅë•ù•—ÃπÕ—Ö…—Õ›•—††à¿à§Ë(ÄÄÄÄÄÄÄÅë•ù•—ÃÄÙÅòàÃÕÌë•ù•—ÕlƒÈuÙà(ÄÄÄÅ•òÅë•ù•—ÃπÕ—Ö…—Õ›•—††àÃÃà§ÅΩ»Å…Ö‹πÕ—Ö…—Õ›•—††à¨à§Ë(ÄÄÄÄÄÄÄÅπΩ…µÖ±•ÈïêÄÙÅòà≠Ìë•ù•—ÕÙà(ÄÄÄÄÄÄÄÅ•òÅ…îπô’±±µÖ—ç†°»âp≠lƒ¥ÂuqëÏ‹∞ƒ—Ùà∞ÅπΩ…µÖ±•Èïê§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅπΩ…µÖ±•Èïê(ÄÄÄÅ…ï—’…∏Äàà(()ëïòÅ}çÖ±ïπë±Â}âΩΩ≠•πù}±ΩçÖ—•Ω∏°ïŸïπ—}—Â¡î∞Å…ï≈’ïÕ—ïë}±ΩçÖ—•Ω∏∞ÅçΩπ—Öç–§Ë(ÄÄÄÅ±ΩçÖ—•ΩπÃÄÙÅïŸïπ—}—Â¡îπùï–†â±ΩçÖ—•ΩπÃà§ÅΩ»Åmt(ÄÄÄÅ•òÅπΩ–Å±ΩçÖ—•ΩπÃË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(ÄÄÄÅ…ï≈’ïÕ—ïë}±ΩçÖ—•Ω∏ÄÙÅ…ï≈’ïÕ—ïë}±ΩçÖ—•Ω∏Å•òÅ•Õ•πÕ—Öπçî°…ï≈’ïÕ—ïë}±ΩçÖ—•Ω∏∞Åë•ç–§Åï±ÕîÅÌÙ(ÄÄÄÅ…ï≈’ïÕ—ïë}≠•πêÄÙÅÕ—»°…ï≈’ïÕ—ïë}±ΩçÖ—•Ω∏πùï–†â≠•πêà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅÕï±ïç—ïêÄÙÅπï·–†(ÄÄÄÄÄÄÄÄ°±ΩçÖ—•Ω∏ÅôΩ»Å±ΩçÖ—•Ω∏Å•∏Å±ΩçÖ—•ΩπÃÅ•òÅ±ΩçÖ—•Ω∏πùï–†â≠•πêà§ÄÙÙÅ…ï≈’ïÕ—ïë}≠•πê§∞(ÄÄÄÄÄÄÄÅ±ΩçÖ—•ΩπÕl¡t∞(ÄÄÄÄ§(ÄÄÄÅ≠•πêÄÙÅÕï±ïç—ïêπùï–†â≠•πêà§(ÄÄÄÅ•òÅπΩ–Å≠•πêË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(ÄÄÄÅ•òÅ≠•πêÄÙÙÄâΩ’—âΩ’πë}çÖ±∞àË(ÄÄÄÄÄÄÄÅ¡°ΩπîÄÙÅ}çÖ±ïπë±Â}¡°Ωπï}π’µâï»†(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï≈’ïÕ—ïë}±ΩçÖ—•Ω∏πùï–†â±ΩçÖ—•Ω∏à§ÅΩ»ÅçΩπ—Öç–πùï–†â—ï±ï¡°Ωπîà§(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å¡°ΩπîË(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†â1îÅπ’∑•…ºÅëîÅ”•≥•¡°ΩπîÅïÕ–Å…ï≈’•ÃÅ¡Ω’»ÅçîÅ—Â¡îÅëîÅ…ïπëïËµŸΩ’Ã∏à§(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏâ≠•πêàËÅ≠•πê∞Äâ±ΩçÖ—•Ω∏àËÅ¡°ΩπïÙ(ÄÄÄÅ•òÅ≠•πêÄÙÙÄâÖÕ≠}•πŸ•—ïîàË(ÄÄÄÄÄÄÄÅ±ΩçÖ—•Ωπ}ŸÖ±’îÄÙÅÕ—»°…ï≈’ïÕ—ïë}±ΩçÖ—•Ω∏πùï–†â±ΩçÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å±ΩçÖ—•Ωπ}ŸÖ±’îË(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†âIïπÕï•ùπïËÅ±îÅ±•ï‘ÅΩ‘Å±îÅµΩÂï∏ÅëîÅçΩπ—Öç–Åë‘Å…ïπëïËµŸΩ’Ã∏à§(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏâ≠•πêàËÅ≠•πê∞Äâ±ΩçÖ—•Ω∏àËÅ±ΩçÖ—•Ωπ}ŸÖ±’ïÙ(ÄÄÄÅ•òÅ≠•πêÅ•∏ÅÏâ¡°ÂÕ•çÖ∞à∞Äâç’Õ—Ω¥âÙË(ÄÄÄÄÄÄÄÅ±ΩçÖ—•Ωπ}ŸÖ±’îÄÙÅÕ—»°Õï±ïç—ïêπùï–†â±ΩçÖ—•Ω∏à§ÅΩ»Å…ï≈’ïÕ—ïë}±ΩçÖ—•Ω∏πùï–†â±ΩçÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å±ΩçÖ—•Ωπ}ŸÖ±’îË(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†âîÅ—Â¡îÅëîÅ…ïπëïËµŸΩ’ÃÅπîÅçΩπ—•ïπ–ÅÖ’ç’∏Å±•ï‘Å’—•±•ÕÖâ±î∏à§(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏâ≠•πêàËÅ≠•πê∞Äâ±ΩçÖ—•Ω∏àËÅ±ΩçÖ—•Ωπ}ŸÖ±’ïÙ(ÄÄÄÅ…ï—’…∏ÅÏâ≠•πêàËÅ≠•πëÙ(()ëïòÅ}çÖ±ïπë±Â}≈’ïÕ—•Ωπ}ÖπÕ›ï…Ã°ïŸïπ—}—Â¡î∞ÅÕ’âµ•——ïë}ÖπÕ›ï…Ã§Ë(ÄÄÄÅÕ’âµ•——ïë}ÖπÕ›ï…ÃÄÙÅÕ’âµ•——ïë}ÖπÕ›ï…ÃÅ•òÅ•Õ•πÕ—Öπçî°Õ’âµ•——ïë}ÖπÕ›ï…Ã∞Åë•ç–§Åï±ÕîÅÌÙ(ÄÄÄÅÖπÕ›ï…ÃÄÙÅmt(ÄÄÄÅ—ï·—}…ïµ•πëï…}π’µâï»ÄÙÄàà(ÄÄÄÅôΩ»Å≈’ïÕ—•Ω∏Å•∏ÅïŸïπ—}—Â¡îπùï–†âç’Õ—Ωµ}≈’ïÕ—•ΩπÃà§ÅΩ»ÅmtË(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å≈’ïÕ—•Ω∏πùï–†âïπÖâ±ïêà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ¡ΩÕ•—•Ω∏ÄÙÅ≈’ïÕ—•Ω∏πùï–†â¡ΩÕ•—•Ω∏à§(ÄÄÄÄÄÄÄÅŸÖ±’îÄÙÅÕ’âµ•——ïë}ÖπÕ›ï…Ãπùï–°Õ—»°¡ΩÕ•—•Ω∏§∞ÅÕ’âµ•——ïë}ÖπÕ›ï…Ãπùï–°¡ΩÕ•—•Ω∏∞Äàà§§(ÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°ŸÖ±’î∞Å±•Õ–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅŸÖ±’îÄÙÄâq∏àπ©Ω•∏°Õ—»°•—ï¥§πÕ—…•¿†§ÅôΩ»Å•—ï¥Å•∏ÅŸÖ±’îÅ•òÅÕ—»°•—ï¥§πÕ—…•¿†§§(ÄÄÄÄÄÄÄÅŸÖ±’îÄÙÅÕ—»°ŸÖ±’îÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅ≈’ïÕ—•Ω∏πùï–†â…ï≈’•…ïêà§ÅÖπêÅπΩ–ÅŸÖ±’îË(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»°òâK•¡ΩπëïËÉÄÅ±ÑÅ≈’ïÕ—•Ω∏ÅΩâ±•ùÖ—Ω•…îÄËÅÌ≈’ïÕ—•Ω∏πùï–†ùπÖµîú•Ù∏à§(ÄÄÄÄÄÄÄÅ•òÅŸÖ±’îÅÖπêÅ≈’ïÕ—•Ω∏πùï–†â—Â¡îà§ÄÙÙÄâ¡°Ωπï}π’µâï»àË(ÄÄÄÄÄÄÄÄÄÄÄÅπΩ…µÖ±•Èïë}¡°ΩπîÄÙÅ}çÖ±ïπë±Â}¡°Ωπï}π’µâï»°ŸÖ±’î§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–ÅπΩ…µÖ±•Èïë}¡°ΩπîË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâ1îÅπ’∑•…ºÅÕÖ•Õ§Å¡Ω’»Å±ÑÅ≈’ïÕ—•Ω∏É
¨ÅÌ≈’ïÕ—•Ω∏πùï–†ùπÖµîú•ÙÉ
ÏÅïÕ–Å•πŸÖ±•ëî∏à(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅŸÖ±’îÄÙÅπΩ…µÖ±•Èïë}¡°Ωπî(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–Å—ï·—}…ïµ•πëï…}π’µâï»Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—ï·—}…ïµ•πëï…}π’µâï»ÄÙÅπΩ…µÖ±•Èïë}¡°Ωπî(ÄÄÄÄÄÄÄÅ•òÅŸÖ±’îË(ÄÄÄÄÄÄÄÄÄÄÄÅÖπÕ›ï…ÃπÖ¡¡ïπê°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ≈’ïÕ—•Ω∏àËÅ≈’ïÕ—•Ω∏πùï–†âπÖµîà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÖπÕ›ï»àËÅŸÖ±’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ¡ΩÕ•—•Ω∏àËÅ¡ΩÕ•—•Ω∏∞(ÄÄÄÄÄÄÄÄÄÄÄÅÙ§(ÄÄÄÅ…ï—’…∏ÅÖπÕ›ï…Ã∞Å—ï·—}…ïµ•πëï…}π’µâï»(()ëïòÅ}çÖ±ïπë±Â}Õ•ùπÖ—’…ï}•Õ}ŸÖ±•ê°…Ö›}âΩë‰∞ÅÕ•ùπÖ—’…ï}°ïÖëï»§Ë(ÄÄÄÅÕ•ùπ•πù}≠ï‰ÄÙÅ}çÖ±ïπë±Â}Õ•ùπ•πù}≠ï‰†§(ÄÄÄÅ•òÅπΩ–ÅÕ•ùπ•πù}≠ï‰ÅΩ»ÅπΩ–ÅÕ•ùπÖ—’…ï}°ïÖëï»Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅ¡Ö…—ÃÄÙÅÌÙ(ÄÄÄÅôΩ»Å•—ï¥Å•∏ÅÕ•ùπÖ—’…ï}°ïÖëï»πÕ¡±•–†à∞à§Ë(ÄÄÄÄÄÄÄÅ≠ï‰∞ÅÕï¡Ö…Ö—Ω»∞ÅŸÖ±’îÄÙÅ•—ï¥πÕ—…•¿†§π¡Ö…—•—•Ω∏†àÙà§(ÄÄÄÄÄÄÄÅ•òÅÕï¡Ö…Ö—Ω»Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö…—Õm≠ïÂtÄÙÅŸÖ±’î(ÄÄÄÅ—•µïÕ—Öµ¿ÄÙÅ¡Ö…—Ãπùï–†â–à§(ÄÄÄÅÕ•ùπÖ—’…îÄÙÅ¡Ö…—Ãπùï–†âÿƒà§(ÄÄÄÅ•òÅπΩ–Å—•µïÕ—Öµ¿ÅΩ»ÅπΩ–ÅÕ•ùπÖ—’…îË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ•òÅÖâÃ°—•µîπ—•µî†§Ä¥Å•π–°—•µïÕ—Öµ¿§§Ä¯ÄÃ¿¿Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅÕ•ùπïë}¡ÖÂ±ΩÖêÄÙÅ—•µïÕ—Öµ¿πïπçΩëî†â’—ò¥‡à§Ä¨Åàà∏àÄ¨Å…Ö›}âΩë‰(ÄÄÄÅï·¡ïç—ïêÄÙÅ°µÖåππï‹†(ÄÄÄÄÄÄÄÅÕ•ùπ•πù}≠ï‰πïπçΩëî†â’—ò¥‡à§∞(ÄÄÄÄÄÄÄÅÕ•ùπïë}¡ÖÂ±ΩÖê∞(ÄÄÄÄÄÄÄÅ°ÖÕ°±•àπÕ°Ñ»‘ÿ∞(ÄÄÄÄ§π°ï·ë•ùïÕ–†§(ÄÄÄÅ…ï—’…∏Å°µÖåπçΩµ¡Ö…ï}ë•ùïÕ–°ï·¡ïç—ïê∞ÅÕ•ùπÖ—’…î§(()ëïòÅ}ç…µ}πΩ‹†§Ë(ÄÄÄÅ…ï—’…∏ÅëÖ—ï—•µîπëÖ—ï—•µîππΩ‹°¡Â—Ëπ—•µïÈΩπî†â’…Ω¡îΩAÖ…•Ãà§§π•ÕΩôΩ…µÖ–°—•µïÕ¡ïåÙâÕïçΩπëÃà§(()ëïòÅ}ç…µ}ôΩ…µÖ—}ô•…Õ—}πÖµî°ŸÖ±’î§Ë(ÄÄÄÄààâ9Ω…µÖ±•ÕîÅ’∏Å¡À•πΩ¥Å—Ω’–Åï∏ÅçΩπÕï…ŸÖπ–Å±ïÃÅœ•¡Ö…Ö—ï’…ÃÅçΩµ¡Ωœ•Ã∏ààà(ÄÄÄÅ—ï·–ÄÙÅÕ—»°ŸÖ±’îÅΩ»Äàà§πÕ—…•¿†§π±Ω›ï»†§(ÄÄÄÅ…ï—’…∏Å…îπÕ’à°»à°yÒmqÃúµt§°mÑµÎÄ∑€‡∑˝t§à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ±ÖµâëÑÅµÖ—ç†ËÅµÖ—ç†πù…Ω’¿†ƒ§Ä¨ÅµÖ—ç†πù…Ω’¿†»§π’¡¡ï»†§∞Å—ï·–§(()ëïòÅ}ç…µ}ôΩ…µÖ—}±ÖÕ—}πÖµî°ŸÖ±’î§Ë(ÄÄÄÄààâôô•ç°îÅÕÂÕ”•µÖ—•≈’ïµïπ–Å±ïÃÅπΩµÃÅëîÅôÖµ•±±îÅï∏ÅçÖ¡•—Ö±ïÃ∏ààà(ÄÄÄÅ…ï—’…∏ÅÕ—»°ŸÖ±’îÅΩ»Äàà§πÕ—…•¿†§π’¡¡ï»†§(()ëïòÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§Ë(ÄÄÄÅ…ï—’…∏Åπï·–†°åÅôΩ»ÅåÅ•∏ÅëÖ—Ölâç…µ}çΩπ—Öç—ÃâtÅ•òÅåπùï–†â•êà§ÄÙÙÅçΩπ—Öç—}•ê§∞Å9Ωπî§(()ëïòÅ}ç…µ}âÖç≠ô•±±}•πôΩ…µÖ—•Ωπ}…ï≈’ïÕ—}ÖπÕ›ï…Ã°çΩπ—Öç–§Ë(ÄÄÄÄààâIïÕ—Ω…îÅ…ïù’±Ö—Ω…‰ÅÖπÕ›ï…ÃÅΩµ•——ïêÅô…Ω¥ÅΩ±ëï»Å•πôΩ…µÖ—•Ω∏µôΩ…¥Å±ïÖëÃ∏ààà(ÄÄÄÅôΩ…¥ÄÙÅçΩπ—Öç–πùï–†âôΩ…µ’±Ö•…îà§(ÄÄÄÅ•òÅçΩπ—Öç–πùï–†âÕΩ’…çîà§ÄÑÙÄâëïµÖπëï}•πôΩÕ}ôΩ…µÖ—•ΩπÃàÅΩ»ÅπΩ–Å•Õ•πÕ—Öπçî°ôΩ…¥∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅç°ÖπùïêÄÙÅÖ±Õî(ÄÄÄÅôΩ»Å≠ï‰Å•∏Ä†âùÖ…ëï}Ÿ’îà∞Äâ—•—…ï}Õï©Ω’»à§Ë(ÄÄÄÄÄÄÄÅŸÖ±’îÄÙÅÕ—»°ôΩ…¥πùï–°≠ï‰§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅŸÖ±’îÅÖπêÅπΩ–ÅÕ—»°çΩπ—Öç–πùï–°≠ï‰§ÅΩ»Äàà§πÕ—…•¿†§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—m≠ïÂtÄÙÅŸÖ±’î(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅ…ï—’…∏Åç°Öπùïê(()I5}==1}M}=I%%8ÄÙÄâΩΩù±îÅëÃà)I5}==1}M}%9Q%%I}-eLÄÙÄ†âùç±•êà∞Äâ›â…Ö•êà∞Äâùâ…Ö•êà§)I5}==1}M}QI-%9}-eLÄÙÄ†(ÄÄÄÄ©I5}==1}M}%9Q%%I}-eL∞(ÄÄÄÄâùÖë}ÕΩ’…çîà∞(ÄÄÄÄâùÖë}çÖµ¡Ö•ùπ•êà∞(ÄÄÄÄâ’—µ}ÕΩ’…çîà∞(ÄÄÄÄâ’—µ}µïë•’¥à∞(ÄÄÄÄâ’—µ}çÖµ¡Ö•ù∏à∞(§(()ëïòÅ}ç…µ}ùΩΩù±ï}ÖëÕ}—…Öç≠•πù}ô•ï±ëÃ°ô•ï±ëÃ§Ë(ÄÄÄÄààâ9Ω…µÖ±•ÈîÅ—°îÅΩΩù±îÅëÃÅ¡Ö…Öµï—ï…ÃÅÖççï¡—ïêÅô…Ω¥Å¡’â±•åÅôΩ…µÃ∏ààà(ÄÄÄÅÕΩ’…çîÄÙÅô•ï±ëÃÅΩ»ÅÌÙ(ÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÅ≠ï‰ËÅÕ—»°ÕΩ’…çîπùï–°≠ï‰§ÅΩ»Äàà§πÕ—…•¿†•lË‘ƒ…t(ÄÄÄÄÄÄÄÅôΩ»Å≠ï‰Å•∏ÅI5}==1}M}QI-%9}-eL(ÄÄÄÅÙ(()ëïòÅ}ç…µ}•πôΩ…µÖ—•Ωπ}…ï≈’ïÕ—}ùΩΩù±ï}ÖëÕ}•ëïπ—•ô•ï»°ô•ï±ëÃ§Ë(ÄÄÄÄààâIï—’…∏Å—°îÅô•…Õ–ÅΩΩù±îÅç±•ç¨Å•ëïπ—•ô•ï»ÅÖπêÅ•—ÃÅ—Â¡î∏ààà(ÄÄÄÅπΩ…µÖ±•ÈïêÄÙÅ}ç…µ}ùΩΩù±ï}ÖëÕ}—…Öç≠•πù}ô•ï±ëÃ°ô•ï±ëÃ§(ÄÄÄÅôΩ»Å≠ï‰Å•∏ÅI5}==1}M}%9Q%%I}-eLË(ÄÄÄÄÄÄÄÅ•òÅπΩ…µÖ±•Èïëm≠ïÂtË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å≠ï‰π’¡¡ï»†§∞ÅπΩ…µÖ±•Èïëm≠ïÂt(ÄÄÄÅ…ï—’…∏Äàà∞Äàà(()ëïòÅ}ç…µ}•πôΩ…µÖ—•Ωπ}…ï≈’ïÕ—}ùç±•ê°ô•ï±ëÃ§Ë(ÄÄÄÄààâIï—’…∏Å—°îÅΩΩù±îÅç±•ç¨Å•ëïπ—•ô•ï»ÅçÖ¡—’…ïêÅâ‰Å—°îÅ¡’â±•åÅôΩ…¥∏ààà(ÄÄÄÅ…ï—’…∏Å}ç…µ}ùΩΩù±ï}ÖëÕ}—…Öç≠•πù}ô•ï±ëÃ°ô•ï±ëÃ•lâùç±•êât(()ëïòÅ}ç…µ}•πôΩ…µÖ—•Ωπ}…ï≈’ïÕ—}•Õ}ùΩΩù±ï}ÖëÃ°ô•ï±ëÃ§Ë(ÄÄÄÄààâï—ïç–Å¡Ö•êÅΩΩù±îÅ—…Öôô•åÅïŸï∏Å›°ï∏Å•=LÅ’ÕïÃÅ]	I%Ω	I%∏ààà(ÄÄÄÅπΩ…µÖ±•ÈïêÄÙÅ}ç…µ}ùΩΩù±ï}ÖëÕ}—…Öç≠•πù}ô•ï±ëÃ°ô•ï±ëÃ§(ÄÄÄÅ•òÅÖπ‰°πΩ…µÖ±•Èïëm≠ïÂtÅôΩ»Å≠ï‰Å•∏ÅI5}==1}M}%9Q%%I}-eL§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅQ…’î(ÄÄÄÅ•òÅπΩ…µÖ±•ÈïëlâùÖë}ÕΩ’…çîâtÄÙÙÄàƒàÅΩ»ÅπΩ…µÖ±•ÈïëlâùÖë}çÖµ¡Ö•ùπ•êâtË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅQ…’î(ÄÄÄÅÕΩ’…çîÄÙÅπΩ…µÖ±•Èïëlâ’—µ}ÕΩ’…çîâtπçÖÕïôΩ±ê†§π…ï¡±Öçî†àÄà∞Äâ|à§(ÄÄÄÅµïë•’¥ÄÙÅπΩ…µÖ±•Èïëlâ’—µ}µïë•’¥âtπçÖÕïôΩ±ê†§π…ï¡±Öçî†àÄà∞Äâ|à§(ÄÄÄÅ…ï—’…∏ÅÕΩ’…çîÅ•∏ÅÏâùΩΩù±îà∞ÄâùΩΩù±ï}ÖëÃà∞ÄâùΩΩù±ïÖëÃà∞ÄâÖë›Ω…ëÃâÙÅÖπêÅµïë•’¥Å•∏ÅÏ(ÄÄÄÄÄÄÄÄâç¡åà∞Äâ¡¡åà∞Äâ¡Ö•êà∞Äâ¡Ö•ë}ÕïÖ…ç†à∞Äâ¡Ö•ëÕïÖ…ç†à∞(ÄÄÄÅÙ(()I5}=I%%9}M=UI}1	1LÄÙÅÏ(ÄÄÄÄâÖÕÕ•Õ—Öπ–µÕïç…ï—Ö…•Ö–àËÄâMïçÀ•—Ö…•Ö–à∞(ÄÄÄÄâÖÕÕ•Õ—Öπ—}Õïç…ï—Ö…•Ö–àËÄâMïçÀ•—Ö…•Ö–à∞(ÄÄÄÄâÕ•µ’±Ö—ï’…}ŸÖï}ëïÕ¿àËÄâM•µ’±Ö—ï’»ÅYà∞(ÄÄÄÄâÕ•µ’±Ö—ï’»µï±•ù•â•±•—îµŸÖîµëïÕ¿àËÄâM•µ’±Ö—ï’»ÅYà∞(ÄÄÄÄâ›ïëΩô}ç¡òàËÄâ5Ω∏ÅΩµ¡—îÅΩ…µÖ—•Ω∏à∞(ÄÄÄÄâëïµÖπëï}ôΩ…µ’±Ö•…ï}ÖâÖπëΩππîàËÄâΩ…µ’±Ö•…îÅÖâÖπëΩπª§à∞)Ù(()ëïòÅ}ç…µ}Ω…•ù•π}≠ï‰°ŸÖ±’î§Ë(ÄÄÄÄààâIï—’…∏ÅÖ∏ÅÖççïπ–µ•πÕïπÕ•—•ŸîÅ≠ï‰Å’ÕïêÅ—ºÅëïë’¡±•çÖ—îÅI4ÅΩ…•ù•πÃ∏ààà(ÄÄÄÅπΩ…µÖ±•ÈïêÄÙÅ’π•çΩëïëÖ—ÑππΩ…µÖ±•Èî†â9-à∞ÅÕ—»°ŸÖ±’îÅΩ»Äàà§πÕ—…•¿†§§(ÄÄÄÅπΩ…µÖ±•ÈïêÄÙÄààπ©Ω•∏†(ÄÄÄÄÄÄÄÅç°Ö…Öç—ï»ÅôΩ»Åç°Ö…Öç—ï»Å•∏ÅπΩ…µÖ±•Èïê(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å’π•çΩëïëÖ—ÑπçΩµâ•π•πú°ç°Ö…Öç—ï»§(ÄÄÄÄ§(ÄÄÄÅ…ï—’…∏Å…îπÕ’à°»âmyÑµË¿¥Ât¨à∞ÄàÄà∞ÅπΩ…µÖ±•ÈïêπçÖÕïôΩ±ê†§§πÕ—…•¿†§(()ëïòÅ}ç…µ}çÖπΩπ•çÖ±}Ω…•ù•∏°ŸÖ±’î§Ë(ÄÄÄÄààâ5Ö¿Å¡ï…Õ•Õ—ïêÅ±Öâï±ÃÅÖπêÅ—ïç°π•çÖ∞ÅÕΩ’…çïÃÅ—ºÅ—°îÅI4ùÃÅŸ•Õ•â±îÅΩ…•ù•πÃ∏ààà(ÄÄÄÅ…Ö‹ÄÙÅÕ—»°ŸÖ±’îÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ≠ï‰ÄÙÅ}ç…µ}Ω…•ù•π}≠ï‰°…Ö‹§(ÄÄÄÅ•òÅπΩ–Å≠ï‰Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Äàà(ÄÄÄÅ•òÅ…Ö‹Å•∏ÅI5}=I%%9}M=UI}1	1LË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅI5}=I%%9}M=UI}1	1Mm…Ö›t(ÄÄÄÅ•òÅ≠ï‰Å•∏ÅÏâµï—Ñà∞ÄâôÖçïâΩΩ¨à∞Äâ•πÕ—Öù…Ö¥âÙÅΩ»Å≠ï‰πÕ—Ö…—Õ›•—††âµï—ÑÄà§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Äâ5Qà(ÄÄÄÅ•òÄâùΩΩù±îàÅ•∏Å≠ï‰ÅΩ»Å≠ï‰Å•∏ÅÏâÖë›Ω…ëÃà∞ÄâùΩΩù±ïÖëÃâÙË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÄâΩΩù±îÅëÃà(ÄÄÄÅ•òÄâ›ïëΩòàÅ•∏Å≠ï‰ÅΩ»ÄâçΩµ¡—îÅôΩ…µÖ—•Ω∏àÅ•∏Å≠ï‰ÅΩ»Å≠ï‰ÄÙÙÄâç¡òàË(ÄÄÄÄÄÄÄÅ…ï—’…∏Äâ5Ω∏ÅΩµ¡—îÅΩ…µÖ—•Ω∏à(ÄÄÄÅ•òÄâÕ•µ’±Ö—ï’»àÅ•∏Å≠ï‰ÅÖπêÄâŸÖîàÅ•∏Å≠ï‰Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÄâM•µ’±Ö—ï’»ÅYà(ÄÄÄÅ•òÄâÕïç…ï—Ö…•Ö–àÅ•∏Å≠ï‰Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÄâMïçÀ•—Ö…•Ö–à(ÄÄÄÅ•òÄââΩ’ç°îàÅ•∏Å≠ï‰ÅÖπêÄâΩ…ï•±±îàÅ•∏Å≠ï‰Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Äâ	Ω’ç°îÉÄÅΩ…ï•±±ïÃà(ÄÄÄÅ•òÅ≠ï‰Å•∏ÅÏâÕ•—îà∞ÄâÕ•—îÅ›ïàà∞ÄâÕ•—îÅ•π—ï…πï–à∞ÄâëïµÖπëîÅ•πôΩÃÅôΩ…µÖ—•ΩπÃâÙË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÄâM•—îÅ•π—ï…πï–à(ÄÄÄÅ•òÄâôΩ…µ’±Ö•…îàÅ•∏Å≠ï‰ÅÖπêÄâÖâÖπëΩ∏àÅ•∏Å≠ï‰Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÄâΩ…µ’±Ö•…îÅÖâÖπëΩπª§à(ÄÄÄÅ•òÅ≠ï‰ÄÙÙÄâÖ©Ω’–ÅµÖπ’ï∞àË(ÄÄÄÄÄÄÄÅ…ï—’…∏Äâ©Ω’–ÅµÖπ’ï∞à(ÄÄÄÅ•òÅ≠ï‰ÄÙÙÄâçÖ±ïπë±‰àË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÄâÖ±ïπë±‰à(ÄÄÄÅ•òÅ≠ï‰ÄÙÙÄâ¡Ωï§àË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÄâA=$à(ÄÄÄÅ…ï—’…∏Å…Ö‹(()ëïòÅ}ç…µ}…ïçΩ…ë}Ω…•ù•∏°çΩπ—Öç–∞ÅΩ…•ù•∏∞Ä®∞ÅÕΩ’…çîÙàà∞Åï·—ï…πÖ±}•êÙàà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—ï·–ı9Ωπî∞ÅëÖ—îı9Ωπî∞ÅµÖ≠ï}¡…•µÖ…‰ıÖ±Õî§Ë(ÄÄÄÄààâAï…Õ•Õ–ÅΩπîÅë•Õ—•πç–ÅΩ…•ù•∏Å›°•±îÅ≠ïï¡•πúÅ—°îÅô•…Õ–Åëï—ïç—ïêÅÖÃÅ¡…•µÖ…‰∏ààà(ÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°çΩπ—Öç–∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅçÖπΩπ•çÖ∞ÄÙÅ}ç…µ}çÖπΩπ•çÖ±}Ω…•ù•∏°Ω…•ù•∏§(ÄÄÄÅ•òÅπΩ–ÅçÖπΩπ•çÖ∞Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅâïôΩ…ï}Ω…•ù•∏ÄÙÅçΩπ—Öç–πùï–†âΩ…•ù•πîà§(ÄÄÄÅâïôΩ…ï}°•Õ—Ω…‰ÄÙÅçΩ¡‰πëïï¡çΩ¡‰°çΩπ—Öç–πùï–†âÕΩ’…çï}°•Õ—Ω…‰à§§(ÄÄÄÅ°•Õ—Ω…‰ÄÙÅçΩπ—Öç–πùï–†âÕΩ’…çï}°•Õ—Ω…‰à§(ÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°°•Õ—Ω…‰∞Å±•Õ–§Ë(ÄÄÄÄÄÄÄÅ°•Õ—Ω…‰ÄÙÅmt(ÄÄÄÅ°•Õ—Ω…‰ÄÙÅm•—ï¥ÅôΩ»Å•—ï¥Å•∏Å°•Õ—Ω…‰Å•òÅ•Õ•πÕ—Öπçî°•—ï¥∞Åë•ç–•t((ÄÄÄÅç’……ïπ—}¡…•µÖ…‰ÄÙÅ}ç…µ}çÖπΩπ•çÖ±}Ω…•ù•∏°çΩπ—Öç–πùï–†âΩ…•ù•πîà§§(ÄÄÄÅ•òÅµÖ≠ï}¡…•µÖ…‰ÅΩ»ÅπΩ–Åç’……ïπ—}¡…•µÖ…‰Ë(ÄÄÄÄÄÄÄÅç’……ïπ—}¡…•µÖ…‰ÄÙÅçÖπΩπ•çÖ∞(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâΩ…•ù•πîâtÄÙÅçÖπΩπ•çÖ∞((ÄÄÄÅëïòÅΩ…•ù•π}ôΩ»°•—ï¥§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å}ç…µ}çÖπΩπ•çÖ±}Ω…•ù•∏°•—ï¥πùï–†âΩ…•ù•∏à§ÅΩ»Å•—ï¥πùï–†âΩ…•ù•πîà§§((ÄÄÄÅëïòÅâ’•±ë}ïπ—…‰°±Öâï∞∞Ä®∞Åïπ—…Â}ÕΩ’…çîÙàà∞Åïπ—…Â}ï·—ï…πÖ±}•êÙàà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Â}çΩπ—ï·–ı9Ωπî∞Åïπ—…Â}ëÖ—îı9Ωπî§Ë(ÄÄÄÄÄÄÄÅëï—Ö•±ÃÄÙÅïπ—…Â}çΩπ—ï·–Å•òÅ•Õ•πÕ—Öπçî°ïπ—…Â}çΩπ—ï·–∞Åë•ç–§Åï±ÕîÅÌÙ(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâΩ…•ù•∏àËÅ±Öâï∞∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕΩ’…çîàËÅÕ—»°ïπ—…Â}ÕΩ’…çîÅΩ»Åëï—Ö•±Ãπùï–†âÕΩ’…çîà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâï·—ï…πÖ±}•êàËÅÕ—»†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Â}ï·—ï…πÖ±}•êÅΩ»Åëï—Ö•±Ãπùï–†âï·—ï…πÖ±}•êà§ÅΩ»Äàà(ÄÄÄÄÄÄÄÄÄÄÄÄ§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçÖµ¡Ö•ù∏àËÅÕ—»†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅëï—Ö•±Ãπùï–†âçÖµ¡Ö•ù∏à§ÅΩ»Åëï—Ö•±Ãπùï–†âçÖµ¡Ö•ùπ}πÖµîà§ÅΩ»Äàà(ÄÄÄÄÄÄÄÄÄÄÄÄ§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÖêàËÅÕ—»°ëï—Ö•±Ãπùï–†âÖêà§ÅΩ»Åëï—Ö•±Ãπùï–†âÖë}πÖµîà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâôΩ…¥àËÅÕ—»†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅëï—Ö•±Ãπùï–†âôΩ…¥à§ÅΩ»Åëï—Ö•±Ãπùï–†âôΩ…µ}πÖµîà§ÅΩ»Äàà(ÄÄÄÄÄÄÄÄÄÄÄÄ§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâëÖ—îàËÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Â}ëÖ—îÅΩ»Åëï—Ö•±Ãπùï–†âëÖ—îà§ÅΩ»Åëï—Ö•±Ãπùï–†â…ïçï•Ÿïë}Ö–à§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ»ÅçΩπ—Öç–πùï–†âç…ïÖ—ïë}Ö–à§ÅΩ»Å}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÅÙ((ÄÄÄÄåÅ!•Õ—Ω…•çÖ∞Å•µ¡Ω…—ÃÅÖπêÅ…ï¡ïÖ—ïêÅ›ïâ°ΩΩ≠ÃÅµÖ‰ÅçΩπ—Ö•∏ÅÕïŸï…Ö∞Åïπ—…•ïÃ(ÄÄÄÄåÅôΩ»Å—°îÅÕÖµîÅŸ•Õ•â±îÅΩ…•ù•∏∏ÅΩ±±Ö¡ÕîÅ—°ï¥Å›°•±îÅ…ï—Ö•π•πúÅ—°îÅïÖ…±•ïÕ–(ÄÄÄÄåÅ¡ΩÕ•—•Ω∏ÅÖπêÅïπ…•ç°•πúÅ•–Å›•—†ÅÖπ‰ÅçΩπ—ï·–ÅôΩ’πêÅ±Ö—ï»∏(ÄÄÄÅëïë’¡±•çÖ—ïêÄÙÅmt(ÄÄÄÅâÂ}Ω…•ù•∏ÄÙÅÌÙ(ÄÄÄÅôΩ»Å•—ï¥Å•∏Å°•Õ—Ω…‰Ë(ÄÄÄÄÄÄÄÅ•—ïµ}Ω…•ù•∏ÄÙÅΩ…•ù•π}ôΩ»°•—ï¥§(ÄÄÄÄÄÄÄÅµÖ…≠ï»ÄÙÅ}ç…µ}Ω…•ù•π}≠ï‰°•—ïµ}Ω…•ù•∏§(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅµÖ…≠ï»Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ•òÅµÖ…≠ï»ÅπΩ–Å•∏ÅâÂ}Ω…•ù•∏Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ•—ïµlâΩ…•ù•∏âtÄÙÅ•—ïµ}Ω…•ù•∏(ÄÄÄÄÄÄÄÄÄÄÄÅâÂ}Ω…•ù•πmµÖ…≠ï…tÄÙÅ•—ï¥(ÄÄÄÄÄÄÄÄÄÄÄÅëïë’¡±•çÖ—ïêπÖ¡¡ïπê°•—ï¥§(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅï·•Õ—•πúÄÙÅâÂ}Ω…•ù•πmµÖ…≠ï…t(ÄÄÄÄÄÄÄÅôΩ»Å≠ï‰Å•∏Ä†âÕΩ’…çîà∞Äâï·—ï…πÖ±}•êà∞ÄâçÖµ¡Ö•ù∏à∞ÄâÖêà∞ÄâôΩ…¥à∞ÄâëÖ—îà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–Åï·•Õ—•πúπùï–°≠ï‰§ÅÖπêÅ•—ï¥πùï–°≠ï‰§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅï·•Õ—•πùm≠ïÂtÄÙÅ•—ïµm≠ïÂt(ÄÄÄÅ°•Õ—Ω…‰ÄÙÅëïë’¡±•çÖ—ïê((ÄÄÄÅ¡…•µÖ…Â}•πëï‡ÄÙÅπï·–†(ÄÄÄÄÄÄÄÄ°•πëï‡ÅôΩ»Å•πëï‡∞Å•—ï¥Å•∏Åïπ’µï…Ö—î°°•Õ—Ω…‰§(ÄÄÄÄÄÄÄÄÅ•òÅ}ç…µ}Ω…•ù•π}≠ï‰°Ω…•ù•π}ôΩ»°•—ï¥§§ÄÙÙÅ}ç…µ}Ω…•ù•π}≠ï‰°ç’……ïπ—}¡…•µÖ…‰§§∞(ÄÄÄÄÄÄÄÅ9Ωπî∞(ÄÄÄÄ§(ÄÄÄÅ•òÅ¡…•µÖ…Â}•πëï‡Å•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅ°•Õ—Ω…‰π•πÕï…–†¿∞Åâ’•±ë}ïπ—…‰†(ÄÄÄÄÄÄÄÄÄÄÄÅç’……ïπ—}¡…•µÖ…‰∞(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Â}ÕΩ’…çîıçΩπ—Öç–πùï–†âÕΩ’…çîà§∞(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Â}çΩπ—ï·–ıçΩπ—Öç–πùï–†âµï—Ö}ÕΩ’…çîà§∞(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Â}ëÖ—îıçΩπ—Öç–πùï–†âç…ïÖ—ïë}Ö–à§∞(ÄÄÄÄÄÄÄÄ§§(ÄÄÄÅï±•òÅ¡…•µÖ…Â}•πëï‡Ë(ÄÄÄÄÄÄÄÅ°•Õ—Ω…‰π•πÕï…–†¿∞Å°•Õ—Ω…‰π¡Ω¿°¡…•µÖ…Â}•πëï‡§§(ÄÄÄÅ°•Õ—Ω…Âl¡ulâΩ…•ù•∏âtÄÙÅç’……ïπ—}¡…•µÖ…‰((ÄÄÄÅçÖπΩπ•çÖ±}•πëï‡ÄÙÅπï·–†(ÄÄÄÄÄÄÄÄ°•πëï‡ÅôΩ»Å•πëï‡∞Å•—ï¥Å•∏Åïπ’µï…Ö—î°°•Õ—Ω…‰§(ÄÄÄÄÄÄÄÄÅ•òÅ}ç…µ}Ω…•ù•π}≠ï‰°Ω…•ù•π}ôΩ»°•—ï¥§§ÄÙÙÅ}ç…µ}Ω…•ù•π}≠ï‰°çÖπΩπ•çÖ∞§§∞(ÄÄÄÄÄÄÄÅ9Ωπî∞(ÄÄÄÄ§(ÄÄÄÅ•òÅçÖπΩπ•çÖ±}•πëï‡Å•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅ°•Õ—Ω…‰πÖ¡¡ïπê°â’•±ë}ïπ—…‰†(ÄÄÄÄÄÄÄÄÄÄÄÅçÖπΩπ•çÖ∞∞(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Â}ÕΩ’…çîıÕΩ’…çî∞(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Â}ï·—ï…πÖ±}•êıï·—ï…πÖ±}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Â}çΩπ—ï·–ıçΩπ—ï·–∞(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Â}ëÖ—îıëÖ—î∞(ÄÄÄÄÄÄÄÄ§§(ÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÅïπ—…‰ÄÙÅ°•Õ—Ω…ÂmçÖπΩπ•çÖ±}•πëï·t(ÄÄÄÄÄÄÄÅïπ—…ÂlâΩ…•ù•∏âtÄÙÅçÖπΩπ•çÖ∞(ÄÄÄÄÄÄÄÅëï—Ö•±ÃÄÙÅçΩπ—ï·–Å•òÅ•Õ•πÕ—Öπçî°çΩπ—ï·–∞Åë•ç–§Åï±ÕîÅÌÙ(ÄÄÄÄÄÄÄÅ’¡ëÖ—ïÃÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâÕΩ’…çîàËÅÕΩ’…çîÅΩ»Åëï—Ö•±Ãπùï–†âÕΩ’…çîà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâï·—ï…πÖ±}•êàËÅï·—ï…πÖ±}•êÅΩ»Åëï—Ö•±Ãπùï–†âï·—ï…πÖ±}•êà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçÖµ¡Ö•ù∏àËÅëï—Ö•±Ãπùï–†âçÖµ¡Ö•ù∏à§ÅΩ»Åëï—Ö•±Ãπùï–†âçÖµ¡Ö•ùπ}πÖµîà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÖêàËÅëï—Ö•±Ãπùï–†âÖêà§ÅΩ»Åëï—Ö•±Ãπùï–†âÖë}πÖµîà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâôΩ…¥àËÅëï—Ö•±Ãπùï–†âôΩ…¥à§ÅΩ»Åëï—Ö•±Ãπùï–†âôΩ…µ}πÖµîà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâëÖ—îàËÅëÖ—îÅΩ»Åëï—Ö•±Ãπùï–†âëÖ—îà§ÅΩ»Åëï—Ö•±Ãπùï–†â…ïçï•Ÿïë}Ö–à§∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÅôΩ»Å≠ï‰∞ÅŸÖ±’îÅ•∏Å’¡ëÖ—ïÃπ•—ïµÃ†§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅŸÖ±’îÅÖπêÅπΩ–Åïπ—…‰πùï–°≠ï‰§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Âm≠ïÂtÄÙÅÕ—»°ŸÖ±’î§πÕ—…•¿†§Å•òÅ≠ï‰ÄÑÙÄâëÖ—îàÅï±ÕîÅŸÖ±’î((ÄÄÄÅ•òÅµÖ≠ï}¡…•µÖ…‰Ë(ÄÄÄÄÄÄÄÅ¡…ΩµΩ—ïë}•πëï‡ÄÙÅπï·–†(ÄÄÄÄÄÄÄÄÄÄÄÅ•πëï‡ÅôΩ»Å•πëï‡∞Å•—ï¥Å•∏Åïπ’µï…Ö—î°°•Õ—Ω…‰§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ}ç…µ}Ω…•ù•π}≠ï‰°Ω…•ù•π}ôΩ»°•—ï¥§§ÄÙÙÅ}ç…µ}Ω…•ù•π}≠ï‰°çÖπΩπ•çÖ∞§(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ•òÅ¡…ΩµΩ—ïë}•πëï‡Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ°•Õ—Ω…‰π•πÕï…–†¿∞Å°•Õ—Ω…‰π¡Ω¿°¡…ΩµΩ—ïë}•πëï‡§§(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâΩ…•ù•πîâtÄÙÅçÖπΩπ•çÖ∞(ÄÄÄÅçΩπ—Öç—lâÕΩ’…çï}°•Õ—Ω…‰âtÄÙÅ°•Õ—Ω…‰(ÄÄÄÅ…ï—’…∏Ä†(ÄÄÄÄÄÄÄÅçΩπ—Öç–πùï–†âΩ…•ù•πîà§ÄÑÙÅâïôΩ…ï}Ω…•ù•∏(ÄÄÄÄÄÄÄÅΩ»ÅçΩπ—Öç–πùï–†âÕΩ’…çï}°•Õ—Ω…‰à§ÄÑÙÅâïôΩ…ï}°•Õ—Ω…‰(ÄÄÄÄ§(()ëïòÅ}ç…µ}•πôΩ…µÖ—•Ωπ}…ï≈’ïÕ—}Ω…•ù•∏°ô•ï±ëÃ§Ë(ÄÄÄÄààâIïÕΩ±ŸîÅ—°îÅI4ÅΩ…•ù•∏Å›•—°Ω’–Åµ•Õç±ÖÕÕ•ôÂ•πúÅÕïç…ï—Ö…•Ö–ÅÕ’âµ•ÕÕ•ΩπÃ∏ààà(ÄÄÄÅ•òÅÕ—»†°ô•ï±ëÃÅΩ»ÅÌÙ§πùï–†âÕΩ’…çï}Õïç…ï—Ö…•Ö–à§ÅΩ»Äàà§ÄÙÙÄàƒàË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÄâMïçÀ•—Ö…•Ö–à(ÄÄÄÅ…ï—’…∏ÅI5}==1}M}=I%%8Å•òÅ}ç…µ}•πôΩ…µÖ—•Ωπ}…ï≈’ïÕ—}•Õ}ùΩΩù±ï}ÖëÃ°ô•ï±ëÃ§Åï±ÕîÄâM•—îÅ•π—ï…πï–à(()ëïòÅ}ç…µ}Ö¡¡±Â}•πôΩ…µÖ—•Ωπ}…ï≈’ïÕ—}Ö——…•â’—•Ω∏°çΩπ—Öç–∞Åô•ï±ëÃ§Ë(ÄÄÄÄààâAï…Õ•Õ–ÅΩΩù±îÅëÃÅµï—ÖëÖ—ÑÅ›•—°Ω’–Å…ï¡±Öç•πúÅÖ∏ÅïÖ…±•ï»Å¡…•µÖ…‰ÅΩ…•ù•∏∏ààà(ÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°ô•ï±ëÃ∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅ•òÄ†(ÄÄÄÄÄÄÄÅÕ—»°ô•ï±ëÃπùï–†âÕΩ’…çï}Õïç…ï—Ö…•Ö–à§ÅΩ»Äàà§ÄÙÙÄàƒà(ÄÄÄÄÄÄÄÅΩ»ÅπΩ–Å}ç…µ}•πôΩ…µÖ—•Ωπ}…ï≈’ïÕ—}•Õ}ùΩΩù±ï}ÖëÃ°ô•ï±ëÃ§(ÄÄÄÄ§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî((ÄÄÄÅπΩ…µÖ±•ÈïêÄÙÅ}ç…µ}ùΩΩù±ï}ÖëÕ}—…Öç≠•πù}ô•ï±ëÃ°ô•ï±ëÃ§(ÄÄÄÅ•ëïπ—•ô•ï…}—Â¡î∞Å•ëïπ—•ô•ï»ÄÙÅ}ç…µ}•πôΩ…µÖ—•Ωπ}…ï≈’ïÕ—}ùΩΩù±ï}ÖëÕ}•ëïπ—•ô•ï»°ô•ï±ëÃ§(ÄÄÄÅç°ÖπùïêÄÙÅÖ±Õî(ÄÄÄÅôΩ»Å≠ï‰Å•∏ÅI5}==1}M}%9Q%%I}-eLË(ÄÄÄÄÄÄÄÅ•òÅπΩ…µÖ±•Èïëm≠ïÂtÅÖπêÅçΩπ—Öç–πùï–°≠ï‰§ÄÑÙÅπΩ…µÖ±•Èïëm≠ïÂtË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—m≠ïÂtÄÙÅπΩ…µÖ±•Èïëm≠ïÂt(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅ•òÅ•ëïπ—•ô•ï»ÅÖπêÅçΩπ—Öç–πùï–†âùΩΩù±ï}ÖëÕ}•ëïπ—•ô•ï»à§ÄÑÙÅ•ëïπ—•ô•ï»Ë(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâùΩΩù±ï}ÖëÕ}•ëïπ—•ô•ï»âtÄÙÅ•ëïπ—•ô•ï»(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅ•òÅ•ëïπ—•ô•ï…}—Â¡îÅÖπêÅçΩπ—Öç–πùï–†âùΩΩù±ï}ÖëÕ}•ëïπ—•ô•ï…}—Â¡îà§ÄÑÙÅ•ëïπ—•ô•ï…}—Â¡îË(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâùΩΩù±ï}ÖëÕ}•ëïπ—•ô•ï…}—Â¡îâtÄÙÅ•ëïπ—•ô•ï…}—Â¡î(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î((ÄÄÄÄåÅ=±êÅ•πôΩ…µÖ—•Ω∏µ…ï≈’ïÕ–Å…ïçΩ…ëÃÅÕΩµï—•µïÃÅÕ—Ω…ïêÄâM•—îÅ•π—ï…πï–àÅïŸï∏(ÄÄÄÄåÅ—°Ω’ù†Å—°ï•»ÅΩ…•ù•πÖ∞Å¡ÖÂ±ΩÖêÅÖ±…ïÖë‰ÅçΩπ—Ö•πïêÅÑÅΩΩù±îÅëÃÅ•ëïπ—•ô•ï»∏(ÄÄÄÄåÅIï¡Ö•»ÅΩπ±‰Å—°Ö–ÅÕ•πù±îµΩ…•ù•∏Å±ïùÖç‰ÅçÖÕîÏÅΩ∏ÅÑÅ…ïçΩπç•±ïêÅçΩπ—Öç–ÅΩΩù±î(ÄÄÄÄåÅëÃÅ•ÃÅÖ¡¡ïπëïêÅÖÃÅÑÅÕïçΩπëÖ…‰ÅΩ…•ù•∏ÅÖπêÅπïŸï»Å¡…ΩµΩ—ïê∏(ÄÄÄÅï·•Õ—•πù}Ω…•ù•πÃÄÙÅÏ(ÄÄÄÄÄÄÄÅ}ç…µ}Ω…•ù•π}≠ï‰°}ç…µ}çÖπΩπ•çÖ±}Ω…•ù•∏°•—ï¥πùï–†âΩ…•ù•∏à§§§(ÄÄÄÄÄÄÄÅôΩ»Å•—ï¥Å•∏ÅçΩπ—Öç–πùï–†âÕΩ’…çï}°•Õ—Ω…‰à∞Åmt§(ÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°•—ï¥∞Åë•ç–§ÅÖπêÅ•—ï¥πùï–†âΩ…•ù•∏à§(ÄÄÄÅÙ(ÄÄÄÅ±ïùÖçÂ}¡…•µÖ…Â}…ï¡Ö•»ÄÙÄ†(ÄÄÄÄÄÄÄÅçΩπ—Öç–πùï–†âÕΩ’…çîà§ÄÙÙÄâëïµÖπëï}•πôΩÕ}ôΩ…µÖ—•ΩπÃà(ÄÄÄÄÄÄÄÅÖπêÅ}ç…µ}çÖπΩπ•çÖ±}Ω…•ù•∏°çΩπ—Öç–πùï–†âΩ…•ù•πîà§§ÄÙÙÄâM•—îÅ•π—ï…πï–à(ÄÄÄÄÄÄÄÅÖπêÅï·•Õ—•πù}Ω…•ù•πÃπ•ÕÕ’âÕï–°Ì}ç…µ}Ω…•ù•π}≠ï‰†âM•—îÅ•π—ï…πï–à•Ù§(ÄÄÄÄ§(ÄÄÄÅçΩπ—ï·–ÄÙÅÏ(ÄÄÄÄÄÄÄÄâçÖµ¡Ö•ù∏àËÅπΩ…µÖ±•Èïêπùï–†â’—µ}çÖµ¡Ö•ù∏à∞Äàà§∞(ÄÄÄÄÄÄÄÄâÖêàËÅπΩ…µÖ±•Èïêπùï–†â’—µ}çΩπ—ïπ–à∞Äàà§∞(ÄÄÄÄÄÄÄÄâôΩ…¥àËÅÕ—»°ô•ï±ëÃπùï–†âôΩ…µ}πÖµîà§ÅΩ»Åô•ï±ëÃπùï–†âôΩ…¥à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÅÙ(ÄÄÄÅ•òÅ}ç…µ}…ïçΩ…ë}Ω…•ù•∏†(ÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÅI5}==1}M}=I%%8∞(ÄÄÄÄÄÄÄÅÕΩ’…çîÙâëïµÖπëï}•πôΩÕ}ôΩ…µÖ—•ΩπÃà∞(ÄÄÄÄÄÄÄÅï·—ï…πÖ±}•êıçΩπ—Öç–πùï–†âÕΩ’…çï}ëïµÖπëï}•êà∞Äàà§∞(ÄÄÄÄÄÄÄÅçΩπ—ï·–ıçΩπ—ï·–∞(ÄÄÄÄÄÄÄÅµÖ≠ï}¡…•µÖ…‰ı±ïùÖçÂ}¡…•µÖ…Â}…ï¡Ö•»∞(ÄÄÄÄ§Ë(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅ•òÅç°ÖπùïêË(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅ…ï—’…∏Åç°Öπùïê(()ëïòÅ}ç…µ}âÖç≠ô•±±}•πôΩ…µÖ—•Ωπ}…ï≈’ïÕ—}Ö——…•â’—•Ω∏°çΩπ—Öç–§Ë(ÄÄÄÄààâIï¡Ö•»ÅçΩπ—Öç—ÃÅç…ïÖ—ïêÅâïôΩ…îÅ1%Å›ÖÃÅ¡…ΩµΩ—ïêÅ—ºÅÑÅô•…Õ–µç±ÖÕÃÅô•ï±ê∏ààà(ÄÄÄÅôΩ…¥ÄÙÅçΩπ—Öç–πùï–†âôΩ…µ’±Ö•…îà§(ÄÄÄÅ•òÅçΩπ—Öç–πùï–†âÕΩ’…çîà§ÄÑÙÄâëïµÖπëï}•πôΩÕ}ôΩ…µÖ—•ΩπÃàÅΩ»ÅπΩ–Å•Õ•πÕ—Öπçî°ôΩ…¥∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅ…ï—’…∏Å}ç…µ}Ö¡¡±Â}•πôΩ…µÖ—•Ωπ}…ï≈’ïÕ—}Ö——…•â’—•Ω∏°çΩπ—Öç–∞ÅôΩ…¥§(()ëïòÅ}ç…µ}çΩπ—Öç—}…ïÕ¡ΩπÕî°çΩπ—Öç–∞ÅëÖ—Ñı9Ωπî∞Å…ïù’±Ö—Ω…Â}ÕπÖ¡Õ°Ω–ı9Ωπî∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅô’πë•πù}Õ—Ö—’Ãı9Ωπî§Ë(ÄÄÄÅ}ç…µ}âÖç≠ô•±±}•πôΩ…µÖ—•Ωπ}…ï≈’ïÕ—}ÖπÕ›ï…Ã°çΩπ—Öç–§(ÄÄÄÅ}ç…µ}âÖç≠ô•±±}•πôΩ…µÖ—•Ωπ}…ï≈’ïÕ—}Ö——…•â’—•Ω∏°çΩπ—Öç–§(ÄÄÄÅ}ç…µ}ïπÕ’…ï}…ï±ÖπçïÃ°çΩπ—Öç–§(ÄÄÄÅ…ïÕ¡ΩπÕîÄÙÅë•ç–°çΩπ—Öç–§(ÄÄÄÅ…ïÕ¡ΩπÕîπÕï—ëïôÖ’±–†â…ïÕ—ï}Ö}ç°Ö…ùï}¡ï…Õºà∞Äàà§(ÄÄÄÅ…ïÕ¡ΩπÕîπÕï—ëïôÖ’±–†âç¡ô}¡Ö±•ï»à∞Äàà§(ÄÄÄÅ…ïÕ¡ΩπÕîπÕï—ëïôÖ’±–†âµΩπ—Öπ—}ÖççΩ…ëï}ô–à∞Äàà§(ÄÄÄÅ…ïÕ¡ΩπÕîπÕï—ëïôÖ’±–†âçπÖ¡Õ}π’àà∞Äàà§(ÄÄÄÅ…ïÕ¡ΩπÕîπÕï—ëïôÖ’±–†âçπÖ¡Õ}çÖ…ë}ŸÖ±•ë•—‰à∞Å9Ωπî§(ÄÄÄÅ…ïÕ¡ΩπÕîπÕï—ëïôÖ’±–†âçπÖ¡Õ}â•…—°}ÂïÖ»à∞Äàà§(ÄÄÄÅ•òÅπΩ–Å…ïÕ¡ΩπÕîπùï–†âô•πÖπçïµïπ—}¡ï…ÕΩ}¡ΩÕÕ•â±îà§Ë(ÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕïlâô•πÖπçïµïπ—}¡ï…ÕΩ}¡ΩÕÕ•â±îâtÄÙÅÕ—»†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–πùï–†â…ïô’Õ}ô—}¡ï…Õºà§ÅΩ»Äàà(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅ…ïÕ¡ΩπÕîπÕï—ëïôÖ’±–†â≈’Ö±•ô•çÖ—•Ωπ}ô±Öúà∞Äàà§(ÄÄÄÄåÅ1îÅÕ—Ö—’–Å]=ÅïÕ–ÅçÖ±ç’≥§Å’πîÅÕï’±îÅôΩ•ÃÅ¡Ω’»Å—Ω’—îÅ±ÑÅ±•Õ—îÅëïÃÅçΩπ—Öç—Ã(ÄÄÄÄåÅ¡’•ÃÅ•π©ïç”§Å•ç§∏Å9îÅ©ÖµÖ•ÃÅ…ï±•…îÅ—Ω’—îÅ±ÑÅâÖÕîÅ]=Åëï¡’•ÃÅçï——îÅôΩπç—•Ω∏ÄË(ÄÄÄÄåÅï±±îÅïÕ–ÅÖ¡¡ï≥•îÅ’πîÅôΩ•ÃÅ¡Ö»Å¡•Õ—îÅï–Å—…ÖπÕôΩ…µï…Ö•–Å±ÑÅ…ï≈◊©—îÅï∏Å8É\Å4∏(ÄÄÄÅ•òÄ°ô’πë•πù}Õ—Ö—’Ã(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅçΩπ—Öç–πùï–†âÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô—}ÕΩ’…çîà§(ÄÄÄÄÄÄÄÄÄÄÄÄÑÙÅI5}59U1}MQQUM}M=UI§Ë(ÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕïlâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–âtÄÙÅô’πë•πù}Õ—Ö—’Ã(ÄÄÄÅÕπÖ¡Õ°Ω–ÄÙÅ…ïù’±Ö—Ω…Â}ÕπÖ¡Õ°Ω–(ÄÄÄÅ•òÅÕπÖ¡Õ°Ω–Å•ÃÅ9ΩπîÅÖπêÅëÖ—ÑÅ•ÃÅπΩ–Å9ΩπîË(ÄÄÄÄÄÄÄÅÕπÖ¡Õ°Ω–ÄÙÅëÖ—Ñπùï–†âç…µ}çπÖ¡Õ}ÕçΩ…•πù}ÕπÖ¡Õ°Ω—Ãà∞ÅÌÙ§πùï–°Õ—»°çΩπ—Öç–πùï–†â•êà§§§(ÄÄÄÅ…ïÕ¡ΩπÕïlâ•π—ïù…Ö—•Ωπ}ÕçΩ…îâtÄÙÅçÖ±ç’±Ö—ï}çÖπë•ëÖ—ï}•π—ïù…Ö—•Ωπ}ÕçΩ…î°…ïÕ¡ΩπÕî∞ÅÕπÖ¡Õ°Ω–§(ÄÄÄÅ…ï—’…∏Å…ïÕ¡ΩπÕî(()ëïòÅ}ç…µ}çΩπ—Öç—}ëï—Ö•±}…ïÕ¡ΩπÕî°çΩπ—Öç–∞ÅëÖ—Ñı9Ωπî∞Å…ïù’±Ö—Ω…Â}ÕπÖ¡Õ°Ω–ı9Ωπî∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅô’πë•πù}Õ—Ö—’Ãı9Ωπî§Ë(ÄÄÄÄààâ	’•±êÅ—°îÅ•π—ï…Öç—•ŸîÅÕ°ïï–Å›•—°Ω’–Åïµâïëë•πúÅ°•Õ—Ω…•çÖ∞Å!Q50ÅâΩë•ïÃ∏ààà(ÄÄÄÅ…ïÕ¡ΩπÕîÄÙÅ}ç…µ}çΩπ—Öç—}…ïÕ¡ΩπÕî†(ÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÅëÖ—Ñ∞(ÄÄÄÄÄÄÄÅ…ïù’±Ö—Ω…Â}ÕπÖ¡Õ°Ω–ı…ïù’±Ö—Ω…Â}ÕπÖ¡Õ°Ω–∞(ÄÄÄÄÄÄÄÅô’πë•πù}Õ—Ö—’Ãıô’πë•πù}Õ—Ö—’Ã∞(ÄÄÄÄ§(ÄÄÄÄåÅQ°îÅΩ…•ù•πÖ∞Å¡’â±•åÅôΩ…¥ÅµÖ‰ÅçΩπ—Ö•∏ÅÑÅ±Ö…ùîÅ—ïç°π•çÖ∞Ω…Ö‹Å¡ÖÂ±ΩÖê∏Å%—Ã(ÄÄÄÄåÅ’Õïô’∞ÅÖπÕ›ï…ÃÅÖ…îÅÖ±…ïÖë‰Å¡…ΩµΩ—ïêÅ—ºÅô•…Õ–µç±ÖÕÃÅI4Åô•ï±ëÃ∏(ÄÄÄÅ…ïÕ¡ΩπÕîπ¡Ω¿†âôΩ…µ’±Ö•…îà∞Å9Ωπî§(ÄÄÄÅçΩµ¡Öç—}Öç—•Ÿ•—•ïÃÄÙÅmt(ÄÄÄÅôΩ»Å…Ö›}Öç—•Ÿ•—‰Å•∏Å…ïÕ¡ΩπÕîπùï–†âÖç—•Ÿ•—•ïÃà∞Åmt§Ë(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°…Ö›}Öç—•Ÿ•—‰∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅÖç—•Ÿ•—‰ÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÅ≠ï‰ËÅŸÖ±’îÅôΩ»Å≠ï‰∞ÅŸÖ±’îÅ•∏Å…Ö›}Öç—•Ÿ•—‰π•—ïµÃ†§Å•òÅ≠ï‰ÄÑÙÄâ¡…ïŸ•ï‹à(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÅ•òÅ…Ö›}Öç—•Ÿ•—‰πùï–†â¡…ïŸ•ï‹à§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÖç—•Ÿ•—Âlâ°ÖÕ}¡…ïŸ•ï‹âtÄÙÅQ…’î(ÄÄÄÄÄÄÄÅçΩµ¡Öç—}Öç—•Ÿ•—•ïÃπÖ¡¡ïπê°Öç—•Ÿ•—‰§(ÄÄÄÅ…ïÕ¡ΩπÕïlâÖç—•Ÿ•—•ïÃâtÄÙÅçΩµ¡Öç—}Öç—•Ÿ•—•ïÃ(ÄÄÄÅ…ïÕ¡ΩπÕïlâÖç—•Ÿ•—Â}çΩ’π–âtÄÙÅ±ï∏°çΩµ¡Öç—}Öç—•Ÿ•—•ïÃ§(ÄÄÄÅ…ï—’…∏Å…ïÕ¡ΩπÕî(()ëïòÅ}ç…µ}Öç—•Ÿ•—‰†(ÄÄÄÄÄÄÄÅçΩπ—Öç–∞Å≠•πê∞Å—•—±î∞Åëï—Ö•∞Ùàà∞Å¡…ïŸ•ï‹Ùàà∞ÅÖ’—°Ω…}πÖµîı9Ωπî§Ë(ÄÄÄÅ•òÅπΩ–ÅÖ’—°Ω…}πÖµîË(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÖ’—°Ω…}πÖµîÄÙÄ°ç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ§πùï–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâπÖµîà∞Äã%≈’•¡îÅ%π”•ù…Ö±îà(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅï·çï¡–ÅI’π—•µï……Ω»Ë(ÄÄÄÄÄÄÄÄÄÄÄÄåÅ1ïÃÅÕÂπç°…Ωπ•ÕÖ—•ΩπÃÅ]=ÅÃùï„•ç’—ïπ–ÅÖ’ÕÕ§Å°Ω…ÃÅ…ï≈◊©—îÅ±ÖÕ¨∏(ÄÄÄÄÄÄÄÄÄÄÄÅÖ’—°Ω…}πÖµîÄÙÄã%≈’•¡îÅ%π”•ù…Ö±îà(ÄÄÄÅçΩπ—Öç–πÕï—ëïôÖ’±–†âÖç—•Ÿ•—•ïÃà∞Åmt§π•πÕï…–†¿∞ÅÏ(ÄÄÄÄÄÄÄÄâ•êàËÅÕ—»°’’•êπ’’•ê–†§§∞ÄâëÖ—îàËÅ}ç…µ}πΩ‹†§∞Äâ≠•πêàËÅ≠•πê∞(ÄÄÄÄÄÄÄÄâ—•—±îàËÅ—•—±î∞Äâëï—Ö•∞àËÅëï—Ö•∞∞Äâ¡…ïŸ•ï‹àËÅ¡…ïŸ•ï‹∞(ÄÄÄÄÄÄÄÄâÖ’—°Ω»àËÅÖ’—°Ω…}πÖµî∞(ÄÄÄÅÙ§(()ëïòÅ}ç…µ}çΩπ—Öç—}ôΩ…}≈’Ω—ï}ïµÖ•∞°ëÖ—Ñ∞Å≈’Ω—î§Ë(ÄÄÄÄààâIï—’…∏Å—°îÅΩπ±‰ÅI4ÅçΩπ—Öç–Å—°Ö–ÅçÖ∏ÅÕÖôï±‰ÅâîÅ±•π≠ïêÅ—ºÅ—°•ÃÅ≈’Ω—î∏ààà(ÄÄÄÅçΩπ—Öç—ÃÄÙÅl(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÅôΩ»ÅçΩπ—Öç–Å•∏ÅëÖ—Ñπùï–†âç…µ}çΩπ—Öç—Ãà∞Åmt§(ÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°çΩπ—Öç–∞Åë•ç–§(ÄÄÄÅt(ÄÄÄÅ≈’Ω—ï}•êÄÙÅÕ—»°≈’Ω—îπùï–†â•êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ±•π≠ïêÄÙÅl(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÅôΩ»ÅçΩπ—Öç–Å•∏ÅçΩπ—Öç—Ã(ÄÄÄÄÄÄÄÅ•òÅ≈’Ω—ï}•êÅÖπêÅÕ—»°çΩπ—Öç–πùï–†âÕΩ’…çï}ëïŸ•Õ}•êà§ÅΩ»Äàà§πÕ—…•¿†§ÄÙÙÅ≈’Ω—ï}•ê(ÄÄÄÅt(ÄÄÄÅ•òÅ±ï∏°±•π≠ïê§ÄÙÙÄƒË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å±•π≠ïël¡t(ÄÄÄÅ•òÅ±•π≠ïêË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî((ÄÄÄÅïµÖ•∞ÄÙÅ}ç…µ}πΩ…µÖ±•Èï}ïµÖ•∞°≈’Ω—îπùï–†âµÖ•∞à§§(ÄÄÄÅ¡°ΩπîÄÙÅ}ç…µ}πΩ…µÖ±•Èï}¡°Ωπî°≈’Ω—îπùï–†â—ï±ï¡°Ωπîà§§(ÄÄÄÅµÖ—ç°ïÃÄÙÅl(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÅôΩ»ÅçΩπ—Öç–Å•∏ÅçΩπ—Öç—Ã(ÄÄÄÄÄÄÄÅ•òÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅïµÖ•∞ÅÖπêÅ}ç…µ}πΩ…µÖ±•Èï}ïµÖ•∞°çΩπ—Öç–πùï–†âµÖ•∞à§§ÄÙÙÅïµÖ•∞(ÄÄÄÄÄÄÄÄ§ÅΩ»Ä†(ÄÄÄÄÄÄÄÄÄÄÄÅ¡°ΩπîÅÖπêÅ}ç…µ}πΩ…µÖ±•Èï}¡°Ωπî°çΩπ—Öç–πùï–†â—ï±ï¡°Ωπîà§§ÄÙÙÅ¡°Ωπî(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅt(ÄÄÄÅ•òÅ±ï∏°µÖ—ç°ïÃ§ÄÑÙÄƒÅΩ»ÅπΩ–Å}ç…µ}πÖµïÕ}çΩµ¡Ö—•â±î°µÖ—ç°ïÕl¡t∞Å≈’Ω—î§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(ÄÄÄÅ…ï—’…∏ÅµÖ—ç°ïÕl¡t(()ëïòÅ}ç…µ}…ïçΩ…ë}≈’Ω—ï}ïµÖ•±}Õïπ–°ëÖ—Ñ∞Å≈’Ω—î∞ÅÕ’â©ïç–∞Å°—µ±}âΩë‰§Ë(ÄÄÄÄààâ¡¡ïπêÅÑÅÕ’ççïÕÕô’∞Å≈’Ω—îÅëï±•Ÿï…‰Å—ºÅ—°îÅµÖ—ç°•πúÅI4ÅÖç—•Ÿ•—‰Å©Ω’…πÖ∞∏ààà(ÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç—}ôΩ…}≈’Ω—ï}ïµÖ•∞°ëÖ—Ñ∞Å≈’Ω—î§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅëï—Ö•∞ÄÙÄâq∏àπ©Ω•∏°ô•±—ï»°9Ωπî∞Ä†(ÄÄÄÄÄÄÄÅòâ=â©ï–ÄËÅÌÕ—»°Õ’â©ïç–ÅΩ»Äúú§πÕ—…•¿†•Ùà∞(ÄÄÄÄÄÄÄÅòâïÕ—•πÖ—Ö•…îÄËÅÌÕ—»°≈’Ω—îπùï–†ùµÖ•∞ú§ÅΩ»Äúú§πÕ—…•¿†•Ùà∞(ÄÄÄÄ§§§(ÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰†(ÄÄÄÄÄÄÄÅçΩπ—Öç–∞ÄâïµÖ•∞à∞ÄâµµÖ•∞É
¨ÅïŸ•ÃÅì•—Ö•±≥§É
ÏÅïπŸΩÁ§à∞(ÄÄÄÄÄÄÄÅëï—Ö•∞∞Å°—µ±}âΩë‰∞(ÄÄÄÄ§(ÄÄÄÅÖç—•Ÿ•—‰ÄÙÅçΩπ—Öç—lâÖç—•Ÿ•—•ïÃâul¡t(ÄÄÄÅÖç—•Ÿ•—ÂlâÕΩ’…çï}ëïŸ•Õ}•êâtÄÙÅÕ—»°≈’Ω—îπùï–†â•êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅÖç—•Ÿ•—ÂlâëÖ—îât(ÄÄÄÅ…ï—’…∏ÅQ…’î(()ëïòÅ}ç…µ}ïë•—}çÖ±±}Öç—•Ÿ•—‰°Öç—•Ÿ•—‰∞Åëï—Ö•∞§Ë(ÄÄÄÄààâU¡ëÖ—îÅÑÅçÖ±∞ÅπΩ—îÅ›°•±îÅ…ï—Ö•π•πúÅïŸï…‰Å¡…ïŸ•Ω’ÃÅ—ï·–ÅÖÃÅÖ∏ÅÖ’ë•–Å—…Ö•∞∏ààà(ÄÄÄÅπΩ…µÖ±•ÈïêÄÙÅÕ—»°ëï—Ö•∞ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–ÅπΩ…µÖ±•ÈïêË(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†â1îÅçΩµ¡—îµ…ïπë‘ÅëîÅ≥äeÖ¡¡ï∞ÅïÕ–Å…ï≈’•Ãà§(ÄÄÄÅ•òÅÖç—•Ÿ•—‰πùï–†â≠•πêà§ÄÑÙÄâÖ¡¡ï∞àË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî∞Äâ•πŸÖ±•ë}≠•πêà(ÄÄÄÅ¡…ïŸ•Ω’ÃÄÙÅÕ—»°Öç—•Ÿ•—‰πùï–†âëï—Ö•∞à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅ¡…ïŸ•Ω’ÃÄÙÙÅπΩ…µÖ±•ÈïêË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî∞Äâ’πç°Öπùïêà(ÄÄÄÅπΩ‹ÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅïë•—Ω»ÄÙÄ°ç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ§πùï–†âπÖµîà∞Äã%≈’•¡îÅ%π”•ù…Ö±îà§(ÄÄÄÅïë•—ÃÄÙÅÖç—•Ÿ•—‰πùï–†âïë•—Ãà§(ÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°ïë•—Ã∞Å±•Õ–§Ë(ÄÄÄÄÄÄÄÅïë•—ÃÄÙÅmt(ÄÄÄÄÄÄÄÅÖç—•Ÿ•—Âlâïë•—ÃâtÄÙÅïë•—Ã(ÄÄÄÅïë•—Ãπ•πÕï…–†¿∞ÅÏ(ÄÄÄÄÄÄÄÄâëï—Ö•∞àËÅ¡…ïŸ•Ω’Ã∞(ÄÄÄÄÄÄÄÄâïë•—ïë}Ö–àËÅπΩ‹∞(ÄÄÄÄÄÄÄÄâïë•—ïë}â‰àËÅïë•—Ω»∞(ÄÄÄÅÙ§(ÄÄÄÅÖç—•Ÿ•—‰π’¡ëÖ—î°Ï(ÄÄÄÄÄÄÄÄâëï—Ö•∞àËÅπΩ…µÖ±•Èïê∞(ÄÄÄÄÄÄÄÄâïë•—ïë}Ö–àËÅπΩ‹∞(ÄÄÄÄÄÄÄÄâïë•—ïë}â‰àËÅïë•—Ω»∞(ÄÄÄÅÙ§(ÄÄÄÅ…ï—’…∏ÅQ…’î∞Äâ’¡ëÖ—ïêà(()I5}I19}MQQUMLÄÙÅÏâÕç°ïë’±ïêà∞ÄâÖπÕ›ï…ïêà∞ÄâπΩ}ÖπÕ›ï»à∞Äâ…ï¡…Ωù…Öµµïêà∞ÄâçÖπçï±±ïêâÙ)I5}I19}5=Q%}5a}19Q ÄÙÄƒÿ¿(()ëïòÅ}ç…µ}…ï±Öπçï}ëÖ—î°ŸÖ±’î∞Ä®∞Å›ïï≠ëÖÂÕ}Ωπ±‰ıÖ±Õî§Ë(ÄÄÄÄààâIï—’…∏ÅÑÅπΩ…µÖ±•ÈïêÅ%M<ÅëÖ—îÅΩ»Å…Ö•ÕîÅÑÅ’Õï»µôÖç•πúÅŸÖ±•ëÖ—•Ω∏Åï……Ω»∏ààà(ÄÄÄÅπΩ…µÖ±•ÈïêÄÙÅÕ—»°ŸÖ±’îÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–ÅπΩ…µÖ±•ÈïêË(ÄÄÄÄÄÄÄÅ…ï—’…∏Äàà(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ¡Ö…ÕïêÄÙÅëÖ—ï—•µîπëÖ—îπô…Ωµ•ÕΩôΩ…µÖ–°πΩ…µÖ±•Èïê§(ÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†â1ÑÅëÖ—îÅëîÅ…ï±ÖπçîÅïÕ–Å•πŸÖ±•ëî∏à§Åô…Ω¥Åï·å(ÄÄÄÅ•òÅ›ïï≠ëÖÂÕ}Ωπ±‰ÅÖπêÅ¡Ö…Õïêπ›ïï≠ëÖ‰†§Ä¯ÙÄ‘Ë(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†(ÄÄÄÄÄÄÄÄÄÄÄÄâ1ïÃÅ…ï±ÖπçïÃÅπîÅ¡ï’Ÿïπ–Å¡ÖÃÉ©—…îÅ¡…Ωù…Öµ∑•ïÃÅ±îÅÕÖµïë§ÅΩ‘Å±îÅë•µÖπç°î∏à(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅ…ï—’…∏ÅπΩ…µÖ±•Èïê(()ëïòÅ}ç…µ}…ï±Öπçï}µΩ—•ò°ŸÖ±’î§Ë(ÄÄÄÄààâIï—’…∏ÅÑÅçΩµ¡Öç–Å’Õï»µôÖç•πúÅ…ï±ÖπçîÅ…ïÖÕΩ∏ÅΩ»Å…ï©ïç–ÅΩŸï…Õ•ÈïêÅ•π¡’–∏ààà(ÄÄÄÅπΩ…µÖ±•ÈïêÄÙÄàÄàπ©Ω•∏°Õ—»°ŸÖ±’îÅΩ»Äàà§πÕ¡±•–†§§(ÄÄÄÅ•òÅ±ï∏°πΩ…µÖ±•Èïê§Ä¯ÅI5}I19}5=Q%}5a}19Q Ë(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†(ÄÄÄÄÄÄÄÄÄÄÄÄâ1îÅµΩ—•òÅëîÅ…ï±ÖπçîÅπîÅ¡ï’–Å¡ÖÃÅì•¡ÖÕÕï»Äà(ÄÄÄÄÄÄÄÄÄÄÄÅòâÌI5}I19}5=Q%}5a}19Q!ÙÅçÖ…Öç”°…ïÃ∏à(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅ…ï—’…∏ÅπΩ…µÖ±•Èïê(()ëïòÅ}ç…µ}…ïô…ïÕ°}…ï±Öπçï}ëÖ—î°çΩπ—Öç–§Ë(ÄÄÄÄààâ-ïï¿Å—°îÅ°•Õ—Ω…•çÖ∞ÅÕçÖ±Ö»Åô•ï±êÅÖ±•ùπïêÅ›•—†Å—°îÅπï·–ÅΩ¡ï∏ÅôΩ±±Ω‹µ’¿∏ààà(ÄÄÄÅÕç°ïë’±ïë}ëÖ—ïÃÄÙÅl(ÄÄÄÄÄÄÄÅÕ—»°•—ï¥πùï–†âÕç°ïë’±ïë}ëÖ—îà§ÅΩ»Äàà§(ÄÄÄÄÄÄÄÅôΩ»Å•—ï¥Å•∏ÅçΩπ—Öç–πùï–†â…ï±ÖπçïÃà∞Åmt§(ÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°•—ï¥∞Åë•ç–§ÅÖπêÅ•—ï¥πùï–†âÕ—Ö—’Ãà§ÄÙÙÄâÕç°ïë’±ïêà(ÄÄÄÄÄÄÄÅÖπêÅÕ—»°•—ï¥πùï–†âÕç°ïë’±ïë}ëÖ—îà§ÅΩ»Äàà§(ÄÄÄÅt(ÄÄÄÅπï·—}ëÖ—îÄÙÅµ•∏°Õç°ïë’±ïë}ëÖ—ïÃ§Å•òÅÕç°ïë’±ïë}ëÖ—ïÃÅï±ÕîÄàà(ÄÄÄÅç°ÖπùïêÄÙÅÕ—»°çΩπ—Öç–πùï–†â…ï±Öπçï}ëÖ—îà§ÅΩ»Äàà§ÄÑÙÅπï·—}ëÖ—î(ÄÄÄÅçΩπ—Öç—lâ…ï±Öπçï}ëÖ—îâtÄÙÅπï·—}ëÖ—î(ÄÄÄÅ…ï—’…∏Åç°Öπùïê(()ëïòÅ}ç…µ}ïπÕ’…ï}…ï±ÖπçïÃ°çΩπ—Öç–§Ë(ÄÄÄÄààâ5•ù…Ö—îÅ—°îÅ±ïùÖç‰ÅÅÅ…ï±Öπçï}ëÖ—ïÅÄÅô•ï±êÅ—ºÅÖ∏ÅÖ’ë•—Öâ±îÅ…ï±ÖπçîÅ±•Õ–∏ààà(ÄÄÄÅç°ÖπùïêÄÙÅÖ±Õî(ÄÄÄÅ…Ö›}…ï±ÖπçïÃÄÙÅçΩπ—Öç–πùï–†â…ï±ÖπçïÃà§(ÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°…Ö›}…ï±ÖπçïÃ∞Å±•Õ–§Ë(ÄÄÄÄÄÄÄÅ…Ö›}…ï±ÖπçïÃÄÙÅmt(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î((ÄÄÄÅπΩ…µÖ±•ÈïêÄÙÅmt(ÄÄÄÅçÖπçï±±ïë}ëÖ—ïÃÄÙÅÕï–†§(ÄÄÄÅÕïïπ}•ëÃÄÙÅÕï–†§(ÄÄÄÅôΩ»Å…Ö‹Å•∏Å…Ö›}…ï±ÖπçïÃË(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°…Ö‹∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ•—ï¥ÄÙÅë•ç–°…Ö‹§(ÄÄÄÄÄÄÄÅ…ï±Öπçï}•êÄÙÅÕ—»°•—ï¥πùï–†â•êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å…ï±Öπçï}•êÅΩ»Å…ï±Öπçï}•êÅ•∏ÅÕïïπ}•ëÃË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï±Öπçï}•êÄÙÅÕ—»°’’•êπ’’•ê–†§§(ÄÄÄÄÄÄÄÄÄÄÄÅ•—ïµlâ•êâtÄÙÅ…ï±Öπçï}•ê(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅÕïïπ}•ëÃπÖëê°…ï±Öπçï}•ê§(ÄÄÄÄÄÄÄÅÕ—Ö—’ÃÄÙÅÕ—»°•—ï¥πùï–†âÕ—Ö—’Ãà§ÅΩ»ÄâÕç°ïë’±ïêà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅÕ—Ö—’ÃÅπΩ–Å•∏ÅI5}I19}MQQUMLË(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö—’ÃÄÙÄâÕç°ïë’±ïêà(ÄÄÄÄÄÄÄÄÄÄÄÅ•—ïµlâÕ—Ö—’ÃâtÄÙÅÕ—Ö—’Ã(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ•òÅÕ—Ö—’ÃÄÙÙÄâçÖπçï±±ïêàË(ÄÄÄÄÄÄÄÄÄÄÄÄåÅUπîÅÖππ’±Ö—•Ω∏ÅïÕ–Å’πîÅÕ’¡¡…ïÕÕ•Ω∏Å∑•—•ï»ÄËÅ±ïÃÅÖπç•ïππïÃÅ—…ÖçïÃ(ÄÄÄÄÄÄÄÄÄÄÄÄåÅçÀß•ïÃÅ¡Ö»Å±îÅô±’‡Å°•Õ—Ω…•≈’îÅπîÅëΩ•Ÿïπ–Å¡±’ÃÉ©—…îÅï·¡Ωœ•ïÃÅπ§(ÄÄÄÄÄÄÄÄÄÄÄÄåÅçΩµ¡”•ïÃÅçΩµµîÅëïÃÅ…ï±ÖπçïÃ∏(ÄÄÄÄÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçÖπçï±±ïë}ëÖ—îÄÙÅ}ç…µ}…ï±Öπçï}ëÖ—î°•—ï¥πùï–†âÕç°ïë’±ïë}ëÖ—îà§§(ÄÄÄÄÄÄÄÄÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçÖπçï±±ïë}ëÖ—îÄÙÄàà(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅçÖπçï±±ïë}ëÖ—îË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçÖπçï±±ïë}ëÖ—ïÃπÖëê°çÖπçï±±ïë}ëÖ—î§(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÕç°ïë’±ïë}ëÖ—îÄÙÅ}ç…µ}…ï±Öπçï}ëÖ—î°•—ï¥πùï–†âÕç°ïë’±ïë}ëÖ—îà§§(ÄÄÄÄÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÕç°ïë’±ïë}ëÖ—îÄÙÄàà(ÄÄÄÄÄÄÄÅ•òÅ•—ï¥πùï–†âÕç°ïë’±ïë}ëÖ—îà§ÄÑÙÅÕç°ïë’±ïë}ëÖ—îË(ÄÄÄÄÄÄÄÄÄÄÄÅ•—ïµlâÕç°ïë’±ïë}ëÖ—îâtÄÙÅÕç°ïë’±ïë}ëÖ—î(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ•òÄâµΩ—•òàÅ•∏Å•—ï¥Ë(ÄÄÄÄÄÄÄÄÄÄÄÄåÅ1ïÃÅëΩπª•ïÃÅ°•Õ—Ω…•≈’ïÃÅ…ïÕ—ïπ–Å±•Õ•â±ïÃÅ∑©µîÅÕ§Åï±±ïÃÅΩπ–É•”§(ÄÄÄÄÄÄÄÄÄÄÄÄåÉ•ç…•—ïÃÅÖŸÖπ–Å∞ùÖ©Ω’–ÅëîÅ±ÑÅŸÖ±•ëÖ—•Ω∏Åè—”§ÅA$∏(ÄÄÄÄÄÄÄÄÄÄÄÅµΩ—•òÄÙÄàÄàπ©Ω•∏°Õ—»°•—ï¥πùï–†âµΩ—•òà§ÅΩ»Äàà§πÕ¡±•–†§•l(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÈI5}I19}5=Q%}5a}19Q (ÄÄÄÄÄÄÄÄÄÄÄÅt(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ•—ï¥πùï–†âµΩ—•òà§ÄÑÙÅµΩ—•òË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•—ïµlâµΩ—•òâtÄÙÅµΩ—•ò(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ•òÄâç…ïÖ—ïë}Ö–àÅπΩ–Å•∏Å•—ï¥Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ•—ïµlâç…ïÖ—ïë}Ö–âtÄÙÅçΩπ—Öç–πùï–†â’¡ëÖ—ïë}Ö–à§ÅΩ»ÅçΩπ—Öç–πùï–†âç…ïÖ—ïë}Ö–à§ÅΩ»Å}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ•òÄâç…ïÖ—ïë}â‰àÅπΩ–Å•∏Å•—ï¥Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ•—ïµlâç…ïÖ—ïë}â‰âtÄÙÄã%≈’•¡îÅ%π”•ù…Ö±îà(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅπΩ…µÖ±•ÈïêπÖ¡¡ïπê°•—ï¥§((ÄÄÄÅ•òÅçΩπ—Öç–πùï–†â…ï±ÖπçïÃà§ÄÑÙÅπΩ…µÖ±•ÈïêË(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅçΩπ—Öç—lâ…ï±ÖπçïÃâtÄÙÅπΩ…µÖ±•Èïê((ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ±ïùÖçÂ}ëÖ—îÄÙÅ}ç…µ}…ï±Öπçï}ëÖ—î°çΩπ—Öç–πùï–†â…ï±Öπçï}ëÖ—îà§§(ÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»Ë(ÄÄÄÄÄÄÄÅ±ïùÖçÂ}ëÖ—îÄÙÄàà(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâ…ï±Öπçï}ëÖ—îâtÄÙÄàà(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅ•òÅ±ïùÖçÂ}ëÖ—îÅÖπêÅ±ïùÖçÂ}ëÖ—îÅπΩ–Å•∏ÅçÖπçï±±ïë}ëÖ—ïÃÅÖπêÅπΩ–ÅÖπ‰†(ÄÄÄÄÄÄÄÅ•—ï¥πùï–†âÕ—Ö—’Ãà§ÄÙÙÄâÕç°ïë’±ïêàÅÖπêÅ•—ï¥πùï–†âÕç°ïë’±ïë}ëÖ—îà§ÄÙÙÅ±ïùÖçÂ}ëÖ—î(ÄÄÄÄÄÄÄÅôΩ»Å•—ï¥Å•∏ÅπΩ…µÖ±•Èïê(ÄÄÄÄ§Ë(ÄÄÄÄÄÄÄÅπΩ…µÖ±•Èïêπ•πÕï…–†¿∞ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ•êàËÅÕ—»°’’•êπ’’•ê–†§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕç°ïë’±ïë}ëÖ—îàËÅ±ïùÖçÂ}ëÖ—î∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö—’ÃàËÄâÕç°ïë’±ïêà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâç…ïÖ—ïë}Ö–àËÅçΩπ—Öç–πùï–†â’¡ëÖ—ïë}Ö–à§ÅΩ»ÅçΩπ—Öç–πùï–†âç…ïÖ—ïë}Ö–à§ÅΩ»Å}ç…µ}πΩ‹†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâç…ïÖ—ïë}â‰àËÄâ!•Õ—Ω…•≈’îÅI4à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕΩ’…çîàËÄâ±ïùÖç‰à∞(ÄÄÄÄÄÄÄÅÙ§(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î((ÄÄÄÅÖç—•ŸîÄÙÅl(ÄÄÄÄÄÄÄÄ°•πëï‡∞Å•—ï¥§ÅôΩ»Å•πëï‡∞Å•—ï¥Å•∏Åïπ’µï…Ö—î°πΩ…µÖ±•Èïê§(ÄÄÄÄÄÄÄÅ•òÅ•—ï¥πùï–†âÕ—Ö—’Ãà§ÄÙÙÄâÕç°ïë’±ïêà(ÄÄÄÅt(ÄÄÄÅ•òÅ±ï∏°Öç—•Ÿî§Ä¯ÄƒË(ÄÄÄÄÄÄÄÅëÖ—ïêÄÙÅl(ÄÄÄÄÄÄÄÄÄÄÄÄ°•πëï‡∞Å•—ï¥§ÅôΩ»Å•πëï‡∞Å•—ï¥Å•∏ÅÖç—•Ÿî(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ•—ï¥πùï–†âÕç°ïë’±ïë}ëÖ—îà§(ÄÄÄÄÄÄÄÅt(ÄÄÄÄÄÄÄÅ|∞ÅπïÖ…ïÕ–ÄÙÅµ•∏†(ÄÄÄÄÄÄÄÄÄÄÄÅëÖ—ïêÅΩ»ÅÖç—•Ÿî∞(ÄÄÄÄÄÄÄÄÄÄÄÅ≠ï‰ı±ÖµâëÑÅïπ—…‰ËÄ°ïπ—…Âl≈tπùï–†âÕç°ïë’±ïë}ëÖ—îà§ÅΩ»Äàà∞Åïπ—…Âl¡t§∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅçΩµ¡±ï—ïë}Ö–ÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÅôΩ»Å|∞Å•—ï¥Å•∏ÅÖç—•ŸîË(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ•—ï¥Å•ÃÅπïÖ…ïÕ–Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÄÄÄÄÅ•—ï¥π’¡ëÖ—î°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö—’ÃàËÄâ…ï¡…Ωù…Öµµïêà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâçΩµ¡±ï—ïë}Ö–àËÅçΩµ¡±ï—ïë}Ö–∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâçΩµ¡±ï—ïë}â‰àËÄâ’—ΩµÖ—•ÕÖ—•Ω∏ÅI4à∞(ÄÄÄÄÄÄÄÄÄÄÄÅÙ§(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î((ÄÄÄÅ•òÅ}ç…µ}…ïô…ïÕ°}…ï±Öπçï}ëÖ—î°çΩπ—Öç–§Ë(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅ…ï—’…∏Åç°Öπùïê(()ëïòÅ}ç…µ}Õç°ïë’±ï}…ï±Öπçî†(ÄÄÄÄÄÄÄÅçΩπ—Öç–∞ÅÕç°ïë’±ïë}ëÖ—î∞Ä®∞ÅÕΩ’…çîÙâµÖπ’Ö∞à∞Å¡Ö…ïπ—}…ï±Öπçï}•êı9Ωπî∞(ÄÄÄÄÄÄÄÅÖç—Ω…}πÖµîı9Ωπî∞ÅµΩ—•òı9Ωπî§Ë(ÄÄÄÄààâMç°ïë’±îÅΩ»Å…ïµΩŸîÅΩ¡ï∏ÅÖç—•ΩπÃÅ›°•±îÅ¡…ïÕï…Ÿ•πúÅçΩµ¡±ï—ïêÅÖ——ïµ¡—Ã∏ààà(ÄÄÄÅÕç°ïë’±ïë}ëÖ—îÄÙÅ}ç…µ}…ï±Öπçï}ëÖ—î°Õç°ïë’±ïë}ëÖ—î§(ÄÄÄÅ•òÅπΩ–ÅÕç°ïë’±ïë}ëÖ—îË(ÄÄÄÄÄÄÄÄåÅ0ùÖππ’±Ö—•Ω∏Åï·¡±•ç•—îÅëΩ•–ÅÕ’¡¡…•µï»Å—Ω’—ïÃÅ±ïÃÅ…ï±ÖπçïÃÅ≈’§É•—Ö•ïπ–(ÄÄÄÄÄÄÄÄåÅïπçΩ…îÅΩ’Ÿï…—ïÃÅÖŸÖπ–Å≈’îÅ±îÅπΩ…µÖ±•Õï’»Å∏ù°•Õ—Ω…•ÕîÅ±ïÃÅëΩ’â±ΩπÃ∏(ÄÄÄÄÄÄÄÅ…Ö›}…ï±ÖπçïÃÄÙÅçΩπ—Öç–πùï–†â…ï±ÖπçïÃà§(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅπΩ–Å•Õ•πÕ—Öπçî°…Ö›}…ï±ÖπçïÃ∞Å±•Õ–§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°…Ö›}…ï±ÖπçïÃ∞Å±•Õ–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö›}…ï±ÖπçïÃÄÙÅmt(ÄÄÄÄÄÄÄÅ…ïµÖ•π•πúÄÙÅl(ÄÄÄÄÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏Å…Ö›}…ï±ÖπçïÃ(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–Ä†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•Õ•πÕ—Öπçî°•—ï¥∞Åë•ç–§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅÕ—»°•—ï¥πùï–†âÕ—Ö—’Ãà§ÅΩ»ÄâÕç°ïë’±ïêà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÙÙÄâÕç°ïë’±ïêà(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅt(ÄÄÄÄÄÄÄÅ•òÅ…ïµÖ•π•πúÄÑÙÅ…Ö›}…ï±ÖπçïÃË(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâ…ï±ÖπçïÃâtÄÙÅ…ïµÖ•π•πú(ÄÄÄÄÄÄÄÅ•òÅÕ—»°çΩπ—Öç–πùï–†â…ï±Öπçï}ëÖ—îà§ÅΩ»Äàà§πÕ—…•¿†§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâ…ï±Öπçï}ëÖ—îâtÄÙÄàà(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ•òÅ}ç…µ}ïπÕ’…ï}…ï±ÖπçïÃ°çΩπ—Öç–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî∞Åç°Öπùïê((ÄÄÄÅπΩ…µÖ±•Èïë}µΩ—•òÄÙÅ9ΩπîÅ•òÅµΩ—•òÅ•ÃÅ9ΩπîÅï±ÕîÅ}ç…µ}…ï±Öπçï}µΩ—•ò°µΩ—•ò§(ÄÄÄÅç°ÖπùïêÄÙÅ}ç…µ}ïπÕ’…ï}…ï±ÖπçïÃ°çΩπ—Öç–§(ÄÄÄÅπΩ‹ÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅÖç—Ω…}πÖµîÄÙÅÖç—Ω…}πÖµîÅΩ»Ä°ç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ§πùï–†(ÄÄÄÄÄÄÄÄâπÖµîà∞Äã%≈’•¡îÅ%π”•ù…Ö±îà(ÄÄÄÄ§(ÄÄÄÅÖç—•ŸîÄÙÅl(ÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅçΩπ—Öç—lâ…ï±ÖπçïÃât(ÄÄÄÄÄÄÄÅ•òÅ•—ï¥πùï–†âÕ—Ö—’Ãà§ÄÙÙÄâÕç°ïë’±ïêà(ÄÄÄÅt((ÄÄÄÅÕÖµîÄÙÅπï·–†°•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅÖç—•ŸîÅ•òÅ•—ï¥πùï–†âÕç°ïë’±ïë}ëÖ—îà§ÄÙÙÅÕç°ïë’±ïë}ëÖ—î§∞Å9Ωπî§(ÄÄÄÅôΩ»Å•—ï¥Å•∏ÅÖç—•ŸîË(ÄÄÄÄÄÄÄÅ•òÅ•—ï¥Å•ÃÅÕÖµîË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ•—ï¥π’¡ëÖ—î°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö—’ÃàËÄâ…ï¡…Ωù…Öµµïêà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩµ¡±ï—ïë}Ö–àËÅπΩ‹∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩµ¡±ï—ïë}â‰àËÅÖç—Ω…}πÖµî∞(ÄÄÄÄÄÄÄÅÙ§(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î((ÄÄÄÅ•òÅÕÖµîÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅÕÖµîÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ•êàËÅÕ—»°’’•êπ’’•ê–†§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕç°ïë’±ïë}ëÖ—îàËÅÕç°ïë’±ïë}ëÖ—î∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö—’ÃàËÄâÕç°ïë’±ïêà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâç…ïÖ—ïë}Ö–àËÅπΩ‹∞(ÄÄÄÄÄÄÄÄÄÄÄÄâç…ïÖ—ïë}â‰àËÅÖç—Ω…}πÖµî∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕΩ’…çîàËÅÕΩ’…çî∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÅ•òÅπΩ…µÖ±•Èïë}µΩ—•òË(ÄÄÄÄÄÄÄÄÄÄÄÅÕÖµïlâµΩ—•òâtÄÙÅπΩ…µÖ±•Èïë}µΩ—•ò(ÄÄÄÄÄÄÄÅ•òÅ¡Ö…ïπ—}…ï±Öπçï}•êË(ÄÄÄÄÄÄÄÄÄÄÄÅÕÖµïlâ¡Ö…ïπ—}…ï±Öπçï}•êâtÄÙÅ¡Ö…ïπ—}…ï±Öπçï}•ê(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâ…ï±ÖπçïÃâtπ•πÕï…–†¿∞ÅÕÖµî§(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅï±•òÅπΩ…µÖ±•Èïë}µΩ—•òÅ•ÃÅπΩ–Å9ΩπîÅÖπêÅÕÖµîπùï–†âµΩ—•òà∞Äàà§ÄÑÙÅπΩ…µÖ±•Èïë}µΩ—•òË(ÄÄÄÄÄÄÄÅ•òÅπΩ…µÖ±•Èïë}µΩ—•òË(ÄÄÄÄÄÄÄÄÄÄÄÅÕÖµïlâµΩ—•òâtÄÙÅπΩ…µÖ±•Èïë}µΩ—•ò(ÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÅÕÖµîπ¡Ω¿†âµΩ—•òà∞Å9Ωπî§(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î((ÄÄÄÅ•òÅ}ç…µ}…ïô…ïÕ°}…ï±Öπçï}ëÖ—î°çΩπ—Öç–§Ë(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅ…ï—’…∏ÅÕÖµî∞Åç°Öπùïê(()ëïòÅ}ç…µ}Õç°ïë’±ï}ô—}…ïô’ÕÖ±}…ï±Öπçî†(ÄÄÄÄÄÄÄÅçΩπ—Öç–∞Ä®∞ÅÕΩ’…çî∞ÅÕ—Öâ±ï}•êÙàà∞ÅπΩ‹ı9Ωπî§Ë(ÄÄÄÄààâA±Öπ•ô•îÅ±ÑÅ…ï±ÖπçîÅΩ’ŸÀ•îÅÕ’•ŸÖπ–Å’∏ÅπΩ’ŸïÖ‘Å…ïô’ÃÅ…ÖπçîÅQ…ÖŸÖ•∞∏ààà(ÄÄÄÅ•òÅçΩπ—Öç–πùï–†âÕ—Ö—’–à§Å•∏ÅÏâΩπŸï…—§à∞Äâ•Õ≈’Ö±•ôß§âÙË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî∞ÅÖ±Õî((ÄÄÄÅ¡Ö…•ÃÄÙÅ¡Â—Ëπ—•µïÈΩπî†â’…Ω¡îΩAÖ…•Ãà§(ÄÄÄÅç’……ïπ–ÄÙÅπΩ‹ÅΩ»ÅëÖ—ï—•µîπëÖ—ï—•µîππΩ‹°¡Ö…•Ã§(ÄÄÄÅ•òÅç’……ïπ–π—È•πôºÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅç’……ïπ–ÄÙÅ¡Ö…•Ãπ±ΩçÖ±•Èî°ç’……ïπ–§(ÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÅç’……ïπ–ÄÙÅç’……ïπ–πÖÕ—•µïÈΩπî°¡Ö…•Ã§(ÄÄÄÅÕç°ïë’±ïë}ëÖ‰ÄÙÅç’……ïπ–πëÖ—î†§(ÄÄÄÅ•òÅÕç°ïë’±ïë}ëÖ‰π›ïï≠ëÖ‰†§Ä¯ÙÄ‘Ë(ÄÄÄÄÄÄÄÅÕç°ïë’±ïë}ëÖ‰Ä¨ÙÅëÖ—ï—•µîπ—•µïëï±—Ñ°ëÖÂÃÙ‹Ä¥ÅÕç°ïë’±ïë}ëÖ‰π›ïï≠ëÖ‰†§§(ÄÄÄÅÕç°ïë’±ïë}ëÖ—îÄÙÅÕç°ïë’±ïë}ëÖ‰π•ÕΩôΩ…µÖ–†§(ÄÄÄÅÖç—Ω…}πÖµîÄÙÄâ…ÖπçîÅQ…ÖŸÖ•∞àÅ•òÅÕΩ’…çîÄÙÙÄâ›ïëΩô}ô—}…ïô’ÕÖ∞àÅï±ÕîÅ9Ωπî(ÄÄÄÅ¡±Öππïê∞Åç°ÖπùïêÄÙÅ}ç…µ}Õç°ïë’±ï}…ï±Öπçî†(ÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÅÕç°ïë’±ïë}ëÖ—î∞(ÄÄÄÄÄÄÄÅÕΩ’…çîıÕΩ’…çî∞(ÄÄÄÄÄÄÄÅÖç—Ω…}πÖµîıÖç—Ω…}πÖµî∞(ÄÄÄÄÄÄÄÅµΩ—•òÙâM’•—îÅ…ïô’ÃÅPà∞(ÄÄÄÄ§((ÄÄÄÅµï—ÖëÖ—ÑÄÙÅÏ(ÄÄÄÄÄÄÄÄâô’πë•πù}…ïô’ÕÖ±}ÕΩ’…çîàËÅÕΩ’…çî∞(ÄÄÄÅÙ(ÄÄÄÅ•òÅÕ—Öâ±ï}•êË(ÄÄÄÄÄÄÄÅµï—ÖëÖ—ÖlâÕΩ’…çï}›ïëΩô}ôΩ±ëï…}•êâtÄÙÅÕ—»°Õ—Öâ±ï}•ê§(ÄÄÄÅôΩ»Å≠ï‰∞ÅŸÖ±’îÅ•∏Åµï—ÖëÖ—Ñπ•—ïµÃ†§Ë(ÄÄÄÄÄÄÄÅ•òÅ¡±Öππïêπùï–°≠ï‰§ÄÑÙÅŸÖ±’îË(ÄÄÄÄÄÄÄÄÄÄÄÅ¡±Öππïëm≠ïÂtÄÙÅŸÖ±’î(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î((ÄÄÄÅ•òÅçΩπ—Öç–πùï–†âÕ—Ö—’–à§ÄÑÙÄâÅ…ï±Öπçï»àË(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâÕ—Ö—’–âtÄÙÄâÅ…ï±Öπçï»à(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î((ÄÄÄÅ•òÅç°ÖπùïêË(ÄÄÄÄÄÄÄÅë•Õ¡±ÖÂ}ëÖ—îÄÙÅÕç°ïë’±ïë}ëÖ‰πÕ—…ô—•µî†àïêºï¥ºïdà§(ÄÄÄÄÄÄÄÅëï—Ö•∞ÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄâ•πÖπçïµïπ–Å…ÖπçîÅQ…ÖŸÖ•∞Å…ïô’œ§∏Äà(ÄÄÄÄÄÄÄÄÄÄÄÅòâIï±ÖπçîÅ¡À•Ÿ’îÅ±îÅÌë•Õ¡±ÖÂ}ëÖ—ïÙ∏à(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ•òÅÕ—Öâ±ï}•êË(ÄÄÄÄÄÄÄÄÄÄÄÅëï—Ö•∞Ä¨ÙÅòàÅΩÕÕ•ï»Å]=ÄËÅÌÕ—Öâ±ï}•ëÙ∏à(ÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ï±Öπçîà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâIï±ÖπçîÅ…ÖπçîÅQ…ÖŸÖ•∞Å¡±Öπ•ôß•îà∞(ÄÄÄÄÄÄÄÄÄÄÄÅëï—Ö•∞∞(ÄÄÄÄÄÄÄÄÄÄÄÅÖ’—°Ω…}πÖµîıÖç—Ω…}πÖµî∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅ…ï—’…∏Å¡±Öππïê∞Åç°Öπùïê(()ëïòÅ}ç…µ}çΩµ¡±ï—ï}…ï±Öπçî°çΩπ—Öç–∞Å…ï±Öπçî∞ÅÕ—Ö—’Ã∞Ä®∞ÅπΩ—îÙàà§Ë(ÄÄÄÄààâ±ΩÕîÅÑÅÕç°ïë’±ïêÅôΩ±±Ω‹µ’¿Åï·Öç—±‰ÅΩπçîÅÖπêÅ…ïô…ïÕ†Å—°îÅπï·–ÅÖç—•Ω∏∏ààà(ÄÄÄÅ•òÅÕ—Ö—’ÃÅπΩ–Å•∏ÅÏâÖπÕ›ï…ïêà∞ÄâπΩ}ÖπÕ›ï»âÙË(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†âK•Õ’±—Ö–ÅëîÅ…ï±ÖπçîÅ•πŸÖ±•ëî∏à§(ÄÄÄÅ•òÅ…ï±Öπçîπùï–†âÕ—Ö—’Ãà§ÄÑÙÄâÕç°ïë’±ïêàË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅ…ï±Öπçîπ’¡ëÖ—î°Ï(ÄÄÄÄÄÄÄÄâÕ—Ö—’ÃàËÅÕ—Ö—’Ã∞(ÄÄÄÄÄÄÄÄâçΩµ¡±ï—ïë}Ö–àËÅ}ç…µ}πΩ‹†§∞(ÄÄÄÄÄÄÄÄâçΩµ¡±ï—ïë}â‰àËÄ°ç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ§πùï–†âπÖµîà∞Äã%≈’•¡îÅ%π”•ù…Ö±îà§∞(ÄÄÄÅÙ§(ÄÄÄÅ•òÅπΩ—îË(ÄÄÄÄÄÄÄÅ…ï±ÖπçïlâπΩ—îâtÄÙÅπΩ—î(ÄÄÄÅ}ç…µ}…ïô…ïÕ°}…ï±Öπçï}ëÖ—î°çΩπ—Öç–§(ÄÄÄÅ…ï—’…∏ÅQ…’î(()ëïòÅ}ç…µ}ëï±ï—ï}…ï±Öπçî°çΩπ—Öç–∞Å…ï±Öπçî§Ë(ÄÄÄÄààâAï…µÖπïπ—±‰Å…ïµΩŸîÅΩπîÅ¡±ÖππïêÅôΩ±±Ω‹µ’¿Åô…Ω¥Å—°•ÃÅçΩπ—Öç–∏ààà(ÄÄÄÅ•òÅ…ï±Öπçîπùï–†âÕ—Ö—’Ãà§ÄÑÙÄâÕç°ïë’±ïêàË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅ…ï±ÖπçïÃÄÙÅçΩπ—Öç–πùï–†â…ï±ÖπçïÃà§(ÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°…ï±ÖπçïÃ∞Å±•Õ–§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅôΩ»Å•πëï‡∞Å•—ï¥Å•∏Åïπ’µï…Ö—î°…ï±ÖπçïÃ§Ë(ÄÄÄÄÄÄÄÅ•òÅ•—ï¥Å•ÃÅ…ï±ÖπçîË(ÄÄÄÄÄÄÄÄÄÄÄÅëï∞Å…ï±ÖπçïÕm•πëï·t(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}…ïô…ïÕ°}…ï±Öπçï}ëÖ—î°çΩπ—Öç–§(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅQ…’î(ÄÄÄÅ…ï—’…∏ÅÖ±Õî(()ëïòÅ}ç…µ}ç…ïÖ—ï}çΩπ—Öç—}ô…Ωµ}•πôΩ…µÖ—•Ωπ}…ï≈’ïÕ–°ëÖ—Ñ∞Åô•ï±ëÃ∞ÅëïµÖπëï}•ê∞ÅëïŸ•Õ}•ê∞ÅëïŸ•Õ}’…∞§Ë(ÄÄÄÄààâÀ•îÅ±ÑÅô•ç°îÅI4ÅçΩµ¡≥°—îÅï–ÅÕΩ∏Å©Ω’…πÖ∞Å±Ω…ÃÅêù’πîÅëïµÖπëîÅêù•πôΩ…µÖ—•ΩπÃ∏ààà(ÄÄÄÅπΩ‹ÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅ•Õ}Õïç…ï—Ö…•Ö–ÄÙÅÕ—»°ô•ï±ëÃπùï–†âÕΩ’…çï}Õïç…ï—Ö…•Ö–à§ÅΩ»Äàà§ÄÙÙÄàƒà(ÄÄÄÅùΩΩù±ï}ÖëÕ}—…Öç≠•πúÄÙÄ†(ÄÄÄÄÄÄÄÅÌ≠ï‰ËÄààÅôΩ»Å≠ï‰Å•∏ÅI5}==1}M}QI-%9}-eMÙ(ÄÄÄÄÄÄÄÅ•òÅ•Õ}Õïç…ï—Ö…•Ö–(ÄÄÄÄÄÄÄÅï±ÕîÅ}ç…µ}ùΩΩù±ï}ÖëÕ}—…Öç≠•πù}ô•ï±ëÃ°ô•ï±ëÃ§(ÄÄÄÄ§(ÄÄÄÅùΩΩù±ï}ÖëÕ}•ëïπ—•ô•ï…}—Â¡î∞ÅùΩΩù±ï}ÖëÕ}•ëïπ—•ô•ï»ÄÙÄ†(ÄÄÄÄÄÄÄÄ†àà∞Äàà§(ÄÄÄÄÄÄÄÅ•òÅ•Õ}Õïç…ï—Ö…•Ö–(ÄÄÄÄÄÄÄÅï±ÕîÅ}ç…µ}•πôΩ…µÖ—•Ωπ}…ï≈’ïÕ—}ùΩΩù±ï}ÖëÕ}•ëïπ—•ô•ï»°ô•ï±ëÃ§(ÄÄÄÄ§(ÄÄÄÅôΩ…µÖ—•Ωπ}≠ï‰ÄÙÅÕ—»°ô•ï±ëÃπùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅôΩ…µÖ—•Ω∏ÄÙÅÏ(ÄÄÄÄÄÄÄÄâMA}%9%PàËÄâM@à∞ÄâMA}YàËÄâM@à∞ÄâMM%@àËÄâMM%@Äƒà∞(ÄÄÄÄÄÄÄÄâYQàËÄâ°Ö’ôôï’»ÅYQà∞(ÄÄÄÅÙπùï–°ôΩ…µÖ—•Ωπ}≠ï‰∞ÅôΩ…µÖ—•Ωπ}≠ï‰§(ÄÄÄÅ±•ï‘ÄÙÅÏ(ÄÄÄÄÄÄÄÄâ¡Ö…•ÃàËÄâAÖ…•Ãà∞ÄâçΩ—ï}ÖÈ’»àËÄâ——îÅìäeÈ’»à∞ÄâÖ’Ÿï…ùπîàËÄâ’Ÿï…ùπîà∞(ÄÄÄÅÙπùï–°Õ—»°ô•ï±ëÃπùï–†âçïπ—…îà§ÅΩ»Äàà§πÕ—…•¿†§∞ÅÕ—»°ô•ï±ëÃπùï–†âçïπ—…îà§ÅΩ»Äàà§πÕ—…•¿†§§(ÄÄÄÅçΩπ—Öç–ÄÙÅÏ(ÄÄÄÄÄÄÄÄâ•êàËÅÕ—»°’’•êπ’’•ê–†§§∞(ÄÄÄÄÄÄÄÄâ¡…ïπΩ¥àËÅ}ç…µ}ôΩ…µÖ—}ô•…Õ—}πÖµî°ô•ï±ëÃπùï–†â¡…ïπΩ¥à§§∞(ÄÄÄÄÄÄÄÄâπΩ¥àËÅ}ç…µ}ôΩ…µÖ—}±ÖÕ—}πÖµî°ô•ï±ëÃπùï–†âπΩ¥à§§∞(ÄÄÄÄÄÄÄÄâ—ï±ï¡°ΩπîàËÅÕ—»°ô•ï±ëÃπùï–†â—ï±ï¡°Ωπîà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâµÖ•∞àËÅÕ—»°ô•ï±ëÃπùï–†âµÖ•∞à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏àËÅôΩ…µÖ—•Ω∏∞(ÄÄÄÄÄÄÄÄâ±•ï‘àËÅ±•ï‘∞(ÄÄÄÄÄÄÄÄâÕ—Ö—’–àËÄâ9Ω’ŸïÖ’‡à∞(ÄÄÄÄÄÄÄÄâëÖ—ïÕ}ôΩ…µÖ—•Ω∏àËÅÕ—»°ô•ï±ëÃπùï–†âëÖ—ïÃà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâç¡òàËÅÕ—»°ô•ï±ëÃπùï–†âç¡ô}çΩπÕ’±—îà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâç¡ô}µΩπ—Öπ–àËÅπΩ…µÖ±•Èï}ç¡ô}ÖµΩ’π–°ô•ï±ëÃπùï–†âç¡ô}µΩπ—Öπ–à§§∞(ÄÄÄÄÄÄÄÄâçÖ…—ï}¡…ºàËÅÕ—»°ô•ï±ëÃπùï–†âçπÖ¡Õ}Ω¨à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâ—•—…ï}Õï©Ω’»àËÅÕ—»°ô•ï±ëÃπùï–†â—•—…ï}Õï©Ω’»à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâùÖ…ëï}Ÿ’îàËÅÕ—»°ô•ï±ëÃπùï–†âùÖ…ëï}Ÿ’îà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâÖπ—ïçïëïπ—ÃàËÅÕ—»°ô•ï±ëÃπùï–†âùÖ…ëï}Ÿ’îà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâëïÕ¡}—Â¡îàËÄâYàÅ•òÅôΩ…µÖ—•Ωπ}≠ï‰ÄÙÙÄâMA}YàÅï±ÕîÄ†â%9%Q%0àÅ•òÅôΩ…µÖ—•Ωπ}≠ï‰ÄÙÙÄâMA}%9%PàÅï±ÕîÄàà§∞(ÄÄÄÄÄÄÄÄâ•ëïπ—•—ï}ç…ïÖ—•Ω∏àËÅÕ—»°ô•ï±ëÃπùï–†â•ëïπ—•—ï}π’µï…•≈’îà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄåÅ1îÅôΩ…µ’±Ö•…îÅ¡’â±•åÅçΩπô•…µîÅ’π•≈’ïµïπ–Å±ÑÅì•µÖ…ç°îÅëîÅçÀ•Ö—•Ω∏ÄÏÅÕΩ∏(ÄÄÄÄÄÄÄÄåÅôΩπç—•Ωππïµïπ–Åï–Å∞ù•πÕç…•¡—•Ω∏ÅPÅÕΩπ–ÅëïÃÅôÖ•—ÃÅë•Õ—•πç—ÃÉÄÅ€•…•ô•ï»∏(ÄÄÄÄÄÄÄÄâ•ëïπ—•—ï}Ω¨àËÄàà∞(ÄÄÄÄÄÄÄÄâô•πÖπçïµïπ—}ô–àËÅÕ—»°ô•ï±ëÃπùï–†âô…Öπçï}—…ÖŸÖ•∞à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–àËÄàà∞ÄâµΩπ—Öπ—}ÖççΩ…ëï}ô–àËÄàà∞(ÄÄÄÄÄÄÄÄâô•πÖπçïµïπ—}¡ï…ÕΩ}¡ΩÕÕ•â±îàËÅÕ—»°ô•ï±ëÃπùï–†âô—}…ïô’Õ}Ω¨à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâ…ïô’Õ}ô—}¡ï…ÕºàËÅÕ—»°ô•ï±ëÃπùï–†âô—}…ïô’Õ}Ω¨à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâ…ïÕ—ï}Ö}ç°Ö…ùï}¡ï…ÕºàËÄàà∞(ÄÄÄÄÄÄÄÄâΩ…•ù•πîàËÅ}ç…µ}•πôΩ…µÖ—•Ωπ}…ï≈’ïÕ—}Ω…•ù•∏°ô•ï±ëÃ§∞(ÄÄÄÄÄÄÄÄâùç±•êàËÅùΩΩù±ï}ÖëÕ}—…Öç≠•πùlâùç±•êât∞(ÄÄÄÄÄÄÄÄâ›â…Ö•êàËÅùΩΩù±ï}ÖëÕ}—…Öç≠•πùlâ›â…Ö•êât∞(ÄÄÄÄÄÄÄÄâùâ…Ö•êàËÅùΩΩù±ï}ÖëÕ}—…Öç≠•πùlâùâ…Ö•êât∞(ÄÄÄÄÄÄÄÄâùΩΩù±ï}ÖëÕ}•ëïπ—•ô•ï»àËÅùΩΩù±ï}ÖëÕ}•ëïπ—•ô•ï»∞(ÄÄÄÄÄÄÄÄâùΩΩù±ï}ÖëÕ}•ëïπ—•ô•ï…}—Â¡îàËÅùΩΩù±ï}ÖëÕ}•ëïπ—•ô•ï…}—Â¡î∞(ÄÄÄÄÄÄÄÄâ•πÕç…•—}ô–àËÄàà∞(ÄÄÄÄÄÄÄÄâçΩµµïπ—Ö•…ïÃàËÄàà∞(ÄÄÄÄÄÄÄÄâ…ï±Öπçï}ëÖ—îàËÄàà∞(ÄÄÄÄÄÄÄÄâç…ïÖ—ïë}Ö–àËÅπΩ‹∞(ÄÄÄÄÄÄÄÄâ’¡ëÖ—ïë}Ö–àËÅπΩ‹∞(ÄÄÄÄÄÄÄÄâÖç—•Ÿ•—•ïÃàËÅmt∞(ÄÄÄÄÄÄÄÄâÕΩ’…çîàËÄâëïµÖπëï}•πôΩÕ}ôΩ…µÖ—•ΩπÃà∞(ÄÄÄÄÄÄÄÄâÕΩ’…çï}ëïµÖπëï}•êàËÅëïµÖπëï}•ê∞(ÄÄÄÄÄÄÄÄâÕΩ’…çï}ëïŸ•Õ}•êàËÅëïŸ•Õ}•ê∞(ÄÄÄÄÄÄÄÄâôΩ…µ’±Ö•…îàËÅë•ç–°ô•ï±ëÃ§∞(ÄÄÄÅÙ(ÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞Äâç…ïÖ—•Ω∏à∞ÄâΩ…µ’±Ö•…îÅëîÅëïµÖπëîÅìäe•πôΩ…µÖ—•ΩπÃÅçΩµ¡≥•”§à∞Äâ•ç°îÅçÀß•îÅÖ’—ΩµÖ—•≈’ïµïπ–ÅÖŸïåÅ±îÅÕ—Ö—’–Å9Ω’ŸïÖ‘à§(ÄÄÄÅ≈’Ω—ï}¡…ïŸ•ï‹ÄÙÄ†(ÄÄÄÄÄÄÄÅòúÒë•ÿÅÕ—Â±îÙâ¡Öëë•πúË»—¡‡à¯Ò†»˘ïŸ•ÃÅì•—Ö•±≥§Ω†»¯Ò¿˘ÌôΩ…µÖ—•Ω∏ÅΩ»ÄâΩ…µÖ—•Ω∏âÙΩ¿¯ú(ÄÄÄÄÄÄÄÅòúÒ¿¯ÒÑÅ°…ïòÙâÌëïŸ•Õ}’…±ÙàÅ—Ö…ùï–Ùâ}â±Öπ¨à˘=’Ÿ…•»Å±îÅëïŸ•ÃΩÑ¯Ω¿¯Ωë•ÿ¯ú(ÄÄÄÄ§(ÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞ÄâëïŸ•Ãà∞ÄâïŸ•ÃÅì•—Ö•±≥§ÅçÀß§à∞ÅòâïŸ•ÃÅª
¿ÅÌëïŸ•Õ}•ëÙà∞Å≈’Ω—ï}¡…ïŸ•ï‹§(ÄÄÄÅµÖ—ç°ïê∞Å|∞Å|ÄÙÅô•πë}Ω…}ç…ïÖ—ï}ç…µ}çΩπ—Öç–†(ÄÄÄÄÄÄÄÅëÖ—Ñ∞Åô•ï±ëÃ∞ÄâëïµÖπëï}•πôΩÕ}ôΩ…µÖ—•ΩπÃà∞Å¡…Ω¡ΩÕïë}çΩπ—Öç–ıçΩπ—Öç–∞(ÄÄÄÄÄÄÄÅï·—ï…πÖ±}•êıëïµÖπëï}•ê∞Åç…ïÖ—ï}Ωπ}Öµâ•ù’•—‰ıQ…’î∞(ÄÄÄÄ§(ÄÄÄÅ•òÅµÖ—ç°ïêË(ÄÄÄÄÄÄÄÅçΩµ¡±ï—ïë}Ö±…ïÖëÂ}…ïçΩ…ëïêÄÙÅµÖ—ç°ïêπùï–†âÕΩ’…çï}ëïµÖπëï}•êà§ÄÙÙÅëïµÖπëï}•ê(ÄÄÄÄÄÄÄÅÕÖôï}ô•ï±ëÃÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏à∞Äâ±•ï‘à∞ÄâëÖ—ïÕ}ôΩ…µÖ—•Ω∏à∞Äâç¡òà∞Äâç¡ô}µΩπ—Öπ–à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçÖ…—ï}¡…ºà∞Äâ—•—…ï}Õï©Ω’»à∞ÄâùÖ…ëï}Ÿ’îà∞ÄâÖπ—ïçïëïπ—Ãà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâëïÕ¡}—Â¡îà∞Äâ•ëïπ—•—ï}ç…ïÖ—•Ω∏à∞Äâô•πÖπçïµïπ—}ô–à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ïô’Õ}ô—}¡ï…Õºà∞Äâ…ïÕ—ï}Ö}ç°Ö…ùï}¡ï…Õºà∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅôΩ»Å≠ï‰Å•∏ÅÕÖôï}ô•ï±ëÃË(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ}ç…µ}•Õ}ïµ¡—‰°µÖ—ç°ïêπùï–°≠ï‰§§ÅÖπêÅπΩ–Å}ç…µ}•Õ}ïµ¡—‰°çΩπ—Öç–πùï–°≠ï‰§§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµÖ—ç°ïëm≠ïÂtÄÙÅçΩπ—Öç—m≠ïÂt(ÄÄÄÄÄÄÄÅ¡…ïÕï…Ÿï}ÖâÖπëΩπïë}Ω…•ù•∏ÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅµÖ—ç°ïêπùï–†âÕΩ’…çîà§ÄÙÙÅ	9=9}59}M=UI(ÄÄÄÄÄÄÄÄÄÄÄÅΩ»ÅÖπ‰†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•—ï¥πùï–†âÕΩ’…çîà§ÄÙÙÅ	9=9}59}M=UI(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅôΩ»Å•—ï¥Å•∏ÅµÖ—ç°ïêπùï–†âÕΩ’…çï}°•Õ—Ω…‰à∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅΩ…•ù•πÖ±}Ω…•ù•∏ÄÙÅµÖ—ç°ïêπùï–†âΩ…•ù•πîà§(ÄÄÄÄÄÄÄÅ}ç…µ}Ö¡¡±Â}•πôΩ…µÖ—•Ωπ}…ï≈’ïÕ—}Ö——…•â’—•Ω∏°µÖ—ç°ïê∞Åô•ï±ëÃ§(ÄÄÄÄÄÄÄÅ•òÅ¡…ïÕï…Ÿï}ÖâÖπëΩπïë}Ω…•ù•∏Ë(ÄÄÄÄÄÄÄÄÄÄÄÅµÖ—ç°ïëlâΩ…•ù•πîâtÄÙÅΩ…•ù•πÖ±}Ω…•ù•∏ÅΩ»Å	9=9}=I5}1	0(ÄÄÄÄÄÄÄÅ•òÅ¡…ïÕï…Ÿï}ÖâÖπëΩπïë}Ω…•ù•∏ÅÖπêÅπΩ–ÅçΩµ¡±ï—ïë}Ö±…ïÖëÂ}…ïçΩ…ëïêË(ÄÄÄÄÄÄÄÄÄÄÄÅµÖ—ç°ïêπÕï—ëïôÖ’±–†âÖç—•Ÿ•—•ïÃà∞Åmt§πï·—ïπê†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩ¡‰πëïï¡çΩ¡‰°çΩπ—Öç–πùï–†âÖç—•Ÿ•—•ïÃà§ÅΩ»Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅµÖ—ç°ïëlâÕΩ’…çï}ëïµÖπëï}•êâtÄÙÅëïµÖπëï}•ê(ÄÄÄÄÄÄÄÄÄÄÄÅµÖ—ç°ïëlâÕΩ’…çï}ëïŸ•Õ}•êâtÄÙÅëïŸ•Õ}•ê(ÄÄÄÄÄÄÄÄÄÄÄÅµÖ—ç°ïëlâôΩ…µ’±Ö•…îâtÄÙÅë•ç–°ô•ï±ëÃ§(ÄÄÄÄÄÄÄÄÄÄÄÅµÖ—ç°ïëlâ’¡ëÖ—ïë}Ö–âtÄÙÅπΩ‹(ÄÄÄÅ…ï—’…∏ÅµÖ—ç°ïê(()ëïòÅ}Õïç…ï—Ö…•Ö—}ÕïÕÕ•Ωπ}ëï—Ö•±Ã°ïπ—…‰§Ë(ÄÄÄÄààâIï—’…∏Å—°îÅI4Åçïπ—…îÅçΩëîÅÖπêÅëÖ—îÅ±Öâï∞ÅÕï±ïç—ïêÅâ‰Å—°îÅÕïç…ï—Ö…‰∏ààà(ÄÄÄÅ¡…ïôï……ïë}ÕïÕÕ•Ω∏ÄÙÅÕ—»°ïπ—…‰πùï–†âôΩ…µÖ—•Ωπ}ëÖ—ï}ÕΩ’°Ö•—ïîà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅÕïÕÕ•Ωπ}±Öâï∞ÄÙÅÕ—»°ïπ—…‰πùï–†âôΩ…µÖ—•Ωπ}ÕïÕÕ•Ωπ}±Öâï∞à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ…Ö›}çïπ—…îÄÙÅÕ—»°ïπ—…‰πùï–†âôΩ…µÖ—•Ωπ}çïπ—…îà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅçïπ—…ï}çΩëîÄÙÅ}πΩ…µÖ±•Èï}çïπ—…ï}çΩëî°…Ö›}çïπ—…î§Å•òÅ…Ö›}çïπ—…îÅï±ÕîÄàà((ÄÄÄÄåÅQ°îÅô’±∞∞ÅŸ•Õ•â±îÅç°Ω•çîÅ•ÃÅ—°îÅµΩÕ–Å…ï±•Öâ±îÅŸÖ±’îËÅ’π±•≠îÅëÖ—ÑÅÖ——…•â’—ïÃ∞(ÄÄÄÄåÅ•–Å•ÃÅÖ±ÕºÅ¡…ïÕïπ–Å•∏Å±ïùÖç‰ÅÕ’âµ•ÕÕ•ΩπÃ∏Å%–Å›•πÃÅ•òÅ—°îÅ—›ºÅŸÖ±’ïÃÅë•ÕÖù…ïî∏(ÄÄÄÅπΩ…µÖ±•Èïë}¡…ïôï…ïπçîÄÙÅ’π•çΩëïëÖ—ÑππΩ…µÖ±•Èî†â9-à∞Å¡…ïôï……ïë}ÕïÕÕ•Ω∏§(ÄÄÄÅπΩ…µÖ±•Èïë}¡…ïôï…ïπçîÄÙÄààπ©Ω•∏†(ÄÄÄÄÄÄÄÅç°Ö…Öç—ï»ÅôΩ»Åç°Ö…Öç—ï»Å•∏ÅπΩ…µÖ±•Èïë}¡…ïôï…ïπçî(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å’π•çΩëïëÖ—ÑπçΩµâ•π•πú°ç°Ö…Öç—ï»§(ÄÄÄÄ§πçÖÕïôΩ±ê†§(ÄÄÄÅπΩ…µÖ±•Èïë}¡…ïôï…ïπçîÄÙÅ…îπÕ’à°»âmyÑµË¿¥Ât¨à∞ÄàÄà∞ÅπΩ…µÖ±•Èïë}¡…ïôï…ïπçî§πÕ—…•¿†§(ÄÄÄÅ•òÄâçΩ—îÅêÅÖÈ’»àÅ•∏ÅπΩ…µÖ±•Èïë}¡…ïôï…ïπçîË(ÄÄÄÄÄÄÄÅçïπ—…ï}çΩëîÄÙÄâçΩ—ï}ÖÈ’»à(ÄÄÄÅï±•òÄâÖ’Ÿï…ùπîàÅ•∏ÅπΩ…µÖ±•Èïë}¡…ïôï…ïπçîË(ÄÄÄÄÄÄÄÅçïπ—…ï}çΩëîÄÙÄâÖ’Ÿï…ùπîà(ÄÄÄÅï±•òÄâ¡Ö…•ÃàÅ•∏ÅπΩ…µÖ±•Èïë}¡…ïôï…ïπçîË(ÄÄÄÄÄÄÄÅçïπ—…ï}çΩëîÄÙÄâ¡Ö…•Ãà((ÄÄÄÅ•òÅπΩ–ÅÕïÕÕ•Ωπ}±Öâï∞ÅÖπêÄàÉäPÄàÅ•∏Å¡…ïôï……ïë}ÕïÕÕ•Ω∏Ë(ÄÄÄÄÄÄÄÅÕïÕÕ•Ωπ}±Öâï∞ÄÙÅ¡…ïôï……ïë}ÕïÕÕ•Ω∏πÕ¡±•–†àÉäPÄà∞Äƒ•l≈tπÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–ÅÕïÕÕ•Ωπ}±Öâï∞Ë(ÄÄÄÄÄÄÄÅÕïÕÕ•Ωπ}±Öâï∞ÄÙÅ¡…ïôï……ïë}ÕïÕÕ•Ω∏(ÄÄÄÅ…ï—’…∏Åçïπ—…ï}çΩëî∞ÅÕïÕÕ•Ωπ}±Öâï∞(()ëïòÅ}ç…µ}Õïç…ï—Ö…•Ö—}ÖπÕ›ï…}ŸÖ±’ïÃ°ïπ—…‰§Ë(ÄÄÄÄààâ5Ö¿ÅÖç—’Ö∞ÅÕ’âµ•——ïêÅÖπÕ›ï…Ã∞ÅÕ°Ö…ïêÅâ‰Å±•ŸîÅÕ’âµ•ÕÕ•ΩπÃÅÖπêÅ…ïçΩŸï…‰∏ààà(ÄÄÄÅôΩ…µÖ—•Ωπ}≠ï‰ÄÙÅÕ—»°ïπ—…‰πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅôΩ…µÖ—•Ω∏ÄÙÅÏ(ÄÄÄÄÄÄÄÄâMA}%9%PàËÄâM@à∞ÄâMA}YàËÄâM@à∞ÄâMM%@àËÄâMM%@Äƒà∞(ÄÄÄÄÄÄÄÄâYQàËÄâ°Ö’ôôï’»ÅYQà∞(ÄÄÄÅÙπùï–°ôΩ…µÖ—•Ωπ}≠ï‰∞ÅôΩ…µÖ—•Ωπ}≠ï‰§(ÄÄÄÅçïπ—…ï}çΩëî∞ÅÕïÕÕ•Ωπ}±Öâï∞ÄÙÅ}Õïç…ï—Ö…•Ö—}ÕïÕÕ•Ωπ}ëï—Ö•±Ã°ïπ—…‰§((ÄÄÄÅ±•ï‘ÄÙÅÏ(ÄÄÄÄÄÄÄÄâ¡Ö…•ÃàËÄâAÖ…•Ãà∞(ÄÄÄÄÄÄÄÄâçΩ—ï}ÖÈ’»àËÄâ——îÅìäeÈ’»à∞(ÄÄÄÄÄÄÄÄâÖ’Ÿï…ùπîàËÄâ’Ÿï…ùπîà∞(ÄÄÄÅÙπùï–°çïπ—…ï}çΩëî∞Äàà§(ÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏àËÅôΩ…µÖ—•Ω∏∞(ÄÄÄÄÄÄÄÄâ±•ï‘àËÅ±•ï‘∞(ÄÄÄÄÄÄÄÄâëÖ—ïÕ}ôΩ…µÖ—•Ω∏àËÅÕïÕÕ•Ωπ}±Öâï∞∞(ÄÄÄÄÄÄÄÄâç¡òàËÅÕ—»°ïπ—…‰πùï–†âç¡ô}çΩπÕ’±—îà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâç¡ô}µΩπ—Öπ–àËÅπΩ…µÖ±•Èï}ç¡ô}ÖµΩ’π–°ïπ—…‰πùï–†âç¡ô}µΩπ—Öπ–à§§∞(ÄÄÄÄÄÄÄÄâçÖ…—ï}¡…ºàËÅÕ—»°ïπ—…‰πùï–†âçπÖ¡Õ}Ω¨à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâ—•—…ï}Õï©Ω’»àËÅÕ—»°ïπ—…‰πùï–†â—•—…ï}Õï©Ω’»à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâùÖ…ëï}Ÿ’îàËÅÕ—»°ïπ—…‰πùï–†âùÖ…ëï}Ÿ’îà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâÖπ—ïçïëïπ—ÃàËÅÕ—»°ïπ—…‰πùï–†âùÖ…ëï}Ÿ’îà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâëïÕ¡}—Â¡îàËÄâYàÅ•òÅôΩ…µÖ—•Ωπ}≠ï‰ÄÙÙÄâMA}YàÅï±ÕîÄ†â%9%Q%0àÅ•òÅôΩ…µÖ—•Ωπ}≠ï‰ÄÙÙÄâMA}%9%PàÅï±ÕîÄàà§∞(ÄÄÄÄÄÄÄÄâ•ëïπ—•—ï}ç…ïÖ—•Ω∏àËÅÕ—»°ïπ—…‰πùï–†â•ëïπ—•—ï}π’µï…•≈’îà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâô•πÖπçïµïπ—}ô–àËÅÕ—»°ïπ—…‰πùï–†âô…Öπçï}—…ÖŸÖ•∞à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâô•πÖπçïµïπ—}¡ï…ÕΩ}¡ΩÕÕ•â±îàËÅÕ—»°ïπ—…‰πùï–†âô—}…ïô’Õ}Ω¨à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâ…ïô’Õ}ô—}¡ï…ÕºàËÅÕ—»°ïπ—…‰πùï–†âô—}…ïô’Õ}Ω¨à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâ…ïÕ—ï}Ö}ç°Ö…ùï}¡ï…ÕºàËÅÕ—»°ïπ—…‰πùï–†âô•πÖπçïµïπ—}¡ï…Õºà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÅÙ(()ëïòÅ}ç…µ}Õïç…ï—Ö…•Ö—}ÖπÕ›ï…Õ}µÖ—ç°}çΩπ—Öç–°çΩπ—Öç–∞Åïπ—…‰§Ë(ÄÄÄÄààâÅë’…Öâ±îÅ…ï≈’ïÕ–Å%ÅÖ±ΩπîÅçÖππΩ–ÅÖ’—°Ω…•ÈîÅô•±±•πúÅ≈’Ö±•ô•çÖ—•Ω∏Åô•ï±ëÃ∏ààà(ÄÄÄÅïµÖ•∞ÄÙÅ}ç…µ}πΩ…µÖ±•Èï}ïµÖ•∞°ïπ—…‰πùï–†âïµÖ•∞à§ÅΩ»Åïπ—…‰πùï–†âµÖ•∞à§§(ÄÄÄÅ¡°ΩπîÄÙÅ}ç…µ}πΩ…µÖ±•Èï}¡°Ωπî°ïπ—…‰πùï–†â—ï±ï¡°Ωπîà§§(ÄÄÄÅÕ—Ω…ïë}ïµÖ•∞ÄÙÅ}ç…µ}πΩ…µÖ±•Èï}ïµÖ•∞°çΩπ—Öç–πùï–†âµÖ•∞à§§(ÄÄÄÅÕ—Ω…ïë}¡°ΩπîÄÙÅ}ç…µ}πΩ…µÖ±•Èï}¡°Ωπî°çΩπ—Öç–πùï–†â—ï±ï¡°Ωπîà§§(ÄÄÄÅÖù…ïïÃÄÙÄ°ïµÖ•∞ÅÖπêÅïµÖ•∞ÄÙÙÅÕ—Ω…ïë}ïµÖ•∞§ÅΩ»Ä°¡°ΩπîÅÖπêÅ¡°ΩπîÄÙÙÅÕ—Ω…ïë}¡°Ωπî§(ÄÄÄÅë•ÕÖù…ïïÃÄÙÄ°ïµÖ•∞ÅÖπêÅÕ—Ω…ïë}ïµÖ•∞ÅÖπêÅïµÖ•∞ÄÑÙÅÕ—Ω…ïë}ïµÖ•∞§ÅΩ»Ä°¡°ΩπîÅÖπêÅÕ—Ω…ïë}¡°ΩπîÅÖπêÅ¡°ΩπîÄÑÙÅÕ—Ω…ïë}¡°Ωπî§(ÄÄÄÅ•òÅπΩ–ÅÖù…ïïÃÅΩ»Åë•ÕÖù…ïïÃË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅô•…Õ–ÄÙÅ}ç…µ}πΩ…µÖ±•Èï}πÖµî°ïπ—…‰πùï–†â¡…ïπΩ¥à§§(ÄÄÄÅÕ—Ω…ïë}ô•…Õ–ÄÙÅ}ç…µ}πΩ…µÖ±•Èï}πÖµî°çΩπ—Öç–πùï–†â¡…ïπΩ¥à§§(ÄÄÄÅ•òÅô•…Õ–ÅÖπêÅÕ—Ω…ïë}ô•…Õ–ÅÖπêÅô•…Õ–ÄÑÙÅÕ—Ω…ïë}ô•…Õ–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÄåÅQ°îÅÕÖŸïêÅôΩ…¥Å’ÕïÃÅÑÅô’±∞ÅπÖµîÏÅ—°îÅ•πâΩ’πêÅÕπÖ¡Õ°Ω–Å’ÕïÃÅÑÅÕ’…πÖµî∏(ÄÄÄÅπÖµîÄÙÅ}ç…µ}πΩ…µÖ±•Èï}πÖµî°ïπ—…‰πùï–†âπΩµ}ôÖµ•±±îà§ÅΩ»Åïπ—…‰πùï–†âπΩ¥à§§(ÄÄÄÅÕ—Ω…ïë}πÖµîÄÙÅ}ç…µ}πΩ…µÖ±•Èï}πÖµî°çΩπ—Öç–πùï–†âπΩ¥à§§(ÄÄÄÅ•òÅπÖµîÅÖπêÅÕ—Ω…ïë}πÖµîÅÖπêÅπÖµîÅπΩ–Å•∏ÅÌÕ—Ω…ïë}πÖµî∞ÅÕ—Ω…ïë}ô•…Õ–Ä¨ÅÕ—Ω…ïë}πÖµïÙË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅ…ï—’…∏ÅQ…’î(()ëïòÅ}ç…µ}…ïçΩŸï…}Õïç…ï—Ö…•Ö—}ÖπÕ›ï…Ã°ëÖ—Ñ∞Ä®∞Åë…Â}…’∏ıQ…’î§Ë(ÄÄÄÅô…Ω¥Åç…µ}Õïç…ï—Ö…•Ö—}ÖπÕ›ï…ÃÅ•µ¡Ω…–Å…ïçΩŸï…}ÖπÕ›ï…Ã(ÄÄÄÅ…ï—’…∏Å…ïçΩŸï…}ÖπÕ›ï…Ã†(ÄÄÄÄÄÄÄÅëÖ—Ñ∞ÅµÖ¡}ÖπÕ›ï…Ãı}ç…µ}Õïç…ï—Ö…•Ö—}ÖπÕ›ï…}ŸÖ±’ïÃ∞(ÄÄÄÄÄÄÄÅ•Õ}ïµ¡—‰ı}ç…µ}•Õ}ïµ¡—‰∞ÅπΩ…µÖ±•Èï}ÖµΩ’π–ıπΩ…µÖ±•Èï}ç¡ô}ÖµΩ’π–∞(ÄÄÄÄÄÄÄÅµÖ—ç°ïÕ}çΩπ—Öç–ı}ç…µ}Õïç…ï—Ö…•Ö—}ÖπÕ›ï…Õ}µÖ—ç°}çΩπ—Öç–∞(ÄÄÄÄÄÄÄÅπΩ‹ı}ç…µ}πΩ‹†§∞Åë…Â}…’∏ıë…Â}…’∏∞(ÄÄÄÄ§(()ëïòÅ}ç…µ}…ïÕ—Ω…ï}Õïç…ï—Ö…•Ö—}ÖπÕ›ï…Õ}Ωπçî†§Ë(ÄÄÄÄààâI’∏Å—°îÅπΩ∏µëïÕ—…’ç—•Ÿî∞ÅŸï…Õ•ΩπïêÅ…ïçΩŸï…‰ÅâïôΩ…îÅÕï…Ÿ•πúÅI4Å—…Öôô•å∏ààà(ÄÄÄÅô…Ω¥Åç…µ}Õïç…ï—Ö…•Ö—}ÖπÕ›ï…ÃÅ•µ¡Ω…–ÅIA%I}-d∞ÅYIM%=8(ÄÄÄÅ›•—†Å}I5}I=9%1%Q%=9}1=,Ë(ÄÄÄÄÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÄÄÄÄÅ•òÄ°ëÖ—Ñπùï–°IA%I}-d§ÅΩ»ÅÌÙ§πùï–†âŸï…Õ•Ω∏à§ÄÙÙÅYIM%=8Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅëÖ—ÖmIA%I}-et(ÄÄÄÄÄÄÄÅ…ï¡Ω…–ÄÙÅ}ç…µ}…ïçΩŸï…}Õïç…ï—Ö…•Ö—}ÖπÕ›ï…Ã°ëÖ—Ñ∞Åë…Â}…’∏ıÖ±Õî§(ÄÄÄÄÄÄÄÅ…ï¡Ω…—lâçΩµ¡±ï—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÅëÖ—ÖmIA%I}-etÄÙÅ…ï¡Ω…–(ÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÄÄÄÄÅÖ¡¿π±Ωùùï»π›Ö…π•πú†(ÄÄÄÄÄÄÄÄÄÄÄÄâÕïç…ï—Ö…•Ö—}ÖπÕ›ï…Õ}…ï¡Ö•»ÅŸï…Õ•Ω∏ÙïÃÅçΩπ—Öç—ÃÙïÃÅô•ï±ëÃÙïÃÅçΩπô±•ç—ÃÙïÃÅÕ≠•¡¡ïêÙïÃà∞(ÄÄÄÄÄÄÄÄÄÄÄÅYIM%=8∞Å…ï¡Ω…—lâçΩπ—Öç—Ãât∞Å…ï¡Ω…—lâô•ï±ëÃât∞(ÄÄÄÄÄÄÄÄÄÄÄÅ±ï∏°…ï¡Ω…—lâçΩπô±•ç—Ãât§∞Å±ï∏°…ï¡Ω…—lâÕ≠•¡¡ïêât§∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å…ï¡Ω…–(()ëïòÅ}ç…µ}ç…ïÖ—ï}çΩπ—Öç—}ô…Ωµ}Õïç…ï—Ö…•Ö–†(ÄÄÄÄÄÄÄÅëÖ—Ñ∞Åïπ—…‰∞Åç…µ}¡ÖÂ±ΩÖê∞Ä®∞Åç…ïÖ—ï}Ωπ}Öµâ•ù’•—‰ıÖ±Õî§Ë(ÄÄÄÄààâ…ïÖ—îÅΩ»ÅçΩµ¡±ï—îÅ—°îÅÕÖôï±‰ÅµÖ—ç°ïêÅI4Å…ïçΩ…êÅô…Ω¥ÅÑÅÕïç…ï—Ö…‰ÅçÖ±∞∏ààà(ÄÄÄÅô…Ω¥Åç…µ}Õïç…ï—Ö…•Ö—}ÖπÕ›ï…ÃÅ•µ¡Ω…–ÅÖ¡¡±Â}ÖπÕ›ï…Ã(ÄÄÄÅπΩ‹ÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅŸÖ±’ïÃÄÙÅ}ç…µ}Õïç…ï—Ö…•Ö—}ÖπÕ›ï…}ŸÖ±’ïÃ°ïπ—…‰§(ÄÄÄÅçΩπ—Öç–ÄÙÅÏ(ÄÄÄÄÄÄÄÄâ•êàËÅÕ—»°’’•êπ’’•ê–†§§∞(ÄÄÄÄÄÄÄÄâ¡…ïπΩ¥àËÅ}ç…µ}ôΩ…µÖ—}ô•…Õ—}πÖµî°ç…µ}¡ÖÂ±ΩÖêπùï–†â¡…ïπΩ¥à§§∞(ÄÄÄÄÄÄÄÄâπΩ¥àËÅ}ç…µ}ôΩ…µÖ—}±ÖÕ—}πÖµî°ç…µ}¡ÖÂ±ΩÖêπùï–†âπΩ¥à§§∞(ÄÄÄÄÄÄÄÄâ—ï±ï¡°ΩπîàËÅÕ—»°ïπ—…‰πùï–†â—ï±ï¡°Ωπîà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâµÖ•∞àËÅÕ—»°ïπ—…‰πùï–†âïµÖ•∞à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄ®©ŸÖ±’ïÃ∞(ÄÄÄÄÄÄÄÄâÕ—Ö—’–àËÄâ9Ω’ŸïÖ’‡à∞(ÄÄÄÄÄÄÄÄâ•ëïπ—•—ï}Ω¨àËÄàà∞(ÄÄÄÄÄÄÄÄâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–àËÄàà∞ÄâµΩπ—Öπ—}ÖççΩ…ëï}ô–àËÄàà∞(ÄÄÄÄÄÄÄÄâΩ…•ù•πîàËÄâMïçÀ•—Ö…•Ö–à∞(ÄÄÄÄÄÄÄÄâ•πÕç…•—}ô–àËÄàà∞(ÄÄÄÄÄÄÄÄâçΩµµïπ—Ö•…ïÃàËÅÕ—»°ïπ—…‰πùï–†âπΩ—ïÃà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâ…ï±Öπçï}ëÖ—îàËÄàà∞(ÄÄÄÄÄÄÄÄâç…ïÖ—ïë}Ö–àËÅπΩ‹∞(ÄÄÄÄÄÄÄÄâ’¡ëÖ—ïë}Ö–àËÅπΩ‹∞(ÄÄÄÄÄÄÄÄâÖç—•Ÿ•—•ïÃàËÅmt∞(ÄÄÄÄÄÄÄÄâÕΩ’…çîàËÄâÖÕÕ•Õ—Öπ–µÕïç…ï—Ö…•Ö–à∞(ÄÄÄÄÄÄÄÄâÕΩ’…çï}Õïç…ï—Ö…•Ö—}•êàËÅïπ—…‰πùï–†â•êà§∞(ÄÄÄÄÄÄÄÄâôΩ…µ’±Ö•…îàËÅë•ç–°ïπ—…‰§∞(ÄÄÄÅÙ(ÄÄÄÅëï—Ö•∞ÄÙÄâ¡¡ï∞Åïπ…ïù•Õ—À§Å¡Ö»Å±îÅÕïçÀ•—Ö…•Ö–à(ÄÄÄÅ•òÅïπ—…‰πùï–†â…ëÿà§Ë(ÄÄÄÄÄÄÄÅëï—Ö•∞Ä¨ÙÅòàÉ
‹ÅIïπëïËµŸΩ’ÃÄËÅÌïπ—…Âlù…ëÿùuÙà(ÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞Äâç…ïÖ—•Ω∏à∞ÄâA•Õ—îÅçÀß•îÅëï¡’•ÃÅ±îÅÕïçÀ•—Ö…•Ö–à∞Åëï—Ö•∞§(ÄÄÄÅ…ïçΩπç•±•Ö—•Ωπ}¡ÖÂ±ΩÖêÄÙÅÏ®©ë•ç–°ïπ—…‰§∞ÄâµÖ•∞àËÅïπ—…‰πùï–†âïµÖ•∞à§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâπΩ¥àËÅç…µ}¡ÖÂ±ΩÖêπùï–†âπΩ¥à§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ¡…ïπΩ¥àËÅç…µ}¡ÖÂ±ΩÖêπùï–†â¡…ïπΩ¥à§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ±•ï‘àËÅŸÖ±’ïÕlâ±•ï‘ât∞ÄâôΩ…µÖ—•Ω∏àËÅŸÖ±’ïÕlâôΩ…µÖ—•Ω∏âuÙ(ÄÄÄÅµÖ—ç°ïê∞Å•πâΩ’πê∞Åç…ïÖ—ïêÄÙÅô•πë}Ω…}ç…ïÖ—ï}ç…µ}çΩπ—Öç–†(ÄÄÄÄÄÄÄÅëÖ—Ñ∞Å…ïçΩπç•±•Ö—•Ωπ}¡ÖÂ±ΩÖê∞ÄâÖÕÕ•Õ—Öπ–µÕïç…ï—Ö…•Ö–à∞(ÄÄÄÄÄÄÄÅ¡…Ω¡ΩÕïë}çΩπ—Öç–ıçΩπ—Öç–∞Åï·—ï…πÖ±}•êıïπ—…‰πùï–†â•êà§∞(ÄÄÄÄÄÄÄÅç…ïÖ—ï}Ωπ}Öµâ•ù’•—‰ıç…ïÖ—ï}Ωπ}Öµâ•ù’•—‰∞(ÄÄÄÄ§(ÄÄÄÅ•òÅ•πâΩ’πêπùï–†âÕ—Ö—’Ãà§ÄÙÙÄâ¡ïπë•πù}…ïŸ•ï‹àË(ÄÄÄÄÄÄÄÅïπ—…Âlâç…µ}çΩπ—Öç—}•êâtÄÙÄàà(ÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(ÄÄÄÅ•òÅµÖ—ç°ïêË(ÄÄÄÄÄÄÄÅ•òÅπΩ–Åç…ïÖ—ïêÅÖπêÅπΩ–Å}ç…µ}Õïç…ï—Ö…•Ö—}ÖπÕ›ï…Õ}µÖ—ç°}çΩπ—Öç–°µÖ—ç°ïê∞Å…ïçΩπç•±•Ö—•Ωπ}¡ÖÂ±ΩÖê§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ•πâΩ’πëlâÕ—Ö—’ÃâtÄÙÄâ¡ïπë•πù}…ïŸ•ï‹à(ÄÄÄÄÄÄÄÄÄÄÄÅ•πâΩ’πêπÕï—ëïôÖ’±–†â…ïŸ•ï›}…ïÖÕΩπÃà∞Åmt§πÖ¡¡ïπê†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâK•¡ΩπÕïÃÅë‘ÅÕïçÀ•—Ö…•Ö–ÅπΩ∏Å•π”•ùÀ•ïÃÄËÅ•ëïπ—•”§ÅΩ‘ÅçΩΩ…ëΩπª•ïÃÅë•ôõ•…ïπ—ïÃÅëîÅ±ÑÅô•ç°îÅ±ß•î∏à(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Âlâç…µ}çΩπ—Öç—}•êâtÄÙÄàà(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(ÄÄÄÄÄÄÄÅïπ—…Âlâç…µ}çΩπ—Öç—}•êâtÄÙÅµÖ—ç°ïëlâ•êât(ÄÄÄÄÄÄÄÅ•òÅπΩ–Åç…ïÖ—ïêË(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïÃ∞ÅçΩπô±•ç—ÃÄÙÅÖ¡¡±Â}ÖπÕ›ï…Ã†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµÖ—ç°ïê∞ÅŸÖ±’ïÃ∞Å•Õ}ïµ¡—‰ı}ç…µ}•Õ}ïµ¡—‰∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅπΩ…µÖ±•Èï}ÖµΩ’π–ıπΩ…µÖ±•Èï}ç¡ô}ÖµΩ’π–∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅë•ôôï…ïπçïÃÄÙÅ•πâΩ’πêπÕï—ëïôÖ’±–†âë•ôôï…ïπçïÃà∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÅπï›}çΩπô±•ç—ÃÄÙÅmô•ï±êÅôΩ»Åô•ï±êÅ•∏ÅçΩπô±•ç—ÃÅ•òÅô•ï±êÅπΩ–Å•∏Åë•ôôï…ïπçïÕt(ÄÄÄÄÄÄÄÄÄÄÄÅë•ôôï…ïπçïÃπï·—ïπê°πï›}çΩπô±•ç—Ã§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅç°ÖπùïÃÅΩ»Åπï›}çΩπô±•ç—ÃË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅëï—Ö•∞ÄÙÄâ°Öµ¡ÃÅçΩµ¡≥•”•ÃÄËÄàÄ¨Äà∞Äàπ©Ω•∏°ç°ÖπùïÃ§Ä¨Äà∏àÅ•òÅç°ÖπùïÃÅï±ÕîÄàà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπï›}çΩπô±•ç—ÃË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅëï—Ö•∞Ä¨ÙÄàÅ%πôΩ…µÖ—•ΩπÃÅë•ôõ•…ïπ—ïÃÅçΩπÕï…€•ïÃÅ¡Ω’»Å€•…•ô•çÖ—•Ω∏ÄËÄàÄ¨Äà∞Äàπ©Ω•∏°πï›}çΩπô±•ç—Ã§Ä¨Äà∏à(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°µÖ—ç°ïê∞Äâ•πâΩ’πë}…ï≈’ïÕ–à∞ÄâK•¡ΩπÕïÃÅë‘ÅÕïçÀ•—Ö…•Ö–Å•π”•ùÀ•ïÃà∞Åëï—Ö•∞§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµÖ—ç°ïëlâ’¡ëÖ—ïë}Ö–âtÄÙÅπΩ‹(ÄÄÄÅ…ï—’…∏ÅµÖ—ç°ïê(()ëïòÅ}ç…µ}ïπÕ’…ï}Õïç…ï—Ö…•Ö—}¡’â±•çÖ—•Ω∏°çΩπ—Öç–∞Åïπ—…‰§Ë(ÄÄÄÄààâA’â±•Õ†Å—°îÅÕïç…ï—Ö…‰ùÃÅçÖ±∞Åëï—Ö•±ÃÅΩπçîÅΩ∏Å—°îÅµÖ—ç°ïêÅI4ÅçΩπ—Öç–∏ààà(ÄÄÄÅ—ï·–ÄÙÅÕ—»°ïπ—…‰πùï–†âπΩ—ïÃà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–ÅΩ»ÅπΩ–Å—ï·–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî((ÄÄÄÅ…ï≈’ïÕ—}•êÄÙÅÕ—»°ïπ—…‰πùï–†â•êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ¡’â±•çÖ—•Ωπ}•êÄÙÅÕ—»°ïπ—…‰πùï–†âç…µ}¡’â±•çÖ—•Ωπ}•êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ¡’â±•çÖ—•ΩπÃÄÙÅçΩπ—Öç–πÕï—ëïôÖ’±–†â¡’â±•çÖ—•ΩπÃà∞Åmt§(ÄÄÄÅ¡’â±•çÖ—•Ω∏ÄÙÅπï·–††(ÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏Å¡’â±•çÖ—•ΩπÃ(ÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°•—ï¥∞Åë•ç–§ÅÖπêÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄ°¡’â±•çÖ—•Ωπ}•êÅÖπêÅÕ—»°•—ï¥πùï–†â•êà§ÅΩ»Äàà§ÄÙÙÅ¡’â±•çÖ—•Ωπ}•ê§(ÄÄÄÄÄÄÄÄÄÄÄÅΩ»Ä†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï≈’ïÕ—}•ê(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅÕ—»°•—ï¥πùï–†âÕΩ’…çï}Õïç…ï—Ö…•Ö—}•êà§ÅΩ»Äàà§ÄÙÙÅ…ï≈’ïÕ—}•ê(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄ§∞Å9Ωπî§((ÄÄÄÅ•òÅ¡’â±•çÖ—•Ω∏Å•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅ¡’â±•çÖ—•Ω∏ÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ•êàËÅÕ—»°’’•êπ’’•ê–†§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâëÖ—îàËÅÕ—»°ïπ—…‰πùï–†âç…ïÖ—ïë}Ö–à§ÅΩ»Å}ç…µ}πΩ‹†§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ—ï·—îàËÅ—ï·–∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÖ’—°Ω»àËÄâMïçÀ•—Ö…•Ö–à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÖ’—°Ω…}ïµÖ•∞àËÄàà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ±•≠ïÃàËÅmt∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩµµïπ—ÃàËÅmt∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕΩ’…çîàËÄâÖÕÕ•Õ—Öπ–µÕïç…ï—Ö…•Ö–à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕΩ’…çï}Õïç…ï—Ö…•Ö—}•êàËÅ…ï≈’ïÕ—}•ê∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÅ¡’â±•çÖ—•ΩπÃπ•πÕï…–†¿∞Å¡’â±•çÖ—•Ω∏§(ÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÅ¡’â±•çÖ—•Ω∏π’¡ëÖ—î°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâ—ï·—îàËÅ—ï·–∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÖ’—°Ω»àËÄâMïçÀ•—Ö…•Ö–à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÖ’—°Ω…}ïµÖ•∞àËÄàà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕΩ’…çîàËÄâÖÕÕ•Õ—Öπ–µÕïç…ï—Ö…•Ö–à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕΩ’…çï}Õïç…ï—Ö…•Ö—}•êàËÅ…ï≈’ïÕ—}•ê∞(ÄÄÄÄÄÄÄÅÙ§((ÄÄÄÅïπ—…Âlâç…µ}¡’â±•çÖ—•Ωπ}•êâtÄÙÅ¡’â±•çÖ—•Ωπlâ•êât(ÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅ…ï—’…∏Å¡’â±•çÖ—•Ω∏(()ëïòÅ}Õïç…ï—Ö…•Ö—}•πôΩ…µÖ—•Ωπ}—ïµ¡±Ö—î°ëÖ—Ñ∞Å≠•πê∞ÅôΩ…µÖ—•Ωπ}çΩëî§Ë(ÄÄÄÄààâIï—’…∏Å—°îÅI4ÅÅÅ%πôΩ…µÖ—•ΩπÃÄÒôΩ…µÖ—•Ω∏˘ÅÄÅ—ïµ¡±Ö—îÅôΩ»ÅÑÅçÖ±∞∏ààà(ÄÄÄÅôΩ…µÖ—•Ωπ}çΩëîÄÙÅÕ—»°ôΩ…µÖ—•Ωπ}çΩëîÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅôΩ…µÖ—•Ω∏ÄÙÅMIQI%Q}=I5Q%=9Lπùï–°ôΩ…µÖ—•Ωπ}çΩëî∞ÅÌÙ§(ÄÄÄÄåÅ1ïÃÅçΩëïÃÅ—ïç°π•≈’ïÃÅë‘ÅÕïçÀ•—Ö…•Ö–ÅπîÅÕΩπ–Å¡ÖÃÅ—Ω’©Ω’…ÃÅçï’‡Åïµ¡±ΩÁ•Ã(ÄÄÄÄåÅëÖπÃÅ±ÑÅâ•â±•Ω—£°≈’îÅI4Ä°¡Ö»Åï·ïµ¡±îÅMA}%9%PÅï–ÅMA}YÅ‰ÅÕΩπ–(ÄÄÄÄåÅü•ª•…Ö±ïµïπ–Å…ïù…Ω’√•ÃÅÕΩ’ÃÉ
¨Å%πôΩ…µÖ—•ΩπÃÅM@É
Ï§∏(ÄÄÄÅÖ±•ÖÕïÃÄÙÅÏ(ÄÄÄÄÄÄÄÄâMM%@àËÅlâMM%@Äƒât∞(ÄÄÄÄÄÄÄÄâMA}%9%PàËÅlâM@Å•π•—•Ö∞à∞ÄâM@ât∞(ÄÄÄÄÄÄÄÄâMA}YàËÅlâYÅM@à∞ÄâM@ÅYà∞ÄâM@ât∞(ÄÄÄÄÄÄÄÄâYQàËÅlâ°Ö’ôôï’»ÅYQât∞(ÄÄÄÅÙπùï–°ôΩ…µÖ—•Ωπ}çΩëî∞Åmt§(ÄÄÄÅπÖµïÃÄÙÅl(ÄÄÄÄÄÄÄÅòâ•πôΩ…µÖ—•ΩπÃÅÌôΩ…µÖ—•Ωπ}çΩëïÙà∞(ÄÄÄÄÄÄÄÅòâ•πôΩ…µÖ—•ΩπÃÅÌôΩ…µÖ—•Ω∏πùï–†ùÕ°Ω…–ú∞Äúú•Ùà∞(ÄÄÄÄÄÄÄÅòâ•πôΩ…µÖ—•ΩπÃÅÌôΩ…µÖ—•Ω∏πùï–†ù±Öâï∞ú∞Äúú•Ùà∞(ÄÄÄÄÄÄÄÄ®°òâ•πôΩ…µÖ—•ΩπÃÅÌÖ±•ÖÕÙàÅôΩ»ÅÖ±•ÖÃÅ•∏ÅÖ±•ÖÕïÃ§∞(ÄÄÄÅt((ÄÄÄÅëïòÅπΩ…µÖ±•Õî°ŸÖ±’î§Ë(ÄÄÄÄÄÄÄÅŸÖ±’îÄÙÅ’π•çΩëïëÖ—ÑππΩ…µÖ±•Èî†â9-à∞ÅÕ—»°ŸÖ±’îÅΩ»Äàà§§(ÄÄÄÄÄÄÄÅŸÖ±’îÄÙÄààπ©Ω•∏°ç°Ö»ÅôΩ»Åç°Ö»Å•∏ÅŸÖ±’îÅ•òÅπΩ–Å’π•çΩëïëÖ—ÑπçΩµâ•π•πú°ç°Ö»§§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å…îπÕ’à°»âmyÑµË¿¥Ât¨à∞ÄàÄà∞ÅŸÖ±’îπ±Ω›ï»†§§πÕ—…•¿†§((ÄÄÄÅï·¡ïç—ïêÄÙÅ±•Õ–°ë•ç–πô…Ωµ≠ïÂÃ°πΩ…µÖ±•Õî°πÖµî§ÅôΩ»ÅπÖµîÅ•∏ÅπÖµïÃ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ…µÖ±•Õî°πÖµî§ÄÑÙÄâ•πôΩ…µÖ—•ΩπÃà§§(ÄÄÄÅ—ïµ¡±Ö—ïÃÄÙÅëÖ—Ñπùï–°òâç…µ}Ì≠•πëı}—ïµ¡±Ö—ïÃà∞Åmt§(ÄÄÄÅ…ï—’…∏Åπï·–†°—ïµ¡±Ö—îÅôΩ»ÅπÖµîÅ•∏Åï·¡ïç—ïêÅôΩ»Å—ïµ¡±Ö—îÅ•∏Å—ïµ¡±Ö—ïÃ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ…µÖ±•Õî°—ïµ¡±Ö—îπùï–†âπΩ¥à§§ÄÙÙÅπÖµî§∞Å9Ωπî§(()ëïòÅ}ç…µ}πï·—}¡°Ωπï}Ö¡¡Ω•π—µïπ–°ëÖ—Ñ∞Åïπ—…‰∞ÅçΩπ—Öç–ı9Ωπî∞ÅπΩ‹ı9Ωπî§Ë(ÄÄÄÄààâIï—’…∏Å—°îÅπï·–ÅçÖç°ïêÅ¡°ΩπîÅÖ¡¡Ω•π—µïπ–ÅµÖ—ç°•πúÅÑÅÕïç…ï—Ö…•Ö–ÅçÖ±∞∏ààà(ÄÄÄÅçΩπ—Öç–ÄÙÅçΩπ—Öç–ÅΩ»ÅÌÙ(ÄÄÄÅçΩπ—Öç—}•êÄÙÅÕ—»°çΩπ—Öç–πùï–†â•êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅïµÖ•±ÃÄÙÅÏ(ÄÄÄÄÄÄÄÅ}ç…µ}πΩ…µÖ±•Èï}ïµÖ•∞°ŸÖ±’î§(ÄÄÄÄÄÄÄÅôΩ»ÅŸÖ±’îÅ•∏Ä°çΩπ—Öç–πùï–†âµÖ•∞à§∞Åïπ—…‰πùï–†âïµÖ•∞à§§(ÄÄÄÄÄÄÄÅ•òÅ}ç…µ}πΩ…µÖ±•Èï}ïµÖ•∞°ŸÖ±’î§(ÄÄÄÅÙ(ÄÄÄÅ¡°ΩπïÃÄÙÅÏ(ÄÄÄÄÄÄÄÅ}ç…µ}πΩ…µÖ±•Èï}¡°Ωπî°ŸÖ±’î§(ÄÄÄÄÄÄÄÅôΩ»ÅŸÖ±’îÅ•∏Ä°çΩπ—Öç–πùï–†â—ï±ï¡°Ωπîà§∞Åïπ—…‰πùï–†â—ï±ï¡°Ωπîà§§(ÄÄÄÄÄÄÄÅ•òÅ}ç…µ}πΩ…µÖ±•Èï}¡°Ωπî°ŸÖ±’î§(ÄÄÄÅÙ(ÄÄÄÅ¡Ö…•ÃÄÙÅ¡Â—Ëπ—•µïÈΩπî†â’…Ω¡îΩAÖ…•Ãà§(ÄÄÄÅπΩ‹ÄÙÅπΩ‹ÅΩ»ÅëÖ—ï—•µîπëÖ—ï—•µîππΩ‹°¡Ö…•Ã§(ÄÄÄÅ•òÅπΩ‹π—È•πôºÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅπΩ‹ÄÙÅ¡Ö…•Ãπ±ΩçÖ±•Èî°πΩ‹§(ÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÅπΩ‹ÄÙÅπΩ‹πÖÕ—•µïÈΩπî°¡Ö…•Ã§(ÄÄÄÅµÖ—ç°ïÃÄÙÅmt(ÄÄÄÅôΩ»ÅÖ¡¡Ω•π—µïπ–Å•∏ÅëÖ—Ñπùï–†âç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Ãà∞Åmt§Ë(ÄÄÄÄÄÄÄÅÕÖµï}çΩπ—Öç–ÄÙÅâΩΩ∞°çΩπ—Öç—}•ê§ÅÖπêÅÕ—»°Ö¡¡Ω•π—µïπ–πùï–†âçΩπ—Öç—}•êà§ÅΩ»Äàà§ÄÙÙÅçΩπ—Öç—}•ê(ÄÄÄÄÄÄÄÅÕÖµï}ïµÖ•∞ÄÙÅ}ç…µ}πΩ…µÖ±•Èï}ïµÖ•∞°Ö¡¡Ω•π—µïπ–πùï–†â•πŸ•—ïï}ïµÖ•∞à§§Å•∏ÅïµÖ•±Ã(ÄÄÄÄÄÄÄÅÕÖµï}¡°ΩπîÄÙÅ}ç…µ}πΩ…µÖ±•Èï}¡°Ωπî°Ö¡¡Ω•π—µïπ–πùï–†â•πŸ•—ïï}¡°Ωπîà§§Å•∏Å¡°ΩπïÃ(ÄÄÄÄÄÄÄÅ•òÅπΩ–Ä°ÕÖµï}çΩπ—Öç–ÅΩ»ÅÕÖµï}ïµÖ•∞ÅΩ»ÅÕÖµï}¡°Ωπî§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ•òÅÕ—»°Ö¡¡Ω•π—µïπ–πùï–†âÕ—Ö—’Ãà§ÅΩ»ÄâÖç—•Ÿîà§πçÖÕïôΩ±ê†§Å•∏ÅÏâçÖπçï±ïêà∞ÄâçÖπçï±±ïêâÙË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ±ΩçÖ—•Ω∏ÄÙÅÖ¡¡Ω•π—µïπ–πùï–†â±ΩçÖ—•Ω∏à§ÅΩ»ÅÌÙ(ÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°±ΩçÖ—•Ω∏∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ±ΩçÖ—•Ωπ}≠•πêÄÙÅÕ—»°±ΩçÖ—•Ω∏πùï–†â≠•πêà§ÅΩ»Å±ΩçÖ—•Ω∏πùï–†â—Â¡îà§ÅΩ»Äàà§πçÖÕïôΩ±ê†§(ÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÅ±ΩçÖ—•Ωπ}≠•πêÄÙÅÕ—»°±ΩçÖ—•Ω∏§πçÖÕïôΩ±ê†§(ÄÄÄÄÄÄÄÄåÅUÕîÅ—°îÅçΩµµΩ∏ÅÕ—ï¥ÅÖÃÅÖ±ïπë±‰ÅïŸïπ–ÅπÖµïÃÅ’Õ’Ö±±‰ÅçΩπ—Ö•∏(ÄÄÄÄÄÄÄÄåÄâ”•≥•¡°Ωπ•≈’îàÅ…Ö—°ï»Å—°Ö∏Å—°îÅπΩ’∏Äâ”•≥•¡°Ωπîà∏ÄÅQ°îÅ¡…ïŸ•Ω’Ã(ÄÄÄÄÄÄÄÄåÅï·Öç–Å—ï…µÃÅë•êÅπΩ–ÅµÖ—ç†ÄâIXÅ”•≥•¡°Ωπ•≈’îÄ∏∏∏àÅ›°ï∏Å—°îÅïŸïπ–Å°Öê(ÄÄÄÄÄÄÄÄåÅπºÅï·¡±•ç•–Å±ΩçÖ—•Ω∏∏(ÄÄÄÄÄÄÄÅ¡°Ωπï}—ï…µÃÄÙÄ†â¡°Ωπîà∞ÄâçÖ±∞à∞ÄâÖ¡¡ï∞à∞Äâ”•≥•¡°Ωπ§à∞Äâ—ï±ï¡°Ωπ§à§(ÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—}πÖµîÄÙÅÕ—»°Ö¡¡Ω•π—µïπ–πùï–†âπÖµîà§ÅΩ»Äàà§πçÖÕïôΩ±ê†§(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅÖπ‰°—ï…¥Å•∏Å±ΩçÖ—•Ωπ}≠•πêÅΩ»Å—ï…¥Å•∏ÅÖ¡¡Ω•π—µïπ—}πÖµîÅôΩ»Å—ï…¥Å•∏Å¡°Ωπï}—ï…µÃ§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö…–ÄÙÅëÖ—ï—•µîπëÖ—ï—•µîπô…Ωµ•ÕΩôΩ…µÖ–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—»°Ö¡¡Ω•π—µïπ–πùï–†âÕ—Ö…—}—•µîà§ÅΩ»Äàà§π…ï¡±Öçî†âhà∞Äà¨¿¿Ë¿¿à§(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅÕ—Ö…–π—È•πôºÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö…–ÄÙÅ¡Â—ËπUQπ±ΩçÖ±•Èî°Õ—Ö…–§(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö…–ÄÙÅÕ—Ö…–πÖÕ—•µïÈΩπî°¡Ö…•Ã§(ÄÄÄÄÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ•òÅÕ—Ö…–ÄÙÅπΩ‹Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅµÖ—ç°ïÃπÖ¡¡ïπê†°Õ—Ö…–∞ÅÖ¡¡Ω•π—µïπ–§§((ÄÄÄÅ•òÅπΩ–ÅµÖ—ç°ïÃË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(ÄÄÄÅ…ï—’…∏Åµ•∏°µÖ—ç°ïÃ∞Å≠ï‰ı±ÖµâëÑÅ•—ï¥ËÅ•—ïµl¡t§(()ëïòÅ}Õïç…ï—Ö…•Ö—}°Âë…Ö—ï}Ö¡¡Ω•π—µïπ—}ô…Ωµ}ç…¥°ëÖ—Ñ∞Åïπ—…‰∞ÅçΩπ—Öç–§Ë(ÄÄÄÄààâΩ¡‰Å—°îÅπï·–Å¡°ΩπîÅÖ¡¡Ω•π—µïπ–Åô…Ω¥Å—°îÅI4Å•π—ºÅ—°îÅôΩ±±Ω‹µ’¿ÅôÖç—Ã∏((ÄÄÄÅÖ±ïπë±‰ÅçÖ∏Å…ïçï•ŸîÅ—°îÅâΩΩ≠•πúÅâï—›ïï∏Å—°îÅô•…Õ–ÅÕïç…ï—Ö…•Ö–ÅôΩ…¥ÅÖπêÅ—°î(ÄÄÄÅô•πÖ∞ÅI4ÅÕ’âµ•ÕÕ•Ω∏∏ÄÅΩπÕï≈’ïπ—±‰Å—°îÅâ…Ω›Õï»ùÃÅ¡ÖÂ±ΩÖêÅ•ÃÅπΩ–Å—°îÅÕΩ’…çî(ÄÄÄÅΩòÅ—…’—†Å°ï…îËÅ—°îÅÖ¡¡Ω•π—µïπ–ÅçÖç°ïêÅΩ∏Å—°îÅI4ÅçΩπ—Öç–Å•ÃÅ±ΩΩ≠ïêÅ’¿ÅÖùÖ•∏(ÄÄÄÅ•µµïë•Ö—ï±‰ÅâïôΩ…îÅ—°îÅÕ’µµÖ…‰ÅîµµÖ•∞Å•ÃÅùïπï…Ö—ïê∏(ÄÄÄÄààà(ÄÄÄÅµÖ—ç†ÄÙÅ}ç…µ}πï·—}¡°Ωπï}Ö¡¡Ω•π—µïπ–°ëÖ—Ñ∞Åïπ—…‰∞ÅçΩπ—Öç–§(ÄÄÄÅ•òÅπΩ–ÅµÖ—ç†Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(ÄÄÄÅÕ—Ö…–∞ÅÖ¡¡Ω•π—µïπ–ÄÙÅµÖ—ç†(ÄÄÄÅçΩπ—Öç—}•êÄÙÅÕ—»°çΩπ—Öç–πùï–†â•êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅïπ—…‰π’¡ëÖ—î°Ï(ÄÄÄÄÄÄÄÄâ…ëÿàËÅ}ç…µ}çÖ±ïπë±Â}ëÖ—ï—•µï}±Öâï∞°Ö¡¡Ω•π—µïπ–πùï–†âÕ—Ö…—}—•µîà§§∞(ÄÄÄÄÄÄÄÄâ…ëŸ}Õ—Ö—’ÃàËÄâÕç°ïë’±ïêà∞(ÄÄÄÄÄÄÄÄâ…ëŸ}ëÖ—îàËÅÕ—Ö…–πÕ—…ô—•µî†àïêºï¥ºïdà§∞(ÄÄÄÄÄÄÄÄâ…ëŸ}—•µîàËÅÕ—Ö…–πÕ—…ô—•µî†àï Ëï4à§∞(ÄÄÄÄÄÄÄÄâ…ëŸ}µΩëîàËÄâ¡¡ï∞Å”•≥•¡°Ωπ•≈’îà∞(ÄÄÄÄÄÄÄÄâ…ëŸ}’…∞àËÄàà∞(ÄÄÄÄÄÄÄÄâ…ëŸ}πÖµîàËÅÖ¡¡Ω•π—µïπ–πùï–†âπÖµîà§ÅΩ»ÄâIïπëïËµŸΩ’ÃÅ”•≥•¡°Ωπ•≈’îà∞(ÄÄÄÄÄÄÄÄâ…ëŸ}°ΩÕ—}πÖµîàËÅÖ¡¡Ω•π—µïπ–πùï–†â°ΩÕ—}πÖµîà§ÅΩ»Äàà∞(ÄÄÄÅÙ§(ÄÄÄÄåÅIï¡Ö•»ÅÑÅÕ—Ö±îΩ’πÖÕÕ•ùπïêÅÖ±ïπë±‰Å±•π¨ÅÕºÅ—°îÅÖ¡¡Ω•π—µïπ–Å•ÃÅŸ•Õ•â±îÅΩ∏(ÄÄÄÄåÅ—°îÅI4Å…ïçΩ…êÅ—°Ö–Å›ÖÃÅ©’Õ–Åç…ïÖ—ïêÅô…Ω¥Å—°îÅÕïç…ï—Ö…•Ö–ÅÕ’âµ•ÕÕ•Ω∏∏(ÄÄÄÅ±•π≠ïë}çΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅÖ¡¡Ω•π—µïπ–πùï–†âçΩπ—Öç—}•êà§§(ÄÄÄÅÖ±…ïÖëÂ}±•π≠ïêÄÙÅÕ—»°Ö¡¡Ω•π—µïπ–πùï–†âçΩπ—Öç—}•êà§ÅΩ»Äàà§ÄÙÙÅçΩπ—Öç—}•ê(ÄÄÄÅ•òÅçΩπ—Öç—}•êÅÖπêÄ°πΩ–Å±•π≠ïë}çΩπ—Öç–ÅΩ»ÅÖ±…ïÖëÂ}±•π≠ïê§Ë(ÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—lâçΩπ—Öç—}•êâtÄÙÅçΩπ—Öç—}•ê(ÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅçΩπ—Öç—lâôΩ…µ’±Ö•…îâtÄÙÅÏ®®°çΩπ—Öç–πùï–†âôΩ…µ’±Ö•…îà§ÅΩ»ÅÌÙ§∞Ä®©ïπ—…ÂÙ(ÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅ…ï—’…∏ÅÖ¡¡Ω•π—µïπ–(()ëïòÅ}Õïç…ï—Ö…•Ö—}…ïô…ïÕ°}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Ã°ëÖ—Ñ∞Åïπ—…‰∞ÅçΩπ—Öç–§Ë(ÄÄÄÄààâIïô…ïÕ†Å—°îÅI4Å…ïçΩ…êÅô…Ω¥ÅÖ±ïπë±‰ÅâïôΩ…îÅâ’•±ë•πúÅ—°îÅÕ’µµÖ…‰ÅïµÖ•∞∏((ÄÄÄÅQ°îÅ›ïâ°ΩΩ¨ÅçÖç°îÅ•ÃÅπΩ…µÖ±±‰Åç’……ïπ–∞Åâ’–ÅÑÅâΩΩ≠•πúÅµÖëîÅ›°•±îÅ—°î(ÄÄÄÅÕïç…ï—Ö…‰Å•ÃÅçΩµ¡±ï—•πúÅ—°îÅôΩ…¥ÅçÖ∏ÅÖ……•ŸîÅÖô—ï»Å—°îÅô•πÖ∞Åâ’——Ω∏Å•Ã(ÄÄÄÅç±•ç≠ïê∏ÄÅÅ—Ö…ùï—ïêÅ±ΩΩ≠’¿Åç±ΩÕïÃÅ—°Ö–Å…Öçî∏ÄÅÖ±ïπë±‰ÅôÖ•±’…ïÃÅ…ïµÖ•∏(ÄÄÄÅπΩ∏µâ±Ωç≠•πúËÅ—°îÅÖ±…ïÖë‰ÅçÖç°ïêÅÖ¡¡Ω•π—µïπ—ÃÅÖ…îÅÕ—•±∞Å’ÕïêÅâ‰Å—°îÅπï·–(ÄÄÄÅÕ—ï¿ÅÖπêÅ—°îÅëï±•Ÿï…‰ÅçÖ∏ÅçΩπ—•π’î∏(ÄÄÄÄààà(ÄÄÄÅÕ—Ö—îÄÙÅëÖ—Ñπùï–†âç…µ}çÖ±ïπë±‰à§ÅΩ»ÅÌÙ(ÄÄÄÅçÖπ}±ΩΩ≠’¿ÄÙÅâΩΩ∞†(ÄÄÄÄÄÄÄÅ}çÖ±ïπë±Â}—Ω≠ï∏†§(ÄÄÄÄÄÄÄÅÖπêÅÕ—Ö—îπùï–†â’Õï»à§(ÄÄÄÄÄÄÄÅÖπêÅÕ—Ö—îπùï–†âΩ…ùÖπ•ÈÖ—•Ω∏à§(ÄÄÄÄÄÄÄÅÖπêÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}πΩ…µÖ±•Èï}ïµÖ•∞°çΩπ—Öç–πùï–†âµÖ•∞à§ÅΩ»Åïπ—…‰πùï–†âïµÖ•∞à§§(ÄÄÄÄÄÄÄÄÄÄÄÅΩ»Å}ç…µ}πΩ…µÖ±•Èï}¡°Ωπî°çΩπ—Öç–πùï–†â—ï±ï¡°Ωπîà§ÅΩ»Åïπ—…‰πùï–†â—ï±ï¡°Ωπîà§§(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄ§(ÄÄÄÅ•òÅπΩ–ÅçÖπ}±ΩΩ≠’¿Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Ä¿((ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëÃ∞Å}±ΩΩ≠’¿ÄÙÅ}ç…µ}çÖ±ïπë±Â}ôï—ç°}çΩπ—Öç—}Ö¡¡Ω•π—µïπ—Ã°ëÖ—Ñ∞ÅçΩπ—Öç–§(ÄÄÄÅï·çï¡–Ä°Ö±ïπë±ÂA%……Ω»∞ÅI’π—•µï……Ω»§ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅïπ—…ÂlâçÖ±ïπë±Â}±ΩΩ≠’¡}›Ö…π•πúâtÄÙÅÕ—»°ï·å§(ÄÄÄÄÄÄÄÅ…ï—’…∏Ä¿((ÄÄÄÅôΩ»Å¡ÖÂ±ΩÖêÅ•∏Å¡ÖÂ±ΩÖëÃË(ÄÄÄÄÄÄÄÅ}ç…µ}’¡Õï…—}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ–†(ÄÄÄÄÄÄÄÄÄÄÄÅëÖ—Ñ∞(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖê∞(ÄÄÄÄÄÄÄÄÄÄÄÅÕΩ’…çîÙâÕïç…ï—Ö…•Ö—}—Ö…ùï—ïë}±ΩΩ≠’¿à∞(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—}•êıçΩπ—Öç–πùï–†â•êà§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïçΩ…ë}Öç—•Ÿ•—‰ıÖ±Õî∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅ}ç…µ}çÖ±ïπë±Â}…ï±•π≠}Ö¡¡Ω•π—µïπ—Ã°ëÖ—Ñ∞ÅçΩπ—Öç–§(ÄÄÄÅ}ç…µ}ÕÂπç}çΩπ—Öç—}çÖ±ïπë±Â}Õ—Ö—’Ã°ëÖ—Ñ∞ÅçΩπ—Öç–§(ÄÄÄÅ…ï—’…∏Å±ï∏°¡ÖÂ±ΩÖëÃ§(()ëïòÅ}µÖÕ≠}ëï±•Ÿï…Â}…ïç•¡•ïπ–°ŸÖ±’î∞Å≠•πê§Ë(ÄÄÄÅŸÖ±’îÄÙÅÕ—»°ŸÖ±’îÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅ≠•πêÄÙÙÄâïµÖ•∞àÅÖπêÄâ àÅ•∏ÅŸÖ±’îË(ÄÄÄÄÄÄÄÅ±ΩçÖ∞∞ÅëΩµÖ•∏ÄÙÅŸÖ±’îπÕ¡±•–†â à∞Äƒ§(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅòâÌ±ΩçÖ±lË…uÙ®®©ÌëΩµÖ•πÙà(ÄÄÄÅë•ù•—ÃÄÙÅ…îπÕ’à°»âqà∞Äàà∞ÅŸÖ±’î§(ÄÄÄÅ…ï—’…∏Åòà®®©Ìë•ù•—Õl¥–ÈuÙàÅ•òÅë•ù•—ÃÅï±ÕîÄâÖâÕïπ–à(()ëïòÅ}Õïç…ï—Ö…•Ö—}Ö’—ΩµÖ—•ç}•πôΩ…µÖ—•Ωπ}—ïµ¡±Ö—î°ëÖ—Ñ∞Åïπ—…‰∞ÅçΩπ—Öç–§Ë(ÄÄÄÄààâ	’•±êÅ—°îÅï·•Õ—•πúÅÖ’—ΩµÖ—•åÅôΩ…¥ÅîµµÖ•∞ÅôΩ»ÅÑÅÕïç…ï—Ö…•Ö–ÅçÖ±∞∏ààà(ÄÄÄÅôΩ…µÖ—•Ωπ}çΩëîÄÙÅÕ—»°ïπ—…‰πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ¡…ïπΩ¥ÄÙÅ}Õïç…ï—Ö…•Ö—}ë•Õ¡±ÖÂ}ô•…Õ—}πÖµî†(ÄÄÄÄÄÄÄÅçΩπ—Öç–πùï–†â¡…ïπΩ¥à§ÅΩ»Åïπ—…‰πùï–†â¡…ïπΩ¥à§(ÄÄÄÄÄÄÄÅΩ»ÅÕ—»°ïπ—…‰πùï–†âπΩ¥à§ÅΩ»Äàà§πÕ¡±•–†àÄà•l¡t(ÄÄÄÄ§(ÄÄÄÅëÖ—ïÃÄÙÅÕ—»†(ÄÄÄÄÄÄÄÅïπ—…‰πùï–†âôΩ…µÖ—•Ωπ}ÕïÕÕ•Ωπ}±Öâï∞à§(ÄÄÄÄÄÄÄÅΩ»Åïπ—…‰πùï–†âôΩ…µÖ—•Ωπ}ëÖ—ï}ÕΩ’°Ö•—ïîà§(ÄÄÄÄÄÄÄÅΩ»Äàà(ÄÄÄÄ§πÕ—…•¿†§(ÄÄÄÅçïπ—…ï}çΩëî∞Å|ÄÙÅ}Õïç…ï—Ö…•Ö—}ÕïÕÕ•Ωπ}ëï—Ö•±Ã°ïπ—…‰§(ÄÄÄÅëïŸ•Õ}’…∞ÄÙÅÕ—»°ïπ—…‰πùï–†âëïŸ•Õ}’…∞à§ÅΩ»ÅçΩπ—Öç–πùï–†âëïŸ•Õ}’…∞à§ÅΩ»Äàà§πÕ—…•¿†§((ÄÄÄÅ•òÅôΩ…µÖ—•Ωπ}çΩëîÄÙÙÄâMA}YàË(ÄÄÄÄÄÄÄÅ—ïµ¡±Ö—ï}•êÄÙÄâÖ’—ΩµÖ—•åµëïÕ¿µŸÖîà(ÄÄÄÄÄÄÄÅÕ’â©ïç–ÄÙÄã¬~NtÅYÉäLÅ•…•ùïÖπ–Åìäeπ—…ï¡…•ÕîÅëîÅO•ç’…•”§ÅA…•€•îÄ°I9@–¿Ã‡‘§à(ÄÄÄÄÄÄÄÅâΩë‰ÄÙÅâ’•±ë}ŸÖï}ëïÕ¡}ïµÖ•±}°—µ∞°¡…ïπΩ¥∞ÅëïŸ•Õ}’…∞§(ÄÄÄÅï±•òÅôΩ…µÖ—•Ωπ}çΩëîÄÙÙÄâÕ@àË(ÄÄÄÄÄÄÄÅ—ïµ¡±Ö—ï}•êÄÙÄâÖ’—ΩµÖ—•åµÑÕ¿à(ÄÄÄÄÄÄÄÅÕ’â©ïç–∞Å|∞ÅâΩë‰ÄÙÅ}ÑÕ¡}•πôΩ…µÖ—•Ωπ}ïµÖ•±}çΩπ—ïπ–†(ÄÄÄÄÄÄÄÄÄÄÄÅ¡…ïπΩ¥∞ÅëÖ—ïÃ∞Åçïπ—…ï}çΩëî∞ÅëïŸ•Õ}’…∞∞ÅëÖ—Ñ∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅï±•òÅôΩ…µÖ—•Ωπ}çΩëîÄÙÙÄâALàË(ÄÄÄÄÄÄÄÅ—ïµ¡±Ö—ï}•êÄÙÄâÖ’—ΩµÖ—•åµÖ¡Ãà(ÄÄÄÄÄÄÄÅÕ’â©ïç–ÄÙÄã¬~Fªä7äfæ‚<ÅΩ…µÖ—•Ω∏Åùïπ–ÅëîÅO•ç’…•”§ÅA…•€•îÄ°AL§à(ÄÄÄÄÄÄÄÅâΩë‰ÄÙÅâ’•±ë}Ö¡Õ}ïµÖ•±}°—µ∞°¡…ïπΩ¥∞ÅëÖ—ïÃ∞Åçïπ—…ï}çΩëî∞ÅëïŸ•Õ}’…∞§(ÄÄÄÅï±•òÅôΩ…µÖ—•Ωπ}çΩëîÄÙÙÄâMM%@àË(ÄÄÄÄÄÄÄÅ—ïµ¡±Ö—ï}•êÄÙÄâÖ’—ΩµÖ—•åµÕÕ•Ö¿ƒà(ÄÄÄÄÄÄÄÅÕ’â©ïç–ÄÙÄã¬~RîÅΩ…µÖ—•Ω∏Åùïπ–ÅëîÅœ•ç’…•”§Å•πçïπë•îÅMM%@Äƒà(ÄÄÄÄÄÄÄÅâΩë‰ÄÙÅâ’•±ë}ÕÕ•Ö¿≈}ïµÖ•±}°—µ∞†(ÄÄÄÄÄÄÄÄÄÄÄÅ¡…ïπΩ¥∞ÅëÖ—ïÃ∞Åçïπ—…ï}çΩëî∞ÅëïŸ•Õ}’…∞∞(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…‰πùï–†âÕÕ•Ö¡}ÕïçΩ’…•Õµï}ŸÖ±•ëîà∞Äàà§∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅï±•òÅôΩ…µÖ—•Ωπ}çΩëîÄÙÙÄâYQàË(ÄÄÄÄÄÄÄÅ—ïµ¡±Ö—ï}•êÄÙÄâÖ’—ΩµÖ—•åµŸ—åà(ÄÄÄÄÄÄÄÅÕ’â©ïç–ÄÙÄã¬~j\ÅΩ…µÖ—•Ω∏Å°Ö’ôôï’»ÅYQà(ÄÄÄÄÄÄÄÅâΩë‰ÄÙÅâ’•±ë}Ÿ—ç}ïµÖ•±}°—µ∞°¡…ïπΩ¥∞Åçïπ—…ï}çΩëî∞ÅëïŸ•Õ}’…∞§(ÄÄÄÅï±•òÅôΩ…µÖ—•Ωπ}çΩëîÄÙÙÄâMA}%9%PàË(ÄÄÄÄÄÄÄÅ—ïµ¡±Ö—ï}•êÄÙÄâÖ’—ΩµÖ—•åµëïÕ¿µ•π•—•Ö∞à(ÄÄÄÄÄÄÄÅÕ’â©ïç–ÄÙÄâYΩ—…îÅëïµÖπëîÅëîÅ…ïπÕï•ùπïµïπ—ÃÉäLÅΩ…µÖ—•Ω∏ÅM@Å•π•—•Ö∞à(ÄÄÄÄÄÄÄÅâΩë‰ÄÙÅâ’•±ë}ëïÕ¡}•π•—}ïµÖ•±}°—µ∞†(ÄÄÄÄÄÄÄÄÄÄÄÅ¡…ïπΩ¥∞ÅëÖ—ïÃ∞Åçïπ—…ï}çΩëî∞ÅëïŸ•Õ}’…∞∞ÅëÖ—Ñ∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄåÅ1ïÃÅ	QLÅ∏ùΩπ–Å¡ÖÃÅïπçΩ…îÅëîÅµΩì°±îÅÖ’—ΩµÖ—•≈’îÅì•ëß§ÅëÖπÃÅ±îÅI4∏(ÄÄÄÄÄÄÄÄåÅ=∏Å…ï¡…ïπêÅëΩπåÅ±îÅµïÕÕÖùîÅü•ª•…•≈’îÅì•´ÄÅïπŸΩÁ§Å¡Ö»Å±îÅôΩ…µ’±Ö•…î(ÄÄÄÄÄÄÄÄåÅ¡’â±•å∞ÅÖô•∏Å≈‘ù’πîÅÖâÕïπçîÅëîÅµΩì°±îÅ¡ï…ÕΩππÖ±•œ§ÅπîÅâ±Ω≈’îÅ©ÖµÖ•Ã(ÄÄÄÄÄÄÄÄåÅÕ•±ïπç•ï’Õïµïπ–Å∞ùîµµÖ•∞Åë‘ÅÕïçÀ•—Ö…•Ö–∏(ÄÄÄÄÄÄÄÅôΩ…µÖ—•Ω∏ÄÙÅ}Õïç…ï—Ö…•Ö—}ôΩ…µÖ—•Ωπ}çΩπô•ú°ôΩ…µÖ—•Ωπ}çΩëî§(ÄÄÄÄÄÄÄÅôΩ…µÖ—•Ωπ}±Öâï∞ÄÙÅôΩ…µÖ—•Ω∏πùï–†â±Öâï∞à§ÅΩ»ÅôΩ…µÖ—•Ω∏πùï–†âÕ°Ω…–à§ÅΩ»ÄâΩ…µÖ—•Ω∏Å%π”•ù…Ö±îÅçÖëïµ‰à(ÄÄÄÄÄÄÄÅÕïÕÕ•Ωπ}°—µ∞ÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅòàÒ¿˘MïÕÕ•Ω∏ÅÕΩ’°Ö•”•îÄËÄÒÕ—…Ωπú˘Ì°—µ±}µΩë’±îπïÕçÖ¡î°ëÖ—ïÃ•ÙΩÕ—…Ωπú¯Ω¿¯à(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅëÖ—ïÃÅï±ÕîÄàà(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅëïŸ•Õ}°—µ∞ÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄúÒ¿ÅÕ—Â±îÙâ—ï·–µÖ±•ù∏Èçïπ—ï»Ïà¯ú(ÄÄÄÄÄÄÄÄÄÄÄÅòúÒÑÅ°…ïòÙâÌ°—µ±}µΩë’±îπïÕçÖ¡î°ëïŸ•Õ}’…∞∞Å≈’Ω—îıQ…’î•ÙàÄú(ÄÄÄÄÄÄÄÄÄÄÄÄùÕ—Â±îÙâë•Õ¡±Ö‰È•π±•πîµâ±Ωç¨Ì¡Öëë•πúËƒ…¡‡Äƒ·¡‡ÌâÖç≠ù…Ω’πêËå¡êŸïôêÌçΩ±Ω»ËçôôòÏú(ÄÄÄÄÄÄÄÄÄÄÄÄùâΩ…ëï»µ…Öë•’ÃËƒ¡¡‡Ì—ï·–µëïçΩ…Ö—•Ω∏ÈπΩπîÌôΩπ–µ›ï•ù°–Ë‹¿¿Ïà¯ú(ÄÄÄÄÄÄÄÄÄÄÄÄâ)îÅ”•≥•ç°Ö…ùîÅµΩ∏ÅëïŸ•ÃÅì•—Ö•±≥§ΩÑ¯Ω¿¯à(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅëïŸ•Õ}’…∞Åï±ÕîÄàà(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ—ïµ¡±Ö—ï}•êÄÙÅòâÖ’—ΩµÖ—•åµÕïç…ï—Ö…•Ö–µÌôΩ…µÖ—•Ωπ}çΩëîπ±Ω›ï»†§π…ï¡±Öçî†ù|ú∞Äú¥ú•Ùà(ÄÄÄÄÄÄÄÅÕ’â©ïç–ÄÙÄâYΩ—…îÅëïµÖπëîÅëîÅ…ïπÕï•ùπïµïπ—ÃÉäLÅ%π”•ù…Ö±îÅçÖëïµ‰à(ÄÄÄÄÄÄÄÅâΩë‰ÄÙÅ}›…Ö¡}°—µ∞†(ÄÄÄÄÄÄÄÄÄÄÄÄàÒ†ƒ˚är†Å5ï…ç§Å¡Ω’»ÅŸΩ—…îÅëïµÖπëîΩ†ƒ¯à∞(ÄÄÄÄÄÄÄÄÄÄÄÅòààà(ÄÄÄÄÄÄÄÄÄÄÄÄÒ¿˘	Ωπ©Ω’»ÄÒÕ—…Ωπú˘Ì°—µ±}µΩë’±îπïÕçÖ¡î°¡…ïπΩ¥•ÙΩÕ—…Ωπú¯∞Ω¿¯(ÄÄÄÄÄÄÄÄÄÄÄÄÒ¿˘)îÅôÖ•ÃÅÕ’•—îÉÄÅŸΩ—…îÅëïµÖπëîÅëîÅ…ïπÕï•ùπïµïπ—ÃÅçΩπçï…πÖπ–ÅπΩ—…îÅôΩ…µÖ—•Ω∏(ÄÄÄÄÄÄÄÄÄÄÄÄÒÕ—…Ωπú˘Ì°—µ±}µΩë’±îπïÕçÖ¡î°ôΩ…µÖ—•Ωπ}±Öâï∞•ÙΩÕ—…Ωπú¯∏Å9Ω’ÃÅŸΩ’ÃÅ…ïµï…ç•ΩπÃÅëîÅπΩ’ÃÅÖŸΩ•»ÅçΩπ—Öç”§ÄÑΩ¿¯(ÄÄÄÄÄÄÄÄÄÄÄÅÌÕïÕÕ•Ωπ}°—µ±Ù(ÄÄÄÄÄÄÄÄÄÄÄÄÒ¿˘YΩ’ÃÅ¡Ω’ŸïËÅçΩπÕ’±—ï»Å±îÅëΩÕÕ•ï»ÅëîÅ¡À•Õïπ—Ö—•Ω∏ÅëîÅπΩÃÅôΩ…µÖ—•ΩπÃÄËΩ¿¯(ÄÄÄÄÄÄÄÄÄÄÄÄÒ¿¯ÒÑÅ°…ïòÙâÌMIQI%Q}=MM%I}UI1Ùà˘ÌMIQI%Q}=MM%I}UI1ÙΩÑ¯Ω¿¯(ÄÄÄÄÄÄÄÄÄÄÄÅÌëïŸ•Õ}°—µ±Ù(ÄÄÄÄÄÄÄÄÄÄÄÄÒ¿˘9Ω—…îÉ•≈’•¡îÅ…ïÕ—îÉÄÅŸΩ—…îÅë•Õ¡ΩÕ•—•Ω∏ÅÖ‘ÄÒÕ—…Ωπú¯¿–Ä»»Ä–‹Ä¿‹Äÿ‡ΩÕ—…Ωπú¯∏Ω¿¯(ÄÄÄÄÄÄÄÄÄÄÄÄÒ¿˘)îÅŸΩ’ÃÅÕΩ’°Ö•—îÅ’πîÅâΩππîÅ©Ω’…ª•î∞Ω¿¯(ÄÄÄÄÄÄÄÄÄÄÄÄÒ¿¯ÒÕ—…Ωπú˘≥•µïπ–ÅY%119PΩÕ—…Ωπú¯Òâ»˘•…ïç—ï’»Å%π”•ù…Ö±îÅçÖëïµ‰Ω¿¯(ÄÄÄÄÄÄÄÄÄÄÄÄààà∞(ÄÄÄÄÄÄÄÄ§((ÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄâ•êàËÅ—ïµ¡±Ö—ï}•ê∞(ÄÄÄÄÄÄÄÄâπΩ¥àËÅòâµµÖ•∞ÅÖ’—ΩµÖ—•≈’îÅÌôΩ…µÖ—•Ωπ}çΩëïÙà∞(ÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏àËÅôΩ…µÖ—•Ωπ}çΩëî∞(ÄÄÄÄÄÄÄÄâÕ’©ï–àËÅÕ’â©ïç–∞(ÄÄÄÄÄÄÄÄâçΩπ—ïπ‘àËÅâΩë‰∞(ÄÄÄÅÙ(()ëïòÅ}Õïç…ï—Ö…•Ö—}•πôΩ…µÖ—•Ωπ}ïµÖ•∞°ëÖ—Ñ∞Åïπ—…‰∞ÅçΩπ—Öç–§Ë(ÄÄÄÄààâ	’•±êÅ—°îÅI4Å•πôΩ…µÖ—•Ω∏ÅîµµÖ•∞ÅµÖ—ç°•πúÅ—°îÅÕï±ïç—ïêÅ—…Ö•π•πú∏ààà(ÄÄÄÅ—ïµ¡±Ö—îÄÙÅ}Õïç…ï—Ö…•Ö—}•πôΩ…µÖ—•Ωπ}—ïµ¡±Ö—î†(ÄÄÄÄÄÄÄÅëÖ—Ñ∞ÄâïµÖ•∞à∞Åïπ—…‰πùï–†âôΩ…µÖ—•Ω∏à§∞(ÄÄÄÄ§(ÄÄÄÅ•òÅπΩ–Å—ïµ¡±Ö—îË(ÄÄÄÄÄÄÄÅ—ïµ¡±Ö—îÄÙÅ}Õïç…ï—Ö…•Ö—}Ö’—ΩµÖ—•ç}•πôΩ…µÖ—•Ωπ}—ïµ¡±Ö—î°ëÖ—Ñ∞Åïπ—…‰∞ÅçΩπ—Öç–§((ÄÄÄÅâΩë‰ÄÙÅ}ç…µ}…ïÕΩ±Ÿï}µïÕÕÖùï}ŸÖ…•Öâ±ïÃ†(ÄÄÄÄÄÄÄÅ—ïµ¡±Ö—îπùï–†âçΩπ—ïπ‘à∞Äàà§∞ÅçΩπ—Öç–∞Å°—µ∞ıQ…’î∞ÅëÖ—Ö}Õ—Ω…îıëÖ—Ñ∞(ÄÄÄÄ§πÕ—…•¿†§(ÄÄÄÅ≈’Ω—ï}’…∞ÄÙÅÕ—»°ïπ—…‰πùï–†âëïŸ•Õ}’…∞à§ÅΩ»ÅçΩπ—Öç–πùï–†âëïŸ•Õ}’…∞à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅôΩ»ÅŸÖ…•Öâ±îÅ•∏Ä†âÌÏÅ±•ïπ}ëïŸ•ÃÅıÙà∞ÄâÌÌ±•ïπ}ëïŸ•ÕıÙà§Ë(ÄÄÄÄÄÄÄÅâΩë‰ÄÙÅâΩë‰π…ï¡±Öçî°ŸÖ…•Öâ±î∞Å°—µ±}µΩë’±îπïÕçÖ¡î°≈’Ω—ï}’…∞∞Å≈’Ω—îıQ…’î§§((ÄÄÄÅÕ’â©ïç–ÄÙÅ}ç…µ}…ïÕΩ±Ÿï}µïÕÕÖùï}ŸÖ…•Öâ±ïÃ†(ÄÄÄÄÄÄÄÅ—ïµ¡±Ö—îπùï–†âÕ’©ï–à§ÅΩ»Äâ%π”•ù…Ö±îÅçÖëïµ‰à∞(ÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÅëÖ—Ö}Õ—Ω…îıëÖ—Ñ∞(ÄÄÄÄ§πÕ—…•¿†§(ÄÄÄÅ¡±Ö•∏ÄÙÅ°—µ±}µΩë’±îπ’πïÕçÖ¡î†(ÄÄÄÄÄÄÄÅ…îπÕ’à°»âqÃ¨à∞ÄàÄà∞Å…îπÕ’à°»àÒmx˘t¨¯à∞ÄàÄà∞ÅâΩë‰§§(ÄÄÄÄ§πÕ—…•¿†§((ÄÄÄÅ•òÅ…îπÕïÖ…ç†°»à†¸ËÖëΩç—Â¡ïÒ°—µ∞•qàà∞ÅâΩë‰∞Å…îπ%9=IM§Ë(ÄÄÄÄÄÄÄÅâ…ÖπëïêÄÙÅâΩë‰(ÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÅôΩ…µÖ—•Ω∏ÄÙÅ}Õïç…ï—Ö…•Ö—}ôΩ…µÖ—•Ωπ}çΩπô•ú°ïπ—…‰πùï–†âôΩ…µÖ—•Ω∏à§§(ÄÄÄÄÄÄÄÅâ…ÖπëïêÄÙÅ…ïπëï…}—ïµ¡±Ö—î†(ÄÄÄÄÄÄÄÄÄÄÄÄâç…µ}ïµÖ•±}›…Ö¡¡ï»π°—µ∞à∞(ÄÄÄÄÄÄÄÄÄÄÄÅ¡…ïπΩ¥ıçΩπ—Öç–πùï–†â¡…ïπΩ¥à§ÅΩ»Åïπ—…‰πùï–†â¡…ïπΩ¥à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ…µÖ—•Ω∏ıôΩ…µÖ—•Ω∏πùï–†â±Öâï∞à§ÅΩ»ÅôΩ…µÖ—•Ω∏πùï–†âÕ°Ω…–à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—ïπ‘ıâΩë‰∞(ÄÄÄÄÄÄÄÄÄÄÄÅïµÖ•±}°ïÖëï…}—•—±îÙâ%πôΩ…µÖ—•ΩπÃÅÕ’»ÅŸΩ—…îÅôΩ…µÖ—•Ω∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÅïµÖ•±}°ïÖëï…}Õ’â—•—±îıôΩ…µÖ—•Ω∏πùï–†â±Öâï∞à§ÅΩ»ÅôΩ…µÖ—•Ω∏πùï–†âÕ°Ω…–à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅ…ï—’…∏Å—ïµ¡±Ö—î∞ÅÕ’â©ïç–∞Å¡±Ö•∏∞Åâ…Öπëïê(()ëïòÅ}Õïπë}Õïç…ï—Ö…•Ö—}•πôΩ…µÖ—•Ωπ}µïÕÕÖùïÃ°ëÖ—Ñ∞Åïπ—…‰∞ÅçΩπ—Öç–§Ë(ÄÄÄÄààâï±•Ÿï»ÅïÖç†Åç°Öππï∞Å•πëï¡ïπëïπ—±‰ÅÖπêÅ¡ï…Õ•Õ–ÅÖ∏Å•ëïµ¡Ω—ïπ–ÅÖ’ë•–Å—…Ö•∞∏ààà(ÄÄÄÅ…ïÕ’±—ÃÄÙÅÌÙ(ÄÄÄÅôΩ»Å≠•πê∞Å…ïç•¡•ïπ—}≠ï‰∞Å¡…ΩŸ•ëï»Å•∏Ä††âïµÖ•∞à∞ÄâïµÖ•∞à∞ÄâM5Q@Ω	…ïŸºà§∞Ä†âÕµÃà∞Äâ—ï±ï¡°Ωπîà∞Äâ	…ïŸºà§§Ë(ÄÄÄÄÄÄÄÅ…ïç•¡•ïπ–ÄÙÅïπ—…‰πùï–°…ïç•¡•ïπ—}≠ï‰§(ÄÄÄÄÄÄÄÅÕ—Ö—’Õ}≠ï‰ÄÙÅòâÌ≠•πëı}Õ’µµÖ…Â}Õ—Ö—’Ãà(ÄÄÄÄÄÄÄÅÕïπ—}≠ï‰ÄÙÅòâÌ≠•πëı}Õ’µµÖ…Â}Õïπ—}Ö–à(ÄÄÄÄÄÄÄÅï……Ω…}≠ï‰ÄÙÅòâÌ≠•πëı}Õ’µµÖ…Â}ï……Ω»à(ÄÄÄÄÄÄÄÅÖ——ïµ¡—ïë}≠ï‰ÄÙÅòâÌ≠•πëı}Õ’µµÖ…Â}Ö——ïµ¡—ïë}Ö–à(ÄÄÄÄÄÄÄÅ±ïùÖçÂ}Õïπ—}≠ï‰ÄÙÅòâ•πôΩ…µÖ—•Ωπ}Ì≠•πëı}Õïπ—}Ö–à(ÄÄÄÄÄÄÄÅ•òÅïπ—…‰πùï–°Õïπ—}≠ï‰§ÅΩ»Åïπ—…‰πùï–°±ïùÖçÂ}Õïπ—}≠ï‰§ÅΩ»Åïπ—…‰πùï–°òâ•πôΩ…µÖ—•Ωπ}Ì≠•πëı}—ïµ¡±Ö—ï}•êà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕ’±—Õm≠•πëtÄÙÄâÖ±…ïÖëÂ}Õïπ–à(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å…ïç•¡•ïπ–ÅΩ»Ä°≠•πêÄÙÙÄâÕµÃàÅÖπêÅπΩ–Å}πΩ…µÖ±•Õï…}—ï±ï¡°Ωπï}ÕµÃ°…ïç•¡•ïπ–§§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…ÂmÕ—Ö—’Õ}≠ïÂtÄÙÄâôÖ•±ïêà(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Âmï……Ω…}≠ïÂtÄÙÄâïÕ—•πÖ—Ö•…îÅÖâÕïπ–ÅΩ‘Å•πŸÖ±•ëîà(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…ÂmÖ——ïµ¡—ïë}≠ïÂtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕ’±—Õm≠•πëtÄÙÄâ…ïç•¡•ïπ—}µ•ÕÕ•πúà(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î((ÄÄÄÄÄÄÄÅ•πôΩ…µÖ—•Ωπ}ïµÖ•∞ÄÙÅ9Ωπî(ÄÄÄÄÄÄÄÅ•òÅ≠•πêÄÙÙÄâïµÖ•∞àË(ÄÄÄÄÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•πôΩ…µÖ—•Ωπ}ïµÖ•∞ÄÙÅ}Õïç…ï—Ö…•Ö—}•πôΩ…µÖ—•Ωπ}ïµÖ•∞°ëÖ—Ñ∞Åïπ—…‰∞ÅçΩπ—Öç–§(ÄÄÄÄÄÄÄÄÄÄÄÅï·çï¡–Å·çï¡—•Ω∏ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅïπ—…ÂmÕ—Ö—’Õ}≠ïÂtÄÙÄâôÖ•±ïêà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Âmï……Ω…}≠ïÂtÄÙÅÕ—»°ï·å•lË‘¿¡t(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅïπ—…ÂmÖ——ïµ¡—ïë}≠ïÂtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕ’±—Õm≠•πëtÄÙÄâôÖ•±ïêà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–Å•πôΩ…µÖ—•Ωπ}ïµÖ•∞Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅïπ—…ÂmÕ—Ö—’Õ}≠ïÂtÄÙÄâôÖ•±ïêà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Âmï……Ω…}≠ïÂtÄÙÄâ’ç’∏ÅµΩì°±îÅìäe•πôΩ…µÖ—•Ω∏ÅπîÅçΩ……ïÕ¡ΩπêÉÄÅ±ÑÅôΩ…µÖ—•Ω∏à(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅïπ—…ÂmÖ——ïµ¡—ïë}≠ïÂtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕ’±—Õm≠•πëtÄÙÄâ—ïµ¡±Ö—ï}µ•ÕÕ•πúà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î((ÄÄÄÄÄÄÄÅïπ—…ÂmÕ—Ö—’Õ}≠ïÂtÄÙÄâÕïπë•πúà(ÄÄÄÄÄÄÄÅïπ—…Âmï……Ω…}≠ïÂtÄÙÄàà(ÄÄÄÄÄÄÄÅïπ—…ÂmÖ——ïµ¡—ïë}≠ïÂtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ≠•πêÄÙÙÄâïµÖ•∞àË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—ïµ¡±Ö—î∞ÅÕ’â©ïç–∞ÅâΩë‰∞Åâ…ÖπëïêÄÙÅ•πôΩ…µÖ—•Ωπ}ïµÖ•∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ¨ÄÙÅ}ç…µ}Õïπë}ïµÖ•±}°—µ∞†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ïç•¡•ïπ–∞ÅÕ’â©ïç–∞ÅâΩë‰∞Åâ…Öπëïê∞Å—ïµ¡±Ö—îı—ïµ¡±Ö—î∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡…ïŸ•ï‹∞Åëï—Ö•∞∞Å—•—±îÄÙÅâ…Öπëïê∞ÅÕ’â©ïç–∞ÄâµµÖ•∞Åìäe•πôΩ…µÖ—•Ω∏ÅïπŸΩÁ§à(ÄÄÄÄÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅâΩë‰ÄÙÅ}â’•±ë}Õïç…ï—Ö…•Ö—}ôΩ±±Ω›’¡}ÕµÃ°ïπ—…‰πùï–†âôΩ…µÖ—•Ω∏à§§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ¨ÄÙÅÕïπë}ÕµÃ°…ïç•¡•ïπ–∞ÅâΩë‰§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡…ïŸ•ï‹∞Åëï—Ö•∞∞Å—•—±îÄÙÅâΩë‰∞ÅâΩë‰∞ÄâM5LÅïπŸΩÁ§à(ÄÄÄÄÄÄÄÅï·çï¡–Å·çï¡—•Ω∏ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÅΩ¨ÄÙÅÖ±Õî(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Âmï……Ω…}≠ïÂtÄÙÅÕ—»°ï·å•lË‘¿¡t(ÄÄÄÄÄÄÄÅπΩ‹ÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÅ…ïÕ’±—Õm≠•πëtÄÙÄâÕïπ–àÅ•òÅΩ¨Åï±ÕîÄâôÖ•±ïêà(ÄÄÄÄÄÄÄÅ•òÅΩ¨Ë(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…ÂmÕ—Ö—’Õ}≠ïÂtÄÙÄâÕïπ–à(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…ÂmÕïπ—}≠ïÂtÄÙÅπΩ‹(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Âm±ïùÖçÂ}Õïπ—}≠ïÂtÄÙÅπΩ‹(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Âmòâ•πôΩ…µÖ—•Ωπ}Ì≠•πëı}çΩπ—ïπ–âtÄÙÅâΩë‰(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ≠•πêÄÙÙÄâïµÖ•∞àË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Âlâ•πôΩ…µÖ—•Ωπ}ïµÖ•±}—ïµ¡±Ö—ï}•êâtÄÙÅ—ïµ¡±Ö—îπùï–†â•êà§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—ïµ¡±Ö—ïlâ’ÕÖùï}çΩ’π–âtÄÙÅ•π–°—ïµ¡±Ö—îπùï–†â’ÕÖùï}çΩ’π–à§ÅΩ»Ä¿§Ä¨Äƒ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—ïµ¡±Ö—ïlâ±ÖÕ—}’Õïë}Ö–âtÄÙÅπΩ‹(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞Å≠•πê∞Å—•—±î∞Åëï—Ö•∞∞Å¡…ïŸ•ï‹§(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅπΩ‹(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ≠•πêÄÙÙÄâïµÖ•∞àÅÖπêÅïπ—…‰πùï–†âëïŸ•Õ}•êà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ≈’Ω—îÄÙÅπï·–†°…Ω‹ÅôΩ»Å…Ω‹Å•∏ÅëÖ—Ñπùï–†âëïµÖπëïÃà∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ…Ω‹πùï–†â•êà§ÄÙÙÅïπ—…ÂlâëïŸ•Õ}•êât§∞Å9Ωπî§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ≈’Ω—îË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ≈’Ω—ïlâÕ—Ö—’—}ëïŸ•ÃâtÄÙÄâπŸΩÁ§à(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ≈’Ω—ïlâëÖ—ï}ïπŸΩ•}¡±Ö∏âtÄÙÅπΩ‹(ÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…ÂmÕ—Ö—’Õ}≠ïÂtÄÙÄâôÖ•±ïêà(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Âmï……Ω…}≠ïÂtÄÙÅïπ—…‰πùï–°ï……Ω…}≠ï‰§ÅΩ»Äâ1îÅôΩ’…π•ÕÕï’»ÅÑÅ…ïô’œ§ÅΩ‘ÅªäeÑÅ¡ÖÃÅçΩπô•…∑§Å≥äeïπŸΩ§à(ÄÄÄÄÄÄÄÅ¡…•π–°òâÕïç…ï—Ö…•Ö—}ëï±•Ÿï…‰ÅÕ’âµ•ÕÕ•Ω∏ıÌïπ—…‰πùï–†ù•êú•ÙÅ…ïç•¡•ïπ–ıÌ}µÖÕ≠}ëï±•Ÿï…Â}…ïç•¡•ïπ–°…ïç•¡•ïπ–∞Å≠•πê•ÙÅ¡…ΩŸ•ëï»ıÌ¡…ΩŸ•ëï…ÙÅÕ—Ö—’ÃıÌïπ—…ÂmÕ—Ö—’Õ}≠ïÂuÙÅï……Ω»ıÌïπ—…‰πùï–°ï……Ω…}≠ï‰∞Äúú•Ùà§(ÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å…ïÕ’±—Ã(()ëïòÅ}Õïç…ï—Ö…•Ö—}ôΩ…µÖ—•Ωπ}çΩπô•ú°ôΩ…µÖ—•Ωπ}çΩëî§Ë(ÄÄÄÅçΩëîÄÙÅÕ—»°ôΩ…µÖ—•Ωπ}çΩëîÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ…ï—’…∏ÅMIQI%Q}=I5Q%=9Lπùï–°çΩëî§ÅΩ»ÅÏ(ÄÄÄÄÄÄÄÄâÕ°Ω…–àËÄâΩ…µÖ—•Ω∏à∞Äâ±Öâï∞àËÅA19}=I5Q%=9Lπùï–°çΩëî§ÅΩ»ÄâΩ…µÖ—•Ω∏Å%π”•ù…Ö±îÅçÖëïµ‰à∞(ÄÄÄÄÄÄÄÄâëΩÕÕ•ï…}’…∞àËÅMIQI%Q}=MM%I}UI0∞Äâ¡±Öππ•πù}’…∞àËÅMIQI%Q}A199%9}UI0∞(ÄÄÄÅÙ(()ëïòÅ}Õïç…ï—Ö…•Ö—}ôΩ…µÖ—•Ωπ}πÖµî°ôΩ…µÖ—•Ωπ}çΩëî§Ë(ÄÄÄÅ…ï—’…∏Å}Õïç…ï—Ö…•Ö—}ôΩ…µÖ—•Ωπ}çΩπô•ú°ôΩ…µÖ—•Ωπ}çΩëî•lâ±Öâï∞ât(()ëïòÅ}â’•±ë}Õïç…ï—Ö…•Ö—}ôΩ±±Ω›’¡}ÕµÃ°ôΩ…µÖ—•Ωπ}çΩëî§Ë(ÄÄÄÅôΩ…µÖ—•Ωπ}πÖµîÄÙÅ}Õïç…ï—Ö…•Ö—}ôΩ…µÖ—•Ωπ}πÖµî°ôΩ…µÖ—•Ωπ}çΩëî§(ÄÄÄÅ…ï—’…∏Ä†(ÄÄÄÄÄÄÄÄâ)îÅôÖ•ÃÅÕ’•—îÉÄÅπΩ—…îÉ•ç°ÖπùîÅ”•≥•¡°Ωπ•≈’îÅÖ‘ÅÕ’©ï–ÅëîÅπΩ—…îÅôΩ…µÖ—•Ω∏Äà(ÄÄÄÄÄÄÄÅòâÌôΩ…µÖ—•Ωπ}πÖµïÙ∏Å5ï…ç§ÅëîÅŸΩ—…îÅ•π”•À©–ÄÖqπq∏à(ÄÄÄÄÄÄÄÄãäZ€æ‚<ÅYΩ’ÃÅ¡Ω’ŸïËÅ”•≥•ç°Ö…ùï»Åì°ÃÅµÖ•π—ïπÖπ–ÅπΩ—…îÅëΩÕÕ•ï»ÅëîÅ¡À•Õïπ—Ö—•Ω∏Äà(ÄÄÄÄÄÄÄÄà°¡…Ωù…ÖµµîÅì•—Ö•±≥§∞ÅëÖ—ïÃ∞Å—Ö…•ôÃ§Åï∏Åç±•≈’Öπ–Å•ç§ÄÈqπq∏à(ÄÄÄÄÄÄÄÅòã¬~F$ÅÌMIQI%Q}=MM%I}UI1ıqπq∏à(ÄÄÄÄÄÄÄÄãäÁæ‚<ÅM§ÅŸΩ’ÃÅÕΩ’°Ö•—ïËÅô•πÖπçï»Å±ÑÅôΩ…µÖ—•Ω∏ÅŸ•ÑÅŸΩ—…îÅΩµ¡—îÅAï…ÕΩππï∞ÅëîÄà(ÄÄÄÄÄÄÄÄâΩ…µÖ—•Ω∏Ä°A§∞Å•∞ÅŸΩ’ÃÅôÖ’ë…ÑÅçÀ•ï»ÅŸΩ—…îÅ%ëïπ—•”§Å9’∑•…•≈’îÅ1ÑÅAΩÕ—îπqπq∏à(ÄÄÄÄÄÄÄÄâ;äe£•Õ•—ïËÅ¡ÖÃÉÄÅµîÅçΩπ—Öç—ï»ÅÕ§ÅŸΩ’ÃÅÖŸïËÅ±ÑÅµΩ•πë…îÅ≈’ïÕ—•Ω∏∞Å©îÅÕï…Ö§Å…ÖŸ§Åìäe‰ÅÀ•¡Ωπë…îÉ¬~b%qπq∏à(ÄÄÄÄÄÄÄÄâ	ΩππîÅ©Ω’…ª•î±qπqπÖÕÕÖπë…îÅ59IqπIïÕ¡ΩπÕÖâ±îÅçΩµµï…ç•Ö±îÅ%π”•ù…Ö±îÅçÖëïµÂq∏¿–Ä»»Ä–‹Ä¿‹Äÿ‡à(ÄÄÄÄ§(()ëïòÅ}ÂïÃ°ŸÖ±’î§Ë(ÄÄÄÅ…ï—’…∏ÅÕ—»°ŸÖ±’îÅΩ»Äàà§πÕ—…•¿†§π’¡¡ï»†§Å•∏ÅÏâ=U$à∞ÄâeLà∞ÄâQIUà∞ÄàƒâÙ(()ëïòÅ}Õïç…ï—Ö…•Ö—}ë•Õ¡±ÖÂ}ô•…Õ—}πÖµî°ŸÖ±’î§Ë(ÄÄÄÄààâΩ…µÖ–ÅÑÅô•…Õ–ÅπÖµîÅ›•—°Ω’–ÅïŸï»Å—…ÖπÕ±•—ï…Ö—•πúÅÖ›Ö‰Å•—ÃÅÖççïπ—Ã∏ààà(ÄÄÄÅŸÖ±’îÄÙÅ…îπÕ’à°»âqÃ¨à∞ÄàÄà∞ÅÕ—»°ŸÖ±’îÅΩ»Äàà§πÕ—…•¿†§§(ÄÄÄÅ•òÅπΩ–ÅŸÖ±’îË(ÄÄÄÄÄÄÄÅ…ï—’…∏Äàà(ÄÄÄÅôΩ…µÖ——ïêÄÙÄà¥àπ©Ω•∏°¡Ö…—lË≈tπ’¡¡ï»†§Ä¨Å¡Ö…—lƒÈtπ±Ω›ï»†§ÅôΩ»Å¡Ö…–Å•∏ÅŸÖ±’îπÕ¡±•–†à¥à§§(ÄÄÄÅ…ï—’…∏ÅÏâç±ïµïπ–àËÄâ≥•µïπ–âÙπùï–°ôΩ…µÖ——ïêπçÖÕïôΩ±ê†§∞ÅôΩ…µÖ——ïê§(()ëïòÅ}Õïç…ï—Ö…•Ö—}’¡çΩµ•πù}ÕïÕÕ•Ωπ}ù…Ω’¡Ã°ôΩ…µÖ—•Ωπ}çΩëî∞ÅÕï±ïç—ïë}ÕïÕÕ•Ω∏Ùàà§Ë(ÄÄÄÅÕï±ïç—ïêÄÙÅÕ—»°Õï±ïç—ïë}ÕïÕÕ•Ω∏ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅù…Ω’¡ÃÄÙÅmt(ÄÄÄÅÕïÕÕ•ΩπÃÄÙÅùï—}’¡çΩµ•πù}ôΩ…µÖ—•Ωπ}ÕïÕÕ•ΩπÃ°±ΩÖë}ëÖ—Ñ†§§(ÄÄÄÅôΩ»Åçïπ—…ï}çΩëî∞Åçïπ—…ï}πÖµîÅ•∏Å=I5Q%=9}9QILπ•—ïµÃ†§Ë(ÄÄÄÄÄÄÄÅ…Ω›ÃÄÙÅÕïÕÕ•ΩπÃπùï–°çïπ—…ï}çΩëî∞ÅÌÙ§πùï–°Õ—»°ôΩ…µÖ—•Ωπ}çΩëîÅΩ»Äàà§πÕ—…•¿†§∞Åmt§(ÄÄÄÄÄÄÄÅ±Öâï±±ïêÄÙÅmt(ÄÄÄÄÄÄÄÅôΩ»Å…Ω‹Å•∏Å…Ω›ÃË(ÄÄÄÄÄÄÄÄÄÄÄÅ±Öâï∞ÄÙÅÕ—»°…Ω‹πùï–†â±Öâï∞à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÄÄÄÄÅï·Ö¥ÄÙÅÕ—»°…Ω‹πùï–†âëÖ—ï}ï·Öµï∏à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–Å±Öâï∞Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÄÄÄÄÅë•Õ¡±Ö‰ÄÙÅ±Öâï∞(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅï·Ö¥ÅÖπêÄâï·Öµï∏àÅπΩ–Å•∏Å±Öâï∞πçÖÕïôΩ±ê†§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅë•Õ¡±Ö‰ÄÙÅòâÌ±Öâï±ÙÄ¥Åï·Öµï∏Å±îÅÌï·ÖµÙà(ÄÄÄÄÄÄÄÄÄÄÄÅ±Öâï±±ïêπÖ¡¡ïπê°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ±Öâï∞àËÅë•Õ¡±Ö‰∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÕï±ïç—ïêàËÅÕï±ïç—ïêÅ•∏ÅÌ±Öâï∞∞Åë•Õ¡±Ö‰∞ÅòâÌçïπ—…ï}πÖµïÙÉäPÅÌ±Öâï±ÙâÙ∞(ÄÄÄÄÄÄÄÄÄÄÄÅÙ§(ÄÄÄÄÄÄÄÅ•òÅ±Öâï±±ïêË(ÄÄÄÄÄÄÄÄÄÄÄÅù…Ω’¡ÃπÖ¡¡ïπê°Ïâçïπ—…îàËÅçïπ—…ï}πÖµî∞ÄâÕïÕÕ•ΩπÃàËÅ±Öâï±±ïëÙ§(ÄÄÄÅ…ï—’…∏Åù…Ω’¡Ã(()ëïòÅ}ïπÕ’…ï}Õïç…ï—Ö…•Ö—}≈’Ω—î°ëÖ—Ñ∞Åïπ—…‰∞ÅçΩπ—Öç–§Ë(ÄÄÄÄààâ…ïÖ—îÅΩ»Å…ïçΩππïç–Å—°îÅÕ•πù±îÅô•πÖπç•πúÅ≈’Ω—îÅâï±Ωπù•πúÅ—ºÅÑÅçÖ±∞∏ààà(ÄÄÄÅ•òÅπΩ–Å}ÂïÃ°ïπ—…‰πùï–†âëïŸ•Ãà§§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(ÄÄÄÅëïµÖπëïÃÄÙÅëÖ—ÑπÕï—ëïôÖ’±–†âëïµÖπëïÃà∞Åmt§(ÄÄÄÅ≈’Ω—îÄÙÅπï·–†°…Ω‹ÅôΩ»Å…Ω‹Å•∏ÅëïµÖπëïÃÅ•òÅ…Ω‹πùï–†â•êà§ÄÙÙÅïπ—…‰πùï–†âëïŸ•Õ}•êà§§∞Å9Ωπî§(ÄÄÄÅ•òÅ≈’Ω—îÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅ≈’Ω—îÄÙÅπï·–†°…Ω‹ÅôΩ»Å…Ω‹Å•∏ÅëïµÖπëïÃ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ…Ω‹πùï–†âÕΩ’…çï}Õïç…ï—Ö…•Ö—}•êà§ÄÙÙÅïπ—…‰πùï–†â•êà§§∞Å9Ωπî§(ÄÄÄÅ•òÅ≈’Ω—îÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅ≈’Ω—ï}•ê∞Å—Ω≠ï∏ÄÙÅÕ—»°’’•êπ’’•ê–†§§∞Å’’•êπ’’•ê–†§π°ï‡(ÄÄÄÄÄÄÄÅëï—Ö•±ÃÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏àËÅïπ—…‰πùï–†âôΩ…µÖ—•Ω∏à∞Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâëÖ—ïÃàËÅïπ—…‰πùï–†âôΩ…µÖ—•Ωπ}ÕïÕÕ•Ωπ}±Öâï∞à§ÅΩ»Åïπ—…‰πùï–†âôΩ…µÖ—•Ωπ}ëÖ—ï}ÕΩ’°Ö•—ïîà∞Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçïπ—…îàËÅïπ—…‰πùï–†âôΩ…µÖ—•Ωπ}çïπ—…îà∞Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâëÖ—ï}ï·Öµï∏àËÅïπ—…‰πùï–†âôΩ…µÖ—•Ωπ}ëÖ—ï}ï·Öµï∏à∞Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâç¡ô}µΩπ—Öπ–àËÅïπ—…‰πùï–†âç¡ô}µΩπ—Öπ–à∞Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâô…Öπçï}—…ÖŸÖ•∞àËÅïπ—…‰πùï–†âô…Öπçï}—…ÖŸÖ•∞à∞Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ•ëïπ—•—ï}π’µï…•≈’îàËÅïπ—…‰πùï–†â•ëïπ—•—ï}π’µï…•≈’îà∞Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕÕ•Ö¡}ÕïçΩ’…•Õµï}ŸÖ±•ëîàËÅïπ—…‰πùï–†âÕÕ•Ö¡}ÕïçΩ’…•Õµï}ŸÖ±•ëîà∞Äàà§∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÅ≈’Ω—îÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ•êàËÅ≈’Ω—ï}•ê∞Äâ—Ω≠ïπ}¡±Ö∏àËÅ—Ω≠ï∏∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕΩ’…çï}Õïç…ï—Ö…•Ö—}•êàËÅïπ—…‰πùï–†â•êà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâπΩ¥àËÅïπ—…‰πùï–†âπΩµ}ôÖµ•±±îà§ÅΩ»ÅÕ—»°ïπ—…‰πùï–†âπΩ¥à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡…ïπΩ¥àËÅ}Õïç…ï—Ö…•Ö—}ë•Õ¡±ÖÂ}ô•…Õ—}πÖµî°ïπ—…‰πùï–†â¡…ïπΩ¥à§ÅΩ»ÅÕ—»°ïπ—…‰πùï–†âπΩ¥à§ÅΩ»Äàà§πÕ¡±•–†àÄà•l¡t§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ—ï±ï¡°ΩπîàËÅïπ—…‰πùï–†â—ï±ï¡°Ωπîà∞Äàà§∞ÄâµÖ•∞àËÅïπ—…‰πùï–†âïµÖ•∞à∞Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâµΩ—•òàËÄâïµÖπëîÅëîÅëïŸ•ÃÅì•—Ö•±≥§à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâëï—Ö•±ÃàËÅ©ÕΩ∏πë’µ¡Ã°ëï—Ö•±Ã∞ÅïπÕ’…ï}ÖÕç•§ıÖ±Õî§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâëÖ—îàËÅëÖ—ï—•µîπëÖ—ï—•µîππΩ‹°¡Â—Ëπ—•µïÈΩπî†â’…Ω¡îΩAÖ…•Ãà§§πÕ—…ô—•µî†àïêºï¥ºïdÄï Ëï4à§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö—’–àËÄâ9Ω∏Å—…Ö•”§à∞ÄâÕ—Ö—’—}ëïŸ•ÃàËÄâÅïπŸΩÂï»à∞ÄâÖ——…•â’—•Ω∏àËÄàà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩµµïπ—Ö•…îàËÄàà∞ÄâçΩµµïπ—Ö•…ï}Öëµ•∏àËÄàà∞ÄâµÖ•±}çΩπô•…µîàËÄàà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâµÖ•±}ï……ï’»àËÄàà∞ÄâµÖ•±}çΩπ—ïπ‘àËÄàà∞ÄâµÖ•±}°—µ∞àËÄàà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡•ïçïÕ}©Ω•π—ïÃàËÅmt∞Äâ…ï¡ΩπÕïÃàËÅmt∞Äâ•Õ}ëΩ’â±Ω∏àËÅÖ±Õî∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…Ö¡¡ï±}ëÖ—îàËÄàà∞Äâ¡±ÖùîàËÄàà∞ÄâπΩ—Ö—•Ωπ}•π—ï…πîàËÄàà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâïç°ïÖπç•ï…}µÖπ’ï∞àËÅmt∞Äâ¡ëô}¡Ö—†àËÄàà∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÅëïµÖπëïÃπÖ¡¡ïπê°≈’Ω—î§(ÄÄÄÄÄÄÄÅ≈’Ω—ï}’…∞ÄÙÅ’…±}ôΩ»†â¡±Öπ}¡’â±•åà∞Å—Ω≠ï∏ı—Ω≠ï∏∞Å}ï·—ï…πÖ∞ıQ…’î§(ÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞ÄâëïŸ•Ãà∞ÄâïŸ•ÃÅì•—Ö•±≥§ÅçÀß§à∞Å≈’Ω—ï}’…∞∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòúÒ¿¯ÒÑÅ°…ïòÙâÌ≈’Ω—ï}’…±ÙàÅ—Ö…ùï–Ùâ}â±Öπ¨à˘=’Ÿ…•»Å±îÅëïŸ•ÃΩÑ¯Ω¿¯ú§(ÄÄÄÅ≈’Ω—ï}’…∞ÄÙÅ’…±}ôΩ»†â¡±Öπ}¡’â±•åà∞Å—Ω≠ï∏ı≈’Ω—ïlâ—Ω≠ïπ}¡±Ö∏ât∞Å}ï·—ï…πÖ∞ıQ…’î§(ÄÄÄÅïπ—…ÂlâëïŸ•Õ}•êât∞Åïπ—…ÂlâëïŸ•Õ}’…∞âtÄÙÅ≈’Ω—ïlâ•êât∞Å≈’Ω—ï}’…∞(ÄÄÄÅçΩπ—Öç—lâÕΩ’…çï}ëïŸ•Õ}•êât∞ÅçΩπ—Öç—lâëïŸ•Õ}’…∞âtÄÙÅ≈’Ω—ïlâ•êât∞Å≈’Ω—ï}’…∞(ÄÄÄÅ…ï—’…∏Å≈’Ω—î(()ëïòÅ}Õïç…ï—Ö…•Ö—}…ëÿ°ïπ—…‰§Ë(ÄÄÄÅÕ—Ö—’ÃÄÙÅÕ—»°ïπ—…‰πùï–†â…ëŸ}Õ—Ö—’Ãà§ÅΩ»Åïπ—…‰πùï–†âÖ¡¡Ω•π—µïπ—}Õ—Ö—’Ãà§ÅΩ»Äàà§π±Ω›ï»†§πÕ—…•¿†§(ÄÄÄÅ•òÅÕ—Ö—’ÃÅ•∏ÅÏâëïç±•πïêà∞ÄâπΩ—}…ï≈’ïÕ—ïêà∞ÄâπΩπîâÙË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(ÄÄÄÅ•òÅÕ—Ö—’ÃÄÙÙÄâÕç°ïë’±ïêàË(ÄÄÄÄÄÄÄÅµΩëîÄÙÅÕ—»°ïπ—…‰πùï–†â…ëŸ}µΩëîà§ÅΩ»Äàà§π±Ω›ï»†§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅµΩëîÅÖπêÅπΩ–ÅÖπ‰°—ï…¥Å•∏ÅµΩëîÅôΩ»Å—ï…¥Å•∏Ä†âÖ¡¡ï∞à∞Äâ”•≥•¡°Ωπîà∞Äâ—ï±ï¡°Ωπîà∞Äâ¡°Ωπîà§§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(ÄÄÄÄÄÄÄÅëÖ—ï}ŸÖ±’îÄÙÅÕ—»°ïπ—…‰πùï–†â…ëŸ}ëÖ—îà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ—•µï}ŸÖ±’îÄÙÅÕ—»°ïπ—…‰πùï–†â…ëŸ}—•µîà§ÅΩ»Äà¿¿Ë¿¿à§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅëÖ—ï}ŸÖ±’îË(ÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö…ÕïêÄÙÅ9Ωπî(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»Å¡Ö——ï…∏Å•∏Ä†àïêºï¥ºïdÄï Ëï4à∞ÄàïêÄïÄïdÄï Ëï4à∞Äàïd¥ï¥¥ïêÄï Ëï4à§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö…ÕïêÄÙÅëÖ—ï—•µîπëÖ—ï—•µîπÕ—…¡—•µî°òâÌëÖ—ï}ŸÖ±’ïÙÅÌ—•µï}ŸÖ±’ïÙà∞Å¡Ö——ï…∏§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅâ…ïÖ¨(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ¡Ö…ÕïêÅ•ÃÅπΩ–Å9ΩπîË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö…•ÃÄÙÅ¡Â—Ëπ—•µïÈΩπî†â’…Ω¡îΩAÖ…•Ãà§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ¡Ö…•Ãπ±ΩçÖ±•Èî°¡Ö…Õïê§ÄÙÅëÖ—ï—•µîπëÖ—ï—•µîππΩ‹°¡Ö…•Ã§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(ÄÄÄÄÄÄÄÅëÖ‰∞ÅµΩπ—†ÄÙÄàà∞Äàà(ÄÄÄÄÄÄÄÅ•òÅëÖ—ï}ŸÖ±’îË(ÄÄÄÄÄÄÄÄÄÄÄÅµÖ—ç†ÄÙÅ…îπµÖ—ç†°»âx°qëÏƒ∞…Ù§º°qëÏƒ∞…Ù§ΩqëÏ—Ùêà∞ÅëÖ—ï}ŸÖ±’î§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅµÖ—ç†Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅëÖ‰ÄÙÅµÖ—ç†πù…Ω’¿†ƒ§πÈô•±∞†»§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµΩπ—°}π’µâï»ÄÙÅ•π–°µÖ—ç†πù…Ω’¿†»§§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÄƒÄÙÅµΩπ—°}π’µâï»ÄÙÄƒ»Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµΩπ—†ÄÙÄ†â)9X∏à∞Äâ%YH∏à∞Äâ5ILà∞ÄâYH∏à∞Äâ5$à∞Äâ)U%8à∞Äâ)U%0∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ?mPà∞ÄâMAP∏à∞Äâ=P∏à∞Äâ9=X∏à∞Äâ%∏à•mµΩπ—°}π’µâï»Ä¥Ä≈t(ÄÄÄÄÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ›Ω…ëÃÄÙÅëÖ—ï}ŸÖ±’îπÕ¡±•–†§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ›Ω…ëÃË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅëÖ‰ÄÙÅ›Ω…ëÕl¡tπÈô•±∞†»§Å•òÅ›Ω…ëÕl¡tπ•Õë•ù•–†§Åï±ÕîÄàà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ±ï∏°›Ω…ëÃ§Ä¯ÄƒË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµΩπ—†ÄÙÅ›Ω…ëÕl≈tπ’¡¡ï»†§(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏâÕ—Ö—’ÃàËÅÕ—Ö—’Ã∞ÄâëÖ—îàËÅïπ—…‰πùï–†â…ëŸ}ëÖ—îà§∞Äâ—•µîàËÅïπ—…‰πùï–†â…ëŸ}—•µîà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâµΩëîàËÅïπ—…‰πùï–†â…ëŸ}µΩëîà§∞Äâ’…∞àËÅïπ—…‰πùï–†â…ëŸ}’…∞à§ÅΩ»Åïπ—…‰πùï–†âçÖ±ïπë±Â}’…∞à§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâπÖµîàËÅïπ—…‰πùï–†â…ëŸ}πÖµîà§ÅΩ»ÄâIïπëïËµŸΩ’ÃÅ”•≥•¡°Ωπ•≈’îà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ°ΩÕ—}πÖµîàËÅïπ—…‰πùï–†â…ëŸ}°ΩÕ—}πÖµîà§ÅΩ»Äàà∞ÄâëÖ‰àËÅëÖ‰∞ÄâµΩπ—†àËÅµΩπ—°Ù(ÄÄÄÅ•òÅÕ—Ö—’ÃÄÙÙÄâ¡…Ω¡ΩÕïêàË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏâÕ—Ö—’ÃàËÅÕ—Ö—’ÕÙ(ÄÄÄÅ…ï—’…∏Å9Ωπî(()ëïòÅ}Õïç…ï—Ö…•Ö—}ïµÖ•±}ôÖ±±âÖç¨°ïπ—…‰§Ë(ÄÄÄÅôΩ…µÖ—•Ω∏ÄÙÅ}Õïç…ï—Ö…•Ö—}ôΩ…µÖ—•Ωπ}πÖµî°ïπ—…‰πùï–†âôΩ…µÖ—•Ω∏à§§(ÄÄÄÅÕïÕÕ•Ω∏ÄÙÅÕ—»°ïπ—…‰πùï–†âôΩ…µÖ—•Ωπ}ëÖ—ï}ÕΩ’°Ö•—ïîà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ¡Ö…Öù…Ö¡°ÃÄÙÅl(ÄÄÄÄÄÄÄÄ°òâYΩ’ÃÅÕΩ’°Ö•—ïËÅëïÃÅ…ïπÕï•ùπïµïπ—ÃÅçΩπçï…πÖπ–Å±ÑÅôΩ…µÖ—•Ω∏ÅÌôΩ…µÖ—•ΩπÙ∞ÅπΩ—Öµµïπ–Å¡Ω’»Å±ÑÅÕïÕÕ•Ω∏ÅÌÕïÕÕ•ΩπÙ∏Å9Ω—…îÉ•≈’•¡îÅ€•…•ô•ï…ÑÅÖŸïåÅŸΩ’ÃÅ±ïÃÅë•Õ¡Ωπ•â•±•”•ÃÅï–Å±ïÃÅ¡À•…ï≈’•ÃÅÖ¡¡±•çÖâ±ïÃ∏à(ÄÄÄÄÄÄÄÄÅ•òÅÕïÕÕ•Ω∏Åï±ÕîÅòâYΩ’ÃÅÕΩ’°Ö•—ïËÅëïÃÅ…ïπÕï•ùπïµïπ—ÃÅçΩπçï…πÖπ–Å±ÑÅôΩ…µÖ—•Ω∏ÅÌôΩ…µÖ—•ΩπÙ∏Å9Ω—…îÉ•≈’•¡îÅŸΩ’ÃÅÖ•ëï…ÑÉÄÅç°Ω•Õ•»Å±ÑÅÕïÕÕ•Ω∏ÅÖëÖ¡”•îÅï–Å€•…•ô•ï…ÑÅÖŸïåÅŸΩ’ÃÅ±ïÃÅë•Õ¡Ωπ•â•±•”•ÃÅï–Å±ïÃÅ¡À•…ï≈’•Ã∏à§∞(ÄÄÄÄÄÄÄÄâYΩ’ÃÅ—…Ω’Ÿï…ïËÅç§µëïÕÕΩ’ÃÅŸΩÃÅ…ï√°…ïÃÅô•Öâ±ïÃÅï–Å±ïÃÅÖç—•ΩπÃÅçΩπçÀ°—ïÃÅ¡Ω’»ÅÖŸÖπçï»∏Å9Ω—…îÉ•≈’•¡îÅ…ïÕ—îÅë•Õ¡Ωπ•â±îÅ¡Ω’»ÅŸΩ’ÃÅÖççΩµ¡Öùπï»ÅÕÖπÃÅ¡À•Õ’µï»ÅëîÅ≥äeÖççΩ…êÅìäe’∏ÅΩ…ùÖπ•ÕµîÅô•πÖπçï’»ÅΩ‘ÅÖëµ•π•Õ—…Ö—•ò∏à∞(ÄÄÄÅt(ÄÄÄÅô•πÖπç•πúÄÙÄàà(ÄÄÄÅç¡òÄÙÅ}¡Ö…Õï}ç¡ô}ŸÖ±’î°ïπ—…‰πùï–†âç¡ô}µΩπ—Öπ–à§§(ÄÄÄÅ¡…•çîÄÙÅA19}QI%Lπùï–°ïπ—…‰πùï–†âôΩ…µÖ—•Ω∏à§∞Ä¿§(ÄÄÄÅ•òÅç¡òÅÖπêÅ¡…•çîÅÖπêÅç¡òÄ¯ÙÅ¡…•çîË(ÄÄÄÄÄÄÄÅô•πÖπç•πúÄÙÄâYΩ—…îÅµΩπ—Öπ–ÅAÅì•ç±ÖÀ§ÅçΩ’Ÿ…îÅ±îÅ—Ö…•ò∞ÅÕΩ’ÃÅÀ•Õï…ŸîÅë‘ÅÕΩ±ëîÅÀ•ï∞Åë•Õ¡Ωπ•â±îÅÖ‘ÅµΩµïπ–ÅëîÅ≥äe•πÕç…•¡—•Ω∏∏à(ÄÄÄÅï±•òÅç¡òÅÖπêÅ¡…•çîË(ÄÄÄÄÄÄÄÅô•πÖπç•πúÄÙÅòâYΩ—…îÅµΩπ—Öπ–ÅAÅì•ç±ÖÀ§Åô•πÖπçîÅ’πîÅ¡Ö…—•îÅë‘Å—Ö…•òÄÏÅ±îÅ…ïÕ—îÉÄÅçΩ’Ÿ…•»ÅïÕ–ÅëîÅÌ¡…•çîÄ¥Åç¡òË±ÙÉä
∞ÅQQ∏àπ…ï¡±Öçî†à∞à∞ÄàÄà§(ÄÄÄÅ•òÅ}ÂïÃ°ïπ—…‰πùï–†âô…Öπçï}—…ÖŸÖ•∞à§§Ë(ÄÄÄÄÄÄÄÅô—}Õ—Ö—’ÃÄÙÅÕ—»°ïπ—…‰πùï–†âô…Öπçï}—…ÖŸÖ•±}Õ—Ö—’Ãà§ÅΩ»Äàà§π±Ω›ï»†§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅô—}Õ—Ö—’ÃÅ•∏ÅÏâÕ’âµ•——ïêà∞Äâ—…ÖπÕµ•——ïêà∞Äâ—…ÖπÕµ•Õîà∞Äâëï¡ΩÕïîà∞Äâì•¡Ωœ•îâÙË(ÄÄÄÄÄÄÄÄÄÄÄÅ›•Õ°ïêÄÙÄâYΩ—…îÅëïµÖπëîÅëîÅô•πÖπçïµïπ–ÅÖ’¡À°ÃÅëîÅ…ÖπçîÅQ…ÖŸÖ•∞ÅÑÉ•”§Å—…ÖπÕµ•ÕîÄÏÅÕÑÅì•ç•Õ•Ω∏Å…ïÕ—îÅª•çïÕÕÖ•…î∏à(ÄÄÄÄÄÄÄÅï±•òÅô—}Õ—Ö—’ÃÅ•∏ÅÏâ¡ïπë•πúà∞Äâïπ}çΩ’…Ãà∞Äâï∏ÅÖ——ïπ—îâÙË(ÄÄÄÄÄÄÄÄÄÄÄÅ›•Õ°ïêÄÙÄâYΩ—…îÅëïµÖπëîÅëîÅô•πÖπçïµïπ–Å…ÖπçîÅQ…ÖŸÖ•∞ÅïÕ–Åï∏ÅçΩ’…ÃÅìäe•πÕ—…’ç—•Ω∏Å¡Ö»Å≥äeΩ…ùÖπ•Õµî∏à(ÄÄÄÄÄÄÄÅï±•òÅô—}Õ—Ö—’ÃÅ•∏ÅÏâÖ¡¡…ΩŸïêà∞ÄâÖççï¡—ïêà∞ÄâÖççï¡—ïîà∞ÄâÖççï¡”•îâÙË(ÄÄÄÄÄÄÄÄÄÄÄÅ›•Õ°ïêÄÙÄâYΩ—…îÅëïµÖπëîÅëîÅô•πÖπçïµïπ–Å…ÖπçîÅQ…ÖŸÖ•∞ÅïÕ–Å•πë•≈◊•îÅçΩµµîÅÖççï¡”•î∏à(ÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÅ›•Õ°ïêÄÙÄâYΩ’ÃÅÕΩ’°Ö•—ïËÉ•—’ë•ï»ÅÖŸïåÅπΩ—…îÉ•≈’•¡îÅ±ÑÅ¡ΩÕÕ•â•±•”§Åìäe’πîÅëïµÖπëîÅëîÅô•πÖπçïµïπ–ÅÖ’¡À°ÃÅëîÅ…ÖπçîÅQ…ÖŸÖ•∞∏à(ÄÄÄÄÄÄÄÅô•πÖπç•πúÄÙÅòâÌô•πÖπç•πùÙÅÌ›•Õ°ïëÙàπÕ—…•¿†§(ÄÄÄÅçπÖ¡ÃÄÙÄàà(ÄÄÄÅôΩ…µÖ—•Ωπ}çΩëîÄÙÅÕ—»°ïπ—…‰πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§π’¡¡ï»†§(ÄÄÄÅ•òÅôΩ…µÖ—•Ωπ}çΩëîÅ•∏ÅÏâALà∞ÄâÕ@âÙË(ÄÄÄÄÄÄÄÅçπÖ¡Õ}Õ—Ö—’ÃÄÙÅÕ—»°ïπ—…‰πùï–†âçπÖ¡Õ}Õ—Ö—’Ãà§ÅΩ»Äàà§π±Ω›ï»†§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅçπÖ¡Õ}Õ—Ö—’ÃÅ•∏ÅÏâÕ’âµ•——ïêà∞Äâ—…ÖπÕµ•——ïêà∞Äâ—…ÖπÕµ•ÕîâÙË(ÄÄÄÄÄÄÄÄÄÄÄÅçπÖ¡ÃÄÙÄâYΩ—…îÅëïµÖπëîÅìäeÖ’—Ω…•ÕÖ—•Ω∏Å¡À•Ö±Öâ±îÅ9ALÅÑÉ•”§Å—…ÖπÕµ•Õî∏Å9Ω—…îÉ•≈’•¡îÅ…ïÕ—îÅë•Õ¡Ωπ•â±îÅ¡ïπëÖπ–ÅÕΩ∏Å•πÕ—…’ç—•Ω∏∏à(ÄÄÄÄÄÄÄÅï±•òÅçπÖ¡Õ}Õ—Ö—’ÃÅ•∏ÅÏâÖ¡¡…ΩŸïêà∞ÄâÖççï¡—ïêà∞ÄâÖççï¡—ïîà∞ÄâÖççï¡”•îâÙË(ÄÄÄÄÄÄÄÄÄÄÄÅçπÖ¡ÃÄÙÄâYΩ—…îÅÖ’—Ω…•ÕÖ—•Ω∏Å9ALÅïÕ–Å•πë•≈◊•îÅçΩµµîÅÖççï¡”•îÄÏÅπΩ—…îÉ•≈’•¡îÅ€•…•ô•ï…ÑÅÖŸïåÅŸΩ’ÃÅ±îÅ©’Õ—•ô•çÖ—•òÅª•çïÕÕÖ•…î∏à(ÄÄÄÄÄÄÄÅï±•òÅ}ÂïÃ°ïπ—…‰πùï–†âçπÖ¡Õ}Ω¨à§§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçπÖ¡ÃÄÙÄâYΩ—…îÅçÖ…—îÅ¡…ΩôïÕÕ•Ωππï±±îÅïÕ–ÅŸÖ±•ëîÄÏÅπΩ—…îÉ•≈’•¡îÅ€•…•ô•ï…ÑÅÖŸïåÅŸΩ’ÃÅ±ïÃÅ©’Õ—•ô•çÖ—•ôÃÅª•çïÕÕÖ•…ïÃ∏à(ÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÅçπÖ¡ÃÄÙÅòâM§ÅŸΩ’ÃÅπîÅë•Õ¡ΩÕïËÅ¡ÖÃÅïπçΩ…îÅìäe’πîÅçÖ…—îÅ¡…ΩôïÕÕ•Ωππï±±î∞Å≥äg•—Ö¡îÅÖ——ïπë’îÅÖŸÖπ–Å≥äeïπ—À•îÅï∏ÅôΩ…µÖ—•Ω∏ÅÌôΩ…µÖ—•Ωπ}çΩëïÙÅïÕ–Å≥äeÖ’—Ω…•ÕÖ—•Ω∏Å¡À•Ö±Öâ±îÅ9AL∏Å9Ω—…îÉ•≈’•¡îÅŸΩ’ÃÅÖççΩµ¡ÖùπîÅëÖπÃÅçï——îÅì•µÖ…ç°î∏à(ÄÄÄÅÕ—ï¡ÃÄÙÅlâΩπÕ’±—ï»Å±îÅëΩÕÕ•ï»ÅëîÅ¡À•Õïπ—Ö—•Ω∏Åï–Å±îÅ¡±Öππ•πú∏ât(ÄÄÄÅ•òÅÕïÕÕ•Ω∏ËÅÕ—ï¡ÃπÖ¡¡ïπê†âΩπô•…µï»ÅÖŸïåÅπΩ—…îÉ•≈’•¡îÅ±ÑÅÕïÕÕ•Ω∏ÅÕΩ’°Ö•”•î∏à§(ÄÄÄÅ•òÅ}ÂïÃ°ïπ—…‰πùï–†âç¡ô}çΩπÕ’±—îà§§ËÅÕ—ï¡ÃπÖ¡¡ïπê†â[•…•ô•ï»Å≈’îÅŸΩ—…îÅ%ëïπ—•”§Å9’∑•…•≈’îÅ1ÑÅAΩÕ—îÅïÕ–ÅôΩπç—•Ωππï±±îÅÖŸÖπ–Å—Ω’—îÅ•πÕç…•¡—•Ω∏ÅA∏à§(ÄÄÄÅ•òÅôΩ…µÖ—•Ωπ}çΩëîÅ•∏ÅÏâALà∞ÄâÕ@âÙÅÖπêÅπΩ–Å}ÂïÃ°ïπ—…‰πùï–†âçπÖ¡Õ}Ω¨à§§ËÅÕ—ï¡ÃπÖ¡¡ïπê†âAÀ•¡Ö…ï»ÅÖŸïåÅπΩ—…îÉ•≈’•¡îÅ±ÑÅì•µÖ…ç°îÅìäeÖ’—Ω…•ÕÖ—•Ω∏Å¡À•Ö±Öâ±îÅ9AL∏à§(ÄÄÄÅ…ï—’…∏ÅÏâÕ’µµÖ…Â}¡Ö…Öù…Ö¡°ÃàËÅ¡Ö…Öù…Ö¡°Ã∞Äâô•πÖπç•πù}µïÕÕÖùîàËÅô•πÖπç•πú∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçπÖ¡Õ}µïÕÕÖùîàËÅçπÖ¡Ã∞Äâπï·—}Õ—ï¡ÃàËÅÕ—ï¡ÕlË—uÙ(()ëïòÅ}ŸÖ±•ëÖ—ï}Õïç…ï—Ö…•Ö—}Ö•}çΩπ—ïπ–°…Ö‹∞ÅôÖ±±âÖç¨∞Åïπ—…‰ı9Ωπî∞Å¡…ΩÕ¡ïç—}ô•…Õ—}πÖµîÙàà§Ë(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅŸÖ±’îÄÙÅ©ÕΩ∏π±ΩÖëÃ°…Ö‹§Å•òÅ•Õ•πÕ—Öπçî°…Ö‹∞ÅÕ—»§Åï±ÕîÅ…Ö‹(ÄÄÄÄÄÄÄÅ¡Ö…Öù…Ö¡°ÃÄÙÅŸÖ±’îπùï–†âÕ’µµÖ…Â}¡Ö…Öù…Ö¡°Ãà§(ÄÄÄÄÄÄÄÅÕ—ï¡ÃÄÙÅŸÖ±’îπùï–†âπï·—}Õ—ï¡Ãà§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°¡Ö…Öù…Ö¡°Ã∞Å±•Õ–§ÅΩ»ÅπΩ–Ä»ÄÙÅ±ï∏°¡Ö…Öù…Ö¡°Ã§ÄÙÄ–ÅΩ»ÅπΩ–Å•Õ•πÕ—Öπçî°Õ—ï¡Ã∞Å±•Õ–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†âÕ—…’ç—’…îÅ•πŸÖ±•ëîà§(ÄÄÄÄÄÄÄÅôΩ…â•ëëï∏ÄÙÄ†àà∞ÄââΩπ©Ω’»à∞ÄâçÖÕÕÖπë…îÅµïπÖ…êà∞Äâ±îÅçÖπë•ëÖ–à∞Äâ±ÑÅçÖπë•ëÖ—îà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ±îÅ¡…ΩÕ¡ïç–à∞Äâ±ÑÅ¡ï…ÕΩππîÅÕΩ’°Ö•—îà∞Äâ•∞ÅÕΩ’°Ö•—îà∞Äâï±±îÅÕΩ’°Ö•—îà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ•∞ÅëïŸ…Ñà∞Äâï±±îÅëïŸ…Ñà§(ÄÄÄÄÄÄÄÅë’¡±•çÖ—ï}•π—…Ωë’ç—•ΩπÃÄÙÄ†âµï…ç§Å¡Ω’»Å±îÅ—ïµ¡Ãà∞Äâµï…ç§Å¡Ω’»Å±îÅ—ïµ¡ÃÅçΩπÕÖçÀ§à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ±Ω…ÃÅëîÅπΩ—…îÉ•ç°Öπùîà∞ÄâπΩ—…îÉ•ç°ÖπùîÅÖ‘ÅÕ’©ï–Åëîà§(ÄÄÄÄÄÄÄÅç±ïÖπ}¡Ö…Öù…Ö¡°ÃÄÙÅmÕ—»°¿§πÕ—…•¿†§ÅôΩ»Å¿Å•∏Å¡Ö…Öù…Ö¡°ÃÅ•òÅÕ—»°¿§πÕ—…•¿†•t(ÄÄÄÄÄÄÄÅ•òÅπΩ–Ä»ÄÙÅ±ï∏°ç±ïÖπ}¡Ö…Öù…Ö¡°Ã§ÄÙÄ–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†âπΩµâ…îÅëîÅ¡Ö…Öù…Ö¡°ïÃÅ•πŸÖ±•ëîà§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Åç±ïÖπ}¡Ö…Öù…Ö¡°Õl¡tπçÖÕïôΩ±ê†§πÕ—Ö…—Õ›•—††âŸΩ’ÃÅÕΩ’°Ö•—ïËÅëïÃÅ…ïπÕï•ùπïµïπ—ÃÅçΩπçï…πÖπ–Å±ÑÅôΩ…µÖ—•Ω∏à§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†âôΩ…µ’±Ö—•Ω∏ÅëîÅ±ÑÅëïµÖπëîÅ•πŸÖ±•ëîà§(ÄÄÄÄÄÄÄÅÖ±±}çΩπ—ïπ–ÄÙÅç±ïÖπ}¡Ö…Öù…Ö¡°ÃÄ¨ÅmÕ—»°ŸÖ±’îπùï–†âô•πÖπç•πù}µïÕÕÖùîà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—»°ŸÖ±’îπùï–†âçπÖ¡Õ}µïÕÕÖùîà§ÅΩ»Äàà§πÕ—…•¿†•t(ÄÄÄÄÄÄÄÅÖ±±}çΩπ—ïπ–Ä¨ÙÅmÕ—»°Õ—ï¿§πÕ—…•¿†§ÅôΩ»ÅÕ—ï¿Å•∏ÅÕ—ï¡Õt(ÄÄÄÄÄÄÄÅ•òÅÖπ‰°Öπ‰°—Ω≠ï∏Å•∏Å—ï·–π±Ω›ï»†§ÅôΩ»Å—Ω≠ï∏Å•∏ÅôΩ…â•ëëï∏§ÅôΩ»Å—ï·–Å•∏ÅÖ±±}çΩπ—ïπ–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†âçΩπ—ïπ‘Å•π—ï…ë•–à§(ÄÄÄÄÄÄÄÅ•òÅÖπ‰°Öπ‰°—Ω≠ï∏Å•∏Å—ï·–πçÖÕïôΩ±ê†§ÅôΩ»Å—Ω≠ï∏Å•∏Åë’¡±•çÖ—ï}•π—…Ωë’ç—•ΩπÃ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅôΩ»Å—ï·–Å•∏Åç±ïÖπ}¡Ö…Öù…Ö¡°Ã§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†â•π—…Ωë’ç—•Ω∏ÅÀ•√•”•îà§(ÄÄÄÄÄÄÄÅô•…Õ—}πÖµîÄÙÅÕ—»°¡…ΩÕ¡ïç—}ô•…Õ—}πÖµîÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅô•…Õ—}πÖµîÅÖπêÅÖπ‰°…îπÕïÖ…ç†°…òà†¸Öq‹•Ì…îπïÕçÖ¡î°ô•…Õ—}πÖµî•Ù†¸Öq‹§à∞Å—ï·–∞Å…îπ$§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅôΩ»Å—ï·–Å•∏ÅÖ±±}çΩπ—ïπ–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†â¡À•πΩ¥Åë‘ÅëïÕ—•πÖ—Ö•…îÅ•π—ï…ë•–à§(ÄÄÄÄÄÄÄÅ…ïÕ’±–ÄÙÅÏâÕ’µµÖ…Â}¡Ö…Öù…Ö¡°ÃàËÅç±ïÖπ}¡Ö…Öù…Ö¡°Ã∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâô•πÖπç•πù}µïÕÕÖùîàËÅÕ—»°ŸÖ±’îπùï–†âô•πÖπç•πù}µïÕÕÖùîà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâçπÖ¡Õ}µïÕÕÖùîàËÅÕ—»°ŸÖ±’îπùï–†âçπÖ¡Õ}µïÕÕÖùîà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâπï·—}Õ—ï¡ÃàËÅmÕ—»°Õ—ï¿§πÕ—…•¿†§ÅôΩ»ÅÕ—ï¿Å•∏ÅÕ—ï¡ÃÅ•òÅÕ—»°Õ—ï¿§πÕ—…•¿†•ulË—uÙ(ÄÄÄÄÄÄÄÅô—}Õ—Ö—’ÃÄÙÅÕ—»†°ïπ—…‰ÅΩ»ÅÌÙ§πùï–†âô…Öπçï}—…ÖŸÖ•±}Õ—Ö—’Ãà§ÅΩ»Äàà§π±Ω›ï»†§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Åô—}Õ—Ö—’ÃÅÖπêÅ…îπÕïÖ…ç†°»à°ëïµÖπëîπÏ¿∞Ã¡Ù°—…ÖπÕµ•ÕïÒì•¡Ωœ•ïÒï∏ÅçΩ’…ÕÒï∏ÅÖ——ïπ—ïÒŸÖ±•ì•ïÒÖççï¡”•î§§à∞Å…ïÕ’±—lâô•πÖπç•πù}µïÕÕÖùîât∞Å…îπ$§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†âÕ—Ö—’–Å…ÖπçîÅQ…ÖŸÖ•∞ÅπΩ∏Å¡…Ω’€§à§(ÄÄÄÄÄÄÄÅôΩ…µÖ—•Ωπ}çΩëîÄÙÅÕ—»†°ïπ—…‰ÅΩ»ÅÌÙ§πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§π’¡¡ï»†§(ÄÄÄÄÄÄÄÅ•òÅôΩ…µÖ—•Ωπ}çΩëîÅπΩ–Å•∏ÅÏâALà∞ÄâÕ@âÙË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕ’±—lâçπÖ¡Õ}µïÕÕÖùîâtÄÙÄàà(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕ’±—lâπï·—}Õ—ï¡ÃâtÄÙÅmÕ—ï¿ÅôΩ»ÅÕ—ï¿Å•∏Å…ïÕ’±—lâπï·—}Õ—ï¡Ãât(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–Å…îπÕïÖ…ç†°»â9AMÒçÖ…—îÅ¡…ΩôïÕÕ•Ωππï±±îà∞ÅÕ—ï¿∞Å…îπ$•t(ÄÄÄÄÄÄÄÅ…ï—’…∏Å…ïÕ’±–(ÄÄÄÅï·çï¡–Ä°YÖ±’ï……Ω»∞ÅQÂ¡ï……Ω»∞Å©ÕΩ∏π)M=9ïçΩëï……Ω»∞Å——…•â’—ï……Ω»§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅôÖ±±âÖç¨(()ëïòÅ}Õïç…ï—Ö…•Ö—}¡…Ω©ïç—}…Ω›Ã°ïπ—…‰∞ÅçΩπô•ú§Ë(ÄÄÄÅ…Ω›ÃÄÙÅl†âΩ…µÖ—•Ω∏à∞ÅçΩπô•ùlâ±Öâï∞ât§∞Ä†âMïÕÕ•Ω∏Åï–Åçïπ—…îà∞Åïπ—…‰πùï–†âôΩ…µÖ—•Ωπ}ëÖ—ï}ÕΩ’°Ö•—ïîà§•t(ÄÄÄÅ•òÅïπ—…‰πùï–†âç¡ô}µΩπ—Öπ–à§ËÅ…Ω›ÃπÖ¡¡ïπê††â	’ëùï–ÅAÅì•ç±ÖÀ§à∞ÅòâÌïπ—…Âlùç¡ô}µΩπ—Öπ–ùuÙÉä
∞à§§(ÄÄÄÅô’πë•πúÄÙÅmt(ÄÄÄÅ•òÅ}ÂïÃ°ïπ—…‰πùï–†âô…Öπçï}—…ÖŸÖ•∞à§§ËÅô’πë•πúπÖ¡¡ïπê†ã•—’ëîÅìäe’πîÅ¡ΩÕÕ•â•±•”§ÅëîÅô•πÖπçïµïπ–Å…ÖπçîÅQ…ÖŸÖ•∞ÅÕΩ’°Ö•”•îà§(ÄÄÄÅ•òÅ}ÂïÃ°ïπ—…‰πùï–†âô•πÖπçïµïπ—}¡ï…Õºà§§ËÅô’πë•πúπÖ¡¡ïπê†âô•πÖπçïµïπ–Å¡ï…ÕΩππï∞Å¡ΩÕÕ•â±îà§(ÄÄÄÅ•òÅô’πë•πúËÅ…Ω›ÃπÖ¡¡ïπê††â•πÖπçïµïπ–ÅïπŸ•ÕÖü§à∞ÄàÄÏÄàπ©Ω•∏°ô’πë•πú§§§(ÄÄÄÅ•òÅ}ÂïÃ°ïπ—…‰πùï–†âëïŸ•Ãà§§ËÅ…Ω›ÃπÖ¡¡ïπê††âïŸ•Ãà∞ÄâU∏ÅëïŸ•ÃÅÑÉ•”§ÅëïµÖπì§à§§(ÄÄÄÅ…ï—’…∏Ål°±Öâï∞∞ÅŸÖ±’î§ÅôΩ»Å±Öâï∞∞ÅŸÖ±’îÅ•∏Å…Ω›ÃÅ•òÅÕ—»°ŸÖ±’îÅΩ»Äàà§πÕ—…•¿†•t(()ëïòÅ}â’•±ë}Õïç…ï—Ö…•Ö—}ôΩ±±Ω›’¡}ïµÖ•∞°ïπ—…‰∞ÅçΩπ—Öç–∞Å±ΩùΩ}Õ…åÙâç•êÈ•π—ïù…Ö±îµÖçÖëïµ‰µ±Ωùºà§Ë(ÄÄÄÅçΩπô•úÄÙÅ}Õïç…ï—Ö…•Ö—}ôΩ…µÖ—•Ωπ}çΩπô•ú°ïπ—…‰πùï–†âôΩ…µÖ—•Ω∏à§§(ÄÄÄÅôÖ±±âÖç¨ÄÙÅ}Õïç…ï—Ö…•Ö—}ïµÖ•±}ôÖ±±âÖç¨°ïπ—…‰§(ÄÄÄÅôÖç—ÃÄÙÅÌ≠ï‰ËÅïπ—…‰πùï–°≠ï‰∞Äàà§ÅôΩ»Å≠ï‰Å•∏Ä†âôΩ…µÖ—•Ωπ}ëÖ—ï}ÕΩ’°Ö•—ïîà∞ÄâôΩ…µÖ—•Ωπ}ÕïÕÕ•Ωπ}±Öâï∞à∞ÄâôΩ…µÖ—•Ωπ}çïπ—…îà∞ÄâôΩ…µÖ—•Ωπ}ëÖ—ï}ï·Öµï∏à∞ÄâëïŸ•Ãà∞Äâ…ëŸ}Õ—Ö—’Ãà∞Äâ…ëŸ}ëÖ—îà∞Äâ…ëŸ}—•µîà∞Äâ…ëŸ}µΩëîà∞Äâç¡ô}çΩπÕ’±—îà∞Äâç¡ô}µΩπ—Öπ–à∞Äâô…Öπçï}—…ÖŸÖ•∞à∞Äâô…Öπçï}—…ÖŸÖ•±}Õ—Ö—’Ãà∞Äâô—}…ïô’Õ}Ω¨à∞Äâô•πÖπçïµïπ—}¡ï…Õºà∞Äâ•ëïπ—•—ï}π’µï…•≈’îà∞ÄâçπÖ¡Õ}Ω¨à∞ÄâçπÖ¡Õ}Õ—Ö—’Ãà∞ÄâπΩ—ïÃà•Ù(ÄÄÄÅôÖç—Ãπ’¡ëÖ—î°ÏâçΩëîàËÅïπ—…‰πùï–†âôΩ…µÖ—•Ω∏à§∞Äâ•π—•—’±îàËÅçΩπô•úπùï–†â±Öâï∞à§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÖë…ïÕÕï}çïπ—…îàËÅçΩπô•úπùï–†â±ΩçÖ—•Ω∏à∞Äàà§∞Äâ—Ö…•òàËÅçΩπô•úπùï–†â¡…•çîà∞Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâë’…ïîàËÅçΩπô•úπùï–†âë’…Ö—•Ω∏à∞Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâΩâ©ïç—•ô}çï…—•ô•çÖ—•Ω∏àËÅçΩπô•úπùï–†âçï…—•ô•çÖ—•Ω∏à§ÅΩ»ÅçΩπô•úπùï–†â¡’…¡ΩÕîà∞Äàà•Ù§(ÄÄÄÅôΩ…µÖ—•Ωπ}çΩëîÄÙÅÕ—»°ïπ—…‰πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§π’¡¡ï»†§(ÄÄÄÅ•òÅôΩ…µÖ—•Ωπ}çΩëîÅπΩ–Å•∏ÅÏâALà∞ÄâÕ@âÙË(ÄÄÄÄÄÄÄÅôÖç—ÃÄÙÅÌ≠ï‰ËÅŸÖ±’îÅôΩ»Å≠ï‰∞ÅŸÖ±’îÅ•∏ÅôÖç—Ãπ•—ïµÃ†§Å•òÅ≠ï‰ÅπΩ–Å•∏ÅÏâçπÖ¡Õ}Ω¨à∞ÄâçπÖ¡Õ}Õ—Ö—’ÃâıÙ(ÄÄÄÅÕÂÕ—ï¥ÄÙÄààâQ‘Å¡…Ωë’•ÃÅ’π•≈’ïµïπ–Å’∏ÅΩâ©ï–Å)M=8ÅŸÖ±•ëîÅÖŸïåÅÕ’µµÖ…Â}¡Ö…Öù…Ö¡°ÃÄ†»ÉÄÄÃÅ¡Ö…Öù…Ö¡°ïÃ∞ÄƒÃ¿ÉÄÄ»»¿ÅµΩ—ÃÅÖ‘Å—Ω—Ö∞§∞Åô•πÖπç•πù}µïÕÕÖùî∞ÅçπÖ¡Õ}µïÕÕÖùîÅï–Åπï·—}Õ—ï¡ÃÄ†»ÉÄÄ–É•≥•µïπ—Ã§∏Å…ÖªùÖ•ÃÅπÖ—’…ï∞∞Å¡…ΩôïÕÕ•Ωππï∞∞Åç°Ö±ï’…ï’‡Åï–Åç±Ö•»∏Å’ç’∏Å!Q50∞Å5Ö…≠ëΩ›∏∞Å¡’çîÅëÖπÃÅ±ïÃÅ¡Ö…Öù…Ö¡°ïÃ∞ÅÕÖ±’—Ö—•Ω∏ÅΩ‘ÅÕ•ùπÖ—’…î∏ÅYΩ’ÃÅÀ•ë•ùïËÅ’∏ÅµïÕÕÖùîÅÖë…ïÕœ§Åë•…ïç—ïµïπ–ÅÖ‘ÅëïÕ—•πÖ—Ö•…î∏Åµ¡±ΩÂïËÅï·ç±’Õ•Ÿïµïπ–ÅŸΩ’Ã∞ÅŸΩ—…îÅï–ÅŸΩÃ∏Å9îÅµïπ—•ΩππïËÅ©ÖµÖ•ÃÅÕΩ∏Å¡À•πΩ¥Åï–ÅπîÅ¡Ö…±ïËÅ©ÖµÖ•ÃÅëîÅ±’§ÉÄÅ±ÑÅ—…Ω•Õß°µîÅ¡ï…ÕΩππî∏Å8ù•πŸïπ—îÅÖ’ç’πîÅ•πôΩ…µÖ—•Ω∏Åï–Å’—•±•ÕîÅ±ïÃÅπΩ—ïÃÅ’π•≈’ïµïπ–ÅçΩµµîÅÕΩ’…çîÅôÖç—’ï±±î∞ÅÕÖπÃÅ…ï¡…Ωë’•…îÅëîÅπΩ—îÅ•π—ï…πî∏Å3äe•π—…Ωë’ç—•Ω∏ÅëîÅ…ïµï…ç•ïµïπ–ÅïÕ–Åì•´ÄÅÖôô•ç£•îÅÖŸÖπ–ÅŸΩ—…îÅ—ï·—î∏Å9îÅ±ÑÅÀ•√•—ïËÅ©ÖµÖ•Ã∏Å1îÅ¡…ïµ•ï»Å¡Ö…Öù…Ö¡°îÅëΩ•–ÅçΩµµïπçï»Åë•…ïç—ïµïπ–Å¡Ö»ÉäqYΩ’ÃÅÕΩ’°Ö•—ïËÅëïÃÅ…ïπÕï•ùπïµïπ—ÃÅçΩπçï…πÖπ–Å±ÑÅôΩ…µÖ—•Ωªäõät∏Å9îÅ¡À•Õïπ—îÅ©ÖµÖ•ÃÅ±ÑÅëïµÖπëîÅëîÅ…ïπÕï•ùπïµïπ—ÃÅçΩµµîÅ’∏ÅÕΩ’°Ö•–ÅëîÅœäe•πÕç…•…î∞Åìäe•π”•ù…ï»ÅΩ‘ÅëîÅÕ’•Ÿ…îÅ±ÑÅôΩ…µÖ—•Ω∏∏Å;äe’—•±•ÕïËÅ¡ÖÃÅ±ïÃÅï·¡…ïÕÕ•ΩπÃÉäq5ï…ç§Å¡Ω’»Å±îÅ—ïµ¡œät∞Éäq±Ω…ÃÅëîÅπΩ—…îÉ•ç°ÖπùóätÅΩ‘ÉäqπΩ—…îÉ•ç°ÖπùîÅÖ‘ÅÕ’©ï–Åëóät∏ÅU∏ÅÕΩ’°Ö•–Å…ÖπçîÅQ…ÖŸÖ•∞Å∏ùïÕ–Å©ÖµÖ•ÃÅ’πîÅëïµÖπëîÅì•¡Ωœ•î∞Åï∏ÅçΩ’…ÃÅΩ‘ÅŸÖ±•ì•î∏ÅMÖπÃÅÕ—Ö—’–Åï·¡±•ç•—î∞Å•πë•≈’ïËÄËÉ
¨ÅYΩ’ÃÅÕΩ’°Ö•—ïËÉ•—’ë•ï»ÅÖŸïåÅπΩ—…îÉ•≈’•¡îÅ±ÑÅ¡ΩÕÕ•â•±•”§Åìäe’πîÅëïµÖπëîÅëîÅô•πÖπçïµïπ–ÅÖ’¡À°ÃÅëîÅ…ÖπçîÅQ…ÖŸÖ•∞∏É
ÏÅ1îÅ9ALÅïÕ–ÅÖ¡¡±•çÖâ±îÅ’π•≈’ïµïπ–ÅÖ’‡ÅôΩ…µÖ—•ΩπÃÅALÅï–ÅÕ@ÄËÅ¡Ω’»Å—Ω’—îÅÖ’—…îÅôΩ…µÖ—•Ω∏∞Å…ïπŸΩ•îÅ’πîÅç°áππîÅŸ•ëîÅëÖπÃÅçπÖ¡Õ}µïÕÕÖùîÅï–Å∏ùÖ©Ω’—îÅÖ’ç’πîÉ•—Ö¡îÅ9ALÅΩ‘ÅçÖ…—îÅ¡…ΩôïÕÕ•Ωππï±±î∏Å•Õ—•πù’îÅÖâÕïπçîÅëîÅçÖ…—î∞ÅÖ’—Ω…•ÕÖ—•Ω∏Å¡À•Ö±Öâ±î∞ÅëïµÖπëîÅ—…ÖπÕµ•Õî∞Åï·¡•…Ö—•Ω∏Åï–Å…ïô’ÃÅ9AL∏Å9îÅÀ•√°—îÅ¡ÖÃÅµΩ–Å¡Ω’»ÅµΩ–Å±îÅ—Öâ±ïÖ‘ÅôÖç—’ï∞∏ààà(ÄÄÄÅ’Õï»ÄÙÅ©ÕΩ∏πë’µ¡Ã°ÏâçΩëï}ôΩ…µÖ—•Ω∏àËÅôΩ…µÖ—•Ωπ}çΩëî∞Äâ•π—•—’±ï}ôΩ…µÖ—•Ω∏àËÅçΩπô•ùlâ±Öâï∞ât∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâôÖ•—Õ}Ö’—Ω…•ÕïÃàËÅôÖç—ÕÙ∞ÅïπÕ’…ï}ÖÕç•§ıÖ±Õî§(ÄÄÄÅô•…Õ—}πÖµîÄÙÅ}Õïç…ï—Ö…•Ö—}ë•Õ¡±ÖÂ}ô•…Õ—}πÖµî°ïπ—…‰πùï–†â¡…ïπΩ¥à§ÅΩ»ÅçΩπ—Öç–πùï–†â¡…ïπΩ¥à§ÅΩ»ÅÕ—»°ïπ—…‰πùï–†âπΩ¥à§ÅΩ»Äàà§πÕ¡±•–†àÄà•l¡t§(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅçΩπ—ïπ–ÄÙÅ}ŸÖ±•ëÖ—ï}Õïç…ï—Ö…•Ö—}Ö•}çΩπ—ïπ–†(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}Ö§°ÕÂÕ—ï¥∞Å’Õï»∞ÅµÖ·}—Ω≠ïπÃÙ‰¿¿§∞ÅôÖ±±âÖç¨∞Åïπ—…‰∞Åô•…Õ—}πÖµî(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅï·çï¡–Å·çï¡—•Ω∏ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ¡…•π–†âΩµ¡—îÅ…ïπë‘Å%Å•πë•Õ¡Ωπ•â±î∞ÅôÖ±±âÖç¨Åì•—ï…µ•π•Õ—îÄËà∞Åï·å§(ÄÄÄÄÄÄÄÅçΩπ—ïπ–ÄÙÅôÖ±±âÖç¨(ÄÄÄÅÕ’â©ïç–ÄÙÅòâYΩ—…îÅ¡…Ω©ï–ÅÌçΩπô•úπùï–†ùÕ°Ω…–ú§ÅΩ»ÅçΩπô•ùlù±Öâï∞ùuÙÉäLÅ±îÅÀ•Õ’∑§ÅëîÅπΩ—…îÉ•ç°Öπùîà(ÄÄÄÅçΩπ—ï·–ÄÙÅë•ç–°¡…ïπΩ¥ıô•…Õ—}πÖµî∞ÅôΩ…µÖ—•Ω∏ıçΩπô•ú∞Åïπ—…‰ıïπ—…‰∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—ïπ–ıçΩπ—ïπ–∞Å¡…Ω©ïç—}…Ω›Ãı}Õïç…ï—Ö…•Ö—}¡…Ω©ïç—}…Ω›Ã°ïπ—…‰∞ÅçΩπô•ú§∞ÅÖ¡¡Ω•π—µïπ–ı}Õïç…ï—Ö…•Ö—}…ëÿ°ïπ—…‰§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ’¡çΩµ•πù}ÕïÕÕ•ΩπÃı}Õïç…ï—Ö…•Ö—}’¡çΩµ•πù}ÕïÕÕ•Ωπ}ù…Ω’¡Ã°ôΩ…µÖ—•Ωπ}çΩëî∞Åïπ—…‰πùï–†âôΩ…µÖ—•Ωπ}ëÖ—ï}ÕΩ’°Ö•—ïîà∞Äàà§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ≈’Ω—ï}’…∞ıïπ—…‰πùï–†âëïŸ•Õ}’…∞à∞Äàà§∞Å±ΩùΩ}Õ…åı±ΩùΩ}Õ…å∞ÅÖ•}’…∞ıMIQI%Q}%}UI0§(ÄÄÄÅ°—µ±}âΩë‰ÄÙÅ…ïπëï…}—ïµ¡±Ö—î†(ÄÄÄÄÄÄÄÄâïµÖ•±ÃΩïµÖ•±}…ïÕ’µï}ïç°Öπùï}•π—ïù…Ö±îπ°—µ∞à∞(ÄÄÄÄÄÄÄÄ®©çΩπ—ï·–(ÄÄÄÄ§(ÄÄÄÅ¡±Ö•∏ÄÙÅ…ïπëï…}—ïµ¡±Ö—î†âïµÖ•±ÃΩïµÖ•±}…ïÕ’µï}ïç°Öπùï}•π—ïù…Ö±îπ—·–à∞Ä®©çΩπ—ï·–§(ÄÄÄÅ…ï—’…∏ÅÕ’â©ïç–∞Å¡±Ö•∏∞Å°—µ±}âΩë‰(()ëïòÅ}Õïç…ï—Ö…•Ö—}¡…ïŸ•ï›}ëÖ—Ñ°Õç°ïë’±ïêıÖ±Õî§Ë(ÄÄÄÅ…ï—’…∏Ä°Ï(ÄÄÄÄÄÄÄÄâ•êàËÄâ¡…ïŸ•ï‹µÕïç…ï—Ö…•Ö–à∞ÄâôΩ…µÖ—•Ω∏àËÄâALà∞ÄâπΩ¥àËÄâ≥•µïπ–Å5Ö…—•∏à∞(ÄÄÄÄÄÄÄÄâïµÖ•∞àËÄâ¡…ïŸ•ï›ï·Öµ¡±îπ•πŸÖ±•êà∞Äâ—ï±ï¡°ΩπîàËÄà¿ÿ¿¿¿¿¿¿¿¿à∞(ÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ωπ}ëÖ—ï}ÕΩ’°Ö•—ïîàËÄâ——îÅìäeÈ’»ÉäPÅë‘Ä‹ÅÕï¡—ïµâ…îÅÖ‘Ä‰ÅΩç—Ωâ…îÄ»¿»ÿà∞(ÄÄÄÄÄÄÄÄâç¡ô}çΩπÕ’±—îàËÄâ=U$à∞Äâç¡ô}µΩπ—Öπ–àËÄà»¿¿¿à∞Äâô…Öπçï}—…ÖŸÖ•∞àËÄâ=U$à∞(ÄÄÄÄÄÄÄÄâô•πÖπçïµïπ—}¡ï…ÕºàËÄâ=U$à∞Äâ•ëïπ—•—ï}π’µï…•≈’îàËÄâ=U$à∞ÄâçπÖ¡Õ}Ω¨àËÄâ9=8à∞(ÄÄÄÄÄÄÄÄâëïŸ•ÃàËÄâ=U$à∞Äâ…ëŸ}Õ—Ö—’ÃàËÄâÕç°ïë’±ïêàÅ•òÅÕç°ïë’±ïêÅï±ÕîÄâπΩπîà∞(ÄÄÄÄÄÄÄÄâ…ëŸ}ëÖ—îàËÄàƒ‘ÅÕï¡—ïµâ…îÄ»¿»ÿàÅ•òÅÕç°ïë’±ïêÅï±ÕîÄàà∞Äâ…ëŸ}—•µîàËÄàƒ¿ËÃ¿àÅ•òÅÕç°ïë’±ïêÅï±ÕîÄàà∞(ÄÄÄÄÄÄÄÄâ…ëŸ}µΩëîàËÄâŸ•Õ•ΩçΩπõ•…ïπçîàÅ•òÅÕç°ïë’±ïêÅï±ÕîÄàà∞(ÄÄÄÅÙ∞ÅÏâ¡…ïπΩ¥àËÄâ≥•µïπ–âÙ§(()Ö¡¿π…Ω’—î†àΩÖëµ•∏ΩÕïç…ï—Ö…•Ö–ΩïµÖ•∞µ¡…ïŸ•ï‹à§)±Ωù•π}…ï≈’•…ïê)ëïòÅÕïç…ï—Ö…•Ö—}ïµÖ•±}¡…ïŸ•ï‹†§Ë(ÄÄÄÅïπ—…‰∞ÅçΩπ—Öç–ÄÙÅ}Õïç…ï—Ö…•Ö—}¡…ïŸ•ï›}ëÖ—Ñ°…ï≈’ïÕ–πÖ…ùÃπùï–†âÕçïπÖ…•ºà§ÄÙÙÄâÕç°ïë’±ïêà§(ÄÄÄÅ|∞Å|∞Å°—µ±}âΩë‰ÄÙÅ}â’•±ë}Õïç…ï—Ö…•Ö—}ôΩ±±Ω›’¡}ïµÖ•∞†(ÄÄÄÄÄÄÄÅïπ—…‰∞ÅçΩπ—Öç–∞Å±ΩùΩ}Õ…åı’…±}ôΩ»†âÕ—Ö—•åà∞Åô•±ïπÖµîÙâ±Ωùºπ¡πúà∞Å}ï·—ï…πÖ∞ıQ…’î§(ÄÄÄÄ§(ÄÄÄÅ…ï—’…∏Å°—µ±}âΩë‰(()Ö¡¿π…Ω’—î†àΩÖ¡§ΩÖëµ•∏ΩÕïç…ï—Ö…•Ö–ΩïµÖ•∞µ¡…ïŸ•ï‹ΩÕïπêà∞Åµï—°ΩëÃılâA=MPât§)±Ωù•π}…ï≈’•…ïê)ëïòÅÕïπë}Õïç…ï—Ö…•Ö—}ïµÖ•±}¡…ïŸ•ï‹†§Ë(ÄÄÄÅ…ïç•¡•ïπ–ÄÙÅÕ—»†°…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ§πùï–†âïµÖ•∞à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–Å…îπô’±±µÖ—ç†°»âmyqÕt≠myqÕt≠pπmyqÕt¨à∞Å…ïç•¡•ïπ–§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâΩ¨àËÅÖ±Õî∞Äâï……Ω»àËÄâë…ïÕÕîÅëîÅ—ïÕ–Å•πŸÖ±•ëîâÙ§∞Ä–¿¿(ÄÄÄÅïπ—…‰∞ÅçΩπ—Öç–ÄÙÅ}Õïç…ï—Ö…•Ö—}¡…ïŸ•ï›}ëÖ—Ñ°Ö±Õî§(ÄÄÄÅÕ’â©ïç–∞Å¡±Ö•∏∞Å°—µ±}âΩë‰ÄÙÅ}â’•±ë}Õïç…ï—Ö…•Ö—}ôΩ±±Ω›’¡}ïµÖ•∞°ïπ—…‰∞ÅçΩπ—Öç–§(ÄÄÄÅ•òÅπΩ–ÅÕïπë}ïµÖ•±}°—µ∞°…ïç•¡•ïπ–∞ÅòâmQMQtÅÌÕ’â©ïç—Ùà∞Å¡±Ö•∏∞Å°—µ±}âΩë‰§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâΩ¨àËÅÖ±Õî∞Äâï……Ω»àËÄã%ç°ïåÅëîÅ≥äeïπŸΩ§ÅëîÅ—ïÕ–âÙ§∞Ä‘¿»(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâΩ¨àËÅQ…’ïÙ§()ëïòÅ}ç…µ}πΩ}ÖπÕ›ï…}µïÕÕÖùî°çΩπ—Öç–§Ë(ÄÄÄÄààâ	’•±êÅ—°îÅÕ°Ö…ïêÅîµµÖ•∞ΩM5LÅôΩ±±Ω‹µ’¿ÅÕïπ–ÅÖô—ï»ÅÖ∏Å’πÖπÕ›ï…ïêÅçÖ±∞∏ààà(ÄÄÄÅôΩ…µÖ—•Ω∏ÄÙÅÕ—»°çΩπ—Öç–πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»ÄâŸΩ—…îÅôΩ…µÖ—•Ω∏à§πÕ—…•¿†§(ÄÄÄÅëïÕ¡}—Â¡îÄÙÅÕ—»°çΩπ—Öç–πùï–†âëïÕ¡}—Â¡îà§ÅΩ»Äàà§πÕ—…•¿†§π’¡¡ï»†§(ÄÄÄÅôΩ…µÖ—•Ωπ}≠ï‰ÄÙÅÏ(ÄÄÄÄÄÄÄÄâALàËÄâALà∞ÄâÕ@àËÄâÕ@à∞ÄâMM%@ÄƒàËÄâMM%@à∞ÄâMM%@àËÄâMM%@à∞(ÄÄÄÄÄÄÄÄâ°Ö’ôôï’»ÅYQàËÄâYQà∞ÄâYQàËÄâYQà∞(ÄÄÄÅÙπùï–°ôΩ…µÖ—•Ω∏§(ÄÄÄÅ•òÅôΩ…µÖ—•Ω∏ÄÙÙÄâM@àË(ÄÄÄÄÄÄÄÅôΩ…µÖ—•Ωπ}≠ï‰ÄÙÄâMA}YàÅ•òÅëïÕ¡}—Â¡îÄÙÙÄâYàÅï±ÕîÄâMA}%9%Pà(ÄÄÄÅçΩπô•úÄÙÅMIQI%Q}=I5Q%=9Lπùï–°ôΩ…µÖ—•Ωπ}≠ï‰∞ÅÌÙ§(ÄÄÄÅô’±±}πÖµîÄÙÅA19}=I5Q%=9Lπùï–°ôΩ…µÖ—•Ωπ}≠ï‰§ÅΩ»ÅçΩπô•úπùï–†â±Öâï∞à§ÅΩ»ÅçΩπô•úπùï–†âÕ°Ω…–à§ÅΩ»ÅôΩ…µÖ—•Ω∏(ÄÄÄÅçÖ±ïπë±Â}’…∞ÄÙÅçΩπô•úπùï–†âçÖ±ïπë±‰à§ÅΩ»Äâ°——¡ÃËºΩçÖ±ïπë±‰πçΩ¥Ω•π—ïù…Ö±ïÖçÖëïµ‰ΩôΩ…µÖ—•Ω∏à(ÄÄÄÅ…ï—’…∏Ä†(ÄÄÄÄÄÄÄÄâ	Ωπ©Ω’»±qπq∏à(ÄÄÄÄÄÄÄÅòâ+äeÖ§Å—ïπ”§ÅëîÅŸΩ’ÃÅ©Ω•πë…îÅçΩπçï…πÖπ–ÅπΩ—…îÅôΩ…µÖ—•Ω∏ÅÌô’±±}πÖµïÙ∞ÅµÖ•ÃÅ©îÅªäeÖ§ÅµÖ±°ï’…ï’Õïµïπ–Å¡ÖÃÅÀ•’ÕÕ§ÉÄÅŸΩ’ÃÅ©Ω•πë…îπqπq∏à(ÄÄÄÄÄÄÄÄâYΩ’ÃÅ¡Ω’ŸïËÅπΩ’ÃÅ…Ö¡¡ï±ï»ÅÖ‘Ä¿–Ä»»Ä–‹Ä¿‹Äÿ‡ÅÖô•∏Å≈’îÅπΩ’ÃÅ¡’•ÕÕ•ΩπÃÅŸΩ’ÃÅ¡À•Õïπ—ï»ÅπΩ—…îÅôΩ…µÖ—•Ω∏Åï∏Åì•—Ö•±ÃÅï–ÅÀ•¡Ωπë…îÉÄÅ—Ω’—ïÃÅŸΩÃÅ≈’ïÕ—•ΩπÃ∏Äà(ÄÄÄÄÄÄÄÄâYΩ’ÃÅ¡Ω’ŸïËÉ•ùÖ±ïµïπ–ÅµîÅçΩπ—Öç—ï»ÅÕ’»ÅµΩ∏Å¡Ω…—Öâ±îÅÖ‘Ä¿‹Ä–ÃÄ‘‡Ä»»Äÿ–πqπq∏à(ÄÄÄÄÄÄÄÄâYΩ’ÃÅ¡Ω’ŸïËÉ•ùÖ±ïµïπ–ÅÀ•Õï…Ÿï»Åë•…ïç—ïµïπ–Å’∏ÅçÀ•πïÖ‘Å”•≥•¡°Ωπ•≈’îÅÖŸïåÅπΩ—…îÉ•≈’•¡îÅï∏Åç±•≈’Öπ–ÅÕ’»Å±îÅ±•ï∏ÅÕ’•ŸÖπ–ÄËÄà(ÄÄÄÄÄÄÄÅòâÌçÖ±ïπë±Â}’…±ıqπq∏à(ÄÄÄÄÄÄÄÄâ9Ω’ÃÅ…ïÕ—ΩπÃÉÄÅŸΩ—…îÅë•Õ¡ΩÕ•—•Ω∏Åï–ÅŸΩ’ÃÅ…ïµï…ç•ΩπÃÅ¡Ö»ÅÖŸÖπçîÅ¡Ω’»ÅŸΩ—…îÅ…ï—Ω’»πqπq∏à(ÄÄÄÄÄÄÄÄâ	•ï∏ÅçΩ…ë•Ö±ïµïπ–±qπqπÖÕÕÖπë…îÅ59IqπIïÕ¡ΩπÕÖâ±îÅçΩµµï…ç•Ö±îÅ%π”•ù…Ö±îÅçÖëïµ‰à(ÄÄÄÄ§(()ëïòÅ}ç…µ}πÖµïë}—ïµ¡±Ö—î°ëÖ—Ñ∞Å≠•πê∞ÅπÖµî§Ë(ÄÄÄÄààâIï—’…∏ÅÑÅµïÕÕÖùîÅ—ïµ¡±Ö—îÅâ‰Å•—ÃÅ’Õï»µôÖç•πúÅπÖµîÄ°çÖÕîÅ•πÕïπÕ•—•Ÿî§∏ààà(ÄÄÄÅï·¡ïç—ïêÄÙÅÕ—»°πÖµî§πÕ—…•¿†§πçÖÕïôΩ±ê†§(ÄÄÄÅ…ï—’…∏Åπï·–†°•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅëÖ—Ñπùï–°òâç…µ}Ì≠•πëı}—ïµ¡±Ö—ïÃà∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅÕ—»°•—ï¥πùï–†âπΩ¥à§ÅΩ»Äàà§πÕ—…•¿†§πçÖÕïôΩ±ê†§ÄÙÙÅï·¡ïç—ïê§∞Å9Ωπî§(()I5}EU%-}I5%9I}Q5A1QÄÙÄâIÖ¡¡ï∞ÅëÖπÃÄ’µ•∏à(()ëïòÅ}ç…µ}Õïπë}Ö¡¡Ω•π—µïπ—}ôΩ±±Ω›’¿°ëÖ—Ñ∞ÅçΩπ—Öç–∞Å—ïµ¡±Ö—ï}πÖµî§Ë(ÄÄÄÄààâMïπêÅ—°îÅîµµÖ•∞ÅÖπêÅM5LÅâïÖ…•πúÅ—°îÅçΩπô•ù’…ïêÅÖ¡¡Ω•π—µïπ–Å—ïµ¡±Ö—îÅπÖµî∏ààà(ÄÄÄÅëï±•Ÿï…‰ÄÙÅÏâÕµÃàËÅÖ±Õî∞ÄâïµÖ•∞àËÅÖ±ÕïÙ(ÄÄÄÅÕµÕ}—ïµ¡±Ö—îÄÙÅ}ç…µ}πÖµïë}—ïµ¡±Ö—î°ëÖ—Ñ∞ÄâÕµÃà∞Å—ïµ¡±Ö—ï}πÖµî§(ÄÄÄÅïµÖ•±}—ïµ¡±Ö—îÄÙÅ}ç…µ}πÖµïë}—ïµ¡±Ö—î°ëÖ—Ñ∞ÄâïµÖ•∞à∞Å—ïµ¡±Ö—ï}πÖµî§(ÄÄÄÄåÅ!•Õ—Ω…•çÖ∞ÅëÖ—ÖâÖÕïÃÅµÖ‰ÅπΩ–ÅÂï–ÅçΩπ—Ö•∏Å—°îÅπï›±‰ÅπÖµïêÅ—ïµ¡±Ö—ïÃ∏Å-ïï¿(ÄÄÄÄåÅ—°îÅï·•Õ—•πúÅµ•ÕÕïêµçÖ±∞ÅôΩ±±Ω‹µ’¿ÅΩ¡ï…Ö—•ΩπÖ∞Å’π—•∞ÅÖ∏ÅÖëµ•π•Õ—…Ö—Ω»(ÄÄÄÄåÅÕÖŸïÃÅ•—ÃÅç’Õ—Ω¥ÅŸï…Õ•ΩπÃÅ•∏Å—°îÅ±•â…Ö…‰∏(ÄÄÄÅ•òÅ—ïµ¡±Ö—ï}πÖµîÄÙÙÄâAÖÃÅëîÅÀ•¡ΩπÕîÅÖ¡¡ï∞àË(ÄÄÄÄÄÄÄÅôÖ±±âÖç¨ÄÙÅ}ç…µ}πΩ}ÖπÕ›ï…}µïÕÕÖùî°çΩπ—Öç–§(ÄÄÄÄÄÄÄÅÕµÕ}—ïµ¡±Ö—îÄÙÅÕµÕ}—ïµ¡±Ö—îÅΩ»ÅÏâçΩπ—ïπ‘àËÅôÖ±±âÖç≠Ù(ÄÄÄÄÄÄÄÅïµÖ•±}—ïµ¡±Ö—îÄÙÅïµÖ•±}—ïµ¡±Ö—îÅΩ»ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ’©ï–àËÅòâ%π”•ù…Ö±îÅçÖëïµ‰ÉäPÅYΩ—…îÅôΩ…µÖ—•Ω∏ÅÌçΩπ—Öç–πùï–†ùôΩ…µÖ—•Ω∏ú§ÅΩ»ÄúùÙàπÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—ïπ‘àËÅôÖ±±âÖç¨∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÅ•òÅÕµÕ}—ïµ¡±Ö—îÅÖπêÅçΩπ—Öç–πùï–†â—ï±ï¡°Ωπîà§Ë(ÄÄÄÄÄÄÄÅÕµÕ}âΩë‰ÄÙÅ}ç…µ}…ïÕΩ±Ÿï}µïÕÕÖùï}ŸÖ…•Öâ±ïÃ†(ÄÄÄÄÄÄÄÄÄÄÄÅÕµÕ}—ïµ¡±Ö—îπùï–†âçΩπ—ïπ‘à§∞ÅçΩπ—Öç–∞ÅëÖ—Ö}Õ—Ω…îıëÖ—Ñ(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅëï±•Ÿï…ÂlâÕµÃâtÄÙÅÕïπë}ÕµÃ°çΩπ—Öç–πùï–†â—ï±ï¡°Ωπîà§∞ÅÕµÕ}âΩë‰§(ÄÄÄÄÄÄÄÅ•òÅëï±•Ÿï…ÂlâÕµÃâtË(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞ÄâÕµÃà∞ÅòâM5LÉ
¨ÅÌ—ïµ¡±Ö—ï}πÖµïÙÉ
ÏÅïπŸΩÁ§à∞ÅÕµÕ}âΩë‰∞ÅÕµÕ}âΩë‰§(ÄÄÄÅ•òÅïµÖ•±}—ïµ¡±Ö—îÅÖπêÅçΩπ—Öç–πùï–†âµÖ•∞à§Ë(ÄÄÄÄÄÄÄÅïµÖ•±}âΩë‰ÄÙÅ}ç…µ}…ïÕΩ±Ÿï}µïÕÕÖùï}ŸÖ…•Öâ±ïÃ†(ÄÄÄÄÄÄÄÄÄÄÄÅïµÖ•±}—ïµ¡±Ö—îπùï–†âçΩπ—ïπ‘à§∞ÅçΩπ—Öç–∞Å°—µ∞ıQ…’î∞ÅëÖ—Ö}Õ—Ω…îıëÖ—Ñ(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅïµÖ•±}°—µ∞ÄÙÅ}ç…µ}ïµÖ•±}°—µ∞°ïµÖ•±}âΩë‰∞ÅçΩπ—Öç–§(ÄÄÄÄÄÄÄÅÕ’â©ïç–ÄÙÅ}ç…µ}…ïÕΩ±Ÿï}µïÕÕÖùï}ŸÖ…•Öâ±ïÃ†(ÄÄÄÄÄÄÄÄÄÄÄÅïµÖ•±}—ïµ¡±Ö—îπùï–†âÕ’©ï–à§ÅΩ»Å—ïµ¡±Ö—ï}πÖµî∞ÅçΩπ—Öç–∞ÅëÖ—Ö}Õ—Ω…îıëÖ—Ñ(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ¡±Ö•∏ÄÙÅ°—µ±}µΩë’±îπ’πïÕçÖ¡î°…îπÕ’à°»âqÃ¨à∞ÄàÄà∞Å…îπÕ’à°»àÒmx˘t¨¯à∞ÄàÄà∞ÅïµÖ•±}âΩë‰§§§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅëï±•Ÿï…ÂlâïµÖ•∞âtÄÙÅ}ç…µ}Õïπë}ïµÖ•±}°—µ∞†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–πùï–†âµÖ•∞à§∞ÅÕ’â©ïç–∞Å¡±Ö•∏∞ÅïµÖ•±}°—µ∞∞(ÄÄÄÄÄÄÄÄÄÄÄÅ—ïµ¡±Ö—îıïµÖ•±}—ïµ¡±Ö—î∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ•òÅëï±•Ÿï…ÂlâïµÖ•∞âtË(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞ÄâïµÖ•∞à∞ÅòâµµÖ•∞É
¨ÅÌ—ïµ¡±Ö—ï}πÖµïÙÉ
ÏÅïπŸΩÁ§à∞ÅÕ’â©ïç–∞ÅïµÖ•±}°—µ∞§(ÄÄÄÅ…ï—’…∏Åëï±•Ÿï…‰(()ëïòÅ}ç…µ}Õïπë}ô—}…ïô’ÕÖ±}µïÕÕÖùïÃ°ëÖ—Ñ∞ÅçΩπ—Öç–§Ë(ÄÄÄÄààâπŸΩ•îÅ±ïÃÅµΩì°±ïÃÅîµµÖ•∞Åï–ÅM5LÅ±Ω…ÃÅêù’∏ÅπΩ’ŸïÖ‘Å…ïô’ÃÅ…ÖπçîÅQ…ÖŸÖ•∞∏ààà(ÄÄÄÅ•òÅ°ÖÕ}Ö¡¡}çΩπ—ï·–†§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å}ç…µ}Õïπë}Ö¡¡Ω•π—µïπ—}ôΩ±±Ω›’¿°ëÖ—Ñ∞ÅçΩπ—Öç–∞ÄâPÅ…ïô’œ§à§(ÄÄÄÅ›•—†ÅÖ¡¿πÖ¡¡}çΩπ—ï·–†§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å}ç…µ}Õïπë}Ö¡¡Ω•π—µïπ—}ôΩ±±Ω›’¿°ëÖ—Ñ∞ÅçΩπ—Öç–∞ÄâPÅ…ïô’œ§à§(()ëïòÅ}ç…µ}Ö§°ÕÂÕ—ïµ}¡…Ωµ¡–∞Å’Õï…}¡…Ωµ¡–∞ÅµÖ·}—Ω≠ïπÃÙ‘¿¿§Ë(ÄÄÄÄààâM•πù±î∞Å—ïÕ—Öâ±îÅïπ—…‰Å¡Ω•π–ÅôΩ»Å—°îÅI4Å›…•—•πúÅÖÕÕ•Õ—Öπ—Ã∏ààà(ÄÄÄÅ•òÅπΩ–ÅΩÃπùï—ïπÿ†â=A9%}A%}-dà§Ë(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅI’π—•µï……Ω»†â=A9%}A%}-dÅπΩ∏ÅçΩπô•ù’À•îà§(ÄÄÄÅç±•ïπ–ÄÙÅ=¡ïπ$°Ö¡•}≠ï‰ıΩÃπùï—ïπÿ†â=A9%}A%}-dà§∞Å—•µïΩ’–Ù»¿§(ÄÄÄÅ…ïÕ¡ΩπÕîÄÙÅç±•ïπ–πç°Ö–πçΩµ¡±ï—•ΩπÃπç…ïÖ—î†(ÄÄÄÄÄÄÄÅµΩëï∞ıΩÃπùï—ïπÿ†â=A9%}5=0à∞Äâù¡–¥—ºµµ•π§à§∞(ÄÄÄÄÄÄÄÅµïÕÕÖùïÃımÏâ…Ω±îàËÄâÕÂÕ—ï¥à∞ÄâçΩπ—ïπ–àËÅÕÂÕ—ïµ}¡…Ωµ¡—Ù∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÏâ…Ω±îàËÄâ’Õï»à∞ÄâçΩπ—ïπ–àËÅ’Õï…}¡…Ωµ¡—ıt∞(ÄÄÄÄÄÄÄÅ—ïµ¡ï…Ö—’…îÙ¿∏»∞ÅµÖ·}—Ω≠ïπÃıµÖ·}—Ω≠ïπÃ∞(ÄÄÄÄ§(ÄÄÄÅ…ïÕ’±–ÄÙÄ°…ïÕ¡ΩπÕîπç°Ω•çïÕl¡tπµïÕÕÖùîπçΩπ—ïπ–ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–Å…ïÕ’±–Ë(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†âK•¡ΩπÕîÅŸ•ëîÅë‘ÅÕï…Ÿ•çîÅ%à§(ÄÄÄÅ…ï—’…∏Å…ïÕ’±–(()ëïòÅ}çÖπë•ëÖ—ï}Ö•}çΩπ—ïπ–°…ïÕ¡ΩπÕî∞ÅÖ±±Ω›}µÖ…≠ëΩ›∏ıÖ±Õî§Ë(ÄÄÄÅç°Ω•çïÃÄÙÅùï—Ö——»°…ïÕ¡ΩπÕî∞Äâç°Ω•çïÃà∞Å9Ωπî§(ÄÄÄÅ•òÅπΩ–Åç°Ω•çïÃË(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅÖπë•ëÖ—ï%IïÕ¡ΩπÕï……Ω»†âïµ¡—Â}…ïÕ¡ΩπÕîà§(ÄÄÄÅç°Ω•çîÄÙÅç°Ω•çïÕl¡t(ÄÄÄÅ•òÅùï—Ö——»°ç°Ω•çî∞Äâô•π•Õ°}…ïÖÕΩ∏à∞Å9Ωπî§ÄÙÙÄâ±ïπù—†àË(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅÖπë•ëÖ—ï%IïÕ¡ΩπÕï……Ω»†â—…’πçÖ—ïë}…ïÕ¡ΩπÕîà∞Äâ3äeÖπÖ±ÂÕîÅü•ª•À•îÉ•—Ö•–Å•πçΩµ¡≥°—î∏ÅYï’•±±ïËÅÀ•ïÕÕÖÂï»∏à§(ÄÄÄÅµïÕÕÖùîÄÙÅùï—Ö——»°ç°Ω•çî∞ÄâµïÕÕÖùîà∞Å9Ωπî§(ÄÄÄÅ•òÅµïÕÕÖùîÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅÖπë•ëÖ—ï%IïÕ¡ΩπÕï……Ω»†âïµ¡—Â}…ïÕ¡ΩπÕîà§(ÄÄÄÅ•òÅùï—Ö——»°µïÕÕÖùî∞Äâ…ïô’ÕÖ∞à∞Å9Ωπî§Ë(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅÖπë•ëÖ—ï%IïÕ¡ΩπÕï……Ω»†âµΩëï±}…ïô’ÕÖ∞à§(ÄÄÄÅçΩπ—ïπ–ÄÙÄ°ùï—Ö——»°µïÕÕÖùî∞ÄâçΩπ—ïπ–à∞Å9Ωπî§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—ïπ–Ë(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅÖπë•ëÖ—ï%IïÕ¡ΩπÕï……Ω»†âïµ¡—Â}…ïÕ¡ΩπÕîà§(ÄÄÄÅ•òÅÖ±±Ω›}µÖ…≠ëΩ›∏Ë(ÄÄÄÄÄÄÄÅçΩπ—ïπ–ÄÙÅ…îπÕ’à°»âyqÃ©ÅÅÄ†¸È©ÕΩ∏§˝qÃ®à∞Äàà∞ÅçΩπ—ïπ–∞ÅçΩ’π–Ùƒ∞Åô±ÖùÃı…îπ$§(ÄÄÄÄÄÄÄÅçΩπ—ïπ–ÄÙÅ…îπÕ’à°»âqÃ©ÅÅÅqÃ®êà∞Äàà∞ÅçΩπ—ïπ–∞ÅçΩ’π–Ùƒ§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅÕ—Ö…–∞ÅïπêÄÙÅçΩπ—ïπ–πô•πê†âÏà§∞ÅçΩπ—ïπ–π…ô•πê†âÙà§(ÄÄÄÄÄÄÄÅ•òÅÕ—Ö…–ÄÄ¿ÅΩ»ÅïπêÄÅÕ—Ö…–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅÖπë•ëÖ—ï%IïÕ¡ΩπÕï……Ω»†â•πŸÖ±•ë}©ÕΩ∏à§(ÄÄÄÄÄÄÄÅçΩπ—ïπ–ÄÙÅçΩπ—ïπ—mÕ—Ö…–ÈïπêÄ¨Ä≈t(ÄÄÄÅ…ï—’…∏ÅçΩπ—ïπ–(()ëïòÅ}©ÕΩπ}Õç°ïµÖ}’πÕ’¡¡Ω…—ïê°ï·å§Ë(ÄÄÄÄààâ8ùÖ’—Ω…•ÕîÅ±îÅ…ï¡±§Å≈’îÅ¡Ω’»Å±îÄ–¿¿Åï·¡±•ç•—îÅë‘ÅôΩ’…π•ÕÕï’»∏ààà(ÄÄÄÅ•òÅùï—Ö——»°ï·å∞ÄâÕ—Ö—’Õ}çΩëîà∞Å9Ωπî§ÄÑÙÄ–¿¿Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅ¡…ΩŸ•ëï…}—ï·–ÄÙÄàÄàπ©Ω•∏°Õ—»°ŸÖ±’î§ÅôΩ»ÅŸÖ±’îÅ•∏Ä†(ÄÄÄÄÄÄÄÅùï—Ö——»°ï·å∞ÄâµïÕÕÖùîà∞Äàà§∞Åùï—Ö——»°ï·å∞ÄââΩë‰à∞Äàà§∞Åï·å§§(ÄÄÄÅ±Ω›ï…ïêÄÙÅ¡…ΩŸ•ëï…}—ï·–π±Ω›ï»†§(ÄÄÄÅ…ï—’…∏Äâ©ÕΩπ}Õç°ïµÑàÅ•∏Å±Ω›ï…ïêÅÖπêÅÖπ‰°µÖ…≠ï»Å•∏Å±Ω›ï…ïêÅôΩ»ÅµÖ…≠ï»Å•∏Ä†(ÄÄÄÄÄÄÄÄâπΩ–ÅÕ’¡¡Ω…—ïêà∞ÄâëΩïÃÅπΩ–ÅÕ’¡¡Ω…–à∞Äâ’πÕ’¡¡Ω…—ïêà∞Äâ∏ùïÕ–Å¡ÖÃÅ¡…•ÃÅï∏Åç°Ö…ùîà§§(()ëïòÅ}ç…µ}Ö•}Õ—…’ç—’…ïê°ÕÂÕ—ïµ}¡…Ωµ¡–∞Å’Õï…}¡…Ωµ¡–§Ë(ÄÄÄÅ•òÅπΩ–ÅΩÃπùï—ïπÿ†â=A9%}A%}-dà§Ë(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅI’π—•µï……Ω»†â=A9%}A%}-dÅπΩ∏ÅçΩπô•ù’À•îà§(ÄÄÄÅç±•ïπ–ÄÙÅ=¡ïπ$°Ö¡•}≠ï‰ıΩÃπùï—ïπÿ†â=A9%}A%}-dà§∞Å—•µïΩ’–Ù»¿§(ÄÄÄÅµΩëï∞ÄÙÅΩÃπùï—ïπÿ†â=A9%}5=0à∞Äâù¡–¥—ºµµ•π§à§(ÄÄÄÅçΩµµΩ∏ÄÙÅÏâµΩëï∞àËÅµΩëï∞∞ÄâµïÕÕÖùïÃàËÅmÏâ…Ω±îàËÄâÕÂÕ—ï¥à∞ÄâçΩπ—ïπ–àËÅÕÂÕ—ïµ}¡…Ωµ¡—Ù∞(ÄÄÄÄÄÄÄÅÏâ…Ω±îàËÄâ’Õï»à∞ÄâçΩπ—ïπ–àËÅ’Õï…}¡…Ωµ¡—ıt∞Äâ—ïµ¡ï…Ö—’…îàËÄ¿∏»∞ÄâµÖ·}—Ω≠ïπÃàËÄƒÿ¿¡Ù(ÄÄÄÅÕ—…’ç—’…ïë}ôΩ…µÖ–ÄÙÅÏâ—Â¡îàËÄâ©ÕΩπ}Õç°ïµÑà∞Äâ©ÕΩπ}Õç°ïµÑàËÅÏ(ÄÄÄÄÄÄÄÄâπÖµîàËÄâçÖπë•ëÖ—ï}Ö•}ÖπÖ±ÂÕ•Ãà∞ÄâÕ—…•ç–àËÅQ…’î∞ÄâÕç°ïµÑàËÅ9%Q}%}IMA=9M}M!5ıÙ(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕîÄÙÅç±•ïπ–πç°Ö–πçΩµ¡±ï—•ΩπÃπç…ïÖ—î†®©çΩµµΩ∏∞Å…ïÕ¡ΩπÕï}ôΩ…µÖ–ıÕ—…’ç—’…ïë}ôΩ…µÖ–§(ÄÄÄÄÄÄÄÅçΩπ—ïπ–ÄÙÅ}çÖπë•ëÖ—ï}Ö•}çΩπ—ïπ–°…ïÕ¡ΩπÕî§(ÄÄÄÅï·çï¡–Å·çï¡—•Ω∏ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å}©ÕΩπ}Õç°ïµÖ}’πÕ’¡¡Ω…—ïê°ï·å§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•Õî(ÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕîÄÙÅç±•ïπ–πç°Ö–πçΩµ¡±ï—•ΩπÃπç…ïÖ—î†®©çΩµµΩ∏∞Å…ïÕ¡ΩπÕï}ôΩ…µÖ–ıÏâ—Â¡îàËÄâ©ÕΩπ}Ωâ©ïç–âÙ§(ÄÄÄÄÄÄÄÅçΩπ—ïπ–ÄÙÅ}çÖπë•ëÖ—ï}Ö•}çΩπ—ïπ–°…ïÕ¡ΩπÕî∞ÅÖ±±Ω›}µÖ…≠ëΩ›∏ıQ…’î§(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅëïçΩëïêÄÙÅ©ÕΩ∏π±ΩÖëÃ°çΩπ—ïπ–§(ÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞Å©ÕΩ∏π)M=9ïçΩëï……Ω»§ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅÖπë•ëÖ—ï%IïÕ¡ΩπÕï……Ω»†â•πŸÖ±•ë}©ÕΩ∏à§Åô…Ω¥Åï·å(ÄÄÄÅ…ï—’…∏ÅŸÖ±•ëÖ—ï}çÖπë•ëÖ—ï}Ö•}ÖπÖ±ÂÕ•Ã°ëïçΩëïê§(()}I5}%}91eM%M}1=-LÄÙÅÌÙ)}I5}%}91eM%M}1=-M}UIÄÙÅ—°…ïÖë•πúπ1Ωç¨†§(()ëïòÅâ’•±ë}çÖπë•ëÖ—ï}Ö•}çΩπ—ï·–°çΩπ—Öç—}•ê∞ÅëÖ—Ñı9Ωπî∞ÅπΩ‹ı9Ωπî∞Ä®∞Åôï—ç°}±•Ÿï}ŸÖîıQ…’î§Ë(ÄÄÄÅëÖ—ÑÄÙÅëÖ—ÑÅΩ»Å±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅ-ïÂ……Ω»°çΩπ—Öç—}•ê§(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ›ïëΩòÄÙÅ}›ïëΩô}çΩπ—Öç—}…ïÕΩ’…çïÃ°çΩπ—Öç—}•ê∞ÅëÖ—Ñ§(ÄÄÄÅï·çï¡–Å·çï¡—•Ω∏Ë(ÄÄÄÄÄÄÄÅ›ïëΩòÄÙÅmt(ÄÄÄÅŸÖï}—…Öç≠•πúÄÙÅ9Ωπî(ÄÄÄÅ•òÄ°ôï—ç°}±•Ÿï}ŸÖî(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅÕ—»°çΩπ—Öç–πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§π’¡¡ï»†§ÄÙÙÄâM@à(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅÕ—»°çΩπ—Öç–πùï–†âëïÕ¡}—Â¡îà§ÅΩ»Äàà§πÕ—…•¿†§π’¡¡ï»†§ÄÙÙÄâYà§Ë(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅô…Ω¥Åç…µ}çπÖ¡Õ}—…Öç≠•πúÅ•µ¡Ω…–Å¡…Ω·Â}…ïù±ïµïπ—Ö•…î(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïµΩ—îÄÙÅ¡…Ω·Â}…ïù±ïµïπ—Ö•…î°Ö¡¿∞ÅçΩπ—Öç–∞Å°——¡}ùï–ı…ï≈’ïÕ—Ãπùï–∞Å°——¡}¡ΩÕ–ı…ï≈’ïÕ—Ãπ¡ΩÕ–§(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕîÄÙÅ…ïµΩ—ïl¡tÅ•òÅ•Õ•πÕ—Öπçî°…ïµΩ—î∞Å—’¡±î§Åï±ÕîÅ…ïµΩ—î(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö—’ÃÄÙÅ…ïµΩ—ïl≈tÅ•òÅ•Õ•πÕ—Öπçî°…ïµΩ—î∞Å—’¡±î§Åï±ÕîÅ…ïÕ¡ΩπÕîπÕ—Ö—’Õ}çΩëî(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ…ïÕ¡ΩπÕîπùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§Å•òÅÕ—Ö—’ÃÄÙÙÄ»¿¿Åï±ÕîÅ9Ωπî(ÄÄÄÄÄÄÄÄÄÄÄÅŸÖï}—…Öç≠•πúÄÙÅ¡ÖÂ±ΩÖêπùï–†âŸÖîà§Å•òÅ•Õ•πÕ—Öπçî°¡ÖÂ±ΩÖê∞Åë•ç–§Åï±ÕîÅ9Ωπî(ÄÄÄÄÄÄÄÅï·çï¡–Å·çï¡—•Ω∏ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÄåÅ0ù•πë•Õ¡Ωπ•â•±•”§Åë‘ÅM$ÅÕ—Öù•Ö•…ïÃÅπîÅëΩ•–Å¡ÖÃÅâ±Ω≈’ï»Å—Ω’—îÅ∞ùÖπÖ±ÂÕî∏(ÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¿π±Ωùùï»π›Ö…π•πú†âM’•Ÿ§ÅYÅ•πë•Õ¡Ωπ•â±îÅ¡Ω’»Å∞ùÖπÖ±ÂÕîÅ%Ä†ïÃ§à∞Å—Â¡î°ï·å§π}}πÖµï}|§(ÄÄÄÄÄÄÄÄÄÄÄÅŸÖï}—…Öç≠•πúÄÙÅ9Ωπî(ÄÄÄÅ…ï—’…∏Å}â’•±ë}çÖπë•ëÖ—ï}Ö•}çΩπ—ï·–†(ÄÄÄÄÄÄÄÅçΩπ—Öç–∞ÅëÖ—Ñ∞ÅçÖ±ç’±Ö—ï}çÖπë•ëÖ—ï}•π—ïù…Ö—•Ωπ}ÕçΩ…î†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞ÅëÖ—Ñπùï–†âç…µ}çπÖ¡Õ}ÕçΩ…•πù}ÕπÖ¡Õ°Ω—Ãà∞ÅÌÙ§πùï–°Õ—»°çΩπ—Öç—}•ê§§§∞(ÄÄÄÄÄÄÄÅ›ïëΩò∞ÅπΩ‹∞ÅŸÖï}—…Öç≠•πú§(()ëïòÅùïπï…Ö—ï}çÖπë•ëÖ—ï}Ö•}ÖπÖ±ÂÕ•Ã°çΩπ—ï·–§Ë(ÄÄÄÄààâ•ª°…îÅ’πîÅÕΩ…—•îÅÕ—…’ç—’À•îÅ¡’•ÃÅçΩπÕï…ŸîÅ±ÑÅŸÖ±•ëÖ—•Ω∏Å∑•—•ï»Å±ΩçÖ±î∏ààà(ÄÄÄÅ’Õï…}µïÕÕÖùîÄÙÅ©ÕΩ∏πë’µ¡Ã°ÏâçÖπë•ëÖ—ï}çΩπ—ï·–àËÅçΩπ—ï·—Ù∞ÅïπÕ’…ï}ÖÕç•§ıÖ±Õî§(ÄÄÄÅµΩëï±}…ïÕ’±–ÄÙÅ}ç…µ}Ö•}Õ—…’ç—’…ïê°%}9%Q}MeMQ5}AI=5AP∞Å’Õï…}µïÕÕÖùî§(ÄÄÄÅ…ï—’…∏Åô•πÖ±•Èï}çÖπë•ëÖ—ï}Ö•}ÖπÖ±ÂÕ•Ã°µΩëï±}…ïÕ’±–∞ÅçΩπ—ï·–§(()ëïòÅùï—}çÖπë•ëÖ—ï}Ö•}ÖπÖ±ÂÕ•Õ}Õ—Ö—î°çΩπ—Öç—}•ê∞ÅëÖ—Ñı9Ωπî§Ë(ÄÄÄÅëÖ—ÑÄÙÅëÖ—ÑÅΩ»Å±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅÕ—Ω…ïêÄÙÅëÖ—Ñπùï–†âç…µ}Ö•}çÖπë•ëÖ—ï}ÖπÖ±ÂÕïÃà∞ÅÌÙ§πùï–°çΩπ—Öç—}•ê§(ÄÄÄÅïπÖâ±ïêÄÙÅâΩΩ∞°ΩÃπùï—ïπÿ†â=A9%}A%}-dà§§(ÄÄÄÅ•òÅπΩ–ÅÕ—Ω…ïêË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏâïπÖâ±ïêàËÅïπÖâ±ïê∞ÄâÕ—Ö—’ÃàËÄâπïŸï…}ùïπï…Ö—ïêàÅ•òÅïπÖâ±ïêÅï±ÕîÄâ’πÖŸÖ•±Öâ±îà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö±îàËÅÖ±Õî∞Äâ…ïÕ’±–àËÅ9Ωπî∞(ÄÄÄÄÄÄÄÄÄÄÄÄâµïÕÕÖùîàËÅ9ΩπîÅ•òÅïπÖâ±ïêÅï±ÕîÄâπÖ±ÂÕîÅ%Å•πë•Õ¡Ωπ•â±îÄËÅ±ÑÅçΩπô•ù’…Ö—•Ω∏Åë‘ÅÕï…Ÿ•çîÅ%ÅïÕ–ÅµÖπ≈’Öπ—î∏âÙ(ÄÄÄÄåÅ0ùÖôô•ç°ÖùîÅêù’πîÅÖπÖ±ÂÕîÅï·•Õ—Öπ—îÅπîÅëΩ•–Å©ÖµÖ•ÃÅì•ç±ïπç°ï»Å’∏ÅÖ¡¡ï∞(ÄÄÄÄåÅŸï…ÃÅïÕ—•Ω∏ÅM—Öù•Ö•…ïÃ∏Å1ïÃÅëΩπª•ïÃÅÀ•ù±ïµïπ—Ö•…ïÃÅì•´ÄÅïπ…ïù•Õ—À•ïÃ(ÄÄÄÄåÅëÖπÃÅ±îÅI4ÅÕ’ôô•Õïπ–ÉÄÅì•—ï…µ•πï»ÅÕ§Å∞ùÖπÖ±ÂÕîÅ±ΩçÖ±îÅïÕ–Å√•…•∑•î∏(ÄÄÄÅÕ—Ö±îÄÙÄ°Õ—Ω…ïêπùï–†âÕΩ’…çï}°ÖÕ†à§ÄÑÙÅçΩµ¡’—ï}çÖπë•ëÖ—ï}Ö•}ÕΩ’…çï}°ÖÕ††(ÄÄÄÄÄÄÄÅâ’•±ë}çÖπë•ëÖ—ï}Ö•}çΩπ—ï·–°çΩπ—Öç—}•ê∞ÅëÖ—Ñ∞Åôï—ç°}±•Ÿï}ŸÖîıÖ±Õî§§(ÄÄÄÄÄÄÄÄÄÄÄÄÅΩ»ÅÕ—Ω…ïêπùï–†âÖπÖ±ÂÕ•Õ}Ÿï…Õ•Ω∏à§ÄÑÙÅ%}9%Q}91eM%M}YIM%=8(ÄÄÄÄÄÄÄÄÄÄÄÄÅΩ»ÅÕ—Ω…ïêπùï–†â¡…Ωµ¡—}Ÿï…Õ•Ω∏à§ÄÑÙÅ%}9%Q}AI=5AQ}YIM%=8§(ÄÄÄÅ…ï—’…∏ÅÏâïπÖâ±ïêàËÅïπÖâ±ïê∞ÄâÕ—Ö—’ÃàËÄâÕ—Ö±îàÅ•òÅÕ—Ö±îÅï±ÕîÄâô…ïÕ†à∞ÄâÕ—Ö±îàËÅÕ—Ö±î∞(ÄÄÄÄÄÄÄÄâùïπï…Ö—ïë}Ö–àËÅÕ—Ω…ïêπùï–†âùïπï…Ö—ïë}Ö–à§∞Äâùïπï…Ö—ïë}â‰àËÅÕ—Ω…ïêπùï–†âùïπï…Ö—ïë}âÂ}πÖµîà§∞(ÄÄÄÄÄÄÄÄâÖπÖ±ÂÕ•Õ}Ÿï…Õ•Ω∏àËÅÕ—Ω…ïêπùï–†âÖπÖ±ÂÕ•Õ}Ÿï…Õ•Ω∏à§∞Äâ¡…Ωµ¡—}Ÿï…Õ•Ω∏àËÅÕ—Ω…ïêπùï–†â¡…Ωµ¡—}Ÿï…Õ•Ω∏à§∞(ÄÄÄÄÄÄÄÄâ…ïÕ’±–àËÅÕ—Ω…ïêπùï–†â…ïÕ’±–à§∞ÄâµïÕÕÖùîàËÅÕ—Ω…ïêπùï–†â±ÖÕ—}ï……Ω»à•Ù(()ëïòÅ}ùïÕ—•Ωπ}Õ—Öù•Ö•…ïÕ}¡ÖÂ±ΩÖê°çΩπ—Öç–§Ë(ÄÄÄÄààâΩπ—…Ö–Å)M=8ÅïπŸΩÁ§ÉÄÅ∞ùÖ¡¡±•çÖ—•Ω∏ÅïÕ—•Ω∏ÅÕ—Öù•Ö•…ïÃ∏ààà(ÄÄÄÅô…Ω¥Åç…µ}çπÖ¡Õ}—…Öç≠•πúÅ•µ¡Ω…–Åç…µ}çΩπ—Öç—}•ëïπ—•—‰(ÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄâÕΩ’…çîàËÄâ•π—ïù…Ö±îµçΩππïç–µç…¥à∞Äâç…µ}çΩπ—Öç—}•êàËÅçΩπ—Öç—lâ•êât∞(ÄÄÄÄÄÄÄÄ®©ç…µ}çΩπ—Öç—}•ëïπ—•—‰°çΩπ—Öç–§∞(ÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏àËÅçΩπ—Öç–πùï–†âôΩ…µÖ—•Ω∏à∞Äàà§∞Äâ¡Ö…çΩ’…ÃàËÅçΩπ—Öç–πùï–†âëïÕ¡}—Â¡îà∞Äàà§∞(ÄÄÄÄÄÄÄÄâçïπ—…îàËÅçΩπ—Öç–πùï–†â±•ï‘à∞Äàà§∞ÄâÕïÕÕ•Ω∏àËÅçΩπ—Öç–πùï–†âëÖ—ïÕ}ôΩ…µÖ—•Ω∏à∞Äàà§∞(ÄÄÄÄÄÄÄÄâçΩµµïπ—Ö•…ïÃàËÅçΩπ—Öç–πùï–†âçΩµµïπ—Ö•…ïÃà∞Äàà§∞(ÄÄÄÅÙ(((åÄ¥¥¥¥¥¥¥¥¥¥¥¥¥¥¥Å]=Ä°çÖç°îÅ±ΩçÖ∞Åï∏Å±ïç—’…îÅÕï’±î§Ä¥¥¥¥¥¥¥¥¥¥¥¥¥¥¥)ç±ÖÕÃÅ]ïëΩôA%……Ω»°I’π—•µï……Ω»§Ë(ÄÄÄÄààâ……ï’»Å]=ÅëΩπ–Å±îÅµïÕÕÖùîÅ¡ï’–É©—…îÅ…ï—Ω’…ª§ÅÕÖπÃÅë•Ÿ’±ù’ï»Å±ÑÅç≥§∏ààà((ÄÄÄÅëïòÅ}}•π•—}|°Õï±ò∞ÅµïÕÕÖùî∞ÅÕ—Ö—’Õ}çΩëîı9Ωπî§Ë(ÄÄÄÄÄÄÄÅÕ’¡ï»†§π}}•π•—}|°µïÕÕÖùî§(ÄÄÄÄÄÄÄÅÕï±òπÕ—Ö—’Õ}çΩëîÄÙÅÕ—Ö—’Õ}çΩëî(()}]=}Me9}1=,ÄÙÅ—°…ïÖë•πúπ1Ωç¨†§)}]=}A=11I}MQIQÄÙÅÖ±Õî)}]=}A=11I}MQ=@ÄÙÅ—°…ïÖë•πúπŸïπ–†§)}]=}U9%9}!}1=,ÄÙÅ—°…ïÖë•πúπI1Ωç¨†§)}]=}U9%9}!}-dÄÙÅ9Ωπî)}]=}U9%9}!}Y1UÄÙÅ9Ωπî)}]=}A}MQQ}!}Y1UÄÙÅÌÙ)}]=}U9%9}!}PÄÙÄ¿∏¿)]=}=9QQ}=A9}IIM!}5%9}}M=9LÄÙÄÃ¿Ä®Äÿ¿)]=}=9QQ}IIM!}1=-}M=9LÄÙÄ»Ä®Äÿ¿)]=}=9QQ}IIM!}IQIe}M=9LÄÙÄ‘Ä®Äÿ¿(()ëïòÅ}›ïëΩô}ëâ}¡Ö—††§Ë(ÄÄÄÄààâA±ÖçîÅ±îÅçÖç°îÉÄÅè—”§ÅëîÅëÖ—Ñπ©ÕΩ∏∞ÅÕÖ’òÅÕ’…ç°Ö…ùîÅï·¡±•ç•—î∏ààà(ÄÄÄÅ…ï—’…∏ÅΩÃπùï—ïπÿ†â]=}	}AQ à§ÅΩ»ÅΩÃπ¡Ö—†π©Ω•∏†(ÄÄÄÄÄÄÄÅΩÃπ¡Ö—†πë•…πÖµî°Q}%1§ÅΩ»Äà∏à∞Äâ›ïëΩòπÕ≈±•—îÃà(ÄÄÄÄ§(()ëïòÅ}›ïëΩô}ëâ}Õ•ùπÖ—’…î†§Ë(ÄÄÄÄààâQ…Öç¨ÅâΩ—†ÅME1•—îÅÖπêÅ•—ÃÅ]0ÅâïçÖ’ÕîÅÕÂπç°…Ωπ•ÕÖ—•Ω∏Å…’πÃÅ•∏Å]0ÅµΩëî∏ààà(ÄÄÄÅ¡Ö—†ÄÙÅΩÃπ¡Ö—†πÖâÕ¡Ö—†°}›ïëΩô}ëâ}¡Ö—††§§(ÄÄÄÅÕ•ùπÖ—’…îÄÙÅm¡Ö—°t(ÄÄÄÅôΩ»ÅçÖπë•ëÖ—îÅ•∏Ä°¡Ö—†∞ÅòâÌ¡Ö—°Ùµ›Ö∞à§Ë(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö–ÄÙÅΩÃπÕ—Ö–°çÖπë•ëÖ—î§(ÄÄÄÄÄÄÄÄÄÄÄÅÕ•ùπÖ—’…îπÖ¡¡ïπê†°Õ—Ö–πÕ—}•πº∞ÅÕ—Ö–πÕ—}Õ•Èî∞ÅÕ—Ö–πÕ—}µ—•µï}πÃ§§(ÄÄÄÄÄÄÄÅï·çï¡–Å=M……Ω»Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÕ•ùπÖ—’…îπÖ¡¡ïπê°9Ωπî§(ÄÄÄÅ…ï—’…∏Å—’¡±î°Õ•ùπÖ—’…î§(()ëïòÅ}›ïëΩô}çΩππïç–†§Ë(ÄÄÄÅ¡Ö—†ÄÙÅ}›ïëΩô}ëâ}¡Ö—††§(ÄÄÄÅΩÃπµÖ≠ïë•…Ã°ΩÃπ¡Ö—†πë•…πÖµî°ΩÃπ¡Ö—†πÖâÕ¡Ö—†°¡Ö—†§§∞Åï·•Õ—}Ω¨ıQ…’î§(ÄÄÄÅçΩππïç—•Ω∏ÄÙÅÕ≈±•—îÃπçΩππïç–°¡Ö—†∞Å—•µïΩ’–Ùƒ¿§(ÄÄÄÅçΩππïç—•Ω∏π…Ω›}ôÖç—Ω…‰ÄÙÅÕ≈±•—îÃπIΩ‹(ÄÄÄÅçΩππïç—•Ω∏πï·ïç’—î†âAI5Åâ’ÕÂ}—•µïΩ’–ÄÙÄƒ¿¿¿¿à§(ÄÄÄÅçΩππïç—•Ω∏πï·ïç’—î†âAI5Å©Ω’…πÖ±}µΩëîÄÙÅ]0à§(ÄÄÄÅçΩππïç—•Ω∏πï·ïç’—î†âAI5ÅôΩ…ï•ùπ}≠ïÂÃÄÙÅ=8à§(ÄÄÄÅçΩππïç—•Ω∏πï·ïç’—ïÕç…•¡–†ààà(ÄÄÄÄÄÄÄÅIQÅQ	1Å%Å9=PÅa%MQLÅ›ïëΩô}…ïÕΩ’…çïÃÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕΩ’…çï}—Â¡îÅQaPÅ9=PÅ9U10∞(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Öâ±ï}•êÅQaPÅ9=PÅ9U10∞(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖë}©ÕΩ∏ÅQaPÅ9=PÅ9U10∞(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïµΩ—ï}ëÖ—îÅQaP∞(ÄÄÄÄÄÄÄÄÄÄÄÅÕÂπçïë}Ö–ÅQaPÅ9=PÅ9U10∞(ÄÄÄÄÄÄÄÄÄÄÄÅAI%5IdÅ-dÄ°…ïÕΩ’…çï}—Â¡î∞ÅÕ—Öâ±ï}•ê§(ÄÄÄÄÄÄÄÄ§Ï(ÄÄÄÄÄÄÄÅIQÅQ	1Å%Å9=PÅa%MQLÅ›ïëΩô}çΩπ—Öç—}±•π≠ÃÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—}•êÅQaPÅ9=PÅ9U10∞(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕΩ’…çï}—Â¡îÅQaPÅ9=PÅ9U10∞(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕΩ’…çï}•êÅQaPÅ9=PÅ9U10∞(ÄÄÄÄÄÄÄÄÄÄÄÅÖ——ïπëïï}•êÅQaP∞(ÄÄÄÄÄÄÄÄÄÄÄÅµÖ—ç°}µï—°ΩêÅQaPÅ9=PÅ9U10∞(ÄÄÄÄÄÄÄÄÄÄÄÅ±•π≠ïë}Ö–ÅQaPÅ9=PÅ9U10∞(ÄÄÄÄÄÄÄÄÄÄÄÅ’¡ëÖ—ïë}Ö–ÅQaPÅ9=PÅ9U10∞(ÄÄÄÄÄÄÄÄÄÄÄÅAI%5IdÅ-dÄ°…ïÕΩ’…çï}—Â¡î∞Å…ïÕΩ’…çï}•ê§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ=I%8Å-dÄ°…ïÕΩ’…çï}—Â¡î∞Å…ïÕΩ’…çï}•ê§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅII9LÅ›ïëΩô}…ïÕΩ’…çïÃ°…ïÕΩ’…çï}—Â¡î∞ÅÕ—Öâ±ï}•ê§Å=8Å1QÅM(ÄÄÄÄÄÄÄÄ§Ï(ÄÄÄÄÄÄÄÅIQÅ%9`Å%Å9=PÅa%MQLÅ•ë·}›ïëΩô}±•π≠Õ}çΩπ—Öç–(ÄÄÄÄÄÄÄÄÄÄÄÅ=8Å›ïëΩô}çΩπ—Öç—}±•π≠Ã°çΩπ—Öç—}•ê§Ï(ÄÄÄÄÄÄÄÅIQÅQ	1Å%Å9=PÅa%MQLÅ›ïëΩô}ÕÂπç}Õ—Ö—îÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅÕÂπç}≠ï‰ÅQaPÅAI%5IdÅ-d∞(ÄÄÄÄÄÄÄÄÄÄÄÅŸÖ±’ï}©ÕΩ∏ÅQaPÅ9=PÅ9U10∞(ÄÄÄÄÄÄÄÄÄÄÄÅ’¡ëÖ—ïë}Ö–ÅQaPÅ9=PÅ9U10(ÄÄÄÄÄÄÄÄ§Ï(ÄÄÄÄÄÄÄÅIQÅQ	1Å%Å9=PÅa%MQLÅ›ïëΩô}›ïâ°ΩΩ≠}ëï±•Ÿï…•ïÃÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅëï±•Ÿï…Â}•êÅQaPÅAI%5IdÅ-d∞(ÄÄÄÄÄÄÄÄÄÄÄÅ¡…ΩçïÕÕïë}Ö–ÅQaPÅ9=PÅ9U10(ÄÄÄÄÄÄÄÄ§Ï(ÄÄÄÄÄÄÄÅIQÅQ	1Å%Å9=PÅa%MQLÅ›ïëΩô}çΩπ—Öç—}…ïô…ïÕ°}Õ—Ö—îÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕΩ’…çï}—Â¡îÅQaPÅ9=PÅ9U10∞(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕΩ’…çï}•êÅQaPÅ9=PÅ9U10∞(ÄÄÄÄÄÄÄÄÄÄÄÅ±ÖÕ—}Ö——ïµ¡—}Ö–ÅI0Å9=PÅ9U10ÅU1PÄ¿∞(ÄÄÄÄÄÄÄÄÄÄÄÅ±ÖÕ—}Õ’ççïÕÕ}Ö–ÅI0Å9=PÅ9U10ÅU1PÄ¿∞(ÄÄÄÄÄÄÄÄÄÄÄÅ±ÖÕ—}ï……Ω»ÅQaPÅ9=PÅ9U10ÅU1PÄúú∞(ÄÄÄÄÄÄÄÄÄÄÄÅ±ïÖÕï}—Ω≠ï∏ÅQaPÅ9=PÅ9U10ÅU1PÄúú∞(ÄÄÄÄÄÄÄÄÄÄÄÅ±ïÖÕï}ï·¡•…ïÕ}Ö–ÅI0Å9=PÅ9U10ÅU1PÄ¿∞(ÄÄÄÄÄÄÄÄÄÄÄÅAI%5IdÅ-dÄ°…ïÕΩ’…çï}—Â¡î∞Å…ïÕΩ’…çï}•ê§(ÄÄÄÄÄÄÄÄ§Ï(ÄÄÄÄààà§(ÄÄÄÅçΩππïç—•Ω∏πçΩµµ•–†§(ÄÄÄÅ…ï—’…∏ÅçΩππïç—•Ω∏(()ëïòÅ}›ïëΩô}πΩ‹†§Ë(ÄÄÄÅ…ï—’…∏ÅëÖ—ï—•µîπëÖ—ï—•µîππΩ‹°ëÖ—ï—•µîπ—•µïÈΩπîπ’—å§π•ÕΩôΩ…µÖ–°—•µïÕ¡ïåÙâÕïçΩπëÃà§(()ëïòÅ}›ïëΩô}…ïÕΩ’…çï}Öùï}ÕïçΩπëÃ°…ïÕΩ’…çî§Ë(ÄÄÄÄààâIï—Ω’…πîÅ∞üâùîÅë‘ÅçÖç°îÅêù’∏ÅëΩÕÕ•ï»∞ÅΩ‘ÅÅÅ9ΩπïÅÄÅÕ§Å±ÑÅëÖ—îÅïÕ–Å•±±•Õ•â±î∏ààà(ÄÄÄÅŸÖ±’îÄÙÅÕ—»†°…ïÕΩ’…çîÅΩ»ÅÌÙ§πùï–†âÕÂπçïë}Ö–à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–ÅŸÖ±’îË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ¡Ö…ÕïêÄÙÅëÖ—ï—•µîπëÖ—ï—•µîπô…Ωµ•ÕΩôΩ…µÖ–°ŸÖ±’îπ…ï¡±Öçî†âhà∞Äà¨¿¿Ë¿¿à§§(ÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(ÄÄÄÅ•òÅ¡Ö…Õïêπ—È•πôºÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅ¡Ö…ÕïêÄÙÅ¡Ö…Õïêπ…ï¡±Öçî°—È•πôºıëÖ—ï—•µîπ—•µïÈΩπîπ’—å§(ÄÄÄÅ…ï—’…∏ÅµÖ‡†¿∏¿∞Å—•µîπ—•µî†§Ä¥Å¡Ö…Õïêπ—•µïÕ—Öµ¿†§§(()ëïòÅ}›ïëΩô}âïù•π}çΩπ—Öç—}…ïô…ïÕ†°…ïÕΩ’…çï}•ê∞Ä®∞ÅÖ’—ΩµÖ—•å§Ë(ÄÄÄÄààâA…ïπêÅ’∏ÅâÖ•∞ÅME1•—îÅ¡Ö…—Öü§Å¡Ö»Å—Ω’ÃÅ±ïÃÅ›Ω…≠ï…ÃÅI4∏((ÄÄÄÅ1îÅçΩµ¡—ï’»Åçïπ—…Ö∞Å¡…Ω”°ùîÅ±îÅ≈’Ω—ÑÅïπ—…îÅÖ¡¡±•çÖ—•ΩπÃ∏ÅîÅâÖ•∞Å¡±’ÃÅô•∏(ÄÄÄÉ•Ÿ•—îÅï∏ÅçΩµ¡≥•µïπ–Å≈’îÅëï’‡ÅΩπù±ï—ÃÅΩ‘Å›Ω…≠ï…ÃÅ…ï±•Õïπ–ÅÕ•µ’±—Öª•µïπ–Å±î(ÄÄÄÅ∑©µîÅëΩÕÕ•ï»∏Å¡À°ÃÅ’∏É•ç°ïåÅÖ’—ΩµÖ—•≈’î∞Å’∏ÅçΩ’…–Åì•±Ö§Åïµ√©ç°îÅ’πîÅ…ÖôÖ±î(ÄÄÄÅëîÅπΩ’Ÿï±±ïÃÅ—ïπ—Ö—•ŸïÃÉÄÅç°Ö≈’îÅΩ’Ÿï…—’…îÅëîÅô•ç°î∏(ÄÄÄÄààà(ÄÄÄÅπΩ‹ÄÙÅ—•µîπ—•µî†§(ÄÄÄÅ—Ω≠ï∏ÄÙÅ’’•êπ’’•ê–†§π°ï‡(ÄÄÄÅ›•—†Å}›ïëΩô}çΩππïç–†§ÅÖÃÅëàË(ÄÄÄÄÄÄÄÅëàπï·ïç’—î†â	%8Å%55%Qà§(ÄÄÄÄÄÄÄÅ…Ω‹ÄÙÅëàπï·ïç’—î†ààà(ÄÄÄÄÄÄÄÄÄÄÄÅM1PÅ±ÖÕ—}Ö——ïµ¡—}Ö–∞Å±ÖÕ—}ï……Ω»∞Å±ïÖÕï}ï·¡•…ïÕ}Ö–(ÄÄÄÄÄÄÄÄÄÄÄÅI=4Å›ïëΩô}çΩπ—Öç—}…ïô…ïÕ°}Õ—Ö—î(ÄÄÄÄÄÄÄÄÄÄÄÅ]!IÅ…ïÕΩ’…çï}—Â¡îÙù…ïù•Õ—…Ö—•ΩπΩ±ëï»úÅ9Å…ïÕΩ’…çï}•êÙ¸(ÄÄÄÄÄÄÄÄààà∞Ä°…ïÕΩ’…çï}•ê∞§§πôï—ç°Ωπî†§(ÄÄÄÄÄÄÄÅ•òÅ…Ω‹ÅÖπêÅô±ΩÖ–°…Ω›lâ±ïÖÕï}ï·¡•…ïÕ}Ö–âtÅΩ»Ä¿§Ä¯ÅπΩ‹Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏâÖç≈’•…ïêàËÅÖ±Õî∞Äâ…ïÖÕΩ∏àËÄâ…ïô…ïÕ°}•π}¡…Ωù…ïÕÃâÙ(ÄÄÄÄÄÄÄÅ•òÄ°Ö’—ΩµÖ—•åÅÖπêÅ…Ω‹ÅÖπêÅÕ—»°…Ω›lâ±ÖÕ—}ï……Ω»âtÅΩ»Äàà§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅπΩ‹Ä¥Åô±ΩÖ–°…Ω›lâ±ÖÕ—}Ö——ïµ¡—}Ö–âtÅΩ»Ä¿§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ]=}=9QQ}IIM!}IQIe}M=9L§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—…Â}Öô—ï»ÄÙÅµÖ‡†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄƒ∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•π–°]=}=9QQ}IIM!}IQIe}M=9L(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ¥Ä°πΩ‹Ä¥Åô±ΩÖ–°…Ω›lâ±ÖÕ—}Ö——ïµ¡—}Ö–âtÅΩ»Ä¿§§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÖç≈’•…ïêàËÅÖ±Õî∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ…ïÖÕΩ∏àËÄâ…ï—…Â}çΩΩ±ëΩ›∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ…ï—…Â}Öô—ï…}ÕïçΩπëÃàËÅ…ï—…Â}Öô—ï»∞(ÄÄÄÄÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÅëàπï·ïç’—î†ààà(ÄÄÄÄÄÄÄÄÄÄÄÅ%9MIPÅ%9Q<Å›ïëΩô}çΩπ—Öç—}…ïô…ïÕ°}Õ—Ö—î(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ°…ïÕΩ’…çï}—Â¡î∞Å…ïÕΩ’…çï}•ê∞Å±ÖÕ—}Ö——ïµ¡—}Ö–∞Å±ÖÕ—}ï……Ω»∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ±ïÖÕï}—Ω≠ï∏∞Å±ïÖÕï}ï·¡•…ïÕ}Ö–§(ÄÄÄÄÄÄÄÄÄÄÄÅY1ULÄ†ù…ïù•Õ—…Ö—•ΩπΩ±ëï»ú∞Ä¸∞Ä¸∞Äù…ïô…ïÕ°}•π}¡…Ωù…ïÕÃú∞Ä¸∞Ä¸§(ÄÄÄÄÄÄÄÄÄÄÄÅ=8Å=91%P°…ïÕΩ’…çï}—Â¡î∞Å…ïÕΩ’…çï}•ê§Å<ÅUAQÅMP(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ±ÖÕ—}Ö——ïµ¡—}Ö–ıï·ç±’ëïêπ±ÖÕ—}Ö——ïµ¡—}Ö–∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ±ÖÕ—}ï……Ω»ıï·ç±’ëïêπ±ÖÕ—}ï……Ω»∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ±ïÖÕï}—Ω≠ï∏ıï·ç±’ëïêπ±ïÖÕï}—Ω≠ï∏∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ±ïÖÕï}ï·¡•…ïÕ}Ö–ıï·ç±’ëïêπ±ïÖÕï}ï·¡•…ïÕ}Ö–(ÄÄÄÄÄÄÄÄààà∞Ä†(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕΩ’…çï}•ê∞ÅπΩ‹∞Å—Ω≠ï∏∞(ÄÄÄÄÄÄÄÄÄÄÄÅπΩ‹Ä¨Å]=}=9QQ}IIM!}1=-}M=9L∞(ÄÄÄÄÄÄÄÄ§§(ÄÄÄÅ…ï—’…∏ÅÏâÖç≈’•…ïêàËÅQ…’î∞Äâ—Ω≠ï∏àËÅ—Ω≠ïπÙ(()ëïòÅ}›ïëΩô}ô•π•Õ°}çΩπ—Öç—}…ïô…ïÕ†°…ïÕΩ’…çï}•ê∞Å—Ω≠ï∏∞Ä®∞Åï……Ω»Ùàà§Ë(ÄÄÄÄààâ1•ã°…îÅ±îÅâÖ•∞Åï–Å∑•µΩ…•ÕîÅ±îÅÕ’çè°ÃÅΩ‘Å∞ü•ç°ïåÅëîÅ±ÑÅ—ïπ—Ö—•Ÿî∏ààà(ÄÄÄÅç±ïÖπ}ï……Ω»ÄÙÅ}›ïëΩô}ç±ïÖ∏°ï……Ω»§Å•òÅï……Ω»Åï±ÕîÄàà(ÄÄÄÅπΩ‹ÄÙÅ—•µîπ—•µî†§(ÄÄÄÅ›•—†Å}›ïëΩô}çΩππïç–†§ÅÖÃÅëàË(ÄÄÄÄÄÄÄÅëàπï·ïç’—î†ààà(ÄÄÄÄÄÄÄÄÄÄÄÅUAQÅ›ïëΩô}çΩπ—Öç—}…ïô…ïÕ°}Õ—Ö—î(ÄÄÄÄÄÄÄÄÄÄÄÅMPÅ±ïÖÕï}—Ω≠ï∏Ùúú∞Å±ïÖÕï}ï·¡•…ïÕ}Ö–Ù¿∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ±ÖÕ—}Õ’ççïÕÕ}Ö–ıMÅ]!8Ä¸ÙúúÅQ!8Ä¸Å1MÅ±ÖÕ—}Õ’ççïÕÕ}Ö–Å9∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ±ÖÕ—}ï……Ω»Ù¸(ÄÄÄÄÄÄÄÄÄÄÄÅ]!IÅ…ïÕΩ’…çï}—Â¡îÙù…ïù•Õ—…Ö—•ΩπΩ±ëï»úÅ9Å…ïÕΩ’…çï}•êÙ¸(ÄÄÄÄÄÄÄÄÄÄÄÄÄÅ9Å±ïÖÕï}—Ω≠ï∏Ù¸(ÄÄÄÄÄÄÄÄààà∞Ä°ç±ïÖπ}ï……Ω»∞ÅπΩ‹∞Åç±ïÖπ}ï……Ω»∞Å…ïÕΩ’…çï}•ê∞Å—Ω≠ï∏§§(()ëïòÅ}›ïëΩô}çÖπçï±}çΩπ—Öç—}…ïô…ïÕ†°…ïÕΩ’…çï}•ê∞Å—Ω≠ï∏§Ë(ÄÄÄÄààâ1•ã°…îÅ’∏ÅâÖ•∞ÅëïŸïπ‘Å•π’—•±îÅÕÖπÃÅïπ…ïù•Õ—…ï»Å’∏ÅôÖ’‡ÅÕ’çè°Ã∏ààà(ÄÄÄÅ›•—†Å}›ïëΩô}çΩππïç–†§ÅÖÃÅëàË(ÄÄÄÄÄÄÄÅëàπï·ïç’—î†ààà(ÄÄÄÄÄÄÄÄÄÄÄÅUAQÅ›ïëΩô}çΩπ—Öç—}…ïô…ïÕ°}Õ—Ö—î(ÄÄÄÄÄÄÄÄÄÄÄÅMPÅ±ïÖÕï}—Ω≠ï∏Ùúú∞Å±ïÖÕï}ï·¡•…ïÕ}Ö–Ù¿∞Å±ÖÕ—}ï……Ω»Ùúú(ÄÄÄÄÄÄÄÄÄÄÄÅ]!IÅ…ïÕΩ’…çï}—Â¡îÙù…ïù•Õ—…Ö—•ΩπΩ±ëï»úÅ9Å…ïÕΩ’…çï}•êÙ¸(ÄÄÄÄÄÄÄÄÄÄÄÄÄÅ9Å±ïÖÕï}—Ω≠ï∏Ù¸(ÄÄÄÄÄÄÄÄààà∞Ä°…ïÕΩ’…çï}•ê∞Å—Ω≠ï∏§§(()ëïòÅ}›ïëΩô}ç±ïÖ∏°ŸÖ±’î§Ë(ÄÄÄÄààâIï—•…îÅ±ÑÅç≥§ÅëîÅ—Ω’–ÅµïÕÕÖùîÅ¡…ΩŸïπÖπ–Åë‘ÅÀ•ÕïÖ‘ÅΩ‘Åêù’πîÅï·çï¡—•Ω∏∏ààà(ÄÄÄÅ—ï·–ÄÙÅÕ—»°ŸÖ±’îÅΩ»Äàà§(ÄÄÄÅÕïç…ï–ÄÙÅΩÃπùï—ïπÿ†â]=}A%}-dà∞Äàà§(ÄÄÄÅ•òÅÕïç…ï–Ë(ÄÄÄÄÄÄÄÅ—ï·–ÄÙÅ—ï·–π…ï¡±Öçî°Õïç…ï–∞ÄâmIQtà§(ÄÄÄÅ—ï·–ÄÙÅ…îπÕ’à°»à†˝§§°‡µÖ¡§µ≠ïÂqÃ©lËıuqÃ®•myqÃ∞Ìt¨à∞Å»âp≈mIQtà∞Å—ï·–§(ÄÄÄÅ…ï—’…∏Å—ï·—lË‘¿¡t(()ëïòÅ}›ïëΩô}…ï≈’ïÕ—}Ω¡ï…Ö—•Ω∏°¡Ö—†§Ë(ÄÄÄÅπΩ…µÖ±•ÈïêÄÙÅ…îπÕ’à†(ÄÄÄÄÄÄÄÅ»à†Ω…ïù•Õ—…Ö—•ΩπΩ±ëï…Ãº•mxΩt¨à∞Å»âpƒÈ•êà∞ÅÕ—»°¡Ö—†ÅΩ»Äàà§∞(ÄÄÄÄ§(ÄÄÄÅ•òÅπΩ…µÖ±•Èïêπ…Õ—…•¿†àºà§πïπëÕ›•—††àΩ…ïù•Õ—…Ö—•ΩπΩ±ëï…Ãà§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Äâ±•Õ—}…ïù•Õ—…Ö—•Ωπ}ôΩ±ëï…Ãà(ÄÄÄÅ•òÄàΩ…ïù•Õ—…Ö—•ΩπΩ±ëï…ÃºÈ•êàÅ•∏ÅπΩ…µÖ±•ÈïêË(ÄÄÄÄÄÄÄÅ…ï—’…∏Äâùï—}…ïù•Õ—…Ö—•Ωπ}ôΩ±ëï»à(ÄÄÄÅ•òÅπΩ…µÖ±•Èïêπ…Õ—…•¿†àºà§πïπëÕ›•—††àΩΩ…ùÖπ•ÕµÃΩµîà§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Äâùï—}ç’……ïπ—}Ω…ùÖπ•Õ¥à(ÄÄÄÅ…ï—’…∏Äâùï—}›ïëΩô}…ïÕΩ’…çîà(()ëïòÅ}›ïëΩô}…ï≈’ïÕ–°¡Ö—†∞Ä®∞Å¡Ö…ÖµÃı9Ωπî∞ÅΩ¡ï…Ö—•Ω∏ı9Ωπî∞Å…ï—…Â}â’ëùï–ı9Ωπî§Ë(ÄÄÄÅ≠ï‰ÄÙÅΩÃπùï—ïπÿ†â]=}A%}-dà∞Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–Å≠ï‰Ë(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅ]ïëΩôA%……Ω»†â]=}A%}-dÅπΩ∏ÅçΩπô•ù’À•îà§(ÄÄÄÅâÖÕï}’…∞ÄÙÅΩÃπùï—ïπÿ†â]=}	M}UI0à∞Äâ°——¡ÃËºΩ››‹π›ïëΩòπô»à§π…Õ—…•¿†àºà§(ÄÄÄÅ—•µïΩ’–ÄÙÅô±ΩÖ–°ΩÃπùï—ïπÿ†â]=}Q%5=UPà∞Äàƒ‘à§§(ÄÄÄÅ…ï—…•ïÃÄÙÄ†(ÄÄÄÄÄÄÄÅµÖ‡†¿∞Åµ•∏°•π–°ΩÃπùï—ïπÿ†â]=}Q}IQI%Là∞Äà»à§§∞Ä‘§§(ÄÄÄÄÄÄÄÅ•òÅ…ï—…Â}â’ëùï–Å•ÃÅ9Ωπî(ÄÄÄÄÄÄÄÅï±ÕîÅµÖ‡†¿∞Åµ•∏°•π–°…ï—…Â}â’ëùï–§∞Ä‘§§(ÄÄÄÄ§(ÄÄÄÅ°ïÖëï…ÃÄÙÅÏ(ÄÄÄÄÄÄÄÄâ`µ¡§µ-ï‰àËÅ≠ï‰∞(ÄÄÄÄÄÄÄÄâççï¡–àËÄâÖ¡¡±•çÖ—•Ω∏Ω©ÕΩ∏à∞(ÄÄÄÄÄÄÄÄâUÕï»µùïπ–àËÄâ%π—ïù…Ö±ïçÖëïµ‰µI4º»¿»ÿ∏¿‡à∞(ÄÄÄÄÄÄÄÄâ`µ%π—ïù…Ö±îµ¡¡±•çÖ—•Ω∏àËÄâç…¥à∞(ÄÄÄÅÙ(ÄÄÄÅôΩ»ÅÖ——ïµ¡–Å•∏Å…Öπùî°…ï—…•ïÃÄ¨Äƒ§Ë(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕï…Ÿï}›ïëΩô}…ï≈’ïÕ–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ¡ï…Ö—•Ω∏ıΩ¡ï…Ö—•Ω∏ÅΩ»Å}›ïëΩô}…ï≈’ïÕ—}Ω¡ï…Ö—•Ω∏°¡Ö—†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµï—°ΩêÙâPà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö—†ı…îπÕ’à†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ»à†Ω…ïù•Õ—…Ö—•ΩπΩ±ëï…Ãº•mxΩt¨à∞Å»âpƒÈ•êà∞ÅÕ—»°¡Ö—†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅï·çï¡–Å]ïëΩôE’Ω—Ö·çïïëïêÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅ]ïëΩôA%……Ω»°Õ—»°ï·å§∞Ä–»‰§Åô…Ω¥Åï·å(ÄÄÄÄÄÄÄÄÄÄÄÅï·çï¡–Å]ïëΩôΩŸï…πΩ………Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅ]ïëΩôA%……Ω»°Õ—»°ï·å§∞Ä‘¿Ã§Åô…Ω¥Åï·å(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕîÄÙÅ…ï≈’ïÕ—Ãπùï–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâÌâÖÕï}’…±ÙΩÌ¡Ö—†π±Õ—…•¿†úºú•Ùà∞Å°ïÖëï…Ãı°ïÖëï…Ã∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö…ÖµÃı¡Ö…ÖµÃ∞Å—•µïΩ’–ı—•µïΩ’–∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ…ïÕ¡ΩπÕîπÕ—Ö—’Õ}çΩëîÅ•∏ÅÏ–¿‡∞Ä–»‰∞Ä‘¿¿∞Ä‘¿»∞Ä‘¿Ã∞Ä‘¿—ÙÅÖπêÅÖ——ïµ¡–ÄÅ…ï—…•ïÃË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—•µîπÕ±ïï¿°µ•∏†¿∏»‘Ä®Ä†»Ä®®ÅÖ——ïµ¡–§∞Äƒ∏¿§§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–Ä»¿¿ÄÙÅ…ïÕ¡ΩπÕîπÕ—Ö—’Õ}çΩëîÄÄÃ¿¿Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅ]ïëΩôA%……Ω»†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâ]=ÅÑÅÀ•¡Ωπë‘ÅÖŸïåÅ±îÅÕ—Ö—’–ÅÌ…ïÕ¡ΩπÕîπÕ—Ö—’Õ}çΩëïÙ∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕîπÕ—Ö—’Õ}çΩëî∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å…ïÕ¡ΩπÕîπ©ÕΩ∏†§∞Å…ïÕ¡ΩπÕîπ°ïÖëï…Ã(ÄÄÄÄÄÄÄÄÄÄÄÅï·çï¡–Ä°YÖ±’ï……Ω»∞Å©ÕΩ∏π)M=9ïçΩëï……Ω»§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅ]ïëΩôA%……Ω»†â]=ÅÑÅ…ï—Ω’…ª§Å’πîÅÀ•¡ΩπÕîÅ)M=8Å•πŸÖ±•ëî∏à§(ÄÄÄÄÄÄÄÅï·çï¡–Å]ïëΩôA%……Ω»Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•Õî(ÄÄÄÄÄÄÄÅï·çï¡–Å…ï≈’ïÕ—ÃπIï≈’ïÕ—·çï¡—•Ω∏ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅÖ——ïµ¡–ÄÅ…ï—…•ïÃË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—•µîπÕ±ïï¿°µ•∏†¿∏»‘Ä®Ä†»Ä®®ÅÖ——ïµ¡–§∞Äƒ∏¿§§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅ]ïëΩôA%……Ω»°òâΩππï·•Ω∏ÉÄÅ]=Å•µ¡ΩÕÕ•â±îÄËÅÌ}›ïëΩô}ç±ïÖ∏°ï·å•Ùà§(()ëïòÅ}›ïëΩô}•—ïµÃ°¡ÖÂ±ΩÖê§Ë(ÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°¡ÖÂ±ΩÖê∞Å±•Õ–§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å¡ÖÂ±ΩÖê(ÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°¡ÖÂ±ΩÖê∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÅôΩ»Å≠ï‰Å•∏Ä†â•—ïµÃà∞Äâ…ïÕΩ’…çïÃà∞ÄâëÖ—Ñà∞Äâ…ïù•Õ—…Ö—•ΩπΩ±ëï…Ãà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°¡ÖÂ±ΩÖêπùï–°≠ï‰§∞Å±•Õ–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å¡ÖÂ±ΩÖëm≠ïÂt(ÄÄÄÅ…Ö•ÕîÅ]ïëΩôA%……Ω»†âΩ…µÖ–ÅëîÅ±•Õ—îÅ]=Å•πÖ——ïπë‘∏à§(()ëïòÅ}›ïëΩô}Ö——ïπëïï}ŸÖ±’ïÃ°ôΩ±ëï»§Ë(ÄÄÄÅÖ——ïπëïîÄÙÅôΩ±ëï»πùï–†âÖ——ïπëïîà§Å•òÅ•Õ•πÕ—Öπçî°ôΩ±ëï»πùï–†âÖ——ïπëïîà§∞Åë•ç–§Åï±ÕîÅÌÙ(ÄÄÄÅïµÖ•±ÃÄÙÅÏ(ÄÄÄÄÄÄÄÅ}ç…µ}πΩ…µÖ±•Èï}ïµÖ•∞°Ö——ïπëïîπùï–°≠ï‰§§(ÄÄÄÄÄÄÄÅôΩ»Å≠ï‰Å•∏Ä†âïµÖ•∞à∞ÄâµÖ•∞à∞ÄâïµÖ•±ëë…ïÕÃà§(ÄÄÄÄÄÄÄÅ•òÅ}ç…µ}πΩ…µÖ±•Èï}ïµÖ•∞°Ö——ïπëïîπùï–°≠ï‰§§(ÄÄÄÅÙ(ÄÄÄÅ¡°ΩπïÃÄÙÅÏ(ÄÄÄÄÄÄÄÅ}ç…µ}πΩ…µÖ±•Èï}¡°Ωπî°Ö——ïπëïîπùï–°≠ï‰§§(ÄÄÄÄÄÄÄÅôΩ»Å≠ï‰Å•∏Ä†â¡°Ωπîà∞Äâ—ï±ï¡°Ωπîà∞ÄâµΩâ•±îà∞Äâ¡°Ωπï9’µâï»à§(ÄÄÄÄÄÄÄÅ•òÅ}ç…µ}πΩ…µÖ±•Èï}¡°Ωπî°Ö——ïπëïîπùï–°≠ï‰§§(ÄÄÄÅÙ(ÄÄÄÅÖ——ïπëïï}•êÄÙÅÖ——ïπëïîπùï–†âï·—ï…πÖ±%êà§ÅΩ»ÅÖ——ïπëïîπùï–†â•êà§(ÄÄÄÅ…ï—’…∏ÅïµÖ•±Ã∞Å¡°ΩπïÃ∞ÅÕ—»°Ö——ïπëïï}•êÅΩ»Äàà§(()ëïòÅ}›ïëΩô}πΩ…µÖ±•Èï}πÖµî°ŸÖ±’î§Ë(ÄÄÄÄààâIï—’…∏ÅÖ∏ÅÖççïπ–µ•πÕïπÕ•—•ŸîÅ•ëïπ—•—‰ÅŸÖ±’îÅôΩ»Å]=ÅµÖ—ç°•πú∏ààà(ÄÄÄÅëïçΩµ¡ΩÕïêÄÙÅ’π•çΩëïëÖ—ÑππΩ…µÖ±•Èî†â9-à∞ÅÕ—»°ŸÖ±’îÅΩ»Äàà§πÕ—…•¿†§§(ÄÄÄÅ›•—°Ω’—}Öççïπ—ÃÄÙÄààπ©Ω•∏†(ÄÄÄÄÄÄÄÅç°Ö…Öç—ï»ÅôΩ»Åç°Ö…Öç—ï»Å•∏ÅëïçΩµ¡ΩÕïê(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å’π•çΩëïëÖ—ÑπçΩµâ•π•πú°ç°Ö…Öç—ï»§(ÄÄÄÄ§(ÄÄÄÅ…ï—’…∏Å…îπÕ’à°»âmyq›t¨à∞ÄàÄà∞Å›•—°Ω’—}Öççïπ—Ã∞Åô±ÖùÃı…îπU9%=§πÕ—…•¿†§πçÖÕïôΩ±ê†§(()ëïòÅ}›ïëΩô}Ö——ïπëïï}πÖµî°ôΩ±ëï»§Ë(ÄÄÄÅÖ——ïπëïîÄÙÅôΩ±ëï»πùï–†âÖ——ïπëïîà§Å•òÅ•Õ•πÕ—Öπçî°ôΩ±ëï»πùï–†âÖ——ïπëïîà§∞Åë•ç–§Åï±ÕîÅÌÙ(ÄÄÄÅô•…Õ—}πÖµîÄÙÅπï·–†°Ö——ïπëïîπùï–°≠ï‰§ÅôΩ»Å≠ï‰Å•∏Ä†(ÄÄÄÄÄÄÄÄâô•…Õ—9Öµîà∞Äâô•…Õ—πÖµîà∞Äâô•…Õ—}πÖµîà∞Äâù•Ÿïπ9Öµîà(ÄÄÄÄ§Å•òÅÖ——ïπëïîπùï–°≠ï‰§§∞Äàà§(ÄÄÄÅ±ÖÕ—}πÖµîÄÙÅπï·–†°Ö——ïπëïîπùï–°≠ï‰§ÅôΩ»Å≠ï‰Å•∏Ä†(ÄÄÄÄÄÄÄÄâ±ÖÕ—9Öµîà∞Äâ±ÖÕ—πÖµîà∞Äâ±ÖÕ—}πÖµîà∞ÄâôÖµ•±Â9Öµîà(ÄÄÄÄ§Å•òÅÖ——ïπëïîπùï–°≠ï‰§§∞Äàà§(ÄÄÄÅ…ï—’…∏Å}›ïëΩô}πΩ…µÖ±•Èï}πÖµî°ô•…Õ—}πÖµî§∞Å}›ïëΩô}πΩ…µÖ±•Èï}πÖµî°±ÖÕ—}πÖµî§(()}I5}]=}=I5Q%=9LÄÙÅÏâALà∞ÄâÕ@à∞ÄâM@à∞ÄâMM%@Äƒà∞Äâ°Ö’ôôï’»ÅYQâÙ)}I5}]=}9QILÄÙÅÏ(ÄÄÄÄâçΩ—ï}ÖÈ’»àËÄâ——îÅìäeÈ’»à∞(ÄÄÄÄâÖ’Ÿï…ùπîàËÄâ’Ÿï…ùπîà∞(ÄÄÄÄâ¡Ö…•ÃàËÄâAÖ…•Ãà∞)Ù)}I9!}5=9Q!}95LÄÙÄ†(ÄÄÄÄâ©ÖπŸ•ï»à∞Äâõ•Ÿ…•ï»à∞ÄâµÖ…Ãà∞ÄâÖŸ…•∞à∞ÄâµÖ§à∞Äâ©’•∏à∞(ÄÄÄÄâ©’•±±ï–à∞ÄâÖøÌ–à∞ÄâÕï¡—ïµâ…îà∞ÄâΩç—Ωâ…îà∞ÄâπΩŸïµâ…îà∞Äâì•çïµâ…îà∞(§(()ëïòÅ}›ïëΩô}ŸÖ±’î°¡ÖÂ±ΩÖê∞Ä©¡Ö—°Ã§Ë(ÄÄÄÄààâIï—’…∏Å—°îÅô•…Õ–ÅπΩ∏µïµ¡—‰ÅŸÖ±’îÅô…Ω¥ÅëΩ——ïêÅ]=Å¡ÖÂ±ΩÖêÅ¡Ö—°Ã∏ààà(ÄÄÄÅôΩ»Å¡Ö—†Å•∏Å¡Ö—°ÃË(ÄÄÄÄÄÄÄÅŸÖ±’îÄÙÅ¡ÖÂ±ΩÖê(ÄÄÄÄÄÄÄÅôΩ»Å≠ï‰Å•∏Å¡Ö—†πÕ¡±•–†à∏à§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°ŸÖ±’î∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅŸÖ±’îÄÙÅ9Ωπî(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅâ…ïÖ¨(ÄÄÄÄÄÄÄÄÄÄÄÅŸÖ±’îÄÙÅŸÖ±’îπùï–°≠ï‰§(ÄÄÄÄÄÄÄÅ•òÅŸÖ±’îÅπΩ–Å•∏Ä°9Ωπî∞Äàà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅŸÖ±’î(ÄÄÄÅ…ï—’…∏Äàà(()ëïòÅ}›ïëΩô}±ΩçÖ—•Ωπ}—ï·–°ŸÖ±’î§Ë(ÄÄÄÄààâ±Ö——ï∏Å—°îÅ’Õ’Ö∞Å]=ÅÖëë…ïÕÃÅŸÖ…•Öπ—ÃÅ•π—ºÅÕïÖ…ç°Öâ±îÅ—ï·–∏ààà(ÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°ŸÖ±’î∞Ä°±•Õ–∞Å—’¡±î§§Ë(ÄÄÄÄÄÄÄÅ¡Ö…—ÃÄÙÅm}›ïëΩô}±ΩçÖ—•Ωπ}—ï·–°•—ï¥§ÅôΩ»Å•—ï¥Å•∏ÅŸÖ±’ït(ÄÄÄÄÄÄÄÅ…ï—’…∏Äà∞Äàπ©Ω•∏°¡Ö…–ÅôΩ»Å¡Ö…–Å•∏Å¡Ö…—ÃÅ•òÅ¡Ö…–§(ÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°ŸÖ±’î∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÕ—»°ŸÖ±’îÅΩ»Äàà§πÕ—…•¿†§((ÄÄÄÅ¡…ïôï……ïë}≠ïÂÃÄÙÄ†(ÄÄÄÄÄÄÄÄâπÖµîà∞Äâ±Öâï∞à∞Äâç•—‰à∞Äâ±ΩçÖ±•—‰à∞ÄâÖëë…ïÕÕ1ΩçÖ±•—‰à∞Äâ—Ω›∏à∞(ÄÄÄÄÄÄÄÄâ¡ΩÕ—Ö±Ωëîà∞ÄâÈ•¡Ωëîà∞Äâ¡ΩÕ—çΩëîà∞ÄâÕ—…ïï—ëë…ïÕÃà∞ÄâÖëë…ïÕÃà∞(ÄÄÄÄÄÄÄÄâô’±±ëë…ïÕÃà∞(ÄÄÄÄ§(ÄÄÄÅ¡Ö…—ÃÄÙÅmt(ÄÄÄÅôΩ»Å≠ï‰Å•∏Å¡…ïôï……ïë}≠ïÂÃË(ÄÄÄÄÄÄÄÅ¡Ö…–ÄÙÅ}›ïëΩô}±ΩçÖ—•Ωπ}—ï·–°ŸÖ±’îπùï–°≠ï‰§§(ÄÄÄÄÄÄÄÅ•òÅ¡Ö…–ÅÖπêÅ¡Ö…–ÅπΩ–Å•∏Å¡Ö…—ÃË(ÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö…—ÃπÖ¡¡ïπê°¡Ö…–§(ÄÄÄÅ…ï—’…∏Äà∞Äàπ©Ω•∏°¡Ö…—Ã§(()ëïòÅ}›ïëΩô}ç…µ}—…Ö•π•πú°—•—±î§Ë(ÄÄÄÄààâQ…ÖπÕ±Ö—îÅÑÅçΩµµï…ç•Ö∞ÅAÅ—•—±îÅ—ºÅ—°îÅô•π•—îÅI4ÅôΩ…µÖ—•Ω∏ÅŸÖ±’ïÃ∏ààà(ÄÄÄÅΩ…•ù•πÖ∞ÄÙÅÕ—»°—•—±îÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅπΩ…µÖ±•ÈïêÄÙÅ}›ïëΩô}πΩ…µÖ±•Èï}πÖµî°Ω…•ù•πÖ∞§(ÄÄÄÅ•òÄ°…îπÕïÖ…ç†°»âqâëïÕ¡qàà∞ÅπΩ…µÖ±•Èïê§(ÄÄÄÄÄÄÄÄÄÄÄÅΩ»Äâë•…•ùïÖπ–ÅêÅïπ—…ï¡…•ÕîÅëîÅÕïç’…•—îàÅ•∏ÅπΩ…µÖ±•Èïê(ÄÄÄÄÄÄÄÄÄÄÄÅΩ»Äâç≈¿Åë•…•ùïÖπ–àÅ•∏ÅπΩ…µÖ±•Èïê§Ë(ÄÄÄÄÄÄÄÅ•Õ}ŸÖîÄÙÄâŸÖîàÅ•∏ÅπΩ…µÖ±•ÈïêÅΩ»ÄâŸÖ±•ëÖ—•Ω∏ÅëïÃÅÖç≈’•ÃàÅ•∏ÅπΩ…µÖ±•Èïê(ÄÄÄÄÄÄÄÅ…ï—’…∏ÄâM@à∞ÄâYàÅ•òÅ•Õ}ŸÖîÅï±ÕîÄâ%9%Q%0à(ÄÄÄÅ•òÄ°…îπÕïÖ…ç†°»âqâÑÕ¡qàà∞ÅπΩ…µÖ±•Èïê§(ÄÄÄÄÄÄÄÄÄÄÄÅΩ»ÄâÖùïπ–ÅëîÅ¡…Ω—ïç—•Ω∏Å¡°ÂÕ•≈’îÅëïÃÅ¡ï…ÕΩππïÃàÅ•∏ÅπΩ…µÖ±•Èïê§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÄâÕ@à∞Äàà(ÄÄÄÅ•òÄ°…îπÕïÖ…ç†°»âqâÕÕ•Ö¡qÃ®≈qàà∞ÅπΩ…µÖ±•Èïê§(ÄÄÄÄÄÄÄÄÄÄÄÅΩ»ÄâÕï…Ÿ•çîÅëîÅÕïç’…•—îÅ•πçïπë•îÅï–ÅêÅÖÕÕ•Õ—ÖπçîÅÑÅ¡ï…ÕΩππïÃÄƒàÅ•∏ÅπΩ…µÖ±•Èïê§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÄâMM%@Äƒà∞Äàà(ÄÄÄÅ•òÅ…îπÕïÖ…ç†°»âqâŸ—çqàà∞ÅπΩ…µÖ±•Èïê§ÅΩ»Äâç°Ö’ôôï’»ÅŸ—åàÅ•∏ÅπΩ…µÖ±•ÈïêË(ÄÄÄÄÄÄÄÅ…ï—’…∏Äâ°Ö’ôôï’»ÅYQà∞Äàà(ÄÄÄÅ•òÄ°…îπÕïÖ…ç†°»âqâÖ¡Õqàà∞ÅπΩ…µÖ±•Èïê§(ÄÄÄÄÄÄÄÄÄÄÄÅΩ»ÄâÖùïπ–ÅëîÅ¡…ïŸïπ—•Ω∏Åï–ÅëîÅÕïç’…•—îàÅ•∏ÅπΩ…µÖ±•Èïê§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÄâALà∞Äàà(ÄÄÄÅ…ï—’…∏ÅΩ…•ù•πÖ∞∞Äàà(()ëïòÅ}›ïëΩô}ëÖ—î°ŸÖ±’î§Ë(ÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°ŸÖ±’î∞ÅëÖ—ï—•µîπëÖ—ï—•µî§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅŸÖ±’îπëÖ—î†§(ÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°ŸÖ±’î∞ÅëÖ—ï—•µîπëÖ—î§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅŸÖ±’î(ÄÄÄÅ—ï·–ÄÙÅÕ—»°ŸÖ±’îÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–Å—ï·–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅëÖ—ï—•µîπëÖ—îπô…Ωµ•ÕΩôΩ…µÖ–°—ï·—lËƒ¡t§(ÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»Ë(ÄÄÄÄÄÄÄÅ¡ÖÕÃ(ÄÄÄÅôΩ»ÅëÖ—ï}ôΩ…µÖ–Å•∏Ä†àïêºï¥ºïdà∞Äàïê¥ï¥¥ïdà§Ë(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅëÖ—ï—•µîπëÖ—ï—•µîπÕ—…¡—•µî°—ï·—lËƒ¡t∞ÅëÖ—ï}ôΩ…µÖ–§πëÖ—î†§(ÄÄÄÄÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÅ…ï—’…∏Å9Ωπî(()ëïòÅ}›ïëΩô}ëÖ—ï}…Öπùï}±Öâï∞°Õ—Ö…–∞Åïπê§Ë(ÄÄÄÅ•òÅπΩ–ÅÕ—Ö…–ÅÖπêÅπΩ–ÅïπêË(ÄÄÄÄÄÄÄÅ…ï—’…∏Äàà(ÄÄÄÅ•òÅπΩ–ÅÕ—Ö…–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Åòâ)’Õ≈◊äeÖ‘ÅÌïπêπëÖÂÙÅÌ}I9!}5=9Q!}95MmïπêπµΩπ—†Ä¥Ä≈uÙÅÌïπêπÂïÖ…Ùà(ÄÄÄÅ•òÅπΩ–ÅïπêË(ÄÄÄÄÄÄÄÅ…ï—’…∏Åòã Å¡Ö…—•»Åë‘ÅÌÕ—Ö…–πëÖÂÙÅÌ}I9!}5=9Q!}95MmÕ—Ö…–πµΩπ—†Ä¥Ä≈uÙÅÌÕ—Ö…–πÂïÖ…Ùà(ÄÄÄÅÕ—Ö…—}µΩπ—†ÄÙÅ}I9!}5=9Q!}95MmÕ—Ö…–πµΩπ—†Ä¥Ä≈t(ÄÄÄÅïπë}µΩπ—†ÄÙÅ}I9!}5=9Q!}95MmïπêπµΩπ—†Ä¥Ä≈t(ÄÄÄÅ•òÅÕ—Ö…–πÂïÖ»ÄÙÙÅïπêπÂïÖ»ÅÖπêÅÕ—Ö…–πµΩπ—†ÄÙÙÅïπêπµΩπ—†Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Åòâ‘ÅÌÕ—Ö…–πëÖÂÙÅÖ‘ÅÌïπêπëÖÂÙÅÌïπë}µΩπ—°ÙÅÌïπêπÂïÖ…Ùà(ÄÄÄÅ•òÅÕ—Ö…–πÂïÖ»ÄÙÙÅïπêπÂïÖ»Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Åòâ‘ÅÌÕ—Ö…–πëÖÂÙÅÌÕ—Ö…—}µΩπ—°ÙÅÖ‘ÅÌïπêπëÖÂÙÅÌïπë}µΩπ—°ÙÅÌïπêπÂïÖ…Ùà(ÄÄÄÅ…ï—’…∏Ä†(ÄÄÄÄÄÄÄÅòâ‘ÅÌÕ—Ö…–πëÖÂÙÅÌÕ—Ö…—}µΩπ—°ÙÅÌÕ—Ö…–πÂïÖ…ÙÄà(ÄÄÄÄÄÄÄÅòâÖ‘ÅÌïπêπëÖÂÙÅÌïπë}µΩπ—°ÙÅÌïπêπÂïÖ…Ùà(ÄÄÄÄ§(()ëïòÅ}›ïëΩô}ç…µ}çïπ—…ï}çΩëî°±ΩçÖ—•Ω∏§Ë(ÄÄÄÅπΩ…µÖ±•ÈïêÄÙÅ}›ïëΩô}πΩ…µÖ±•Èï}πÖµî°}›ïëΩô}±ΩçÖ—•Ωπ}—ï·–°±ΩçÖ—•Ω∏§§(ÄÄÄÅ•òÅÖπ‰°µÖ…≠ï»Å•∏ÅπΩ…µÖ±•ÈïêÅôΩ»ÅµÖ…≠ï»Å•∏Ä†(ÄÄÄÄÄÄÄÄâçΩ—îÅêÅÖÈ’»à∞Äâ¡’ùï–ÅÕ’»ÅÖ…ùïπÃà∞Äâô…ï©’Ãà∞Äà‡Ã–‡¿à∞ÄàÅŸÖ»à∞(ÄÄÄÄ§§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÄâçΩ—ï}ÖÈ’»à(ÄÄÄÅ•òÅÖπ‰°µÖ…≠ï»Å•∏ÅπΩ…µÖ±•ÈïêÅôΩ»ÅµÖ…≠ï»Å•∏Ä†(ÄÄÄÄÄÄÄÄâÖ’Ÿï…ùπîà∞ÄâÖ’…•±±Öåà∞ÄâÖ…¡Ö©Ω∏ÅÕ’»Åçï…îà∞ÄâçÖπ—Ö∞à∞(ÄÄÄÄ§§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÄâÖ’Ÿï…ùπîà(ÄÄÄÅ•òÄâ¡Ö…•ÃàÅ•∏ÅπΩ…µÖ±•ÈïêÅΩ»Äâ•±îÅëîÅô…ÖπçîàÅ•∏ÅπΩ…µÖ±•ÈïêË(ÄÄÄÄÄÄÄÅ…ï—’…∏Äâ¡Ö…•Ãà(ÄÄÄÅ…ï—’…∏Äàà(()ëïòÅ}›ïëΩô}ÕïÕÕ•Ωπ}çΩëî°ôΩ…µÖ—•Ω∏∞ÅëïÕ¡}—Â¡î§Ë(ÄÄÄÅ•òÅôΩ…µÖ—•Ω∏ÄÙÙÄâM@àË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÄâMA}YàÅ•òÅëïÕ¡}—Â¡îÄÙÙÄâYàÅï±ÕîÄâMA}%9%Pà(ÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄâALàËÄâALà∞ÄâÕ@àËÄâÕ@à∞ÄâMM%@ÄƒàËÄâMM%@à∞(ÄÄÄÄÄÄÄÄâ°Ö’ôôï’»ÅYQàËÄâYQà∞(ÄÄÄÅÙπùï–°ôΩ…µÖ—•Ω∏∞Äàà§(()ëïòÅ}›ïëΩô}ç…µ}ÕïÕÕ•Ω∏°ëÖ—Ñ∞ÅôΩ…µÖ—•Ω∏∞ÅëïÕ¡}—Â¡î∞Å…Ö›}±ΩçÖ—•Ω∏∞ÅÕ—Ö…–∞Åïπê§Ë(ÄÄÄÄààâIïÕΩ±ŸîÅ]=ÅëÖ—ïÃΩÖëë…ïÕÃÅ—ºÅ—°îÅï·Öç–ÅÕï±ïç—Öâ±îÅI4ÅÕïÕÕ•Ω∏∏ààà(ÄÄÄÅçïπ—…ï}çΩëîÄÙÅ}›ïëΩô}ç…µ}çïπ—…ï}çΩëî°…Ö›}±ΩçÖ—•Ω∏§(ÄÄÄÅÕïÕÕ•Ωπ}çΩëîÄÙÅ}›ïëΩô}ÕïÕÕ•Ωπ}çΩëî°ôΩ…µÖ—•Ω∏∞ÅëïÕ¡}—Â¡î§(ÄÄÄÅçÖπë•ëÖ—ïÃÄÙÅmt(ÄÄÄÅ•òÅÕïÕÕ•Ωπ}çΩëîÅÖπêÄ°Õ—Ö…–ÅΩ»Åïπê§Ë(ÄÄÄÄÄÄÄÅôΩ»ÅçÖπë•ëÖ—ï}çïπ—…î∞ÅôΩ…µÖ—•ΩπÃÅ•∏Åùï—}ôΩ…µÖ—•Ωπ}ÕïÕÕ•ΩπÃ°ëÖ—Ñ§π•—ïµÃ†§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»Å…Ω‹Å•∏ÅôΩ…µÖ—•ΩπÃπùï–°ÕïÕÕ•Ωπ}çΩëî∞Åmt§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ±Öâï∞ÄÙÅÕ—»°…Ω‹πùï–†â±Öâï∞à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…Ω›}Õ—Ö…–∞Å…Ω›}ïπêÄÙÅ}ÕïÕÕ•Ωπ}ëÖ—ï}…Öπùî°±Öâï∞§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–Å±Öâï∞Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅÕ—Ö…–ÅÖπêÅ…Ω›}Õ—Ö…–ÄÑÙÅÕ—Ö…–Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅïπêÅÖπêÅ…Ω›}ïπêÄÑÙÅïπêË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçÖπë•ëÖ—ïÃπÖ¡¡ïπê†°çÖπë•ëÖ—ï}çïπ—…î∞Å±Öâï∞§§((ÄÄÄÄåÅQ°îÅÖëë…ïÕÃÅ›•πÃÅ›°ï∏Å—°îÅÕÖµîÅëÖ—ïÃÅï·•Õ–ÅÖ–ÅÕïŸï…Ö∞ÅçÖµ¡’ÕïÃ∏(ÄÄÄÅ•òÅçïπ—…ï}çΩëîË(ÄÄÄÄÄÄÄÅÕÖµï}çïπ—…îÄÙÅmçÖπë•ëÖ—îÅôΩ»ÅçÖπë•ëÖ—îÅ•∏ÅçÖπë•ëÖ—ïÃÅ•òÅçÖπë•ëÖ—ïl¡tÄÙÙÅçïπ—…ï}çΩëït(ÄÄÄÄÄÄÄÅçÖπë•ëÖ—ïÃÄÙÅÕÖµï}çïπ—…îÅΩ»Åmt(ÄÄÄÅ’π•≈’ï}çÖπë•ëÖ—ïÃÄÙÅ±•Õ–°ë•ç–πô…Ωµ≠ïÂÃ°çÖπë•ëÖ—ïÃ§§(ÄÄÄÅ•òÅ±ï∏°’π•≈’ï}çÖπë•ëÖ—ïÃ§ÄÙÙÄƒË(ÄÄÄÄÄÄÄÅçïπ—…ï}çΩëî∞ÅÕïÕÕ•Ωπ}±Öâï∞ÄÙÅ’π•≈’ï}çÖπë•ëÖ—ïÕl¡t(ÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÅÕïÕÕ•Ωπ}±Öâï∞ÄÙÅ}›ïëΩô}ëÖ—ï}…Öπùï}±Öâï∞°Õ—Ö…–∞Åïπê§((ÄÄÄÅ±ΩçÖ—•Ω∏ÄÙÅ}I5}]=}9QILπùï–†(ÄÄÄÄÄÄÄÅçïπ—…ï}çΩëî∞Å}›ïëΩô}±ΩçÖ—•Ωπ}—ï·–°…Ö›}±ΩçÖ—•Ω∏§(ÄÄÄÄ§(ÄÄÄÅ…ï—’…∏Å±ΩçÖ—•Ω∏∞ÅÕïÕÕ•Ωπ}±Öâï∞(()ëïòÅ}›ïëΩô}çΩπ—Öç—}¡ÖÂ±ΩÖê°ôΩ±ëï»∞ÅëÖ—Ñı9Ωπî§Ë(ÄÄÄÄààâ·—…Ö•–Å±ïÃÅ•πôΩ…µÖ—•ΩπÃÅ’—•±ïÃÉÄÅ’πîÅ¡•Õ—îÅÕÖπÃÅÖ±”•…ï»Å±îÅ)M=8Å]=∏ààà(ÄÄÄÅÖ——ïπëïîÄÙÅ}›ïëΩô}ŸÖ±’î°ôΩ±ëï»∞ÄâÖ——ïπëïîà∞Äâ±ïÖ…πï»à∞Äâ—…Ö•πïîà§(ÄÄÄÅÖ——ïπëïîÄÙÅÖ——ïπëïîÅ•òÅ•Õ•πÕ—Öπçî°Ö——ïπëïî∞Åë•ç–§Åï±ÕîÅÌÙ(ÄÄÄÅô•…Õ—}πÖµîÄÙÅπï·–†°Ö——ïπëïîπùï–°≠ï‰§ÅôΩ»Å≠ï‰Å•∏Ä†(ÄÄÄÄÄÄÄÄâô•…Õ—9Öµîà∞Äâô•…Õ—πÖµîà∞Äâô•…Õ—}πÖµîà∞Äâù•Ÿïπ9Öµîà(ÄÄÄÄ§Å•òÅÖ——ïπëïîπùï–°≠ï‰§§∞Äàà§(ÄÄÄÅ±ÖÕ—}πÖµîÄÙÅπï·–†°Ö——ïπëïîπùï–°≠ï‰§ÅôΩ»Å≠ï‰Å•∏Ä†(ÄÄÄÄÄÄÄÄâ±ÖÕ—9Öµîà∞Äâ±ÖÕ—πÖµîà∞Äâ±ÖÕ—}πÖµîà∞ÄâôÖµ•±Â9Öµîà(ÄÄÄÄ§Å•òÅÖ——ïπëïîπùï–°≠ï‰§§∞Äàà§(ÄÄÄÅïµÖ•∞ÄÙÅπï·–†°Ö——ïπëïîπùï–°≠ï‰§ÅôΩ»Å≠ï‰Å•∏Ä†(ÄÄÄÄÄÄÄÄâïµÖ•∞à∞ÄâµÖ•∞à∞ÄâïµÖ•±ëë…ïÕÃà(ÄÄÄÄ§Å•òÅÖ——ïπëïîπùï–°≠ï‰§§∞Äàà§(ÄÄÄÅ¡°ΩπîÄÙÅπï·–†°Ö——ïπëïîπùï–°≠ï‰§ÅôΩ»Å≠ï‰Å•∏Ä†(ÄÄÄÄÄÄÄÄâ¡°Ωπï9’µâï»à∞Äâ¡°Ωπîà∞Äâ—ï±ï¡°Ωπîà∞ÄâµΩâ•±îà(ÄÄÄÄ§Å•òÅÖ——ïπëïîπùï–°≠ï‰§§∞Äàà§(ÄÄÄÅ…Ö›}ôΩ…µÖ—•Ω∏ÄÙÅ}›ïëΩô}ŸÖ±’î†(ÄÄÄÄÄÄÄÅôΩ±ëï»∞Äâ—…Ö•π•πùç—•Ωπ%πôºπ—•—±îà∞Äâ—…Ö•π•πúπ—•—±îà∞(ÄÄÄÄÄÄÄÄâ—…Ö•π•πùç—•Ω∏π—•—±îà∞Äâ—…Ö•π•πùQ•—±îà∞Äâ—•—±îà∞(ÄÄÄÄ§(ÄÄÄÅôΩ…µÖ—•Ω∏∞ÅëïÕ¡}—Â¡îÄÙÅ}›ïëΩô}ç…µ}—…Ö•π•πú°…Ö›}ôΩ…µÖ—•Ω∏§(ÄÄÄÅ…Ö›}±ΩçÖ—•Ω∏ÄÙÅ}›ïëΩô}ŸÖ±’î†(ÄÄÄÄÄÄÄÅôΩ±ëï»∞Äâ—…Ö•π•πùç—•Ωπ%πôºπÖëë…ïÕÃà∞Äâ—…Ö•π•πùç—•Ωπ%πôºπ±ΩçÖ—•Ω∏à∞(ÄÄÄÄÄÄÄÄâ—…Ö•π•πúπÖëë…ïÕÃà∞Äâ—…Ö•π•πúπ±ΩçÖ—•Ω∏à∞Äâ±ΩçÖ—•Ω∏ππÖµîà∞Äâ±ΩçÖ—•Ω∏à∞(ÄÄÄÄÄÄÄÄâÕïÕÕ•Ω∏π±ΩçÖ—•Ω∏à∞ÄâÕïÕÕ•Ω∏πÖëë…ïÕÃà∞(ÄÄÄÄ§(ÄÄÄÅÕ—Ö…–ÄÙÅ}›ïëΩô}ëÖ—î°}›ïëΩô}ŸÖ±’î†(ÄÄÄÄÄÄÄÅôΩ±ëï»∞Äâ—…Ö•π•πùç—•Ωπ%πôºπÕïÕÕ•ΩπM—Ö…—Ö—îà∞ÄâÕïÕÕ•Ω∏πÕ—Ö…—Ö—îà∞(ÄÄÄÄÄÄÄÄâÕïÕÕ•Ω∏πÕ—Ö…–à∞ÄâÕ—Ö…—Ö—îà∞(ÄÄÄÄ§§(ÄÄÄÅïπêÄÙÅ}›ïëΩô}ëÖ—î°}›ïëΩô}ŸÖ±’î†(ÄÄÄÄÄÄÄÅôΩ±ëï»∞Äâ—…Ö•π•πùç—•Ωπ%πôºπÕïÕÕ•ΩππëÖ—îà∞ÄâÕïÕÕ•Ω∏πïπëÖ—îà∞(ÄÄÄÄÄÄÄÄâÕïÕÕ•Ω∏πïπêà∞ÄâïπëÖ—îà∞(ÄÄÄÄ§§(ÄÄÄÅ±ΩçÖ—•Ω∏∞ÅëÖ—ïÃÄÙÅ}›ïëΩô}ç…µ}ÕïÕÕ•Ω∏†(ÄÄÄÄÄÄÄÅëÖ—Ñ∞ÅôΩ…µÖ—•Ω∏∞ÅëïÕ¡}—Â¡î∞Å…Ö›}±ΩçÖ—•Ω∏∞ÅÕ—Ö…–∞Åïπê∞(ÄÄÄÄ§(ÄÄÄÅÕ—Öâ±ï}•êÄÙÅÕ—»°ôΩ±ëï»πùï–†âï·—ï…πÖ±%êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄâ¡…ïπΩ¥àËÅÕ—»°ô•…Õ—}πÖµîÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâπΩ¥àËÅÕ—»°±ÖÕ—}πÖµîÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâµÖ•∞àËÅÕ—»°ïµÖ•∞ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâ—ï±ï¡°ΩπîàËÅÕ—»°¡°ΩπîÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏àËÅÕ—»°ôΩ…µÖ—•Ω∏ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâëïÕ¡}—Â¡îàËÅëïÕ¡}—Â¡î∞(ÄÄÄÄÄÄÄÄâ±•ï‘àËÅÕ—»°±ΩçÖ—•Ω∏ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâëÖ—ïÕ}ôΩ…µÖ—•Ω∏àËÅëÖ—ïÃ∞(ÄÄÄÄÄÄÄÄâç¡òàËÄâ=U$à∞(ÄÄÄÄÄÄÄÄâçΩµµïπ—Ö•…îàËÅòâïµÖπëîÅAÅ…óù’îÅŸ•ÑÅ]=É
‹ÅëΩÕÕ•ï»ÅÌÕ—Öâ±ï}•ëÙà∞(ÄÄÄÅÙ(()ëïòÅ}›ïëΩô}°ÖÕ}’ÕÖâ±ï}•ëïπ—•—‰°¡ÖÂ±ΩÖê§Ë(ÄÄÄÄààâµ√©ç°îÅ±ÑÅçÀ•Ö—•Ω∏Åêù’πîÅ¡•Õ—îÅŸ•ëîÅÕ§Å]=ÅΩµï–Å∞ù•ëïπ—•”§∏ààà(ÄÄÄÅ…ï—’…∏ÅâΩΩ∞†(ÄÄÄÄÄÄÄÅ}ç…µ}πΩ…µÖ±•Èï}ïµÖ•∞°¡ÖÂ±ΩÖêπùï–†âµÖ•∞à§§(ÄÄÄÄÄÄÄÅΩ»Å}ç…µ}πΩ…µÖ±•Èï}¡°Ωπî°¡ÖÂ±ΩÖêπùï–†â—ï±ï¡°Ωπîà§§(ÄÄÄÄÄÄÄÅΩ»Ä†(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}πΩ…µÖ±•Èï}πÖµî°¡ÖÂ±ΩÖêπùï–†â¡…ïπΩ¥à§§(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅ}ç…µ}πΩ…µÖ±•Èï}πÖµî°¡ÖÂ±ΩÖêπùï–†âπΩ¥à§§(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄ§(()]=}A}1}MQIQ}QÄÙÅëÖ—ï—•µîπëÖ—î†»¿»ÿ∞Ä‡∞Äƒ»§(()ëïòÅ}›ïëΩô}ôΩ±ëï…}ç…ïÖ—•Ωπ}ëÖ—î°ôΩ±ëï»§Ë(ÄÄÄÄààâIï—Ω’…πîÅ±ÑÅëÖ—îÅëîÅçÀ•Ö—•Ω∏Å]=∞ÅÕÖπÃÅ’—•±•Õï»Å’πîÅëÖ—îÅëîÅµ•ÕîÉÄÅ©Ω’»∏ààà(ÄÄÄÅ…Ö›}ŸÖ±’îÄÙÅπï·–†°ôΩ±ëï»πùï–°≠ï‰§ÅôΩ»Å≠ï‰Å•∏Ä†(ÄÄÄÄÄÄÄÄâç…ïÖ—ïë–à∞Äâç…ïÖ—ïë=∏à∞ÄâëÖ—ï…ïÖ—ïêà∞Äâç…ïÖ—•ΩπÖ—îà∞(ÄÄÄÄ§Å•òÅôΩ±ëï»πùï–°≠ï‰§§∞Å9Ωπî§(ÄÄÄÅ•òÅπΩ–Å…Ö›}ŸÖ±’îË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅëÖ—ï—•µîπëÖ—îπô…Ωµ•ÕΩôΩ…µÖ–°Õ—»°…Ö›}ŸÖ±’î§πÕ—…•¿†•lËƒ¡t§(ÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(()ëïòÅ}›ïëΩô}•Õ}Ω¡ïπ}ç¡ô}…ï≈’ïÕ–°ôΩ±ëï»§Ë(ÄÄÄÄààâ’—Ω…•ÕîÅ’π•≈’ïµïπ–Å±ïÃÅπΩ’ŸïÖ’‡ÅëΩÕÕ•ï…ÃÅAÅ…óù’ÃÅëï¡’•ÃÅ±îÄƒ»º¿‡º»¿»ÿ∏ààà(ÄÄÄÅôΩ±ëï…}—Â¡îÄÙÅ…îπÕ’à†(ÄÄÄÄÄÄÄÅ»âmyÑµË¿¥Âtà∞Äàà∞Å’π•çΩëïëÖ—ÑππΩ…µÖ±•Èî†(ÄÄÄÄÄÄÄÄÄÄÄÄâ9à∞ÅÕ—»°ôΩ±ëï»πùï–†â—Â¡îà§ÅΩ»Äàà§(ÄÄÄÄÄÄÄÄ§πïπçΩëî†âÖÕç•§à∞Äâ•ùπΩ…îà§πëïçΩëî†§π±Ω›ï»†§∞(ÄÄÄÄ§(ÄÄÄÅç…ïÖ—ïë}Ω∏ÄÙÅ}›ïëΩô}ôΩ±ëï…}ç…ïÖ—•Ωπ}ëÖ—î°ôΩ±ëï»§(ÄÄÄÅ•òÅôΩ±ëï…}—Â¡îÄÑÙÄâç¡òàÅΩ»Åç…ïÖ—ïë}Ω∏Å•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅ•òÅç…ïÖ—ïë}Ω∏ÄÅ]=}A}1}MQIQ}QË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî((ÄÄÄÅÕ—Ö—îÄÙÅ…îπÕ’à†(ÄÄÄÄÄÄÄÅ»âmyÑµË¿¥Âtà∞Äàà∞(ÄÄÄÄÄÄÄÅ’π•çΩëïëÖ—ÑππΩ…µÖ±•Èî†â9à∞ÅÕ—»†(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ±ëï»πùï–†âÕ—Ö—îà§ÅΩ»ÅôΩ±ëï»πùï–†âÕ—Ö—’Ãà§(ÄÄÄÄÄÄÄÄÄÄÄÅΩ»ÅôΩ±ëï»πùï–†â…ïù•Õ—…Ö—•ΩπM—Ö—îà§ÅΩ»Äàà(ÄÄÄÄÄÄÄÄ§§πïπçΩëî†âÖÕç•§à∞Äâ•ùπΩ…îà§πëïçΩëî†§π±Ω›ï»†§∞(ÄÄÄÄ§(ÄÄÄÅ—ï…µ•πÖ±}Õ—Ö—ïÃÄÙÅÏ(ÄÄÄÄÄÄÄÄâ•π—…Ö•π•πúà∞Äâ—ï…µ•πÖ—ïêà∞ÄâÕï…Ÿ•çïëΩπïëïç±Ö…ïêà∞(ÄÄÄÄÄÄÄÄâÕï…Ÿ•çïëΩπïŸÖ±•ëÖ—ïêà∞ÄâπΩ—â•±±Öâ±îà∞Äâ—Ωâ•±∞à∞Äââ•±±ïêà∞Äâ¡Ö•êà∞(ÄÄÄÄÄÄÄÄâçÖπçï±±ïêà∞ÄâçÖπçï±ïêà∞Äâ…ïô’Õïêà∞Äâ…ï©ïç—ïêà∞ÄâÖâÖπëΩπïêà∞(ÄÄÄÅÙ(ÄÄÄÅ•òÅÕ—Ö—îÅ•∏Å—ï…µ•πÖ±}Õ—Ö—ïÃË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅÕïÕÕ•Ωπ}ïπêÄÙÅ}›ïëΩô}ŸÖ±’î†(ÄÄÄÄÄÄÄÅôΩ±ëï»∞Äâ—…Ö•π•πùç—•Ωπ%πôºπÕïÕÕ•ΩππëÖ—îà∞ÄâÕïÕÕ•Ω∏πïπëÖ—îà∞(ÄÄÄÄÄÄÄÄâÕïÕÕ•Ω∏πïπêà∞ÄâïπëÖ—îà∞(ÄÄÄÄ§(ÄÄÄÅ•òÅÕïÕÕ•Ωπ}ïπêË(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅïπë}ëÖ—îÄÙÅëÖ—ï—•µîπëÖ—îπô…Ωµ•ÕΩôΩ…µÖ–°Õ—»°ÕïÕÕ•Ωπ}ïπê§πÕ—…•¿†•lËƒ¡t§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅïπë}ëÖ—îÄÅëÖ—ï—•µîπëÖ—ï—•µîππΩ‹°¡Â—Ëπ—•µïÈΩπî†â’…Ω¡îΩAÖ…•Ãà§§πëÖ—î†§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÄÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÕÃ(ÄÄÄÅ…ï—’…∏ÅQ…’î(()ëïòÅ}›ïëΩô}•Õ}ç¡ô}ôΩ±ëï»°ôΩ±ëï»§Ë(ÄÄÄÄààâ%πë•≈’îÅÕ§Å±îÅëΩÕÕ•ï»Å]=Å¡…ΩŸ•ïπ–ÅëîÅ5Ω∏ÅΩµ¡—îÅΩ…µÖ—•Ω∏∏ààà(ÄÄÄÅôΩ±ëï…}—Â¡îÄÙÅ’π•çΩëïëÖ—ÑππΩ…µÖ±•Èî†(ÄÄÄÄÄÄÄÄâ9à∞ÅÕ—»°ôΩ±ëï»πùï–†â—Â¡îà§ÅΩ»Äàà§(ÄÄÄÄ§πïπçΩëî†âÖÕç•§à∞Äâ•ùπΩ…îà§πëïçΩëî†§πçÖÕïôΩ±ê†§(ÄÄÄÅ…ï—’…∏Å…îπÕ’à°»âmyÑµË¿¥Âtà∞Äàà∞ÅôΩ±ëï…}—Â¡î§ÄÙÙÄâç¡òà(()ëïòÅ}›ïëΩô}Öç—•Ÿ•—‰°çΩπ—Öç–∞Å—•—±î∞Åëï—Ö•∞§Ë(ÄÄÄÄààâ©Ω’—îÅ’πîÅÖç—•Ÿ•”§Å’—•±•ÕÖâ±îÅÖ’ÕÕ§Å°Ω…ÃÅêù’∏ÅçΩπ—ï·—îÅëîÅ…ï≈◊©—îÅ±ÖÕ¨∏ààà(ÄÄÄÅçΩπ—Öç–πÕï—ëïôÖ’±–†âÖç—•Ÿ•—•ïÃà∞Åmt§π•πÕï…–†¿∞ÅÏ(ÄÄÄÄÄÄÄÄâ•êàËÅÕ—»°’’•êπ’’•ê–†§§∞ÄâëÖ—îàËÅ}ç…µ}πΩ‹†§∞Äâ≠•πêàËÄâ•πâΩ’πë}…ï≈’ïÕ–à∞(ÄÄÄÄÄÄÄÄâ—•—±îàËÅ—•—±î∞Äâëï—Ö•∞àËÅëï—Ö•∞∞Äâ¡…ïŸ•ï‹àËÄàà∞(ÄÄÄÄÄÄÄÄâÖ’—°Ω»àËÄâ’—ΩµÖ—•ÕÖ—•Ω∏Å]=à∞(ÄÄÄÅÙ§(()ëïòÅ}›ïëΩô}πï›}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞Å¡ÖÂ±ΩÖê∞ÅÕ—Öâ±ï}•ê§Ë(ÄÄÄÄààâΩπÕ—…’•–Å’πîÅ¡•Õ—îÅI4ÅçΩµ¡≥°—îÉÄÅ¡Ö…—•»Åêù’πîÅëïµÖπëîÅ5Ω∏ÅΩµ¡—îÅΩ…µÖ—•Ω∏∏ààà(ÄÄÄÅπΩ‹ÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅçΩπ—Öç–ÄÙÅÏ(ÄÄÄÄÄÄÄÄâ•êàËÅÕ—»°’’•êπ’’•ê–†§§∞(ÄÄÄÄÄÄÄÄâ¡…ïπΩ¥àËÅ}ç…µ}ôΩ…µÖ—}ô•…Õ—}πÖµî°¡ÖÂ±ΩÖêπùï–†â¡…ïπΩ¥à§§∞(ÄÄÄÄÄÄÄÄâπΩ¥àËÅ}ç…µ}ôΩ…µÖ—}±ÖÕ—}πÖµî°¡ÖÂ±ΩÖêπùï–†âπΩ¥à§§∞(ÄÄÄÄÄÄÄÄâ—ï±ï¡°ΩπîàËÅÕ—»°¡ÖÂ±ΩÖêπùï–†â—ï±ï¡°Ωπîà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâµÖ•∞àËÅÕ—»°¡ÖÂ±ΩÖêπùï–†âµÖ•∞à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏àËÅÕ—»°¡ÖÂ±ΩÖêπùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâ±•ï‘àËÅÕ—»°¡ÖÂ±ΩÖêπùï–†â±•ï‘à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâÕ—Ö—’–àËÅπï·–†°Õ—Ö—’ÃÅôΩ»ÅÕ—Ö—’ÃÅ•∏Å}ç…µ}Õ—Ö—’ÕïÃ°ëÖ—Ñ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅÕ—Ö—’ÃÅπΩ–Å•∏ÅI5}IMIY}MQQUML§∞Äâ9Ω’ŸïÖ’‡à§∞(ÄÄÄÄÄÄÄÄâëÖ—ïÕ}ôΩ…µÖ—•Ω∏àËÅÕ—»°¡ÖÂ±ΩÖêπùï–†âëÖ—ïÕ}ôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâç¡òàËÄâ=U$à∞Äâç¡ô}µΩπ—Öπ–àËÄàà∞ÄâçÖ…—ï}¡…ºàËÄàà∞(ÄÄÄÄÄÄÄÄâÖπ—ïçïëïπ—ÃàËÄàà∞ÄâùÖ…ëï}Ÿ’îàËÄàà∞Äâ—•—…ï}Õï©Ω’»àËÄàà∞(ÄÄÄÄÄÄÄÄâ—•—…ï}Õï©Ω’…}çπÖ¡ÃàËÄàà∞ÄâçΩµ¡—ï}çπÖ¡ÃàËÄàà∞ÄâçπÖ¡Õ}’Õï…πÖµîàËÄàà∞(ÄÄÄÄÄÄÄÄâçπÖ¡Õ}â•…—°}ÂïÖ»àËÄàà∞ÄâçπÖ¡Õ}¡ÖÕÕ›Ω…êàËÄàà∞Äâ•π—ïù…Ö—•Ωπ}ë…ÖçÖ»àËÄàà∞(ÄÄÄÄÄÄÄÄâëïÕ¡}—Â¡îàËÅÕ—»°¡ÖÂ±ΩÖêπùï–†âëïÕ¡}—Â¡îà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâ•ëïπ—•—ï}ç…ïÖ—•Ω∏àËÄàà∞Äâ•ëïπ—•—ï}Ω¨àËÄàà∞Äâô•πÖπçïµïπ—}ô–àËÄàà∞(ÄÄÄÄÄÄÄÄâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–àËÄàà∞ÄâµΩπ—Öπ—}ÖççΩ…ëï}ô–àËÄàà∞(ÄÄÄÄÄÄÄÄâô•πÖπçïµïπ—}¡ï…ÕΩ}¡ΩÕÕ•â±îàËÄàà∞Äâ…ïô’Õ}ô—}¡ï…ÕºàËÄàà∞(ÄÄÄÄÄÄÄÄâ…ïÕ—ï}Ö}ç°Ö…ùï}¡ï…ÕºàËÄàà∞Äâ•πÕç…•—}ô–àËÄàà∞Äâ…ï±Öπçï}ëÖ—îàËÄàà∞(ÄÄÄÄÄÄÄÄâÕ—Ö—’—}ÕïçΩπëÖ•…îàËÄàà∞ÄâΩ…•ù•πîàËÄâ5Ω∏ÅΩµ¡—îÅΩ…µÖ—•Ω∏à∞(ÄÄÄÄÄÄÄÄâçΩµµïπ—Ö•…ïÃàËÅòâïµÖπëîÅAÅ…óù’îÅÖ’—ΩµÖ—•≈’ïµïπ–ÅŸ•ÑÅ]=É
‹ÅëΩÕÕ•ï»ÅÌÕ—Öâ±ï}•ëÙ∏à∞(ÄÄÄÄÄÄÄÄâç…ïÖ—ïë}Ö–àËÅπΩ‹∞Äâ’¡ëÖ—ïë}Ö–àËÅπΩ‹∞ÄâÖç—•Ÿ•—•ïÃàËÅmt∞(ÄÄÄÄÄÄÄÄâÕΩ’…çîàËÄâ›ïëΩô}ç¡òà∞ÄâÕΩ’…çï}›ïëΩô}ôΩ±ëï…}•êàËÅÕ—Öâ±ï}•ê∞(ÄÄÄÅÙ(ÄÄÄÅ}›ïëΩô}Öç—•Ÿ•—‰†(ÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄâA•Õ—îÅçÀß•îÅëï¡’•ÃÅ5Ω∏ÅΩµ¡—îÅΩ…µÖ—•Ω∏à∞(ÄÄÄÄÄÄÄÅòâΩÕÕ•ï»ÅAÅÌÕ—Öâ±ï}•ëÙÅÕÂπç°…Ωπ•œ§ÅÖ’—ΩµÖ—•≈’ïµïπ–ÅŸ•ÑÅ]=∏à∞(ÄÄÄÄ§(ÄÄÄÅ…ï—’…∏ÅçΩπ—Öç–(()ëïòÅ}›ïëΩô}Ö¡¡±Â}çΩπ—Öç—}ëï—Ö•±Ã°çΩπ—Öç–∞Å¡ÖÂ±ΩÖê§Ë(ÄÄÄÄààâ•±∞ÅΩ»Å…ï¡Ö•»ÅI4µÕï±ïç–ÅŸÖ±’ïÃÅô…Ω¥ÅÖ∏ÅÖ±…ïÖë‰Å±•π≠ïêÅAÅôΩ±ëï»∏ààà(ÄÄÄÅç°Öπùïë}ô•ï±ëÃÄÙÅmt(ÄÄÄÅÕΩ’…çï}•Õ}›ïëΩòÄÙÅçΩπ—Öç–πùï–†âÕΩ’…çîà§ÄÙÙÄâ›ïëΩô}ç¡òà((ÄÄÄÅ•πçΩµ•πù}ôΩ…µÖ—•Ω∏ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅç’……ïπ—}ôΩ…µÖ—•Ω∏ÄÙÅÕ—»°çΩπ—Öç–πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÄ°•πçΩµ•πù}ôΩ…µÖ—•Ω∏Å•∏Å}I5}]=}=I5Q%=9L(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÄ°}ç…µ}•Õ}ïµ¡—‰°ç’……ïπ—}ôΩ…µÖ—•Ω∏§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ»Ä°ÕΩ’…çï}•Õ}›ïëΩò(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅç’……ïπ—}ôΩ…µÖ—•Ω∏ÅπΩ–Å•∏Å}I5}]=}=I5Q%=9L§§§Ë(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâôΩ…µÖ—•Ω∏âtÄÙÅ•πçΩµ•πù}ôΩ…µÖ—•Ω∏(ÄÄÄÄÄÄÄÅç’……ïπ—}ôΩ…µÖ—•Ω∏ÄÙÅ•πçΩµ•πù}ôΩ…µÖ—•Ω∏(ÄÄÄÄÄÄÄÅç°Öπùïë}ô•ï±ëÃπÖ¡¡ïπê†âôΩ…µÖ—•Ω∏à§((ÄÄÄÅ•πçΩµ•πù}ëïÕ¡}—Â¡îÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âëïÕ¡}—Â¡îà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅç’……ïπ—}ëïÕ¡}—Â¡îÄÙÅÕ—»°çΩπ—Öç–πùï–†âëïÕ¡}—Â¡îà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÄ°ç’……ïπ—}ôΩ…µÖ—•Ω∏ÄÙÙÄâM@àÅÖπêÅ•πçΩµ•πù}ëïÕ¡}—Â¡îÅ•∏ÅÏâ%9%Q%0à∞ÄâYâÙ(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÄ°}ç…µ}•Õ}ïµ¡—‰°ç’……ïπ—}ëïÕ¡}—Â¡î§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ»Ä°ÕΩ’…çï}•Õ}›ïëΩò(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅç’……ïπ—}ëïÕ¡}—Â¡îÅπΩ–Å•∏ÅÏâ%9%Q%0à∞ÄâYâÙ§§§Ë(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâëïÕ¡}—Â¡îâtÄÙÅ•πçΩµ•πù}ëïÕ¡}—Â¡î(ÄÄÄÄÄÄÄÅç°Öπùïë}ô•ï±ëÃπÖ¡¡ïπê†â¡Ö…çΩ’…ÃÅM@à§((ÄÄÄÅ•πçΩµ•πù}±ΩçÖ—•Ω∏ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†â±•ï‘à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅç’……ïπ—}±ΩçÖ—•Ω∏ÄÙÅÕ—»°çΩπ—Öç–πùï–†â±•ï‘à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅçÖπΩπ•çÖ±}±ΩçÖ—•ΩπÃÄÙÅÕï–°}I5}]=}9QILπŸÖ±’ïÃ†§§(ÄÄÄÅ•òÄ°•πçΩµ•πù}±ΩçÖ—•Ω∏(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÄ°}ç…µ}•Õ}ïµ¡—‰°ç’……ïπ—}±ΩçÖ—•Ω∏§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ»Ä°ÕΩ’…çï}•Õ}›ïëΩò(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅ•πçΩµ•πù}±ΩçÖ—•Ω∏Å•∏ÅçÖπΩπ•çÖ±}±ΩçÖ—•ΩπÃ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅç’……ïπ—}±ΩçÖ—•Ω∏ÅπΩ–Å•∏ÅçÖπΩπ•çÖ±}±ΩçÖ—•ΩπÃ§§§Ë(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâ±•ï‘âtÄÙÅ•πçΩµ•πù}±ΩçÖ—•Ω∏(ÄÄÄÄÄÄÄÅç°Öπùïë}ô•ï±ëÃπÖ¡¡ïπê†â±•ï‘à§((ÄÄÄÅ•πçΩµ•πù}ëÖ—ïÃÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âëÖ—ïÕ}ôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅç’……ïπ—}ëÖ—ïÃÄÙÅÕ—»°çΩπ—Öç–πùï–†âëÖ—ïÕ}ôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ±ïùÖçÂ}µÖç°•πï}ëÖ—ïÃÄÙÅâΩΩ∞†(ÄÄÄÄÄÄÄÅ…îπÕïÖ…ç†°»âqëÏ—ÙµqëÏ…ÙµqëÏ…Ùà∞Åç’……ïπ—}ëÖ—ïÃ§(ÄÄÄÄÄÄÄÅΩ»ÄàÉäHÄàÅ•∏Åç’……ïπ—}ëÖ—ïÃ(ÄÄÄÄ§(ÄÄÄÅ•òÄ°•πçΩµ•πù}ëÖ—ïÃ(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÄ°}ç…µ}•Õ}ïµ¡—‰°ç’……ïπ—}ëÖ—ïÃ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ»Ä°ÕΩ’…çï}•Õ}›ïëΩòÅÖπêÅ±ïùÖçÂ}µÖç°•πï}ëÖ—ïÃ§§§Ë(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâëÖ—ïÕ}ôΩ…µÖ—•Ω∏âtÄÙÅ•πçΩµ•πù}ëÖ—ïÃ(ÄÄÄÄÄÄÄÅç°Öπùïë}ô•ï±ëÃπÖ¡¡ïπê†âëÖ—ïÃÅÕΩ’°Ö•”•ïÃà§((ÄÄÄÅ•òÅç°Öπùïë}ô•ï±ëÃË(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅ…ï—’…∏Åç°Öπùïë}ô•ï±ëÃ(()ëïòÅ}›ïëΩô}çΩπ—Öç—}πÖµï}µÖ—ç°ïÃ°ôΩ±ëï»∞ÅçΩπ—Öç–§Ë(ÄÄÄÄààâΩµ¡Ö…îÅ∞ù•ëïπ—•”§ÅÕÖπÃÅ©ÖµÖ•ÃÅµΩë•ô•ï»Å∞ùΩ…—°Ωù…Ö¡°îÅÕ—ΩçØ•îÅëÖπÃÅ±îÅI4∏ààà(ÄÄÄÅô•…Õ—}πÖµî∞Å±ÖÕ—}πÖµîÄÙÅ}›ïëΩô}Ö——ïπëïï}πÖµî°ôΩ±ëï»§(ÄÄÄÅ…ï—’…∏ÅâΩΩ∞†(ÄÄÄÄÄÄÄÅô•…Õ—}πÖµî(ÄÄÄÄÄÄÄÅÖπêÅ±ÖÕ—}πÖµî(ÄÄÄÄÄÄÄÅÖπêÅ}›ïëΩô}πΩ…µÖ±•Èï}πÖµî°çΩπ—Öç–πùï–†â¡…ïπΩ¥à§§ÄÙÙÅô•…Õ—}πÖµî(ÄÄÄÄÄÄÄÅÖπêÅ}›ïëΩô}πΩ…µÖ±•Èï}πÖµî°çΩπ—Öç–πùï–†âπΩ¥à§§ÄÙÙÅ±ÖÕ—}πÖµî(ÄÄÄÄ§(()ëïòÅ}›ïëΩô}µÖ—ç°}çΩπ—Öç–°ôΩ±ëï»∞ÅçΩπ—Öç—Ã§Ë(ÄÄÄÅïµÖ•±Ã∞Å¡°ΩπïÃ∞Å|ÄÙÅ}›ïëΩô}Ö——ïπëïï}ŸÖ±’ïÃ°ôΩ±ëï»§(ÄÄÄÅïµÖ•±}µÖ—ç°ïÃÄÙÅl(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÅôΩ»ÅçΩπ—Öç–Å•∏ÅçΩπ—Öç—Ã(ÄÄÄÄÄÄÄÅ•òÅ}ç…µ}πΩ…µÖ±•Èï}ïµÖ•∞°çΩπ—Öç–πùï–†âµÖ•∞à§§Å•∏ÅïµÖ•±Ã(ÄÄÄÅt(ÄÄÄÅ•òÅïµÖ•±ÃÅÖπêÅ±ï∏°ïµÖ•±}µÖ—ç°ïÃ§ÄÙÙÄƒË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅïµÖ•±}µÖ—ç°ïÕl¡t∞ÄâïµÖ•∞à(ÄÄÄÅ¡°Ωπï}µÖ—ç°ïÃÄÙÅl(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÅôΩ»ÅçΩπ—Öç–Å•∏ÅçΩπ—Öç—Ã(ÄÄÄÄÄÄÄÅ•òÅ}ç…µ}πΩ…µÖ±•Èï}¡°Ωπî°çΩπ—Öç–πùï–†â—ï±ï¡°Ωπîà§§Å•∏Å¡°ΩπïÃ(ÄÄÄÅt(ÄÄÄÅ•òÅ¡°ΩπïÃÅÖπêÅ±ï∏°¡°Ωπï}µÖ—ç°ïÃ§ÄÙÙÄƒË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å¡°Ωπï}µÖ—ç°ïÕl¡t∞Äâ¡°Ωπîà((ÄÄÄÅô•…Õ—}πÖµî∞Å±ÖÕ—}πÖµîÄÙÅ}›ïëΩô}Ö——ïπëïï}πÖµî°ôΩ±ëï»§(ÄÄÄÅ•òÅπΩ–Åô•…Õ—}πÖµîÅΩ»ÅπΩ–Å±ÖÕ—}πÖµîË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî∞Å9Ωπî(ÄÄÄÅπÖµï}µÖ—ç°ïÃÄÙÅl(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÅôΩ»ÅçΩπ—Öç–Å•∏ÅçΩπ—Öç—Ã(ÄÄÄÄÄÄÄÅ•òÅ}›ïëΩô}πΩ…µÖ±•Èï}πÖµî°çΩπ—Öç–πùï–†â¡…ïπΩ¥à§§ÄÙÙÅô•…Õ—}πÖµî(ÄÄÄÄÄÄÄÅÖπêÅ}›ïëΩô}πΩ…µÖ±•Èï}πÖµî°çΩπ—Öç–πùï–†âπΩ¥à§§ÄÙÙÅ±ÖÕ—}πÖµî(ÄÄÄÄÄÄÄÅÖπêÄ°πΩ–ÅïµÖ•±ÃÅΩ»ÅπΩ–Å}ç…µ}πΩ…µÖ±•Èï}ïµÖ•∞°çΩπ—Öç–πùï–†âµÖ•∞à§§(ÄÄÄÄÄÄÄÄÄÄÄÄÅΩ»Å}ç…µ}πΩ…µÖ±•Èï}ïµÖ•∞°çΩπ—Öç–πùï–†âµÖ•∞à§§Å•∏ÅïµÖ•±Ã§(ÄÄÄÄÄÄÄÅÖπêÄ°πΩ–Å¡°ΩπïÃÅΩ»ÅπΩ–Å}ç…µ}πΩ…µÖ±•Èï}¡°Ωπî°çΩπ—Öç–πùï–†â—ï±ï¡°Ωπîà§§(ÄÄÄÄÄÄÄÄÄÄÄÄÅΩ»Å}ç…µ}πΩ…µÖ±•Èï}¡°Ωπî°çΩπ—Öç–πùï–†â—ï±ï¡°Ωπîà§§Å•∏Å¡°ΩπïÃ§(ÄÄÄÅt(ÄÄÄÅ•òÅ±ï∏°πÖµï}µÖ—ç°ïÃ§ÄÙÙÄƒË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅπÖµï}µÖ—ç°ïÕl¡t∞ÄâπÖµîà(ÄÄÄÅ…ï—’…∏Å9Ωπî∞Å9Ωπî(()ëïòÅ}›ïëΩô}Õ—Ω…ï}¡Öùî†(ÄÄÄÄÄÄÄÅ•—ïµÃ∞ÅëÖ—Ñ∞Å¡Öùî∞Å—Ω—Ö±}çΩ’π–ı9Ωπî∞Ä®∞Å’¡ëÖ—ï}ÕÂπç}Õ—Ö—îıQ…’î§Ë(ÄÄÄÄààâIÖ¡¡…Ωç°îÅï–Å¡ï…Õ•Õ—îÅ’πîÅ¡ÖùîÅÕÖπÃÅçΩπç’……ïπçï»Å’πîÅÖ’—…îÅµ’—Ö—•Ω∏ÅI4∏ààà(ÄÄÄÅ›•—†Å}I5}I=9%1%Q%=9}1=,Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å}›ïëΩô}Õ—Ω…ï}¡Öùï}±Ωç≠ïê†(ÄÄÄÄÄÄÄÄÄÄÄÅ•—ïµÃ∞ÅëÖ—ÑÅ•òÅëÖ—ÑÅ•ÃÅπΩ–Å9ΩπîÅï±ÕîÅ±ΩÖë}ëÖ—Ñ†§∞Å¡Öùî∞Å—Ω—Ö±}çΩ’π–∞(ÄÄÄÄÄÄÄÄÄÄÄÅ’¡ëÖ—ï}ÕÂπç}Õ—Ö—îı’¡ëÖ—ï}ÕÂπç}Õ—Ö—î∞(ÄÄÄÄÄÄÄÄ§(()ëïòÅ}›ïëΩô}Õ—Ω…ï}¡Öùï}±Ωç≠ïê†(ÄÄÄÄÄÄÄÅ•—ïµÃ∞ÅëÖ—Ñ∞Å¡Öùî∞Å—Ω—Ö±}çΩ’π–ı9Ωπî∞Ä®∞Å’¡ëÖ—ï}ÕÂπç}Õ—Ö—îıQ…’î§Ë(ÄÄÄÅçΩπ—Öç—ÃÄÙÅëÖ—ÑπÕï—ëïôÖ’±–†âç…µ}çΩπ—Öç—Ãà∞Åmt§(ÄÄÄÅπΩ‹ÄÙÅ}›ïëΩô}πΩ‹†§(ÄÄÄÅç…µ}ç°ÖπùïêÄÙÅÖ±Õî(ÄÄÄÅç…ïÖ—ïë}çΩπ—Öç—ÃÄÙÄ¿(ÄÄÄÅ±•π≠ïë}ôΩ±ëï…ÃÄÙÄ¿(ÄÄÄÅ¡ïπë•πù}…ïŸ•ï›ÃÄÙÄ¿(ÄÄÄÅ›•—†Å}›ïëΩô}çΩππïç–†§ÅÖÃÅëàË(ÄÄÄÄÄÄÄÅôΩ»ÅôΩ±ëï»Å•∏Å•—ïµÃË(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°ôΩ±ëï»∞Åë•ç–§ÅΩ»ÅπΩ–ÅôΩ±ëï»πùï–†âï·—ï…πÖ±%êà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Öâ±ï}•êÄÙÅÕ—»°ôΩ±ëï…lâï·—ï…πÖ±%êât§(ÄÄÄÄÄÄÄÄÄÄÄÅ¡…ïŸ•Ω’Õ}ô’πë•πù}Õ—Ö—’ÃÄÙÄàà(ÄÄÄÄÄÄÄÄÄÄÄÅ¡…ïŸ•Ω’Õ}…ïÕΩ’…çîÄÙÅëàπï·ïç’—î†ààà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅM1PÅ¡ÖÂ±ΩÖë}©ÕΩ∏ÅI=4Å›ïëΩô}…ïÕΩ’…çïÃ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ]!IÅ…ïÕΩ’…çï}—Â¡îÙù…ïù•Õ—…Ö—•ΩπΩ±ëï»úÅ9ÅÕ—Öâ±ï}•êÙ¸(ÄÄÄÄÄÄÄÄÄÄÄÄààà∞Ä°Õ—Öâ±ï}•ê∞§§πôï—ç°Ωπî†§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ¡…ïŸ•Ω’Õ}…ïÕΩ’…çîË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡…ïŸ•Ω’Õ}ô’πë•πù}Õ—Ö—’ÃÄÙÅ}›ïëΩô}ô…Öπçï}—…ÖŸÖ•±}Õ—Ö—’Ã†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ©ÕΩ∏π±ΩÖëÃ°¡…ïŸ•Ω’Õ}…ïÕΩ’…çïlâ¡ÖÂ±ΩÖë}©ÕΩ∏ât§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»∞Å©ÕΩ∏π)M=9ïçΩëï……Ω»§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡…ïŸ•Ω’Õ}ô’πë•πù}Õ—Ö—’ÃÄÙÄàà(ÄÄÄÄÄÄÄÄÄÄÄÅç…µ}¡ÖÂ±ΩÖêÄÙÅ}›ïëΩô}çΩπ—Öç—}¡ÖÂ±ΩÖê°ôΩ±ëï»∞ÅëÖ—Ñ§(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖë}©ÕΩ∏ÄÙÅ©ÕΩ∏πë’µ¡Ã°ôΩ±ëï»∞ÅïπÕ’…ï}ÖÕç•§ıÖ±Õî∞ÅÕï¡Ö…Ö—Ω…ÃÙ†à∞à∞ÄàËà§§(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïµΩ—ï}ëÖ—îÄÙÅπï·–†°Õ—»°ôΩ±ëï»πùï–°≠ï‰§§ÅôΩ»Å≠ï‰Å•∏Ä†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ’¡ëÖ—ïë–à∞Äâ’¡ëÖ—ïë=∏à∞ÄâµΩë•ô•ïë–à∞ÄâëÖ—ïU¡ëÖ—ïêà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâç…ïÖ—ïë–à∞Äâç…ïÖ—ïë=∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§Å•òÅôΩ±ëï»πùï–°≠ï‰§§∞Å9Ωπî§(ÄÄÄÄÄÄÄÄÄÄÄÅëàπï·ïç’—î†ààà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ%9MIPÅ%9Q<Å›ïëΩô}…ïÕΩ’…çïÃ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ°…ïÕΩ’…çï}—Â¡î∞ÅÕ—Öâ±ï}•ê∞Å¡ÖÂ±ΩÖë}©ÕΩ∏∞Å…ïµΩ—ï}ëÖ—î∞ÅÕÂπçïë}Ö–§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅY1ULÄ†ù…ïù•Õ—…Ö—•ΩπΩ±ëï»ú∞Ä¸∞Ä¸∞Ä¸∞Ä¸§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ=8Å=91%P°…ïÕΩ’…çï}—Â¡î∞ÅÕ—Öâ±ï}•ê§Å<ÅUAQÅMP(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖë}©ÕΩ∏ıï·ç±’ëïêπ¡ÖÂ±ΩÖë}©ÕΩ∏∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ïµΩ—ï}ëÖ—îıï·ç±’ëïêπ…ïµΩ—ï}ëÖ—î∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕÂπçïë}Ö–ıï·ç±’ëïêπÕÂπçïë}Ö–(ÄÄÄÄÄÄÄÄÄÄÄÄààà∞Ä°Õ—Öâ±ï}•ê∞Å¡ÖÂ±ΩÖë}©ÕΩ∏∞Å…ïµΩ—ï}ëÖ—î∞ÅπΩ‹§§(ÄÄÄÄÄÄÄÄÄÄÄÅï·•Õ—•πúÄÙÅëàπï·ïç’—î†ààà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅM1PÅçΩπ—Öç—}•êÅI=4Å›ïëΩô}çΩπ—Öç—}±•π≠Ã(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ]!IÅ…ïÕΩ’…çï}—Â¡îÙù…ïù•Õ—…Ö—•ΩπΩ±ëï»úÅ9Å…ïÕΩ’…çï}•êÙ¸(ÄÄÄÄÄÄÄÄÄÄÄÄààà∞Ä°Õ—Öâ±ï}•ê∞§§πôï—ç°Ωπî†§(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–ÄÙÅ9Ωπî(ÄÄÄÄÄÄÄÄÄÄÄÅµï—°ΩêÄÙÅ9Ωπî(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅï·•Õ—•πúË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–ÄÙÅπï·–†°åÅôΩ»ÅåÅ•∏ÅçΩπ—Öç—ÃÅ•òÅåπùï–†â•êà§ÄÙÙÅï·•Õ—•πùlâçΩπ—Öç—}•êât§∞Å9Ωπî§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµï—°ΩêÄÙÄâÕ—Öâ±îàÅ•òÅçΩπ—Öç–Åï±ÕîÅ9Ωπî(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞Åµï—°ΩêÄÙÅ}›ïëΩô}µÖ—ç°}çΩπ—Öç–°ôΩ±ëï»∞ÅçΩπ—Öç—Ã§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞Å|∞Å|ÄÙÅô•πë}Ω…}ç…ïÖ—ï}ç…µ}çΩπ—Öç–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅëÖ—Ñ∞Åç…µ}¡ÖÂ±ΩÖê∞Äâ›ïëΩô}ç¡òà∞Åï·—ï…πÖ±}•êıÕ—Öâ±ï}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕï±ïç—ïë}çΩπ—Öç—}•êıçΩπ—Öç–πùï–†â•êà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ…ëï…ïë}çΩΩ…ë•πÖ—ïÃıQ…’î∞Å…ïçΩ…ë}Öç—•Ÿ•—‰ıÖ±Õî∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅç…µ}ç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}›ïëΩô}Öç—•Ÿ•—‰†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞Äâ9Ω’Ÿï±±îÅëïµÖπëîÅAÅ…óù’îà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâΩÕÕ•ï»ÅÌÕ—Öâ±ï}•ëÙÅÖÕÕΩçß§ÅÖ’—ΩµÖ—•≈’ïµïπ–ÅŸ•ÑÅ]=∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅï±•òÄ°}›ïëΩô}°ÖÕ}’ÕÖâ±ï}•ëïπ—•—‰°ç…µ}¡ÖÂ±ΩÖê§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅ}›ïëΩô}•Õ}Ω¡ïπ}ç¡ô}…ï≈’ïÕ–°ôΩ±ëï»§§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡…Ω¡ΩÕïêÄÙÅ}›ïëΩô}πï›}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞Åç…µ}¡ÖÂ±ΩÖê∞ÅÕ—Öâ±ï}•ê§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞Å•πâΩ’πê∞Åç…ïÖ—ïêÄÙÅô•πë}Ω…}ç…ïÖ—ï}ç…µ}çΩπ—Öç–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅëÖ—Ñ∞Åç…µ}¡ÖÂ±ΩÖê∞Äâ›ïëΩô}ç¡òà∞Åï·—ï…πÖ±}•êıÕ—Öâ±ï}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡…Ω¡ΩÕïë}çΩπ—Öç–ı¡…Ω¡ΩÕïê∞ÅΩ…ëï…ïë}çΩΩ…ë•πÖ—ïÃıQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ïçΩ…ë}Öç—•Ÿ•—‰ıÖ±Õî∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅç…µ}ç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅç…ïÖ—ïêË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµï—°ΩêÄÙÄâç…ïÖ—ïêà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅç…ïÖ—ïë}çΩπ—Öç—ÃÄ¨ÙÄƒ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅï±•òÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ|∞Åµï—°ΩêÄÙÅ}›ïëΩô}µÖ—ç°}çΩπ—Öç–°ôΩ±ëï»∞ÅmçΩπ—Öç—t§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµï—°ΩêÄÙÅµï—°ΩêÅΩ»ÄâµÖ—ç°ïêà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}›ïëΩô}Öç—•Ÿ•—‰†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞Äâ9Ω’Ÿï±±îÅëïµÖπëîÅAÅ…óù’îà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâΩÕÕ•ï»ÅÌÕ—Öâ±ï}•ëÙÅÖÕÕΩçß§ÅÖ’—ΩµÖ—•≈’ïµïπ–ÅŸ•ÑÅ]=∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅï±•òÅ•πâΩ’πêπùï–†âÕ—Ö—’Ãà§ÄÙÙÄâ¡ïπë•πù}…ïŸ•ï‹àË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡ïπë•πù}…ïŸ•ï›ÃÄ¨ÙÄƒ(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï¡Ö•…ïë}ô•ï±ëÃÄÙÅ}›ïëΩô}Ö¡¡±Â}çΩπ—Öç—}ëï—Ö•±Ã°çΩπ—Öç–∞Åç…µ}¡ÖÂ±ΩÖê§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ…ï¡Ö•…ïë}ô•ï±ëÃË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}›ïëΩô}Öç—•Ÿ•—‰†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞Äâ%πôΩ…µÖ—•ΩπÃÅAÅÕÂπç°…Ωπ•œ•ïÃà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ°Öµ¡ÃÅçΩµ¡≥•”•ÃÅëï¡’•ÃÅ]=ÄËÄà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ¨Äà∞Äàπ©Ω•∏°…ï¡Ö•…ïë}ô•ï±ëÃ§Ä¨Äà∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅç…µ}ç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄåÅ1ÑÅ¡À•ÕïπçîÅë‘ÅëΩÕÕ•ï»Å]=Å¡…Ω’ŸîÅ≈’îÅ±îÅçΩµ¡—îÅAÅïÕ–ÅÖç—•ò∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄåÅ‰ÅçΩµ¡…•ÃÅÕ§Å’πîÅÖπç•ïππîÅÀ•¡ΩπÕîÅI4Å•πë•≈’Ö•–ÅïπçΩ…îÉ
¨Å9=8É
Ï∏(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅÕ—»°çΩπ—Öç–πùï–†âç¡òà§ÅΩ»Äàà§πÕ—…•¿†§π’¡¡ï»†§ÄÑÙÄâ=U$àË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâç¡òâtÄÙÄâ=U$à(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅç…µ}ç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄåÅK•¡Ö…îÅÖ’ÕÕ§Å±ïÃÅ¡•Õ—ïÃÅçÀß•ïÃÅΩ‘Å…Ö¡¡…Ωç£•ïÃÅÖŸÖπ–Å≈’îÅ±ï’»(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄåÅ¡…ΩŸïπÖπçîÅ]=ÅÕΩ•–Åïπ…ïù•Õ—À•îÅëÖπÃÅ±îÅI4∏(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ}›ïëΩô}•Õ}ç¡ô}ôΩ±ëï»°ôΩ±ëï»§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ}ç…µ}…ïçΩ…ë}Ω…•ù•∏†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ5Ω∏ÅΩµ¡—îÅΩ…µÖ—•Ω∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕΩ’…çîÙâ›ïëΩô}ç¡òà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅï·—ï…πÖ±}•êıÕ—Öâ±ï}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅëÖ—îı}ç…µ}πΩ‹†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅç…µ}ç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ|∞Å|∞ÅÖ——ïπëïï}•êÄÙÅ}›ïëΩô}Ö——ïπëïï}ŸÖ±’ïÃ°ôΩ±ëï»§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅëàπï·ïç’—î†ààà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ%9MIPÅ%9Q<Å›ïëΩô}çΩπ—Öç—}±•π≠Ã(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ°çΩπ—Öç—}•ê∞Å…ïÕΩ’…çï}—Â¡î∞Å…ïÕΩ’…çï}•ê∞ÅÖ——ïπëïï}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµÖ—ç°}µï—°Ωê∞Å±•π≠ïë}Ö–∞Å’¡ëÖ—ïë}Ö–§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅY1ULÄ†¸∞Äù…ïù•Õ—…Ö—•ΩπΩ±ëï»ú∞Ä¸∞Ä¸∞Ä¸∞Ä¸∞Ä¸§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ=8Å=91%P°…ïÕΩ’…çï}—Â¡î∞Å…ïÕΩ’…çï}•ê§Å<ÅUAQÅMP(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—}•êıï·ç±’ëïêπçΩπ—Öç—}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖ——ïπëïï}•êıï·ç±’ëïêπÖ——ïπëïï}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµÖ—ç°}µï—°Ωêıï·ç±’ëïêπµÖ—ç°}µï—°Ωê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ’¡ëÖ—ïë}Ö–ıï·ç±’ëïêπ’¡ëÖ—ïë}Ö–(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄààà∞Ä°çΩπ—Öç—lâ•êât∞ÅÕ—Öâ±ï}•ê∞ÅÖ——ïπëïï}•ê∞Åµï—°Ωê∞ÅπΩ‹∞ÅπΩ‹§§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡…ïŸ•Ω’Õ}Õ—Ö—’Õ}ïŸ•ëïπçîÄÙÅ¡…ïŸ•Ω’Õ}ô’πë•πù}Õ—Ö—’Ã(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÄ°πΩ–Å¡…ïŸ•Ω’Õ}Õ—Ö—’Õ}ïŸ•ëïπçî(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅçΩπ—Öç–πùï–†âÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô—}ÕΩ’…çîà§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÑÙÅI5}59U1}MQQUM}M=UI(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅÕ—»°çΩπ—Öç–πùï–†âÕΩ’…çï}›ïëΩô}ôΩ±ëï…}•êà§ÅΩ»Äàà§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÙÙÅÕ—Öâ±ï}•ê§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄåÅK•¡Ö…îÅÖ’ÕÕ§Å±ïÃÅëΩÕÕ•ï…ÃÅëΩπ–Å±îÅ…ï—Ω’»ÉÄÅÅÅŸÖ±•ëÖ—ïëÅÄ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄåÅÑÅì•´ÄÅ…ïµ¡±Öè§Å±îÅ¡ÖÂ±ΩÖêÅêù•πÕ—…’ç—•Ω∏ÅëÖπÃÅ±îÅçÖç°î∏(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡…ïŸ•Ω’Õ}Õ—Ö—’Õ}ïŸ•ëïπçîÄÙÅÕ—»†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–πùï–†âÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–à§ÅΩ»Äàà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§πÕ—…•¿†§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅç’……ïπ—}ô’πë•πù}Õ—Ö—’ÃÄÙÅ}›ïëΩô}ô…Öπçï}—…ÖŸÖ•±}Õ—Ö—’Ã†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅôΩ±ëï»∞Å¡…ïŸ•Ω’Õ}Õ—Ö—’Ãı¡…ïŸ•Ω’Õ}Õ—Ö—’Õ}ïŸ•ëïπçî∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄåÉ
¨Å∏ÅçΩ’…ÃÉ
ÏÅ…ïÕ—îÅ’πîÉ•—Ö¡îÅ¡…ΩŸ•ÕΩ•…îÄËÅ’πîÅì•ç•Õ•Ω∏Åëî(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄåÅ…ïô’ÃÅ…ïµΩπ”•îÅ¡Ö»Å]=ÅëΩ•–Å¡Ω’ŸΩ•»Å±ÑÅç≥——’…ï»∞Å∑©µîÅÕ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄåÅçï——îÉ•—Ö¡îÅÖŸÖ•–É•”§Åœ•±ïç—•Ωπª•îÅµÖπ’ï±±ïµïπ–∏Å1ïÃÅÖ’—…ïÃ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄåÅç°Ω•‡ÅµÖπ’ï±ÃÅ…ïÕ—ïπ–Å¡…Ω”•ü•Ã∏(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµÖπ’Ö±}ô’πë•πù}•Õ}¡…ΩŸ•Õ•ΩπÖ∞ÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–πùï–†âÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô—}ÕΩ’…çîà§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÙÙÅI5}59U1}MQQUM}M=UI(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅçΩπ—Öç–πùï–†âÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–à§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÙÙÄâïπ}çΩ’…Õ}•πÕ—…’ç—•Ω∏à(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÄ°ç’……ïπ—}ô’πë•πù}Õ—Ö—’ÃÄÙÙÄâ…ïô’Õïîà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÄ°çΩπ—Öç–πùï–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô—}ÕΩ’…çîà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§ÄÑÙÅI5}59U1}MQQUM}M=UI(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ»ÅµÖπ’Ö±}ô’πë•πù}•Õ}¡…ΩŸ•Õ•ΩπÖ∞§§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ω…ïë}ô’πë•πù}›ÖÕ}…ïô’ÕïêÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–πùï–†âÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–à§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÙÙÄâ…ïô’Õïîà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–ÅÕ—Ω…ïë}ô’πë•πù}›ÖÕ}…ïô’ÕïêË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–âtÄÙÄâ…ïô’Õïîà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅç…µ}ç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅµÖπ’Ö±}ô’πë•πù}•Õ}¡…ΩŸ•Õ•ΩπÖ∞Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–π¡Ω¿†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô—}ÕΩ’…çîà∞Å9Ωπî∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅç…µ}ç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµÖπ’Ö±}ÕïçΩπëÖ…Â}•Õ}¡…ΩŸ•Õ•ΩπÖ∞ÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–πùï–†âÕ—Ö—’—}ÕïçΩπëÖ•…ï}ÕΩ’…çîà§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÙÙÅI5}59U1}MQQUM}M=UI(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅçΩπ—Öç–πùï–†âÕ—Ö—’—}ÕïçΩπëÖ•…îà§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÙÙÄâ•πÖπçïµïπ–ÅPÅï∏ÅçΩ’…Ãà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÄ†°çΩπ—Öç–πùï–†âÕ—Ö—’—}ÕïçΩπëÖ•…ï}ÕΩ’…çîà§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÑÙÅI5}59U1}MQQUM}M=UI(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ»ÅµÖπ’Ö±}ÕïçΩπëÖ…Â}•Õ}¡…ΩŸ•Õ•ΩπÖ∞§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅçΩπ—Öç–πùï–†âÕ—Ö—’—}ÕïçΩπëÖ•…îà§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÑÙÄâ•πÖπçïµïπ–ÅPÅ…ïô’œ§à§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâÕ—Ö—’—}ÕïçΩπëÖ•…îâtÄÙÄâ•πÖπçïµïπ–ÅPÅ…ïô’œ§à(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅµÖπ’Ö±}ÕïçΩπëÖ…Â}•Õ}¡…ΩŸ•Õ•ΩπÖ∞Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–π¡Ω¿†âÕ—Ö—’—}ÕïçΩπëÖ•…ï}ÕΩ’…çîà∞Å9Ωπî§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅç…µ}ç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ¡…ïŸ•Ω’Õ}ô’πë•πù}Õ—Ö—’ÃÄÑÙÄâ…ïô’ÕïîàË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}Öëë}ô’πë•πù}…ïô’ÕÖ±}πΩ—•ô•çÖ—•ΩπÃ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅëÖ—Ñ∞ÅçΩπ—Öç–∞ÅÕ—Öâ±ï}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}Õïπë}ô—}…ïô’ÕÖ±}µïÕÕÖùïÃ°ëÖ—Ñ∞ÅçΩπ—Öç–§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅç…µ}ç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÄ°πΩ–ÅÕ—Ω…ïë}ô’πë•πù}›ÖÕ}…ïô’Õïê(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ»Å¡…ïŸ•Ω’Õ}ô’πë•πù}Õ—Ö—’ÃÄÑÙÄâ…ïô’Õïîà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ|∞Å…ï±Öπçï}ç°ÖπùïêÄÙÅ}ç…µ}Õç°ïë’±ï}ô—}…ïô’ÕÖ±}…ï±Öπçî†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕΩ’…çîÙâ›ïëΩô}ô—}…ïô’ÕÖ∞à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—Öâ±ï}•êıÕ—Öâ±ï}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅç…µ}ç°ÖπùïêÄÙÅ…ï±Öπçï}ç°ÖπùïêÅΩ»Åç…µ}ç°Öπùïê(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ±•π≠ïë}ôΩ±ëï…ÃÄ¨ÙÄƒ(ÄÄÄÄÄÄÄÅ•òÅ’¡ëÖ—ï}ÕÂπç}Õ—Ö—îË(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö—îÄÙÅÏâπï·—}¡ÖùîàËÅ¡ÖùîÄ¨Äƒ∞Äâ•π}¡…Ωù…ïÕÃàËÅQ…’î∞Äâ±ÖÕ—}ï……Ω»àËÄàâÙ(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ—Ω—Ö±}çΩ’π–Å•ÃÅπΩ–Å9ΩπîË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö—ïlâ—Ω—Ö±}çΩ’π–âtÄÙÅ—Ω—Ö±}çΩ’π–(ÄÄÄÄÄÄÄÄÄÄÄÅëàπï·ïç’—î†ààà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ%9MIPÅ%9Q<Å›ïëΩô}ÕÂπç}Õ—Ö—î°ÕÂπç}≠ï‰∞ÅŸÖ±’ï}©ÕΩ∏∞Å’¡ëÖ—ïë}Ö–§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅY1ULÄ†ù…ïù•Õ—…Ö—•ΩπΩ±ëï…Ãú∞Ä¸∞Ä¸§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ=8Å=91%P°ÕÂπç}≠ï‰§Å<ÅUAQÅMP(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅŸÖ±’ï}©ÕΩ∏ıï·ç±’ëïêπŸÖ±’ï}©ÕΩ∏∞Å’¡ëÖ—ïë}Ö–ıï·ç±’ëïêπ’¡ëÖ—ïë}Ö–(ÄÄÄÄÄÄÄÄÄÄÄÄààà∞Ä°©ÕΩ∏πë’µ¡Ã°Õ—Ö—î§∞ÅπΩ‹§§(ÄÄÄÄÄÄÄÅ•òÅç…µ}ç°ÖπùïêË(ÄÄÄÄÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄâç…ïÖ—ïë}çΩπ—Öç—ÃàËÅç…ïÖ—ïë}çΩπ—Öç—Ã∞(ÄÄÄÄÄÄÄÄâ±•π≠ïë}ôΩ±ëï…ÃàËÅ±•π≠ïë}ôΩ±ëï…Ã∞(ÄÄÄÄÄÄÄÄâ¡ïπë•πù}…ïŸ•ï›ÃàËÅ¡ïπë•πù}…ïŸ•ï›Ã∞(ÄÄÄÅÙ(()ëïòÅ}›ïëΩô}Õï—}Õ—Ö—î†®©Õ—Ö—î§Ë(ÄÄÄÅπΩ‹ÄÙÅ}›ïëΩô}πΩ‹†§(ÄÄÄÅ›•—†Å}›ïëΩô}çΩππïç–†§ÅÖÃÅëàË(ÄÄÄÄÄÄÄÅëàπï·ïç’—î†ààà(ÄÄÄÄÄÄÄÄÄÄÄÅ%9MIPÅ%9Q<Å›ïëΩô}ÕÂπç}Õ—Ö—î°ÕÂπç}≠ï‰∞ÅŸÖ±’ï}©ÕΩ∏∞Å’¡ëÖ—ïë}Ö–§(ÄÄÄÄÄÄÄÄÄÄÄÅY1ULÄ†ù…ïù•Õ—…Ö—•ΩπΩ±ëï…Ãú∞Ä¸∞Ä¸§(ÄÄÄÄÄÄÄÄÄÄÄÅ=8Å=91%P°ÕÂπç}≠ï‰§Å<ÅUAQÅMP(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅŸÖ±’ï}©ÕΩ∏ıï·ç±’ëïêπŸÖ±’ï}©ÕΩ∏∞Å’¡ëÖ—ïë}Ö–ıï·ç±’ëïêπ’¡ëÖ—ïë}Ö–(ÄÄÄÄÄÄÄÄààà∞Ä°©ÕΩ∏πë’µ¡Ã°Õ—Ö—î§∞ÅπΩ‹§§(()ëïòÅ}›ïëΩô}Õ—Ö—î†§Ë(ÄÄÄÅ›•—†Å}›ïëΩô}çΩππïç–†§ÅÖÃÅëàË(ÄÄÄÄÄÄÄÅ…Ω‹ÄÙÅëàπï·ïç’—î†âM1PÅŸÖ±’ï}©ÕΩ∏∞Å’¡ëÖ—ïë}Ö–ÅI=4Å›ïëΩô}ÕÂπç}Õ—Ö—îÅ]!IÅÕÂπç}≠ï‰Ùù…ïù•Õ—…Ö—•ΩπΩ±ëï…Ãúà§πôï—ç°Ωπî†§(ÄÄÄÅ•òÅπΩ–Å…Ω‹Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÌÙ(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏ®©©ÕΩ∏π±ΩÖëÃ°…Ω›lâŸÖ±’ï}©ÕΩ∏ât§∞Äâ’¡ëÖ—ïë}Ö–àËÅ…Ω›lâ’¡ëÖ—ïë}Ö–âuÙ(ÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏâ±ÖÕ—}ï……Ω»àËÄã%—Ö–ÅëîÅÕÂπç°…Ωπ•ÕÖ—•Ω∏Å•±±•Õ•â±î∏âÙ(()ëïòÅ}›ïëΩô}ÕÂπå†®∞Å¡Öùï}â’ëùï–ı9Ωπî§Ë(ÄÄÄÄààâ„•ç’—îÅ’πîÅÕï’±îÅÀ•çΩπç•±•Ö—•Ω∏Åù±ΩâÖ±î∞Å—Ω’ÃÅ¡…ΩçïÕÕ’ÃΩÖ¡¡ÃÅçΩπôΩπë’Ã∏ààà(ÄÄÄÅ•òÅπΩ–Å}]=}Me9}1=,πÖç≈’•…î°â±Ωç≠•πúıÖ±Õî§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâΩ¨àËÅQ…’î∞Äâ•π}¡…Ωù…ïÕÃàËÅQ…’î∞Äâ¡…ΩçïÕÕïêàËÄ¿∞(ÄÄÄÄÄÄÄÄÄÄÄÄâç…ïÖ—ïë}çΩπ—Öç—ÃàËÄ¿∞Äâ±•π≠ïë}ôΩ±ëï…ÃàËÄ¿∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡ïπë•πù}…ïŸ•ï›ÃàËÄ¿∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÅ±ïÖÕîÄÙÅÌÙ(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ±ïÖÕîÄÙÅÖç≈’•…ï}›ïëΩô}±Ωç¨†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ›ïëΩòµù±ΩâÖ∞µ…ïçΩπç•±•Ö—•Ω∏à∞Å——±}ÕïçΩπëÃÙÃÿ¿¿∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅï·çï¡–Å]ïëΩôΩŸï…πΩ………Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅ]ïëΩôA%……Ω»°Õ—»°ï·å§∞Ä‘¿Ã§Åô…Ω¥Åï·å(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å±ïÖÕîπùï–†âÖç≈’•…ïêà∞ÅÖ±Õî§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâΩ¨àËÅQ…’î∞Äâ•π}¡…Ωù…ïÕÃàËÅQ…’î∞Äâ¡…ΩçïÕÕïêàËÄ¿∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâç…ïÖ—ïë}çΩπ—Öç—ÃàËÄ¿∞Äâ±•π≠ïë}ôΩ±ëï…ÃàËÄ¿∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ¡ïπë•πù}…ïŸ•ï›ÃàËÄ¿∞(ÄÄÄÄÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÅ…ï—’…∏Å}›ïëΩô}ÕÂπç}±Ωç≠ïê°¡Öùï}â’ëùï–ı¡Öùï}â’ëùï–§(ÄÄÄÅô•πÖ±±‰Ë(ÄÄÄÄÄÄÄÅ…ï±ïÖÕï}›ïëΩô}±Ωç¨†(ÄÄÄÄÄÄÄÄÄÄÄÄâ›ïëΩòµù±ΩâÖ∞µ…ïçΩπç•±•Ö—•Ω∏à∞ÅÕ—»°±ïÖÕîπùï–†â—Ω≠ï∏à§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ}]=}Me9}1=,π…ï±ïÖÕî†§(()ëïòÅ}›ïëΩô}ÕÂπç}±Ωç≠ïê†®∞Å¡Öùï}â’ëùï–ı9Ωπî§Ë(ÄÄÄÅÕ—Ö—îÄÙÅ}›ïëΩô}Õ—Ö—î†§(ÄÄÄÅ¡ÖùîÄÙÅ•π–°Õ—Ö—îπùï–†âπï·—}¡Öùîà§ÅΩ»Äƒ§Å•òÅÕ—Ö—îπùï–†â•π}¡…Ωù…ïÕÃà§Åï±ÕîÄƒ(ÄÄÄÅµÖ·}¡ÖùïÃÄÙÅµÖ‡†ƒ∞Åµ•∏°•π–°ΩÃπùï—ïπÿ†â]=}5a}ALà∞Äàƒ¿¿¿à§§∞Äƒ¿¿¿¿§§(ÄÄÄÅ•òÅ¡Öùï}â’ëùï–Å•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅ¡ÖùïÕ}—°•Õ}…’∏ÄÙÅµÖ·}¡ÖùïÃ(ÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖùïÕ}—°•Õ}…’∏ÄÙÅµÖ‡†ƒ∞Åµ•∏°•π–°¡Öùï}â’ëùï–§∞ÅµÖ·}¡ÖùïÃ§§(ÄÄÄÄÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖùïÕ}—°•Õ}…’∏ÄÙÄƒ(ÄÄÄÅ¡…ΩçïÕÕïêÄÙÄ¿(ÄÄÄÅç…ïÖ—ïë}çΩπ—Öç—ÃÄÙÄ¿(ÄÄÄÅ±•π≠ïë}ôΩ±ëï…ÃÄÙÄ¿(ÄÄÄÅ¡ïπë•πù}…ïŸ•ï›ÃÄÙÄ¿(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅôΩ»Å|Å•∏Å…Öπùî°¡ÖùïÕ}—°•Õ}…’∏§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖê∞Å°ïÖëï…ÃÄÙÅ}›ïëΩô}…ï≈’ïÕ–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄàΩÖ¡§Ω…ïù•Õ—…Ö—•ΩπΩ±ëï…Ãà∞Å¡Ö…ÖµÃıÏâ±•µ•–àËÄƒ¿¿∞Äâ¡ÖùîàËÅ¡ÖùïÙ(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅ•—ïµÃÄÙÅ}›ïëΩô}•—ïµÃ°¡ÖÂ±ΩÖê§(ÄÄÄÄÄÄÄÄÄÄÄÅ—Ω—Ö∞ÄÙÅ°ïÖëï…Ãπùï–†â‡µ—Ω—Ö∞µçΩ’π–à§ÅΩ»Å°ïÖëï…Ãπùï–†â`µQΩ—Ö∞µΩ’π–à§(ÄÄÄÄÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—Ω—Ö∞ÄÙÅ•π–°—Ω—Ö∞§Å•òÅ—Ω—Ö∞Å•ÃÅπΩ–Å9ΩπîÅï±ÕîÅ9Ωπî(ÄÄÄÄÄÄÄÄÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—Ω—Ö∞ÄÙÅ9Ωπî(ÄÄÄÄÄÄÄÄÄÄÄÄåÅIïç°Ö…ùîÅ±îÅô•ç°•ï»ÅÖ¡À°ÃÅ∞ùÖ¡¡ï∞ÅÀ•ÕïÖ‘ÄËÅ’πîÅÕÖ•Õ•îÅÀ•Ö±•œ•î(ÄÄÄÄÄÄÄÄÄÄÄÄåÅ¡ïπëÖπ–Å±ÑÅ¡Öù•πÖ—•Ω∏ÅπîÅëΩ•–Å©ÖµÖ•ÃÉ©—…îÉ•ç…Öœ•îÅ¡Ö»Å’∏ÅÖπç•ï∏ÅÕπÖ¡Õ°Ω–∏(ÄÄÄÄÄÄÄÄÄÄÄÅ¡Öùï}…ïÕ’±–ÄÙÅ}›ïëΩô}Õ—Ω…ï}¡Öùî°•—ïµÃ∞Å9Ωπî∞Å¡Öùî∞Å—Ω—Ö∞§(ÄÄÄÄÄÄÄÄÄÄÄÅ¡…ΩçïÕÕïêÄ¨ÙÅ±ï∏°•—ïµÃ§(ÄÄÄÄÄÄÄÄÄÄÄÅç…ïÖ—ïë}çΩπ—Öç—ÃÄ¨ÙÅ¡Öùï}…ïÕ’±—lâç…ïÖ—ïë}çΩπ—Öç—Ãât(ÄÄÄÄÄÄÄÄÄÄÄÅ±•π≠ïë}ôΩ±ëï…ÃÄ¨ÙÅ¡Öùï}…ïÕ’±—lâ±•π≠ïë}ôΩ±ëï…Ãât(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ïπë•πù}…ïŸ•ï›ÃÄ¨ÙÅ¡Öùï}…ïÕ’±—lâ¡ïπë•πù}…ïŸ•ï›Ãât(ÄÄÄÄÄÄÄÄÄÄÄÅç’……ïπ–ÄÙÅ°ïÖëï…Ãπùï–†â‡µç’……ïπ–µ¡Öùîà§ÅΩ»Å°ïÖëï…Ãπùï–†â`µ’……ïπ–µAÖùîà§ÅΩ»Å¡Öùî(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ï…}¡ÖùîÄÙÅ°ïÖëï…Ãπùï–†â‡µ•—ï¥µ¡ï»µ¡Öùîà§ÅΩ»Å°ïÖëï…Ãπùï–†â`µ%—ï¥µAï»µAÖùîà§ÅΩ»Äƒ¿¿(ÄÄÄÄÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩµ¡±ï—îÄÙÅ—Ω—Ö∞Å•ÃÅπΩ–Å9ΩπîÅÖπêÅ•π–°ç’……ïπ–§Ä®Å•π–°¡ï…}¡Öùî§Ä¯ÙÅ—Ω—Ö∞(ÄÄÄÄÄÄÄÄÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩµ¡±ï—îÄÙÅÖ±Õî(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅçΩµ¡±ï—îÅΩ»Å±ï∏°•—ïµÃ§ÄÄƒ¿¿Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅô•π•Õ°ïêÄÙÅ}›ïëΩô}πΩ‹†§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}›ïëΩô}Õï—}Õ—Ö—î°πï·—}¡ÖùîÙƒ∞Å•π}¡…Ωù…ïÕÃıÖ±Õî∞Å±ÖÕ—}ï……Ω»Ùàà∞Å±ÖÕ—}ÕÂπç}Ö–ıô•π•Õ°ïê§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâΩ¨àËÅQ…’î∞Äâ¡…ΩçïÕÕïêàËÅ¡…ΩçïÕÕïê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâç…ïÖ—ïë}çΩπ—Öç—ÃàËÅç…ïÖ—ïë}çΩπ—Öç—Ã∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ±•π≠ïë}ôΩ±ëï…ÃàËÅ±•π≠ïë}ôΩ±ëï…Ã∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ¡ïπë•πù}…ïŸ•ï›ÃàËÅ¡ïπë•πù}…ïŸ•ï›Ã∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ±ÖÕ—}ÕÂπç}Ö–àËÅô•π•Õ°ïê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖùîÄ¨ÙÄƒ(ÄÄÄÄÄÄÄÄåÅ1ÑÅÀ•çΩπç•±•Ö—•Ω∏ÅÖ’—ΩµÖ—•≈’îÅÖŸÖπçîÅëÖπÃÅ±ÑÅ¡Öù•πÖ—•Ω∏ÅÖŸïåÅ’∏Å¡ï—•–(ÄÄÄÄÄÄÄÄåÅâ’ëùï–Åô•·î∏Å1îÅç’…Õï’»ÅïÕ–ÅçΩπÕï…€§ÅÖô•∏ÅëîÅçΩ’Ÿ…•»Å¡…Ωù…ïÕÕ•Ÿïµïπ–(ÄÄÄÄÄÄÄÄåÅ∞ù°•Õ—Ω…•≈’îÅÕÖπÃÅ…ïôÖ•…îÅ’∏ÅÕçÖ∏ÅçΩµ¡±ï–Å≈’Ö—…îÅôΩ•ÃÅ¡Ö»Å©Ω’»∏(ÄÄÄÄÄÄÄÅ}›ïëΩô}Õï—}Õ—Ö—î†(ÄÄÄÄÄÄÄÄÄÄÄÅπï·—}¡Öùîı¡Öùî∞Å•π}¡…Ωù…ïÕÃıQ…’î∞Å±ÖÕ—}ï……Ω»Ùàà∞(ÄÄÄÄÄÄÄÄÄÄÄÅ±ÖÕ—}¡Ö…—•Ö±}ÕÂπç}Ö–ı}›ïëΩô}πΩ‹†§∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâΩ¨àËÅQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡Ö…—•Ö∞àËÅQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ•π}¡…Ωù…ïÕÃàËÅQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄâπï·—}¡ÖùîàËÅ¡Öùî∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡…ΩçïÕÕïêàËÅ¡…ΩçïÕÕïê∞(ÄÄÄÄÄÄÄÄÄÄÄÄâç…ïÖ—ïë}çΩπ—Öç—ÃàËÅç…ïÖ—ïë}çΩπ—Öç—Ã∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ±•π≠ïë}ôΩ±ëï…ÃàËÅ±•π≠ïë}ôΩ±ëï…Ã∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡ïπë•πù}…ïŸ•ï›ÃàËÅ¡ïπë•πù}…ïŸ•ï›Ã∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÅï·çï¡–Å·çï¡—•Ω∏ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅµïÕÕÖùîÄÙÅ}›ïëΩô}ç±ïÖ∏°ï·å§(ÄÄÄÄÄÄÄÅ}›ïëΩô}Õï—}Õ—Ö—î°πï·—}¡Öùîı¡Öùî∞Å•π}¡…Ωù…ïÕÃıQ…’î∞Å±ÖÕ—}ï……Ω»ıµïÕÕÖùî§(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅ]ïëΩôA%……Ω»°µïÕÕÖùî§(()ëïòÅ}›ïëΩô}¡ΩÕ•—•Ÿï}•π—ï…ŸÖ∞°πÖµî∞ÅëïôÖ’±–∞Åµ•π•µ’¥§Ë(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅµÖ‡°µ•π•µ’¥∞Å•π–°ΩÃπùï—ïπÿ°πÖµî∞ÅÕ—»°ëïôÖ’±–§§§§(ÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅëïôÖ’±–(()ëïòÅ}›ïëΩô}ô—}›Ö—ç°±•Õ–°ëÖ—Ñı9Ωπî§Ë(ÄÄÄÄààâIï—Ω’…πîÅ±ïÃÅëΩÕÕ•ï…ÃÅ±ïÃÅ¡±’ÃÅÀ•çïπ—ÃÅïπçΩ…îÅï∏Å•πÕ—…’ç—•Ω∏ÅP∏((ÄÄÄÅMï’±ÃÅ±ïÃÅëΩÕÕ•ï…ÃÅì•´ÄÅ±ß•ÃÉÄÅ’πîÅô•ç°îÅI4ÅÕΩπ–ÅçΩπçï…ª•Ã∏Å1îÅ—…§Å¡Ö»(ÄÄÄÅÖπç•ïππîÅëÖ—îÅëîÅÕÂπç°…Ωπ•ÕÖ—•Ω∏ÅôÖ•–Å—Ω’…πï»É•≈’•—Öâ±ïµïπ–Å±ÑÅ±•Õ—îÅÕ§Å±î(ÄÄÄÅ¡±ÖôΩπêÅ¡Ö»Å¡ÖÕÕÖùîÅïÕ–ÅÖ——ï•π–∏(ÄÄÄÄààà(ÄÄÄÅëÖ—ÑÄÙÅëÖ—ÑÅΩ»Å±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅçΩπ—Öç—ÃÄÙÅÏ(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†â•êà§ÅΩ»Äàà§ËÅçΩπ—Öç–(ÄÄÄÄÄÄÄÅôΩ»ÅçΩπ—Öç–Å•∏ÅëÖ—Ñπùï–†âç…µ}çΩπ—Öç—Ãà∞Åmt§(ÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°çΩπ—Öç–∞Åë•ç–§ÅÖπêÅçΩπ—Öç–πùï–†â•êà§(ÄÄÄÅÙ(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç—ÃÅΩ»ÅπΩ–ÅΩÃπ¡Ö—†πï·•Õ—Ã°}›ïëΩô}ëâ}¡Ö—††§§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Åmt(ÄÄÄÅ±Ö—ïÕ—}âÂ}çΩπ—Öç–ÄÙÅÌÙ(ÄÄÄÅ›•—†Å}›ïëΩô}çΩππïç–†§ÅÖÃÅëàË(ÄÄÄÄÄÄÄÅ…Ω›ÃÄÙÅëàπï·ïç’—î†ààà(ÄÄÄÄÄÄÄÄÄÄÄÅM1PÅ»πÕ—Öâ±ï}•ê∞Å»π¡ÖÂ±ΩÖë}©ÕΩ∏∞Å»π…ïµΩ—ï}ëÖ—î∞Å»πÕÂπçïë}Ö–∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ∞πçΩπ—Öç—}•ê(ÄÄÄÄÄÄÄÄÄÄÄÅI=4Å›ïëΩô}…ïÕΩ’…çïÃÅ»Å)=%8Å›ïëΩô}çΩπ—Öç—}±•π≠ÃÅ∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÅ=8Å»π…ïÕΩ’…çï}—Â¡îı∞π…ïÕΩ’…çï}—Â¡î(ÄÄÄÄÄÄÄÄÄÄÄÄÅ9Å»πÕ—Öâ±ï}•êı∞π…ïÕΩ’…çï}•ê(ÄÄÄÄÄÄÄÄÄÄÄÅ]!IÅ»π…ïÕΩ’…çï}—Â¡îÙù…ïù•Õ—…Ö—•ΩπΩ±ëï»ú(ÄÄÄÄÄÄÄÄààà§(ÄÄÄÄÄÄÄÅôΩ»Å…Ω‹Å•∏Å…Ω›ÃË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—}•êÄÙÅÕ—»°…Ω›lâçΩπ—Öç—}•êâtÅΩ»Äàà§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç—}•êÅπΩ–Å•∏ÅçΩπ—Öç—ÃË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ©ÕΩ∏π±ΩÖëÃ°…Ω›lâ¡ÖÂ±ΩÖë}©ÕΩ∏ât§(ÄÄÄÄÄÄÄÄÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»∞Å©ÕΩ∏π)M=9ïçΩëï……Ω»§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïçïπç‰ÄÙÅ}›ïëΩô}ôΩ±ëï…}…ïçïπçÂ}≠ï‰†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅôÖ±±âÖç¨ı…Ω›lâ…ïµΩ—ï}ëÖ—îâtÅΩ»Å…Ω›lâÕÂπçïë}Ö–ât∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—Öâ±ï}•êı…Ω›lâÕ—Öâ±ï}•êât∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅ¡…ïŸ•Ω’ÃÄÙÅ±Ö—ïÕ—}âÂ}çΩπ—Öç–πùï–°çΩπ—Öç—}•ê§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ¡…ïŸ•Ω’ÃÅ•ÃÅ9ΩπîÅΩ»Å…ïçïπç‰Ä¯Å¡…ïŸ•Ω’Õl¡tË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ±Ö—ïÕ—}âÂ}çΩπ—Öç—mçΩπ—Öç—}•ëtÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ïçïπç‰∞ÅÕ—»°…Ω›lâÕ—Öâ±ï}•êât§∞Å¡ÖÂ±ΩÖê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—»°…Ω›lâÕÂπçïë}Ö–âtÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§((ÄÄÄÅ›Ö—ç°±•Õ–ÄÙÅmt(ÄÄÄÅôΩ»Ä†(ÄÄÄÄÄÄÄÅçΩπ—Öç—}•ê∞Ä°}…ïçïπç‰∞ÅÕ—Öâ±ï}•ê∞Å¡ÖÂ±ΩÖê∞ÅÕÂπçïë}Ö–§(ÄÄÄÄ§Å•∏Å±Ö—ïÕ—}âÂ}çΩπ—Öç–π•—ïµÃ†§Ë(ÄÄÄÄÄÄÄÅÕ—Ö—’ÃÄÙÅ}›ïëΩô}ô…Öπçï}—…ÖŸÖ•±}Õ—Ö—’Ã°¡ÖÂ±ΩÖê§(ÄÄÄÄÄÄÄÅ•òÅÕ—Ö—’ÃÄÙÙÄâïπ}çΩ’…Õ}•πÕ—…’ç—•Ω∏àË(ÄÄÄÄÄÄÄÄÄÄÄÅ›Ö—ç°±•Õ–πÖ¡¡ïπê°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—Öç—}•êàËÅçΩπ—Öç—}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâôΩ±ëï…}•êàËÅÕ—Öâ±ï}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÕÂπçïë}Ö–àËÅÕÂπçïë}Ö–∞(ÄÄÄÄÄÄÄÄÄÄÄÅÙ§(ÄÄÄÅ…ï—’…∏ÅÕΩ…—ïê†(ÄÄÄÄÄÄÄÅ›Ö—ç°±•Õ–∞(ÄÄÄÄÄÄÄÅ≠ï‰ı±ÖµâëÑÅ•—ï¥ËÄ°•—ï¥πùï–†âÕÂπçïë}Ö–à§ÅΩ»Äàà∞Å•—ïµlâôΩ±ëï…}•êât§∞(ÄÄÄÄ§(()ëïòÅ}›ïëΩô}…ïçΩπç•±ï}ô—}›Ö—ç°±•Õ–†®∞ÅµÖ·}ôΩ±ëï…Ãı9Ωπî§Ë(ÄÄÄÄààâIï±•–Å’π•≈’ïµïπ–Å±ïÃÅëΩÕÕ•ï…ÃÅçΩππ’ÃÅïπçΩ…îÅï∏Å•πÕ—…’ç—•Ω∏Å…ÖπçîÅQ…ÖŸÖ•∞∏ààà(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅçΩπô•ù’…ïë}±•µ•–ÄÙÅ•π–°ΩÃπùï—ïπÿ†(ÄÄÄÄÄÄÄÄÄÄÄÄâ]=}Q}I=9%1%Q%=9}5a}=1ILà∞Äà‘¿à∞(ÄÄÄÄÄÄÄÄ§§(ÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»§Ë(ÄÄÄÄÄÄÄÅçΩπô•ù’…ïë}±•µ•–ÄÙÄ‘¿(ÄÄÄÅ•òÅµÖ·}ôΩ±ëï…ÃÅ•ÃÅπΩ–Å9ΩπîË(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπô•ù’…ïë}±•µ•–ÄÙÅ•π–°µÖ·}ôΩ±ëï…Ã§(ÄÄÄÄÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπô•ù’…ïë}±•µ•–ÄÙÄƒ(ÄÄÄÅçΩπô•ù’…ïë}±•µ•–ÄÙÅµÖ‡†ƒ∞Åµ•∏°çΩπô•ù’…ïë}±•µ•–∞Äƒ¿¿§§(ÄÄÄÅ›Ö—ç°±•Õ–ÄÙÅ}›ïëΩô}ô—}›Ö—ç°±•Õ–†§(ÄÄÄÅç°ïç≠ïêÄÙÄ¿(ÄÄÄÅ…ïô’ÕïêÄÙÄ¿(ÄÄÄÅï……Ω…ÃÄÙÄ¿(ÄÄÄÅôΩ»Å•—ï¥Å•∏Å›Ö—ç°±•Õ—lÈçΩπô•ù’…ïë}±•µ•—tË(ÄÄÄÄÄÄÄÅôΩ±ëï…}•êÄÙÅ•—ïµlâôΩ±ëï…}•êât(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖê∞Å}°ïÖëï…ÃÄÙÅ}›ïëΩô}…ï≈’ïÕ–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòàΩÖ¡§Ω…ïù•Õ—…Ö—•ΩπΩ±ëï…ÃΩÌ≈’Ω—î°ôΩ±ëï…}•ê∞ÅÕÖôîÙúú•Ùà(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ±ëï»ÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêπùï–†âëÖ—Ñà§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°¡ÖÂ±ΩÖê∞Åë•ç–§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅ•Õ•πÕ—Öπçî°¡ÖÂ±ΩÖêπùï–†âëÖ—Ñà§∞Åë•ç–§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅï±ÕîÅ¡ÖÂ±ΩÖê(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°ôΩ±ëï»∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅ]ïëΩôA%……Ω»†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ]=ÅÑÅ…ï—Ω’…ª§Å’∏ÅëΩÕÕ•ï»ÅëÖπÃÅ’∏ÅôΩ…µÖ–Å•πÖ——ïπë‘∏à(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…πïë}•êÄÙÅÕ—»°ôΩ±ëï»πùï–†âï·—ï…πÖ±%êà§ÅΩ»ÅôΩ±ëï…}•ê§πÕ—…•¿†§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ…ï—’…πïë}•êÄÑÙÅôΩ±ëï…}•êË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅ]ïëΩôA%……Ω»†â]=ÅÑÅ…ï—Ω’…ª§Å’∏ÅÖ’—…îÅπ’∑•…ºÅëîÅëΩÕÕ•ï»∏à§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–ÅôΩ±ëï»πùï–†âï·—ï…πÖ±%êà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅôΩ±ëï»ÄÙÅÏ®©ôΩ±ëï»∞Äâï·—ï…πÖ±%êàËÅôΩ±ëï…}•ëÙ(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÄ°}›ïëΩô}ô…Öπçï}—…ÖŸÖ•±}Õ—Ö—’Ã†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅôΩ±ëï»∞Å¡…ïŸ•Ω’Õ}Õ—Ö—’ÃÙâïπ}çΩ’…Õ}•πÕ—…’ç—•Ω∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§ÄÙÙÄâ…ïô’Õïîà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ïô’ÕïêÄ¨ÙÄƒ(ÄÄÄÄÄÄÄÄÄÄÄÅ}›ïëΩô}Õ—Ω…ï}¡Öùî†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅmôΩ±ëï…t∞Å9Ωπî∞Ä¿∞Å’¡ëÖ—ï}ÕÂπç}Õ—Ö—îıÖ±Õî∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅç°ïç≠ïêÄ¨ÙÄƒ(ÄÄÄÄÄÄÄÅï·çï¡–Å]ïëΩôA%……Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÅï……Ω…ÃÄ¨ÙÄƒ(ÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¿π±Ωùùï»π›Ö…π•πú†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ›ïëΩòÅPÅ…ïçΩπç•±•Ö—•Ω∏ÅôÖ•±ïêÅôΩ±ëï»ÙïÃÅï……Ω»ÙïÃà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅôΩ±ëï…}•ê∞Å}›ïëΩô}ç±ïÖ∏°ï·å§∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅùï—Ö——»°ï·å∞ÄâÕ—Ö—’Õ}çΩëîà∞Å9Ωπî§Å•∏ÅÏ–»‰∞Ä‘¿ÕÙË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅâ…ïÖ¨(ÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄâΩ¨àËÅï……Ω…ÃÄÙÙÄ¿∞(ÄÄÄÄÄÄÄÄâçÖπë•ëÖ—ïÃàËÅ±ï∏°›Ö—ç°±•Õ–§∞(ÄÄÄÄÄÄÄÄâç°ïç≠ïêàËÅç°ïç≠ïê∞(ÄÄÄÄÄÄÄÄâ…ïô’ÕïêàËÅ…ïô’Õïê∞(ÄÄÄÄÄÄÄÄâï……Ω…ÃàËÅï……Ω…Ã∞(ÄÄÄÄÄÄÄÄâ…ïµÖ•π•πúàËÅµÖ‡†¿∞Å±ï∏°›Ö—ç°±•Õ–§Ä¥Åç°ïç≠ïê§∞(ÄÄÄÅÙ(()ëïòÅ}›ïëΩô}Õç°ïë’±ïë}…ïçΩπç•±•Ö—•Ω∏†§Ë(ÄÄÄÄààâ„•ç’—îÅ±îÅçΩπ—À—±îÅPÅ¡…•Ω…•—Ö•…îÅ¡’•ÃÅ≈’ï±≈’ïÃÅ¡ÖùïÃÅù±ΩâÖ±ïÃ∏ààà(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ¡Öùï}â’ëùï–ÄÙÅ•π–°ΩÃπùï—ïπÿ†(ÄÄÄÄÄÄÄÄÄÄÄÄâ]=}I=9%1%Q%=9}A}	UPà∞Äà‘à∞(ÄÄÄÄÄÄÄÄ§§(ÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»§Ë(ÄÄÄÄÄÄÄÅ¡Öùï}â’ëùï–ÄÙÄ‘(ÄÄÄÅ¡Öùï}â’ëùï–ÄÙÅµÖ‡†ƒ∞Åµ•∏°¡Öùï}â’ëùï–∞Ä»¿§§(ÄÄÄÅô—}…ïÕ’±–ÄÙÅ}›ïëΩô}…ïçΩπç•±ï}ô—}›Ö—ç°±•Õ–†§(ÄÄÄÅù±ΩâÖ±}…ïÕ’±–ÄÙÅ}›ïëΩô}ÕÂπå°¡Öùï}â’ëùï–ı¡Öùï}â’ëùï–§(ÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄâΩ¨àËÅâΩΩ∞°ô—}…ïÕ’±–πùï–†âΩ¨à§ÅÖπêÅù±ΩâÖ±}…ïÕ’±–πùï–†âΩ¨à§§∞(ÄÄÄÄÄÄÄÄâô…Öπçï}—…ÖŸÖ•∞àËÅô—}…ïÕ’±–∞(ÄÄÄÄÄÄÄÄâù±ΩâÖ∞àËÅù±ΩâÖ±}…ïÕ’±–∞(ÄÄÄÅÙ(()ëïòÅ}›ïëΩô}âÖç≠ù…Ω’πë}ÕÂπç}±ΩΩ¿†§Ë(ÄÄÄÅ•π•—•Ö±}ëï±Ö‰ÄÙÅ}›ïëΩô}¡ΩÕ•—•Ÿï}•π—ï…ŸÖ∞†(ÄÄÄÄÄÄÄÄâ]=}Me9}%9%Q%1}1e}M=9Là∞ÄÃ¿¿∞Äÿ¿∞(ÄÄÄÄ§(ÄÄÄÅ•π—ï…ŸÖ∞ÄÙÅ}›ïëΩô}¡ΩÕ•—•Ÿï}•π—ï…ŸÖ∞†(ÄÄÄÄÄÄÄÄâ]=}I=9%1%Q%=9}%9QIY1}M=9Là∞Ä»ƒÿ¿¿∞Ä»ƒÿ¿¿∞(ÄÄÄÄ§(ÄÄÄÅ•òÅ}]=}A=11I}MQ=@π›Ö•–°•π•—•Ö±}ëï±Ö‰§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏(ÄÄÄÅ›°•±îÅπΩ–Å}]=}A=11I}MQ=@π•Õ}Õï–†§Ë(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄåÅA±’Õ•ï’…ÃÅ›Ω…≠ï…ÃÅ›ïàÅ¡ï’Ÿïπ–Åì•µÖ……ï»ÅçîÅ—°…ïÖê∏ÅîÅâÖ•∞Å∏ùïÕ–(ÄÄÄÄÄÄÄÄÄÄÄÄåÅŸΩ±Ωπ—Ö•…ïµïπ–Å¡ÖÃÅ±•ã•À§ÄËÅÕΩ∏Åï·¡•…Ö—•Ω∏ÅµÖ”•…•Ö±•ÕîÅ±îÅì•±Ö§(ÄÄÄÄÄÄÄÄÄÄÄÄåÅëîÅÕ•‡Å°ï’…ïÃÅï–Åïµ√©ç°îÅ’∏ÅÕïçΩπêÅ›Ω…≠ï»ÅëîÅ…ïôÖ•…îÅ±îÅ¡ÖÕÕÖùî∏(ÄÄÄÄÄÄÄÄÄÄÄÅÕç°ïë’±îÄÙÅÖç≈’•…ï}›ïëΩô}±Ωç¨†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ›ïëΩòµç…¥µ…ïçΩπç•±•Ö—•Ω∏µÕç°ïë’±îà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ——±}ÕïçΩπëÃı•π—ï…ŸÖ∞∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–ÅÕç°ïë’±îπùï–†âÖç≈’•…ïêà∞ÅÖ±Õî§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕ’±–ÄÙÅÏâΩ¨àËÅQ…’î∞ÄâÕ—Ö—’ÃàËÄâÖ±…ïÖëÂ}Õç°ïë’±ïêâÙ(ÄÄÄÄÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕ’±–ÄÙÅ}›ïëΩô}Õç°ïë’±ïë}…ïçΩπç•±•Ö—•Ω∏†§(ÄÄÄÄÄÄÄÄÄÄÄÅç…ïÖ—ïêÄÙÅ…ïÕ’±–πùï–†âù±ΩâÖ∞à∞ÅÌÙ§πùï–†âç…ïÖ—ïë}çΩπ—Öç—Ãà∞Ä¿§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅç…ïÖ—ïêË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¿π±Ωùùï»π•πôº†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ›ïëΩòÅÖ’—ºµÕÂπåÅç…ïÖ—ïë}çΩπ—Öç—ÃÙïÃÅ¡…ΩçïÕÕïêÙïÃà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅç…ïÖ—ïê∞Å…ïÕ’±–πùï–†âù±ΩâÖ∞à∞ÅÌÙ§πùï–†â¡…ΩçïÕÕïêà∞Ä¿§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅï·çï¡–Å·çï¡—•Ω∏ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¿π±Ωùùï»π›Ö…π•πú†â›ïëΩòÅÖ’—ºµÕÂπåÅôÖ•±ïêËÄïÃà∞Å}›ïëΩô}ç±ïÖ∏°ï·å§§(ÄÄÄÄÄÄÄÅ•òÅ}]=}A=11I}MQ=@π›Ö•–°•π—ï…ŸÖ∞§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏(()ëïòÅ}Õ—Ö…—}›ïëΩô}âÖç≠ù…Ω’πë}ÕÂπå†§Ë(ÄÄÄÄààâK•çΩπç•±•Ö—•Ω∏Åù±ΩâÖ±îÅΩ¡—•Ωππï±±î∞Åì•ÕÖç—•€•îÅ¡Ö»Åì•ôÖ’–Åï–ÅÖ‘Å¡±’ÃÄ–ÅôΩ•ÃΩ©Ω’»∏ààà(ÄÄÄÅù±ΩâÖ∞Å}]=}A=11I}MQIQ(ÄÄÄÄåÅ9Ω’ŸïÖ‘Åë…Ö¡ïÖ‘ÅŸΩ±Ωπ—Ö•…îÄËÅ’πîÅÖπç•ïππîÅçΩπô•ù’…Ö—•Ω∏(ÄÄÄÄåÅ]=}UQ=}Me9}9	1ı—…’îÅπîÅëΩ•–Å©ÖµÖ•ÃÅ…ïÕÕ’Õç•—ï»Å±îÅ¡Ω±±ï»Ä‘Åµ•∏∏(ÄÄÄÅïπÖâ±ïêÄÙÅÕ—»°ΩÃπùï—ïπÿ†(ÄÄÄÄÄÄÄÄâ]=}I5}I=9%1%Q%=9}9	1à∞Äâ—…’îà∞(ÄÄÄÄ§§πÕ—…•¿†§πçÖÕïôΩ±ê†§(ÄÄÄÅ•òÄ°}]=}A=11I}MQIQÅΩ»ÅπΩ–ÅΩÃπùï—ïπÿ†â]=}A%}-dà∞Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÄÄÄÄÅΩ»ÅïπÖâ±ïêÅ•∏ÅÏà¿à∞ÄâôÖ±Õîà∞ÄâπΩ∏à∞Äâπºà∞ÄâΩôòâÙ§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅ}]=}A=11I}MQIQÄÙÅQ…’î(ÄÄÄÅ—°…ïÖë•πúπQ°…ïÖê†(ÄÄÄÄÄÄÄÅ—Ö…ùï–ı}›ïëΩô}âÖç≠ù…Ω’πë}ÕÂπç}±ΩΩ¿∞(ÄÄÄÄÄÄÄÅπÖµîÙâ›ïëΩòµç…¥µ…ïçΩπç•±•Ö—•Ω∏à∞ÅëÖïµΩ∏ıQ…’î∞(ÄÄÄÄ§πÕ—Ö…–†§(ÄÄÄÅ…ï—’…∏ÅQ…’î(()ëïòÅ}›ïëΩô}çΩπ—Öç—}…ïÕΩ’…çïÃ°çΩπ—Öç—}•ê∞ÅëÖ—Ñı9Ωπî§Ë(ÄÄÄÅëÖ—ÑÄÙÅëÖ—ÑÅΩ»Å±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Åmt((ÄÄÄÅçΩπ—Öç—}πÖµîÄÙÄ†(ÄÄÄÄÄÄÄÅ}›ïëΩô}πΩ…µÖ±•Èï}πÖµî°çΩπ—Öç–πùï–†â¡…ïπΩ¥à§§∞(ÄÄÄÄÄÄÄÅ}›ïëΩô}πΩ…µÖ±•Èï}πÖµî°çΩπ—Öç–πùï–†âπΩ¥à§§∞(ÄÄÄÄ§(ÄÄÄÅÕÖµï}πÖµï}çΩ’π–ÄÙÅÕ’¥†(ÄÄÄÄÄÄÄÄƒÅôΩ»ÅçÖπë•ëÖ—îÅ•∏ÅëÖ—Ñπùï–†âç…µ}çΩπ—Öç—Ãà∞Åmt§(ÄÄÄÄÄÄÄÅ•òÅÖ±∞°çΩπ—Öç—}πÖµî§ÅÖπêÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅ}›ïëΩô}πΩ…µÖ±•Èï}πÖµî°çÖπë•ëÖ—îπùï–†â¡…ïπΩ¥à§§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ}›ïëΩô}πΩ…µÖ±•Èï}πÖµî°çÖπë•ëÖ—îπùï–†âπΩ¥à§§∞(ÄÄÄÄÄÄÄÄ§ÄÙÙÅçΩπ—Öç—}πÖµî(ÄÄÄÄ§((ÄÄÄÄåÅÖÃÅçΩ’…Öπ–ÄËÅ±îÅëΩÕÕ•ï»ÅïÕ–Åì•´ÄÅ…Ö——Öç£§ÉÄÅçï——îÅ¡•Õ—î∏Å1îÅô•±—…îÅME0(ÄÄÄÄåÉ•Ÿ•—îÅÖ±Ω…ÃÅëîÅç°Ö…ùï»Åï–Åì•çΩëï»Å—Ω’ÃÅ±ïÃÅëΩÕÕ•ï…ÃÅ]=ÅëîÅ∞ùΩ…ùÖπ•Õµî∏(ÄÄÄÅ›•—†Å}›ïëΩô}çΩππïç–†§ÅÖÃÅëàË(ÄÄÄÄÄÄÄÅë•…ïç—±Â}±•π≠ïë}…Ω›ÃÄÙÅëàπï·ïç’—î†ààà(ÄÄÄÄÄÄÄÄÄÄÄÅM1PÅ»π…ïÕΩ’…çï}—Â¡î∞Å»πÕ—Öâ±ï}•ê∞Å»π¡ÖÂ±ΩÖë}©ÕΩ∏∞Å»π…ïµΩ—ï}ëÖ—î∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ»πÕÂπçïë}Ö–∞Å∞πçΩπ—Öç—}•êÅLÅ±•π≠ïë}çΩπ—Öç—}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ∞πÖ——ïπëïï}•ê∞Å∞πµÖ—ç°}µï—°Ωê(ÄÄÄÄÄÄÄÄÄÄÄÅI=4Å›ïëΩô}…ïÕΩ’…çïÃÅ»Å1PÅ)=%8Å›ïëΩô}çΩπ—Öç—}±•π≠ÃÅ∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÅ=8Å»π…ïÕΩ’…çï}—Â¡îı∞π…ïÕΩ’…çï}—Â¡îÅ9Å»πÕ—Öâ±ï}•êı∞π…ïÕΩ’…çï}•ê(ÄÄÄÄÄÄÄÄÄÄÄÅ]!IÅ∞πçΩπ—Öç—}•êÙ¸(ÄÄÄÄÄÄÄÄÄÄÄÅ=IHÅ	dÅ»πÕÂπçïë}Ö–ÅM(ÄÄÄÄÄÄÄÄààà∞Ä°Õ—»°çΩπ—Öç—}•ê§∞§§πôï—ç°Ö±∞†§((ÄÄÄÄÄÄÄÄåÅ1îÅâÖ±ÖÂÖùîÅ¡Ö»Å•ëïπ—•”§ÅπîÅ…ïÕ—îÅª•çïÕÕÖ•…îÅ≈’îÅ¡Ω’»Å’πîÅπΩ’Ÿï±±î(ÄÄÄÄÄÄÄÄåÅ¡•Õ—îÅπΩ∏ÅïπçΩ…îÅ±ß•îÅΩ‘Å¡Ω’»ÅëîÅŸ…Ö•ÃÅëΩ’â±ΩπÃÅπΩ¥Ω¡À•πΩ¥∏(ÄÄÄÄÄÄÄÅ•òÅë•…ïç—±Â}±•π≠ïë}…Ω›ÃÅÖπêÅÕÖµï}πÖµï}çΩ’π–ÄÙÄƒË(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ω›ÃÄÙÅë•…ïç—±Â}±•π≠ïë}…Ω›Ã(ÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ω›ÃÄÙÅëàπï·ïç’—î†ààà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅM1PÅ»π…ïÕΩ’…çï}—Â¡î∞Å»πÕ—Öâ±ï}•ê∞Å»π¡ÖÂ±ΩÖë}©ÕΩ∏∞Å»π…ïµΩ—ï}ëÖ—î∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ»πÕÂπçïë}Ö–∞Å∞πçΩπ—Öç—}•êÅLÅ±•π≠ïë}çΩπ—Öç—}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ∞πÖ——ïπëïï}•ê∞Å∞πµÖ—ç°}µï—°Ωê(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅI=4Å›ïëΩô}…ïÕΩ’…çïÃÅ»Å1PÅ)=%8Å›ïëΩô}çΩπ—Öç—}±•π≠ÃÅ∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ=8Å»π…ïÕΩ’…çï}—Â¡îı∞π…ïÕΩ’…çï}—Â¡îÅ9Å»πÕ—Öâ±ï}•êı∞π…ïÕΩ’…çï}•ê(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ=IHÅ	dÅ»πÕÂπçïë}Ö–ÅM(ÄÄÄÄÄÄÄÄÄÄÄÄààà§πôï—ç°Ö±∞†§((ÄÄÄÅ…ïÕΩ’…çïÃÄÙÅmt(ÄÄÄÅôΩ»Å…Ω‹Å•∏Å…Ω›ÃË(ÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ©ÕΩ∏π±ΩÖëÃ°…Ω›lâ¡ÖÂ±ΩÖë}©ÕΩ∏ât§(ÄÄÄÄÄÄÄÅë•…ïç—±Â}±•π≠ïêÄÙÅÕ—»°…Ω›lâ±•π≠ïë}çΩπ—Öç—}•êâtÅΩ»Äàà§ÄÙÙÅÕ—»°çΩπ—Öç—}•ê§(ÄÄÄÄÄÄÄÄåÅUπîÅ∑©µîÅ¡ï…ÕΩππîÅ¡ï’–ÅÖŸΩ•»Å¡±’Õ•ï’…ÃÅ¡•Õ—ïÃÅI4∏Å1îÅ±•ï∏Å]=Å…ïÕ—î(ÄÄÄÄÄÄÄÄåÅ’π•≈’îÅï∏ÅâÖÕî∞ÅµÖ•ÃÅç°Ö≈’îÅëΩ’â±Ω∏Åêù•ëïπ—•”§ÅëΩ•–Å¡Ω’ŸΩ•»ÅçΩπÕ’±—ï»(ÄÄÄÄÄÄÄÄåÅÕïÃÅëΩÕÕ•ï…ÃÅÕÖπÃÅ¡ï…ë…îÅ±ïÃÅÖççïπ—ÃÅëîÅÕΩ∏ÅπΩ¥ÅëÖπÃÅ±îÅI4∏(ÄÄÄÄÄÄÄÅ•òÅπΩ–Åë•…ïç—±Â}±•π≠ïêÅÖπêÅπΩ–Å}›ïëΩô}çΩπ—Öç—}πÖµï}µÖ—ç°ïÃ°¡ÖÂ±ΩÖê∞ÅçΩπ—Öç–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅÖ——ïπëïï}•êÄÙÅ…Ω›lâÖ——ïπëïï}•êât(ÄÄÄÄÄÄÄÅµÖ—ç°}µï—°ΩêÄÙÅ…Ω›lâµÖ—ç°}µï—°Ωêât(ÄÄÄÄÄÄÄÅ•òÅπΩ–Åë•…ïç—±Â}±•π≠ïêË(ÄÄÄÄÄÄÄÄÄÄÄÅ|∞Å|∞ÅÖ——ïπëïï}•êÄÙÅ}›ïëΩô}Ö——ïπëïï}ŸÖ±’ïÃ°¡ÖÂ±ΩÖê§(ÄÄÄÄÄÄÄÄÄÄÄÅµÖ—ç°}µï—°ΩêÄÙÄâπÖµîà(ÄÄÄÄÄÄÄÅ…ïÕΩ’…çïÃπÖ¡¡ïπê°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâ—Â¡îàËÅ…Ω›lâ…ïÕΩ’…çï}—Â¡îât∞ÄâÕ—Öâ±ï}•êàËÅ…Ω›lâÕ—Öâ±ï}•êât∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡ÖÂ±ΩÖêàËÅ¡ÖÂ±ΩÖê∞Äâ…ïµΩ—ï}ëÖ—îàËÅ…Ω›lâ…ïµΩ—ï}ëÖ—îât∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕÂπçïë}Ö–àËÅ…Ω›lâÕÂπçïë}Ö–ât∞ÄâÖ——ïπëïï}•êàËÅÖ——ïπëïï}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÄâµÖ—ç°}µï—°ΩêàËÅµÖ—ç°}µï—°Ωê∞(ÄÄÄÄÄÄÄÅÙ§(ÄÄÄÄåÅQΩ’—ïÃÅ±ïÃÅŸ’ïÃÅAΩPÅëΩ•Ÿïπ–Å¡Ö…—Öùï»Å±ÑÅ∑©µîÅÕΩ’…çîÅëîÅ€•…•”§ÄËÅ±î(ÄÄÄÄåÅëΩÕÕ•ï»ÅçÀß§Å±îÅ¡±’ÃÅÀ•çïµµïπ–∏Å1ïÃÅÖ’—…ïÃÅ…ïÕ—ïπ–ÅçΩπÕ’±—Öâ±ïÃÅçΩµµî(ÄÄÄÄåÅ°•Õ—Ω…•≈’î∞ÅµÖ•ÃÅπîÅëΩ•Ÿïπ–Å©ÖµÖ•ÃÅ¡Ö…—•ç•¡ï»ÅÖ’‡ÅÕ—Ö—’—ÃÅçÖ±ç’≥•Ã∏(ÄÄÄÅ…ïÕΩ’…çïÃπÕΩ…–†(ÄÄÄÄÄÄÄÅ≠ï‰ı±ÖµâëÑÅ…ïÕΩ’…çîËÅ}›ïëΩô}ôΩ±ëï…}…ïçïπçÂ}≠ï‰†(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕΩ’…çïlâ¡ÖÂ±ΩÖêât∞(ÄÄÄÄÄÄÄÄÄÄÄÅôÖ±±âÖç¨ı…ïÕΩ’…çîπùï–†â…ïµΩ—ï}ëÖ—îà§ÅΩ»Å…ïÕΩ’…çîπùï–†âÕÂπçïë}Ö–à§∞(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Öâ±ï}•êı…ïÕΩ’…çîπùï–†âÕ—Öâ±ï}•êà§∞(ÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÅ…ïŸï…ÕîıQ…’î∞(ÄÄÄÄ§(ÄÄÄÅôΩ»Å•πëï‡∞Å…ïÕΩ’…çîÅ•∏Åïπ’µï…Ö—î°…ïÕΩ’…çïÃ§Ë(ÄÄÄÄÄÄÄÅ…ïÕΩ’…çïlâ•Õ}±Ö—ïÕ–âtÄÙÅ•πëï‡ÄÙÙÄ¿(ÄÄÄÅ…ï—’…∏Å…ïÕΩ’…çïÃ(()ëïòÅ}›ïëΩô}…ïô…ïÕ°}çΩπ—Öç—}…ïÕΩ’…çî°çΩπ—Öç—}•ê∞ÅëÖ—Ñı9Ωπî∞Ä®∞ÅÖ’—ΩµÖ—•åıÖ±Õî§Ë(ÄÄÄÄààâIï±•–Å’π•≈’ïµïπ–Å±îÅëΩÕÕ•ï»Å]=Å±îÅ¡±’ÃÅÀ•çïπ–Åì•´ÄÅçΩππ‘Åë‘ÅçΩπ—Öç–∏((ÄÄÄÅ∏ÅΩ’Ÿï…—’…îÅÖ’—ΩµÖ—•≈’î∞Å’∏ÅçÖç°îÅëîÅµΩ•πÃÅëîÅ—…ïπ—îÅµ•π’—ïÃÅïÕ–Å…ïπŸΩÁ§(ÄÄÄÅÕÖπÃÅ…ï≈◊©—îÅë•Õ—Öπ—î∏Å1îÅâΩ’—Ω∏ÅµÖπ’ï∞ÅçΩπÕï…ŸîÅ±ÑÅ¡ΩÕÕ•â•±•”§ÅëîÅôΩ…çï»(ÄÄÄÅ’πîÅ±ïç—’…î∞Å—Ω’–Åï∏Å…ïÕ¡ïç—Öπ–Å±îÅâÖ•∞Å≈’§Åì•ë’¡±•≈’îÅ±ïÃÅÖ¡¡ï±ÃÅçΩπç’……ïπ—Ã∏(ÄÄÄÄààà(ÄÄÄÅ…ïÕΩ’…çïÃÄÙÅ}›ïëΩô}çΩπ—Öç—}…ïÕΩ’…çïÃ°çΩπ—Öç—}•ê∞ÅëÖ—Ñ§(ÄÄÄÅ±Ö—ïÕ–ÄÙÅπï·–†(ÄÄÄÄÄÄÄÄ°…ïÕΩ’…çîÅôΩ»Å…ïÕΩ’…çîÅ•∏Å…ïÕΩ’…çïÃÅ•òÅ…ïÕΩ’…çîπùï–†â•Õ}±Ö—ïÕ–à§§∞(ÄÄÄÄÄÄÄÅ…ïÕΩ’…çïÕl¡tÅ•òÅ…ïÕΩ’…çïÃÅï±ÕîÅ9Ωπî∞(ÄÄÄÄ§(ÄÄÄÅ•òÅπΩ–Å±Ö—ïÕ–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâΩ¨àËÅQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ≠•¡¡ïêàËÅQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ïÖÕΩ∏àËÄâπΩ}≠πΩ›π}ôΩ±ëï»à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡…ΩçïÕÕïêàËÄ¿∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÅï·—ï…πÖ±}•êÄÙÅÕ—»°±Ö—ïÕ–πùï–†âÕ—Öâ±ï}•êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–Åï·—ï…πÖ±}•êË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâΩ¨àËÅQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ≠•¡¡ïêàËÅQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ïÖÕΩ∏àËÄâπΩ}≠πΩ›π}ôΩ±ëï»à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡…ΩçïÕÕïêàËÄ¿∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÅÖùï}ÕïçΩπëÃÄÙÅ}›ïëΩô}…ïÕΩ’…çï}Öùï}ÕïçΩπëÃ°±Ö—ïÕ–§(ÄÄÄÅ•òÄ°Ö’—ΩµÖ—•åÅÖπêÅÖùï}ÕïçΩπëÃÅ•ÃÅπΩ–Å9Ωπî(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅÖùï}ÕïçΩπëÃÄÅ]=}=9QQ}=A9}IIM!}5%9}}M=9L§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâΩ¨àËÅQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ≠•¡¡ïêàËÅQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ïÖÕΩ∏àËÄâô…ïÕ°}çÖç°îà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡…ΩçïÕÕïêàËÄ¿∞(ÄÄÄÄÄÄÄÄÄÄÄÄâôΩ±ëï…}•êàËÅï·—ï…πÖ±}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ±ÖÕ—}ÕÂπç}Ö–àËÅ±Ö—ïÕ–πùï–†âÕÂπçïë}Ö–à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÅÙ((ÄÄÄÅ±ïÖÕîÄÙÅ}›ïëΩô}âïù•π}çΩπ—Öç—}…ïô…ïÕ†°ï·—ï…πÖ±}•ê∞ÅÖ’—ΩµÖ—•åıÖ’—ΩµÖ—•å§(ÄÄÄÅ•òÅπΩ–Å±ïÖÕîπùï–†âÖç≈’•…ïêà§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâΩ¨àËÅQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ≠•¡¡ïêàËÅQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡…ΩçïÕÕïêàËÄ¿∞(ÄÄÄÄÄÄÄÄÄÄÄÄâôΩ±ëï…}•êàËÅï·—ï…πÖ±}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ±ÖÕ—}ÕÂπç}Ö–àËÅ±Ö—ïÕ–πùï–†âÕÂπçïë}Ö–à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄÄÄÄÄ®©±ïÖÕî∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÅ—Ω≠ï∏ÄÙÅÕ—»°±ïÖÕîπùï–†â—Ω≠ï∏à§ÅΩ»Äàà§(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄåÅU∏ÅÖ’—…îÅ›Ω…≠ï»ÅÑÅ¡‘Å—ï…µ•πï»Åïπ—…îÅ±îÅ¡…ïµ•ï»ÅçΩπ—À—±îÅëîÅô…áπç°ï’»(ÄÄÄÄÄÄÄÄåÅï–Å±ÑÅ¡…•ÕîÅë‘ÅâÖ•∞∏ÅIï±•…îÅ±îÅçÖç°îÉ•Ÿ•—îÅÖ±Ω…ÃÅ’∏ÅÕïçΩπêÅP∏(ÄÄÄÄÄÄÄÅ•òÅÖ’—ΩµÖ—•åË(ÄÄÄÄÄÄÄÄÄÄÄÅç’……ïπ—}…ïÕΩ’…çïÃÄÙÅ}›ïëΩô}çΩπ—Öç—}…ïÕΩ’…çïÃ°çΩπ—Öç—}•ê§(ÄÄÄÄÄÄÄÄÄÄÄÅç’……ïπ—}±Ö—ïÕ–ÄÙÅπï·–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ°…ïÕΩ’…çîÅôΩ»Å…ïÕΩ’…çîÅ•∏Åç’……ïπ—}…ïÕΩ’…çïÃ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ…ïÕΩ’…çîπùï–†â•Õ}±Ö—ïÕ–à§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅç’……ïπ—}…ïÕΩ’…çïÕl¡tÅ•òÅç’……ïπ—}…ïÕΩ’…çïÃÅï±ÕîÅ9Ωπî∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅç’……ïπ—}•êÄÙÅÕ—»†°ç’……ïπ—}±Ö—ïÕ–ÅΩ»ÅÌÙ§πùï–†âÕ—Öâ±ï}•êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅç’……ïπ—}•êÅÖπêÅç’……ïπ—}•êÄÑÙÅï·—ï…πÖ±}•êË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}›ïëΩô}çÖπçï±}çΩπ—Öç—}…ïô…ïÕ†°ï·—ï…πÖ±}•ê∞Å—Ω≠ï∏§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å}›ïëΩô}…ïô…ïÕ°}çΩπ—Öç—}…ïÕΩ’…çî†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—}•ê∞ÅÖ’—ΩµÖ—•åıQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅç’……ïπ—}ÖùîÄÙÅ}›ïëΩô}…ïÕΩ’…çï}Öùï}ÕïçΩπëÃ°ç’……ïπ—}±Ö—ïÕ–§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÄ°ç’……ïπ—}ÖùîÅ•ÃÅπΩ–Å9Ωπî(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅç’……ïπ—}ÖùîÄÅ]=}=9QQ}=A9}IIM!}5%9}}M=9L§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}›ïëΩô}çÖπçï±}çΩπ—Öç—}…ïô…ïÕ†°ï·—ï…πÖ±}•ê∞Å—Ω≠ï∏§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâΩ¨àËÅQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÕ≠•¡¡ïêàËÅQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ…ïÖÕΩ∏àËÄâô…ïÕ°}çÖç°îà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ¡…ΩçïÕÕïêàËÄ¿∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâôΩ±ëï…}•êàËÅï·—ï…πÖ±}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ±ÖÕ—}ÕÂπç}Ö–àËÄ°ç’……ïπ—}±Ö—ïÕ–ÅΩ»ÅÌÙ§πùï–†âÕÂπçïë}Ö–à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÙ((ÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖê∞Å}°ïÖëï…ÃÄÙÅ}›ïëΩô}…ï≈’ïÕ–†(ÄÄÄÄÄÄÄÄÄÄÄÅòàΩÖ¡§Ω…ïù•Õ—…Ö—•ΩπΩ±ëï…ÃΩÌ≈’Ω—î°ï·—ï…πÖ±}•ê∞ÅÕÖôîÙúú•Ùà∞(ÄÄÄÄÄÄÄÄÄÄÄÅΩ¡ï…Ö—•Ω∏Ù†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ…ïô…ïÕ°}±Ö—ïÕ—}ôΩ±ëï…}Ωπ}Ω¡ï∏à(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅÖ’—ΩµÖ—•åÅï±ÕîÄâ…ïô…ïÕ°}±Ö—ïÕ—}ôΩ±ëï…}µÖπ’Ö∞à(ÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄÄÄÄÄåÅUπîÅΩ’Ÿï…—’…îÅëîÅô•ç°îÅπîÅëΩ•–Å©ÖµÖ•ÃÅµ’±—•¡±•ï»Å±ïÃÅ—ïπ—Ö—•ŸïÃ(ÄÄÄÄÄÄÄÄÄÄÄÄåÅ!QQ@∏Å∏ÅçÖÃÅêü•ç°ïå∞Å±îÅçÖç°îÅ…ïÕ—îÅŸ•Õ•â±îÅï–Å±îÅçΩΩ±ëΩ›∏Å¡…ïπê(ÄÄÄÄÄÄÄÄÄÄÄÄåÅ±îÅ…ï±Ö•ÃÄÏÅ±îÅâΩ’—Ω∏ÅµÖπ’ï∞Åëïµï’…îÅë•Õ¡Ωπ•â±îÅ¡Ω’»ÅÀ•ïÕÕÖÂï»∏(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—…Â}â’ëùï–Ù¿Å•òÅÖ’—ΩµÖ—•åÅï±ÕîÅ9Ωπî∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅôΩ±ëï»ÄÙÅ¡ÖÂ±ΩÖêπùï–†âëÖ—Ñà§Å•òÅ•Õ•πÕ—Öπçî°¡ÖÂ±ΩÖê∞Åë•ç–§Åï±ÕîÅ9Ωπî(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°ôΩ±ëï»∞Åë•ç–§ÅΩ»ÅπΩ–ÅôΩ±ëï»πùï–†âï·—ï…πÖ±%êà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ±ëï»ÄÙÅ¡ÖÂ±ΩÖê(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°ôΩ±ëï»∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅ]ïëΩôA%……Ω»†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ]=ÅÑÅ…ï—Ω’…ª§Å’∏ÅëΩÕÕ•ï»ÅëÖπÃÅ’∏ÅôΩ…µÖ–Å•πÖ——ïπë‘∏à(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ…ï—’…πïë}•êÄÙÅÕ—»°ôΩ±ëï»πùï–†âï·—ï…πÖ±%êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅ…ï—’…πïë}•êÅÖπêÅ…ï—’…πïë}•êÄÑÙÅï·—ï…πÖ±}•êË(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅ]ïëΩôA%……Ω»†â]=ÅÑÅ…ï—Ω’…ª§Å’∏ÅÖ’—…îÅπ’∑•…ºÅëîÅëΩÕÕ•ï»∏à§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å…ï—’…πïë}•êË(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ±ëï»ÄÙÅÏ®©ôΩ±ëï»∞Äâï·—ï…πÖ±%êàËÅï·—ï…πÖ±}•ëÙ(ÄÄÄÄÄÄÄÅ…ïÕ’±–ÄÙÅ}›ïëΩô}Õ—Ω…ï}¡Öùî†(ÄÄÄÄÄÄÄÄÄÄÄÅmôΩ±ëï…t∞Å9Ωπî∞Ä¿∞Å’¡ëÖ—ï}ÕÂπç}Õ—Ö—îıÖ±Õî∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ…ïô…ïÕ°ïêÄÙÅ}›ïëΩô}çΩπ—Öç—}…ïÕΩ’…çïÃ°çΩπ—Öç—}•ê§(ÄÄÄÄÄÄÄÅ…ïô…ïÕ°ïë}±Ö—ïÕ–ÄÙÅπï·–†(ÄÄÄÄÄÄÄÄÄÄÄÄ°…ïÕΩ’…çîÅôΩ»Å…ïÕΩ’…çîÅ•∏Å…ïô…ïÕ°ïêÅ•òÅ…ïÕΩ’…çîπùï–†â•Õ}±Ö—ïÕ–à§§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïô…ïÕ°ïël¡tÅ•òÅ…ïô…ïÕ°ïêÅï±ÕîÅÌÙ∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ}›ïëΩô}ô•π•Õ°}çΩπ—Öç—}…ïô…ïÕ†°ï·—ï…πÖ±}•ê∞Å—Ω≠ï∏§(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâΩ¨àËÅQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ≠•¡¡ïêàËÅÖ±Õî∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡…ΩçïÕÕïêàËÄƒ∞(ÄÄÄÄÄÄÄÄÄÄÄÄâôΩ±ëï…}•êàËÅï·—ï…πÖ±}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÄ®©…ïÕ’±–∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ±ÖÕ—}ÕÂπç}Ö–àËÅ…ïô…ïÕ°ïë}±Ö—ïÕ–πùï–†âÕÂπçïë}Ö–à§ÅΩ»Å}›ïëΩô}πΩ‹†§∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÅï·çï¡–Å·çï¡—•Ω∏ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ}›ïëΩô}ô•π•Õ°}çΩπ—Öç—}…ïô…ïÕ†°ï·—ï…πÖ±}•ê∞Å—Ω≠ï∏∞Åï……Ω»ıï·å§(ÄÄÄÄÄÄÄÅ…Ö•Õî(()ëïòÅ}›ïëΩô}πΩ…µÖ±•Èï}çΩëî°ŸÖ±’î§Ë(ÄÄÄÅ…ï—’…∏Å…îπÕ’à°»âmyÑµË¿¥Âtà∞Äàà∞Å’π•çΩëïëÖ—ÑππΩ…µÖ±•Èî†(ÄÄÄÄÄÄÄÄâ9à∞ÅÕ—»°ŸÖ±’îÅΩ»Äàà§(ÄÄÄÄ§πïπçΩëî†âÖÕç•§à∞Äâ•ùπΩ…îà§πëïçΩëî†§π±Ω›ï»†§§(()ëïòÅ}›ïëΩô}ÕΩ±•ç•—Ö—•Ωπ}Õ—Ö—’ÕïÃ°¡ÖÂ±ΩÖê§Ë(ÄÄÄÄààâ1•–Å±ïÃÅì•ç•Õ•ΩπÃÅëîÅô•πÖπçïµïπ–Å•µâ…•≈◊•ïÃÅëÖπÃÅ±îÅëΩÕÕ•ï»Å]=∏ààà(ÄÄÄÅ—…Ö•π•πù}•πôºÄÙÅ¡ÖÂ±ΩÖêπùï–†â—…Ö•π•πùç—•Ωπ%πôºà§(ÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°—…Ö•π•πù}•πôº∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÕï–†§(ÄÄÄÅÕΩ±•ç•—Ö—•ΩπÃÄÙÅ—…Ö•π•πù}•πôºπùï–†âÕΩ±•ç•—Ö—•ΩπÃà§(ÄÄÄÅÕ—Ö—’ÕïÃÄÙÅÕï–†§((ÄÄÄÅëïòÅŸ•Õ•–°ŸÖ±’î§Ë(ÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°ŸÖ±’î∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»Å≠ï‰Å•∏Ä†âÕ—Ö—’Ãà∞ÄâÕ—Ö—îà∞Äâ…ïÕ’±–à§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçÖπë•ëÖ—îÄÙÅŸÖ±’îπùï–°≠ï‰§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅçÖπë•ëÖ—îÅπΩ–Å•∏Ä°9Ωπî∞Äàà∞ÅÖ±Õî§ÅÖπêÅπΩ–Å•Õ•πÕ—Öπçî†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçÖπë•ëÖ—î∞Ä°ë•ç–∞Å±•Õ–∞Å—’¡±î§§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö—’ÕïÃπÖëê°}›ïëΩô}πΩ…µÖ±•Èï}çΩëî°çÖπë•ëÖ—î§§(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»ÅπïÕ—ïêÅ•∏ÅŸÖ±’îπŸÖ±’ïÃ†§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°πïÕ—ïê∞Ä°ë•ç–∞Å±•Õ–∞Å—’¡±î§§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅŸ•Õ•–°πïÕ—ïê§(ÄÄÄÄÄÄÄÅï±•òÅ•Õ•πÕ—Öπçî°ŸÖ±’î∞Ä°±•Õ–∞Å—’¡±î§§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»ÅπïÕ—ïêÅ•∏ÅŸÖ±’îË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅŸ•Õ•–°πïÕ—ïê§((ÄÄÄÅŸ•Õ•–°ÕΩ±•ç•—Ö—•ΩπÃ§(ÄÄÄÅ…ï—’…∏ÅÕ—Ö—’ÕïÃ(()ëïòÅ}›ïëΩô}ô…Öπçï}—…ÖŸÖ•±}Õ—Ö—’Ã°¡ÖÂ±ΩÖê∞Å¡…ïŸ•Ω’Õ}Õ—Ö—’ÃÙàà§Ë(ÄÄÄÄààâ•ë’•–Å±îÅÕ—Ö—’–ÅPÅêù’∏ÅëΩÕÕ•ï»Å]=ÅÕÖπÃÅì•¡ïπë…îÅë‘ÅÕ—Ö—’–ÅçΩµµï…ç•Ö∞∏ààà(ÄÄÄÅπΩ…µÖ±•ÈîÄÙÅ}›ïëΩô}πΩ…µÖ±•Èï}çΩëî(ÄÄÄÅÕ—Ö—îÄÙÅπΩ…µÖ±•Èî°¡ÖÂ±ΩÖêπùï–†âÕ—Ö—îà§ÅΩ»Å¡ÖÂ±ΩÖêπùï–†âÕ—Ö—’Ãà§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ»Å¡ÖÂ±ΩÖêπùï–†â…ïù•Õ—…Ö—•ΩπM—Ö—îà§§(ÄÄÄÅ°•Õ—Ω…‰ÄÙÅ¡ÖÂ±ΩÖêπùï–†â°•Õ—Ω…‰à§ÅΩ»Å¡ÖÂ±ΩÖêπùï–†âÕ—Ö—ï!•Õ—Ω…‰à§ÅΩ»Å¡ÖÂ±ΩÖêπùï–†âïŸïπ—Ãà§ÅΩ»Åmt((ÄÄÄÅëïòÅ°•Õ—Ω…Â}ŸÖ±’ïÃ°ŸÖ±’î§Ë(ÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°ŸÖ±’î∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»Å≠ï‰∞ÅπïÕ—ïêÅ•∏ÅŸÖ±’îπ•—ïµÃ†§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπïÕ—ïêÅ•∏Ä°9Ωπî∞Äàà∞ÅÖ±Õî§ÅΩ»ÅπïÕ—ïêÅ•∏Ä°mt∞ÅÌÙ§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÂ•ï±êÅ≠ï‰(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÂ•ï±êÅô…Ω¥Å°•Õ—Ω…Â}ŸÖ±’ïÃ°πïÕ—ïê§(ÄÄÄÄÄÄÄÅï±•òÅ•Õ•πÕ—Öπçî°ŸÖ±’î∞Ä°±•Õ–∞Å—’¡±î§§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»ÅπïÕ—ïêÅ•∏ÅŸÖ±’îË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÂ•ï±êÅô…Ω¥Å°•Õ—Ω…Â}ŸÖ±’ïÃ°πïÕ—ïê§(ÄÄÄÄÄÄÄÅï±•òÅŸÖ±’îÅπΩ–Å•∏Ä°9Ωπî∞Äàà∞ÅÖ±Õî§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÂ•ï±êÅŸÖ±’î((ÄÄÄÅ°•Õ—Ω…Â}µÖ…≠ï…ÃÄÙÅÏ(ÄÄÄÄÄÄÄÅπΩ…µÖ±•Èî°ŸÖ±’î§ÅôΩ»ÅŸÖ±’îÅ•∏Å°•Õ—Ω…Â}ŸÖ±’ïÃ°°•Õ—Ω…‰§Å•òÅŸÖ±’î(ÄÄÄÅÙ((ÄÄÄÄåÅU∏Å…ïô’ÃÅï·¡±•ç•—îÅïÕ–Å’πîÅ¡…ï’ŸîÅÕ’ôô•ÕÖπ—îÅï∏Å±’§µ∑©µî∞Å‰ÅçΩµ¡…•Ã(ÄÄÄÄåÅ±Ω…Õ≈’îÅ]=ÅπîÅôΩ’…π•–Å¡ÖÃÅ∞ù°•Õ—Ω…•≈’îÅëîÅ∞ù•πÕ—…’ç—•Ω∏ÅP∏(ÄÄÄÅÕΩ±•ç•—Ö—•Ωπ}°ÖÕ}…ïô’ÕÖ∞ÄÙÅÖπ‰†(ÄÄÄÄÄÄÄÅ…îπÕïÖ…ç†°»â…ïô’ÕÒ…ï©ïç–à∞ÅÕ—Ö—’Ã§(ÄÄÄÄÄÄÄÅôΩ»ÅÕ—Ö—’ÃÅ•∏Å}›ïëΩô}ÕΩ±•ç•—Ö—•Ωπ}Õ—Ö—’ÕïÃ°¡ÖÂ±ΩÖê§(ÄÄÄÄ§(ÄÄÄÅ•òÅ…îπÕïÖ…ç†°»â…ïô’ÕÒ…ï©ïç–à∞ÅÕ—Ö—î§ÅΩ»ÅÕΩ±•ç•—Ö—•Ωπ}°ÖÕ}…ïô’ÕÖ∞Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Äâ…ïô’Õïîà(ÄÄÄÅ°•Õ—Ω…Â}°ÖÕ}ô•πÖπçï…}…ïô’ÕÖ∞ÄÙÅÖπ‰†(ÄÄÄÄÄÄÄÄ†âô•πÖπçï»àÅ•∏ÅµÖ…≠ï»ÅΩ»Äâô•πÖπçï’»àÅ•∏ÅµÖ…≠ï»§(ÄÄÄÄÄÄÄÅÖπêÄ†â…ïô’ÃàÅ•∏ÅµÖ…≠ï»ÅΩ»Äâ…ï©ïç–àÅ•∏ÅµÖ…≠ï»§(ÄÄÄÄÄÄÄÅôΩ»ÅµÖ…≠ï»Å•∏Å°•Õ—Ω…Â}µÖ…≠ï…Ã(ÄÄÄÄ§(ÄÄÄÅ°Öë}•πÕ—…’ç—•Ω∏ÄÙÄ†(ÄÄÄÄÄÄÄÅÕ—»°¡…ïŸ•Ω’Õ}Õ—Ö—’ÃÅΩ»Äàà§πÕ—…•¿†§ÄÙÙÄâïπ}çΩ’…Õ}•πÕ—…’ç—•Ω∏à(ÄÄÄÄÄÄÄÅΩ»ÅÕ—Ö—îÄÙÙÄâ›Ö•—•πùÖççï¡—Ö—•Ω∏à(ÄÄÄÄÄÄÄÅΩ»ÅÖπ‰†(ÄÄÄÄÄÄÄÄÄÄÄÄâ›Ö•—•πùÖççï¡—Ö—•Ω∏àÅ•∏ÅµÖ…≠ï»(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»ÅµÖ…≠ï»Å•∏Å°•Õ—Ω…Â}µÖ…≠ï…Ã(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄ§(ÄÄÄÄåÅ1îÅÕ—Ö—’–ÅçΩµµï…ç•Ö∞Åë‘ÅëΩÕÕ•ï»Å¡ï’–ÅïπÕ’•—îÅ…ïëïŸïπ•»ÅÅÅÖççï¡—ïëÅÄ(ÄÄÄÄåÅ±Ω…Õ≈’îÅ±îÅçÖπë•ëÖ–Å±îÅŸÖ±•ëîÅëîÅπΩ’ŸïÖ‘∏Å1ÑÅ¡…ï’ŸîÅëîÅ…ïô’ÃÅë‘Åô•πÖπçï’»(ÄÄÄÄåÅ…ïÕ—îÅª•ÖπµΩ•πÃÅŸÖ±Öâ±îÅ¡Ω’»Å±ÑÅÕïçΩπëîÅ—•µï±•πîÅëîÅçîÅ∑©µîÅëΩÕÕ•ï»∏(ÄÄÄÅ•òÅ°•Õ—Ω…Â}°ÖÕ}ô•πÖπçï…}…ïô’ÕÖ∞Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Äâ…ïô’Õïîà(ÄÄÄÅ•òÅπΩ–Å°Öë}•πÕ—…’ç—•Ω∏Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Äàà(ÄÄÄÅ•òÅ…îπÕïÖ…ç†°»âçÖπçï±ÒÖππ’±ÒÖâÖπëΩ∏à∞ÅÕ—Ö—î§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÄâÖππ’±ïîà(ÄÄÄÅ•òÅÕ—Ö—îÅ•∏ÅÏâÖççï¡—ïêà∞Äâ•π—…Ö•π•πúà∞Äâ—ï…µ•πÖ—ïêà∞ÄâÕï…Ÿ•çïëΩπïëïç±Ö…ïêà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÕï…Ÿ•çïëΩπïŸÖ±•ëÖ—ïêà∞Äâ—Ωâ•±∞à∞Äââ•±±ïêà∞Äâ¡Ö•êâÙË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÄâÖççï¡—ïîà(ÄÄÄÄåÅ¡À°ÃÅ’∏Å…ïô’ÃÅëîÅô•πÖπçïµïπ–Å…ÖπçîÅQ…ÖŸÖ•∞∞Å]=ÅπîÅçΩπÕï…ŸîÅ¡ÖÃÅ’∏(ÄÄÄÄåÉ•—Ö–Å—ï…µ•πÖ∞Åì•ëß§ÄËÅ±îÅëΩÕÕ•ï»ÅAÅ…ïŸ•ïπ–ÉÄÅÅÅŸÖ±•ëÖ—ïëÅÄÅÖô•∏Å≈’îÅ±î(ÄÄÄÄåÅçÖπë•ëÖ–Å¡’•ÕÕîÅëîÅπΩ’ŸïÖ‘Å∞ùÖççï¡—ï»ÅΩ‘Åç°Ω•Õ•»Å’∏ÅÖ’—…îÅô•πÖπçïµïπ–∏(ÄÄÄÄåÅ1ÑÅ¡À•ÕïπçîÅÖπ”•…•ï’…îÅëîÅÅÅ›Ö•—•πùççï¡—Ö—•ΩπÅÄÅ¡ï…µï–ÅëîÅë•Õ—•πù’ï»Åçî(ÄÄÄÄåÅ…ï—Ω’»Åêù’∏ÅëΩÕÕ•ï»ÅÕ•µ¡±ïµïπ–ÅŸÖ±•ì§Å≈’§Å∏ùÑÅ©ÖµÖ•ÃÉ•”§Å—…ÖπÕµ•ÃÉÄÅP∏(ÄÄÄÅ•òÅÕ—Ö—îÄÙÙÄâŸÖ±•ëÖ—ïêàË(ÄÄÄÄÄÄÄÅ…ï—’…∏Äâ…ïô’Õïîà(ÄÄÄÅ…ï—’…∏Äâïπ}çΩ’…Õ}•πÕ—…’ç—•Ω∏à(()ëïòÅ}›ïëΩô}ôΩ±ëï…}…ïçïπçÂ}≠ï‰°¡ÖÂ±ΩÖê∞Ä®∞ÅôÖ±±âÖç¨Ùàà∞ÅÕ—Öâ±ï}•êÙàà§Ë(ÄÄÄÄààâ±ÖÕÕîÅ’∏ÅëΩÕÕ•ï»Å¡Ö»ÅÕÑÅçÀ•Ö—•Ω∏∞Å©ÖµÖ•ÃÅ¡Ö»ÅÕÑÅëï…πß°…îÅµΩë•ô•çÖ—•Ω∏∏ààà(ÄÄÄÅ…Ö›}ç…ïÖ—ïêÄÙÅπï·–†°¡ÖÂ±ΩÖêπùï–°≠ï‰§ÅôΩ»Å≠ï‰Å•∏Ä†(ÄÄÄÄÄÄÄÄâç…ïÖ—ïë–à∞Äâç…ïÖ—ïë=∏à∞ÄâëÖ—ï…ïÖ—ïêà∞Äâç…ïÖ—•ΩπÖ—îà∞(ÄÄÄÄ§Å•òÅ¡ÖÂ±ΩÖêπùï–°≠ï‰§§∞Å9Ωπî§((ÄÄÄÅëïòÅ—•µïÕ—Öµ¿°ŸÖ±’î§Ë(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö…ÕïêÄÙÅëÖ—ï—•µîπëÖ—ï—•µîπô…Ωµ•ÕΩôΩ…µÖ–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—»°ŸÖ±’îÅΩ»Äàà§πÕ—…•¿†§π…ï¡±Öçî†âhà∞Äà¨¿¿Ë¿¿à§(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(ÄÄÄÄÄÄÄÅ•òÅ¡Ö…Õïêπ—È•πôºÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö…ÕïêÄÙÅ¡Ö…Õïêπ…ï¡±Öçî°—È•πôºıëÖ—ï—•µîπ—•µïÈΩπîπ’—å§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å¡Ö…Õïêπ—•µïÕ—Öµ¿†§((ÄÄÄÅç…ïÖ—ïë}Ö–ÄÙÅ—•µïÕ—Öµ¿°…Ö›}ç…ïÖ—ïê§(ÄÄÄÅôÖ±±âÖç≠}Ö–ÄÙÅ—•µïÕ—Öµ¿°ôÖ±±âÖç¨§(ÄÄÄÅ…Ö›}•êÄÙÅÕ—»°Õ—Öâ±ï}•êÅΩ»Äàà§(ÄÄÄÅπ’µï…•ç}•êÄÙÅ•π–°…Ö›}•ê§Å•òÅ…Ö›}•êπ•Õë•ù•–†§Åï±ÕîÄ¥ƒ(ÄÄÄÅ…ï—’…∏Ä†(ÄÄÄÄÄÄÄÄƒÅ•òÅç…ïÖ—ïë}Ö–Å•ÃÅπΩ–Å9ΩπîÅï±ÕîÄ¿∞(ÄÄÄÄÄÄÄÅç…ïÖ—ïë}Ö–Å•òÅç…ïÖ—ïë}Ö–Å•ÃÅπΩ–Å9ΩπîÅï±ÕîÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅôÖ±±âÖç≠}Ö–Å•òÅôÖ±±âÖç≠}Ö–Å•ÃÅπΩ–Å9ΩπîÅï±ÕîÅô±ΩÖ–†àµ•πòà§(ÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÅπ’µï…•ç}•ê∞(ÄÄÄÄÄÄÄÅ…Ö›}•ê∞(ÄÄÄÄ§(()ëïòÅ}›ïëΩô}ïôôïç—•Ÿï}ô’πë•πù}Õ—Ö—’Ã†(ÄÄÄÄÄÄÄÅÕ—Ö—’ÕïÃ∞Ä®∞ÅôÖ±±âÖç≠}Õ—Ö—’ÃÙàà∞ÅôÖ±±âÖç≠}ôΩ±ëï…}•êÙàà§Ë(ÄÄÄÄààâIï—•ïπ–Åï·ç±’Õ•Ÿïµïπ–Å∞ü•—Ö–Åë‘ÅëΩÕÕ•ï»Å]=ÅçÀß§Å±îÅ¡±’ÃÅÀ•çïµµïπ–∏ààà(ÄÄÄÅç±ïÖ∏ÄÙÅl(ÄÄÄÄÄÄÄÄ°…ïçïπç‰∞ÅÕ—»°Õ—Ö—’ÃÅΩ»Äàà§πÕ—…•¿†§∞ÅÕ—»°Õ—Öâ±ï}•êÅΩ»Äàà§§(ÄÄÄÄÄÄÄÅôΩ»Å…ïçïπç‰∞ÅÕ—Ö—’Ã∞ÅÕ—Öâ±ï}•êÅ•∏ÅÕ—Ö—’ÕïÃ(ÄÄÄÅt(ÄÄÄÅ•òÅπΩ–Åç±ïÖ∏Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Äàà(ÄÄÄÅ|∞ÅÕ—Ö—’Ã∞ÅÕ—Öâ±ï}•êÄÙÅµÖ‡°ç±ïÖ∏∞Å≠ï‰ı±ÖµâëÑÅ•—ï¥ËÅ•—ïµl¡t§(ÄÄÄÅ•òÄ°πΩ–ÅÕ—Ö—’Ã(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅÕ—»°ôÖ±±âÖç≠}Õ—Ö—’ÃÅΩ»Äàà§πÕ—…•¿†§ÄÙÙÄâ…ïô’Õïîà(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÄ°Õ—Öâ±ï}•êÄÙÙÅÕ—»°ôÖ±±âÖç≠}ôΩ±ëï…}•êÅΩ»Äàà§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ»Å±ï∏°ç±ïÖ∏§ÄÙÙÄƒ§§Ë(ÄÄÄÄÄÄÄÄåÅ1îÅ¡ÖÂ±ΩÖêÅÅÅŸÖ±•ëÖ—ïëÅÄÅ¡ï’–Å¡ï…ë…îÅÕΩ∏Å°•Õ—Ω…•≈’îÅÖ¡À°ÃÅ’∏Å…ïô’Ã∏(ÄÄÄÄÄÄÄÄåÅΩπÕï…Ÿï»ÅÖ±Ω…ÃÅ±ÑÅëï…πß°…îÅ¡…ï’ŸîÅ¡ï…Õ•Õ”•îÅëîÅçîÅ∑©µîÅëΩÕÕ•ï»∏(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÕ—»°ôÖ±±âÖç≠}Õ—Ö—’ÃÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ…ï—’…∏ÅÕ—Ö—’Ã(()ëïòÅ}›ïëΩô}ô’πë•πù}Õ—Ö—’ÕïÕ}âÂ}çΩπ—Öç–°ëÖ—Ñ∞Ä®∞Å•πç±’ëï}ç¡òıÖ±Õî§Ë(ÄÄÄÄààâÖ±ç’±îÅ±ïÃÅÕ—Ö—’—ÃÅPÅï–ÅAÅï∏Å’∏ÅÕï’∞Å¡Ö…çΩ’…ÃÅë‘ÅçÖç°îÅ]=∏((ÄÄÄÅ0ùÖπç•ïππîÅ•µ¡≥•µïπ—Ö—•Ω∏Å…ï±•ÕÖ•–Åï–Åì•çΩëÖ•–Å±ÑÅ—Öâ±îÅçΩµ¡≥°—îÅ¡Ω’»Åç°Ö≈’î(ÄÄÄÅçΩπ—Öç–∏Åï——îÅôΩπç—•Ω∏ÅçΩπÕ—…’•–ÅêùÖâΩ…êÅ’∏Å•πëï‡ÅëïÃÅçΩπ—Öç—ÃÅ¡Ö»Å•ëïπ—•”§∞(ÄÄÄÅ¡’•ÃÅπîÅì•çΩëîÅç°Ö≈’îÅëΩÕÕ•ï»Å]=Å≈‘ù’πîÅÕï’±îÅôΩ•Ã∏(ÄÄÄÄààà(ÄÄÄÅù±ΩâÖ∞Å}]=}U9%9}!}P∞Å}]=}U9%9}!}-d(ÄÄÄÅù±ΩâÖ∞Å}]=}U9%9}!}Y1U∞Å}]=}A}MQQ}!}Y1U(ÄÄÄÅ•òÅπΩ–ÅΩÃπ¡Ö—†πï·•Õ—Ã°}›ïëΩô}ëâ}¡Ö—††§§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Ä°ÌÙ∞ÅÌÙ§Å•òÅ•πç±’ëï}ç¡òÅï±ÕîÅÌÙ((ÄÄÄÅçΩπ—Öç—ÃÄÙÅëÖ—Ñπùï–†âç…µ}çΩπ—Öç—Ãà∞Åmt§(ÄÄÄÅ•ëïπ—•—Â}Õ•ùπÖ—’…îÄÙÅ—’¡±î†(ÄÄÄÄÄÄÄÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†â•êà§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ}›ïëΩô}πΩ…µÖ±•Èï}πÖµî°çΩπ—Öç–πùï–†â¡…ïπΩ¥à§§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ}›ïëΩô}πΩ…µÖ±•Èï}πÖµî°çΩπ—Öç–πùï–†âπΩ¥à§§∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅôΩ»ÅçΩπ—Öç–Å•∏ÅçΩπ—Öç—Ã(ÄÄÄÄ§(ÄÄÄÅçÖç°ï}≠ï‰ÄÙÄ°}›ïëΩô}ëâ}Õ•ùπÖ—’…î†§∞Å•ëïπ—•—Â}Õ•ùπÖ—’…î§(ÄÄÄÅ›•—†Å}]=}U9%9}!}1=,Ë(ÄÄÄÄÄÄÄÅ•òÄ°}]=}U9%9}!}Y1UÅ•ÃÅπΩ–Å9Ωπî(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅ}]=}U9%9}!}-dÄÙÙÅçÖç°ï}≠ï‰(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅ—•µîπµΩπΩ—Ωπ•å†§Ä¥Å}]=}U9%9}!}PÄÄÃ¿¿§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅô’πë•πúÄÙÅë•ç–°}]=}U9%9}!}Y1U§(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Ä°ô’πë•πú∞Åë•ç–°}]=}A}MQQ}!}Y1U§§Å•òÅ•πç±’ëï}ç¡òÅï±ÕîÅô’πë•πú((ÄÄÄÅ≠πΩ›π}çΩπ—Öç—}•ëÃÄÙÅÏ(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†â•êà§§ÅôΩ»ÅçΩπ—Öç–Å•∏ÅçΩπ—Öç—ÃÅ•òÅçΩπ—Öç–πùï–†â•êà§(ÄÄÄÅÙ(ÄÄÄÅçΩπ—Öç—Õ}âÂ}πÖµîÄÙÅÌÙ(ÄÄÄÅôΩ»ÅçΩπ—Öç–Å•∏ÅçΩπ—Öç—ÃË(ÄÄÄÄÄÄÄÅ≠ï‰ÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅ}›ïëΩô}πΩ…µÖ±•Èï}πÖµî°çΩπ—Öç–πùï–†â¡…ïπΩ¥à§§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ}›ïëΩô}πΩ…µÖ±•Èï}πÖµî°çΩπ—Öç–πùï–†âπΩ¥à§§∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ•òÅÖ±∞°≠ï‰§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—Õ}âÂ}πÖµîπÕï—ëïôÖ’±–°≠ï‰∞Åmt§πÖ¡¡ïπê°Õ—»°çΩπ—Öç–πùï–†â•êà§§§((ÄÄÄÅÕ—Ö—’ÕïÕ}âÂ}çΩπ—Öç–ÄÙÅÌÙ(ÄÄÄÅ±Ö—ïÕ—}ç¡ô}âÂ}çΩπ—Öç–ÄÙÅÌÙ(ÄÄÄÅ›•—†Å}›ïëΩô}çΩππïç–†§ÅÖÃÅëàË(ÄÄÄÄÄÄÄÅ…Ω›ÃÄÙÅëàπï·ïç’—î†ààà(ÄÄÄÄÄÄÄÄÄÄÄÅM1PÅ»πÕ—Öâ±ï}•ê∞Å»π¡ÖÂ±ΩÖë}©ÕΩ∏∞Å»π…ïµΩ—ï}ëÖ—î∞Å»πÕÂπçïë}Ö–∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ∞πçΩπ—Öç—}•êÅLÅ±•π≠ïë}çΩπ—Öç—}•ê(ÄÄÄÄÄÄÄÄÄÄÄÅI=4Å›ïëΩô}…ïÕΩ’…çïÃÅ»Å1PÅ)=%8Å›ïëΩô}çΩπ—Öç—}±•π≠ÃÅ∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÅ=8Å»π…ïÕΩ’…çï}—Â¡îı∞π…ïÕΩ’…çï}—Â¡îÅ9Å»πÕ—Öâ±ï}•êı∞π…ïÕΩ’…çï}•ê(ÄÄÄÄÄÄÄÄÄÄÄÅ=IHÅ	dÅ»πÕÂπçïë}Ö–ÅM∞Å»πÕ—Öâ±ï}•êÅM(ÄÄÄÄÄÄÄÄààà§((ÄÄÄÄÄÄÄÄåÅ%”•…ï»ÅÕ’»Å±îÅç’…Õï’»Å¡±’”—–Å≈’îÅôï—ç°Ö±∞†§ÅùÖ…ëîÅ’∏ÅÕï’∞Åù…ΩÃÅ)M=8(ÄÄÄÄÄÄÄÄåÅ]=Åï∏Å∑•µΩ•…îÉÄÅ±ÑÅôΩ•Ã∞Å∑©µîÅÖŸïåÅ¡±’Õ•ï’…ÃÅµ•±±•ï…ÃÅëîÅëΩÕÕ•ï…Ã∏(ÄÄÄÄÄÄÄÅôΩ»Å…Ω‹Å•∏Å…Ω›ÃË(ÄÄÄÄÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ©ÕΩ∏π±ΩÖëÃ°…Ω›lâ¡ÖÂ±ΩÖë}©ÕΩ∏ât§(ÄÄÄÄÄÄÄÄÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»∞Å©ÕΩ∏π)M=9ïçΩëï……Ω»§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¿π±Ωùùï»π›Ö…π•πú†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâΩÕÕ•ï»Å]=Å•ùπΩÀ§ÄËÅ)M=8Å±ΩçÖ∞Å•±±•Õ•â±îÄ†ïÃ§à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…Ω›lâÕ—Öâ±ï}•êât∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÄÄÄÄÅô’πë•πù}Õ—Ö—’ÃÄÙÅ}›ïëΩô}ô…Öπçï}—…ÖŸÖ•±}Õ—Ö—’Ã°¡ÖÂ±ΩÖê§(ÄÄÄÄÄÄÄÄÄÄÄÅç¡ô}Õ—Ö—îÄÙÅ}›ïëΩô}πΩ…µÖ±•Èï}çΩëî†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêπùï–†âÕ—Ö—îà§ÅΩ»Å¡ÖÂ±ΩÖêπùï–†âÕ—Ö—’Ãà§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ»Å¡ÖÂ±ΩÖêπùï–†â…ïù•Õ—…Ö—•ΩπM—Ö—îà§(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïçïπç‰ÄÙÅ}›ïëΩô}ôΩ±ëï…}…ïçïπçÂ}≠ï‰†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅôÖ±±âÖç¨ı…Ω›lâ…ïµΩ—ï}ëÖ—îâtÅΩ»Å…Ω›lâÕÂπçïë}Ö–ât∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—Öâ±ï}•êı…Ω›lâÕ—Öâ±ï}•êât∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅ—Ö…ùï—}•ëÃÄÙÅÕï–†§(ÄÄÄÄÄÄÄÄÄÄÄÅ±•π≠ïë}çΩπ—Öç—}•êÄÙÅÕ—»°…Ω›lâ±•π≠ïë}çΩπ—Öç—}•êâtÅΩ»Äàà§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ±•π≠ïë}çΩπ—Öç—}•êÅ•∏Å≠πΩ›π}çΩπ—Öç—}•ëÃË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—Ö…ùï—}•ëÃπÖëê°±•π≠ïë}çΩπ—Öç—}•ê§(ÄÄÄÄÄÄÄÄÄÄÄÅ—Ö…ùï—}•ëÃπ’¡ëÖ—î°çΩπ—Öç—Õ}âÂ}πÖµîπùï–°}›ïëΩô}Ö——ïπëïï}πÖµî°¡ÖÂ±ΩÖê§∞Åmt§§(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»Å—Ö…ùï—}•êÅ•∏Å—Ö…ùï—}•ëÃË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö—’ÕïÕ}âÂ}çΩπ—Öç–πÕï—ëïôÖ’±–°—Ö…ùï—}•ê∞Åmt§πÖ¡¡ïπê†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ°…ïçïπç‰∞Åô’πë•πù}Õ—Ö—’Ã∞Å…Ω›lâÕ—Öâ±ï}•êât§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ±Ö—ïÕ—}ç¡òÄÙÅ±Ö—ïÕ—}ç¡ô}âÂ}çΩπ—Öç–πùï–°—Ö…ùï—}•ê§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ±Ö—ïÕ—}ç¡òÅ•ÃÅ9ΩπîÅΩ»Å…ïçïπç‰Ä¯Å±Ö—ïÕ—}ç¡ôl¡tË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ±Ö—ïÕ—}ç¡ô}âÂ}çΩπ—Öç—m—Ö…ùï—}•ëtÄÙÄ°…ïçïπç‰∞Åç¡ô}Õ—Ö—î§((ÄÄÄÅçΩπ—Öç—Õ}âÂ}•êÄÙÅÏ(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†â•êà§ÅΩ»Äàà§ËÅçΩπ—Öç–ÅôΩ»ÅçΩπ—Öç–Å•∏ÅçΩπ—Öç—Ã(ÄÄÄÅÙ(ÄÄÄÅ…ïÕ’±–ÄÙÅÌÙ(ÄÄÄÅôΩ»ÅçΩπ—Öç—}•ê∞ÅÕ—Ö—’ÕïÃÅ•∏ÅÕ—Ö—’ÕïÕ}âÂ}çΩπ—Öç–π•—ïµÃ†§Ë(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÄÙÅçΩπ—Öç—Õ}âÂ}•êπùï–°çΩπ—Öç—}•ê∞ÅÌÙ§(ÄÄÄÄÄÄÄÅ…ïÕ’±—mçΩπ—Öç—}•ëtÄÙÅ}›ïëΩô}ïôôïç—•Ÿï}ô’πë•πù}Õ—Ö—’Ã†(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö—’ÕïÃ∞(ÄÄÄÄÄÄÄÄÄÄÄÅôÖ±±âÖç≠}Õ—Ö—’ÃıçΩπ—Öç–πùï–†âÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–à§∞(ÄÄÄÄÄÄÄÄÄÄÄÅôÖ±±âÖç≠}ôΩ±ëï…}•êıçΩπ—Öç–πùï–†âÕΩ’…çï}›ïëΩô}ôΩ±ëï…}•êà§∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅç¡ô}Õ—Ö—ïÃÄÙÅÏ(ÄÄÄÄÄÄÄÅçΩπ—Öç—}•êËÅÕ—Ö—îÅôΩ»ÅçΩπ—Öç—}•ê∞Ä°|∞ÅÕ—Ö—î§Å•∏Å±Ö—ïÕ—}ç¡ô}âÂ}çΩπ—Öç–π•—ïµÃ†§(ÄÄÄÅÙ(ÄÄÄÅ›•—†Å}]=}U9%9}!}1=,Ë(ÄÄÄÄÄÄÄÅ}]=}U9%9}!}-dÄÙÄ°}›ïëΩô}ëâ}Õ•ùπÖ—’…î†§∞Å•ëïπ—•—Â}Õ•ùπÖ—’…î§(ÄÄÄÄÄÄÄÅ}]=}U9%9}!}Y1UÄÙÅë•ç–°…ïÕ’±–§(ÄÄÄÄÄÄÄÅ}]=}A}MQQ}!}Y1UÄÙÅë•ç–°ç¡ô}Õ—Ö—ïÃ§(ÄÄÄÄÄÄÄÅ}]=}U9%9}!}PÄÙÅ—•µîπµΩπΩ—Ωπ•å†§(ÄÄÄÅ…ï—’…∏Ä°…ïÕ’±–∞Åç¡ô}Õ—Ö—ïÃ§Å•òÅ•πç±’ëï}ç¡òÅï±ÕîÅ…ïÕ’±–(()ëïòÅ}›ïëΩô}ç¡ô}Õ—Ö—ïÕ}âÂ}çΩπ—Öç–°ëÖ—Ñ§Ë(ÄÄÄÄààâ·¡ΩÕîÅ±îÅÕ—Ö—’–ÅAÅ±ΩçÖ∞ÅÕÖπÃÅ…ïπë…îÅ±ïÃÅ±ïç—’…ïÃÅI4Åì•¡ïπëÖπ—ïÃÅë‘ÅçÖç°î∏ààà(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ|∞ÅÕ—Ö—ïÃÄÙÅ}›ïëΩô}ô’πë•πù}Õ—Ö—’ÕïÕ}âÂ}çΩπ—Öç–°ëÖ—Ñ∞Å•πç±’ëï}ç¡òıQ…’î§(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÕ—Ö—ïÃ(ÄÄÄÅï·çï¡–Å·çï¡—•Ω∏ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅÖ¡¿π±Ωùùï»π›Ö…π•πú†â1ïç—’…îÅëïÃÅÕ—Ö—’—ÃÅAÅ•ùπΩÀ•îÄ†ïÃ§à∞Å—Â¡î°ï·å§π}}πÖµï}|§(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÌÙ(()ëïòÅ}›ïëΩô}Õ—Ö—’Õ}¡ÖÂ±ΩÖê°—ïÕ—}çΩππïç—•Ω∏ıQ…’î§Ë(ÄÄÄÅçΩπô•ù’…ïêÄÙÅâΩΩ∞°ΩÃπùï—ïπÿ†â]=}A%}-dà∞Äàà§πÕ—…•¿†§§(ÄÄÄÅçΩππïç—ïêÄÙÅÖ±Õî(ÄÄÄÅï……Ω»ÄÙÄàà(ÄÄÄÅ•òÅçΩπô•ù’…ïêÅÖπêÅ—ïÕ—}çΩππïç—•Ω∏Ë(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ}›ïëΩô}…ï≈’ïÕ–†àΩÖ¡§ΩΩ…ùÖπ•ÕµÃΩµîà§(ÄÄÄÄÄÄÄÄÄÄÄÅçΩππïç—ïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅï·çï¡–Å·çï¡—•Ω∏ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÅï……Ω»ÄÙÅ}›ïëΩô}ç±ïÖ∏°ï·å§(ÄÄÄÅÕ—Ö—îÄÙÅ}›ïëΩô}Õ—Ö—î†§(ÄÄÄÅ›•—†Å}›ïëΩô}çΩππïç–†§ÅÖÃÅëàË(ÄÄÄÄÄÄÄÅ…ïÕΩ’…çïÃÄÙÅëàπï·ïç’—î†âM1PÅ=U9P†®§ÅI=4Å›ïëΩô}…ïÕΩ’…çïÃà§πôï—ç°Ωπî†•l¡t(ÄÄÄÄÄÄÄÅ±•π≠ïêÄÙÅëàπï·ïç’—î†âM1PÅ=U9P†®§ÅI=4Å›ïëΩô}çΩπ—Öç—}±•π≠Ãà§πôï—ç°Ωπî†•l¡t(ÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄâçΩπô•ù’…ïêàËÅçΩπô•ù’…ïê∞ÄâçΩππïç—ïêàËÅçΩππïç—ïê∞(ÄÄÄÄÄÄÄÄâ±ÖÕ—}ÕÂπç}Ö–àËÅÕ—Ö—îπùï–†â±ÖÕ—}ÕÂπç}Ö–à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄâ…ïÕΩ’…çï}çΩ’π–àËÅ…ïÕΩ’…çïÃ∞Äâ±•π≠ïë}ôΩ±ëï…}çΩ’π–àËÅ±•π≠ïê∞(ÄÄÄÄÄÄÄÄâï……Ω»àËÅï……Ω»ÅΩ»Å}›ïëΩô}ç±ïÖ∏°Õ—Ö—îπùï–†â±ÖÕ—}ï……Ω»à∞Äàà§§∞(ÄÄÄÅÙ(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥Ω›ïëΩòΩÕ—Ö—’Ãà§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}›ïëΩô}Õ—Ö—’Ã†§Ë(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°}›ïëΩô}Õ—Ö—’Õ}¡ÖÂ±ΩÖê†§§(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥Ω›ïëΩòΩÕÂπåà∞Åµï—°ΩëÃılâA=MPât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}›ïëΩô}ÕÂπå†§Ë(ÄÄÄÅ•òÄ°ç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ§πùï–†â…Ω±îà§ÄÑÙÄâÖëµ•∏àË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâMï’∞Å’∏ÅÖëµ•π•Õ—…Ö—ï’»Å¡ï’–ÅÕÂπç°…Ωπ•Õï»Å]=∏âÙ§∞Ä–¿Ã(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°}›ïëΩô}ÕÂπå†§§(ÄÄÄÅï·çï¡–Å]ïëΩôA%……Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÅ}›ïëΩô}ç±ïÖ∏°ï·å•Ù§∞Ä‘¿Ã(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯Ω›ïëΩòà§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çΩπ—Öç—}›ïëΩò°çΩπ—Öç—}•ê§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅ•òÅπΩ–Å}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄâ…ïÕΩ’…çïÃàËÅ}›ïëΩô}çΩπ—Öç—}…ïÕΩ’…çïÃ°çΩπ—Öç—}•ê∞ÅëÖ—Ñ§∞(ÄÄÄÄÄÄÄÄâÕ—Ö—’ÃàËÅ}›ïëΩô}Õ—Ö—’Õ}¡ÖÂ±ΩÖê°—ïÕ—}çΩππïç—•Ω∏ıÖ±Õî§∞(ÄÄÄÅÙ§(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯Ω›ïëΩòΩ…ïô…ïÕ†à∞Åµï—°ΩëÃılâA=MPât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çΩπ—Öç—}›ïëΩô}…ïô…ïÕ†°çΩπ—Öç—}•ê§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅ•òÅπΩ–Å}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅÕÂπåÄÙÅ}›ïëΩô}…ïô…ïÕ°}çΩπ—Öç—}…ïÕΩ’…çî°çΩπ—Öç—}•ê∞ÅëÖ—Ñ§(ÄÄÄÄÄÄÄÅ…ïô…ïÕ°ïë}ëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâÕÂπåàËÅÕÂπå∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ïÕΩ’…çïÃàËÅ}›ïëΩô}çΩπ—Öç—}…ïÕΩ’…çïÃ°çΩπ—Öç—}•ê∞Å…ïô…ïÕ°ïë}ëÖ—Ñ§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—Öç–àËÅ}ç…µ}çΩπ—Öç–°…ïô…ïÕ°ïë}ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§∞(ÄÄÄÄÄÄÄÅÙ§(ÄÄÄÅï·çï¡–Å]ïëΩôA%……Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÅ}›ïëΩô}ç±ïÖ∏°ï·å•Ù§∞Ä‘¿Ã(()Ö¡¿π…Ω’—î†(ÄÄÄÄàΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯Ω›ïëΩòΩ…ïô…ïÕ†µΩ∏µΩ¡ï∏à∞(ÄÄÄÅµï—°ΩëÃılâA=MPât∞(§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çΩπ—Öç—}›ïëΩô}…ïô…ïÕ°}Ωπ}Ω¡ï∏°çΩπ—Öç—}•ê§Ë(ÄÄÄÄààâç—’Ö±•ÕîÅÖ‘Å¡±’ÃÅ’∏ÅëΩÕÕ•ï»ÅçΩππ‘∞ÅÖ‘ÅµÖ·•µ’¥Å’πîÅôΩ•ÃÅ¡Ö»Åëïµ§µ°ï’…î∏ààà(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅ•òÅπΩ–Å}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅÕÂπåÄÙÅ}›ïëΩô}…ïô…ïÕ°}çΩπ—Öç—}…ïÕΩ’…çî†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—}•ê∞ÅëÖ—Ñ∞ÅÖ’—ΩµÖ—•åıQ…’î∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ…ïô…ïÕ°ïë}ëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâÕÂπåàËÅÕÂπå∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ïÕΩ’…çïÃàËÅ}›ïëΩô}çΩπ—Öç—}…ïÕΩ’…çïÃ°çΩπ—Öç—}•ê∞Å…ïô…ïÕ°ïë}ëÖ—Ñ§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—Öç–àËÅ}ç…µ}çΩπ—Öç–°…ïô…ïÕ°ïë}ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§∞(ÄÄÄÄÄÄÄÅÙ§(ÄÄÄÅï·çï¡–Å]ïëΩôA%……Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅÕ—Ö—’Õ}çΩëîÄÙÄ–»‰Å•òÅùï—Ö——»°ï·å∞ÄâÕ—Ö—’Õ}çΩëîà∞Å9Ωπî§ÄÙÙÄ–»‰Åï±ÕîÄ‘¿Ã(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÅ}›ïëΩô}ç±ïÖ∏°ï·å•Ù§∞ÅÕ—Ö—’Õ}çΩëî(()ëïòÅ}›ïëΩô}›ïâ°ΩΩ≠}Ö’—°ïπ—•çÖ—ïê°…Ö›}âΩë‰§Ë(ÄÄÄÅÕïç…ï—}—ï·–ÄÙÄ°ΩÃπùï—ïπÿ†â]=}]	!==-}MIPà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–ÅÕïç…ï—}—ï·–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅÕ•ùπÖ—’…îÄÙÄ°…ï≈’ïÕ–π°ïÖëï…Ãπùï–†â`µ]ïëΩòµM•ùπÖ—’…îà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅÕ•ùπÖ—’…îË(ÄÄÄÄÄÄÄÅÕ’¡¡±•ïêÄÙÅÕ•ùπÖ—’…îπÕ¡±•–†àÙà∞Äƒ•l¥≈tπÕ—…•¿†§πÕ—…•¿†úàú§πÕ—…•¿†àúà§(ÄÄÄÄÄÄÄÄåÅ]=ÅÕ•ùπîÅΩôô•ç•ï±±ïµïπ–Å±îÅçΩ…¡ÃÅâ…’–Åï∏Å!5µM!‘ƒ»Å°ï·Öì•ç•µÖ∞∏(ÄÄÄÄÄÄÄÄåÅ1ïÃÅŸÖ…•Öπ—ïÃÅM!»‘ÿÅ…ïÕ—ïπ–ÅÖççï¡”•ïÃÅ¡ïπëÖπ–Å±ÑÅ—…ÖπÕ•—•Ω∏Å¡Ω’»Åπî(ÄÄÄÄÄÄÄÄåÅ¡ÖÃÅçÖÕÕï»Å’∏É•Ÿïπ—’ï∞É•µï——ï’»Å•π—ï…πîÅ°•Õ—Ω…•≈’î∏(ÄÄÄÄÄÄÄÅçÖπë•ëÖ—ïÃÄÙÅmt(ÄÄÄÄÄÄÄÅôΩ»ÅÖ±ùΩ…•—°¥Å•∏Ä°°ÖÕ°±•àπÕ°Ñ‘ƒ»∞Å°ÖÕ°±•àπÕ°Ñ»‘ÿ§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅë•ùïÕ—}âÂ—ïÃÄÙÅ°µÖåππï‹†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕïç…ï—}—ï·–πïπçΩëî†â’—ò¥‡à§∞Å…Ö›}âΩë‰∞ÅÖ±ùΩ…•—°¥∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§πë•ùïÕ–†§(ÄÄÄÄÄÄÄÄÄÄÄÅçÖπë•ëÖ—ïÃπï·—ïπê††(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅë•ùïÕ—}âÂ—ïÃπ°ï‡†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅë•ùïÕ—}âÂ—ïÃπ°ï‡†§π’¡¡ï»†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅâÖÕîÿ–πàÿ—ïπçΩëî°ë•ùïÕ—}âÂ—ïÃ§πëïçΩëî†âÖÕç•§à§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅâÖÕîÿ–π’…±ÕÖôï}àÿ—ïπçΩëî°ë•ùïÕ—}âÂ—ïÃ§πëïçΩëî†âÖÕç•§à§π…Õ—…•¿†àÙà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§§(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖπ‰°°µÖåπçΩµ¡Ö…ï}ë•ùïÕ–°Õ’¡¡±•ïê∞ÅçÖπë•ëÖ—î§ÅôΩ»ÅçÖπë•ëÖ—îÅ•∏ÅçÖπë•ëÖ—ïÃ§(ÄÄÄÅÕ’¡¡±•ïë}Õïç…ï–ÄÙÄ†(ÄÄÄÄÄÄÄÅ…ï≈’ïÕ–π°ïÖëï…Ãπùï–†â`µ]ïëΩòµMïç…ï–à§(ÄÄÄÄÄÄÄÅΩ»Å…ï≈’ïÕ–π°ïÖëï…Ãπùï–†â`µ]ïâ°ΩΩ¨µMïç…ï–à§(ÄÄÄÄÄÄÄÅΩ»Äàà(ÄÄÄÄ§πÕ—…•¿†§(ÄÄÄÅÖ’—°Ω…•ÈÖ—•Ω∏ÄÙÄ°…ï≈’ïÕ–π°ïÖëï…Ãπùï–†â’—°Ω…•ÈÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅÖ’—°Ω…•ÈÖ—•Ω∏πçÖÕïôΩ±ê†§πÕ—Ö…—Õ›•—††ââïÖ…ï»Äà§Ë(ÄÄÄÄÄÄÄÅÕ’¡¡±•ïë}Õïç…ï–ÄÙÅÖ’—°Ω…•ÈÖ—•Ωπl‹ÈtπÕ—…•¿†§(ÄÄÄÅ…ï—’…∏ÅâΩΩ∞°Õ’¡¡±•ïë}Õïç…ï–§ÅÖπêÅ°µÖåπçΩµ¡Ö…ï}ë•ùïÕ–†(ÄÄÄÄÄÄÄÅÕ’¡¡±•ïë}Õïç…ï–∞ÅÕïç…ï—}—ï·–∞(ÄÄÄÄ§(()ëïòÅ}›ïëΩô}›ïâ°ΩΩ≠}ôΩ±ëï»°¡ÖÂ±ΩÖê§Ë(ÄÄÄÄààâQ…Ω’ŸîÅ’∏ÅëΩÕÕ•ï»ÅçΩµ¡±ï–Å•πç±’ÃÅëÖπÃÅ∞ü•€•πïµïπ–∞ÅÕÖπÃÅÖ¡¡ï∞Åë•Õ—Öπ–∏ààà(ÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°¡ÖÂ±ΩÖê∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(ÄÄÄÅ•òÅ¡ÖÂ±ΩÖêπùï–†âï·—ï…πÖ±%êà§ÅÖπêÅÖπ‰†(ÄÄÄÄÄÄÄÄÄÄÄÅ≠ï‰Å•∏Å¡ÖÂ±ΩÖêÅôΩ»Å≠ï‰Å•∏Ä†âÕ—Ö—îà∞ÄâÖ——ïπëïîà∞Äâ—…Ö•π•πùç—•Ωπ%πôºà§§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å¡ÖÂ±ΩÖê(ÄÄÄÅôΩ»Å≠ï‰Å•∏Ä†â…ïù•Õ—…Ö—•ΩπΩ±ëï»à∞ÄâôΩ±ëï»à∞Äâ…ïÕΩ’…çîà∞ÄâëÖ—Ñà∞Äâ¡ÖÂ±ΩÖêà§Ë(ÄÄÄÄÄÄÄÅçÖπë•ëÖ—îÄÙÅ}›ïëΩô}›ïâ°ΩΩ≠}ôΩ±ëï»°¡ÖÂ±ΩÖêπùï–°≠ï‰§§(ÄÄÄÄÄÄÄÅ•òÅçÖπë•ëÖ—îË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅçÖπë•ëÖ—î(ÄÄÄÅ…ï—’…∏Å9Ωπî(()ëïòÅ}›ïëΩô}›ïâ°ΩΩ≠}ôΩ±ëï…}•ê°¡ÖÂ±ΩÖê§Ë(ÄÄÄÅôΩ±ëï»ÄÙÅ}›ïëΩô}›ïâ°ΩΩ≠}ôΩ±ëï»°¡ÖÂ±ΩÖê§(ÄÄÄÅ•òÅôΩ±ëï»Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÕ—»°ôΩ±ëï»πùï–†âï·—ï…πÖ±%êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°¡ÖÂ±ΩÖê∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Äàà(ÄÄÄÅôΩ»Å≠ï‰Å•∏Ä†(ÄÄÄÄÄÄÄÄâï·—ï…πÖ±%êà∞ÄâôΩ±ëï…%êà∞Äâ…ïù•Õ—…Ö—•ΩπΩ±ëï…%êà∞(ÄÄÄÄÄÄÄÄâ…ïù•Õ—…Ö—•Ωπ}ôΩ±ëï…}•êà∞Äâ…ïÕΩ’…çï%êà∞ÄâëΩÕÕ•ï…%êà∞(ÄÄÄÄ§Ë(ÄÄÄÄÄÄÄÅŸÖ±’îÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–°≠ï‰§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅŸÖ±’îË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅŸÖ±’î(ÄÄÄÅôΩ»Å≠ï‰Å•∏Ä†â…ïù•Õ—…Ö—•ΩπΩ±ëï»à∞ÄâôΩ±ëï»à∞Äâ…ïÕΩ’…çîà∞ÄâëÖ—Ñà∞Äâ¡ÖÂ±ΩÖêà§Ë(ÄÄÄÄÄÄÄÅŸÖ±’îÄÙÅ}›ïëΩô}›ïâ°ΩΩ≠}ôΩ±ëï…}•ê°¡ÖÂ±ΩÖêπùï–°≠ï‰§§(ÄÄÄÄÄÄÄÅ•òÅŸÖ±’îË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅŸÖ±’î(ÄÄÄÅ…ï—’…∏Äàà(()Ö¡¿π¡ΩÕ–†àΩÖ¡§Ω›ïâ°ΩΩ≠ÃΩ›ïëΩòà§)ëïòÅç…µ}›ïëΩô}›ïâ°ΩΩ¨†§Ë(ÄÄÄÅ…Ö›}âΩë‰ÄÙÅ…ï≈’ïÕ–πùï—}ëÖ—Ñ°çÖç°îıQ…’î§ÅΩ»Åààà(ÄÄÄÅ•òÅπΩ–Ä°ΩÃπùï—ïπÿ†â]=}]	!==-}MIPà§ÅΩ»Äàà§πÕ—…•¿†§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâΩ¨àËÅÖ±Õî∞Äâï……Ω»àËÄâ›ïâ°ΩΩ≠}πΩ—}çΩπô•ù’…ïêâÙ§∞Ä‘¿Ã(ÄÄÄÅ•òÅπΩ–Å}›ïëΩô}›ïâ°ΩΩ≠}Ö’—°ïπ—•çÖ—ïê°…Ö›}âΩë‰§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâΩ¨àËÅÖ±Õî∞Äâï……Ω»àËÄâôΩ…â•ëëï∏âÙ§∞Ä–¿Ã(ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§(ÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°¡ÖÂ±ΩÖê∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâΩ¨àËÅÖ±Õî∞Äâï……Ω»àËÄâ•πŸÖ±•ë}¡ÖÂ±ΩÖêâÙ§∞Ä–¿¿(ÄÄÄÅëï±•Ÿï…Â}•êÄÙÄ†(ÄÄÄÄÄÄÄÅ…ï≈’ïÕ–π°ïÖëï…Ãπùï–†â`µ]ïëΩòµï±•Ÿï…‰à§(ÄÄÄÄÄÄÄÅΩ»Å°ÖÕ°±•àπÕ°Ñ»‘ÿ°…Ö›}âΩë‰§π°ï·ë•ùïÕ–†§(ÄÄÄÄ§πÕ—…•¿†•lË»¿¡t(ÄÄÄÅ›•—†Å}›ïëΩô}çΩππïç–†§ÅÖÃÅëàË(ÄÄÄÄÄÄÄÅë’¡±•çÖ—îÄÙÅëàπï·ïç’—î†(ÄÄÄÄÄÄÄÄÄÄÄÄâM1PÄƒÅI=4Å›ïëΩô}›ïâ°ΩΩ≠}ëï±•Ÿï…•ïÃÅ]!IÅëï±•Ÿï…Â}•êÙ¸à∞(ÄÄÄÄÄÄÄÄÄÄÄÄ°ëï±•Ÿï…Â}•ê∞§∞(ÄÄÄÄÄÄÄÄ§πôï—ç°Ωπî†§(ÄÄÄÅ•òÅë’¡±•çÖ—îË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâΩ¨àËÅQ…’î∞Äâë’¡±•çÖ—îàËÅQ…’ïÙ§∞Ä»¿¿((ÄÄÄÅôΩ±ëï»ÄÙÅ}›ïëΩô}›ïâ°ΩΩ≠}ôΩ±ëï»°¡ÖÂ±ΩÖê§(ÄÄÄÅôΩ±ëï…}•êÄÙÅ}›ïëΩô}›ïâ°ΩΩ≠}ôΩ±ëï…}•ê°¡ÖÂ±ΩÖê§(ÄÄÄÅÕΩ’…çîÄÙÄâ¡ÖÂ±ΩÖêà(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ•òÅôΩ±ëï»Å•ÃÅ9ΩπîÅÖπêÅôΩ±ëï…}•êË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïµΩ—ï}¡ÖÂ±ΩÖê∞Å}°ïÖëï…ÃÄÙÅ}›ïëΩô}…ï≈’ïÕ–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòàΩÖ¡§Ω…ïù•Õ—…Ö—•ΩπΩ±ëï…ÃΩÌ≈’Ω—î°ôΩ±ëï…}•ê∞ÅÕÖôîÙúú•Ùà(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ±ëï»ÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ïµΩ—ï}¡ÖÂ±ΩÖêπùï–†âëÖ—Ñà§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°…ïµΩ—ï}¡ÖÂ±ΩÖê∞Åë•ç–§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅ•Õ•πÕ—Öπçî°…ïµΩ—ï}¡ÖÂ±ΩÖêπùï–†âëÖ—Ñà§∞Åë•ç–§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅï±ÕîÅ…ïµΩ—ï}¡ÖÂ±ΩÖê(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅÕΩ’…çîÄÙÄâ—Ö…ùï—ïë}ùï–à(ÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°ôΩ±ëï»∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅôΩ±ëï…}•êÅÖπêÅπΩ–ÅôΩ±ëï»πùï–†âï·—ï…πÖ±%êà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅôΩ±ëï»ÄÙÅÏ®©ôΩ±ëï»∞Äâï·—ï…πÖ±%êàËÅôΩ±ëï…}•ëÙ(ÄÄÄÄÄÄÄÄÄÄÄÅ}›ïëΩô}Õ—Ω…ï}¡Öùî†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅmôΩ±ëï…t∞Å9Ωπî∞Ä¿∞Å’¡ëÖ—ï}ÕÂπç}Õ—Ö—îıÖ±Õî∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ›•—†Å}›ïëΩô}çΩππïç–†§ÅÖÃÅëàË(ÄÄÄÄÄÄÄÄÄÄÄÅëàπï·ïç’—î†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ%9MIPÅ=HÅ%9=IÅ%9Q<Å›ïëΩô}›ïâ°ΩΩ≠}ëï±•Ÿï…•ïÃÄà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄà°ëï±•Ÿï…Â}•ê∞Å¡…ΩçïÕÕïë}Ö–§ÅY1ULÄ†¸∞Ä¸§à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ°ëï±•Ÿï…Â}•ê∞Å}›ïëΩô}πΩ‹†§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâΩ¨àËÅQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡…ΩçïÕÕïêàËÅâΩΩ∞°•Õ•πÕ—Öπçî°ôΩ±ëï»∞Åë•ç–§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕΩ’…çîàËÅÕΩ’…çîÅ•òÅ•Õ•πÕ—Öπçî°ôΩ±ëï»∞Åë•ç–§Åï±ÕîÄâïŸïπ—}Ωπ±‰à∞(ÄÄÄÄÄÄÄÅÙ§∞Ä»¿¿(ÄÄÄÅï·çï¡–Å]ïëΩôA%……Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâΩ¨àËÅÖ±Õî∞Äâï……Ω»àËÅ}›ïëΩô}ç±ïÖ∏°ï·å•Ù§∞Ä‘¿Ã(()Ö¡¿π…Ω’—î†àΩç…¥à∞ÅëïôÖ’±—ÃıÏâÕïç—•Ω∏àËÄâÖçç’ï•∞âÙ§)Ö¡¿π…Ω’—î†àΩç…¥ºÒÕïç—•Ω∏¯à§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…¥°Õïç—•Ω∏§Ë(ÄÄÄÅ•òÅÕïç—•Ω∏ÅπΩ–Å•∏ÅI5}A}1	1LË(ÄÄÄÄÄÄÄÅÖâΩ…–†–¿–§(ÄÄÄÅ’Õï»ÄÙÅç’……ïπ—}’Õï»†§(ÄÄÄÄåÅÅâ…Ω›Õï»ÅÕïÕÕ•Ω∏ÅçÖ∏ÅΩ’—±•ŸîÅ—°îÅÖççΩ’π–ÅçΩπô•ù’…Ö—•Ω∏Å—°Ö–Åç…ïÖ—ïêÅ•–∏(ÄÄÄÄåÅŸΩ•êÅ¡ÖÕÕ•πúÅ9ΩπîÅ—ºÅ—°îÅ—ïµ¡±Ö—î∞Å›°ï…îÅ’Õï»ÅÖ——…•â’—ïÃÅâïçΩµîÅ)•π©Ñ(ÄÄÄÄåÅUπëïô•πïêÅΩâ©ïç—ÃÅ—°Ö–Å—°îÅI5}=9%Å)M=8ÅÕï…•Ö±•Èï»ÅçÖππΩ–ÅïπçΩëî∏(ÄÄÄÅ•òÅπΩ–Å’Õï»Ë(ÄÄÄÄÄÄÄÅÕïÕÕ•Ω∏π¡Ω¿†â’Õï…}ïµÖ•∞à∞Å9Ωπî§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å…ïë•…ïç–°’…±}ôΩ»†â±Ωù•∏à∞Åπï·–ı…ï≈’ïÕ–π¡Ö—†§§(ÄÄÄÅ…ï—’…∏Å…ïπëï…}—ïµ¡±Ö—î†(ÄÄÄÄÄÄÄÄâç…¥π°—µ∞à∞(ÄÄÄÄÄÄÄÅÕïç—•Ω∏ıÕïç—•Ω∏∞(ÄÄÄÄÄÄÄÅ¡Öùï}—•—±îıI5}A}1	1MmÕïç—•Ωπt∞(ÄÄÄÄÄÄÄÅÕ—Ö—’ÕïÃı}ç…µ}Õ—Ö—’ÕïÃ°±ΩÖë}ëÖ—Ñ†§§∞(ÄÄÄÄÄÄÄÅ’Õï»ı’Õï»∞(ÄÄÄÄÄÄÄÅç…µ}—ïÖ¥ıl(ÄÄÄÄÄÄÄÄÄÄÄÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâπÖµîàËÅµïµâï…lâπÖµîât∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâô•…Õ—}πÖµîàËÅµïµâï…lâô•…Õ—}πÖµîât∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâïµÖ•∞àËÅµïµâï…lâïµÖ•∞ât∞(ÄÄÄÄÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»Åµïµâï»Å•∏ÅUMILπŸÖ±’ïÃ†§(ÄÄÄÄÄÄÄÅt∞(ÄÄÄÄÄÄÄÅÖÕÕï—}Ÿï…Õ•Ω∏ıI5}MMQ}YIM%=8∞(ÄÄÄÄ§(()Ö¡¿πùï–†àΩÖ¡§Ωç…¥Ωï·¡Ω…—ÃºÒï·¡Ω…—}≠ï‰¯à§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}ï·¡Ω…—}ï·çï∞°ï·¡Ω…—}≠ï‰§Ë(ÄÄÄÄààâS•≥•ç°Ö…ùîÅ±ïÃÅ•πÕç…•—ÃÅêù’πîÅôΩ…µÖ—•Ω∏ÅëÖπÃÅ’∏Åç±ÖÕÕï’»Å·çï∞∏ààà(ÄÄÄÅ•òÅï·¡Ω…—}≠ï‰ÅπΩ–Å•∏ÅI5}aA=IQ}%9%Q%=9LË(ÄÄÄÄÄÄÄÅÖâΩ…–†–¿–§(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅΩ’—¡’–ÄÙÅâ’•±ë}ç…µ}ï·¡Ω…—}›Ω…≠âΩΩ¨°ëÖ—Ñπùï–†âç…µ}çΩπ—Öç—Ãà∞Åmt§∞Åï·¡Ω…—}≠ï‰§(ÄÄÄÅ…ïÕ¡ΩπÕîÄÙÅÕïπë}ô•±î†(ÄÄÄÄÄÄÄÅΩ’—¡’–∞(ÄÄÄÄÄÄÄÅÖÕ}Ö——Öç°µïπ–ıQ…’î∞(ÄÄÄÄÄÄÄÅëΩ›π±ΩÖë}πÖµîıç…µ}ï·¡Ω…—}ô•±ïπÖµî°ï·¡Ω…—}≠ï‰§∞(ÄÄÄÄÄÄÄÅµ•µï—Â¡îÙ†(ÄÄÄÄÄÄÄÄÄÄÄÄâÖ¡¡±•çÖ—•Ω∏ΩŸπêπΩ¡ïπ·µ±ôΩ…µÖ—ÃµΩôô•çïëΩç’µïπ–∏à(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ¡…ïÖëÕ°ïï—µ∞πÕ°ïï–à(ÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÅµÖ·}ÖùîÙ¿∞(ÄÄÄÄ§(ÄÄÄÅ…ïÕ¡ΩπÕîπ°ïÖëï…ÕlâÖç°îµΩπ—…Ω∞âtÄÙÄâ¡…•ŸÖ—î∞ÅπºµÕ—Ω…î∞ÅµÖ‡µÖùîÙ¿à(ÄÄÄÅ…ïÕ¡ΩπÕîπ°ïÖëï…Õlâ`µΩπ—ïπ–µQÂ¡îµ=¡—•ΩπÃâtÄÙÄâπΩÕπ•ôòà(ÄÄÄÅ…ï—’…∏Å…ïÕ¡ΩπÕî(()Ö¡¿π…Ω’—î†àΩI4à∞ÅëïôÖ’±—ÃıÏâÕïç—•Ω∏àËÄâÖçç’ï•∞âÙ§)Ö¡¿π…Ω’—î†àΩI4ºÒÕïç—•Ω∏¯à§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}’¡¡ï…çÖÕî°Õïç—•Ω∏§Ë(ÄÄÄÄààâAÀ•Õï…ŸîÅ±ïÃÅ±•ïπÃÅ°•Õ—Ω…•≈’ïÃÅ≈’§Å’—•±•Õïπ–Å±îÅç°ïµ•∏ÅI4Åï∏ÅµÖ©’Õç’±ïÃ∏ààà(ÄÄÄÅ—Ö…ùï–ÄÙÅ’…±}ôΩ»†âç…¥à∞ÅÕïç—•Ω∏ıÕïç—•Ω∏§(ÄÄÄÅ…ï—’…∏Å…ïë•…ïç–°òâÌ—Ö…ùï—Ù˝Ì…ï≈’ïÕ–π≈’ï…Â}Õ—…•πúπëïçΩëî†•ÙàÅ•òÅ…ï≈’ïÕ–π≈’ï…Â}Õ—…•πúÅï±ÕîÅ—Ö…ùï–§(()ëïòÅ}ç…µ}çÖ±ïπë±Â}Õ—Ö—’Õ}¡ÖÂ±ΩÖê°ëÖ—Ñ§Ë(ÄÄÄÅÕ—Ö—îÄÙÅëÖ—Ñπùï–†âç…µ}çÖ±ïπë±‰à§ÅΩ»ÅÌÙ(ÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄâçΩπô•ù’…ïêàËÅâΩΩ∞°}çÖ±ïπë±Â}—Ω≠ï∏†§§∞(ÄÄÄÄÄÄÄÄâÕ•ùπ•πù}≠ïÂ}çΩπô•ù’…ïêàËÅâΩΩ∞°}çÖ±ïπë±Â}Õ•ùπ•πù}≠ï‰†§§∞(ÄÄÄÄÄÄÄÄâçΩππïç—ïêàËÅâΩΩ∞°Õ—Ö—îπùï–†â›ïâ°ΩΩ≠}’…§à§§∞(ÄÄÄÄÄÄÄÄâÕçΩ¡îàËÅÕ—Ö—îπùï–†âÕçΩ¡îà§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄâÖççΩ’π—}πÖµîàËÅÕ—Ö—îπùï–†âÖççΩ’π—}πÖµîà§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄâÖççΩ’π—}ïµÖ•∞àËÅÕ—Ö—îπùï–†âÖççΩ’π—}ïµÖ•∞à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄâ±ÖÕ—}ÕÂπç}Ö–àËÅÕ—Ö—îπùï–†â±ÖÕ—}ÕÂπç}Ö–à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄâ±ÖÕ—}ô’±±}ÕÂπç}Ö–àËÅÕ—Ö—îπùï–†â±ÖÕ—}ô’±±}ÕÂπç}Ö–à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄâÕÂπç}çΩµ¡±ï—îàËÅâΩΩ∞°Õ—Ö—îπùï–†âÕÂπç}çΩµ¡±ï—îà§§∞(ÄÄÄÄÄÄÄÄâÕÂπç}•π}¡…Ωù…ïÕÃàËÅâΩΩ∞°Õ—Ö—îπùï–†âÕÂπç}ç’…ÕΩ»à§§ÅÖπêÅπΩ–ÅÕ—Ö—îπùï–†âÕÂπç}çΩµ¡±ï—îà§∞(ÄÄÄÅÙ(()ëïòÅ}çÖ±ïπë±Â}ç…ïÖ—ï}Ω…}…ï’Õï}›ïâ°ΩΩ¨°çΩπ—ï·–∞ÅÕçΩ¡î∞ÅçÖ±±âÖç≠}’…∞§Ë(ÄÄÄÅ¡Ö…ÖµÃÄÙÅÏ(ÄÄÄÄÄÄÄÄâΩ…ùÖπ•ÈÖ—•Ω∏àËÅçΩπ—ï·—lâΩ…ùÖπ•ÈÖ—•Ω∏ât∞(ÄÄÄÄÄÄÄÄâÕçΩ¡îàËÅÕçΩ¡î∞(ÄÄÄÄÄÄÄÄâçΩ’π–àËÄƒ¿¿∞(ÄÄÄÅÙ(ÄÄÄÅ•òÅÕçΩ¡îÄÙÙÄâ’Õï»àË(ÄÄÄÄÄÄÄÅ¡Ö…ÖµÕlâ’Õï»âtÄÙÅçΩπ—ï·—lâ’Õï»ât(ÄÄÄÅÕ’âÕç…•¡—•ΩπÃÄÙÅ}çÖ±ïπë±Â}¡Öù•πÖ—ïë}çΩ±±ïç—•Ω∏†(ÄÄÄÄÄÄÄÄàΩ›ïâ°ΩΩ≠}Õ’âÕç…•¡—•ΩπÃà∞(ÄÄÄÄÄÄÄÅ¡Ö…ÖµÃı¡Ö…ÖµÃ∞(ÄÄÄÄÄÄÄÅµÖ·}¡ÖùïÃÙƒ¿∞(ÄÄÄÄ§(ÄÄÄÅï·¡ïç—ïë}ïŸïπ—ÃÄÙÅÕï–°191e}]	!==-}Y9QL§(ÄÄÄÅôΩ»ÅÕ’âÕç…•¡—•Ω∏Å•∏ÅÕ’âÕç…•¡—•ΩπÃË(ÄÄÄÄÄÄÄÅ•òÅÕ’âÕç…•¡—•Ω∏πùï–†âçÖ±±âÖç≠}’…∞à§ÄÑÙÅçÖ±±âÖç≠}’…∞Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ•òÅÕ’âÕç…•¡—•Ω∏πùï–†âÕ—Ö—îà§ÄÙÙÄâÖç—•ŸîàÅÖπêÅï·¡ïç—ïë}ïŸïπ—Ãπ•ÕÕ’âÕï–†(ÄÄÄÄÄÄÄÄÄÄÄÅÕï–°Õ’âÕç…•¡—•Ω∏πùï–†âïŸïπ—Ãà§ÅΩ»Åmt§(ÄÄÄÄÄÄÄÄ§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅÕ’âÕç…•¡—•Ω∏(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅI’π—•µï……Ω»†(ÄÄÄÄÄÄÄÄÄÄÄÄâU∏ÅÖπç•ï∏Å›ïâ°ΩΩ¨ÅÖ±ïπë±‰Å’—•±•ÕîÅì•´ÄÅçï——îÅÖë…ïÕÕîÅµÖ•ÃÅ∏ùïÕ–Å¡ÖÃÅÖç—•òÅΩ‘Å•πçΩµ¡±ï–∏Äà(ÄÄÄÄÄÄÄÄÄÄÄÄâM’¡¡…•µïËµ±îÅëÖπÃÅÖ±ïπë±‰ÅÖŸÖπ–ÅëîÅ…ï±Öπçï»Å±ÑÅçΩπô•ù’…Ö—•Ω∏∏à(ÄÄÄÄÄÄÄÄ§((ÄÄÄÅâΩë‰ÄÙÅÏ(ÄÄÄÄÄÄÄÄâ’…∞àËÅçÖ±±âÖç≠}’…∞∞(ÄÄÄÄÄÄÄÄâïŸïπ—ÃàËÅ±•Õ–°191e}]	!==-}Y9QL§∞(ÄÄÄÄÄÄÄÄâΩ…ùÖπ•ÈÖ—•Ω∏àËÅçΩπ—ï·—lâΩ…ùÖπ•ÈÖ—•Ω∏ât∞(ÄÄÄÄÄÄÄÄâÕçΩ¡îàËÅÕçΩ¡î∞(ÄÄÄÄÄÄÄÄâÕ•ùπ•πù}≠ï‰àËÅ}çÖ±ïπë±Â}Õ•ùπ•πù}≠ï‰†§∞(ÄÄÄÅÙ(ÄÄÄÅ•òÅÕçΩ¡îÄÙÙÄâ’Õï»àË(ÄÄÄÄÄÄÄÅâΩëÂlâ’Õï»âtÄÙÅçΩπ—ï·—lâ’Õï»ât(ÄÄÄÅ…ï—’…∏Ä°}çÖ±ïπë±Â}…ï≈’ïÕ–†(ÄÄÄÄÄÄÄÄâA=MPà∞(ÄÄÄÄÄÄÄÄàΩ›ïâ°ΩΩ≠}Õ’âÕç…•¡—•ΩπÃà∞(ÄÄÄÄÄÄÄÅ©ÕΩπ}âΩë‰ıâΩë‰∞(ÄÄÄÄ§πùï–†â…ïÕΩ’…çîà§ÅΩ»ÅÌÙ§(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçÖ±ïπë±‰ΩÕ—Ö—’Ãà§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çÖ±ïπë±Â}Õ—Ö—’Ã†§Ë(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°}ç…µ}çÖ±ïπë±Â}Õ—Ö—’Õ}¡ÖÂ±ΩÖê°±ΩÖë}ëÖ—Ñ†§§§(()I5}19I}=9QQ}%1LÄÙÄ†(ÄÄÄÄâ•êà∞Äâ¡…ïπΩ¥à∞ÄâπΩ¥à∞Äâ—ï±ï¡°Ωπîà∞ÄâµÖ•∞à∞ÄâôΩ…µÖ—•Ω∏à∞Äâ±•ï‘à∞(ÄÄÄÄâÕ—Ö—’–à∞ÄâëÖ—ïÕ}ôΩ…µÖ—•Ω∏à∞ÄâΩ…•ù•πîà∞Äâ…ï±Öπçï}ëÖ—îà∞(ÄÄÄÄâ¡…Ωç°Ö•πï}Öç—•Ωπ}µÖπ’ï±±îà∞ÄâëïÕ¡}—Â¡îà∞Äâç¡òà∞Äâç¡ô}µΩπ—Öπ–à∞Äâç¡ô}¡Ö±•ï»à∞(ÄÄÄÄâ•ëïπ—•—ï}ç…ïÖ—•Ω∏à∞Äâ•ëïπ—•—ï}Ω¨à∞Äâô•πÖπçïµïπ—}ô–à∞(ÄÄÄÄâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–à∞ÄâµΩπ—Öπ—}ÖççΩ…ëï}ô–à∞(ÄÄÄÄâô•πÖπçïµïπ—}¡ï…ÕΩ}¡ΩÕÕ•â±îà∞Äâ…ïô’Õ}ô—}¡ï…Õºà∞Äâ•πÕç…•—}ô–à∞(ÄÄÄÄâ…ïÕ—ï}Ö}ç°Ö…ùï}¡ï…Õºà∞ÄâçÖ…—ï}¡…ºà∞Äâ—•—…ï}Õï©Ω’»à∞(ÄÄÄÄâ—•—…ï}Õï©Ω’…}çπÖ¡Ãà∞ÄâùÖ…ëï}Ÿ’îà∞ÄâÖπ—ïçïëïπ—Ãà∞ÄâçΩµ¡—ï}çπÖ¡Ãà∞(ÄÄÄÄâ•π—ïù…Ö—•Ωπ}ë…ÖçÖ»à∞(§()ëïòÅ}ç…µ}çÖ±ïπëÖ…}çΩπ—Öç—}¡ÖÂ±ΩÖê°ëÖ—Ñ∞ÅçΩπ—Öç–§Ë(ÄÄÄÄààâ·¡ΩÕîÅ’π•≈’ïµïπ–Å±ïÃÅç°Öµ¡ÃÅ…ï≈’•ÃÅ¡Ö»Å±ïÃÅ•πë•çÖ—ï’…ÃÅë‘ÅçÖ±ïπë…•ï»∏ààà(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ•êàËÄàà∞Äâ¡…ïπΩ¥àËÄàà∞ÄâπΩ¥àËÄàà∞ÄâôΩ…µÖ—•Ω∏àËÄàà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ—ï±ï¡°ΩπîàËÄàà∞ÄâµÖ•∞àËÄàà∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅÏ(ÄÄÄÄÄÄÄÅ≠ï‰ËÅçΩπ—Öç–πùï–°≠ï‰§(ÄÄÄÄÄÄÄÅôΩ»Å≠ï‰Å•∏ÅI5}19I}=9QQ}%1L(ÄÄÄÅÙ(ÄÄÄÅÕπÖ¡Õ°Ω–ÄÙÅëÖ—Ñπùï–†âç…µ}çπÖ¡Õ}ÕçΩ…•πù}ÕπÖ¡Õ°Ω—Ãà∞ÅÌÙ§πùï–†(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†â•êà§ÅΩ»Äàà§(ÄÄÄÄ§(ÄÄÄÅïôôïç—•Ÿï}çΩπ—Öç–ÄÙÅë•ç–°çΩπ—Öç–§(ÄÄÄÅïôôïç—•Ÿï}çΩπ—Öç–πÕï—ëïôÖ’±–†(ÄÄÄÄÄÄÄÄâô•πÖπçïµïπ—}¡ï…ÕΩ}¡ΩÕÕ•â±îà∞(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†â…ïô’Õ}ô—}¡ï…Õºà§ÅΩ»Äàà§∞(ÄÄÄÄ§(ÄÄÄÅÕçΩ…îÄÙÅçÖ±ç’±Ö—ï}çÖπë•ëÖ—ï}•π—ïù…Ö—•Ωπ}ÕçΩ…î°ïôôïç—•Ÿï}çΩπ—Öç–∞ÅÕπÖ¡Õ°Ω–§(ÄÄÄÅ¡ÖÂ±ΩÖëlâ•π—ïù…Ö—•Ωπ}ÕçΩ…îâtÄÙÅÏ(ÄÄÄÄÄÄÄÅ≠ï‰ËÅÕçΩ…îπùï–°≠ï‰§(ÄÄÄÄÄÄÄÅôΩ»Å≠ï‰Å•∏Ä†(ÄÄÄÄÄÄÄÄÄÄÄÄâÕçΩ…îà∞Äâ±ïŸï∞à∞Äâ±Öâï∞à∞ÄâΩ¡ï…Ö—•ΩπÖ±}Õ—Ö—’Ãà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡ï…ÕΩπÖ±}…ïµÖ•πëï…}Ö¡¡±•çÖâ±îà∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅÙ(ÄÄÄÅ…ï—’…∏Å¡ÖÂ±ΩÖê(()ëïòÅ}ç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Õ}¡ÖÂ±ΩÖê°ëÖ—Ñ§Ë(ÄÄÄÄààâΩπÕ—…’•–Å∞ùÖùïπëÑÅI4ÅÕÖπÃÅ…ï±•…îÅ±îÅô•ç°•ï»ÅëîÅëΩπª•ïÃ∏ààà(ÄÄÄÅçΩπ—Öç—ÃÄÙÅÌ•—ï¥πùï–†â•êà§ËÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅëÖ—Ñπùï–†âç…µ}çΩπ—Öç—Ãà∞Åmt•Ù(ÄÄÄÅÖ¡¡Ω•π—µïπ—ÃÄÙÅmt(ÄÄÄÅôΩ»Å•—ï¥Å•∏ÅëÖ—Ñπùï–†âç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Ãà∞Åmt§Ë(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÄÙÅçΩπ—Öç—Ãπùï–°•—ï¥πùï–†âçΩπ—Öç—}•êà§§ÅΩ»ÅÌÙ(ÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—ÃπÖ¡¡ïπê°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄ®©•—ï¥∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—Öç–àËÅ}ç…µ}çÖ±ïπëÖ…}çΩπ—Öç—}¡ÖÂ±ΩÖê°ëÖ—Ñ∞ÅçΩπ—Öç–§∞(ÄÄÄÄÄÄÄÅÙ§(ÄÄÄÅÖ¡¡Ω•π—µïπ—ÃπÕΩ…–°≠ï‰ı±ÖµâëÑÅ•—ï¥ËÅ•—ï¥πùï–†âÕ—Ö…—}—•µîà§ÅΩ»Äàà§(ÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄâÖ¡¡Ω•π—µïπ—ÃàËÅÖ¡¡Ω•π—µïπ—Ã∞(ÄÄÄÄÄÄÄÄâ•π—ïù…Ö—•Ω∏àËÅ}ç…µ}çÖ±ïπë±Â}Õ—Ö—’Õ}¡ÖÂ±ΩÖê°ëÖ—Ñ§∞(ÄÄÄÅÙ(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçÖ±ïπë±‰ΩÖ¡¡Ω•π—µïπ—Ãà§)±Ωù•π}…ï≈’•…ïê)}ç…µ}Õï…•Ö±•Èïê)ëïòÅç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Ã†§Ë(ÄÄÄÄààâIï—Ω’…πîÅ∞ùÖùïπëÑÅ¡Ö…—Öü§∞Åïπ…•ç°§ÅÖŸïåÅ±ÑÅô•ç°îÅI4ÅÖÕÕΩçß•î∏ààà(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°}ç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Õ}¡ÖÂ±ΩÖê°±ΩÖë}ëÖ—Ñ†§§§(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçÖ±ïπë±‰ΩÖ¡¡Ω•π—µïπ—ÃºÒÖ¡¡Ω•π—µïπ—}•ê¯à∞Åµï—°ΩëÃılâAQ ât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çÖ±ïπë±Â}’¡ëÖ—ï}Ö¡¡Ω•π—µïπ–°Ö¡¡Ω•π—µïπ—}•ê§Ë(ÄÄÄÄààâπ…ïù•Õ—…îÅ±îÅÀ•Õ’±—Ö–ÅëîÅ±ÑÅ¡…•ÕîÅëîÅçΩπ—Öç–ÅÖÕÕΩçß•îÉÄÅ’∏Å…ïπëïËµŸΩ’Ã∏ààà(ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(ÄÄÄÅ…ïÕ¡ΩπÕï}Õ—Ö—’ÃÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†â…ïÕ¡ΩπÕï}Õ—Ö—’Ãà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅ…ïÕ¡ΩπÕï}Õ—Ö—’ÃÅπΩ–Å•∏ÅÏàà∞ÄâÖπÕ›ï…ïêà∞ÄâπΩ}ÖπÕ›ï»âÙË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâK•Õ’±—Ö–Åë‘Å…ïπëïËµŸΩ’ÃÅ•πŸÖ±•ëî∏âÙ§∞Ä–¿¿((ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅÖ¡¡Ω•π—µïπ–ÄÙÅπï·–†(ÄÄÄÄÄÄÄÄ°•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅëÖ—Ñπùï–†âç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Ãà∞Åmt§(ÄÄÄÄÄÄÄÄÅ•òÅ•—ï¥πùï–†â•êà§ÄÙÙÅÖ¡¡Ω•π—µïπ—}•ê§∞(ÄÄÄÄÄÄÄÅ9Ωπî∞(ÄÄÄÄ§(ÄÄÄÅ•òÅπΩ–ÅÖ¡¡Ω•π—µïπ–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâIïπëïËµŸΩ’ÃÅ•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–((ÄÄÄÅ¡…ïŸ•Ω’Õ}Õ—Ö—’ÃÄÙÅÖ¡¡Ω•π—µïπ–πùï–†â…ïÕ¡ΩπÕï}Õ—Ö—’Ãà§ÅΩ»Äàà(ÄÄÄÅπΩ‹ÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅÖ¡¡Ω•π—µïπ—lâ…ïÕ¡ΩπÕï}Õ—Ö—’ÃâtÄÙÅ…ïÕ¡ΩπÕï}Õ—Ö—’Ã(ÄÄÄÅÖ¡¡Ω•π—µïπ—lâ…ïÕ¡ΩπÕï}Õ—Ö—’Õ}’¡ëÖ—ïë}Ö–âtÄÙÅπΩ‹(ÄÄÄÅÖ¡¡Ω•π—µïπ—lâ’¡ëÖ—ïë}Ö–âtÄÙÅπΩ‹(ÄÄÄÅëï±•Ÿï…‰ÄÙÅ9Ωπî(ÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅÖ¡¡Ω•π—µïπ–πùï–†âçΩπ—Öç—}•êà§§(ÄÄÄÅ•òÅ…ïÕ¡ΩπÕï}Õ—Ö—’ÃÅ•∏ÅÏâπΩ}ÖπÕ›ï»à∞ÄâÖπÕ›ï…ïêâÙÅÖπêÅ¡…ïŸ•Ω’Õ}Õ—Ö—’ÃÄÑÙÅ…ïÕ¡ΩπÕï}Õ—Ö—’ÃË(ÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ…ïÕ¡ΩπÕï}Õ—Ö—’ÃÄÙÙÄâπΩ}ÖπÕ›ï»àË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâÕ—Ö—’–âtÄÙÄâÅ…ï±Öπçï»à(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö…•Õ}—ΩëÖ‰ÄÙÅëÖ—ï—•µîπëÖ—ï—•µîππΩ‹°¡Â—Ëπ—•µïÈΩπî†â’…Ω¡îΩAÖ…•Ãà§§πëÖ—î†§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅπï·—}…ï±Öπçï}ëÖ—îÄÙÄ°¡Ö…•Õ}—ΩëÖ‰Ä¨ÅëÖ—ï—•µîπ—•µïëï±—Ñ°ëÖÂÃÙ‹§§π•ÕΩôΩ…µÖ–†§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}Õç°ïë’±ï}…ï±Öπçî†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅπï·—}…ï±Öπçï}ëÖ—î∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕΩ’…çîÙâçÖ±ïπë±Â}πΩ}ÖπÕ›ï»à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµΩ—•òÙâM’•—îÅÖâÕïπçîÅÖ‘Å…ïπëïËµŸΩ’Ãà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞ÄâÕ—Ö—’–à∞ÄâM—Ö—’–ÄËÅÅ…ï±Öπçï»à∞ÄâMÖπÃÅÀ•¡ΩπÕîÅÖ‘Å…ïπëïËµŸΩ’ÃÉ
‹Å…ï±ÖπçîÅÖ’—ΩµÖ—•≈’îÉÄÅ(¨‹à§(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅπΩ‹(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ…ïÕ¡ΩπÕï}Õ—Ö—’ÃÄÙÙÄâπΩ}ÖπÕ›ï»àË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅëï±•Ÿï…‰ÄÙÅ}ç…µ}Õïπë}Ö¡¡Ω•π—µïπ—}ôΩ±±Ω›’¿°ëÖ—Ñ∞ÅçΩπ—Öç–∞ÄâAÖÃÅëîÅÀ•¡ΩπÕîÅÖ¡¡ï∞à§(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ïÕ’±–ÄÙÅë•ç–°Ö¡¡Ω•π—µïπ–§(ÄÄÄÅ•òÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÄåÅIï—’…∏Å—°îÅçΩπ—Öç–Åç°ÖπùïêÅâ‰Å—°•ÃÅÖç—•Ω∏ÅÕºÅ—°îÅI4ÅçÖ∏Å’¡ëÖ—îÅ•—Ã(ÄÄÄÄÄÄÄÄåÅ•∏µµïµΩ…‰Å±•Õ–Å•µµïë•Ö—ï±‰∞Å›•—°Ω’–Å…ï≈’•…•πúÅÑÅô’±∞Å¡ÖùîÅ…ïô…ïÕ†∏(ÄÄÄÄÄÄÄÅ…ïÕ’±—lâçΩπ—Öç–âtÄÙÅçΩπ—Öç–(ÄÄÄÅ•òÅëï±•Ÿï…‰Å•ÃÅπΩ–Å9ΩπîË(ÄÄÄÄÄÄÄÅ…ïÕ’±—lâëï±•Ÿï…‰âtÄÙÅëï±•Ÿï…‰(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°…ïÕ’±–§(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçÖ±ïπë±‰ΩÕï—’¿à∞Åµï—°ΩëÃılâA=MPât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çÖ±ïπë±Â}Õï—’¿†§Ë(ÄÄÄÅ’Õï»ÄÙÅç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ(ÄÄÄÅ•òÅ’Õï»πùï–†â…Ω±îà§ÄÑÙÄâÖëµ•∏àË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâMï’∞Å’∏ÅÖëµ•π•Õ—…Ö—ï’»Å¡ï’–ÅçΩπô•ù’…ï»ÅÖ±ïπë±‰∏âÙ§∞Ä–¿Ã(ÄÄÄÅ•òÅπΩ–Å}çÖ±ïπë±Â}—Ω≠ï∏†§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ©Ω’—ïËÅ191e}MM}Q=-8ÅëÖπÃÅ±ïÃÅŸÖ…•Öâ±ïÃÅIïπëï»∏âÙ§∞Ä‘¿Ã(ÄÄÄÅ•òÅπΩ–Å}çÖ±ïπë±Â}Õ•ùπ•πù}≠ï‰†§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ©Ω’—ïËÅ191e}]	!==-}M%9%9}-dÅëÖπÃÅ±ïÃÅŸÖ…•Öâ±ïÃÅIïπëï»∏âÙ§∞Ä‘¿Ã(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅçΩπ—ï·–ÄÙÅ}çÖ±ïπë±Â}’Õï…}çΩπ—ï·–†§(ÄÄÄÄÄÄÄÅçÖ±±âÖç≠}’…∞ÄÙÅ}çÖ±ïπë±Â}çÖ±±âÖç≠}’…∞†§(ÄÄÄÄÄÄÄÅÕçΩ¡îÄÙÄâΩ…ùÖπ•ÈÖ—•Ω∏à(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÕ’âÕç…•¡—•Ω∏ÄÙÅ}çÖ±ïπë±Â}ç…ïÖ—ï}Ω…}…ï’Õï}›ïâ°ΩΩ¨°çΩπ—ï·–∞ÅÕçΩ¡î∞ÅçÖ±±âÖç≠}’…∞§(ÄÄÄÄÄÄÄÅï·çï¡–ÅÖ±ïπë±ÂA%……Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅï·åπÕ—Ö—’Õ}çΩëîÄÑÙÄ–¿ÃÅΩ»Åï·åπ•πÕ’ôô•ç•ïπ—}ÕçΩ¡îË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•Õî(ÄÄÄÄÄÄÄÄÄÄÄÅÕçΩ¡îÄÙÄâ’Õï»à(ÄÄÄÄÄÄÄÄÄÄÄÅÕ’âÕç…•¡—•Ω∏ÄÙÅ}çÖ±ïπë±Â}ç…ïÖ—ï}Ω…}…ï’Õï}›ïâ°ΩΩ¨°çΩπ—ï·–∞ÅÕçΩ¡î∞ÅçÖ±±âÖç≠}’…∞§((ÄÄÄÄÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÄÄÄÄÅëÖ—Ölâç…µ}çÖ±ïπë±‰âtÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄ®®°ëÖ—Ñπùï–†âç…µ}çÖ±ïπë±‰à§ÅΩ»ÅÌÙ§∞(ÄÄÄÄÄÄÄÄÄÄÄÄ®©çΩπ—ï·–∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕçΩ¡îàËÅÕçΩ¡î∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ›ïâ°ΩΩ≠}’…§àËÅÕ’âÕç…•¡—•Ω∏πùï–†â’…§à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ›ïâ°ΩΩ≠}’…∞àËÅçÖ±±âÖç≠}’…∞∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ›ïâ°ΩΩ≠}Õ—Ö—îàËÅÕ’âÕç…•¡—•Ω∏πùï–†âÕ—Ö—îà§ÅΩ»ÄâÖç—•Ÿîà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÖççΩ’π—}πÖµîàËÅçΩπ—ï·–πùï–†âÖççΩ’π—}πÖµîà§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÖççΩ’π—}ïµÖ•∞àËÅçΩπ—ï·–πùï–†âÖççΩ’π—}ïµÖ•∞à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕÂπç}ç’…ÕΩ»àËÅ9Ωπî∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕÂπç}çΩµ¡±ï—îàËÅÖ±Õî∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπô•ù’…ïë}Ö–àËÅ}ç…µ}πΩ‹†§∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕîÄÙÅ}ç…µ}çÖ±ïπë±Â}Õ—Ö—’Õ}¡ÖÂ±ΩÖê°ëÖ—Ñ§(ÄÄÄÄÄÄÄÅ•òÅÕçΩ¡îÄÙÙÄâ’Õï»àË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕïlâ›Ö…π•πúâtÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ1îÅ©ï—Ω∏Å∏ùÑÅ¡ÖÃÅ±ïÃÅë…Ω•—ÃÅÖëµ•π•Õ—…Ö—ï’»ÅëîÅ∞ùΩ…ùÖπ•ÕÖ—•Ω∏∏Äà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ1ÑÅÕÂπç°…Ωπ•ÕÖ—•Ω∏ÅçΩ’Ÿ…îÅ—Ω’ÃÅ±ïÃÅ…ïπëïËµŸΩ’ÃÅë‘ÅçΩµ¡—îÅÖ±ïπë±‰Å±ß§ÅÖ‘Å©ï—Ω∏∏à(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°…ïÕ¡ΩπÕî§(ÄÄÄÅï·çï¡–Ä°Ö±ïπë±ÂA%……Ω»∞ÅI’π—•µï……Ω»§ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å}çÖ±ïπë±Â}…Ω’—ï}ï……Ω»°ï·å§(()ëïòÅ}çÖ±ïπë±Â}ïŸïπ—}—Â¡ïÕ}ôΩ…}çΩπ—ï·–°ëÖ—Ñ§Ë(ÄÄÄÅçΩπ—ï·–ÄÙÅ}çÖ±ïπë±Â}çΩπ—ï·—}ô…Ωµ}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ¡Ö…ÖµÃÄÙÅÏâÖç—•ŸîàËÄâ—…’îà∞ÄâçΩ’π–àËÄƒ¿¿∞ÄâÕΩ…–àËÄâπÖµîÈÖÕåâÙ(ÄÄÄÅ•òÅçΩπ—ï·–πùï–†âÕçΩ¡îà§ÄÙÙÄâ’Õï»àË(ÄÄÄÄÄÄÄÅ¡Ö…ÖµÕlâ’Õï»âtÄÙÅçΩπ—ï·—lâ’Õï»ât(ÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÅ¡Ö…ÖµÕlâΩ…ùÖπ•ÈÖ—•Ω∏âtÄÙÅçΩπ—ï·—lâΩ…ùÖπ•ÈÖ—•Ω∏ât(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅïŸïπ—}—Â¡ïÃÄÙÅ}çÖ±ïπë±Â}¡Öù•πÖ—ïë}çΩ±±ïç—•Ω∏†àΩïŸïπ—}—Â¡ïÃà∞Å¡Ö…ÖµÃı¡Ö…ÖµÃ§(ÄÄÄÅï·çï¡–ÅÖ±ïπë±ÂA%……Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ•òÅçΩπ—ï·–πùï–†âÕçΩ¡îà§ÄÑÙÄâΩ…ùÖπ•ÈÖ—•Ω∏àÅΩ»Åï·åπÕ—Ö—’Õ}çΩëîÄÑÙÄ–¿ÃÅΩ»Åï·åπ•πÕ’ôô•ç•ïπ—}ÕçΩ¡îË(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•Õî(ÄÄÄÄÄÄÄÅ¡Ö…ÖµÃπ¡Ω¿†âΩ…ùÖπ•ÈÖ—•Ω∏à∞Å9Ωπî§(ÄÄÄÄÄÄÄÅ¡Ö…ÖµÕlâ’Õï»âtÄÙÅçΩπ—ï·—lâ’Õï»ât(ÄÄÄÄÄÄÄÅïŸïπ—}—Â¡ïÃÄÙÅ}çÖ±ïπë±Â}¡Öù•πÖ—ïë}çΩ±±ïç—•Ω∏†àΩïŸïπ—}—Â¡ïÃà∞Å¡Ö…ÖµÃı¡Ö…ÖµÃ§(ÄÄÄÅ’π•≈’îÄÙÅÌÙ(ÄÄÄÅôΩ»ÅïŸïπ—}—Â¡îÅ•∏ÅïŸïπ—}—Â¡ïÃË(ÄÄÄÄÄÄÄÅ’…§ÄÙÅïŸïπ—}—Â¡îπùï–†â’…§à§(ÄÄÄÄÄÄÄÅ•òÅ’…§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ’π•≈’ïm’…•tÄÙÅïŸïπ—}—Â¡î(ÄÄÄÅ…ï—’…∏ÅÕΩ…—ïê†(ÄÄÄÄÄÄÄÅ’π•≈’îπŸÖ±’ïÃ†§∞(ÄÄÄÄÄÄÄÅ≠ï‰ı±ÖµâëÑÅ•—ï¥ËÅÕ—»°•—ï¥πùï–†âπÖµîà§ÅΩ»Äàà§πçÖÕïôΩ±ê†§∞(ÄÄÄÄ§(()191e}Y9Q}QeA}Q=-9M}	e}=I5Q%=8ÄÙÅÏ(ÄÄÄÄâÖ¡ÃàËÄ†âÖùïπ–ÅëîÅÕïç’…•—îà∞ÄàÅÖ¡Ãà§∞(ÄÄÄÄâÑÕ¿àËÄ†(ÄÄÄÄÄÄÄÄâÑÕ¿à∞(ÄÄÄÄÄÄÄÄàÅÖ¡»à∞(ÄÄÄÄÄÄÄÄâùÖ…ëîÅë‘ÅçΩ…¡Ãà∞(ÄÄÄÄÄÄÄÄâ¡…Ω—ïç—•Ω∏Å¡°ÂÕ•≈’îà∞(ÄÄÄÄÄÄÄÄâ¡…Ω—ïç—•Ω∏Å…Ö¡¡…Ωç°ïîà∞(ÄÄÄÄ§∞(ÄÄÄÄâëïÕ¿àËÄ†âëïÕ¿à∞Äâë•…•ùïÖπ–à§∞(ÄÄÄÄâÕÕ•Ö¿ÄƒàËÄ†âÕÕ•Ö¿à∞§∞(ÄÄÄÄâç°Ö’ôôï’»ÅŸ—åàËÄ†âŸ—åà∞Äâç°Ö’ôôï’»à§∞)Ù(()ëïòÅ}çÖ±ïπë±Â}ïŸïπ—}—Â¡ï}µÖ—ç°ïÕ}ôΩ…µÖ—•Ω∏°ïŸïπ—}—Â¡î∞ÅôΩ…µÖ—•Ω∏§Ë(ÄÄÄÅπΩ…µÖ±•Èïë}ôΩ…µÖ—•Ω∏ÄÙÄ†(ÄÄÄÄÄÄÄÅ’π•çΩëïëÖ—ÑππΩ…µÖ±•Èî†â9-à∞ÅÕ—»°ôΩ…µÖ—•Ω∏ÅΩ»Äàà§§(ÄÄÄÄÄÄÄÄπïπçΩëî†âÖÕç•§à∞Äâ•ùπΩ…îà§(ÄÄÄÄÄÄÄÄπëïçΩëî†§(ÄÄÄÄÄÄÄÄπ±Ω›ï»†§(ÄÄÄÄÄÄÄÄπÕ—…•¿†§(ÄÄÄÄ§(ÄÄÄÅ•òÅπΩ–ÅπΩ…µÖ±•Èïë}ôΩ…µÖ—•Ω∏Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅQ…’î(ÄÄÄÅï·¡ïç—ïêÄÙÅ191e}Y9Q}QeA}Q=-9M}	e}=I5Q%=8πùï–†(ÄÄÄÄÄÄÄÅπΩ…µÖ±•Èïë}ôΩ…µÖ—•Ω∏∞(ÄÄÄÄÄÄÄÄ°òàÅÌπΩ…µÖ±•Èïë}ôΩ…µÖ—•ΩπÙà∞§∞(ÄÄÄÄ§(ÄÄÄÅπΩ…µÖ±•Èïë}πÖµîÄÙÄ†(ÄÄÄÄÄÄÄÄàÄà(ÄÄÄÄÄÄÄÄ¨Å’π•çΩëïëÖ—ÑππΩ…µÖ±•Èî†â9-à∞ÅÕ—»°ïŸïπ—}—Â¡îπùï–†âπÖµîà§ÅΩ»Äàà§§(ÄÄÄÄÄÄÄÄπïπçΩëî†âÖÕç•§à∞Äâ•ùπΩ…îà§(ÄÄÄÄÄÄÄÄπëïçΩëî†§(ÄÄÄÄÄÄÄÄπ±Ω›ï»†§(ÄÄÄÄ§(ÄÄÄÅ…ï—’…∏ÅÖπ‰°—Ω≠ï∏Å•∏ÅπΩ…µÖ±•Èïë}πÖµîÅôΩ»Å—Ω≠ï∏Å•∏Åï·¡ïç—ïê§(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçÖ±ïπë±‰ΩïŸïπ–µ—Â¡ïÃà§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çÖ±ïπë±Â}ïŸïπ—}—Â¡ïÃ†§Ë(ÄÄÄÅ•òÅπΩ–Å}çÖ±ïπë±Â}—Ω≠ï∏†§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ191e}MM}Q=-8Å∏ùïÕ–Å¡ÖÃÅçΩπô•ù’À§ÅëÖπÃÅIïπëï»∏âÙ§∞Ä‘¿Ã(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅïŸïπ—}—Â¡ïÃÄÙÅ}çÖ±ïπë±Â}ïŸïπ—}—Â¡ïÕ}ôΩ…}çΩπ—ï·–°±ΩÖë}ëÖ—Ñ†§§(ÄÄÄÄÄÄÄÅôΩ…µÖ—•Ω∏ÄÙÅÕ—»°…ï≈’ïÕ–πÖ…ùÃπùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅôΩ…µÖ—•Ω∏Ë(ÄÄÄÄÄÄÄÄÄÄÄÅïŸïπ—}—Â¡ïÃÄÙÅl(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅïŸïπ—}—Â¡ïÃ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ}çÖ±ïπë±Â}ïŸïπ—}—Â¡ï}µÖ—ç°ïÕ}ôΩ…µÖ—•Ω∏°•—ï¥∞ÅôΩ…µÖ—•Ω∏§(ÄÄÄÄÄÄÄÄÄÄÄÅt(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°l(ÄÄÄÄÄÄÄÄÄÄÄÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ’…§àËÅ•—ï¥πùï–†â’…§à§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâπÖµîàËÅ•—ï¥πùï–†âπÖµîà§ÅΩ»ÄâIïπëïËµŸΩ’ÃÅÖ±ïπë±‰à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâë’…Ö—•Ω∏àËÅ•—ï¥πùï–†âë’…Ö—•Ω∏à§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÖç—•ŸîàËÅ•—ï¥πùï–†âÖç—•Ÿîà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ≠•πêàËÅ•—ï¥πùï–†â≠•πêà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ¡ΩΩ±•πù}—Â¡îàËÅ•—ï¥πùï–†â¡ΩΩ±•πù}—Â¡îà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄââΩΩ≠•πù}µï—°ΩêàËÅ•—ï¥πùï–†ââΩΩ≠•πù}µï—°Ωêà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ•Õ}¡Ö•êàËÅâΩΩ∞°•—ï¥πùï–†â•Õ}¡Ö•êà§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÕç°ïë’±•πù}’…∞àËÅ•—ï¥πùï–†âÕç°ïë’±•πù}’…∞à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ±ΩçÖ—•ΩπÃàËÅ•—ï¥πùï–†â±ΩçÖ—•ΩπÃà§ÅΩ»Åmt∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâç’Õ—Ωµ}≈’ïÕ—•ΩπÃàËÅ•—ï¥πùï–†âç’Õ—Ωµ}≈’ïÕ—•ΩπÃà§ÅΩ»Åmt∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ¡…Ωô•±îàËÅ•—ï¥πùï–†â¡…Ωô•±îà§ÅΩ»ÅÌÙ∞(ÄÄÄÄÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»Å•—ï¥Å•∏ÅïŸïπ—}—Â¡ïÃ(ÄÄÄÄÄÄÄÅt§(ÄÄÄÅï·çï¡–Ä°Ö±ïπë±ÂA%……Ω»∞ÅI’π—•µï……Ω»§ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å}çÖ±ïπë±Â}…Ω’—ï}ï……Ω»°ï·å§(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçÖ±ïπë±‰ΩÖŸÖ•±Öâ•±•—‰à§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çÖ±ïπë±Â}ÖŸÖ•±Öâ•±•—‰†§Ë(ÄÄÄÅïŸïπ—}—Â¡îÄÙÅÕ—»°…ï≈’ïÕ–πÖ…ùÃπùï–†âïŸïπ—}—Â¡îà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅÕ—Ö…—}—•µîÄÙÅÕ—»°…ï≈’ïÕ–πÖ…ùÃπùï–†âÕ—Ö…—}—•µîà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅïπë}—•µîÄÙÅÕ—»°…ï≈’ïÕ–πÖ…ùÃπùï–†âïπë}—•µîà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–Å}çÖ±ïπë±Â}…ïÕΩ’…çï}’’•ê°ïŸïπ—}—Â¡î∞ÄâïŸïπ—}—Â¡ïÃà§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâQÂ¡îÅëîÅ…ïπëïËµŸΩ’ÃÅÖ±ïπë±‰Å•πŸÖ±•ëî∏âÙ§∞Ä–¿¿(ÄÄÄÅ•òÅπΩ–ÅÕ—Ö…—}—•µîÅΩ»ÅπΩ–Åïπë}—•µîË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1ÑÅ√•…•ΩëîÅëîÅë•Õ¡Ωπ•â•±•”§ÅïÕ–Å•πçΩµ¡≥°—î∏âÙ§∞Ä–¿¿(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅÖŸÖ•±Öâ±ï}Õ—Ö…–ÄÙÅëÖ—ï—•µîπëÖ—ï—•µîπô…Ωµ•ÕΩôΩ…µÖ–†(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö…—}—•µîπ…ï¡±Öçî†âhà∞Äà¨¿¿Ë¿¿à§(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅÖŸÖ•±Öâ±ï}ïπêÄÙÅëÖ—ï—•µîπëÖ—ï—•µîπô…Ωµ•ÕΩôΩ…µÖ–†(ÄÄÄÄÄÄÄÄÄÄÄÅïπë}—•µîπ…ï¡±Öçî†âhà∞Äà¨¿¿Ë¿¿à§(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ•òÅÖŸÖ•±Öâ±ï}Õ—Ö…–π—È•πôºÅ•ÃÅ9ΩπîÅΩ»ÅÖŸÖ•±Öâ±ï}ïπêπ—È•πôºÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»(ÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâ1ÑÅ√•…•ΩëîÅëîÅë•Õ¡Ωπ•â•±•”§ÅëΩ•–ÅçΩπ—ïπ•»ÅëïÃÅëÖ—ïÃÅ%M<Ä‡ÿ¿ƒÅÖŸïåÅô’ÕïÖ‘Å°Ω…Ö•…î∏à(ÄÄÄÄÄÄÄÅÙ§∞Ä–¿¿(ÄÄÄÅ•òÅÖŸÖ•±Öâ±ï}ïπêÄÙÅÖŸÖ•±Öâ±ï}Õ—Ö…–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâ1ÑÅô•∏ÅëîÅ±ÑÅ√•…•ΩëîÅëîÅë•Õ¡Ωπ•â•±•”§ÅëΩ•–É©—…îÅ¡ΩÕ”•…•ï’…îÉÄÅÕΩ∏Åì•â’–∏à(ÄÄÄÄÄÄÄÅÙ§∞Ä–¿¿(ÄÄÄÅ•òÅÖŸÖ•±Öâ±ï}ïπêÄ¥ÅÖŸÖ•±Öâ±ï}Õ—Ö…–Ä¯ÅëÖ—ï—•µîπ—•µïëï±—Ñ°ëÖÂÃÙ‹§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâ1ÑÅ√•…•ΩëîÅëîÅë•Õ¡Ωπ•â•±•”§ÅÖ±ïπë±‰ÅπîÅ¡ï’–Å¡ÖÃÅì•¡ÖÕÕï»Ä‹Å©Ω’…Ã∏à(ÄÄÄÄÄÄÄÅÙ§∞Ä–¿¿(ÄÄÄÄåÅÖ±ïπë±‰Åï·•ùîÅ’πîÅâΩ…πîÅëîÅì•â’–ÅÕ—…•ç—ïµïπ–Åô’—’…î∏Å1ÑÅëÖ—îÅïπŸΩÁ•î(ÄÄÄÄåÅ¡Ö»Å±îÅπÖŸ•ùÖ—ï’»Å¡ï’–Åì•´ÄÉ©—…îÅ¡ÖÕœ•îÅëîÅ≈’ï±≈’ïÃÅµ•±±•ÕïçΩπëïÃÅÖ‘(ÄÄÄÄåÅµΩµïπ–Åø‰Å±ÑÅ…ï≈◊©—îÅÖ——ï•π–ÅÖ±ïπë±‰∞ÅëΩπåÅùÖ…ëΩπÃÅ’πîÅ¡ï—•—îÅµÖ…ùî∏(ÄÄÄÅµ•π•µ’µ}Õ—Ö…–ÄÙÄ†(ÄÄÄÄÄÄÄÅëÖ—ï—•µîπëÖ—ï—•µîππΩ‹°ëÖ—ï—•µîπ—•µïÈΩπîπ’—å§(ÄÄÄÄÄÄÄÄ¨ÅëÖ—ï—•µîπ—•µïëï±—Ñ°µ•π’—ïÃÙƒ§(ÄÄÄÄ§(ÄÄÄÅ•òÅÖŸÖ•±Öâ±ï}ïπêÄÙÅµ•π•µ’µ}Õ—Ö…–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâ1ÑÅ√•…•ΩëîÅëîÅë•Õ¡Ωπ•â•±•”§ÅÖ±ïπë±‰ÅëΩ•–É©—…îÅÕ•—◊•îÅëÖπÃÅ±îÅô’—’»∏à(ÄÄÄÄÄÄÄÅÙ§∞Ä–¿¿(ÄÄÄÅ•òÅÖŸÖ•±Öâ±ï}Õ—Ö…–ÄÅµ•π•µ’µ}Õ—Ö…–Ë(ÄÄÄÄÄÄÄÅÖŸÖ•±Öâ±ï}Õ—Ö…–ÄÙÅµ•π•µ’µ}Õ—Ö…–(ÄÄÄÄÄÄÄÅÕ—Ö…—}—•µîÄÙÅÖŸÖ•±Öâ±ï}Õ—Ö…–π•ÕΩôΩ…µÖ–°—•µïÕ¡ïåÙâÕïçΩπëÃà§π…ï¡±Öçî†à¨¿¿Ë¿¿à∞Äâhà§(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕîÄÙÅ}çÖ±ïπë±Â}…ï≈’ïÕ–†(ÄÄÄÄÄÄÄÄÄÄÄÄâPà∞(ÄÄÄÄÄÄÄÄÄÄÄÄàΩïŸïπ—}—Â¡ï}ÖŸÖ•±Öâ±ï}—•µïÃà∞(ÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö…ÖµÃıÏ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâïŸïπ—}—Â¡îàËÅïŸïπ—}—Â¡î∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö…—}—•µîàËÅÕ—Ö…—}—•µî∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâïπë}—•µîàËÅïπë}—•µî∞(ÄÄÄÄÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°…ïÕ¡ΩπÕîπùï–†âçΩ±±ïç—•Ω∏à§ÅΩ»Åmt§(ÄÄÄÅï·çï¡–Ä°Ö±ïπë±ÂA%……Ω»∞ÅI’π—•µï……Ω»§ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å}çÖ±ïπë±Â}…Ω’—ï}ï……Ω»°ï·å§(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯ΩçÖ±ïπë±‰ΩÖ¡¡Ω•π—µïπ—Ãà∞Åµï—°ΩëÃılâPà∞ÄâA=MPât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çΩπ—Öç—}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Ã°çΩπ—Öç—}•ê§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–((ÄÄÄÅ•òÅ…ï≈’ïÕ–πµï—°ΩêÄÙÙÄâPàË(ÄÄÄÄÄÄÄÅ±ΩΩ≠’¿ÄÙÅÏâµï—°ΩêàËÄâ±ΩçÖ∞à∞Äâ¡…ΩçïÕÕïë}ïŸïπ—ÃàËÄ¡Ù(ÄÄÄÄÄÄÄÅ±ΩΩ≠’¡}›Ö…π•πúÄÙÄàà(ÄÄÄÄÄÄÄÅ±ΩΩ≠’¡}Õ’ççïïëïêÄÙÅÖ±Õî(ÄÄÄÄÄÄÄÅôï—ç°ïë}¡ÖÂ±ΩÖëÃÄÙÅmt(ÄÄÄÄÄÄÄÅ…ïô…ïÕ°}…ï≈’ïÕ—ïêÄÙÅÕ—»°…ï≈’ïÕ–πÖ…ùÃπùï–†â…ïô…ïÕ†à§ÅΩ»Äàà§πÕ—…•¿†§π±Ω›ï»†§Å•∏ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄàƒà∞Äâ—…’îà∞ÄâÂïÃà∞ÄâΩ’§à∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÅÕ—Ö—îÄÙÅëÖ—Ñπùï–†âç…µ}çÖ±ïπë±‰à§ÅΩ»ÅÌÙ(ÄÄÄÄÄÄÄÅçÖπ}±ΩΩ≠’¡}âÂ}ïµÖ•∞ÄÙÅâΩΩ∞†(ÄÄÄÄÄÄÄÄÄÄÄÅ}çÖ±ïπë±Â}—Ω≠ï∏†§(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅÕ—Ö—îπùï–†â’Õï»à§(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅÕ—Ö—îπùï–†âΩ…ùÖπ•ÈÖ—•Ω∏à§(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅ}ç…µ}πΩ…µÖ±•Èï}ïµÖ•∞°çΩπ—Öç–πùï–†âµÖ•∞à§§(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄåÅ1ÑÅô•ç°îÅÖôô•ç°îÅ•πÕ—Öπ—Öª•µïπ–Å±îÅçÖç°îÅ±ΩçÖ∞ÅÖ±•µïπ”§Å¡Ö»Å±ïÃ(ÄÄÄÄÄÄÄÄåÅ›ïâ°ΩΩ≠ÃΩÕÂπç°…Ωπ•ÕÖ—•ΩπÃ∏ÅUπîÅ…ïç°ï…ç°îÅÖ±ïπë±‰Åë•Õ—Öπ—îÄ°≈’§Å¡ï’–(ÄÄÄÄÄÄÄÄåÅ¡…ïπë…îÅ¡±’Õ•ï’…ÃÅÕïçΩπëïÃ§Å∏ùïÕ–ÅôÖ•—îÅ≈‘ùÖ¡À°ÃÅç±•åÅÕ’»Åç—’Ö±•Õï»∏(ÄÄÄÄÄÄÄÅ•òÅ…ïô…ïÕ°}…ï≈’ïÕ—ïêÅÖπêÅçÖπ}±ΩΩ≠’¡}âÂ}ïµÖ•∞Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅôï—ç°ïë}¡ÖÂ±ΩÖëÃ∞Å±ΩΩ≠’¿ÄÙÅ}ç…µ}çÖ±ïπë±Â}ôï—ç°}çΩπ—Öç—}Ö¡¡Ω•π—µïπ—Ã†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅëÖ—Ñ∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ±ΩΩ≠’¡}Õ’ççïïëïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÄÄÄÄÅï·çï¡–Ä°Ö±ïπë±ÂA%……Ω»∞ÅI’π—•µï……Ω»§ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ±ΩΩ≠’¡}›Ö…π•πúÄÙÅÕ—»°ï·å§((ÄÄÄÄÄÄÄÅ±Ö—ïÕ—}ëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÄÄÄÄÅ±Ö—ïÕ—}çΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°±Ö—ïÕ—}ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å±Ö—ïÕ—}çΩπ—Öç–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅÖ±Õî(ÄÄÄÄÄÄÄÅôΩ»Åôï—ç°ïë}¡ÖÂ±ΩÖêÅ•∏Åôï—ç°ïë}¡ÖÂ±ΩÖëÃË(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}’¡Õï…—}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ±Ö—ïÕ—}ëÖ—Ñ∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅôï—ç°ïë}¡ÖÂ±ΩÖê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕΩ’…çîÙâ—Ö…ùï—ïë}±ΩΩ≠’¿à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—}•êıçΩπ—Öç—}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ïçΩ…ë}Öç—•Ÿ•—‰ıÖ±Õî∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ•òÅ}ç…µ}çÖ±ïπë±Â}…ï±•π≠}Ö¡¡Ω•π—µïπ—Ã°±Ö—ïÕ—}ëÖ—Ñ∞Å±Ö—ïÕ—}çΩπ—Öç–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ•òÅ}ç…µ}ÕÂπç}çΩπ—Öç—}çÖ±ïπë±Â}Õ—Ö—’Ã°±Ö—ïÕ—}ëÖ—Ñ∞Å±Ö—ïÕ—}çΩπ—Öç–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ•òÅ±ΩΩ≠’¡}Õ’ççïïëïêË(ÄÄÄÄÄÄÄÄÄÄÄÅ±Ö—ïÕ—}ëÖ—ÑπÕï—ëïôÖ’±–†âç…µ}çÖ±ïπë±‰à∞ÅÌÙ•lâ±ÖÕ—}ÕÂπç}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ•òÅç°ÖπùïêË(ÄÄÄÄÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°±Ö—ïÕ—}ëÖ—Ñ§(ÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—ÃÄÙÅl(ÄÄÄÄÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏Å±Ö—ïÕ—}ëÖ—Ñπùï–†âç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Ãà∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ•—ï¥πùï–†âçΩπ—Öç—}•êà§ÄÙÙÅçΩπ—Öç—}•ê(ÄÄÄÄÄÄÄÅt(ÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—ÃπÕΩ…–°≠ï‰ı±ÖµâëÑÅ•—ï¥ËÅ•—ï¥πùï–†âÕ—Ö…—}—•µîà§ÅΩ»Äàà∞Å…ïŸï…ÕîıQ…’î§(ÄÄÄÄÄÄÄÅ•π—ïù…Ö—•Ω∏ÄÙÅ}ç…µ}çÖ±ïπë±Â}Õ—Ö—’Õ}¡ÖÂ±ΩÖê°±Ö—ïÕ—}ëÖ—Ñ§(ÄÄÄÄÄÄÄÅ•òÅ±ΩΩ≠’¡}›Ö…π•πúË(ÄÄÄÄÄÄÄÄÄÄÄÅ•π—ïù…Ö—•Ωπlâ±ΩΩ≠’¡}›Ö…π•πúâtÄÙÅ±ΩΩ≠’¡}›Ö…π•πú(ÄÄÄÄÄÄÄÅ±ΩΩ≠’¡lâµÖ—ç°ïë}Ö¡¡Ω•π—µïπ—ÃâtÄÙÅ±ï∏°Ö¡¡Ω•π—µïπ—Ã§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâÖ¡¡Ω•π—µïπ—ÃàËÅÖ¡¡Ω•π—µïπ—Ã∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ•π—ïù…Ö—•Ω∏àËÅ•π—ïù…Ö—•Ω∏∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ±ΩΩ≠’¿àËÅ±ΩΩ≠’¿∞(ÄÄÄÄÄÄÄÅÙ§((ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(ÄÄÄÅïŸïπ—}—Â¡ï}’…§ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âïŸïπ—}—Â¡îà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅïŸïπ—}—Â¡ï}’’•êÄÙÅ}çÖ±ïπë±Â}…ïÕΩ’…çï}’’•ê°ïŸïπ—}—Â¡ï}’…§∞ÄâïŸïπ—}—Â¡ïÃà§(ÄÄÄÅÕ—Ö…—}—•µîÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âÕ—Ö…—}—•µîà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ—•µïÈΩπîÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†â—•µïÈΩπîà§ÅΩ»Äâ’…Ω¡îΩAÖ…•Ãà§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–ÅïŸïπ—}—Â¡ï}’’•êÅΩ»ÅπΩ–ÅÕ—Ö…—}—•µîË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ°Ω•Õ•ÕÕïËÅ’∏Å—Â¡îÅëîÅ…ïπëïËµŸΩ’ÃÅï–Å’∏Å°Ω…Ö•…î∏âÙ§∞Ä–¿¿(ÄÄÄÅ•òÅπΩ–Å}ç…µ}πΩ…µÖ±•Èï}ïµÖ•∞°çΩπ—Öç–πùï–†âµÖ•∞à§§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ©Ω’—ïËÅ∞ùÖë…ïÕÕîÅîµµÖ•∞ÅëîÅ±ÑÅ¡ï…ÕΩππîÅÖŸÖπ–ÅëîÅ¡±Öπ•ô•ï»Å±îÅ…ïπëïËµŸΩ’Ã∏âÙ§∞Ä–¿¿(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ¡Ö…Õïë}Õ—Ö…–ÄÙÅëÖ—ï—•µîπëÖ—ï—•µîπô…Ωµ•ÕΩôΩ…µÖ–°Õ—Ö…—}—•µîπ…ï¡±Öçî†âhà∞Äà¨¿¿Ë¿¿à§§(ÄÄÄÄÄÄÄÅ•òÅ¡Ö…Õïë}Õ—Ö…–π—È•πôºÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»(ÄÄÄÄÄÄÄÅ¡Â—Ëπ—•µïÈΩπî°—•µïÈΩπî§(ÄÄÄÅï·çï¡–Ä°YÖ±’ï……Ω»∞Å¡Â—ËπUπ≠πΩ›πQ•µïiΩπï……Ω»§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1îÅçÀ•πïÖ‘ÅΩ‘Å±îÅô’ÕïÖ‘Å°Ω…Ö•…îÅë‘Å…ïπëïËµŸΩ’ÃÅïÕ–Å•πŸÖ±•ëî∏âÙ§∞Ä–¿¿((ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅïŸïπ—}—Â¡îÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅ}çÖ±ïπë±Â}…ï≈’ïÕ–†âPà∞ÅòàΩïŸïπ—}—Â¡ïÃΩÌïŸïπ—}—Â¡ï}’’•ëÙà§πùï–†â…ïÕΩ’…çîà§ÅΩ»ÅÌÙ(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅïŸïπ—}—Â¡îπùï–†âÖç—•Ÿîà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâîÅ—Â¡îÅëîÅ…ïπëïËµŸΩ’ÃÅÖ±ïπë±‰Å∏ùïÕ–Å¡±’ÃÅÖç—•ò∏âÙ§∞Ä–¿¿(ÄÄÄÄÄÄÄÅçΩπ—Öç—}ôΩ…µÖ—•Ω∏ÄÙÅÕ—»°çΩπ—Öç–πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å}çÖ±ïπë±Â}ïŸïπ—}—Â¡ï}µÖ—ç°ïÕ}ôΩ…µÖ—•Ω∏°ïŸïπ—}—Â¡î∞ÅçΩπ—Öç—}ôΩ…µÖ—•Ω∏§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅïŸïπ—}—Â¡ï}πÖµîÄÙÅïŸïπ—}—Â¡îπùï–†âπÖµîà§ÅΩ»ÄâIïπëïËµŸΩ’ÃÅÖ±ïπë±‰à(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâ1îÅ—Â¡îÅÖ±ïπë±‰É
¨ÅÌïŸïπ—}—Â¡ï}πÖµïÙÉ
ÏÅπîÅçΩ……ïÕ¡ΩπêÅ¡ÖÃÉÄÅ±ÑÅôΩ…µÖ—•Ω∏Äà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâÌçΩπ—Öç—}ôΩ…µÖ—•ΩπÙ∏ÅIïç°Ö…ùïËÅ±ÑÅô•ç°îÅÖŸÖπ–ÅëîÅç°Ω•Õ•»Å’∏ÅπΩ’ŸïÖ‘ÅçÀ•πïÖ‘∏à(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄÄÄÄÅÙ§∞Ä–¿‰(ÄÄÄÄÄÄÄÅ•òÅïŸïπ—}—Â¡îπùï–†â•Õ}¡Ö•êà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâÖ±ïπë±‰Å•µ¡ΩÕîÅ’πîÅ¡ÖùîÅëîÅ¡Ö•ïµïπ–Å¡Ω’»ÅçîÅ—Â¡îÅëîÅ…ïπëïËµŸΩ’Ã∏ÅU—•±•ÕïËÅÕΩ∏Å±•ï∏ÅÖ±ïπë±‰∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÕç°ïë’±•πù}’…∞àËÅïŸïπ—}—Â¡îπùï–†âÕç°ïë’±•πù}’…∞à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄÄÄÄÅÙ§∞Ä–¿¿(ÄÄÄÄÄÄÄÅ•òÅïŸïπ—}—Â¡îπùï–†ââΩΩ≠•πù}µï—°Ωêà§ÄÙÙÄâ¡Ω±∞àË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâîÅ—Â¡îÅÖ±ïπë±‰ÅïÕ–Å’∏ÅÕΩπëÖùîÅëîÅëÖ—ïÃÅï–ÅπîÅ¡ï’–Å¡ÖÃÉ©—…îÅÀ•Õï…€§Åë•…ïç—ïµïπ–Å¡Ö»Å∞ùA$∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÕç°ïë’±•πù}’…∞àËÅïŸïπ—}—Â¡îπùï–†âÕç°ïë’±•πù}’…∞à§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄÄÄÄÅÙ§∞Ä–¿¿((ÄÄÄÄÄÄÄÅâΩΩ≠•πù}±ΩçÖ—•Ω∏ÄÙÅ}çÖ±ïπë±Â}âΩΩ≠•πù}±ΩçÖ—•Ω∏†(ÄÄÄÄÄÄÄÄÄÄÄÅïŸïπ—}—Â¡î∞(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêπùï–†â±ΩçÖ—•Ω∏à§∞(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅÖπÕ›ï…Ã∞Å—ï·—}…ïµ•πëï…}π’µâï»ÄÙÅ}çÖ±ïπë±Â}≈’ïÕ—•Ωπ}ÖπÕ›ï…Ã†(ÄÄÄÄÄÄÄÄÄÄÄÅïŸïπ—}—Â¡î∞(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêπùï–†âÖπÕ›ï…Ãà§∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅô•…Õ—}πÖµîÄÙÅÕ—»°çΩπ—Öç–πùï–†â¡…ïπΩ¥à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ±ÖÕ—}πÖµîÄÙÅÕ—»°çΩπ—Öç–πùï–†âπΩ¥à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•πŸ•—ïîÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâπÖµîàËÄàÄàπ©Ω•∏†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö…–ÅôΩ»Å¡Ö…–Å•∏Åmô•…Õ—}πÖµî∞Å±ÖÕ—}πÖµïtÅ•òÅ¡Ö…–(ÄÄÄÄÄÄÄÄÄÄÄÄ§πÕ—…•¿†§ÅΩ»ÅçΩπ—Öç–πùï–†âµÖ•∞à§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâïµÖ•∞àËÅçΩπ—Öç–πùï–†âµÖ•∞à§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ—•µïÈΩπîàËÅ—•µïÈΩπî∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÅ•òÅô•…Õ—}πÖµîÅÖπêÅ±ÖÕ—}πÖµîË(ÄÄÄÄÄÄÄÄÄÄÄÅ•πŸ•—ïïlâô•…Õ—}πÖµîâtÄÙÅô•…Õ—}πÖµî(ÄÄÄÄÄÄÄÄÄÄÄÅ•πŸ•—ïïlâ±ÖÕ—}πÖµîâtÄÙÅ±ÖÕ—}πÖµî(ÄÄÄÄÄÄÄÅ•òÅ—ï·—}…ïµ•πëï…}π’µâï»Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ•πŸ•—ïïlâ—ï·—}…ïµ•πëï…}π’µâï»âtÄÙÅ—ï·—}…ïµ•πëï…}π’µâï»(ÄÄÄÄÄÄÄÅâΩΩ≠•πù}âΩë‰ÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâïŸïπ—}—Â¡îàËÅïŸïπ—}—Â¡îπùï–†â’…§à§ÅΩ»ÅïŸïπ—}—Â¡ï}’…§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö…—}—•µîàËÅÕ—Ö…—}—•µî∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ•πŸ•—ïîàËÅ•πŸ•—ïî∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÅ•òÅâΩΩ≠•πù}±ΩçÖ—•Ω∏Ë(ÄÄÄÄÄÄÄÄÄÄÄÅâΩΩ≠•πù}âΩëÂlâ±ΩçÖ—•Ω∏âtÄÙÅâΩΩ≠•πù}±ΩçÖ—•Ω∏(ÄÄÄÄÄÄÄÅ•òÅÖπÕ›ï…ÃË(ÄÄÄÄÄÄÄÄÄÄÄÅâΩΩ≠•πù}âΩëÂlâ≈’ïÕ—•ΩπÕ}Öπë}ÖπÕ›ï…ÃâtÄÙÅÖπÕ›ï…Ã((ÄÄÄÄÄÄÄÅ•πŸ•—ïï}…ïÕΩ’…çîÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅ}çÖ±ïπë±Â}…ï≈’ïÕ–†âA=MPà∞ÄàΩ•πŸ•—ïïÃà∞Å©ÕΩπ}âΩë‰ıâΩΩ≠•πù}âΩë‰§πùï–†â…ïÕΩ’…çîà§ÅΩ»ÅÌÙ(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅïŸïπ—}’…§ÄÙÅ•πŸ•—ïï}…ïÕΩ’…çîπùï–†âïŸïπ–à§ÅΩ»Äàà(ÄÄÄÄÄÄÄÅïŸïπ—}’’•êÄÙÅ}çÖ±ïπë±Â}…ïÕΩ’…çï}’’•ê°ïŸïπ—}’…§∞ÄâÕç°ïë’±ïë}ïŸïπ—Ãà§(ÄÄÄÄÄÄÄÅÕç°ïë’±ïë}ïŸïπ–ÄÙÅÌÙ(ÄÄÄÄÄÄÄÅ•òÅïŸïπ—}’’•êË(ÄÄÄÄÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕç°ïë’±ïë}ïŸïπ–ÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}çÖ±ïπë±Â}…ï≈’ïÕ–†âPà∞ÅòàΩÕç°ïë’±ïë}ïŸïπ—ÃΩÌïŸïπ—}’’•ëÙà§πùï–†â…ïÕΩ’…çîà§ÅΩ»ÅÌÙ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅï·çï¡–Ä°Ö±ïπë±ÂA%……Ω»∞ÅI’π—•µï……Ω»§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕç°ïë’±ïë}ïŸïπ–ÄÙÅÌÙ(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅÕç°ïë’±ïë}ïŸïπ–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö…Õïë}Õ—Ö…–ÄÙÅëÖ—ï—•µîπëÖ—ï—•µîπô…Ωµ•ÕΩôΩ…µÖ–°Õ—Ö…—}—•µîπ…ï¡±Öçî†âhà∞Äà¨¿¿Ë¿¿à§§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅïπë}—•µîÄÙÅ¡Ö…Õïë}Õ—Ö…–Ä¨ÅëÖ—ï—•µîπ—•µïëï±—Ñ°µ•π’—ïÃı•π–°ïŸïπ—}—Â¡îπùï–†âë’…Ö—•Ω∏à§ÅΩ»Ä¿§§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçÖ±ç’±Ö—ïë}ïπêÄÙÅïπë}—•µîπ•ÕΩôΩ…µÖ–†§π…ï¡±Öçî†à¨¿¿Ë¿¿à∞Äâhà§(ÄÄÄÄÄÄÄÄÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçÖ±ç’±Ö—ïë}ïπêÄÙÄàà(ÄÄÄÄÄÄÄÄÄÄÄÅÕç°ïë’±ïë}ïŸïπ–ÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ’…§àËÅïŸïπ—}’…§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâπÖµîàËÅïŸïπ—}—Â¡îπùï–†âπÖµîà§ÅΩ»ÄâIïπëïËµŸΩ’ÃÅÖ±ïπë±‰à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö—’ÃàËÄâÖç—•Ÿîà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö…—}—•µîàËÅÕ—Ö…—}—•µî∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâïπë}—•µîàËÅçÖ±ç’±Ö—ïë}ïπê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâïŸïπ—}—Â¡îàËÅïŸïπ—}—Â¡ï}’…§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ±ΩçÖ—•Ω∏àËÅâΩΩ≠•πù}±ΩçÖ—•Ω∏∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâïŸïπ—}µïµâï…Õ°•¡ÃàËÅmt∞(ÄÄÄÄÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—}¡ÖÂ±ΩÖêÄÙÅÏ®©•πŸ•—ïï}…ïÕΩ’…çî∞ÄâÕç°ïë’±ïë}ïŸïπ–àËÅÕç°ïë’±ïë}ïŸïπ—Ù(ÄÄÄÄÄÄÄÅ±Ö—ïÕ—}ëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÄÄÄÄÅ±Ö—ïÕ—}çΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°±Ö—ïÕ—}ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å±Ö—ïÕ—}çΩπ—Öç–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1îÅ…ïπëïËµŸΩ’ÃÅÑÉ•”§ÅçÀß§ÅµÖ•ÃÅ±ÑÅ¡•Õ—îÅ∏ùï·•Õ—îÅ¡±’Ã∏âÙ§∞Ä–¿‰(ÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ–∞Å±Ö—ïÕ—}çΩπ—Öç–ÄÙÅ}ç…µ}’¡Õï…—}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ–†(ÄÄÄÄÄÄÄÄÄÄÄÅ±Ö—ïÕ—}ëÖ—Ñ∞(ÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—}¡ÖÂ±ΩÖê∞(ÄÄÄÄÄÄÄÄÄÄÄÅÕΩ’…çîÙâç…¥à∞(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—}•êıçΩπ—Öç—}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïçΩ…ë}Öç—•Ÿ•—‰ıQ…’î∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°±Ö—ïÕ—}ëÖ—Ñ§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâÖ¡¡Ω•π—µïπ–àËÅÖ¡¡Ω•π—µïπ–∞ÄâçΩπ—Öç–àËÅ±Ö—ïÕ—}çΩπ—Öç—Ù§∞Ä»¿ƒ(ÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÅÕ—»°ï·å•Ù§∞Ä–¿¿(ÄÄÄÅï·çï¡–Ä°Ö±ïπë±ÂA%……Ω»∞ÅI’π—•µï……Ω»§ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å}çÖ±ïπë±Â}…Ω’—ï}ï……Ω»°ï·å∞ÅÖç—•Ω∏Ùâ±ÑÅçÀ•Ö—•Ω∏Åë‘Å…ïπëïËµŸΩ’Ãà§(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçÖ±ïπë±‰ΩÕÂπåà∞Åµï—°ΩëÃılâA=MPât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çÖ±ïπë±Â}ÕÂπå†§Ë(ÄÄÄÅ’Õï»ÄÙÅç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ(ÄÄÄÅ•òÅ’Õï»πùï–†â…Ω±îà§ÄÑÙÄâÖëµ•∏àË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâMï’∞Å’∏ÅÖëµ•π•Õ—…Ö—ï’»Å¡ï’–Å±Öπçï»Å±ÑÅÕÂπç°…Ωπ•ÕÖ—•Ω∏ÅÖ±ïπë±‰∏âÙ§∞Ä–¿Ã(ÄÄÄÅâΩë‰ÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅÕ—Ö—îÄÙÅëÖ—Ñπùï–†âç…µ}çÖ±ïπë±‰à§ÅΩ»ÅÌÙ(ÄÄÄÅ•òÅπΩ–ÅÕ—Ö—îπùï–†â›ïâ°ΩΩ≠}’…§à§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâç—•ŸïËÅêùÖâΩ…êÅ±ÑÅÕÂπç°…Ωπ•ÕÖ—•Ω∏ÅÖ±ïπë±‰∏âÙ§∞Ä–¿‰(ÄÄÄÅç’…ÕΩ»ÄÙÅ9ΩπîÅ•òÅâΩë‰πùï–†â…ïÕ—Ö…–à§Åï±ÕîÅÕ—Ö—îπùï–†âÕÂπç}ç’…ÕΩ»à§(ÄÄÄÅ•òÅÕ—Ö—îπùï–†âÕÂπç}çΩµ¡±ï—îà§ÅÖπêÅπΩ–ÅâΩë‰πùï–†â…ïÕ—Ö…–à§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâçΩµ¡±ï—îàËÅQ…’î∞Äâ¡…ΩçïÕÕïë}ïŸïπ—ÃàËÄ¿∞ÄâÖ¡¡Ω•π—µïπ—ÃàËÄ¡Ù§((ÄÄÄÅâÖ—ç°}Õ•ÈîÄÙÅµÖ‡†ƒ∞Åµ•∏°•π–°ΩÃπùï—ïπÿ†â191e}Me9}	Q!}M%ià∞Äà»¿à§§∞Äƒ¿¿§§(ÄÄÄÅ¡Ö…ÖµÃÄÙÅÏâçΩ’π–àËÅâÖ—ç°}Õ•Èî∞ÄâÕΩ…–àËÄâÕ—Ö…—}—•µîÈëïÕåâÙ(ÄÄÄÅ•òÅÕ—Ö—îπùï–†âÕçΩ¡îà§ÄÙÙÄâ’Õï»àË(ÄÄÄÄÄÄÄÅ¡Ö…ÖµÕlâ’Õï»âtÄÙÅÕ—Ö—îπùï–†â’Õï»à§(ÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÅ¡Ö…ÖµÕlâΩ…ùÖπ•ÈÖ—•Ω∏âtÄÙÅÕ—Ö—îπùï–†âΩ…ùÖπ•ÈÖ—•Ω∏à§(ÄÄÄÅ•òÅç’…ÕΩ»Ë(ÄÄÄÄÄÄÄÅ¡Ö…ÖµÕlâ¡Öùï}—Ω≠ï∏âtÄÙÅç’…ÕΩ»((ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅïŸïπ—}¡ÖùîÄÙÅ}çÖ±ïπë±Â}…ï≈’ïÕ–†âPà∞ÄàΩÕç°ïë’±ïë}ïŸïπ—Ãà∞Å¡Ö…ÖµÃı¡Ö…ÖµÃ∞Å—•µïΩ’–ÙÃ‘§(ÄÄÄÄÄÄÄÅ•µ¡Ω…—ïë}¡ÖÂ±ΩÖëÃÄÙÅmt(ÄÄÄÄÄÄÄÅôΩ»ÅÕç°ïë’±ïë}ïŸïπ–Å•∏ÅïŸïπ—}¡Öùîπùï–†âçΩ±±ïç—•Ω∏à§ÅΩ»ÅmtË(ÄÄÄÄÄÄÄÄÄÄÄÅïŸïπ—}’’•êÄÙÅ}çÖ±ïπë±Â}…ïÕΩ’…çï}’’•ê°Õç°ïë’±ïë}ïŸïπ–πùï–†â’…§à§∞ÄâÕç°ïë’±ïë}ïŸïπ—Ãà§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–ÅïŸïπ—}’’•êË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÄÄÄÄÅ•πŸ•—ïïÃÄÙÅ}çÖ±ïπë±Â}¡Öù•πÖ—ïë}çΩ±±ïç—•Ω∏†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòàΩÕç°ïë’±ïë}ïŸïπ—ÃΩÌïŸïπ—}’’•ëÙΩ•πŸ•—ïïÃà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö…ÖµÃıÏâçΩ’π–àËÄƒ¿¡Ù∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµÖ·}¡ÖùïÃÙƒ¿¿∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»Å•πŸ•—ïîÅ•∏Å•πŸ•—ïïÃË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•µ¡Ω…—ïë}¡ÖÂ±ΩÖëÃπÖ¡¡ïπê°Ï®©•πŸ•—ïî∞ÄâÕç°ïë’±ïë}ïŸïπ–àËÅÕç°ïë’±ïë}ïŸïπ—Ù§((ÄÄÄÄÄÄÄÅ›•—†Å}I5}I=9%1%Q%=9}1=,Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ±Ö—ïÕ—}ëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÄÄÄÄÄÄÄÄÅâïôΩ…ï}çΩ’π–ÄÙÅ±ï∏°±Ö—ïÕ—}ëÖ—Ñπùï–†âç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Ãà∞Åmt§§(ÄÄÄÄÄÄÄÄÄÄÄÅµÖ—ç°ïêÄÙÄ¿(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»Å•µ¡Ω…—ïë}¡ÖÂ±ΩÖêÅ•∏Å•µ¡Ω…—ïë}¡ÖÂ±ΩÖëÃË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ|∞ÅçΩπ—Öç–ÄÙÅ}ç…µ}’¡Õï…—}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ±Ö—ïÕ—}ëÖ—Ñ∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•µ¡Ω…—ïë}¡ÖÂ±ΩÖê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕΩ’…çîÙâÕÂπåà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ïçΩ…ë}Öç—•Ÿ•—‰ıÖ±Õî∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµÖ—ç°ïêÄ¨ÙÄƒ(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï¡Ö•…ïêÄÙÅ}ç…µ}…ï¡Ö•…}çÖç°ïë}çÖ±ïπë±Â}çΩπ—Öç—Ã°±Ö—ïÕ—}ëÖ—Ñ§(ÄÄÄÄÄÄÄÄÄÄÄÅπï·—}ç’…ÕΩ»ÄÙÄ°ïŸïπ—}¡Öùîπùï–†â¡Öù•πÖ—•Ω∏à§ÅΩ»ÅÌÙ§πùï–†âπï·—}¡Öùï}—Ω≠ï∏à§(ÄÄÄÄÄÄÄÄÄÄÄÅ•π—ïù…Ö—•Ωπ}Õ—Ö—îÄÙÅ±Ö—ïÕ—}ëÖ—ÑπÕï—ëïôÖ’±–†âç…µ}çÖ±ïπë±‰à∞ÅÌÙ§(ÄÄÄÄÄÄÄÄÄÄÄÅ•π—ïù…Ö—•Ωπ}Õ—Ö—ïlâÕÂπç}ç’…ÕΩ»âtÄÙÅπï·—}ç’…ÕΩ»(ÄÄÄÄÄÄÄÄÄÄÄÅ•π—ïù…Ö—•Ωπ}Õ—Ö—ïlâÕÂπç}çΩµ¡±ï—îâtÄÙÅπΩ–ÅâΩΩ∞°πï·—}ç’…ÕΩ»§(ÄÄÄÄÄÄÄÄÄÄÄÅ•π—ïù…Ö—•Ωπ}Õ—Ö—ïlâ±ÖÕ—}ÕÂπç}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–Åπï·—}ç’…ÕΩ»Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•π—ïù…Ö—•Ωπ}Õ—Ö—ïlâ±ÖÕ—}ô’±±}ÕÂπç}Ö–âtÄÙÅ•π—ïù…Ö—•Ωπ}Õ—Ö—ïlâ±ÖÕ—}ÕÂπç}Ö–ât(ÄÄÄÄÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°±Ö—ïÕ—}ëÖ—Ñ§(ÄÄÄÄÄÄÄÄÄÄÄÅÖô—ï…}çΩ’π–ÄÙÅ±ï∏°±Ö—ïÕ—}ëÖ—Ñπùï–†âç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Ãà∞Åmt§§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩµ¡±ï—îàËÅπΩ–ÅâΩΩ∞°πï·—}ç’…ÕΩ»§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡…ΩçïÕÕïë}ïŸïπ—ÃàËÅ±ï∏°ïŸïπ—}¡Öùîπùï–†âçΩ±±ïç—•Ω∏à§ÅΩ»Åmt§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÖ¡¡Ω•π—µïπ—ÃàËÅ±ï∏°•µ¡Ω…—ïë}¡ÖÂ±ΩÖëÃ§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâπï›}Ö¡¡Ω•π—µïπ—ÃàËÅÖô—ï…}çΩ’π–Ä¥ÅâïôΩ…ï}çΩ’π–∞(ÄÄÄÄÄÄÄÄÄÄÄÄâµÖ—ç°ïêàËÅµÖ—ç°ïê∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ï¡Ö•…ïë}Ö¡¡Ω•π—µïπ—ÃàËÅ…ï¡Ö•…ïê∞(ÄÄÄÄÄÄÄÅÙ§(ÄÄÄÅï·çï¡–Ä°Ö±ïπë±ÂA%……Ω»∞ÅI’π—•µï……Ω»§ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å}çÖ±ïπë±Â}…Ω’—ï}ï……Ω»°ï·å§(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçÖ±ïπë±‰Ω›ïâ°ΩΩ¨à∞Åµï—°ΩëÃılâA=MPât§)}ç…µ}Õï…•Ö±•Èïê)ëïòÅç…µ}çÖ±ïπë±Â}›ïâ°ΩΩ¨†§Ë(ÄÄÄÅ…Ö›}âΩë‰ÄÙÅ…ï≈’ïÕ–πùï—}ëÖ—Ñ°çÖç°îıQ…’î§(ÄÄÄÅÕ•ùπÖ—’…îÄÙÅ…ï≈’ïÕ–π°ïÖëï…Ãπùï–†âÖ±ïπë±‰µ]ïâ°ΩΩ¨µM•ùπÖ—’…îà∞Äàà§(ÄÄÄÅ•òÅπΩ–Å}çÖ±ïπë±Â}Õ•ùπÖ—’…ï}•Õ}ŸÖ±•ê°…Ö›}âΩë‰∞ÅÕ•ùπÖ—’…î§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâM•ùπÖ—’…îÅÖ±ïπë±‰Å•πŸÖ±•ëî∏âÙ§∞Ä–¿ƒ(ÄÄÄÅ›ïâ°ΩΩ¨ÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(ÄÄÄÅïŸïπ—}πÖµîÄÙÅÕ—»°›ïâ°ΩΩ¨πùï–†âïŸïπ–à§ÅΩ»Äàà§(ÄÄÄÅ•òÅïŸïπ—}πÖµîÅπΩ–Å•∏Å191e}]	!==-}Y9QLË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâΩ¨àËÅQ…’î∞Äâ•ùπΩ…ïêàËÅQ…’ïÙ§(ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ›ïâ°ΩΩ¨πùï–†â¡ÖÂ±ΩÖêà§ÅΩ»ÅÌÙ(ÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°¡ÖÂ±ΩÖê∞Åë•ç–§ÅΩ»ÅπΩ–Å¡ÖÂ±ΩÖêπùï–†â’…§à§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâAÖÂ±ΩÖêÅÖ±ïπë±‰Å•πŸÖ±•ëî∏âÙ§∞Ä–¿¿(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅÖ¡¡Ω•π—µïπ–∞ÅçΩπ—Öç–ÄÙÅ}ç…µ}’¡Õï…—}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ–†(ÄÄÄÄÄÄÄÅëÖ—Ñ∞(ÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖê∞(ÄÄÄÄÄÄÄÅ›ïâ°ΩΩ≠}ïŸïπ–ıïŸïπ—}πÖµî∞(ÄÄÄÄÄÄÄÅÕΩ’…çîÙâ›ïâ°ΩΩ¨à∞(ÄÄÄÄÄÄÄÅ…ïçΩ…ë}Öç—•Ÿ•—‰ıQ…’î∞(ÄÄÄÄ§(ÄÄÄÅëÖ—ÑπÕï—ëïôÖ’±–†âç…µ}çÖ±ïπë±‰à∞ÅÌÙ•lâ±ÖÕ—}ÕÂπç}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄâΩ¨àËÅQ…’î∞(ÄÄÄÄÄÄÄÄâÖ¡¡Ω•π—µïπ—}•êàËÅÖ¡¡Ω•π—µïπ–πùï–†â•êà§∞(ÄÄÄÄÄÄÄÄâçΩπ—Öç—}•êàËÅçΩπ—Öç–πùï–†â•êà§Å•òÅçΩπ—Öç–Åï±ÕîÅ9Ωπî∞(ÄÄÄÅÙ§(()I5}U1Q}M1}AI%LÄÙÅÏ(ÄÄÄÄâALàËÄƒÿ‘¿∞(ÄÄÄÄâÕ@àËÄ–»¿¿∞(ÄÄÄÄâMM%@ÄƒàËÄƒ»Ã¿∞(ÄÄÄÄâ!UUHÅYQàËÄƒ‘¿¿∞(ÄÄÄÄâMA}%9%Q%0àËÄ–Ã¿¿∞(ÄÄÄÄâMA}YàËÄÃ‡¿¿∞)Ù(()ëïòÅ}ç…µ}ëïôÖ’±—}ÕÖ±ï}¡…•çî°çΩπ—Öç–§Ë(ÄÄÄÅôΩ…µÖ—•Ω∏ÄÙÅÕ—»°çΩπ—Öç–πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§π’¡¡ï»†§(ÄÄÄÅ•òÅôΩ…µÖ—•Ω∏Å•∏ÅÏâYà∞ÄâYÅM@à∞ÄâM@ÅYâÙË(ÄÄÄÄÄÄÄÅôΩ…µÖ—•Ω∏ÄÙÄâMA}Yà(ÄÄÄÅï±•òÅôΩ…µÖ—•Ω∏ÄÙÙÄâM@àË(ÄÄÄÄÄÄÄÅôΩ…µÖ—•Ω∏ÄÙÅòâMA}ÌÕ—»°çΩπ—Öç–πùï–†ùëïÕ¡}—Â¡îú§ÅΩ»Äù%9%Q%0ú§πÕ—…•¿†§π’¡¡ï»†•Ùà(ÄÄÄÅï±•òÅôΩ…µÖ—•Ω∏ÄÙÙÄâYQàË(ÄÄÄÄÄÄÄÅôΩ…µÖ—•Ω∏ÄÙÄâ!UUHÅYQà(ÄÄÄÅï±•òÅôΩ…µÖ—•Ω∏πÕ—Ö…—Õ›•—††âMM%@à§Ë(ÄÄÄÄÄÄÄÅôΩ…µÖ—•Ω∏ÄÙÄâMM%@Äƒà(ÄÄÄÅ…ï—’…∏ÅI5}U1Q}M1}AI%Lπùï–°ôΩ…µÖ—•Ω∏∞Äàà§(()ëïòÅ}ç…µ}›Ω…≠Õ¡Öçï}âÖç≠ô•±∞°çΩπ—Öç–§Ë(ÄÄÄÄààâ©Ω’—îÅ±ïÃÅç°Öµ¡ÃÅë‘Å¡ΩÕ—îÅëîÅ—…ÖŸÖ•∞ÅÕÖπÃÅÖ±”•…ï»Å±ïÃÅëΩπª•ïÃÅï·•Õ—Öπ—ïÃ∏ààà(ÄÄÄÅç°ÖπùïêÄÙÅÖ±Õî(ÄÄÄÅëïôÖ’±—}ÕÖ±ï}¡…•çîÄÙÅ}ç…µ}ëïôÖ’±—}ÕÖ±ï}¡…•çî°çΩπ—Öç–§(ÄÄÄÅëïôÖ’±—ÃÄÙÅÏ(ÄÄÄÄÄÄÄÄâçΩµµï…ç•Ö∞àËÄàà∞(ÄÄÄÄÄÄÄÄâ—ÖùÃàËÄàà∞(ÄÄÄÄÄÄÄÄâ¡…•·}Ÿïπ—îàËÅëïôÖ’±—}ÕÖ±ï}¡…•çî∞(ÄÄÄÄÄÄÄÄâçΩ’—}ïÕ—•µîàËÄàà∞(ÄÄÄÄÄÄÄÄâë•Õ≈’Ö±•ô•çÖ—•Ωπ}…ïÖÕΩ∏àËÄàà∞(ÄÄÄÄÄÄÄÄâë•Õ≈’Ö±•ô•çÖ—•Ωπ}ëï—Ö•∞àËÄàà∞(ÄÄÄÄÄÄÄÄâ…ïÖç—•ŸÖ—•Ωπ}ëÖ—îàËÄàà∞(ÄÄÄÄÄÄÄÄâÖ…ç°•Ÿïë}Ö–àËÄàà∞(ÄÄÄÄÄÄÄÄâçΩπŸï…—ïë}Ö–àËÄàà∞(ÄÄÄÄÄÄÄÄâÕ—Ö—’Õ}ç°Öπùïë}Ö–àËÅçΩπ—Öç–πùï–†â’¡ëÖ—ïë}Ö–à§ÅΩ»ÅçΩπ—Öç–πùï–†âç…ïÖ—ïë}Ö–à§ÅΩ»Äàà∞(ÄÄÄÅÙ(ÄÄÄÅôΩ»Å≠ï‰∞ÅŸÖ±’îÅ•∏ÅëïôÖ’±—Ãπ•—ïµÃ†§Ë(ÄÄÄÄÄÄÄÅ•òÅ≠ï‰ÅπΩ–Å•∏ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—m≠ïÂtÄÙÅŸÖ±’î(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅç’……ïπ—}ÕÖ±ï}¡…•çîÄÙÅÕ—»°çΩπ—Öç–πùï–†â¡…•·}Ÿïπ—îà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ±ïùÖçÂ}ëïôÖ’±—}¡…•çîÄÙÄ†(ÄÄÄÄÄÄÄÄ°ëïôÖ’±—}ÕÖ±ï}¡…•çîÄÙÙÅI5}U1Q}M1}AI%MlâMM%@Äƒât(ÄÄÄÄÄÄÄÄÅÖπêÅç’……ïπ—}ÕÖ±ï}¡…•çîÅ•∏ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄÄà‰‡¿à∞Äà‰‡¿∏¿à∞Äà‰‡¿∏¿¿à∞Äàƒ»¿¿à∞Äàƒ»¿¿∏¿à∞Äàƒ»¿¿∏¿¿à∞(ÄÄÄÄÄÄÄÄÅÙ§(ÄÄÄÄÄÄÄÅΩ»Ä°ëïôÖ’±—}ÕÖ±ï}¡…•çîÄÙÙÅI5}U1Q}M1}AI%Mlâ!UUHÅYQât(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅç’……ïπ—}ÕÖ±ï}¡…•çîÅ•∏ÅÏàƒÿ¿¿à∞Äàƒÿ¿¿∏¿à∞Äàƒÿ¿¿∏¿¿âÙ§(ÄÄÄÄ§(ÄÄÄÅ•òÅëïôÖ’±—}ÕÖ±ï}¡…•çîÅÖπêÄ°πΩ–Åç’……ïπ—}ÕÖ±ï}¡…•çîÅΩ»Å±ïùÖçÂ}ëïôÖ’±—}¡…•çî§Ë(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâ¡…•·}Ÿïπ—îâtÄÙÅëïôÖ’±—}ÕÖ±ï}¡…•çî(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅ•òÅçΩπ—Öç–πùï–†âÕ—Ö—’–à§ÄÙÙÄâΩπŸï…—§àÅÖπêÅπΩ–ÅçΩπ—Öç–πùï–†âçΩπŸï…—ïë}Ö–à§Ë(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâçΩπŸï…—ïë}Ö–âtÄÙÅçΩπ—Öç–πùï–†â’¡ëÖ—ïë}Ö–à§ÅΩ»ÅçΩπ—Öç–πùï–†âç…ïÖ—ïë}Ö–à§ÅΩ»Å}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅ•òÅ}ç…µ}…ïçΩ…ë}Ω…•ù•∏†(ÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÅçΩπ—Öç–πùï–†âΩ…•ù•πîà§ÅΩ»ÅçΩπ—Öç–πùï–†âÕΩ’…çîà§∞(ÄÄÄÄÄÄÄÅÕΩ’…çîıçΩπ—Öç–πùï–†âÕΩ’…çîà∞Äàà§∞(ÄÄÄÄÄÄÄÅçΩπ—ï·–ıçΩπ—Öç–πùï–†âµï—Ö}ÕΩ’…çîà§∞(ÄÄÄÄÄÄÄÅëÖ—îıçΩπ—Öç–πùï–†âç…ïÖ—ïë}Ö–à§∞(ÄÄÄÄ§Ë(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅ…ï—’…∏Åç°Öπùïê(()ëïòÅ}ç…µ}¡…ï¡Ö…ï}çΩπ—Öç—Ã°ëÖ—Ñ§Ë(ÄÄÄÄààâ¡¡±•≈’îÅ±ïÃÅµ•ù…Ö—•ΩπÃÅ≥•ü°…ïÃÅï–Å±ïÃÅÕ—Ö—’—ÃÅ]=Åï∏Å’∏ÅÕï’∞Å¡ÖÕÕÖùî∏ààà(ÄÄÄÅç°ÖπùïêÄÙÅ}ç…µ}âÖç≠ô•±±}µï—Ö}Õ’âµ•ÕÕ•ΩπÃ°ëÖ—Ñ§(ÄÄÄÅÕ—Ö—’ÕïÃÄÙÅ}ç…µ}Õ—Ö—’ÕïÃ°ëÖ—Ñ§(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ›ïëΩô}ô’πë•πù}Õ—Ö—’ÕïÃÄÙÅ}›ïëΩô}ô’πë•πù}Õ—Ö—’ÕïÕ}âÂ}çΩπ—Öç–°ëÖ—Ñ§(ÄÄÄÅï·çï¡–Å·çï¡—•Ω∏ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄåÅ1îÅI4ÅëΩ•–Å…ïÕ—ï»Åë•Õ¡Ωπ•â±îÅ∑©µîÅÕ§ÅÕΩ∏ÅçÖç°îÅ]=ÅïÕ–ÅµΩµïπ—Öª•µïπ–(ÄÄÄÄÄÄÄÄåÅŸï……Ω’•±≥§ÅΩ‘Å•±±•Õ•â±î∏Å1ïÃÅëï…πß°…ïÃÅŸÖ±ï’…ÃÅ¡ï…Õ•Õ”•ïÃÅÕΩπ–ÅçΩπÕï…€•ïÃ∏(ÄÄÄÄÄÄÄÅÖ¡¿π±Ωùùï»π›Ö…π•πú†(ÄÄÄÄÄÄÄÄÄÄÄÄâMÂπç°…Ωπ•ÕÖ—•Ω∏ÅëïÃÅÕ—Ö—’—ÃÅ]=Å•ùπΩÀ•îÄ†ïÃ§à∞Å—Â¡î°ï·å§π}}πÖµï}|∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ›ïëΩô}ô’πë•πù}Õ—Ö—’ÕïÃÄÙÅÌÙ((ÄÄÄÅÖ’—ΩµÖ—•ç}ÕïçΩπëÖ…Â}±Öâï±ÃÄÙÅÕï–°I5}Q}M=9Ie}	e}MQQULπŸÖ±’ïÃ†§§(ÄÄÄÄåÅÖç†ÅçΩπ—Öç–ÅπïïëÃÅΩπ±‰Å•—ÃÅΩ›∏ÅÖ¡¡Ω•π—µïπ—Ã∏ÅMçÖππ•πúÅ—°îÅô’±∞ÅÖùïπëÑ(ÄÄÄÄåÅ¡ï»ÅçΩπ—Öç–ÅµÖëîÅïŸï…‰ÅçÖç°îÅ…ïâ’•±êÅù…Ω‹ÅÖÃÅçΩπ—Öç—ÃÄ®ÅÖ¡¡Ω•π—µïπ—Ã∏(ÄÄÄÅÖ¡¡Ω•π—µïπ—Õ}âÂ}çΩπ—Öç–ÄÙÅÌÙ(ÄÄÄÅôΩ»ÅÖ¡¡Ω•π—µïπ–Å•∏ÅëÖ—Ñπùï–†âç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Ãà∞Åmt§Ë(ÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—Õ}âÂ}çΩπ—Öç–πÕï—ëïôÖ’±–†(ÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ–πùï–†âçΩπ—Öç—}•êà§∞Åmt∞(ÄÄÄÄÄÄÄÄ§πÖ¡¡ïπê°Ö¡¡Ω•π—µïπ–§((ÄÄÄÅôΩ»Åï·•Õ—•πúÅ•∏ÅëÖ—Ñπùï–†âç…µ}çΩπ—Öç—Ãà∞Åmt§Ë(ÄÄÄÄÄÄÄÅ•òÅ}ç…µ}ç±ïÖ…}•πçΩπÕ•Õ—ïπ—}—•—…ï}Õï©Ω’…}çπÖ¡Ã°ï·•Õ—•πú§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ•òÅ}ç…µ}µ•ù…Ö—ï}…ïù•Õ—…Ö—•Ωπ}Ö¡¡Ω•π—µïπ—}Õ—Ö—’Ã°ï·•Õ—•πú§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ•òÅ}ç…µ}âÖç≠ô•±±}•πôΩ…µÖ—•Ωπ}…ï≈’ïÕ—}Ö——…•â’—•Ω∏°ï·•Õ—•πú§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ•òÅ}ç…µ}›Ω…≠Õ¡Öçï}âÖç≠ô•±∞°ï·•Õ—•πú§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ•òÅ}ç…µ}ïπôΩ…çï}µï—Ö}ëïôÖ’±—Ã°ï·•Õ—•πú§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ•òÅï·•Õ—•πúπùï–†âÕ—Ö—’—}ÕïçΩπëÖ•…îà§ÄÙÙÄâMïÕÕ•Ω∏ÅPàË(ÄÄÄÄÄÄÄÄÄÄÄÅï·•Õ—•πùlâÕ—Ö—’—}ÕïçΩπëÖ•…îâtÄÙÄâ5Ö…ç£§ÅPà(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î((ÄÄÄÄÄÄÄÅçΩπ—Öç—}•êÄÙÅÕ—»°ï·•Õ—•πúπùï–†â•êà§ÅΩ»Äàà§(ÄÄÄÄÄÄÄÅ±•Ÿï}ô’πë•πù}Õ—Ö—’ÃÄÙÅ›ïëΩô}ô’πë•πù}Õ—Ö—’ÕïÃπùï–°çΩπ—Öç—}•ê§(ÄÄÄÄÄÄÄÅ•òÄ°çΩπ—Öç—}•êÅ•∏Å›ïëΩô}ô’πë•πù}Õ—Ö—’ÕïÃ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅï·•Õ—•πúπùï–†âÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô—}ÕΩ’…çîà§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÑÙÅI5}59U1}MQQUM}M=UI(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅï·•Õ—•πúπùï–†âÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–à§ÄÑÙÅ±•Ÿï}ô’πë•πù}Õ—Ö—’Ã§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅï·•Õ—•πùlâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–âtÄÙÅ±•Ÿï}ô’πë•πù}Õ—Ö—’Ã(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î((ÄÄÄÄÄÄÄÅô’πë•πù}Õ—Ö—’ÃÄÙÅÕ—»†(ÄÄÄÄÄÄÄÄÄÄÄÅï·•Õ—•πúπùï–†âÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–à§ÅΩ»Äàà(ÄÄÄÄÄÄÄÄ§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅÖ’—ΩµÖ—•ç}ÕïçΩπëÖ…‰ÄÙÅI5}Q}M=9Ie}	e}MQQULπùï–°ô’πë•πù}Õ—Ö—’Ã§(ÄÄÄÄÄÄÄÅÕïçΩπëÖ…Â}•Õ}µÖπ’Ö∞ÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅï·•Õ—•πúπùï–†âÕ—Ö—’—}ÕïçΩπëÖ•…ï}ÕΩ’…çîà§ÄÙÙÅI5}59U1}MQQUM}M=UI(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅÕïçΩπëÖ…Â}•Õ}µÖπ’Ö∞Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÄ°Ö’—ΩµÖ—•ç}ÕïçΩπëÖ…‰(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅï·•Õ—•πúπùï–†âÕ—Ö—’—}ÕïçΩπëÖ•…îà§ÄÑÙÅÖ’—ΩµÖ—•ç}ÕïçΩπëÖ…‰§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅï·•Õ—•πùlâÕ—Ö—’—}ÕïçΩπëÖ•…îâtÄÙÅÖ’—ΩµÖ—•ç}ÕïçΩπëÖ…‰(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÄÄÄÄÅï±•òÄ°πΩ–ÅÖ’—ΩµÖ—•ç}ÕïçΩπëÖ…‰(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅï·•Õ—•πúπùï–†âÕ—Ö—’—}ÕïçΩπëÖ•…îà§Å•∏ÅÖ’—ΩµÖ—•ç}ÕïçΩπëÖ…Â}±Öâï±Ã§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅï·•Õ—•πùlâÕ—Ö—’—}ÕïçΩπëÖ•…îâtÄÙÄàà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ•òÄâÕ—Ö—’—}ÕïçΩπëÖ•…îàÅπΩ–Å•∏Åï·•Õ—•πúË(ÄÄÄÄÄÄÄÄÄÄÄÅï·•Õ—•πùlâÕ—Ö—’—}ÕïçΩπëÖ•…îâtÄÙÄàà(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î((ÄÄÄÄÄÄÄÅ•òÅï·•Õ—•πúπùï–†âÕ—Ö—’–à§ÅπΩ–Å•∏ÅÕ—Ö—’ÕïÃË(ÄÄÄÄÄÄÄÄÄÄÄÅï·•Õ—•πùlâÕ—Ö—’–âtÄÙÅÕ—Ö—’ÕïÕl¡t(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ•òÅ}ç…µ}âÖç≠ô•±±}•πôΩ…µÖ—•Ωπ}…ï≈’ïÕ—}ÖπÕ›ï…Ã°ï·•Õ—•πú§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ•òÅ}ç…µ}ïπÕ’…ï}…ï±ÖπçïÃ°ï·•Õ—•πú§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ•òÅ}ç…µ}ÕÂπç}çΩπ—Öç—}çÖ±ïπë±Â}Õ—Ö—’Ã†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅëÖ—Ñ∞Åï·•Õ—•πú∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—ÃıÖ¡¡Ω•π—µïπ—Õ}âÂ}çΩπ—Öç–πùï–°ï·•Õ—•πúπùï–†â•êà§∞Ä†§§§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ¡…ïπΩ¥ÄÙÅ}ç…µ}ôΩ…µÖ—}ô•…Õ—}πÖµî°ï·•Õ—•πúπùï–†â¡…ïπΩ¥à§§(ÄÄÄÄÄÄÄÅπΩ¥ÄÙÅ}ç…µ}ôΩ…µÖ—}±ÖÕ—}πÖµî°ï·•Õ—•πúπùï–†âπΩ¥à§§(ÄÄÄÄÄÄÄÅ•òÄ°¡…ïπΩ¥∞ÅπΩ¥§ÄÑÙÄ°ï·•Õ—•πúπùï–†â¡…ïπΩ¥à∞Äàà§∞Åï·•Õ—•πúπùï–†âπΩ¥à∞Äàà§§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅï·•Õ—•πùlâ¡…ïπΩ¥ât∞Åï·•Õ—•πùlâπΩ¥âtÄÙÅ¡…ïπΩ¥∞ÅπΩ¥(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î((ÄÄÄÅ…ï—’…∏Åç°Öπùïê∞Å›ïëΩô}ô’πë•πù}Õ—Ö—’ÕïÃ(()ëïòÅ}ç…µ}ç±ïÖ…}•πçΩπÕ•Õ—ïπ—}—•—…ï}Õï©Ω’…}çπÖ¡Ã°çΩπ—Öç–§Ë(ÄÄÄÄààâIïµΩŸîÅÑÅ…ïÕ•ëïπçîµ¡ï…µ•–ÅÖÕÕïÕÕµïπ–Åô…Ω¥ÅπΩ∏µ°Ω±ëï…ÃÅΩπ±‰∏ààà(ÄÄÄÅ•òÅ}ÂïÃ°çΩπ—Öç–πùï–†â—•—…ï}Õï©Ω’»à§§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅ•òÅπΩ–ÅÕ—»°çΩπ—Öç–πùï–†â—•—…ï}Õï©Ω’…}çπÖ¡Ãà§ÅΩ»Äàà§πÕ—…•¿†§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅçΩπ—Öç—lâ—•—…ï}Õï©Ω’…}çπÖ¡ÃâtÄÙÄàà(ÄÄÄÅ…ï—’…∏ÅQ…’î(()}I5}I}5=1}1=,ÄÙÅ—°…ïÖë•πúπI1Ωç¨†§)}I5}I}5=1}-dÄÙÅ9Ωπî)}I5}I}5=1}Y1UÄÙÅ9Ωπî(()ëïòÅ}ç…µ}…ïÖë}µΩëï±}≠ï‰†§Ë(ÄÄÄÅ›ïëΩô}Õ•ùπÖ—’…îÄÙÄ†(ÄÄÄÄÄÄÄÅ}›ïëΩô}ëâ}Õ•ùπÖ—’…î†§Å•òÅΩÃπ¡Ö—†πï·•Õ—Ã°}›ïëΩô}ëâ}¡Ö—††§§Åï±ÕîÅ9Ωπî(ÄÄÄÄ§(ÄÄÄÅ…ï—’…∏Å}ëÖ—Ö}ô•±ï}Õ•ùπÖ—’…î†§∞Å›ïëΩô}Õ•ùπÖ—’…î(()ëïòÅ}ç…µ}¡…ï¡Ö…ïë}…ïÖë}µΩëï∞†§Ë(ÄÄÄÄààâIï’ÕîÅΩπîÅ¡…ï¡Ö…ïêÅI4ÅµΩëï∞ÅÖç…ΩÕÃÅâΩΩ—Õ—…Ö¿∞Åëï—Ö•∞ÅÖπêÅ¡Ω±±•πúÅ…ïÖëÃ∏ààà(ÄÄÄÅù±ΩâÖ∞Å}I5}I}5=1}-d∞Å}I5}I}5=1}Y1U(ÄÄÄÅ›•—†Å}I5}I}5=1}1=,Ë(ÄÄÄÄÄÄÄÄåÅπΩ—°ï»Å…ïÖëï»ÅµÖ‰Å°ÖŸîÅ…ïâ’•±–Å—°îÅçÖç°îÅ›°•±îÅ—°•ÃÅ…ï≈’ïÕ–Å›Ö•—ïê∏(ÄÄÄÄÄÄÄÄåÅΩµ¡Ö…îÅ—°îÅç’……ïπ–Å…ïŸ•Õ•Ω∏ÅΩπ±‰ÅÖô—ï»ÅÖç≈’•…•πúÅ—°îÅÕ°Ö…ïêÅ±Ωç¨∏(ÄÄÄÄÄÄÄÅ≠ï‰ÄÙÅ}ç…µ}…ïÖë}µΩëï±}≠ï‰†§(ÄÄÄÄÄÄÄÅ•òÅ}I5}I}5=1}Y1UÅ•ÃÅπΩ–Å9ΩπîÅÖπêÅ}I5}I}5=1}-dÄÙÙÅ≠ï‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å}I5}I}5=1}Y1U((ÄÄÄÄÄÄÄÅôΩ»ÅÖ——ïµ¡–Å•∏Å…Öπùî†»§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}¡…ï¡Ö…ï}çΩπ—Öç—Ã°ëÖ—Ñ§(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}âÖç≠ô•±±}çÖ±±âÖç≠}…ï≈’ïÕ—Ã°ëÖ—Ñ§(ÄÄÄÄÄÄÄÄÄÄÄÅô•πÖ±}≠ï‰ÄÙÅ}ç…µ}…ïÖë}µΩëï±}≠ï‰†§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅô•πÖ±}≠ï‰ÄÙÙÅ≠ï‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}I5}I}5=1}-dÄÙÅ≠ï‰(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}I5}I}5=1}Y1UÄÙÅëÖ—Ñ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅëÖ—Ñ(ÄÄÄÄÄÄÄÄÄÄÄÄåÅÅçΩπç’……ïπ–Å›…•—îÅ±Öπëïê∏ÅIï—…‰ÅΩπçî∞Å≠ïï¡•πúÅ—°îÅ…ïŸ•Õ•Ω∏Åô…Ω¥(ÄÄÄÄÄÄÄÄÄÄÄÄåÅ	=IÅ—°îÅ±ΩÖêÅÕºÅÖ∏ÅΩ±ëï»ÅÕπÖ¡Õ°Ω–ÅçÖππΩ–Åç±Ö•¥ÅÑÅπï›ï»Å≠ï‰∏(ÄÄÄÄÄÄÄÄÄÄÄÅ≠ï‰ÄÙÅô•πÖ±}≠ï‰(ÄÄÄÄÄÄÄÄåÅUπëï»ÅçΩπ—•π’Ω’ÃÅ›…•—ïÃ∞ÅÕï…ŸîÅ—°•ÃÅâΩ’πëïêÅÖ——ïµ¡–Åâ’–Å±ï–Å—°îÅπï·–(ÄÄÄÄÄÄÄÄåÅ…ïÖëï»Å…ïâ’•±ê∏Å9ïŸï»ÅçÖç°îÅΩ±êÅçΩπ—ïπ–Å’πëï»Å—°îÅ±Ö—ïÕ–Å…ïŸ•Õ•Ω∏∏(ÄÄÄÄÄÄÄÅ}I5}I}5=1}-dÄÙÅ9Ωπî(ÄÄÄÄÄÄÄÅ}I5}I}5=1}Y1UÄÙÅ9Ωπî(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅëÖ—Ñ(()ëïòÅ}ç…µ}¡ï…Õ•Õ—}¡…ï¡Ö…ïë}çΩπ—Öç—}•ô}•ë±î°¡…ï¡Ö…ïë}çΩπ—Öç–§Ë(ÄÄÄÄààâAï…Õ•Õ–ÅÑÅΩπîµ—•µîÅ±ïùÖç‰Åµ•ù…Ö—•Ω∏Å›•—°Ω’–ÅïŸï»Å›Ö•—•πúÅâï°•πêÅ]=∏ààà(ÄÄÄÅÖç≈’•…ïêÄÙÅ}I5}I=9%1%Q%=9}1=,πÖç≈’•…î°â±Ωç≠•πúıÖ±Õî§(ÄÄÄÅ•òÅπΩ–ÅÖç≈’•…ïêË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅµΩëï±}≠ï‰ÄÙÅ}I5}I}5=1}-d(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅµΩëï±}≠ï‰ÅΩ»ÅµΩëï±}≠ïÂl¡tÄÑÙÅ}ëÖ—Ö}ô•±ï}Õ•ùπÖ—’…î†§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÄÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÄÄÄÄÅÕ—Ω…ïêÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞Å¡…ï¡Ö…ïë}çΩπ—Öç–πùï–†â•êà§§(ÄÄÄÄÄÄÄÅ•òÅÕ—Ω…ïêÅ•ÃÅ9ΩπîÅΩ»ÅÕ—Ω…ïêÄÙÙÅ¡…ï¡Ö…ïë}çΩπ—Öç–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÄÄÄÄÅ•πëï‡ÄÙÅëÖ—Ölâç…µ}çΩπ—Öç—Ãâtπ•πëï‡°Õ—Ω…ïê§(ÄÄÄÄÄÄÄÅëÖ—Ölâç…µ}çΩπ—Öç—Ãâum•πëï·tÄÙÅçΩ¡‰πëïï¡çΩ¡‰°¡…ï¡Ö…ïë}çΩπ—Öç–§(ÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅQ…’î(ÄÄÄÅô•πÖ±±‰Ë(ÄÄÄÄÄÄÄÅ}I5}I=9%1%Q%=9}1=,π…ï±ïÖÕî†§(()I5}=9QQ}MU55Ie}%1LÄÙÄ†(ÄÄÄÄâ•êà∞Äâ¡…ïπΩ¥à∞ÄâπΩ¥à∞Äâ—ï±ï¡°Ωπîà∞ÄâµÖ•∞à∞ÄâôΩ…µÖ—•Ω∏à∞Äâ±•ï‘à∞(ÄÄÄÄâÕ—Ö—’–à∞ÄâÕ—Ö—’—}ÕïçΩπëÖ•…îà∞ÄâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–à∞(ÄÄÄÄâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô—}ÕΩ’…çîà∞ÄâëÖ—ïÕ}ôΩ…µÖ—•Ω∏à∞Äâç¡òà∞(ÄÄÄÄâç¡ô}µΩπ—Öπ–à∞Äâç¡ô}¡Ö±•ï»à∞Äâô•πÖπçïµïπ—}ô–à∞ÄâµΩπ—Öπ—}ÖççΩ…ëï}ô–à∞(ÄÄÄÄâô•πÖπçïµïπ—}¡ï…ÕΩ}¡ΩÕÕ•â±îà∞Äâ…ïô’Õ}ô—}¡ï…Õºà∞Äâ…ïÕ—ï}Ö}ç°Ö…ùï}¡ï…Õºà∞(ÄÄÄÄâ•ëïπ—•—ï}ç…ïÖ—•Ω∏à∞Äâ•ëïπ—•—ï}Ω¨à∞Äâ•πÕç…•—}ô–à∞ÄâëïÕ¡}—Â¡îà∞(ÄÄÄÄâçÖ…—ï}¡…ºà∞Äâ—•—…ï}Õï©Ω’»à∞Äâ—•—…ï}Õï©Ω’…}çπÖ¡Ãà∞ÄâùÖ…ëï}Ÿ’îà∞(ÄÄÄÄâÖπ—ïçïëïπ—Ãà∞ÄâçΩµ¡—ï}çπÖ¡Ãà∞ÄâçπÖ¡Õ}π’àà∞Äâ•π—ïù…Ö—•Ωπ}ë…ÖçÖ»à∞(ÄÄÄÄâΩ…•ù•πîà∞ÄâÕΩ’…çîà∞ÄâÕΩ’…çï}ëï—Ö•∞à∞ÄâÕΩ’…çï}°•Õ—Ω…‰à∞ÄâçΩµµï…ç•Ö∞à∞Äâ—ÖùÃà∞(ÄÄÄÄâ¡…•·}Ÿïπ—îà∞ÄâçΩ’—}ïÕ—•µîà∞Äâ…ï±Öπçï}ëÖ—îà∞Äâ¡…Ωç°Ö•πï}Öç—•Ωπ}µÖπ’ï±±îà∞(ÄÄÄÄâÖ…ç°•Ÿïë}Ö–à∞(ÄÄÄÄâçΩπŸï…—ïë}Ö–à∞ÄâÕ—Ö—’Õ}ç°Öπùïë}Ö–à∞Äâë•Õ≈’Ö±•ô•çÖ—•Ωπ}…ïÖÕΩ∏à∞(ÄÄÄÄâë•Õ≈’Ö±•ô•çÖ—•Ωπ}ëï—Ö•∞à∞Äâ…ïÖç—•ŸÖ—•Ωπ}ëÖ—îà∞Äâç…ïÖ—ïë}Ö–à∞(ÄÄÄÄâ…ïçï•Ÿïë}Ö–à∞Äâ’¡ëÖ—ïë}Ö–à∞ÄâçΩµµïπ—Ö•…ïÃà∞Äâ›ïëΩô}Õ—Ö—’Ãà∞(ÄÄÄÄâ≈’Ö±•ô•çÖ—•Ωπ}ô±Öúà∞(§)I5}=9QQ}Q%Y%Qe}-%9LÄÙÅÏâÖ¡¡ï∞à∞ÄâïµÖ•∞à∞ÄâÕµÃà∞ÄâëïµÖπëï}…Ö¡¡ï∞âÙ)I5}Q%Y%Qe}MQ%=9LÄÙÅÏâπΩ—•ô•çÖ—•ΩπÃà∞Äâô•∞µÖç—‘âÙ)I5}=U9Q}I19}MQQUMLÄÙÅÏâÕç°ïë’±ïêà∞ÄâÖπÕ›ï…ïêà∞ÄâπΩ}ÖπÕ›ï»âÙ)I5}911}AA=%9Q59Q}MQQUMLÄÙÅÏâçÖπçï±ïêà∞ÄâçÖπçï±±ïêâÙ(()ëïòÅ}ç…µ}Ö¡¡Ω•π—µïπ—}çΩ’π—Õ}âÂ}çΩπ—Öç–°ëÖ—Ñ§Ë(ÄÄÄÄààâ%πëï·îÅ±ïÃÅ…ïπëïËµŸΩ’ÃÅ¡ÖÕœ•ÃÅΩ‘ÉÄÅŸïπ•»ÅÕÖπÃÅçΩµ¡—ï»Å±ïÃÅÖππ’±Ö—•ΩπÃ∏ààà(ÄÄÄÅçΩ’π—ÃÄÙÅÌÙ(ÄÄÄÅôΩ»ÅÖ¡¡Ω•π—µïπ–Å•∏ÅëÖ—Ñπùï–†âç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Ãà∞Åmt§Ë(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°Ö¡¡Ω•π—µïπ–∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅçΩπ—Öç—}•êÄÙÅÕ—»°Ö¡¡Ω•π—µïπ–πùï–†âçΩπ—Öç—}•êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅÕ—Ö—’ÃÄÙÅÕ—»°Ö¡¡Ω•π—µïπ–πùï–†âÕ—Ö—’Ãà§ÅΩ»ÄâÖç—•Ÿîà§πÕ—…•¿†§π±Ω›ï»†§(ÄÄÄÄÄÄÄÅ•òÄ°πΩ–ÅçΩπ—Öç—}•êÅΩ»ÅπΩ–ÅÖ¡¡Ω•π—µïπ–πùï–†âÕ—Ö…—}—•µîà§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ»ÅÕ—Ö—’ÃÅ•∏ÅI5}911}AA=%9Q59Q}MQQUML§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅçΩ’π—ÕmçΩπ—Öç—}•ëtÄÙÅçΩ’π—Ãπùï–°çΩπ—Öç—}•ê∞Ä¿§Ä¨Äƒ(ÄÄÄÅ…ï—’…∏ÅçΩ’π—Ã(()ëïòÅ}ç…µ}çΩπ—Öç—}Öç—•Ÿ•—Â}çΩ’π—Ã°çΩπ—Öç–∞ÅÖ¡¡Ω•π—µïπ—}çΩ’π–Ù¿§Ë(ÄÄÄÄààâIï—Ω’…πîÅ±ïÃÅçΩµ¡—ï’…ÃÅ≥•ùï…ÃÅÖôô•ç£•ÃÅëÖπÃÅ±ÑÅ±•Õ—îÅëïÃÅçΩπ—Öç—Ã∏ààà(ÄÄÄÅç°Öππï±}çΩ’π—ÃÄÙÅÏâïµÖ•∞àËÄ¿∞ÄâÕµÃàËÄ¡Ù(ÄÄÄÅôΩ»ÅÖç—•Ÿ•—‰Å•∏ÅçΩπ—Öç–πùï–†âÖç—•Ÿ•—•ïÃà∞Åmt§Ë(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°Öç—•Ÿ•—‰∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ≠•πêÄÙÅÕ—»°Öç—•Ÿ•—‰πùï–†â≠•πêà§ÅΩ»Äàà§πÕ—…•¿†§π±Ω›ï»†§(ÄÄÄÄÄÄÄÅ•òÅ≠•πêÅ•∏Åç°Öππï±}çΩ’π—ÃË(ÄÄÄÄÄÄÄÄÄÄÄÅç°Öππï±}çΩ’π—Õm≠•πëtÄ¨ÙÄƒ((ÄÄÄÅ…ï±Öπçï}çΩ’π–ÄÙÅÕ’¥†(ÄÄÄÄÄÄÄÄƒÅôΩ»Å…ï±ÖπçîÅ•∏ÅçΩπ—Öç–πùï–†â…ï±ÖπçïÃà∞Åmt§(ÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°…ï±Öπçî∞Åë•ç–§(ÄÄÄÄÄÄÄÅÖπêÅÕ—»°…ï±Öπçîπùï–†âÕ—Ö—’Ãà§ÅΩ»ÄâÕç°ïë’±ïêà§πÕ—…•¿†§π±Ω›ï»†§(ÄÄÄÄÄÄÄÅ•∏ÅI5}=U9Q}I19}MQQUML(ÄÄÄÄÄÄÄÅÖπêÅâΩΩ∞°…ï±Öπçîπùï–†âÕç°ïë’±ïë}ëÖ—îà§§(ÄÄÄÄ§(ÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄâÖ¡¡Ω•π—µïπ—ÃàËÅµÖ‡†¿∞Å•π–°Ö¡¡Ω•π—µïπ—}çΩ’π–ÅΩ»Ä¿§§∞(ÄÄÄÄÄÄÄÄâ…ï±ÖπçïÃàËÅ…ï±Öπçï}çΩ’π–∞(ÄÄÄÄÄÄÄÄâïµÖ•±ÃàËÅç°Öππï±}çΩ’π—ÕlâïµÖ•∞ât∞(ÄÄÄÄÄÄÄÄâÕµÃàËÅç°Öππï±}çΩ’π—ÕlâÕµÃât∞(ÄÄÄÅÙ(()ëïòÅ}ç…µ}çΩµ¡Öç—}çΩπ—Öç—}Öç—•Ÿ•—•ïÃ°çΩπ—Öç–§Ë(ÄÄÄÄààâΩπÕï…ŸîÅ’π•≈’ïµïπ–Å±ïÃÅµÖ…≈’ï’…ÃÅª•çïÕÕÖ•…ïÃÅÖ’‡Å±•Õ—ïÃÅï–ÅÕ—Ö—•Õ—•≈’ïÃ∏ààà(ÄÄÄÅÖç—•Ÿ•—•ïÃÄÙÅl(ÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅçΩπ—Öç–πùï–†âÖç—•Ÿ•—•ïÃà∞Åmt§(ÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°•—ï¥∞Åë•ç–§ÅÖπêÅ•—ï¥πùï–†âëÖ—îà§(ÄÄÄÅt(ÄÄÄÅ•òÅπΩ–ÅÖç—•Ÿ•—•ïÃË(ÄÄÄÄÄÄÄÅ…ï—’…∏Åmt(ÄÄÄÅπï›ïÕ–ÄÙÅµÖ‡°Öç—•Ÿ•—•ïÃ∞Å≠ï‰ı±ÖµâëÑÅ•—ï¥ËÅÕ—»°•—ï¥πùï–†âëÖ—îà§ÅΩ»Äàà§§(ÄÄÄÅçΩπ—Öç—ïêÄÙÅl(ÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅÖç—•Ÿ•—•ïÃ(ÄÄÄÄÄÄÄÅ•òÅ•—ï¥πùï–†â≠•πêà§Å•∏ÅI5}=9QQ}Q%Y%Qe}-%9L(ÄÄÄÅt(ÄÄÄÅÕï±ïç—ïêÄÙÅmπï›ïÕ—t(ÄÄÄÅ•òÅçΩπ—Öç—ïêË(ÄÄÄÄÄÄÄÅ±Ö—ïÕ—}çΩπ—Öç–ÄÙÅµÖ‡†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—ïê∞Å≠ï‰ı±ÖµâëÑÅ•—ï¥ËÅÕ—»°•—ï¥πùï–†âëÖ—îà§ÅΩ»Äàà§(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ•òÅ±Ö—ïÕ—}çΩπ—Öç–Å•ÃÅπΩ–Åπï›ïÕ–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÕï±ïç—ïêπÖ¡¡ïπê°±Ö—ïÕ—}çΩπ—Öç–§(ÄÄÄÅ…ï—’…∏Ål(ÄÄÄÄÄÄÄÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ•êàËÅ•—ï¥πùï–†â•êà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ≠•πêàËÅ•—ï¥πùï–†â≠•πêà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâëÖ—îàËÅ•—ï¥πùï–†âëÖ—îà§∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÅôΩ»Å•—ï¥Å•∏ÅÕï±ïç—ïê(ÄÄÄÅt(()ëïòÅ}ç…µ}çΩπ—Öç—}Õ’µµÖ…Â}…ïÕ¡ΩπÕî°çΩπ—Öç–∞ÅëÖ—Ñ∞Ä®∞Åô’πë•πù}Õ—Ö—’Ãı9Ωπî∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖç—•Ÿ•—•ïÃı9Ωπî∞Å¡’â±•çÖ—•ΩπÃı9Ωπî∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—}çΩ’π–Ù¿∞Åç¡ô}Õ—Ö—’ÃÙàà§Ë(ÄÄÄÄààâΩπÕ—…’•–Å’πîÅô•ç°îÅ≥•ü°…îÄÏÅ±îÅì•—Ö•∞ÅçΩµ¡±ï–Å…ïÕ—îÅç°Ö…ü§ÉÄÅ±ÑÅëïµÖπëî∏ààà(ÄÄÄÅÕ’µµÖ…‰ÄÙÅÏ(ÄÄÄÄÄÄÄÅ≠ï‰ËÅçΩπ—Öç–πùï–°≠ï‰§(ÄÄÄÄÄÄÄÅôΩ»Å≠ï‰Å•∏ÅI5}=9QQ}MU55Ie}%1L(ÄÄÄÄÄÄÄÅ•òÅ≠ï‰Å•∏ÅçΩπ—Öç–(ÄÄÄÅÙ(ÄÄÄÅÕ’µµÖ…Âlâ≈’Ö±•ô•çÖ—•Ωπ}ô±ÖúâtÄÙÅÕ—»°çΩπ—Öç–πùï–†â≈’Ö±•ô•çÖ—•Ωπ}ô±Öúà§ÅΩ»Äàà§(ÄÄÄÅÕ’µµÖ…Âlâç¡ô}Õ—Ö—’ÃâtÄÙÅç¡ô}Õ—Ö—’Ã(ÄÄÄÅ•òÄ°ô’πë•πù}Õ—Ö—’Ã(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅçΩπ—Öç–πùï–†âÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô—}ÕΩ’…çîà§(ÄÄÄÄÄÄÄÄÄÄÄÄÑÙÅI5}59U1}MQQUM}M=UI§Ë(ÄÄÄÄÄÄÄÅÕ’µµÖ…ÂlâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–âtÄÙÅô’πë•πù}Õ—Ö—’Ã(ÄÄÄÅÕπÖ¡Õ°Ω–ÄÙÅëÖ—Ñπùï–†âç…µ}çπÖ¡Õ}ÕçΩ…•πù}ÕπÖ¡Õ°Ω—Ãà∞ÅÌÙ§πùï–†(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†â•êà§§(ÄÄÄÄ§(ÄÄÄÅïôôïç—•Ÿï}çΩπ—Öç–ÄÙÅë•ç–°çΩπ—Öç–§(ÄÄÄÅïôôïç—•Ÿï}çΩπ—Öç–πÕï—ëïôÖ’±–†(ÄÄÄÄÄÄÄÄâô•πÖπçïµïπ—}¡ï…ÕΩ}¡ΩÕÕ•â±îà∞(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†â…ïô’Õ}ô—}¡ï…Õºà§ÅΩ»Äàà§∞(ÄÄÄÄ§(ÄÄÄÅ•òÅÕ’µµÖ…‰πùï–†âÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–à§Ë(ÄÄÄÄÄÄÄÅïôôïç—•Ÿï}çΩπ—Öç—lâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–âtÄÙÅÕ’µµÖ…Âl(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–à(ÄÄÄÄÄÄÄÅt(ÄÄÄÅÕçΩ…îÄÙÅçÖ±ç’±Ö—ï}çÖπë•ëÖ—ï}•π—ïù…Ö—•Ωπ}ÕçΩ…î°ïôôïç—•Ÿï}çΩπ—Öç–∞ÅÕπÖ¡Õ°Ω–§(ÄÄÄÅÕ’µµÖ…Âlâ•π—ïù…Ö—•Ωπ}ÕçΩ…îâtÄÙÅÏ(ÄÄÄÄÄÄÄÅ≠ï‰ËÅÕçΩ…îπùï–°≠ï‰§(ÄÄÄÄÄÄÄÅôΩ»Å≠ï‰Å•∏Ä†(ÄÄÄÄÄÄÄÄÄÄÄÄâÕçΩ…îà∞Äâ±ïŸï∞à∞Äâ±Öâï∞à∞ÄâΩ¡ï…Ö—•ΩπÖ±}Õ—Ö—’Ãà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâô•πÖπç•Ö±}ÕçΩ…îà∞ÄâÕçΩ…ï}çΩµ¡±ï—îà∞ÄâÕçΩ…ï}ïÕ—•µÖ—ïêà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâëÖ—Ö}çΩπô•ëïπçï}¡ï…çïπ–à∞Äâ’πÕïç’…ïë}ÖµΩ’π—}ï’»à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ïµÖ•π•πù}—Ω}ô•πÖπçï}µÖ·}ï’»à∞Äâ…ïù’±Ö—Ω…Â}Ö¡¡±•çÖâ±îà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ïù’±Ö—Ω…Â}Õ—Ö—’Ãà∞Äâ…ïù’±Ö—Ω…Â}±Öâï∞à∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅÙ(ÄÄÄÄåÅïÃÅëï’‡ÅΩâ©ï—ÃÅÕΩπ–Å¡ï—•—ÃÅï–ÅÕï…Ÿïπ–ÅÖ‘Å—Öâ±ïÖ‘ÅëîÅâΩ…êÅêùÖç≈’•Õ•—•Ω∏∏(ÄÄÄÅÕ’µµÖ…Âlâµï—Ö}ÕΩ’…çîâtÄÙÅçΩπ—Öç–πùï–†âµï—Ö}ÕΩ’…çîà§ÅΩ»ÅÌÙ(ÄÄÄÅÕ’µµÖ…ÂlâŸÖï}ï±•ù•â•±•—‰âtÄÙÅçΩπ—Öç–πùï–†âŸÖï}ï±•ù•â•±•—‰à§(ÄÄÄÄåÅ1îÅ—Öâ±ïÖ‘ÅëîÅâΩ…êÅΩΩù±îÅëÃÅ…óùΩ•–Å’π•≈’ïµïπ–Å±ïÃÅç≥•ÃÅêùÖ——…•â’—•Ω∏(ÄÄÄÄåÅ’—•±ïÃ∏Å1îÅôΩ…µ’±Ö•…îÅçΩµ¡±ï–∞Å¡Ω—ïπ—•ï±±ïµïπ–ÅŸΩ±’µ•πï’‡∞Å…ïÕ—îÅÀ•Õï…€§(ÄÄÄÄåÉÄÅ±ÑÅô•ç°îÅì•—Ö•±≥•î∏(ÄÄÄÅôΩ…¥ÄÙÅçΩπ—Öç–πùï–†âôΩ…µ’±Ö•…îà§(ÄÄÄÅôΩ…¥ÄÙÅôΩ…¥Å•òÅ•Õ•πÕ—Öπçî°ôΩ…¥∞Åë•ç–§Åï±ÕîÅÌÙ(ÄÄÄÅùΩΩù±ï}ÖëÕ}—…Öç≠•πúÄÙÅÏ(ÄÄÄÄÄÄÄÅ≠ï‰ËÅâΩΩ∞°çΩπ—Öç–πùï–°≠ï‰§ÅΩ»ÅôΩ…¥πùï–°≠ï‰§§(ÄÄÄÄÄÄÄÅôΩ»Å≠ï‰Å•∏ÅI5}==1}M}%9Q%%I}-eL(ÄÄÄÅÙ(ÄÄÄÅùΩΩù±ï}ÖëÕ}—…Öç≠•πúπ’¡ëÖ—î°Ï(ÄÄÄÄÄÄÄÅ≠ï‰ËÅÕ—»°çΩπ—Öç–πùï–°≠ï‰§ÅΩ»ÅôΩ…¥πùï–°≠ï‰§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅôΩ»Å≠ï‰Å•∏ÅI5}==1}M}QI-%9}-eL(ÄÄÄÄÄÄÄÅ•òÅ≠ï‰ÅπΩ–Å•∏ÅI5}==1}M}%9Q%%I}-eL(ÄÄÄÄÄÄÄÅÖπêÄ°çΩπ—Öç–πùï–°≠ï‰§ÅΩ»ÅôΩ…¥πùï–°≠ï‰§§(ÄÄÄÅÙ§(ÄÄÄÅÕ’µµÖ…ÂlâùΩΩù±ï}ÖëÕ}—…Öç≠•πúâtÄÙÅùΩΩù±ï}ÖëÕ}—…Öç≠•πú(ÄÄÄÄåÅ1ïÃÅ…ï±ÖπçïÃÅ…ïÕ—ïπ–Åë•Õ¡Ωπ•â±ïÃÅÕ’»Å±ÑÅŸ’îÅì•ëß•î∞ÅµÖ•ÃÅ±ïÃÅîµµÖ•±Ã∞(ÄÄÄÄåÅÖ¡ïÀù’ÃÅ!Q50∞ÅÀ•¡ΩπÕïÃÅ5QÅï–ÅÖ’—…ïÃÅç°Öµ¡ÃÅ±Ω’…ëÃÅπîÅ¡Ö…—ïπ–Å¡±’ÃÅ•ç§∏(ÄÄÄÅÕ’µµÖ…Âlâ…ï±ÖπçïÃâtÄÙÅçΩπ—Öç–πùï–†â…ï±ÖπçïÃà∞Åmt§(ÄÄÄÅÕ’µµÖ…ÂlâÖç—•Ÿ•—•ïÃâtÄÙÄ†(ÄÄÄÄÄÄÄÅÖç—•Ÿ•—•ïÃ(ÄÄÄÄÄÄÄÅ•òÅÖç—•Ÿ•—•ïÃÅ•ÃÅπΩ–Å9Ωπî(ÄÄÄÄÄÄÄÅï±ÕîÅ}ç…µ}çΩµ¡Öç—}çΩπ—Öç—}Öç—•Ÿ•—•ïÃ°çΩπ—Öç–§(ÄÄÄÄ§(ÄÄÄÅÕ’µµÖ…ÂlâÖç—•Ÿ•—Â}çΩ’π—ÃâtÄÙÅ}ç…µ}çΩπ—Öç—}Öç—•Ÿ•—Â}çΩ’π—Ã†(ÄÄÄÄÄÄÄÅçΩπ—Öç–∞ÅÖ¡¡Ω•π—µïπ—}çΩ’π–∞(ÄÄÄÄ§(ÄÄÄÅÕ’µµÖ…Âlâ¡’â±•çÖ—•ΩπÃâtÄÙÅ¡’â±•çÖ—•ΩπÃÅ•òÅ¡’â±•çÖ—•ΩπÃÅ•ÃÅπΩ–Å9ΩπîÅï±ÕîÅmt(ÄÄÄÅÕ’µµÖ…Âlâ}Õ’µµÖ…‰âtÄÙÅQ…’î(ÄÄÄÅ…ï—’…∏ÅÕ’µµÖ…‰(()ëïòÅ}ç…µ}çΩπ—Öç—}Õ’µµÖ…•ïÕ}¡ÖÂ±ΩÖê°ëÖ—Ñ∞ÅÕïç—•Ω∏Ùàà∞Ä®∞Å¡…ï¡Ö…ïêıÖ±Õî§Ë(ÄÄÄÄààâAÀ•¡Ö…îÅ’∏Å•πÕ—Öπ—Öª§ÅçΩµ¡Öç–ÅÖëÖ¡”§ÉÄÅ±ÑÅ…’â…•≈’îÅëïµÖπì•î∏((ÄÄÄÅ1ïÃÅÖπç•ïππïÃÅÀ•¡ΩπÕïÃÅ…ïπŸΩÂÖ•ïπ–Åç°Ö≈’îÅô•ç°îÅçΩµ¡≥°—î∞ÅπΩ—Öµµïπ–Å±ïÃ(ÄÄÄÅÖ¡ïÀù’ÃÅêùîµµÖ•±ÃÅ!Q50Åï–Å±ïÃÅÀ•¡ΩπÕïÃÅâ…’—ïÃÅÖ’‡ÅôΩ…µ’±Ö•…ïÃ∏Å∏Å¡…Ωë’ç—•Ω∏(ÄÄÄÅçï±ÑÅ…ï¡À•Õïπ—Ö•–Å¡±’ÃÅëîÄ‘Å5ºÉÄÅç°Ö≈’îÅΩ’Ÿï…—’…îÅë‘ÅI4∏(ÄÄÄÄààà(ÄÄÄÅ•òÅ¡…ï¡Ö…ïêË(ÄÄÄÄÄÄÄÅç°Öπùïê∞Å›ïëΩô}ô’πë•πù}Õ—Ö—’ÕïÃÄÙÅÖ±Õî∞ÅÌÙ(ÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÅç°Öπùïê∞Å›ïëΩô}ô’πë•πù}Õ—Ö—’ÕïÃÄÙÅ}ç…µ}¡…ï¡Ö…ï}çΩπ—Öç—Ã°ëÖ—Ñ§(ÄÄÄÅ›ïëΩô}ç¡ô}Õ—Ö—ïÃÄÙÅ}›ïëΩô}ç¡ô}Õ—Ö—ïÕ}âÂ}çΩπ—Öç–°ëÖ—Ñ§(ÄÄÄÅÖ¡¡Ω•π—µïπ—}çΩ’π—ÃÄÙÅ}ç…µ}Ö¡¡Ω•π—µïπ—}çΩ’π—Õ}âÂ}çΩπ—Öç–°ëÖ—Ñ§(ÄÄÄÅ•πç±’ëï}Öç—•Ÿ•—‰ÄÙÅÕ—»°Õïç—•Ω∏ÅΩ»Äàà§πÕ—…•¿†§π±Ω›ï»†§Å•∏ÅI5}Q%Y%Qe}MQ%=9L(ÄÄÄÅÖç—•Ÿ•—Â}âÂ}çΩπ—Öç–ÄÙÅÌÙ(ÄÄÄÅ¡’â±•çÖ—•Ωπ}âÂ}çΩπ—Öç–ÄÙÅÌÙ(ÄÄÄÅ•òÅ•πç±’ëï}Öç—•Ÿ•—‰Ë(ÄÄÄÄÄÄÄÅÖç—•Ÿ•—Â}…Ω›ÃÄÙÅÕΩ…—ïê†(ÄÄÄÄÄÄÄÄÄÄÄÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ°Õ—»°•—ï¥πùï–†âëÖ—îà§ÅΩ»Äàà§∞ÅÕ—»°çΩπ—Öç–πùï–†â•êà§ÅΩ»Äàà§∞Å•—ï¥§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅôΩ»ÅçΩπ—Öç–Å•∏ÅëÖ—Ñπùï–†âç…µ}çΩπ—Öç—Ãà∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅôΩ»Å•—ï¥Å•∏ÅçΩπ—Öç–πùï–†âÖç—•Ÿ•—•ïÃà∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°•—ï¥∞Åë•ç–§(ÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ≠ï‰ı±ÖµâëÑÅ…Ω‹ËÅ…Ω›l¡t∞(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïŸï…ÕîıQ…’î∞(ÄÄÄÄÄÄÄÄ•lËƒ‘¡t(ÄÄÄÄÄÄÄÅôΩ»Å|∞ÅçΩπ—Öç—}•ê∞Å•—ï¥Å•∏ÅÖç—•Ÿ•—Â}…Ω›ÃË(ÄÄÄÄÄÄÄÄÄÄÄÅÖç—•Ÿ•—Â}âÂ}çΩπ—Öç–πÕï—ëïôÖ’±–°çΩπ—Öç—}•ê∞Åmt§πÖ¡¡ïπê°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ≠ï‰ËÅ•—ï¥πùï–°≠ï‰§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅôΩ»Å≠ï‰Å•∏Ä†â•êà∞ÄâëÖ—îà∞Äâ≠•πêà∞Äâ—•—±îà∞Äâëï—Ö•∞à∞ÄâÖ’—°Ω»à§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ≠ï‰Å•∏Å•—ï¥(ÄÄÄÄÄÄÄÄÄÄÄÅÙ§(ÄÄÄÄÄÄÄÅ¡’â±•çÖ—•Ωπ}âÂ}çΩπ—Öç–ÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†â•êà§ÅΩ»Äàà§ËÅçΩπ—Öç–πùï–†â¡’â±•çÖ—•ΩπÃà∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»ÅçΩπ—Öç–Å•∏ÅëÖ—Ñπùï–†âç…µ}çΩπ—Öç—Ãà∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç–πùï–†â¡’â±•çÖ—•ΩπÃà§(ÄÄÄÄÄÄÄÅÙ((ÄÄÄÅçΩπ—Öç—ÃÄÙÅmt(ÄÄÄÅôΩ»ÅçΩπ—Öç–Å•∏ÅëÖ—Ölâç…µ}çΩπ—Öç—ÃâtË(ÄÄÄÄÄÄÄÅçΩπ—Öç—}•êÄÙÅÕ—»°çΩπ—Öç–πùï–†â•êà§ÅΩ»Äàà§(ÄÄÄÄÄÄÄÅçΩπ—Öç—ÃπÖ¡¡ïπê°}ç…µ}çΩπ—Öç—}Õ’µµÖ…Â}…ïÕ¡ΩπÕî†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÅëÖ—Ñ∞(ÄÄÄÄÄÄÄÄÄÄÄÅô’πë•πù}Õ—Ö—’Ãı›ïëΩô}ô’πë•πù}Õ—Ö—’ÕïÃπùï–°çΩπ—Öç—}•ê§∞(ÄÄÄÄÄÄÄÄÄÄÄÅç¡ô}Õ—Ö—’Ãı›ïëΩô}ç¡ô}Õ—Ö—ïÃπùï–°çΩπ—Öç—}•ê∞Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÅÖç—•Ÿ•—•ïÃÙ°Öç—•Ÿ•—Â}âÂ}çΩπ—Öç–πùï–°çΩπ—Öç—}•ê∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ•πç±’ëï}Öç—•Ÿ•—‰Åï±ÕîÅ9Ωπî§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ¡’â±•çÖ—•ΩπÃÙ°¡’â±•çÖ—•Ωπ}âÂ}çΩπ—Öç–πùï–°çΩπ—Öç—}•ê∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ•πç±’ëï}Öç—•Ÿ•—‰Åï±ÕîÅ9Ωπî§∞(ÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—}çΩ’π–ıÖ¡¡Ω•π—µïπ—}çΩ’π—Ãπùï–°çΩπ—Öç—}•ê∞Ä¿§∞(ÄÄÄÄÄÄÄÄ§§(ÄÄÄÅ…ï—’…∏ÅçΩπ—Öç—Ã∞Åç°Öπùïê(()ëïòÅ}ç…µ}çΩπ—Öç—Õ}¡ÖÂ±ΩÖê°ëÖ—Ñ§Ë(ÄÄÄÄààâΩπÕï…ŸîÅ±ÑÅÀ•¡ΩπÕîÅ°•Õ—Ω…•≈’îÅçΩµ¡≥°—îÅ¡Ω’»Å±ïÃÅ•π”•ù…Ö—•ΩπÃÅï·¡±•ç•—ïÃ∏ààà(ÄÄÄÅç°Öπùïê∞Å›ïëΩô}ô’πë•πù}Õ—Ö—’ÕïÃÄÙÅ}ç…µ}¡…ï¡Ö…ï}çΩπ—Öç—Ã°ëÖ—Ñ§(ÄÄÄÅçΩπ—Öç—ÃÄÙÅl(ÄÄÄÄÄÄÄÅ}ç…µ}çΩπ—Öç—}…ïÕ¡ΩπÕî†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÅëÖ—Ñ∞(ÄÄÄÄÄÄÄÄÄÄÄÅô’πë•πù}Õ—Ö—’Ãı›ïëΩô}ô’πë•πù}Õ—Ö—’ÕïÃπùï–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†â•êà§ÅΩ»Äàà§(ÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅôΩ»ÅçΩπ—Öç–Å•∏ÅëÖ—Ölâç…µ}çΩπ—Öç—Ãât(ÄÄÄÅt(ÄÄÄÅ…ï—’…∏ÅçΩπ—Öç—Ã∞Åç°Öπùïê(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—Ãà∞Åµï—°ΩëÃılâPà∞ÄâA=MPât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çΩπ—Öç—Ã†§Ë(ÄÄÄÅ•òÅ…ï≈’ïÕ–πµï—°ΩêÄÙÙÄâPàË(ÄÄÄÄÄÄÄÅÕïç—•Ω∏ÄÙÅ…ï≈’ïÕ–πÖ…ùÃπùï–†âÕïç—•Ω∏à∞Äàà§(ÄÄÄÄÄÄÄÅ•òÅÕïç—•Ω∏ÅΩ»Å…ï≈’ïÕ–πÖ…ùÃπùï–†âçΩµ¡Öç–à§ÄÙÙÄàƒàË(ÄÄÄÄÄÄÄÄÄÄÄÅëÖ—ÑÄÙÅ}ç…µ}¡…ï¡Ö…ïë}…ïÖë}µΩëï∞†§(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—Ã∞Å|ÄÙÅ}ç…µ}çΩπ—Öç—}Õ’µµÖ…•ïÕ}¡ÖÂ±ΩÖê†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅëÖ—Ñ∞ÅÕïç—•Ω∏∞Å¡…ï¡Ö…ïêıQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—Ã∞Åç°ÖπùïêÄÙÅ}ç…µ}çΩπ—Öç—Õ}¡ÖÂ±ΩÖê°ëÖ—Ñ§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅç°ÖπùïêË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ›•—†Å}I5}I=9%1%Q%=9}1=,Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°çΩπ—Öç—Ã§((ÄÄÄÅ›•—†Å}I5}I=9%1%Q%=9}1=,Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å}ç…µ}ç…ïÖ—ï}çΩπ—Öç—}±Ωç≠ïê†§(()ëïòÅ}ç…µ}ç…ïÖ—ï}çΩπ—Öç—}±Ωç≠ïê†§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(ÄÄÄÅπΩ‹ÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅçΩπ—Öç–ÄÙÅÏ(ÄÄÄÄÄÄÄÄâ•êàËÅÕ—»°’’•êπ’’•ê–†§§∞Äâ¡…ïπΩ¥àËÅ}ç…µ}ôΩ…µÖ—}ô•…Õ—}πÖµî°¡ÖÂ±ΩÖêπùï–†â¡…ïπΩ¥à§§∞(ÄÄÄÄÄÄÄÄâπΩ¥àËÅ}ç…µ}ôΩ…µÖ—}±ÖÕ—}πÖµî°¡ÖÂ±ΩÖêπùï–†âπΩ¥à§§∞(ÄÄÄÄÄÄÄÄâ—ï±ï¡°ΩπîàËÅÕ—»°¡ÖÂ±ΩÖêπùï–†â—ï±ï¡°Ωπîà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâµÖ•∞àËÅÕ—»°¡ÖÂ±ΩÖêπùï–†âµÖ•∞à§ÅΩ»Å¡ÖÂ±ΩÖêπùï–†âïµÖ•∞à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏àËÅÕ—»°¡ÖÂ±ΩÖêπùï–†âôΩ…µÖ—•Ω∏à∞ÄâALà§§∞Äâ±•ï‘àËÅÕ—»°¡ÖÂ±ΩÖêπùï–†â±•ï‘à§ÅΩ»ÄâAÖ…•Ãà§∞(ÄÄÄÄÄÄÄÄâÕ—Ö—’–àËÅπï·–†°Õ—Ö—’ÃÅôΩ»ÅÕ—Ö—’ÃÅ•∏Å}ç…µ}Õ—Ö—’ÕïÃ°ëÖ—Ñ§Å•òÅÕ—Ö—’ÃÅπΩ–Å•∏ÅI5}IMIY}MQQUML§∞Äâ9Ω’ŸïÖ’‡à§∞ÄâëÖ—ïÕ}ôΩ…µÖ—•Ω∏àËÄàà∞Äâç¡òàËÄàà∞ÄâçÖ…—ï}¡…ºàËÄàà∞(ÄÄÄÄÄÄÄÄâÖπ—ïçïëïπ—ÃàËÄàà∞ÄâùÖ…ëï}Ÿ’îàËÄàà∞Äâ—•—…ï}Õï©Ω’»àËÄàà∞Äâ—•—…ï}Õï©Ω’…}çπÖ¡ÃàËÄàà∞ÄâçΩµ¡—ï}çπÖ¡ÃàËÄàà∞ÄâçπÖ¡Õ}π’ààËÄàà∞ÄâçπÖ¡Õ}çÖ…ë}ŸÖ±•ë•—‰àËÅ9Ωπî∞(ÄÄÄÄÄÄÄÄâçπÖ¡Õ}’Õï…πÖµîàËÄàà∞ÄâçπÖ¡Õ}â•…—°}ÂïÖ»àËÄàà∞ÄâçπÖ¡Õ}¡ÖÕÕ›Ω…êàËÄàà∞(ÄÄÄÄÄÄÄÄâ•π—ïù…Ö—•Ωπ}ë…ÖçÖ»àËÄàà∞(ÄÄÄÄÄÄÄÄâëïÕ¡}—Â¡îàËÄàà∞Äâ•ëïπ—•—ï}ç…ïÖ—•Ω∏àËÄàà∞Äâç¡ô}µΩπ—Öπ–àËÄàà∞(ÄÄÄÄÄÄÄÄâç¡ô}¡Ö±•ï»àËÄàà∞(ÄÄÄÄÄÄÄÄâ•ëïπ—•—ï}Ω¨àËÄàà∞Äâô•πÖπçïµïπ—}ô–àËÄàà∞ÄâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–àËÄàà∞(ÄÄÄÄÄÄÄÄâµΩπ—Öπ—}ÖççΩ…ëï}ô–àËÄàà∞Äâô•πÖπçïµïπ—}¡ï…ÕΩ}¡ΩÕÕ•â±îàËÄàà∞(ÄÄÄÄÄÄÄÄâ…ïô’Õ}ô—}¡ï…ÕºàËÄàà∞Äâ…ïÕ—ï}Ö}ç°Ö…ùï}¡ï…ÕºàËÄàà∞(ÄÄÄÄÄÄÄÄâΩ…•ù•πîàËÅÕ—»°¡ÖÂ±ΩÖêπùï–†âΩ…•ù•πîà§ÅΩ»Äâ©Ω’–ÅµÖπ’ï∞à§∞ÄâçΩµµï…ç•Ö∞àËÅÕ—»°¡ÖÂ±ΩÖêπùï–†âçΩµµï…ç•Ö∞à§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÄâ•πÕç…•—}ô–àËÄàà∞ÄâçΩµµïπ—Ö•…ïÃàËÄàà∞Äâ…ï±Öπçï}ëÖ—îàËÄàà∞(ÄÄÄÄÄÄÄÄâ¡…Ωç°Ö•πï}Öç—•Ωπ}µÖπ’ï±±îàËÄàà∞Äâ…ï±ÖπçïÃàËÅmt∞ÄâÕ—Ö—’—}ÕïçΩπëÖ•…îàËÄàà∞(ÄÄÄÄÄÄÄÄâç…ïÖ—ïë}Ö–àËÅπΩ‹∞Äâ’¡ëÖ—ïë}Ö–àËÅπΩ‹∞ÄâÖç—•Ÿ•—•ïÃàËÅmt∞(ÄÄÄÅÙ(ÄÄÄÅçΩπ—Öç—lâ¡…•·}Ÿïπ—îâtÄÙÅ}ç…µ}ëïôÖ’±—}ÕÖ±ï}¡…•çî°çΩπ—Öç–§(ÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞Äâç…ïÖ—•Ω∏à∞ÄâA•Õ—îÅçÀß•îà∞Äâ©Ω’”•îÅëÖπÃÅ%π”•ù…Ö±îÅΩππïç–ÅI4à§(ÄÄÄÅçΩπ—Öç–∞Å•πâΩ’πê∞Åç…ïÖ—ïêÄÙÅô•πë}Ω…}ç…ïÖ—ï}ç…µ}çΩπ—Öç–†(ÄÄÄÄÄÄÄÅëÖ—Ñ∞Å¡ÖÂ±ΩÖê∞ÄâÕÖ•Õ•ï}µÖπ’ï±±îà∞Å¡…Ω¡ΩÕïë}çΩπ—Öç–ıçΩπ—Öç–∞(ÄÄÄÄÄÄÄÅÕï±ïç—ïë}çΩπ—Öç—}•êı¡ÖÂ±ΩÖêπùï–†âÕï±ïç—ïë}çΩπ—Öç—}•êà§∞(ÄÄÄÄÄÄÄÅôΩ…çï}ç…ïÖ—îıâΩΩ∞°¡ÖÂ±ΩÖêπùï–†âôΩ…çï}ç…ïÖ—îà§§∞(ÄÄÄÄ§(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ•òÅçΩπ—Öç–Å•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâÕ—Ö—’ÃàËÄâ¡ïπë•πù}…ïŸ•ï‹à∞Äâ…ï≈’ïÕ—}•êàËÅ•πâΩ’πëlâ•êâuÙ§∞Ä»¿»(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°}ç…µ}çΩπ—Öç—}…ïÕ¡ΩπÕî°çΩπ—Öç–∞ÅëÖ—Ñ§§∞Ä»¿ƒÅ•òÅç…ïÖ—ïêÅï±ÕîÄ»¿¿(()Ö¡¿πùï–†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃΩ’¡ëÖ—ïÃà§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çΩπ—Öç—}’¡ëÖ—ïÃ†§Ë(ÄÄÄÄààâIï—Ω’…πîÅ’π•≈’ïµïπ–Å±ïÃÅëΩπª•ïÃÅ’—•±ïÃÅÖ‘Å…Öô…áπç°•ÕÕïµïπ–ÅçΩ±±ÖâΩ…Ö—•ò∏ààà(ÄÄÄÅÕïç—•Ω∏ÄÙÅ…ï≈’ïÕ–πÖ…ùÃπùï–†âÕïç—•Ω∏à∞Äàà§(ÄÄÄÅ•òÅÕïç—•Ω∏ÄÙÙÄâëïµÖπëïÃµ…Ö¡¡ï∞àË(ÄÄÄÄÄÄÄÄåÅÖ±ïπë±‰Å¡ï’–Å…ïçïŸΩ•»Å’πîÅÀ•Õï…ŸÖ—•Ω∏ÅÖ¡À°ÃÅ∞ùÖ¡¡ï∞Åë‘ÅÕïçÀ•—Ö…•Ö–∏(ÄÄÄÄÄÄÄÄåÅK•çΩπç•±•ï»Å±ÑÅëïµÖπëîÅÖŸÖπ–Åç°Ö≈’îÅ…Öô…áπç°•ÕÕïµïπ–ÅëîÅçï——îÅ¡Öùî(ÄÄÄÄÄÄÄÄåÉ•Ÿ•—îÅëîÅçΩπÕï…Ÿï»Å’∏Å±•âï±≥§ÅΩ‘Å’πîÅëÖ—îÅëîÅ…ïπëïËµŸΩ’ÃÅΩâÕΩ≥°—î∏(ÄÄÄÄÄÄÄÅ›•—†Å}MIQI%Q}1%YIe}1=,∞Å}I5}I=9%1%Q%=9}1=,Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ω…ïë}ëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ}ç…µ}âÖç≠ô•±±}çÖ±±âÖç≠}…ï≈’ïÕ—Ã°Õ—Ω…ïë}ëÖ—Ñ§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°Õ—Ω…ïë}ëÖ—Ñ§(ÄÄÄÅëÖ—ÑÄÙÅ}ç…µ}¡…ï¡Ö…ïë}…ïÖë}µΩëï∞†§(ÄÄÄÅ›ïëΩô}ç¡ô}Õ—Ö—ïÃÄÙÅ}›ïëΩô}ç¡ô}Õ—Ö—ïÕ}âÂ}çΩπ—Öç–°ëÖ—Ñ§(ÄÄÄÅÖ¡¡Ω•π—µïπ—}çΩ’π—ÃÄÙÅ}ç…µ}Ö¡¡Ω•π—µïπ—}çΩ’π—Õ}âÂ}çΩπ—Öç–°ëÖ—Ñ§(ÄÄÄÅÖ¡¡Ω•π—µïπ—ÃÄÙÅ}ç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Õ}¡ÖÂ±ΩÖê°ëÖ—Ñ•lâÖ¡¡Ω•π—µïπ—Ãât((ÄÄÄÅÕ’µµÖ…•ïÃÄÙÅl(ÄÄÄÄÄÄÄÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ•êàËÅçΩπ—Öç–πùï–†â•êà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö—’–àËÅçΩπ—Öç–πùï–†âÕ—Ö—’–à§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö—’—}ÕïçΩπëÖ•…îàËÅçΩπ—Öç–πùï–†âÕ—Ö—’—}ÕïçΩπëÖ•…îà∞Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâç¡ô}Õ—Ö—’ÃàËÅ›ïëΩô}ç¡ô}Õ—Ö—ïÃπùï–°Õ—»°çΩπ—Öç–πùï–†â•êà§ÅΩ»Äàà§∞Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ï±Öπçï}ëÖ—îàËÅçΩπ—Öç–πùï–†â…ï±Öπçï}ëÖ—îà∞Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–àËÅçΩπ—Öç–πùï–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–à∞Äàà(ÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ’¡ëÖ—ïë}Ö–àËÅçΩπ—Öç–πùï–†â’¡ëÖ—ïë}Ö–à§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÖç—•Ÿ•—Â}çΩ’π—ÃàËÅ}ç…µ}çΩπ—Öç—}Öç—•Ÿ•—Â}çΩ’π—Ã†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—}çΩ’π—Ãπùï–°Õ—»°çΩπ—Öç–πùï–†â•êà§ÅΩ»Äàà§∞Ä¿§∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÅôΩ»ÅçΩπ—Öç–Å•∏ÅëÖ—Ñπùï–†âç…µ}çΩπ—Öç—Ãà∞Åmt§(ÄÄÄÅt(ÄÄÄÅÕï±ïç—ïêÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞Å…ï≈’ïÕ–πÖ…ùÃπùï–†âçΩπ—Öç—}•êà§§(ÄÄÄÅÕï±ïç—ïë}¡ÖÂ±ΩÖêÄÙÅ9Ωπî(ÄÄÄÅ•òÅÕï±ïç—ïêË(ÄÄÄÄÄÄÄÅÕï±ïç—ïë}¡ÖÂ±ΩÖêÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ•êàËÅÕï±ïç—ïêπùï–†â•êà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÖç—•Ÿ•—•ïÃàËÅÕï±ïç—ïêπùï–†âÖç—•Ÿ•—•ïÃà∞Åmt§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡’â±•çÖ—•ΩπÃàËÅÕï±ïç—ïêπùï–†â¡’â±•çÖ—•ΩπÃà∞Åmt§∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅÏ(ÄÄÄÄÄÄÄÄâçΩπ—Öç—ÃàËÅÕ’µµÖ…•ïÃ∞(ÄÄÄÄÄÄÄÄâÕï±ïç—ïêàËÅÕï±ïç—ïë}¡ÖÂ±ΩÖê∞(ÄÄÄÄÄÄÄÄâÖ¡¡Ω•π—µïπ—ÃàËÅÖ¡¡Ω•π—µïπ—Ã∞(ÄÄÄÄÄÄÄÄâçÖ±±âÖç≠}¡ïπë•πù}çΩ’π–àËÅ}ç…µ}çÖ±±âÖç≠}¡ïπë•πù}çΩ’π–°ëÖ—Ñ§∞(ÄÄÄÅÙ(ÄÄÄÅ•òÅÕïç—•Ω∏ÄÙÙÄâëïµÖπëïÃµ…Ö¡¡ï∞àË(ÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëlâçÖ±±âÖç≠}…ï≈’ïÕ—ÃâtÄÙÅ}ç…µ}çÖ±±âÖç≠}…ï≈’ïÕ—Õ}¡ÖÂ±ΩÖê°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°¡ÖÂ±ΩÖê§(()Ö¡¿πëï±ï—î†àΩÖ¡§Ωç…¥ΩëÖ—ÖâÖÕîà§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}ëï±ï—ï}ëÖ—ÖâÖÕî†§Ë(ÄÄÄÄààâôôÖçîÅ±ïÃÅëΩπª•ïÃÅëïÃÅ¡…ΩÕ¡ïç—ÃÅÕÖπÃÅ—Ω’ç°ï»ÅÖ’‡ÅÖ’—…ïÃÅΩ’—•±ÃÅë‘ÅÕ•—î∏ààà(ÄÄÄÅ•òÄ°ç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ§πùï–†â…Ω±îà§ÄÑÙÄâÖëµ•∏àË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâï——îÅÖç—•Ω∏ÅïÕ–ÅÀ•Õï…€•îÉÄÅ≥äeÖëµ•π•Õ—…Ö—ï’»âÙ§∞Ä–¿Ã((ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅëï±ï—ïë}çΩ’π–ÄÙÅ±ï∏°ëÖ—Ñπùï–†âç…µ}çΩπ—Öç—Ãà∞Åmt§§(ÄÄÄÄåÅ1ïÃÅÀ•ù±ÖùïÃÅI4Ä£•—Ö¡ïÃ∞ÅµΩì°±ïÃÅï–ÅçΩππï·•Ω∏ÅÖ±ïπë±‰§ÅÕΩπ–ÅçΩπÕï…€•Ã∞(ÄÄÄÄåÅµÖ•ÃÅ—Ω’—ïÃÅ±ïÃÅëΩπª•ïÃÅ…Ö——Öç£•ïÃÅÖ’‡Å¡…ΩÕ¡ïç—ÃÅëΩ•Ÿïπ–Åë•Õ¡Ö…áπ—…î∏(ÄÄÄÅëÖ—Ölâç…µ}çΩπ—Öç—ÃâtÄÙÅmt(ÄÄÄÅëÖ—Ölâç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—ÃâtÄÙÅmt(ÄÄÄÅëÖ—Ölâç…µ}πΩ—•ô•çÖ—•ΩπÃâtÄÙÅmt(ÄÄÄÅëÖ—Ölâç…µ}Ö•}çÖπë•ëÖ—ï}ÖπÖ±ÂÕïÃâtÄÙÅÌÙ(ÄÄÄÅëÖ—Ölâç…µ}çπÖ¡Õ}ÕçΩ…•πù}ÕπÖ¡Õ°Ω—ÃâtÄÙÅÌÙ(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâΩ¨àËÅQ…’î∞Äâëï±ï—ïë}çΩ’π–àËÅëï±ï—ïë}çΩ’π—Ù§(()Ö¡¿πùï–†àΩÖ¡§Ωç…¥Ω•πâΩ’πêµ…ï≈’ïÕ—ÃΩ¡ïπë•πúà§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}¡ïπë•πù}•πâΩ’πë}…ï≈’ïÕ—Ã†§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°m…Ω‹ÅôΩ»Å…Ω‹Å•∏ÅëÖ—Ñπùï–†âç…µ}•πâΩ’πë}…ï≈’ïÕ—Ãà∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ…Ω‹πùï–†âÕ—Ö—’Ãà§ÄÙÙÄâ¡ïπë•πù}…ïŸ•ï‹ât§(()Ö¡¿π¡ΩÕ–†àΩÖ¡§Ωç…¥Ω•πâΩ’πêµ…ï≈’ïÕ—ÃºÒ…ï≈’ïÕ—}•ê¯Ω…ïÕΩ±Ÿîà§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}…ïÕΩ±Ÿï}•πâΩ’πë}…ï≈’ïÕ–°…ï≈’ïÕ—}•ê§Ë(ÄÄÄÄààâK•ÕΩ’–Åï·¡±•ç•—ïµïπ–Å’πîÅçΩ……ïÕ¡ΩπëÖπçîÅÕÖπÃÅ©ÖµÖ•ÃÅµΩë•ô•ï»Å±ÑÅô•ç°îÅç•â±î∏ààà(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§ÏÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(ÄÄÄÅ•πâΩ’πêÄÙÅπï·–†°…Ω‹ÅôΩ»Å…Ω‹Å•∏ÅëÖ—Ñπùï–†âç…µ}•πâΩ’πë}…ï≈’ïÕ—Ãà∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ…Ω‹πùï–†â•êà§ÄÙÙÅ…ï≈’ïÕ—}•ê§∞Å9Ωπî§(ÄÄÄÅ•òÅπΩ–Å•πâΩ’πêÅΩ»Å•πâΩ’πêπùï–†âÕ—Ö—’Ãà§ÄÑÙÄâ¡ïπë•πù}…ïŸ•ï‹àË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâïµÖπëîÉÄÅ€•…•ô•ï»Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅÖç—•Ω∏ÄÙÅ¡ÖÂ±ΩÖêπùï–†âÖç—•Ω∏à§(ÄÄÄÅ•òÅÖç—•Ω∏ÄÙÙÄâçÖπçï∞àË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°•πâΩ’πê§(ÄÄÄÅ•òÅÖç—•Ω∏ÄÙÙÄâÖ——Öç†àË(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞Å¡ÖÂ±ΩÖêπùï–†âçΩπ—Öç—}•êà§§(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–ËÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ•ç°îÅ•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅï±•òÅÖç—•Ω∏ÄÙÙÄâç…ïÖ—îàË(ÄÄÄÄÄÄÄÅ…Ö‹ÄÙÅ•πâΩ’πêπùï–†â…Ö›}¡ÖÂ±ΩÖêà§ÅΩ»ÅÌÙ(ÄÄÄÄÄÄÄÅçΩπ—Öç–∞Å|∞Å|ÄÙÅô•πë}Ω…}ç…ïÖ—ï}ç…µ}çΩπ—Öç–†(ÄÄÄÄÄÄÄÄÄÄÄÅëÖ—Ñ∞Å…Ö‹∞Å•πâΩ’πêπùï–†âÕΩ’…çîà§ÅΩ»ÄâÀ•ÕΩ±’—•Ωπ}µÖπ’ï±±îà∞ÅôΩ…çï}ç…ïÖ—îıQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÅï·—ï…πÖ±}•êıòâ…ïÕΩ±’—•Ω∏ÈÌ…ï≈’ïÕ—}•ëÙà§(ÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâç—•Ω∏Å•πŸÖ±•ëîâÙ§∞Ä–¿¿(ÄÄÄÅ•πâΩ’πëlâçΩπ—Öç—}•êâtÄÙÅçΩπ—Öç–πùï–†â•êà§ÏÅ•πâΩ’πëlâÕ—Ö—’ÃâtÄÙÄâ…ïÕΩ±Ÿïêà(ÄÄÄÅ•πâΩ’πëlâ…ïÕΩ±Ÿïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅ•πâΩ’πëlâ…ïÕΩ±Ÿïë}â‰âtÄÙÄ°ç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ§πùï–†âïµÖ•∞à§ÅΩ»Ä°ç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ§πùï–†âπÖµîà§(ÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞Äâ•πâΩ’πë}…ï≈’ïÕ–à∞ÄâΩ……ïÕ¡ΩπëÖπçîÅÀ•ÕΩ±’îÅµÖπ’ï±±ïµïπ–à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâMΩ’…çîÄËÅÌ•πâΩ’πêπùï–†ùÕΩ’…çîú•Ù∏Åç—•Ω∏ÄËÅÌÖç—•ΩπÙ∏à§(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°•πâΩ’πê§(()Ö¡¿πùï–†àΩÖ¡§Ωç…¥Ωâ…ïŸºΩÕµÃµç…ïë•—Ãà§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}â…ïŸΩ}ÕµÕ}ç…ïë•—Ã†§Ë(ÄÄÄÄààâ·¡ΩÕîÅ—°îÅ±•ŸîÅ	…ïŸºÅM5LÅâÖ±ÖπçîÅ—ºÅÖ’—°ïπ—•çÖ—ïêÅI4Å’Õï…Ã∏ààà(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅç…ïë•—ÃÄÙÅ}â…ïŸΩ}ÕµÕ}ç…ïë•—Ã†§(ÄÄÄÅï·çï¡–Ä°…ï≈’ïÕ—ÃπIï≈’ïÕ—·çï¡—•Ω∏∞ÅI’π—•µï……Ω»∞ÅYÖ±’ï……Ω»§ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÅÕ—»°ï·å§ÅΩ»Äâ1îÅÕΩ±ëîÅM5LÅ	…ïŸºÅïÕ–Å•πë•Õ¡Ωπ•â±î∏âÙ§∞Ä‘¿Ã(ÄÄÄÅ}πΩ—•ôÂ}â…ïŸΩ}ÕµÕ}±Ω›}âÖ±Öπçî°ç…ïë•—Ã§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâç…ïë•—ÃàËÅç…ïë•—ÕÙ§(()I5}Y1=A59Q}MUAA=IQ}A1Q=I5LÄÙÅÏ(ÄÄÄÄâI4àËÄâI4à∞(ÄÄÄÄâïÕ—•Ω∏ÅÕ—Öù•Ö•…ïÃàËÄâïÕ—•Ω∏ÅÕ—Öù•Ö•…ïÃà∞(ÄÄÄÄâM•—îÅ•π—ï…πï–ÅΩôô•ç•ï∞àËÄâM•—îÅ•π—ï…πï–ÅΩôô•ç•ï∞à∞)Ù)I5}9=Q%=9}Q}M=UI}%ÄÙÄà›òƒ…ôî‰»µëâå–¥–¡å‡µÖò—î¥‹‹‘‹·à’ëâôå¿à)I5}Y1=A59Q}MUAA=IQ}9=Q%=9}MQQULÄÙÄã Å91eMHà)I5}9=Q%=9}QQ!59Q}AI=AIQdÄÙÄâô•ç°•ï»à)I5}Y1=A59Q}MUAA=IQ}5a}QQ!59Q}	eQLÄÙÄ»¿Ä®Äƒ¿»–Ä®Äƒ¿»–)I5}Y1=A59Q}MUAA=IQ}QQ!59Q}aQ9M%=9LÄÙÅô…ΩÈïπÕï–°Ï(ÄÄÄÄàπçÕÿà∞ÄàπëΩåà∞ÄàπëΩç‡à∞Äàπù•òà∞Äàπ°ï•åà∞Äàπ©¡ïúà∞Äàπ©¡úà∞Äàπ¡ëòà∞(ÄÄÄÄàπ¡πúà∞Äàπ—·–à∞Äàπ›ïâ¿à∞Äàπ·±Ãà∞Äàπ·±Õ‡à∞)Ù§(()ëïòÅ}ç…µ}πΩ—•Ωπ}…•ç°}—ï·–°ŸÖ±’î§Ë(ÄÄÄÅ—ï·–ÄÙÅÕ—»°ŸÖ±’îÅΩ»Äàà§(ÄÄÄÅ…ï—’…∏Ål(ÄÄÄÄÄÄÄÅÏâ—Â¡îàËÄâ—ï·–à∞Äâ—ï·–àËÅÏâçΩπ—ïπ–àËÅ—ï·—m•πëï‡È•πëï‡Ä¨Ä…|¿¿¡uıÙ(ÄÄÄÄÄÄÄÅôΩ»Å•πëï‡Å•∏Å…Öπùî†¿∞Å±ï∏°—ï·–§∞Ä…|¿¿¿§(ÄÄÄÅtÅΩ»ÅmÏâ—Â¡îàËÄâ—ï·–à∞Äâ—ï·–àËÅÏâçΩπ—ïπ–àËÄàâııt(()ëïòÅ}ç…µ}ëïŸï±Ω¡µïπ—}Õ’¡¡Ω…—}Ö——Öç°µïπ–†§Ë(ÄÄÄÅ’¡±ΩÖëïêÄÙÅ…ï≈’ïÕ–πô•±ïÃπùï–†âÖ——Öç°µïπ–à§(ÄÄÄÅ•òÅπΩ–Å’¡±ΩÖëïêÅΩ»ÅπΩ–Å’¡±ΩÖëïêπô•±ïπÖµîË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(ÄÄÄÅô•±ïπÖµîÄÙÅÕïç’…ï}ô•±ïπÖµî°’¡±ΩÖëïêπô•±ïπÖµî§(ÄÄÄÅï·—ïπÕ•Ω∏ÄÙÅΩÃπ¡Ö—†πÕ¡±•—ï·–°ô•±ïπÖµî•l≈tπ±Ω›ï»†§(ÄÄÄÅ•òÅπΩ–Åô•±ïπÖµîÅΩ»Åï·—ïπÕ•Ω∏ÅπΩ–Å•∏ÅI5}Y1=A59Q}MUAA=IQ}QQ!59Q}aQ9M%=9LË(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†(ÄÄÄÄÄÄÄÄÄÄÄÄâΩ…µÖ–ÅëîÅ¡ß°çîÅ©Ω•π—îÅπΩ∏ÅÖççï¡”§∏ÅU—•±•ÕïËÅ’πîÅ•µÖùî∞Å’∏ÅA∞Äà(ÄÄÄÄÄÄÄÄÄÄÄÄâ’∏ÅëΩç’µïπ–Å]Ω…êΩ·çï∞∞Å’∏ÅMXÅΩ‘Å’∏Åô•ç°•ï»Å—ï·—î∏à(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅ•òÅ±ï∏°ô•±ïπÖµîπïπçΩëî†â’—ò¥‡à§§Ä¯Ä»–¿Ë(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†â1îÅπΩ¥ÅëîÅ±ÑÅ¡ß°çîÅ©Ω•π—îÅïÕ–Å—…Ω¿Å±Ωπú∏à§(ÄÄÄÅçΩπ—ïπ–ÄÙÅ’¡±ΩÖëïêπÕ—…ïÖ¥π…ïÖê°I5}Y1=A59Q}MUAA=IQ}5a}QQ!59Q}	eQLÄ¨Äƒ§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—ïπ–Ë(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†â1ÑÅ¡ß°çîÅ©Ω•π—îÅïÕ–ÅŸ•ëî∏à§(ÄÄÄÅ•òÅ±ï∏°çΩπ—ïπ–§Ä¯ÅI5}Y1=A59Q}MUAA=IQ}5a}QQ!59Q}	eQLË(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅ=Ÿï…ô±Ω›……Ω»†â1ÑÅ¡ß°çîÅ©Ω•π—îÅπîÅëΩ•–Å¡ÖÃÅì•¡ÖÕÕï»Ä»¿Å5º∏à§(ÄÄÄÄåÅQ°îÅâ…Ω›Õï»µ¡…ΩŸ•ëïêÅ5%5Å—Â¡îÅ•ÃÅ’Õï»µçΩπ—…Ω±±ïêÏÅëï…•ŸîÅ•–Åô…Ω¥Å—°î(ÄÄÄÄåÅŸÖ±•ëÖ—ïêÅï·—ïπÕ•Ω∏ÅâïôΩ…îÅôΩ…›Ö…ë•πúÅ—°îÅô•±îÅ—ºÅ9Ω—•Ω∏∏(ÄÄÄÅçΩπ—ïπ—}—Â¡îÄÙÅµ•µï—Â¡ïÃπù’ïÕÕ}—Â¡î°ô•±ïπÖµî•l¡tÅΩ»ÄâÖ¡¡±•çÖ—•Ω∏ΩΩç—ï–µÕ—…ïÖ¥à(ÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄâô•±ïπÖµîàËÅô•±ïπÖµî∞(ÄÄÄÄÄÄÄÄâçΩπ—ïπ–àËÅçΩπ—ïπ–∞(ÄÄÄÄÄÄÄÄâçΩπ—ïπ—}—Â¡îàËÅçΩπ—ïπ—}—Â¡î∞(ÄÄÄÅÙ(()ëïòÅ}ç…µ}’¡±ΩÖë}πΩ—•Ωπ}Ö——Öç°µïπ–°—Ω≠ï∏∞ÅπΩ—•Ωπ}Ÿï…Õ•Ω∏∞ÅÖ——Öç°µïπ–§Ë(ÄÄÄÅ°ïÖëï…ÃÄÙÅÏ(ÄÄÄÄÄÄÄÄâ’—°Ω…•ÈÖ—•Ω∏àËÅòâ	ïÖ…ï»ÅÌ—Ω≠ïπÙà∞(ÄÄÄÄÄÄÄÄâ9Ω—•Ω∏µYï…Õ•Ω∏àËÅπΩ—•Ωπ}Ÿï…Õ•Ω∏∞(ÄÄÄÅÙ(ÄÄÄÅç…ïÖ—ïë}…ïÕ¡ΩπÕîÄÙÅ…ï≈’ïÕ—Ãπ¡ΩÕ–†(ÄÄÄÄÄÄÄÄâ°——¡ÃËºΩÖ¡§ππΩ—•Ω∏πçΩ¥ΩÿƒΩô•±ï}’¡±ΩÖëÃà∞(ÄÄÄÄÄÄÄÅ°ïÖëï…ÃıÏ®©°ïÖëï…Ã∞ÄâΩπ—ïπ–µQÂ¡îàËÄâÖ¡¡±•çÖ—•Ω∏Ω©ÕΩ∏âÙ∞(ÄÄÄÄÄÄÄÅ©ÕΩ∏ıÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâµΩëîàËÄâÕ•πù±ï}¡Ö…–à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâô•±ïπÖµîàËÅÖ——Öç°µïπ—lâô•±ïπÖµîât∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—ïπ—}—Â¡îàËÅÖ——Öç°µïπ—lâçΩπ—ïπ—}—Â¡îât∞(ÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÄÄÄÄÅ—•µïΩ’–Ù†ƒ¿∞ÄÃ¿§∞(ÄÄÄÄ§(ÄÄÄÅ•òÅç…ïÖ—ïë}…ïÕ¡ΩπÕîπÕ—Ö—’Õ}çΩëîÅπΩ–Å•∏ÅÏ»¿¿∞Ä»¿≈ÙË(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅI’π—•µï……Ω»°òâ9Ω—•Ω∏Å•±îÅU¡±ΩÖêÅ!QQ@ÅÌç…ïÖ—ïë}…ïÕ¡ΩπÕîπÕ—Ö—’Õ}çΩëïÙà§(ÄÄÄÅ’¡±ΩÖë}•êÄÙÅÕ—»†°ç…ïÖ—ïë}…ïÕ¡ΩπÕîπ©ÕΩ∏†§ÅΩ»ÅÌÙ§πùï–†â•êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–Å’¡±ΩÖë}•êË(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†âK•¡ΩπÕîÅ9Ω—•Ω∏ÅÕÖπÃÅ•ëïπ—•ô•Öπ–ÅëîÅô•ç°•ï»à§(ÄÄÄÅÕïπ—}…ïÕ¡ΩπÕîÄÙÅ…ï≈’ïÕ—Ãπ¡ΩÕ–†(ÄÄÄÄÄÄÄÅòâ°——¡ÃËºΩÖ¡§ππΩ—•Ω∏πçΩ¥ΩÿƒΩô•±ï}’¡±ΩÖëÃΩÌ≈’Ω—î°’¡±ΩÖë}•ê∞ÅÕÖôîÙúú•ÙΩÕïπêà∞(ÄÄÄÄÄÄÄÅ°ïÖëï…Ãı°ïÖëï…Ã∞(ÄÄÄÄÄÄÄÅô•±ïÃıÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâô•±îàËÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖ——Öç°µïπ—lâô•±ïπÖµîât∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖ——Öç°µïπ—lâçΩπ—ïπ–ât∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖ——Öç°µïπ—lâçΩπ—ïπ—}—Â¡îât∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÄÄÄÄÅ—•µïΩ’–Ù†ƒ¿∞Äÿ¿§∞(ÄÄÄÄ§(ÄÄÄÅ•òÅÕïπ—}…ïÕ¡ΩπÕîπÕ—Ö—’Õ}çΩëîÅπΩ–Å•∏ÅÏ»¿¿∞Ä»¿≈ÙË(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅI’π—•µï……Ω»°òâ9Ω—•Ω∏Å•±îÅMïπêÅ!QQ@ÅÌÕïπ—}…ïÕ¡ΩπÕîπÕ—Ö—’Õ}çΩëïÙà§(ÄÄÄÅÕïπ—}¡ÖÂ±ΩÖêÄÙÅÕïπ—}…ïÕ¡ΩπÕîπ©ÕΩ∏†§ÅΩ»ÅÌÙ(ÄÄÄÅ•òÅÕïπ—}¡ÖÂ±ΩÖêπùï–†âÕ—Ö—’Ãà§ÄÑÙÄâ’¡±ΩÖëïêàË(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†â9Ω—•Ω∏ÅªäeÑÅ¡ÖÃÅçΩπô•…∑§Å±îÅ”•≥•Ÿï…Õïµïπ–Åë‘Åô•ç°•ï»à§(ÄÄÄÅ…ï—’…∏Å’¡±ΩÖë}•ê(()ëïòÅ}ç…µ}ëïŸï±Ω¡µïπ—}Õ’¡¡Ω…—}Õ’â©ïç–°…ï›…•——ïπ}Öç—•ΩπÃ∞ÅΩ…•ù•πÖ±}Öç—•ΩπÃ§Ë(ÄÄÄÄààâ·—…Öç–ÅÑÅ’Õïô’∞Å9Ω—•Ω∏Å—•—±îÅô…Ω¥Å—°îÅ$ùÃÅôΩ…µÖ——ïêÅ…ïÕ¡ΩπÕî∏ààà(ÄÄÄÅ…ï›…•——ï∏ÄÙÅ…îπÕ’à†(ÄÄÄÄÄÄÄÅ»à†˝§§Òâ…qÃ®º¸¯à∞Äâq∏à∞ÅÕ—»°…ï›…•——ïπ}Öç—•ΩπÃÅΩ»Äàà§(ÄÄÄÄ§π…ï¡±Öçî†âq»à∞Äâq∏à§(ÄÄÄÅ±•πïÃÄÙÅmt(ÄÄÄÅôΩ»Å…Ö›}±•πîÅ•∏Å…ï›…•——ï∏πÕ¡±•—±•πïÃ†§Ë(ÄÄÄÄÄÄÄÅ±•πîÄÙÅ…îπÕ’à°»âl©}Åtà∞Äàà∞Å…Ö›}±•πî§(ÄÄÄÄÄÄÄÅ±•πîÄÙÅ…îπÕ’à°»âyqÃ®†¸ËçÏƒ∞ŸıqÃ©Òl∑äOäP˘uqÃ®§à∞Äàà∞Å±•πî§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅ±•πîË(ÄÄÄÄÄÄÄÄÄÄÄÅ±•πïÃπÖ¡¡ïπê°±•πî§((ÄÄÄÅôΩ»Å•πëï‡∞Å±•πîÅ•∏Åïπ’µï…Ö—î°±•πïÃ§Ë(ÄÄÄÄÄÄÄÅΩâ©ïç—•ŸîÄÙÅ…îπµÖ—ç†°»à†˝§•yΩâ©ïç—•ôqÃ®Ë˝qÃ®†∏®§êà∞Å±•πî§(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅΩâ©ïç—•ŸîË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅÕ’â©ïç–ÄÙÅΩâ©ïç—•Ÿîπù…Ω’¿†ƒ§πÕ—…•¿†àÅq–ÎäOäP¥à§(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅÕ’â©ïç–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»ÅôΩ±±Ω›•πù}±•πîÅ•∏Å±•πïÕm•πëï‡Ä¨ÄƒÈtË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ…îπµÖ—ç††(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ»à†˝§•x†¸ÈµΩë•ô•çÖ—•ΩπÃÅëïµÖπì•ïÕÒç…•”°…ïÃÅΩâÕï…ŸÖâ±ïÃ•qÃ®Ë¸à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅôΩ±±Ω›•πù}±•πî§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅâ…ïÖ¨(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ’â©ïç–ÄÙÅôΩ±±Ω›•πù}±•πî(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅâ…ïÖ¨(ÄÄÄÄÄÄÄÅ•òÅÕ’â©ïç–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å…îπÕ’à°»âqÃ¨à∞ÄàÄà∞ÅÕ’â©ïç–§πÕ—…•¿†§π…Õ—…•¿†à∏Ïà§((ÄÄÄÅôÖ±±âÖç¨ÄÙÅÕ—»°Ω…•ù•πÖ±}Öç—•ΩπÃÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅçÖπë•ëÖ—îÄÙÅ±•πïÕl¡tÅ•òÅ±•πïÃÅï±ÕîÅôÖ±±âÖç¨(ÄÄÄÅ…ï—’…∏Å…îπÕ’à°»âqÃ¨à∞ÄàÄà∞ÅçÖπë•ëÖ—î§πÕ—…•¿†§π…Õ—…•¿†à∏Ïà§(()ëïòÅ}ç…µ}ëïŸï±Ω¡µïπ—}Õ’¡¡Ω…—}¡Öùî†(ÄÄÄÄÄÄÄÅ¡±Ö—ôΩ…¥∞Å¡Öùï}’…∞∞ÅΩ…•ù•πÖ±}Öç—•ΩπÃ∞Å…ï›…•——ïπ}Öç—•ΩπÃ∞Ä®∞ÅÖ•}…ï›…•——ï∏ıQ…’î∞(ÄÄÄÄÄÄÄÅÖ——Öç°µïπ—}’¡±ΩÖë}•êÙàà∞ÅÖ——Öç°µïπ—}ô•±ïπÖµîÙàà∞ÅÖ——Öç°µïπ—}çΩπ—ïπ—}—Â¡îÙàà§Ë(ÄÄÄÅ’Õï»ÄÙÅç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ(ÄÄÄÅÕ’â©ïç–ÄÙÅ}ç…µ}ëïŸï±Ω¡µïπ—}Õ’¡¡Ω…—}Õ’â©ïç–°…ï›…•——ïπ}Öç—•ΩπÃ∞ÅΩ…•ù•πÖ±}Öç—•ΩπÃ§(ÄÄÄÅ—•—±îÄÙÅòâÌ¡±Ö—ôΩ…µÙÉäPÅÌÕ’â©ïç–ÅΩ»ÅΩ…•ù•πÖ±}Öç—•ΩπÕÙâlËƒ¿¡t(ÄÄÄÅ¡Ö…Öù…Ö¡†ÄÙÅ±ÖµâëÑÅŸÖ±’îËÅÏâΩâ©ïç–àËÄââ±Ωç¨à∞Äâ—Â¡îàËÄâ¡Ö…Öù…Ö¡†à∞(ÄÄÄÄÄÄÄÄâ¡Ö…Öù…Ö¡†àËÅÏâ…•ç°}—ï·–àËÅ}ç…µ}πΩ—•Ωπ}…•ç°}—ï·–°ŸÖ±’î•ıÙ(ÄÄÄÅ°ïÖë•πúÄÙÅ±ÖµâëÑÅŸÖ±’îËÅÏâΩâ©ïç–àËÄââ±Ωç¨à∞Äâ—Â¡îàËÄâ°ïÖë•πù|»à∞(ÄÄÄÄÄÄÄÄâ°ïÖë•πù|»àËÅÏâ…•ç°}—ï·–àËÅ}ç…µ}πΩ—•Ωπ}…•ç°}—ï·–°ŸÖ±’î•ıÙ(ÄÄÄÅ¡ÖùîÄÙÅÏ(ÄÄÄÄÄÄÄÄâ¡Ö…ïπ–àËÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ—Â¡îàËÄâëÖ—Ö}ÕΩ’…çï}•êà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâëÖ—Ö}ÕΩ’…çï}•êàËÅΩÃπùï—ïπÿ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ9=Q%=9}I5}Q}M=UI}%à∞ÅI5}9=Q%=9}Q}M=UI}%§∞(ÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÄÄÄÄÄâ¡…Ω¡ï…—•ïÃàËÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâAïπœ•îàËÅÏâ—Â¡îàËÄâ—•—±îà∞Äâ—•—±îàËÅ}ç…µ}πΩ—•Ωπ}…•ç°}—ï·–°—•—±î•Ù∞(ÄÄÄÄÄÄÄÄÄÄÄÄâΩµÖ•πîàËÅÏâ—Â¡îàËÄâÕï±ïç–à∞ÄâÕï±ïç–àËÅÏâπÖµîàËÄâ•Ÿï±Ω¡¡ïµïπ–Å›ïàâıÙ∞(ÄÄÄÄÄÄÄÄÄÄÄÄâA±Ö—ïôΩ…µîàËÅÏâ—Â¡îàËÄâÕï±ïç–à∞ÄâÕï±ïç–àËÅÏâπÖµîàËÅ¡±Ö—ôΩ…µıÙ∞(ÄÄÄÄÄÄÄÄÄÄÄÄâM—Ö—’–àËÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ—Â¡îàËÄâÕï±ïç–à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÕï±ïç–àËÅÏâπÖµîàËÅI5}Y1=A59Q}MUAA=IQ}9=Q%=9}MQQUMÙ∞(ÄÄÄÄÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÄÄÄÄÄÄÄÄÄâQÂ¡îàËÅÏâ—Â¡îàËÄâÕï±ïç–à∞ÄâÕï±ïç–àËÅÏâπÖµîàËÄã ÅôÖ•…îâıÙ∞(ÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÄÄÄÄÄâç°•±ë…ï∏àËÅl(ÄÄÄÄÄÄÄÄÄÄÄÅ°ïÖë•πú†âïµÖπëîÅ…ïôΩ…µ’≥•îÅ¡Ö»Å≥äe%àÅ•òÅÖ•}…ï›…•——ï∏Åï±ÕîÄâïµÖπëîÉÄÅ…ïôΩ…µ’±ï»à§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö…Öù…Ö¡†°…ï›…•——ïπ}Öç—•ΩπÃ§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ°ïÖë•πú†âAÖùîÅçΩπçï…ª•îà§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö…Öù…Ö¡†°¡Öùï}’…∞§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ°ïÖë•πú†âïµÖπëîÅΩ…•ù•πÖ±îà§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö…Öù…Ö¡†°Ω…•ù•πÖ±}Öç—•ΩπÃ§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ°ïÖë•πú†âïµÖπëï’»à§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö…Öù…Ö¡†°’Õï»πùï–†âπÖµîà§ÅΩ»Å’Õï»πùï–†âïµÖ•∞à§ÅΩ»Äâëµ•π•Õ—…Ö—ï’»ÅI4à§∞(ÄÄÄÄÄÄÄÅt∞(ÄÄÄÅÙ(ÄÄÄÅ•òÅÖ——Öç°µïπ—}’¡±ΩÖë}•êË(ÄÄÄÄÄÄÄÅô•±ï}’¡±ΩÖêÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ—Â¡îàËÄâô•±ï}’¡±ΩÖêà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâô•±ï}’¡±ΩÖêàËÅÏâ•êàËÅÖ——Öç°µïπ—}’¡±ΩÖë}•ëÙ∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÅ•òÅÕ—»°Ö——Öç°µïπ—}çΩπ—ïπ—}—Â¡î§πÕ—Ö…—Õ›•—††â•µÖùîºà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ¡Öùïlâç°•±ë…ï∏âtπï·—ïπê°l(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ°ïÖë•πú†â%µÖùîÅ©Ω•π—îà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâΩâ©ïç–àËÄââ±Ωç¨à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ—Â¡îàËÄâ•µÖùîà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ•µÖùîàËÅÏ®©ô•±ï}’¡±ΩÖê∞ÄâçÖ¡—•Ω∏àËÅmuÙ∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÄÄÄÄÄÄÄÄÅt§(ÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÅ¡Öùïlâ¡…Ω¡ï…—•ïÃâumI5}9=Q%=9}QQ!59Q}AI=AIQetÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ—Â¡îàËÄâô•±ïÃà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâô•±ïÃàËÅmÏ®©ô•±ï}’¡±ΩÖê∞ÄâπÖµîàËÅÖ——Öç°µïπ—}ô•±ïπÖµïıt∞(ÄÄÄÄÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÄÄÄÄÅ¡Öùïlâç°•±ë…ï∏âtπï·—ïπê°l(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ°ïÖë•πú†âAß°çîÅ©Ω•π—îà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâΩâ©ïç–àËÄââ±Ωç¨à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ—Â¡îàËÄâô•±îà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâô•±îàËÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ®©ô•±ï}’¡±ΩÖê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâπÖµîàËÅÖ——Öç°µïπ—}ô•±ïπÖµî∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâçÖ¡—•Ω∏àËÅmt∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÄÄÄÄÄÄÄÄÅt§(ÄÄÄÅ…ï—’…∏Å¡Öùî(()Ö¡¿π¡ΩÕ–†àΩÖ¡§Ωç…¥ΩëïŸï±Ω¡µïπ–µÕ’¡¡Ω…–à§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}ëïŸï±Ω¡µïπ—}Õ’¡¡Ω…–†§Ë(ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÄ°…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ§Å•òÅ…ï≈’ïÕ–π•Õ}©ÕΩ∏Åï±ÕîÅ…ï≈’ïÕ–πôΩ…¥(ÄÄÄÅ¡±Ö—ôΩ…¥ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†â¡±Ö—ôΩ…¥à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ¡Öùï}’…∞ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†â¡Öùï}’…∞à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅÖç—•ΩπÃÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âÖç—•ΩπÃà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅ¡±Ö—ôΩ…¥ÅπΩ–Å•∏ÅI5}Y1=A59Q}MUAA=IQ}A1Q=I5LË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ°Ω•Õ•ÕÕïËÅ’πîÅ¡±Ö—ïôΩ…µîÅŸÖ±•ëî∏âÙ§∞Ä–¿¿(ÄÄÄÅ¡Ö…Õïë}’…∞ÄÙÅ’…±¡Ö…Õî°¡Öùï}’…∞§(ÄÄÄÅ•òÅ¡Ö…Õïë}’…∞πÕç°ïµîÅπΩ–Å•∏ÅÏâ°——¿à∞Äâ°——¡ÃâÙÅΩ»ÅπΩ–Å¡Ö…Õïë}’…∞ππï—±ΩåÅΩ»Å±ï∏°¡Öùï}’…∞§Ä¯Ä…|¿¿¿Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâIïπÕï•ùπïËÅ’πîÅUI0Å°——¿°Ã§ÅŸÖ±•ëî∏âÙ§∞Ä–¿¿(ÄÄÄÅ•òÅ±ï∏°Öç—•ΩπÃ§ÄÄ»¿ÅΩ»Å±ï∏°Öç—•ΩπÃ§Ä¯ÄŸ|¿¿¿Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ•—Ö•±±ïËÅ±ïÃÅÖç—•ΩπÃÉÄÅµïπï»Åïπ—…îÄ»¿Åï–ÄÿÄ¿¿¿ÅçÖ…Öç”°…ïÃ∏âÙ§∞Ä–¿¿(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅÖ——Öç°µïπ–ÄÙÅ}ç…µ}ëïŸï±Ω¡µïπ—}Õ’¡¡Ω…—}Ö——Öç°µïπ–†§(ÄÄÄÅï·çï¡–Å=Ÿï…ô±Ω›……Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÅÕ—»°ï·å•Ù§∞Ä–ƒÃ(ÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÅÕ—»°ï·å•Ù§∞Ä–¿¿((ÄÄÄÅÖ•}…ï›…•——ï∏ÄÙÅQ…’î(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ…ï›…•——ï∏ÄÙÅ}ç…µ}Ö§†(ÄÄÄÄÄÄÄÄÄÄÄÄâQ‘Å…ïôΩ…µ’±ïÃÅ’πîÅëïµÖπëîÅ•π—ï…πîÅëîÅì•Ÿï±Ω¡¡ïµïπ–ÅÕÖπÃÅ•πŸïπ—ï»∞Äà(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ’¡¡…•µï»Åπ§Åï„•ç’—ï»ÅÖ’ç’πîÅ•πÕ—…’ç—•Ω∏∏ÅK•¡ΩπëÃÅ’π•≈’ïµïπ–Åï∏Åô…ÖªùÖ•ÃÄà(ÄÄÄÄÄÄÄÄÄÄÄÄâÖŸïåÅ—…Ω•ÃÅÕïç—•ΩπÃÅçΩ’…—ïÃÄËÅ=â©ïç—•ò∞Å5Ωë•ô•çÖ—•ΩπÃÅëïµÖπì•ïÃ∞Äà(ÄÄÄÄÄÄÄÄÄÄÄÄâ…•”°…ïÃÅΩâÕï…ŸÖâ±ïÃ∏ÅΩπÕï…ŸîÅ—Ω’ÃÅ±ïÃÅì•—Ö•±ÃÅôΩπç—•Ωππï±ÃÅ’—•±ïÃ∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÅòâA±Ö—ïôΩ…µîÄËÅÌ¡±Ö—ôΩ…µıqπUI0ÄËÅÌ¡Öùï}’…±ıqπïµÖπëîÅâ…’—îÄÈqπÌÖç—•ΩπÕÙà∞(ÄÄÄÄÄÄÄÄÄÄÄÅµÖ·}—Ω≠ïπÃÙ‹¿¿∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅï·çï¡–Å·çï¡—•Ω∏ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄåÅ1ÑÅçΩ±±ïç—îÅëîÅ±ÑÅëïµÖπëîÅ…ïÕ—îÅ¡…•Ω…•—Ö•…îÄËÅ’πîÅ¡ÖππîÅ%ÅπîÅëΩ•–Å¡ÖÃ(ÄÄÄÄÄÄÄÄåÅôÖ•…îÅ¡ï…ë…îÅ±ÑÅÕÖ•Õ•î∏Å]Ω…¨Å¡Ω’……ÑÅ…ïôΩ…µ’±ï»Å±îÅ—ï·—îÅ±Ω…ÃÅë‘Å—…Ö•—ïµïπ–∏(ÄÄÄÄÄÄÄÅ¡…•π–°òâM’¡¡Ω…–Åì•Ÿï±Ω¡¡ïµïπ–ÉäPÅ…ïôΩ…µ’±Ö—•Ω∏Åë•ôõ•À•îÄËÅÌï·çÙà∞Åô±’Õ†ıQ…’î§(ÄÄÄÄÄÄÄÅ…ï›…•——ï∏ÄÙÅÖç—•ΩπÃ(ÄÄÄÄÄÄÄÅÖ•}…ï›…•——ï∏ÄÙÅÖ±Õî((ÄÄÄÅ—Ω≠ï∏ÄÙÅΩÃπùï—ïπÿ†â9=Q%=9}A%}Q=-8à§(ÄÄÄÅ•òÅπΩ–Å—Ω≠ï∏Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1ÑÅçΩππï·•Ω∏Å9Ω—•Ω∏Åë‘ÅI4ÅªäeïÕ–Å¡ÖÃÅçΩπô•ù’À•î∏âÙ§∞Ä‘¿Ã(ÄÄÄÅπΩ—•Ωπ}Ÿï…Õ•Ω∏ÄÙÅΩÃπùï—ïπÿ†â9=Q%=9}A%}YIM%=8à∞Äà»¿»‘¥¿‰¥¿Ãà§(ÄÄÄÅÖ——Öç°µïπ—}’¡±ΩÖë}•êÄÙÄàà(ÄÄÄÅ•òÅÖ——Öç°µïπ–Ë(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÖ——Öç°µïπ—}’¡±ΩÖë}•êÄÙÅ}ç…µ}’¡±ΩÖë}πΩ—•Ωπ}Ö——Öç°µïπ–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—Ω≠ï∏∞ÅπΩ—•Ωπ}Ÿï…Õ•Ω∏∞ÅÖ——Öç°µïπ–§(ÄÄÄÄÄÄÄÅï·çï¡–Ä°…ï≈’ïÕ—ÃπIï≈’ïÕ—·çï¡—•Ω∏∞ÅI’π—•µï……Ω»∞ÅYÖ±’ï……Ω»§ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÅ¡…•π–°òâM’¡¡Ω…–Åì•Ÿï±Ω¡¡ïµïπ–ÉäPÅ”•≥•Ÿï…Õïµïπ–Å9Ω—•Ω∏Å•µ¡ΩÕÕ•â±îÄËÅÌï·çÙà∞Åô±’Õ†ıQ…’î§(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ1ÑÅ¡ß°çîÅ©Ω•π—îÅªäeÑÅ¡ÖÃÅ¡‘É©—…îÅïπŸΩÁ•îÉÄÅ9Ω—•Ω∏∏Äà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ1ÑÅëïµÖπëîÅªäeÑÅ¡ÖÃÉ•”§ÅçÀß•î∏à(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄÄÄÄÅÙ§∞Ä‘¿Ã(ÄÄÄÅπΩ—•Ωπ}¡ÖÂ±ΩÖêÄÙÅ}ç…µ}ëïŸï±Ω¡µïπ—}Õ’¡¡Ω…—}¡Öùî†(ÄÄÄÄÄÄÄÅ¡±Ö—ôΩ…¥∞Å¡Öùï}’…∞∞ÅÖç—•ΩπÃ∞Å…ï›…•——ï∏∞ÅÖ•}…ï›…•——ï∏ıÖ•}…ï›…•——ï∏∞(ÄÄÄÄÄÄÄÅÖ——Öç°µïπ—}’¡±ΩÖë}•êıÖ——Öç°µïπ—}’¡±ΩÖë}•ê∞(ÄÄÄÄÄÄÄÅÖ——Öç°µïπ—}ô•±ïπÖµîıÖ——Öç°µïπ—lâô•±ïπÖµîâtÅ•òÅÖ——Öç°µïπ–Åï±ÕîÄàà∞(ÄÄÄÄÄÄÄÅÖ——Öç°µïπ—}çΩπ—ïπ—}—Â¡îıÖ——Öç°µïπ—lâçΩπ—ïπ—}—Â¡îâtÅ•òÅÖ——Öç°µïπ–Åï±ÕîÄàà∞(ÄÄÄÄ§(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕîÄÙÅ…ï≈’ïÕ—Ãπ¡ΩÕ–†(ÄÄÄÄÄÄÄÄÄÄÄÄâ°——¡ÃËºΩÖ¡§ππΩ—•Ω∏πçΩ¥ΩÿƒΩ¡ÖùïÃà∞(ÄÄÄÄÄÄÄÄÄÄÄÅ°ïÖëï…ÃıÏ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ’—°Ω…•ÈÖ—•Ω∏àËÅòâ	ïÖ…ï»ÅÌ—Ω≠ïπÙà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ9Ω—•Ω∏µYï…Õ•Ω∏àËÅπΩ—•Ωπ}Ÿï…Õ•Ω∏∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâΩπ—ïπ–µQÂ¡îàËÄâÖ¡¡±•çÖ—•Ω∏Ω©ÕΩ∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÄÄÄÄÄÄÄÄÅ©ÕΩ∏ıπΩ—•Ωπ}¡ÖÂ±ΩÖê∞(ÄÄÄÄÄÄÄÄÄÄÄÅ—•µïΩ’–Ù†ƒ¿∞ÄÃ¿§∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ•òÅ…ïÕ¡ΩπÕîπÕ—Ö—’Õ}çΩëîÅπΩ–Å•∏ÅÏ»¿¿∞Ä»¿≈ÙË(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅI’π—•µï……Ω»°òâ9Ω—•Ω∏Å!QQ@ÅÌ…ïÕ¡ΩπÕîπÕ—Ö—’Õ}çΩëïÙà§(ÄÄÄÄÄÄÄÅç…ïÖ—ïêÄÙÅ…ïÕ¡ΩπÕîπ©ÕΩ∏†§(ÄÄÄÄÄÄÄÅπΩ—•Ωπ}’…∞ÄÙÅÕ—»°ç…ïÖ—ïêπùï–†â’…∞à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅπΩ—•Ωπ}’…∞Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†âK•¡ΩπÕîÅ9Ω—•Ω∏ÅÕÖπÃÅUI0à§(ÄÄÄÅï·çï¡–Ä°…ï≈’ïÕ—ÃπIï≈’ïÕ—·çï¡—•Ω∏∞ÅI’π—•µï……Ω»∞ÅYÖ±’ï……Ω»§ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ¡…•π–°òâM’¡¡Ω…–Åì•Ÿï±Ω¡¡ïµïπ–ÉäPÅçÀ•Ö—•Ω∏Å9Ω—•Ω∏Å•µ¡ΩÕÕ•â±îÄËÅÌï·çÙà∞Åô±’Õ†ıQ…’î§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1ÑÅëïµÖπëîÅªäeÑÅ¡ÖÃÅ¡‘É©—…îÅçÀß•îÅëÖπÃÅ9Ω—•Ω∏∏âÙ§∞Ä‘¿Ã(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄâ’…∞àËÅπΩ—•Ωπ}’…∞∞(ÄÄÄÄÄÄÄÄâ—•—±îàËÅπΩ—•Ωπ}¡ÖÂ±ΩÖëlâ¡…Ω¡ï…—•ïÃâulâAïπœ•îâulâ—•—±îâul¡ulâ—ï·–âulâçΩπ—ïπ–ât∞(ÄÄÄÄÄÄÄÄâÖ•}…ï›…•——ï∏àËÅÖ•}…ï›…•——ï∏∞(ÄÄÄÄÄÄÄÄâÖ——Öç°µïπ—}’¡±ΩÖëïêàËÅâΩΩ∞°Ö——Öç°µïπ—}’¡±ΩÖë}•ê§∞(ÄÄÄÅÙ§∞Ä»¿ƒ(()Ö¡¿π¡ΩÕ–†àΩÖ¡§Ωç…¥ΩÕ—Ö—’ÕïÃà§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}Öëë}Õ—Ö—’Ã†§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§ÏÅ±Öâï∞ÄÙÅÕ—»†°…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ§πùï–†â±Öâï∞à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅÕ—Ö—’ÕïÃÄÙÅ}ç…µ}Õ—Ö—’ÕïÃ°ëÖ—Ñ§(ÄÄÄÅ•òÅπΩ–Å±Öâï∞ÅΩ»Å±Öâï∞Å•∏ÅÕ—Ö—’ÕïÃË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâï——îÉ•—Ö¡îÅïÕ–ÅŸ•ëîÅΩ‘Åï·•Õ—îÅì•´ÄâÙ§∞Ä–¿¿(ÄÄÄÅëÖ—Ölâç…µ}Õ—Ö—’ÕïÃâtÄÙÅl©Õ—Ö—’ÕïÕlË¥Õt∞Å±Öâï∞∞Ä©Õ—Ö—’ÕïÕl¥ÃÈut(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§ÏÅ…ï—’…∏Å©ÕΩπ•ô‰°ëÖ—Ölâç…µ}Õ—Ö—’ÕïÃât§∞Ä»¿ƒ(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩÕ—Ö—’ÕïÃºÒ¡Ö—†ÈΩ±ë}±Öâï∞¯à∞Åµï—°ΩëÃılâAQ à∞Äâ1Qât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}ç°Öπùï}Õ—Ö—’Ã°Ω±ë}±Öâï∞§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§ÏÅÕ—Ö—’ÕïÃÄÙÅ}ç…µ}Õ—Ö—’ÕïÃ°ëÖ—Ñ§(ÄÄÄÅ•òÅΩ±ë}±Öâï∞ÅπΩ–Å•∏ÅÕ—Ö—’ÕïÃË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄã%—Ö¡îÅ•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅ•òÅΩ±ë}±Öâï∞Å•∏ÅI5}IMIY}MQQUMLË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâï——îÉ•—Ö¡îÅÕÂÕ”°µîÅπîÅ¡ï’–Å¡ÖÃÉ©—…îÅµΩë•ôß•îâÙ§∞Ä–¿¿(ÄÄÄÅ•òÅ…ï≈’ïÕ–πµï—°ΩêÄÙÙÄâAQ àË(ÄÄÄÄÄÄÄÅ…ï¡±Öçïµïπ–ÄÙÅÕ—»†°…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ§πùï–†â±Öâï∞à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å…ï¡±Öçïµïπ–ÅΩ»Ä°…ï¡±Öçïµïπ–Å•∏ÅÕ—Ö—’ÕïÃÅÖπêÅ…ï¡±Öçïµïπ–ÄÑÙÅΩ±ë}±Öâï∞§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ%π—•—’≥§Å•πŸÖ±•ëîâÙ§∞Ä–¿¿(ÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÅ…ï¡±Öçïµïπ–ÄÙÅπï·–†°ŸÖ±’îÅôΩ»ÅŸÖ±’îÅ•∏ÅÕ—Ö—’ÕïÃÅ•òÅŸÖ±’îÄÑÙÅΩ±ë}±Öâï∞ÅÖπêÅŸÖ±’îÅπΩ–Å•∏ÅI5}IMIY}MQQUML§∞Äâ9Ω’ŸïÖ’‡à§(ÄÄÄÅπï·—}Õ—Ö—’ÕïÃÄÙÅm…ï¡±Öçïµïπ–Å•òÅŸÖ±’îÄÙÙÅΩ±ë}±Öâï∞Åï±ÕîÅŸÖ±’îÅôΩ»ÅŸÖ±’îÅ•∏ÅÕ—Ö—’ÕïÕtÅ•òÅ…ï≈’ïÕ–πµï—°ΩêÄÙÙÄâAQ àÅï±ÕîÅmŸÖ±’îÅôΩ»ÅŸÖ±’îÅ•∏ÅÕ—Ö—’ÕïÃÅ•òÅŸÖ±’îÄÑÙÅΩ±ë}±Öâï±t(ÄÄÄÅëÖ—Ölâç…µ}Õ—Ö—’ÕïÃâtÄÙÅπï·—}Õ—Ö—’ÕïÃ(ÄÄÄÅôΩ»ÅçΩπ—Öç–Å•∏ÅëÖ—Ñπùï–†âç…µ}çΩπ—Öç—Ãà∞Åmt§Ë(ÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç–πùï–†âÕ—Ö—’–à§ÄÙÙÅΩ±ë}±Öâï∞Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâÕ—Ö—’–âtÄÙÅ…ï¡±Öçïµïπ–(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâÕ—Ö—’ÕïÃàËÅπï·—}Õ—Ö—’ÕïÃ∞Äâ…ï¡±Öçïµïπ–àËÅ…ï¡±Öçïµïπ—Ù§(()ëïòÅ}ç…µ}Õï——•πùÕ}¡ÖÂ±ΩÖê°ëÖ—Ñ§Ë(ÄÄÄÄààâ9Ω…µÖ±•ÕîÅ±ïÃÅÀ•ù±ÖùïÃÅI4ÅÕÖπÃÅ…ï±•…îÅ±îÅô•ç°•ï»Å)M=8∏ààà(ÄÄÄÅÕï——•πùÃÄÙÅëÖ—ÑπÕï—ëïôÖ’±–†âç…µ}Õï——•πùÃà∞ÅÌÙ§(ÄÄÄÅëïôÖ’±—ÃÄÙÅU1Q}Qlâç…µ}Õï——•πùÃât(ÄÄÄÅôΩ»Å≠ï‰∞ÅŸÖ±’îÅ•∏ÅëïôÖ’±—Ãπ•—ïµÃ†§Ë(ÄÄÄÄÄÄÄÅÕï——•πùÃπÕï—ëïôÖ’±–†(ÄÄÄÄÄÄÄÄÄÄÄÅ≠ï‰∞(ÄÄÄÄÄÄÄÄÄÄÄÅŸÖ±’îπçΩ¡‰†§Å•òÅ•Õ•πÕ—Öπçî°ŸÖ±’î∞Ä°ë•ç–∞Å±•Õ–§§Åï±ÕîÅŸÖ±’î∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅ…ï—’…∏ÅÕï——•πùÃ(()ëïòÅ}ç…µ}¡…ïÕï—}ŸÖ±’ïÃ°ŸÖ±’î∞Å±Öâï∞§Ë(ÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°ŸÖ±’î∞Å±•Õ–§Ë(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»°òâ1ïÃÅÌ±Öâï±ÙÅëΩ•Ÿïπ–É©—…îÅ—…ÖπÕµ•ÃÅÕΩ’ÃÅôΩ…µîÅëîÅ±•Õ—î∏à§(ÄÄÄÅ•òÅ±ï∏°ŸÖ±’î§Ä¯Ä‘¿Ë(ÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»°òâYΩ’ÃÅπîÅ¡Ω’ŸïËÅ¡ÖÃÅïπ…ïù•Õ—…ï»Å¡±’ÃÅëîÄ‘¿ÅÌ±Öâï±Ù∏à§(ÄÄÄÅπΩ…µÖ±•ÈïêÄÙÅmt(ÄÄÄÅÕïï∏ÄÙÅÕï–†§(ÄÄÄÅôΩ»Å•—ï¥Å•∏ÅŸÖ±’îË(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°•—ï¥∞ÅÕ—»§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâ°Ö≈’îÉ•≥•µïπ–ÅëîÅ±ÑÅ±•Õ—îÉ
¨ÅÌ±Öâï±ÙÉ
ÏÅëΩ•–É©—…îÅ’∏Å—ï·—î∏à(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ—ï·–ÄÙÄàÄàπ©Ω•∏°•—ï¥πÕ¡±•–†§§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å—ï·–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ•òÅ±ï∏°—ï·–§Ä¯Äƒÿ¿Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâ°Ö≈’îÉ•≥•µïπ–ÅëîÅ±ÑÅ±•Õ—îÉ
¨ÅÌ±Öâï±ÙÉ
ÏÅïÕ–Å±•µ•”§ÉÄÄƒÿ¿ÅçÖ…Öç”°…ïÃ∏à(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ≠ï‰ÄÙÅ—ï·–πçÖÕïôΩ±ê†§(ÄÄÄÄÄÄÄÅ•òÅ≠ï‰Å•∏ÅÕïï∏Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅÕïï∏πÖëê°≠ï‰§(ÄÄÄÄÄÄÄÅπΩ…µÖ±•ÈïêπÖ¡¡ïπê°—ï·–§(ÄÄÄÅ…ï—’…∏ÅπΩ…µÖ±•Èïê(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩÕï——•πùÃà∞Åµï—°ΩëÃılâPà∞ÄâAQ ât§)±Ωù•π}…ï≈’•…ïê)}ç…µ}Õï…•Ö±•Èïê)ëïòÅç…µ}Õï——•πùÃ†§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅÕï——•πùÃÄÙÅ}ç…µ}Õï——•πùÕ}¡ÖÂ±ΩÖê°ëÖ—Ñ§(ÄÄÄÅ•òÅ…ï≈’ïÕ–πµï—°ΩêÄÙÙÄâPàË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Õï——•πùÃ§(ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(ÄÄÄÅÖ±±Ω›ïêÄÙÅÏ(ÄÄÄÄÄÄÄÄâçÖ±ïπëÖ…}ëïôÖ’±—}Ÿ•ï‹à∞ÄâçÖ±ïπëÖ…}›Ω…≠ëÖÂ}Õ—Ö…–à∞ÄâçÖ±ïπëÖ…}›Ω…≠ëÖÂ}ïπêà∞(ÄÄÄÄÄÄÄÄâπΩ—•ô•çÖ—•Ωπ}µïπ—•ΩπÃà∞ÄâπΩ—•ô•çÖ—•Ωπ}ÕÂÕ—ï¥à∞Äâë•…ïç—•Ωπ}çΩÕ—Ãà∞(ÄÄÄÄÄÄÄÄâçÖ±±}πΩ—ï}¡…ïÕï—Ãà∞Äâ…ï±Öπçï}µΩ—•ô}¡…ïÕï—Ãà∞(ÄÄÄÄÄÄÄÄâµÖπ’Ö±}πï·—}Öç—•Ωπ}¡…ïÕï—Ãà∞(ÄÄÄÅÙ(ÄÄÄÅ•òÄâë•…ïç—•Ωπ}çΩÕ—ÃàÅ•∏Å¡ÖÂ±ΩÖêÅÖπêÄ°ç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ§πùï–†â…Ω±îà§ÄÑÙÄâÖëµ•∏àË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1ÑÅçΩπô•ù’…Ö—•Ω∏ÅëïÃÅçøÌ—ÃÅïÕ–ÅÀ•Õï…€•îÉÄÅ≥äeÖëµ•π•Õ—…Ö—ï’»∏âÙ§∞Ä–¿Ã(ÄÄÄÅôΩ»Å≠ï‰Å•∏ÅÖ±±Ω›ïêπ•π—ï…Õïç—•Ω∏°¡ÖÂ±ΩÖê§Ë(ÄÄÄÄÄÄÄÅ•òÅ≠ï‰Å•∏ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâçÖ±±}πΩ—ï}¡…ïÕï—Ãà∞Äâ…ï±Öπçï}µΩ—•ô}¡…ïÕï—Ãà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâµÖπ’Ö±}πï·—}Öç—•Ωπ}¡…ïÕï—Ãà∞(ÄÄÄÄÄÄÄÅÙË(ÄÄÄÄÄÄÄÄÄÄÄÅ±Öâï∞ÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâçÖ±±}πΩ—ï}¡…ïÕï—ÃàËÄâÀ•¡ΩπÕïÃÅ¡À§µïπ…ïù•Õ—À•ïÃà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ…ï±Öπçï}µΩ—•ô}¡…ïÕï—ÃàËÄâµΩ—•ôÃÅëîÅ…ï±Öπçîà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâµÖπ’Ö±}πï·—}Öç—•Ωπ}¡…ïÕï—ÃàËÄâ¡…Ωç°Ö•πïÃÅÖç—•ΩπÃÅ¡À§µïπ…ïù•Õ—À•ïÃà∞(ÄÄÄÄÄÄÄÄÄÄÄÅım≠ïÂt(ÄÄÄÄÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕï——•πùÕm≠ïÂtÄÙÅ}ç…µ}¡…ïÕï—}ŸÖ±’ïÃ°¡ÖÂ±ΩÖêπùï–°≠ï‰§∞Å±Öâï∞§(ÄÄÄÄÄÄÄÄÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÅÕ—»°ï·å•Ù§∞Ä–¿¿(ÄÄÄÄÄÄÄÅï±•òÅ≠ï‰ÄÙÙÄâë•…ïç—•Ωπ}çΩÕ—ÃàË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩÕ—ÃÄÙÅ¡ÖÂ±ΩÖêπùï–°≠ï‰§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°çΩÕ—Ã∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1ÑÅçΩπô•ù’…Ö—•Ω∏ÅëïÃÅçøÌ—ÃÅïÕ–Å•πŸÖ±•ëî∏âÙ§∞Ä–¿¿(ÄÄÄÄÄÄÄÄÄÄÄÅπΩ…µÖ±•ÈïêÄÙÅÌÙ(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»Å±Öâï∞∞ÅŸÖ±’îÅ•∏ÅçΩÕ—Ãπ•—ïµÃ†§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…Ö‹ÄÙÅÕ—»°ŸÖ±’îÅΩ»Äàà§πÕ—…•¿†§π…ï¡±Öçî†àÄà∞Äàà§π…ï¡±Öçî†à∞à∞Äà∏à§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–Å…Ö‹Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖµΩ’π–ÄÙÅô±ΩÖ–°…Ö‹§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÅòâ1îÅçøÌ–ÅëîÅÌ±Öâï±ÙÅïÕ–Å•πŸÖ±•ëî∏âÙ§∞Ä–¿¿(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅÖµΩ’π–ÄÄ¿Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1ïÃÅçøÌ—ÃÅëΩ•Ÿïπ–É©—…îÅ¡ΩÕ•—•ôÃ∏âÙ§∞Ä–¿¿(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅπΩ…µÖ±•ÈïëmÕ—»°±Öâï∞•tÄÙÅÖµΩ’π–(ÄÄÄÄÄÄÄÄÄÄÄÅÕï——•πùÕm≠ïÂtÄÙÅπΩ…µÖ±•Èïê(ÄÄÄÄÄÄÄÅï±•òÅ≠ï‰πÕ—Ö…—Õ›•—††âπΩ—•ô•çÖ—•Ωπ|à§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÕï——•πùÕm≠ïÂtÄÙÅâΩΩ∞°¡ÖÂ±ΩÖêπùï–°≠ï‰§§(ÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÅÕï——•πùÕm≠ïÂtÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–°≠ï‰§ÅΩ»Äàà§(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Õï——•πùÃ§(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃΩâ’±¨à∞Åµï—°ΩëÃılâAQ à∞Äâ1Qât§)±Ωù•π}…ï≈’•…ïê)}ç…µ}Õï…•Ö±•Èïê)ëïòÅç…µ}çΩπ—Öç—Õ}â’±¨†§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§ÏÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(ÄÄÄÅ•ëÃÄÙÅÌÕ—»°ŸÖ±’î§ÅôΩ»ÅŸÖ±’îÅ•∏Å¡ÖÂ±ΩÖêπùï–†â•ëÃà∞Åmt§Å•òÅŸÖ±’ïÙ(ÄÄÄÅ•òÅπΩ–Å•ëÃÅΩ»Å±ï∏°•ëÃ§Ä¯Ä‘¿¿Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâO•±ïç—•Ω∏Å•πŸÖ±•ëî∏âÙ§∞Ä–¿¿(ÄÄÄÅ•òÅ…ï≈’ïÕ–πµï—°ΩêÄÙÙÄâ1QàË(ÄÄÄÄÄÄÄÅëï±ï—ïë}•ëÃÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†â•êà§§(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»ÅçΩπ—Öç–Å•∏ÅëÖ—Ñπùï–†âç…µ}çΩπ—Öç—Ãà∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅÕ—»°çΩπ—Öç–πùï–†â•êà§§Å•∏Å•ëÃ(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÅëÖ—Ölâç…µ}çΩπ—Öç—ÃâtÄÙÅl(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–ÅôΩ»ÅçΩπ—Öç–Å•∏ÅëÖ—Ñπùï–†âç…µ}çΩπ—Öç—Ãà∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅÕ—»°çΩπ—Öç–πùï–†â•êà§§ÅπΩ–Å•∏Åëï±ï—ïë}•ëÃ(ÄÄÄÄÄÄÄÅt(ÄÄÄÄÄÄÄÅëÖ—Ölâç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—ÃâtÄÙÅl(ÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ–(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»ÅÖ¡¡Ω•π—µïπ–Å•∏ÅëÖ—Ñπùï–†âç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Ãà∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅÕ—»°Ö¡¡Ω•π—µïπ–πùï–†âçΩπ—Öç—}•êà§§ÅπΩ–Å•∏Åëï±ï—ïë}•ëÃ(ÄÄÄÄÄÄÄÅt(ÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâëï±ï—ïë}•ëÃàËÅÕΩ…—ïê°ëï±ï—ïë}•ëÃ§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩ’π–àËÅ±ï∏°ëï±ï—ïë}•ëÃ§∞(ÄÄÄÄÄÄÄÅÙ§(ÄÄÄÅÖç—•Ω∏ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âÖç—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅÕ—Ö—’ÕïÃÄÙÅ}ç…µ}Õ—Ö—’ÕïÃ°ëÖ—Ñ§(ÄÄÄÅ’¡ëÖ—ïêÄÙÅmt(ÄÄÄÅôΩ»ÅçΩπ—Öç–Å•∏ÅëÖ—Ñπùï–†âç…µ}çΩπ—Öç—Ãà∞Åmt§Ë(ÄÄÄÄÄÄÄÅ•òÅÕ—»°çΩπ—Öç–πùï–†â•êà§§ÅπΩ–Å•∏Å•ëÃË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅΩ±ë}Õ—Ö—’ÃÄÙÅçΩπ—Öç–πùï–†âÕ—Ö—’–à§(ÄÄÄÄÄÄÄÅ•òÅÖç—•Ω∏ÄÙÙÄâÕ—Ö—’ÃàË(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö—’ÃÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âŸÖ±’îà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅÕ—Ö—’ÃÅπΩ–Å•∏ÅÕ—Ö—’ÕïÃË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄã%—Ö¡îÅ•πçΩππ’î∏âÙ§∞Ä–¿¿(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâÕ—Ö—’–âtÄÙÅÕ—Ö—’Ã(ÄÄÄÄÄÄÄÅï±•òÅÖç—•Ω∏ÄÙÙÄâçΩµµï…ç•Ö∞àË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâçΩµµï…ç•Ö∞âtÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âŸÖ±’îà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅï±•òÅÖç—•Ω∏ÄÙÙÄâÖ…ç°•ŸîàË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâÖ…ç°•Ÿïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÅï±•òÅÖç—•Ω∏ÄÙÙÄâ…ïÕ—Ω…îàË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâÖ…ç°•Ÿïë}Ö–âtÄÙÄàà(ÄÄÄÄÄÄÄÅï±•òÅÖç—•Ω∏ÄÙÙÄâ…ï±ÖπçîàË(ÄÄÄÄÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï≈’ïÕ—ïë}ëÖ—îÄÙÅ}ç…µ}…ï±Öπçï}ëÖ—î†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêπùï–†âŸÖ±’îà§∞Å›ïï≠ëÖÂÕ}Ωπ±‰ıQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡±Öππïê∞Å|ÄÙÅ}ç…µ}Õç°ïë’±ï}…ï±Öπçî†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï≈’ïÕ—ïë}ëÖ—î∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕΩ’…çîÙââ’±¨à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµΩ—•òı¡ÖÂ±ΩÖêπùï–†âµΩ—•òà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÅÕ—»°ï·å•Ù§∞Ä–¿¿(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâÕ—Ö—’–âtÄÙÄâÅ…ï±Öπçï»à(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ¡±ÖππïêË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞Äâ…ï±Öπçîà∞ÄâIï±ÖπçîÅ¡±Öπ•ôß•îÅï∏Åù…Ω’¡îà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄàÉ
‹Äàπ©Ω•∏°ô•±—ï»°9Ωπî∞Ål(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâA…Ωç°Ö•πîÅ…ï±ÖπçîÅ±îÅÌ¡±ÖππïëlùÕç°ïë’±ïë}ëÖ—îùuÙà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâ5Ω—•òÄËÅÌ¡±Öππïêπùï–†ùµΩ—•òú•ÙàÅ•òÅ¡±Öππïêπùï–†âµΩ—•òà§Åï±ÕîÄàà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅt§§§(ÄÄÄÄÄÄÄÅï±•òÅÖç—•Ω∏ÄÙÙÄâë•Õ≈’Ö±•ô‰àË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÖÕΩ∏ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†â…ïÖÕΩ∏à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–Å…ïÖÕΩ∏Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1îÅµΩ—•òÅëîÅë•Õ≈’Ö±•ô•çÖ—•Ω∏ÅïÕ–ÅΩâ±•ùÖ—Ω•…î∏âÙ§∞Ä–¿¿(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}Õç°ïë’±ï}…ï±Öπçî°çΩπ—Öç–∞Äàà§(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–π’¡ëÖ—î°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö—’–àËÄâ•Õ≈’Ö±•ôß§à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâë•Õ≈’Ö±•ô•çÖ—•Ωπ}…ïÖÕΩ∏àËÅ…ïÖÕΩ∏∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâë•Õ≈’Ö±•ô•çÖ—•Ωπ}ëï—Ö•∞àËÅÕ—»°¡ÖÂ±ΩÖêπùï–†âëï—Ö•∞à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ…ïÖç—•ŸÖ—•Ωπ}ëÖ—îàËÅÕ—»°¡ÖÂ±ΩÖêπùï–†â…ïÖç—•ŸÖ—•Ωπ}ëÖ—îà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄÄÄÄÅÙ§(ÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâç—•Ω∏Åù…Ω’√•îÅ•πçΩππ’î∏âÙ§∞Ä–¿¿(ÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç–πùï–†âÕ—Ö—’–à§ÄÑÙÅΩ±ë}Õ—Ö—’ÃË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâÕ—Ö—’Õ}ç°Öπùïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç–πùï–†âÕ—Ö—’–à§ÄÙÙÄâΩπŸï…—§àÅÖπêÅπΩ–ÅçΩπ—Öç–πùï–†âçΩπŸï…—ïë}Ö–à§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâçΩπŸï…—ïë}Ö–âtÄÙÅçΩπ—Öç—lâÕ—Ö—’Õ}ç°Öπùïë}Ö–ât(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç–πùï–†âÕ—Ö—’–à§ÄÑÙÄâ•Õ≈’Ö±•ôß§àË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâë•Õ≈’Ö±•ô•çÖ—•Ωπ}…ïÖÕΩ∏âtÄÙÄàà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâë•Õ≈’Ö±•ô•çÖ—•Ωπ}ëï—Ö•∞âtÄÙÄàà(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç–πùï–†âÕ—Ö—’–à§ÄÙÙÄâ•Õ≈’Ö±•ôß§àË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖç—•Ÿ•—Â}ëï—Ö•±ÃÄÙÅl(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâπç•ï∏ÅÕ—Ö—’–ÄËÅÌΩ±ë}Õ—Ö—’ÃÅΩ»Äù9Ω∏Å…ïπÕï•ùª§ùÙà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâ5Ω—•òÄËÅÌçΩπ—Öç–πùï–†ùë•Õ≈’Ö±•ô•çÖ—•Ωπ}…ïÖÕΩ∏ú•Ùà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅt(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç–πùï–†âë•Õ≈’Ö±•ô•çÖ—•Ωπ}ëï—Ö•∞à§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖç—•Ÿ•—Â}ëï—Ö•±ÃπÖ¡¡ïπê†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâAÀ•ç•Õ•ΩπÃÄËÅÌçΩπ—Öç—lùë•Õ≈’Ö±•ô•çÖ—•Ωπ}ëï—Ö•∞ùuÙà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç–πùï–†â…ïÖç—•ŸÖ—•Ωπ}ëÖ—îà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖç—•Ÿ•—Â}ëï—Ö•±ÃπÖ¡¡ïπê†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâK•Öç—•ŸÖ—•Ω∏Å¡À•Ÿ’îÄËÅÌçΩπ—Öç—lù…ïÖç—•ŸÖ—•Ωπ}ëÖ—îùuÙà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞ÄâÕ—Ö—’–à∞ÄâA•Õ—îÅë•Õ≈’Ö±•ôß•îà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄàÉ
‹Äàπ©Ω•∏°Öç—•Ÿ•—Â}ëï—Ö•±Ã§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞ÄâÕ—Ö—’–à∞ÅòâM—Ö—’–ÄËÅÌçΩπ—Öç—lùÕ—Ö—’–ùuÙà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâπç•ï∏ÅÕ—Ö—’–ÄËÅÌΩ±ë}Õ—Ö—’ÕÙÉ
‹ÅÖç—•Ω∏Åù…Ω’√•îà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÅ’¡ëÖ—ïêπÖ¡¡ïπê°}ç…µ}çΩπ—Öç—}…ïÕ¡ΩπÕî°çΩπ—Öç–∞ÅëÖ—Ñ§§(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâ’¡ëÖ—ïêàËÅ’¡ëÖ—ïê∞ÄâçΩ’π–àËÅ±ï∏°’¡ëÖ—ïê•Ù§(()Ö¡¿π¡ΩÕ–†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃΩµï…ùîà§)±Ωù•π}…ï≈’•…ïê)}ç…µ}Õï…•Ö±•Èïê)ëïòÅç…µ}çΩπ—Öç—Õ}µï…ùî†§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§ÏÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(ÄÄÄÅ—Ö…ùï–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞Å¡ÖÂ±ΩÖêπùï–†â—Ö…ùï—}•êà§§(ÄÄÄÅÕΩ’…çîÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞Å¡ÖÂ±ΩÖêπùï–†âÕΩ’…çï}•êà§§(ÄÄÄÅ•òÅπΩ–Å—Ö…ùï–ÅΩ»ÅπΩ–ÅÕΩ’…çîÅΩ»Å—Ö…ùï–Å•ÃÅÕΩ’…çîË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1ïÃÅëï’‡Åô•ç°ïÃÉÄÅô’Õ•Ωππï»ÅÕΩπ–Å•πŸÖ±•ëïÃ∏âÙ§∞Ä–¿¿(ÄÄÄÅ¡…Ω—ïç—ïêÄÙÅÏâ•êà∞Äâç…ïÖ—ïë}Ö–âÙ(ÄÄÄÅôΩ»Å≠ï‰∞ÅŸÖ±’îÅ•∏ÅÕΩ’…çîπ•—ïµÃ†§Ë(ÄÄÄÄÄÄÄÅ•òÅ≠ï‰Å•∏Å¡…Ω—ïç—ïêÅΩ»Å≠ï‰Å•∏ÅÏâÖç—•Ÿ•—•ïÃà∞Äâ¡’â±•çÖ—•ΩπÃà∞Äâ…ï±ÖπçïÃà∞ÄâÕΩ’…çï}°•Õ—Ω…‰à∞Äâµï—Ö}ÖπÕ›ï…ÃâÙË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å—Ö…ùï–πùï–°≠ï‰§ÅÖπêÅŸÖ±’îÅπΩ–Å•∏Ä°9Ωπî∞Äàà∞Åmt∞ÅÌÙ§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ—Ö…ùï—m≠ïÂtÄÙÅŸÖ±’î(ÄÄÄÅôΩ»Å≠ï‰Å•∏Ä†âÖç—•Ÿ•—•ïÃà∞Äâ¡’â±•çÖ—•ΩπÃà∞Äâ…ï±ÖπçïÃà∞Äâµï—Ö}ÖπÕ›ï…Ãà§Ë(ÄÄÄÄÄÄÄÅµï…ùïêÄÙÅl®°—Ö…ùï–πùï–°≠ï‰§ÅΩ»Åmt§∞Ä®°ÕΩ’…çîπùï–°≠ï‰§ÅΩ»Åmt•t(ÄÄÄÄÄÄÄÅÕïï∏ÄÙÅÕï–†§ÏÅ’π•≈’îÄÙÅmt(ÄÄÄÄÄÄÄÅôΩ»Å•—ï¥Å•∏Åµï…ùïêË(ÄÄÄÄÄÄÄÄÄÄÄÅµÖ…≠ï»ÄÙÅÕ—»°•—ï¥πùï–†â•êà§ÅΩ»Å©ÕΩ∏πë’µ¡Ã°•—ï¥∞ÅÕΩ…—}≠ïÂÃıQ…’î∞ÅïπÕ’…ï}ÖÕç•§ıÖ±Õî§§Å•òÅ•Õ•πÕ—Öπçî°•—ï¥∞Åë•ç–§Åï±ÕîÅÕ—»°•—ï¥§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅµÖ…≠ï»Å•∏ÅÕïï∏Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÄÄÄÄÅÕïï∏πÖëê°µÖ…≠ï»§ÏÅ’π•≈’îπÖ¡¡ïπê°•—ï¥§(ÄÄÄÄÄÄÄÅ—Ö…ùï—m≠ïÂtÄÙÅ’π•≈’î(ÄÄÄÅ}ç…µ}…ïçΩ…ë}Ω…•ù•∏†(ÄÄÄÄÄÄÄÅ—Ö…ùï–∞Å—Ö…ùï–πùï–†âΩ…•ù•πîà§ÅΩ»Å—Ö…ùï–πùï–†âÕΩ’…çîà§∞(ÄÄÄÄÄÄÄÅÕΩ’…çîı—Ö…ùï–πùï–†âÕΩ’…çîà∞Äàà§∞ÅëÖ—îı—Ö…ùï–πùï–†âç…ïÖ—ïë}Ö–à§∞(ÄÄÄÄ§(ÄÄÄÅ}ç…µ}…ïçΩ…ë}Ω…•ù•∏†(ÄÄÄÄÄÄÄÅ—Ö…ùï–∞ÅÕΩ’…çîπùï–†âΩ…•ù•πîà§ÅΩ»ÅÕΩ’…çîπùï–†âÕΩ’…çîà§∞(ÄÄÄÄÄÄÄÅÕΩ’…çîıÕΩ’…çîπùï–†âÕΩ’…çîà∞Äàà§∞ÅëÖ—îıÕΩ’…çîπùï–†âç…ïÖ—ïë}Ö–à§∞(ÄÄÄÄ§(ÄÄÄÅôΩ»ÅΩ…•ù•π}ïπ—…‰Å•∏ÅÕΩ’…çîπùï–†âÕΩ’…çï}°•Õ—Ω…‰à∞Åmt§Ë(ÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°Ω…•ù•π}ïπ—…‰∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}…ïçΩ…ë}Ω…•ù•∏†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—Ö…ùï–∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ…•ù•π}ïπ—…‰πùï–†âΩ…•ù•∏à§ÅΩ»ÅΩ…•ù•π}ïπ—…‰πùï–†âΩ…•ù•πîà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕΩ’…çîıΩ…•ù•π}ïπ—…‰πùï–†âÕΩ’…çîà∞Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅï·—ï…πÖ±}•êıΩ…•ù•π}ïπ—…‰πùï–†âï·—ï…πÖ±}•êà∞Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—ï·–ıΩ…•ù•π}ïπ—…‰∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅëÖ—îıΩ…•ù•π}ïπ—…‰πùï–†âëÖ—îà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÅôΩ»ÅÖ¡¡Ω•π—µïπ–Å•∏ÅëÖ—Ñπùï–†âç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Ãà∞Åmt§Ë(ÄÄÄÄÄÄÄÅ•òÅÕ—»°Ö¡¡Ω•π—µïπ–πùï–†âçΩπ—Öç—}•êà§§ÄÙÙÅÕ—»°ÕΩ’…çîπùï–†â•êà§§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—lâçΩπ—Öç—}•êâtÄÙÅ—Ö…ùï–πùï–†â•êà§(ÄÄÄÅëÖ—Ölâç…µ}çΩπ—Öç—Ãâtπ…ïµΩŸî°ÕΩ’…çî§(ÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°—Ö…ùï–∞Äâô’Õ•Ω∏à∞Äâ•ç°ïÃÅô’Õ•Ωπª•ïÃà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâ1ÑÅô•ç°îÅëîÅÌÕΩ’…çîπùï–†ù¡…ïπΩ¥ú∞Äúú•ÙÅÌÕΩ’…çîπùï–†ùπΩ¥ú∞Äúú•ÙÅÑÉ•”§Å…ïù…Ω’√•îÅ•ç§∏à§(ÄÄÄÅ—Ö…ùï—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâçΩπ—Öç–àËÅ}ç…µ}çΩπ—Öç—}…ïÕ¡ΩπÕî°—Ö…ùï–∞ÅëÖ—Ñ§∞Äâ…ïµΩŸïë}•êàËÅÕΩ’…çîπùï–†â•êà•Ù§(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯à∞Åµï—°ΩëÃılâPà∞ÄâAQ à∞Äâ1Qât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çΩπ—Öç–°çΩπ—Öç—}•ê§Ë(ÄÄÄÅ•òÅ…ï≈’ïÕ–πµï—°ΩêÄÙÙÄâPàË(ÄÄÄÄÄÄÄÅëÖ—ÑÄÙÅ}ç…µ}¡…ï¡Ö…ïë}…ïÖë}µΩëï∞†§(ÄÄÄÄÄÄÄÅÕΩ’…çï}çΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅÕΩ’…çï}çΩπ—Öç–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÄÄÄÄÅ}ç…µ}¡ï…Õ•Õ—}¡…ï¡Ö…ïë}çΩπ—Öç—}•ô}•ë±î°ÕΩ’…çï}çΩπ—Öç–§(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÄÙÅçΩ¡‰πëïï¡çΩ¡‰°ÕΩ’…çï}çΩπ—Öç–§(ÄÄÄÄÄÄÄÅÕπÖ¡Õ°Ω–ÄÙÅçΩ¡‰πëïï¡çΩ¡‰†(ÄÄÄÄÄÄÄÄÄÄÄÅëÖ—Ñπùï–†âç…µ}çπÖ¡Õ}ÕçΩ…•πù}ÕπÖ¡Õ°Ω—Ãà∞ÅÌÙ§πùï–°Õ—»°çΩπ—Öç—}•ê§§(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°}ç…µ}çΩπ—Öç—}ëï—Ö•±}…ïÕ¡ΩπÕî†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞Å…ïù’±Ö—Ω…Â}ÕπÖ¡Õ°Ω–ıÕπÖ¡Õ°Ω–∞(ÄÄÄÄÄÄÄÄ§§((ÄÄÄÄåÅ5’—Ö—•ΩπÃÅ…ïµÖ•∏ÅÕï…•Ö±•ÈïêÏÅ…ïÖêµΩπ±‰ÅçΩπ—Öç–ÅÕ°ïï—ÃÅπºÅ±Ωπùï»Å≈’ï’î(ÄÄÄÄåÅâï°•πêÅÑÅ±ΩπúÅ]=Å…ïçΩπç•±•Ö—•Ω∏ÅΩ»ÅÖπΩ—°ï»Å’Õï»ùÃÅÖ’—ΩÕÖŸî∏(ÄÄÄÅ›•—†Å}I5}I=9%1%Q%=9}1=,Ë(ÄÄÄÄÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÄÄÄÄÅ•òÅ…ï≈’ïÕ–πµï—°ΩêÄÙÙÄâ1QàË(ÄÄÄÄÄÄÄÄÄÄÄÅëÖ—Ölâç…µ}çΩπ—Öç—Ãâtπ…ïµΩŸî°çΩπ—Öç–§(ÄÄÄÄÄÄÄÄÄÄÄÅëÖ—Ölâç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—ÃâtÄÙÅl(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅëÖ—Ñπùï–†âç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Ãà∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ•—ï¥πùï–†âçΩπ—Öç—}•êà§ÄÑÙÅçΩπ—Öç—}•ê(ÄÄÄÄÄÄÄÄÄÄÄÅt(ÄÄÄÄÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Äàà∞Ä»¿–(ÄÄÄÄÄÄÄÅ…ï—’…∏Å}ç…µ}¡Ö—ç°}çΩπ—Öç—}±Ωç≠ïê°ëÖ—Ñ∞ÅçΩπ—Öç–∞ÅçΩπ—Öç—}•ê§(()Ö¡¿π…Ω’—î†(ÄÄÄÄàΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯ΩçπÖ¡ÃµçÖ…êµŸÖ±•ë•—‰à∞(ÄÄÄÅµï—°ΩëÃılâA=MPât∞(§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çΩπ—Öç—}çπÖ¡Õ}çÖ…ë}ŸÖ±•ë•—‰°çΩπ—Öç—}•ê§Ë(ÄÄÄÄààâ[•…•ô•îÅ’∏Å9UÅëÖπÃÅ∞ùÖππ’Ö•…îÅ¡’â±•åÅ9ALÉÄÅ±ÑÅëïµÖπëî∏ààà(ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(ÄÄÄÅçπÖ¡Õ}π’àÄÙÅ…îπÕ’à°»âqÃ¨à∞Äàà∞ÅÕ—»°¡ÖÂ±ΩÖêπùï–†âπ’àà§ÅΩ»Äàà§§(ÄÄÄÅ•òÅπΩ–Å…îπô’±±µÖ—ç†°»âqëÏ›Ùà∞ÅçπÖ¡Õ}π’à§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâ1îÅ9UÅëΩ•–ÅçΩµ¡Ω…—ï»Åï·Öç—ïµïπ–Ä‹Åç°•ôô…ïÃ∏à(ÄÄÄÄÄÄÄÅÙ§∞Ä–¿¿((ÄÄÄÄåÅ1îÅ9UÅÕÖ•Õ§ÅïÕ–ÅçΩπÕï…€§ÅÖŸÖπ–Å∞ùÖ¡¡ï∞Åë•Õ—Öπ–∞Å∑©µîÅÕ§Å∞ùÖππ’Ö•…îÅïÕ–(ÄÄÄÄåÅµΩµïπ—Öª•µïπ–Å•πë•Õ¡Ωπ•â±î∏Å1îÅŸï……Ω‘Å∏ùïÕ–Å©ÖµÖ•ÃÅùÖ…ì§Å¡ïπëÖπ–Å±î(ÄÄÄÄåÅÀ•ÕïÖ‘ÅÖô•∏ÅëîÅπîÅ¡ÖÃÅâ±Ω≈’ï»Å±ïÃÅÖ’—…ïÃÅÕÖ’ŸïùÖ…ëïÃÅI4∏(ÄÄÄÅ›•—†Å}I5}I=9%1%Q%=9}1=,Ë(ÄÄÄÄÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÄÄÄÄÅ±ÖÕ—}πÖµîÄÙÄàÄàπ©Ω•∏°Õ—»°çΩπ—Öç–πùï–†âπΩ¥à§ÅΩ»Äàà§πÕ—…•¿†§πÕ¡±•–†§§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å±ÖÕ—}πÖµîË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâIïπÕï•ùπïËÅ±îÅπΩ¥ÅëîÅ±ÑÅ¡ï…ÕΩππîÅÖŸÖπ–Å±ÑÅ€•…•ô•çÖ—•Ω∏Å9AL∏à(ÄÄÄÄÄÄÄÄÄÄÄÅÙ§∞Ä–»»(ÄÄÄÄÄÄÄÅ•òÅÕ—»°çΩπ—Öç–πùï–†âçπÖ¡Õ}π’àà§ÅΩ»Äàà§ÄÑÙÅçπÖ¡Õ}π’àË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâçπÖ¡Õ}π’àâtÄÙÅçπÖ¡Õ}π’à(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–π¡Ω¿†âçπÖ¡Õ}çÖ…ë}ŸÖ±•ë•—‰à∞Å9Ωπî§(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§((ÄÄÄÅô…Ω¥Åç…µ}çπÖ¡Õ}—…Öç≠•πúÅ•µ¡Ω…–Åôï—ç°}çπÖ¡Õ}çÖ…ë}ŸÖ±•ë•—‰((ÄÄÄÅ…ïÕ’±–ÄÙÅôï—ç°}çπÖ¡Õ}çÖ…ë}ŸÖ±•ë•—‰°±ÖÕ—}πÖµî∞ÅçπÖ¡Õ}π’à§(ÄÄÄÅ•òÅ…ïÕ’±–πùï–†âç°ïç≠}Õ—Ö—’Ãà§ÄÑÙÄâÕ’ççïÕÃàË(ÄÄÄÄÄÄÄÅÖ¡¿π±Ωùùï»π›Ö…π•πú†(ÄÄÄÄÄÄÄÄÄÄÄÄâ[•…•ô•çÖ—•Ω∏ÅëîÅçÖ…—îÅ9ALÅ•πë•Õ¡Ωπ•â±îÄ†ïÃ§à∞(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕ’±–πùï–†â°——¡}Õ—Ö—’Ãà§ÅΩ»Äâπï—›Ω…¨à∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ1ÑÅ€•…•ô•çÖ—•Ω∏Å9ALÅïÕ–ÅµΩµïπ—Öª•µïπ–Å•πë•Õ¡Ωπ•â±î∏Äà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâK•ïÕÕÖÂïËÅëÖπÃÅ≈’ï±≈’ïÃÅ•πÕ—Öπ—Ã∏à(ÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ïÖÕΩ∏àËÄâçπÖ¡Õ}’πÖŸÖ•±Öâ±îà∞(ÄÄÄÄÄÄÄÅÙ§∞Ä‘¿»(ÄÄÄÅ¡ï…Õ•Õ—ïë}…ïÕ’±–ÄÙÅçΩ¡‰πëïï¡çΩ¡‰°…ïÕ’±–§(ÄÄÄÅ¡ï…Õ•Õ—ïë}…ïÕ’±–π’¡ëÖ—î°Ï(ÄÄÄÄÄÄÄÄâç°ïç≠}Õ—Ö—’ÃàËÄâÕ’ççïÕÃà∞(ÄÄÄÄÄÄÄÄâç°ïç≠ïë}Ö–àËÅÕ—»°…ïÕ’±–πùï–†âç°ïç≠ïë}Ö–à§ÅΩ»Å}ç…µ}πΩ‹†§§∞(ÄÄÄÄÄÄÄÄâπ’ààËÅçπÖ¡Õ}π’à∞(ÄÄÄÅÙ§(ÄÄÄÅ›•—†Å}I5}I=9%1%Q%=9}1=,Ë(ÄÄÄÄÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÄÄÄÄÅç’……ïπ—}±ÖÕ—}πÖµîÄÙÄàÄàπ©Ω•∏†(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†âπΩ¥à§ÅΩ»Äàà§πÕ—…•¿†§πÕ¡±•–†§(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ•òÄ°Õ—»°çΩπ—Öç–πùï–†âçπÖ¡Õ}π’àà§ÅΩ»Äàà§ÄÑÙÅçπÖ¡Õ}π’à(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ»Åç’……ïπ—}±ÖÕ—}πÖµîÄÑÙÅ±ÖÕ—}πÖµî§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ1îÅπΩ¥ÅΩ‘Å±îÅ9UÅÑÉ•”§ÅµΩë•ôß§Å¡ïπëÖπ–Å±ÑÅ€•…•ô•çÖ—•Ω∏∏Äà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâIï±ÖπçïËÅ±ÑÅ€•…•ô•çÖ—•Ω∏∏à(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅÙ§∞Ä–¿‰(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâçπÖ¡Õ}çÖ…ë}ŸÖ±•ë•—‰âtÄÙÅ¡ï…Õ•Õ—ïë}…ïÕ’±–(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°¡ï…Õ•Õ—ïë}…ïÕ’±–§(()ëïòÅ}ç…µ}¡Ö—ç°}çΩπ—Öç—}±Ωç≠ïê°ëÖ—Ñ∞ÅçΩπ—Öç–∞ÅçΩπ—Öç—}•ê§Ë(ÄÄÄÄààâ¡¡±‰ÅΩπîÅçΩπ—Öç–ÅAQ Å›°•±îÅ—°îÅçÖ±±ï»Å°Ω±ëÃÅ—°îÅI4Å›…•—îÅ±Ωç¨∏ààà(ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(ÄÄÄÅïôôïç—•Ÿï}—•—…ï}Õï©Ω’»ÄÙÅ¡ÖÂ±ΩÖêπùï–†(ÄÄÄÄÄÄÄÄâ—•—…ï}Õï©Ω’»à∞ÅçΩπ—Öç–πùï–†â—•—…ï}Õï©Ω’»à§(ÄÄÄÄ§(ÄÄÄÅ•òÅπΩ–Å}ÂïÃ°ïôôïç—•Ÿï}—•—…ï}Õï©Ω’»§Ë(ÄÄÄÄÄÄÄÅ•òÅÕ—»°¡ÖÂ±ΩÖêπùï–†â—•—…ï}Õï©Ω’…}çπÖ¡Ãà§ÅΩ»Äàà§πÕ—…•¿†§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ1ÑÅÕ•—’Ö—•Ω∏Åë‘Å—•—…îÅëîÅœ•©Ω’»ÅπîÅ¡ï’–É©—…îÅ…ïπÕï•ùª•îÄà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ≈’îÅÕ§Å±ÑÅ¡ï…ÕΩππîÅïÕ–Å—•—’±Ö•…îÅìäe’∏Å—•—…îÅëîÅœ•©Ω’»∏à(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅÙ§∞Ä–¿¿(ÄÄÄÄÄÄÄÄåÅÅ¡Ö…—•Ö∞ÅAQ Åç°Öπù•πúÅ—°îÅ°Ω±ëï»ÅÖπÕ›ï»Åµ’Õ–ÅÖ±ÕºÅ¡’…ùîÅÑÅ±ïùÖç‰(ÄÄÄÄÄÄÄÄåÅÖÕÕïÕÕµïπ–ÅΩµ•——ïêÅâ‰Å—°îÅë•ÕÖâ±ïêÅâ…Ω›Õï»ÅçΩπ—…Ω∞∏(ÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëlâ—•—…ï}Õï©Ω’…}çπÖ¡ÃâtÄÙÄàà(ÄÄÄÄåÅ1ÑÅ¡…ΩŸïπÖπçîÅêù’πîÅ¡•Õ—îÅ]=Å…ïÕ—îÅ5Ω∏ÅΩµ¡—îÅΩ…µÖ—•Ω∏Å±Ω…Õ≈’î(ÄÄÄÄåÅ∞ü•≈’•¡îÅçΩ……•ùîÅµÖπ’ï±±ïµïπ–Å±ÑÅôΩ…µÖ—•Ω∏ÅΩ‘Å’∏ÅÖ’—…îÅç°Öµ¿∏(ÄÄÄÅ•òÅçΩπ—Öç–πùï–†âÕΩ’…çîà§ÄÙÙÄâ›ïëΩô}ç¡òàË(ÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëlâΩ…•ù•πîâtÄÙÄâ5Ω∏ÅΩµ¡—îÅΩ…µÖ—•Ω∏à(ÄÄÄÄåÅ0ùÖπç•ï∏Åœ•±ïç—ï’»ÅπîÅ¡…Ω¡ΩÕÖ•–Å¡ÖÃÅ5QÅï–ÅïπŸΩÂÖ•–Å’πîÅŸÖ±ï’»ÅŸ•ëîÅ±Ω…Ã(ÄÄÄÄåÅëîÅç°Ö≈’îÅÕÖ’ŸïùÖ…ëîÅÖ’—ΩµÖ—•≈’î∏Å1ÑÅ¡…ΩŸïπÖπçîÅï–Å±îÅ±•ï‘ÅëîÅçÖµ¡Öùπî(ÄÄÄÄåÅ…ïÕ—ïπ–Å¡…Ω”•ü•Ã∞ÅµÖ•ÃÅ±ÑÅôΩ…µÖ—•Ω∏Å¡ï’–É©—…îÅçΩ……•ü•îÅ¡Ö»Å∞ü•≈’•¡î∏(ÄÄÄÅ•òÅ}ç…µ}•Õ}µï—Ö}çΩπ—Öç–°çΩπ—Öç–§Ë(ÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêπ’¡ëÖ—î°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâΩ…•ù•πîàËÄâ5Qà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ±•ï‘àËÅ}5Q}U1Q}1=Q%=8∞(ÄÄÄÄÄÄÄÅÙ§(ÄÄÄÅ}ç…µ}ïπÕ’…ï}…ï±ÖπçïÃ°çΩπ—Öç–§(ÄÄÄÅ…ï±Öπçï}ëÖ—ï}Õ’¡¡±•ïêÄÙÄâ…ï±Öπçï}ëÖ—îàÅ•∏Å¡ÖÂ±ΩÖê(ÄÄÄÅ…ï≈’ïÕ—ïë}…ï±Öπçï}ëÖ—îÄÙÅ¡ÖÂ±ΩÖêπùï–†â…ï±Öπçï}ëÖ—îà§(ÄÄÄÅ•òÅ…ï±Öπçï}ëÖ—ï}Õ’¡¡±•ïêË(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï≈’ïÕ—ïë}…ï±Öπçï}ëÖ—îÄÙÅ}ç…µ}…ï±Öπçï}ëÖ—î†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï≈’ïÕ—ïë}…ï±Öπçï}ëÖ—î∞Å›ïï≠ëÖÂÕ}Ωπ±‰ıQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÅÕ—»°ï·å•Ù§∞Ä–¿¿(ÄÄÄÅ…ï≈’ïÕ—ïë}…ï±Öπçï}µΩ—•òÄÙÄ†(ÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêπùï–†â…ï±Öπçï}µΩ—•òà§Å•òÄâ…ï±Öπçï}µΩ—•òàÅ•∏Å¡ÖÂ±ΩÖêÅï±ÕîÅ9Ωπî(ÄÄÄÄ§(ÄÄÄÅÖ±±Ω›ïêÄÙÅÏâ¡…ïπΩ¥à∞ÄâπΩ¥à∞Äâ—ï±ï¡°Ωπîà∞ÄâµÖ•∞à∞ÄâëÖ—ïÕ}ôΩ…µÖ—•Ω∏à∞Äâç¡òà∞ÄâçÖ…—ï}¡…ºà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÖπ—ïçïëïπ—Ãà∞ÄâùÖ…ëï}Ÿ’îà∞Äâ—•—…ï}Õï©Ω’»à∞Äâ—•—…ï}Õï©Ω’…}çπÖ¡Ãà∞ÄâçΩµ¡—ï}çπÖ¡Ãà∞ÄâçπÖ¡Õ}π’àà∞ÄâçπÖ¡Õ}’Õï…πÖµîà∞ÄâçπÖ¡Õ}â•…—°}ÂïÖ»à∞ÄâçπÖ¡Õ}¡ÖÕÕ›Ω…êà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ•π—ïù…Ö—•Ωπ}ë…ÖçÖ»à∞ÄâôΩ…µÖ—•Ω∏à∞Äâ±•ï‘à∞ÄâëïÕ¡}—Â¡îà∞Äâ•ëïπ—•—ï}ç…ïÖ—•Ω∏à∞Äâ•ëïπ—•—ï}Ω¨à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâô•πÖπçïµïπ—}ô–à∞ÄâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–à∞ÄâµΩπ—Öπ—}ÖççΩ…ëï}ô–à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâô•πÖπçïµïπ—}¡ï…ÕΩ}¡ΩÕÕ•â±îà∞Äâ…ïô’Õ}ô—}¡ï…Õºà∞Äâ…ïÕ—ï}Ö}ç°Ö…ùï}¡ï…Õºà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâΩ…•ù•πîà∞Äâ•πÕç…•—}ô–à∞ÄâçΩµµïπ—Ö•…ïÃà∞Äâç¡ô}µΩπ—Öπ–à∞Äâç¡ô}¡Ö±•ï»à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö—’–à∞ÄâÕ—Ö—’—}ÕïçΩπëÖ•…îà∞ÄâçΩµµï…ç•Ö∞à∞Äâ—ÖùÃà∞Äâ¡…•·}Ÿïπ—îà∞ÄâçΩ’—}ïÕ—•µîà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ¡…Ωç°Ö•πï}Öç—•Ωπ}µÖπ’ï±±îà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâë•Õ≈’Ö±•ô•çÖ—•Ωπ}…ïÖÕΩ∏à∞Äâë•Õ≈’Ö±•ô•çÖ—•Ωπ}ëï—Ö•∞à∞Äâ…ïÖç—•ŸÖ—•Ωπ}ëÖ—îà∞ÄâÖ…ç°•Ÿïë}Ö–à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ≈’Ö±•ô•çÖ—•Ωπ}ô±ÖúâÙ(ÄÄÄÅ•òÄâ≈’Ö±•ô•çÖ—•Ωπ}ô±ÖúàÅ•∏Å¡ÖÂ±ΩÖêË(ÄÄÄÄÄÄÄÅ≈’Ö±•ô•çÖ—•Ωπ}ô±ÖúÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†â≈’Ö±•ô•çÖ—•Ωπ}ô±Öúà§ÅΩ»Äàà§πÕ—…•¿†§π±Ω›ï»†§(ÄÄÄÄÄÄÄÅ•òÅ≈’Ö±•ô•çÖ—•Ωπ}ô±ÖúÅπΩ–Å•∏ÅÏàà∞Äâù…ïï∏à∞Äâ…ïêâÙË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1ÑÅ≈’Ö±•ô•çÖ—•Ω∏ÅëΩ•–É©—…îÅŸ•ëî∞Åù…ïï∏ÅΩ‘Å…ïê∏âÙ§∞Ä–¿¿(ÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëlâ≈’Ö±•ô•çÖ—•Ωπ}ô±ÖúâtÄÙÅ≈’Ö±•ô•çÖ—•Ωπ}ô±Öú(ÄÄÄÅΩ±ë}Õ—Ö—’ÃÄÙÅçΩπ—Öç–πùï–†âÕ—Ö—’–à§(ÄÄÄÅΩ±ë}ÕïçΩπëÖ…Â}Õ—Ö—’ÃÄÙÅçΩπ—Öç–πùï–†âÕ—Ö—’—}ÕïçΩπëÖ•…îà∞Äàà§(ÄÄÄÅΩ±ë}ô’πë•πù}Õ—Ö—’ÃÄÙÅÕ—»†(ÄÄÄÄÄÄÄÅçΩπ—Öç–πùï–†âÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–à§ÅΩ»Äàà(ÄÄÄÄ§πÕ—…•¿†§(ÄÄÄÅΩ±ë}Ω…•ù•∏ÄÙÅçΩπ—Öç–πùï–†âΩ…•ù•πîà∞Äàà§(ÄÄÄÅΩ±ë}≈’Ö±•ô•çÖ—•Ωπ}ô±ÖúÄÙÅÕ—»°çΩπ—Öç–πùï–†â≈’Ö±•ô•çÖ—•Ωπ}ô±Öúà§ÅΩ»Äàà§(ÄÄÄÅΩ±ë}çΩµµïπ—ÃÄÙÅÕ—»°çΩπ—Öç–πùï–†âçΩµµïπ—Ö•…ïÃà§ÅΩ»Äàà§(ÄÄÄÅΩ±ë}çπÖ¡Õ}•ëïπ—•—‰ÄÙÄ†(ÄÄÄÄÄÄÄÅ}ç…µ}ôΩ…µÖ—}±ÖÕ—}πÖµî°çΩπ—Öç–πùï–†âπΩ¥à§§∞(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†âçπÖ¡Õ}π’àà§ÅΩ»Äàà§∞(ÄÄÄÄ§(ÄÄÄÅÕπÖ¡Õ°Ω–ÄÙÅëÖ—Ñπùï–†âç…µ}çπÖ¡Õ}ÕçΩ…•πù}ÕπÖ¡Õ°Ω—Ãà∞ÅÌÙ§πùï–°Õ—»°çΩπ—Öç—}•ê§§(ÄÄÄÅΩ±ë}ÕçΩ…îÄÙÅçÖ±ç’±Ö—ï}çÖπë•ëÖ—ï}•π—ïù…Ö—•Ωπ}ÕçΩ…î°çΩπ—Öç–∞ÅÕπÖ¡Õ°Ω–§(ÄÄÄÅ•òÄâç¡ô}µΩπ—Öπ–àÅ•∏Å¡ÖÂ±ΩÖêË(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëlâç¡ô}µΩπ—Öπ–âtÄÙÅπΩ…µÖ±•Èï}ç¡ô}ÖµΩ’π–°¡ÖÂ±ΩÖêπùï–†âç¡ô}µΩπ—Öπ–à§§(ÄÄÄÄÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÅÕ—»°ï·å•Ù§∞Ä–¿¿(ÄÄÄÅ•òÄâµΩπ—Öπ—}ÖççΩ…ëï}ô–àÅ•∏Å¡ÖÂ±ΩÖêË(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëlâµΩπ—Öπ—}ÖççΩ…ëï}ô–âtÄÙÅπΩ…µÖ±•Èï}ç¡ô}ÖµΩ’π–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêπùï–†âµΩπ—Öπ—}ÖççΩ…ëï}ô–à§(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâ1îÅµΩπ—Öπ–ÅÖççΩ…ì§Å¡Ö»Å…ÖπçîÅQ…ÖŸÖ•∞ÅëΩ•–É©—…îÅ¡ΩÕ•—•òÅï–ÅçΩµ¡Ω…—ï»ÅÖ‘ÅµÖ·•µ’¥Åëï’‡Åì•ç•µÖ±ïÃ∏à(ÄÄÄÄÄÄÄÄÄÄÄÅÙ§∞Ä–¿¿(ÄÄÄÅ•òÄâç¡ô}¡Ö±•ï»àÅ•∏Å¡ÖÂ±ΩÖêË(ÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëlâç¡ô}¡Ö±•ï»âtÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âç¡ô}¡Ö±•ï»à§ÅΩ»Äàà§πÕ—…•¿†•lËƒ»¡t(ÄÄÄÅ•òÄâçπÖ¡Õ}π’ààÅ•∏Å¡ÖÂ±ΩÖêË(ÄÄÄÄÄÄÄÅçπÖ¡Õ}π’àÄÙÅ…îπÕ’à°»âqÃ¨à∞Äàà∞ÅÕ—»°¡ÖÂ±ΩÖêπùï–†âçπÖ¡Õ}π’àà§ÅΩ»Äàà§§(ÄÄÄÄÄÄÄÅ•òÅçπÖ¡Õ}π’àÅÖπêÄ°πΩ–ÅçπÖ¡Õ}π’àπ•Õë•ù•–†§ÅΩ»Å±ï∏°çπÖ¡Õ}π’à§Ä¯Ä‹§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâ1îÅ9UÅëΩ•–ÅçΩµ¡Ω…—ï»ÅÖ‘ÅµÖ·•µ’¥Ä‹Åç°•ôô…ïÃ∏à(ÄÄÄÄÄÄÄÄÄÄÄÅÙ§∞Ä–¿¿(ÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëlâçπÖ¡Õ}π’àâtÄÙÅçπÖ¡Õ}π’à(ÄÄÄÅ•òÄâçπÖ¡Õ}â•…—°}ÂïÖ»àÅ•∏Å¡ÖÂ±ΩÖêË(ÄÄÄÄÄÄÄÅâ•…—°}ÂïÖ»ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âçπÖ¡Õ}â•…—°}ÂïÖ»à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅç’……ïπ—}ÂïÖ»ÄÙÅëÖ—ï—•µîπëÖ—îπ—ΩëÖ‰†§πÂïÖ»(ÄÄÄÄÄÄÄÅ•òÅâ•…—°}ÂïÖ»ÅÖπêÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅπΩ–Å…îπô’±±µÖ—ç†°»âqëÏƒ∞—Ùà∞Åâ•…—°}ÂïÖ»§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ»Ä°±ï∏°â•…—°}ÂïÖ»§ÄÙÙÄ–(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅπΩ–Äƒ‰¿¿ÄÙÅ•π–°â•…—°}ÂïÖ»§ÄÙÅç’……ïπ—}ÂïÖ»§§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ3äeÖπª•îÅëîÅπÖ•ÕÕÖπçîÅëΩ•–ÅçΩµ¡Ω…—ï»Ä–Åç°•ôô…ïÃÅï–ÅπîÄà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ¡ï’–Å¡ÖÃÉ©—…îÅëÖπÃÅ±îÅô’—’»∏à(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅÙ§∞Ä–¿¿(ÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëlâçπÖ¡Õ}â•…—°}ÂïÖ»âtÄÙÅâ•…—°}ÂïÖ»(ÄÄÄÅôΩ»ÅµΩπïÂ}ô•ï±êÅ•∏Ä†â¡…•·}Ÿïπ—îà∞ÄâçΩ’—}ïÕ—•µîà§Ë(ÄÄÄÄÄÄÄÅ•òÅµΩπïÂ}ô•ï±êÅ•∏Å¡ÖÂ±ΩÖêË(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö›}µΩπï‰ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–°µΩπïÂ}ô•ï±ê§ÅΩ»Äàà§πÕ—…•¿†§π…ï¡±Öçî†àÄà∞Äàà§π…ï¡±Öçî†à∞à∞Äà∏à§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ…Ö›}µΩπï‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅô±ΩÖ–°…Ö›}µΩπï‰§ÄÄ¿Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅYÖ±’ï……Ω»(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1ïÃÅµΩπ—Öπ—ÃÅçΩµµï…ç•Ö’‡ÅëΩ•Ÿïπ–É©—…îÅ¡ΩÕ•—•ôÃ∏âÙ§∞Ä–¿¿(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëmµΩπïÂ}ô•ï±ëtÄÙÅ…Ö›}µΩπï‰(ÄÄÄÅ•òÄâ¡…Ωç°Ö•πï}Öç—•Ωπ}µÖπ’ï±±îàÅ•∏Å¡ÖÂ±ΩÖêË(ÄÄÄÄÄÄÄÅµÖπ’Ö±}πï·—}Öç—•Ω∏ÄÙÅÕ—»†(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêπùï–†â¡…Ωç°Ö•πï}Öç—•Ωπ}µÖπ’ï±±îà§ÅΩ»Äàà(ÄÄÄÄÄÄÄÄ§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅ±ï∏°µÖπ’Ö±}πï·—}Öç—•Ω∏§Ä¯ÄÃ¿¿Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâ1ÑÅ¡…Ωç°Ö•πîÅÖç—•Ω∏ÅµÖπ’ï±±îÅïÕ–Å±•µ•”•îÉÄÄÃ¿¿ÅçÖ…Öç”°…ïÃ∏à(ÄÄÄÄÄÄÄÄÄÄÄÅÙ§∞Ä–¿¿(ÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëlâ¡…Ωç°Ö•πï}Öç—•Ωπ}µÖπ’ï±±îâtÄÙÅµÖπ’Ö±}πï·—}Öç—•Ω∏(ÄÄÄÅΩ±ë}—…Ö•π•πúÄÙÄ†(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†âëïÕ¡}—Â¡îà§ÅΩ»Äàà§∞(ÄÄÄÄ§(ÄÄÄÅΩ±ë}ëïôÖ’±—}ÕÖ±ï}¡…•çîÄÙÅÕ—»°}ç…µ}ëïôÖ’±—}ÕÖ±ï}¡…•çî°çΩπ—Öç–§ÅΩ»Äàà§(ÄÄÄÅΩ±ë}ÕÖ±ï}¡…•çîÄÙÅÕ—»°çΩπ—Öç–πùï–†â¡…•·}Ÿïπ—îà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅôΩ»Å≠ï‰∞ÅŸÖ±’îÅ•∏Å¡ÖÂ±ΩÖêπ•—ïµÃ†§Ë(ÄÄÄÄÄÄÄÅ•òÅ≠ï‰Å•∏ÅÖ±±Ω›ïêË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—m≠ïÂtÄÙÅÕ—»°ŸÖ±’îÅΩ»Äàà§(ÄÄÄÅπï›}—…Ö•π•πúÄÙÄ†(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†âëïÕ¡}—Â¡îà§ÅΩ»Äàà§∞(ÄÄÄÄ§(ÄÄÄÅ•òÅπï›}—…Ö•π•πúÄÑÙÅΩ±ë}—…Ö•π•πúË(ÄÄÄÄÄÄÄÅπï›}ëïôÖ’±—}ÕÖ±ï}¡…•çîÄÙÅ}ç…µ}ëïôÖ’±—}ÕÖ±ï}¡…•çî°çΩπ—Öç–§(ÄÄÄÄÄÄÄÅÕ’âµ•——ïë}ÕÖ±ï}¡…•çîÄÙÅÕ—»†(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêπùï–†â¡…•·}Ÿïπ—îà∞ÅΩ±ë}ÕÖ±ï}¡…•çî§ÅΩ»Äàà(ÄÄÄÄÄÄÄÄ§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÄ°πï›}ëïôÖ’±—}ÕÖ±ï}¡…•çî(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÄ°πΩ–ÅÕ’âµ•——ïë}ÕÖ±ï}¡…•çî(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ»ÅÕ’âµ•——ïë}ÕÖ±ï}¡…•çîÄÙÙÅΩ±ë}ëïôÖ’±—}ÕÖ±ï}¡…•çî§§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâ¡…•·}Ÿïπ—îâtÄÙÅÕ—»°πï›}ëïôÖ’±—}ÕÖ±ï}¡…•çî§(ÄÄÄÅπï›}çΩµµïπ—ÃÄÙÅÕ—»°çΩπ—Öç–πùï–†âçΩµµïπ—Ö•…ïÃà§ÅΩ»Äàà§(ÄÄÄÅ•òÄâçΩµµïπ—Ö•…ïÃàÅ•∏Å¡ÖÂ±ΩÖêÅÖπêÅπï›}çΩµµïπ—ÃÄÑÙÅΩ±ë}çΩµµïπ—ÃË(ÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ’•Ÿ§à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâM’•Ÿ§Åµ•ÃÉÄÅ©Ω’»à∞(ÄÄÄÄÄÄÄÄÄÄÄÅπï›}çΩµµïπ—ÃÅΩ»ÄâΩµµïπ—Ö•…îÅ…ï—•À§Åë‘ÅÕ’•Ÿ§∏à∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅ•òÅ…ï±Öπçï}ëÖ—ï}Õ’¡¡±•ïêË(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ¡±Öππïë}…ï±Öπçî∞Å…ï±Öπçï}ç°ÖπùïêÄÙÅ}ç…µ}Õç°ïë’±ï}…ï±Öπçî†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï≈’ïÕ—ïë}…ï±Öπçï}ëÖ—î∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕΩ’…çîÙâµÖπ’Ö∞à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅµΩ—•òı…ï≈’ïÕ—ïë}…ï±Öπçï}µΩ—•ò∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÅÕ—»°ï·å•Ù§∞Ä–¿¿(ÄÄÄÄÄÄÄÅ•òÅ…ï±Öπçï}ç°ÖπùïêË(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ¡±Öππïë}…ï±ÖπçîË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ…ï±Öπçîà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâIï±ÖπçîÅ¡±Öπ•ôß•îà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄàÉ
‹Äàπ©Ω•∏°ô•±—ï»°9Ωπî∞Ål(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâA…Ωç°Ö•πîÅ…ï±ÖπçîÅ±îÅÌ¡±Öππïë}…ï±ÖπçïlùÕç°ïë’±ïë}ëÖ—îùuÙà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâ5Ω—•òÄËÅÌ¡±Öππïë}…ï±Öπçîπùï–†ùµΩ—•òú•ÙàÅ•òÅ¡±Öππïë}…ï±Öπçîπùï–†âµΩ—•òà§Åï±ÕîÄàà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅt§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞Äâ…ï±Öπçîà∞ÄâIï±ÖπçîÅÖππ’≥•îà§(ÄÄÄÅ•òÅçΩπ—Öç–πùï–†âÕ—Ö—’—}ÕïçΩπëÖ•…îà§ÄÙÙÄâMïÕÕ•Ω∏ÅPàË(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâÕ—Ö—’—}ÕïçΩπëÖ•…îâtÄÙÄâ5Ö…ç£§ÅPà(ÄÄÄÅ•òÄâÕ—Ö—’—}ÕïçΩπëÖ•…îàÅ•∏Å¡ÖÂ±ΩÖêË(ÄÄÄÄÄÄÄÄåÅUπîÉ•—Ö¡îÅç°Ω•Õ•îÅëÖπÃÅ±ÑÅ—•µï±•πîÅïÕ–ÅŸΩ±Ωπ—Ö•…îÄËÅï±±îÅπîÅëΩ•–Å¡±’Ã(ÄÄÄÄÄÄÄÄåÉ©—…îÉ•ç…Öœ•îÅÖ‘Å¡…Ωç°Ö•∏ÅPÅ¡Ö»Å’πîÅŸÖ±ï’»Å]=ÅïπçΩ…îÅï∏ÅçÖç°î∏(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâÕ—Ö—’—}ÕïçΩπëÖ•…ï}ÕΩ’…çîâtÄÙÅI5}59U1}MQQUM}M=UI(ÄÄÄÄÄÄÄÅµÖπ’Ö±}ô’πë•πù}Õ—Ö—’ÃÄÙÅI5}Q}MQQUM}	e}M=9Idπùï–†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–πùï–†âÕ—Ö—’—}ÕïçΩπëÖ•…îà§(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ•òÅµÖπ’Ö±}ô’πë•πù}Õ—Ö—’ÃË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–âtÄÙÅµÖπ’Ö±}ô’πë•πù}Õ—Ö—’Ã(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô—}ÕΩ’…çîâtÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅI5}59U1}MQQUM}M=UI(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÅ•òÄâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–àÅ•∏Å¡ÖÂ±ΩÖêË(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô—}ÕΩ’…çîâtÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅI5}59U1}MQQUM}M=UI(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅÖ’—ΩµÖ—•ç}ÕïçΩπëÖ…‰ÄÙÅI5}Q}M=9Ie}	e}MQQULπùï–†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–πùï–†âÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–à§(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ•òÅÖ’—ΩµÖ—•ç}ÕïçΩπëÖ…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâÕ—Ö—’—}ÕïçΩπëÖ•…îâtÄÙÅÖ’—ΩµÖ—•ç}ÕïçΩπëÖ…‰(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâÕ—Ö—’—}ÕïçΩπëÖ•…ï}ÕΩ’…çîâtÄÙÅI5}59U1}MQQUM}M=UI(ÄÄÄÄÄÄÄÅï±•òÅΩ±ë}ÕïçΩπëÖ…Â}Õ—Ö—’ÃÅ•∏ÅÏâ•πÖπçïµïπ–ÅPÅï∏ÅçΩ’…Ãà∞Äâ•πÖπçïµïπ–ÅPÅ…ïô’œ§âÙË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâÕ—Ö—’—}ÕïçΩπëÖ•…îâtÄÙÄàà(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâÕ—Ö—’—}ÕïçΩπëÖ•…ï}ÕΩ’…çîâtÄÙÅI5}59U1}MQQUM}M=UI(ÄÄÄÅπï›}ô’πë•πù}Õ—Ö—’ÃÄÙÅÕ—»†(ÄÄÄÄÄÄÄÅçΩπ—Öç–πùï–†âÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–à§ÅΩ»Äàà(ÄÄÄÄ§πÕ—…•¿†§(ÄÄÄÅ•òÅπï›}ô’πë•πù}Õ—Ö—’ÃÄÙÙÄâ…ïô’ÕïîàÅÖπêÅΩ±ë}ô’πë•πù}Õ—Ö—’ÃÄÑÙÄâ…ïô’ÕïîàË(ÄÄÄÄÄÄÄÅ}ç…µ}Õïπë}ô—}…ïô’ÕÖ±}µïÕÕÖùïÃ°ëÖ—Ñ∞ÅçΩπ—Öç–§(ÄÄÄÄÄÄÄÅ}ç…µ}Õç°ïë’±ï}ô—}…ïô’ÕÖ±}…ï±Öπçî†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÅÕΩ’…çîÙâµÖπ’Ö±}ô—}…ïô’ÕÖ∞à∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅÕïçΩπëÖ…Â}Õ—Ö—’ÕïÃÄÙÅÏàà∞Ä©I5}M=9Ie}MQQUMMÙ(ÄÄÄÅ•òÅçΩπ—Öç–πùï–†âÕ—Ö—’—}ÕïçΩπëÖ•…îà∞Äàà§ÅπΩ–Å•∏ÅÕïçΩπëÖ…Â}Õ—Ö—’ÕïÃË(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâÕ—Ö—’—}ÕïçΩπëÖ•…îâtÄÙÄàà(ÄÄÄÄåÅUπîÅçΩπô•…µÖ—•Ω∏Å¡Ω…—îÅÕ’»Å’∏ÅµΩπ—Öπ–Åï·Öç–ÄËÅ—Ω’—îÅµΩë•ô•çÖ—•Ω∏ÅëîÅÕïÃ(ÄÄÄÄåÅì•—ï…µ•πÖπ—ÃÅ∞ù•πŸÖ±•ëîÅÖô•∏Å≈‘ùï±±îÅπîÅÕΩ•–Å©ÖµÖ•ÃÅÀ•’—•±•œ•îÅ¡Ω’»Å’∏ÅÖ’—…îÅµΩπ—Öπ–∏(ÄÄÄÅëï—ï…µ•πÖπ—ÃÄÙÅÏ(ÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏à∞ÄâëïÕ¡}—Â¡îà∞Äâç¡òà∞Äâç¡ô}µΩπ—Öπ–à∞Äâç¡ô}¡Ö±•ï»à∞(ÄÄÄÄÄÄÄÄâô•πÖπçïµïπ—}ô–à∞ÄâÕ—Ö—’—}ëïµÖπëï}ô•πÖπçïµïπ—}ô–à∞(ÄÄÄÄÄÄÄÄâµΩπ—Öπ—}ÖççΩ…ëï}ô–à∞Äâô•πÖπçïµïπ—}¡ï…ÕΩ}¡ΩÕÕ•â±îà∞(ÄÄÄÅÙ(ÄÄÄÅ¡…ΩŸ•Õ•ΩπÖ±}ÕçΩ…îÄÙÅçÖ±ç’±Ö—ï}çÖπë•ëÖ—ï}•π—ïù…Ö—•Ωπ}ÕçΩ…î°çΩπ—Öç–∞ÅÕπÖ¡Õ°Ω–§(ÄÄÄÅΩ±ë}ÖµΩ’π–ÄÙÅΩ±ë}ÕçΩ…îπùï–†â¡ï…ÕΩπÖ±}…ïµÖ•πëï…}ÖµΩ’π—}ï’»à§(ÄÄÄÅπï›}ÖµΩ’π–ÄÙÅ¡…ΩŸ•Õ•ΩπÖ±}ÕçΩ…îπùï–†â¡ï…ÕΩπÖ±}…ïµÖ•πëï…}ÖµΩ’π—}ï’»à§(ÄÄÄÅ•òÄ°ëï—ï…µ•πÖπ—Ãπ•π—ï…Õïç—•Ω∏°¡ÖÂ±ΩÖê§(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÄ°Ω±ë}ÖµΩ’π–ÄÑÙÅπï›}ÖµΩ’π–ÅΩ»Å¡…ΩŸ•Õ•ΩπÖ±}ÕçΩ…îπùï–†â¡ï…ÕΩπÖ±}…ïµÖ•πëï…}Ö¡¡±•çÖâ±îà§Å•ÃÅπΩ–ÅQ…’î§§Ë(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâ…ïÕ—ï}Ö}ç°Ö…ùï}¡ï…ÕºâtÄÙÄàà(ÄÄÄÅ}ç…µ}çÖ±ïπë±Â}…ï±•π≠}Ö¡¡Ω•π—µïπ—Ã°ëÖ—Ñ∞ÅçΩπ—Öç–§(ÄÄÄÅ}ç…µ}ÕÂπç}çΩπ—Öç—}çÖ±ïπë±Â}Õ—Ö—’Ã°ëÖ—Ñ∞ÅçΩπ—Öç–§(ÄÄÄÅçΩπ—Öç—lâ¡…ïπΩ¥âtÄÙÅ}ç…µ}ôΩ…µÖ—}ô•…Õ—}πÖµî°çΩπ—Öç–πùï–†â¡…ïπΩ¥à§§(ÄÄÄÅçΩπ—Öç—lâπΩ¥âtÄÙÅ}ç…µ}ôΩ…µÖ—}±ÖÕ—}πÖµî°çΩπ—Öç–πùï–†âπΩ¥à§§(ÄÄÄÅ•òÄ°ÏâπΩ¥à∞ÄâçπÖ¡Õ}π’àâÙπ•π—ï…Õïç—•Ω∏°¡ÖÂ±ΩÖê§(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÄ°çΩπ—Öç–πùï–†âπΩ¥à§∞ÅÕ—»°çΩπ—Öç–πùï–†âçπÖ¡Õ}π’àà§ÅΩ»Äàà§§(ÄÄÄÄÄÄÄÄÄÄÄÄÑÙÅΩ±ë}çπÖ¡Õ}•ëïπ—•—‰§Ë(ÄÄÄÄÄÄÄÅçΩπ—Öç–π¡Ω¿†âçπÖ¡Õ}çÖ…ë}ŸÖ±•ë•—‰à∞Å9Ωπî§(ÄÄÄÅÕ—Ö—’ÕïÃÄÙÅ}ç…µ}Õ—Ö—’ÕïÃ°ëÖ—Ñ§(ÄÄÄÅ•òÅçΩπ—Öç–πùï–†âÕ—Ö—’–à§ÅπΩ–Å•∏ÅÕ—Ö—’ÕïÃË(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâÕ—Ö—’–âtÄÙÅΩ±ë}Õ—Ö—’ÃÅ•òÅΩ±ë}Õ—Ö—’ÃÅ•∏ÅÕ—Ö—’ÕïÃÅï±ÕîÅÕ—Ö—’ÕïÕl¡t(ÄÄÄÅ•òÅçΩπ—Öç–πùï–†âÕ—Ö—’–à§ÄÑÙÅΩ±ë}Õ—Ö—’ÃË(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâÕ—Ö—’Õ}ç°Öπùïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç–πùï–†âÕ—Ö—’–à§ÄÙÙÄâΩπŸï…—§àÅÖπêÅπΩ–ÅçΩπ—Öç–πùï–†âçΩπŸï…—ïë}Ö–à§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâçΩπŸï…—ïë}Ö–âtÄÙÅçΩπ—Öç—lâÕ—Ö—’Õ}ç°Öπùïë}Ö–ât(ÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç–πùï–†âÕ—Ö—’–à§ÄÑÙÄâ•Õ≈’Ö±•ôß§àË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâë•Õ≈’Ö±•ô•çÖ—•Ωπ}…ïÖÕΩ∏âtÄÙÄàà(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—lâë•Õ≈’Ö±•ô•çÖ—•Ωπ}ëï—Ö•∞âtÄÙÄàà(ÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞ÄâÕ—Ö—’–à∞ÅòâM—Ö—’–ÄËÅÌçΩπ—Öç—lùÕ—Ö—’–ùuÙà∞Åòâπç•ï∏ÅÕ—Ö—’–ÄËÅÌΩ±ë}Õ—Ö—’ÕÙà§(ÄÄÄÅ•òÅçΩπ—Öç–πùï–†âΩ…•ù•πîà§ÄÑÙÅΩ±ë}Ω…•ù•∏Ë(ÄÄÄÄÄÄÄÅç°Öπùïë}Ö–ÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÅ•òÅΩ±ë}Ω…•ù•∏Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}…ïçΩ…ë}Ω…•ù•∏†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞ÅΩ±ë}Ω…•ù•∏∞ÅÕΩ’…çîÙâµÖπ’Ö∞à∞ÅëÖ—îıç°Öπùïë}Ö–∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ}ç…µ}…ïçΩ…ë}Ω…•ù•∏†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–πùï–†âΩ…•ù•πîà§ÅΩ»Äâ9Ω∏Å…ïπÕï•ùª•îà∞(ÄÄÄÄÄÄÄÄÄÄÄÅÕΩ’…çîÙâµÖπ’Ö∞à∞(ÄÄÄÄÄÄÄÄÄÄÄÅëÖ—îıç°Öπùïë}Ö–∞(ÄÄÄÄÄÄÄÄÄÄÄÅµÖ≠ï}¡…•µÖ…‰ıQ…’î∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞ÄâΩ…•ù•πîà∞Åòâ=…•ù•πîÄËÅÌçΩπ—Öç–πùï–†ùΩ…•ù•πîú§ÅΩ»Äù9Ω∏Å…ïπÕï•ùª•îùÙà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâπç•ïππîÅΩ…•ù•πîÄËÅÌΩ±ë}Ω…•ù•∏ÅΩ»Äù9Ω∏Å…ïπÕï•ùª•îùÙà§(ÄÄÄÅ•òÅçΩπ—Öç–πùï–†âÕ—Ö—’—}ÕïçΩπëÖ•…îà∞Äàà§ÄÑÙÅΩ±ë}ÕïçΩπëÖ…Â}Õ—Ö—’ÃË(ÄÄÄÄÄÄÄÅÕïçΩπëÖ…Â}±Öâï∞ÄÙÅçΩπ—Öç–πùï–†âÕ—Ö—’—}ÕïçΩπëÖ•…îà§ÅΩ»Äâ…ï—•À§à(ÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞ÄâÕ—Ö—’–à∞Åòâï’·ß°µîÅÕ—Ö—’–ÄËÅÌÕïçΩπëÖ…Â}±Öâï±Ùà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâπç•ï∏Åëï’·ß°µîÅÕ—Ö—’–ÄËÅÌΩ±ë}ÕïçΩπëÖ…Â}Õ—Ö—’ÃÅΩ»ÄùÖ’ç’∏ùÙà§(ÄÄÄÅπï›}≈’Ö±•ô•çÖ—•Ωπ}ô±ÖúÄÙÅÕ—»°çΩπ—Öç–πùï–†â≈’Ö±•ô•çÖ—•Ωπ}ô±Öúà§ÅΩ»Äàà§(ÄÄÄÅ•òÅπï›}≈’Ö±•ô•çÖ—•Ωπ}ô±ÖúÄÑÙÅΩ±ë}≈’Ö±•ô•çÖ—•Ωπ}ô±ÖúË(ÄÄÄÄÄÄÄÅ±Öâï±ÃÄÙÅÏààËÄâ’ç’∏Åô±Öúà∞Äâù…ïï∏àËÄâ…ïï∏Å±Öúà∞Äâ…ïêàËÄâIïêÅ±ÖúâÙ(ÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ≈’Ö±•ô•çÖ—•Ω∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÅòâE’Ö±•ô•çÖ—•Ω∏ÄËÅÌ±Öâï±Õmπï›}≈’Ö±•ô•çÖ—•Ωπ}ô±ÖùuÙà∞(ÄÄÄÄÄÄÄÄÄÄÄÅòâπç•ïππîÅ≈’Ö±•ô•çÖ—•Ω∏ÄËÅÌ±Öâï±Ãπùï–°Ω±ë}≈’Ö±•ô•çÖ—•Ωπ}ô±Öú∞Äù’ç’∏Åô±Öúú•Ùà∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅπï›}ÕçΩ…îÄÙÅçÖ±ç’±Ö—ï}çÖπë•ëÖ—ï}•π—ïù…Ö—•Ωπ}ÕçΩ…î°çΩπ—Öç–∞ÅÕπÖ¡Õ°Ω–§(ÄÄÄÅ•òÅΩ±ë}ÕçΩ…îπùï–†â±ïŸï∞à§ÅÖπêÅπï›}ÕçΩ…îπùï–†â±ïŸï∞à§ÄÑÙÅΩ±ë}ÕçΩ…îπùï–†â±ïŸï∞à§Ë(ÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞ÄâÕçΩ…îà∞ÅòâMçΩ…îÅìäe•π”•ù…Ö—•Ω∏Å¡ÖÕœ§ÅëîÅÌΩ±ë}ÕçΩ…ïlùÕçΩ…îùuÙÉÄÅÌπï›}ÕçΩ…ïlùÕçΩ…îùuÙÄËÅÌπï›}ÕçΩ…ïlù±Öâï∞ùuÙà§(ÄÄÄÅ•òÅΩ±ë}ÕçΩ…îπùï–†âΩ¡ï…Ö—•ΩπÖ±}Õ—Ö—’Ãà§ÄÑÙÅπï›}ÕçΩ…îπùï–†âΩ¡ï…Ö—•ΩπÖ±}Õ—Ö—’Ãà§Ë(ÄÄÄÄÄÄÄÅ•òÅπï›}ÕçΩ…îπùï–†âΩ¡ï…Ö—•ΩπÖ±}Õ—Ö—’Ãà§ÄÙÙÄââ±Ωç≠ïêàË(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞ÄâÕçΩ…îà∞Äâ1îÅëΩÕÕ•ï»Å¡À•Õïπ—îÅµÖ•π—ïπÖπ–Å’∏Åâ±ΩçÖùîÅëîÅô•πÖπçïµïπ–à§(ÄÄÄÄÄÄÄÅï±•òÅΩ±ë}ÕçΩ…îπùï–†âΩ¡ï…Ö—•ΩπÖ±}Õ—Ö—’Ãà§ÄÙÙÄââ±Ωç≠ïêàÅÖπêÅπï›}ÕçΩ…îπùï–†âΩ¡ï…Ö—•ΩπÖ±}Õ—Ö—’Ãà§ÄÙÙÄâ…ïÖë‰àË(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞ÄâÕçΩ…îà∞Äâ1îÅâ±ΩçÖùîÅëîÅô•πÖπçïµïπ–ÅÑÉ•”§Å±ï€§à§(ÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°}ç…µ}çΩπ—Öç—}ëï—Ö•±}…ïÕ¡ΩπÕî°çΩπ—Öç–∞ÅëÖ—Ñ§§(()Ö¡¿π¡Ö—ç††àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯ΩÖç—•Ÿ•—•ïÃºÒÖç—•Ÿ•—Â}•ê¯à§)±Ωù•π}…ï≈’•…ïê)}ç…µ}Õï…•Ö±•Èïê)ëïòÅç…µ}ïë•—}çÖ±±}Öç—•Ÿ•—‰°çΩπ—Öç—}•ê∞ÅÖç—•Ÿ•—Â}•ê§Ë(ÄÄÄÄààâë•–ÅΩπîÅ±ΩùùïêÅçÖ±∞Å›•—°Ω’–Å…ï¡±ÖÂ•πúÅÖ¡¡Ω•π—µïπ–ÅΩ»Å…ï±ÖπçîÅÖ’—ΩµÖ—•ΩπÃ∏ààà(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅÖç—•Ÿ•—‰ÄÙÅπï·–†(ÄÄÄÄÄÄÄÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅçΩπ—Öç–πùï–†âÖç—•Ÿ•—•ïÃà∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°•—ï¥∞Åë•ç–§ÅÖπêÅÕ—»°•—ï¥πùï–†â•êà§§ÄÙÙÅÖç—•Ÿ•—Â}•ê(ÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÅ9Ωπî∞(ÄÄÄÄ§(ÄÄÄÅ•òÅπΩ–ÅÖç—•Ÿ•—‰Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâç—•Ÿ•”§Å•π—…Ω’ŸÖâ±îÅ¡Ω’»ÅçîÅçΩπ—Öç–âÙ§∞Ä–¿–(ÄÄÄÅ•òÅÖç—•Ÿ•—‰πùï–†â≠•πêà§ÄÑÙÄâÖ¡¡ï∞àË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâMï’±ÃÅ±ïÃÅÖ¡¡ï±ÃÅçΩπÕ•ùª•ÃÅ¡ï’Ÿïπ–É©—…îÅµΩë•ôß•ÃâÙ§∞Ä–¿‰((ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§(ÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°¡ÖÂ±ΩÖê∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1îÅçΩ…¡ÃÅ)M=8ÅëΩ•–É©—…îÅ’∏ÅΩâ©ï–âÙ§∞Ä–¿¿(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅç°Öπùïê∞Å|ÄÙÅ}ç…µ}ïë•—}çÖ±±}Öç—•Ÿ•—‰°Öç—•Ÿ•—‰∞Å¡ÖÂ±ΩÖêπùï–†âçΩµµïπ—Ö•…îà§§(ÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÅÕ—»°ï·å•Ù§∞Ä–¿¿(ÄÄÄÅ•òÅç°ÖπùïêË(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄâçΩπ—Öç–àËÅ}ç…µ}çΩπ—Öç—}…ïÕ¡ΩπÕî°çΩπ—Öç–∞ÅëÖ—Ñ§∞(ÄÄÄÄÄÄÄÄâÖç—•Ÿ•—‰àËÅÖç—•Ÿ•—‰∞(ÄÄÄÄÄÄÄÄâç°ÖπùïêàËÅç°Öπùïê∞(ÄÄÄÅÙ§(()Ö¡¿πùï–†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯ΩÖç—•Ÿ•—•ïÃºÒÖç—•Ÿ•—Â}•ê¯Ω¡…ïŸ•ï‹à§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çΩπ—Öç—}Öç—•Ÿ•—Â}¡…ïŸ•ï‹°çΩπ—Öç—}•ê∞ÅÖç—•Ÿ•—Â}•ê§Ë(ÄÄÄÄààâ1ΩÖêÅÑÅ¡Ω—ïπ—•Ö±±‰Å±Ö…ùîÅîµµÖ•∞ΩM5LÅâΩë‰ÅΩπ±‰Å›°ï∏Å—°îÅ’Õï»ÅΩ¡ïπÃÅ•–∏ààà(ÄÄÄÅëÖ—ÑÄÙÅ}±ΩÖë}ëÖ—Ö}ÕπÖ¡Õ°Ω–†§(ÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅÖç—•Ÿ•—‰ÄÙÅπï·–†(ÄÄÄÄÄÄÄÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅçΩπ—Öç–πùï–†âÖç—•Ÿ•—•ïÃà∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°•—ï¥∞Åë•ç–§ÅÖπêÅÕ—»°•—ï¥πùï–†â•êà§§ÄÙÙÅÖç—•Ÿ•—Â}•ê(ÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÅ9Ωπî∞(ÄÄÄÄ§(ÄÄÄÅ•òÅπΩ–ÅÖç—•Ÿ•—‰ÅΩ»ÅπΩ–ÅÖç—•Ÿ•—‰πùï–†â¡…ïŸ•ï‹à§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ¡ïÀù‘Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâ¡…ïŸ•ï‹àËÅÖç—•Ÿ•—Âlâ¡…ïŸ•ï‹ât∞Äâ≠•πêàËÅÖç—•Ÿ•—‰πùï–†â≠•πêà∞Äàà•Ù§(()Ö¡¿π¡ΩÕ–†àΩÖ¡§Ωç…¥ΩçÖπë•ëÖ—îµÕçΩ…îΩ¡…ïŸ•ï‹à§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çÖπë•ëÖ—ï}ÕçΩ…ï}¡…ïŸ•ï‹†§Ë(ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(ÄÄÄÅ•òÄâç¡ô}µΩπ—Öπ–àÅ•∏Å¡ÖÂ±ΩÖêË(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëlâç¡ô}µΩπ—Öπ–âtÄÙÅπΩ…µÖ±•Èï}ç¡ô}ÖµΩ’π–°¡ÖÂ±ΩÖêπùï–†âç¡ô}µΩπ—Öπ–à§§(ÄÄÄÄÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÅÕ—»°ï·å•Ù§∞Ä–¿¿(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°çÖ±ç’±Ö—ï}çÖπë•ëÖ—ï}•π—ïù…Ö—•Ωπ}ÕçΩ…î°¡ÖÂ±ΩÖê§§((()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯ΩÖ¡¡ï∞à∞Åµï—°ΩëÃılâA=MPât§)±Ωù•π}…ï≈’•…ïê)}ç…µ}Õï…•Ö±•Èïê)ëïòÅç…µ}±Ωù}çÖ±∞°çΩπ—Öç—}•ê§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§ÏÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–ËÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(ÄÄÄÅπΩ—îÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âçΩµµïπ—Ö•…îà∞Äàà§§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–ÅπΩ—îËÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâU∏ÅçΩµµïπ—Ö•…îÅïÕ–Å…ï≈’•ÃâÙ§∞Ä–¿¿(ÄÄÄÅ}ç…µ}ïπÕ’…ï}…ï±ÖπçïÃ°çΩπ—Öç–§(ÄÄÄÅ…ï±ÖπçîÄÙÅ9Ωπî(ÄÄÄÅ…ï±Öπçï}•êÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†â…ï±Öπçï}•êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅ…ï±Öπçï}•êË(ÄÄÄÄÄÄÄÅ…ï±ÖπçîÄÙÅπï·–†(ÄÄÄÄÄÄÄÄÄÄÄÄ°•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅçΩπ—Öç–πùï–†â…ï±ÖπçïÃà∞Åmt§Å•òÅ•—ï¥πùï–†â•êà§ÄÙÙÅ…ï±Öπçï}•ê§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ9Ωπî∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å…ï±ÖπçîË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâIï±ÖπçîÅ•π—…Ω’ŸÖâ±îÅ¡Ω’»ÅçîÅçΩπ—Öç–âÙ§∞Ä–¿–(ÄÄÄÄÄÄÄÅ•òÅ…ï±Öπçîπùï–†âÕ—Ö—’Ãà§ÄÑÙÄâÕç°ïë’±ïêàË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—Öç–àËÅ}ç…µ}çΩπ—Öç—}…ïÕ¡ΩπÕî°çΩπ—Öç–∞ÅëÖ—Ñ§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ…ï±ÖπçîàËÅ…ï±Öπçî∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâë’¡±•çÖ—îàËÅQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÅÙ§(ÄÄÄÅÖ¡¡Ω•π—µïπ–ÄÙÅ9Ωπî(ÄÄÄÅÖ¡¡Ω•π—µïπ—}•êÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âÖ¡¡Ω•π—µïπ—}•êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅÖ¡¡Ω•π—µïπ—}•êË(ÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ–ÄÙÅπï·–†(ÄÄÄÄÄÄÄÄÄÄÄÄ°•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅëÖ—Ñπùï–†âç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Ãà∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ•—ï¥πùï–†â•êà§ÄÙÙÅÖ¡¡Ω•π—µïπ—}•êÅÖπêÅ•—ï¥πùï–†âçΩπ—Öç—}•êà§ÄÙÙÅçΩπ—Öç—}•ê§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ9Ωπî∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅÖ¡¡Ω•π—µïπ–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâIïπëïËµŸΩ’ÃÅ•π—…Ω’ŸÖâ±îÅ¡Ω’»ÅçîÅçΩπ—Öç–âÙ§∞Ä–¿–(ÄÄÄÅπΩ‹ÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅπΩ‹(ÄÄÄÅëï±•Ÿï…‰ÄÙÅ9Ωπî(ÄÄÄÅ•òÅ…ï±ÖπçîË(ÄÄÄÄÄÄÄÅ}ç…µ}çΩµ¡±ï—ï}…ï±Öπçî°çΩπ—Öç–∞Å…ï±Öπçî∞ÄâÖπÕ›ï…ïêà∞ÅπΩ—îıπΩ—î§(ÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ï±Öπçîà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâIï±ÖπçîÅ—…Ö•”•îÉäPÅÑÅÀ•¡Ωπë‘à∞(ÄÄÄÄÄÄÄÄÄÄÄÅòâ¡¡ï∞ÅçΩπÕ•ùª§ÄËÅÌπΩ—ïÙà∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅ•òÅÖ¡¡Ω•π—µïπ–Ë(ÄÄÄÄÄÄÄÅ•òÅÖ¡¡Ω•π—µïπ–πùï–†â…ïÕ¡ΩπÕï}Õ—Ö—’Ãà§ÄÑÙÄâÖπÕ›ï…ïêàË(ÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—lâ…ïÕ¡ΩπÕï}Õ—Ö—’ÃâtÄÙÄâÖπÕ›ï…ïêà(ÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—lâ…ïÕ¡ΩπÕï}Õ—Ö—’Õ}’¡ëÖ—ïë}Ö–âtÄÙÅπΩ‹(ÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—lâ’¡ëÖ—ïë}Ö–âtÄÙÅπΩ‹(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅÖ¡¡Ω•π—µïπ–πùï–†âÖπÕ›ï…ïë}ôΩ±±Ω›’¡}Õïπ—}Ö–à§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—lâÖπÕ›ï…ïë}ôΩ±±Ω›’¡}Õïπ—}Ö–âtÄÙÅπΩ‹(ÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¡Ω•π—µïπ—lâ’¡ëÖ—ïë}Ö–âtÄÙÅπΩ‹(ÄÄÄÄÄÄÄÄÄÄÄÅëï±•Ÿï…‰ÄÙÅ}ç…µ}Õïπë}Ö¡¡Ω•π—µïπ—}ôΩ±±Ω›’¿°ëÖ—Ñ∞ÅçΩπ—Öç–∞ÄâM’•—îÅÖ¡¡ï∞ÅÀ•¡Ωπë‘à§(ÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞ÄâÖ¡¡ï∞à∞Äâ¡¡ï∞ÅçΩπÕ•ùª§à∞ÅπΩ—î§(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ•òÅπΩ–ÅÖ¡¡Ω•π—µïπ–ÅÖπêÅπΩ–Å…ï±ÖπçîË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°}ç…µ}çΩπ—Öç—}…ïÕ¡ΩπÕî°çΩπ—Öç–∞ÅëÖ—Ñ§§(ÄÄÄÅ…ïÕ’±–ÄÙÅÏâçΩπ—Öç–àËÅ}ç…µ}çΩπ—Öç—}…ïÕ¡ΩπÕî°çΩπ—Öç–∞ÅëÖ—Ñ•Ù(ÄÄÄÅ•òÅÖ¡¡Ω•π—µïπ–Ë(ÄÄÄÄÄÄÄÅ…ïÕ’±—lâÖ¡¡Ω•π—µïπ–âtÄÙÅÖ¡¡Ω•π—µïπ–(ÄÄÄÅ•òÅ…ï±ÖπçîË(ÄÄÄÄÄÄÄÅ…ïÕ’±—lâ…ï±ÖπçîâtÄÙÅ…ï±Öπçî(ÄÄÄÅ•òÅëï±•Ÿï…‰Å•ÃÅπΩ–Å9ΩπîË(ÄÄÄÄÄÄÄÅ…ïÕ’±—lâëï±•Ÿï…‰âtÄÙÅëï±•Ÿï…‰(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°…ïÕ’±–§(()Ö¡¿πëï±ï—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯Ω…ï±ÖπçïÃºÒ…ï±Öπçï}•ê¯à§)±Ωù•π}…ï≈’•…ïê)}ç…µ}Õï…•Ö±•Èïê)ëïòÅç…µ}ëï±ï—ï}…ï±Öπçî°çΩπ—Öç—}•ê∞Å…ï±Öπçï}•ê§Ë(ÄÄÄÄààâAï…µÖπïπ—±‰Åëï±ï—îÅï·Öç—±‰ÅΩπîÅ¡±ÖππïêÅ…ï±ÖπçîÅôΩ»Å—°•ÃÅçΩπ—Öç–∏ààà(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–((ÄÄÄÅ}ç…µ}ïπÕ’…ï}…ï±ÖπçïÃ°çΩπ—Öç–§(ÄÄÄÅ…ï±ÖπçîÄÙÅπï·–†(ÄÄÄÄÄÄÄÄ°•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅçΩπ—Öç–πùï–†â…ï±ÖπçïÃà∞Åmt§Å•òÅ•—ï¥πùï–†â•êà§ÄÙÙÅ…ï±Öπçï}•ê§∞(ÄÄÄÄÄÄÄÅ9Ωπî∞(ÄÄÄÄ§(ÄÄÄÅ•òÅπΩ–Å…ï±ÖπçîË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâIï±ÖπçîÅ•π—…Ω’ŸÖâ±îÅ¡Ω’»ÅçîÅçΩπ—Öç–âÙ§∞Ä–¿–(ÄÄÄÅ•òÅ…ï±Öπçîπùï–†âÕ—Ö—’Ãà§ÄÑÙÄâÕç°ïë’±ïêàË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâMï’±îÅ’πîÅ…ï±ÖπçîÅ¡±Öπ•ôß•îÅ¡ï’–É©—…îÅÕ’¡¡…•∑•îâÙ§∞Ä–¿‰((ÄÄÄÅ}ç…µ}ëï±ï—ï}…ï±Öπçî°çΩπ—Öç–∞Å…ï±Öπçî§(ÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄâçΩπ—Öç–àËÅ}ç…µ}çΩπ—Öç—}…ïÕ¡ΩπÕî°çΩπ—Öç–∞ÅëÖ—Ñ§∞(ÄÄÄÄÄÄÄÄâëï±ï—ïë}…ï±Öπçï}•êàËÅ…ï±Öπçï}•ê∞(ÄÄÄÅÙ§(()Ö¡¿π¡ΩÕ–†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯Ω…ï±ÖπçïÃºÒ…ï±Öπçï}•ê¯ΩÕÖπÃµ…ï¡ΩπÕîà§)±Ωù•π}…ï≈’•…ïê)}ç…µ}Õï…•Ö±•Èïê)ëïòÅç…µ}…ï±Öπçï}πΩ}ÖπÕ›ï»°çΩπ—Öç—}•ê∞Å…ï±Öπçï}•ê§Ë(ÄÄÄÄààâ±ΩÕîÅΩπîÅ…ï±Öπçî∞ÅÕç°ïë’±îÅ—°îÅπï·–ÅΩπîÅÖπêÅÕïπêÅâΩ—†ÅπÖµïêÅ—ïµ¡±Ö—ïÃÅΩπçî∏ààà(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅ}ç…µ}ïπÕ’…ï}…ï±ÖπçïÃ°çΩπ—Öç–§(ÄÄÄÅ…ï±ÖπçîÄÙÅπï·–†(ÄÄÄÄÄÄÄÄ°•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅçΩπ—Öç–πùï–†â…ï±ÖπçïÃà∞Åmt§Å•òÅ•—ï¥πùï–†â•êà§ÄÙÙÅ…ï±Öπçï}•ê§∞(ÄÄÄÄÄÄÄÅ9Ωπî∞(ÄÄÄÄ§(ÄÄÄÅ•òÅπΩ–Å…ï±ÖπçîË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâIï±ÖπçîÅ•π—…Ω’ŸÖâ±îÅ¡Ω’»ÅçîÅçΩπ—Öç–âÙ§∞Ä–¿–((ÄÄÄÅ•òÅ…ï±Öπçîπùï–†âÕ—Ö—’Ãà§ÄÑÙÄâÕç°ïë’±ïêàË(ÄÄÄÄÄÄÄÅπï·—}…ï±ÖπçîÄÙÅπï·–†(ÄÄÄÄÄÄÄÄÄÄÄÄ°•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅçΩπ—Öç–πùï–†â…ï±ÖπçïÃà∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ•—ï¥πùï–†â¡Ö…ïπ—}…ï±Öπçï}•êà§ÄÙÙÅ…ï±Öπçï}•êÅÖπêÅ•—ï¥πùï–†âÕ—Ö—’Ãà§ÄÙÙÄâÕç°ïë’±ïêà§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ9Ωπî∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—Öç–àËÅ}ç…µ}çΩπ—Öç—}…ïÕ¡ΩπÕî°çΩπ—Öç–∞ÅëÖ—Ñ§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ï±ÖπçîàËÅ…ï±Öπçî∞(ÄÄÄÄÄÄÄÄÄÄÄÄâπï·—}…ï±ÖπçîàËÅπï·—}…ï±Öπçî∞(ÄÄÄÄÄÄÄÄÄÄÄÄâëï±•Ÿï…‰àËÅ…ï±Öπçîπùï–†âëï±•Ÿï…‰à§ÅΩ»ÅÏâÕµÃàËÅÖ±Õî∞ÄâïµÖ•∞àËÅÖ±ÕïÙ∞(ÄÄÄÄÄÄÄÄÄÄÄÄâë’¡±•çÖ—îàËÅQ…’î∞(ÄÄÄÄÄÄÄÅÙ§((ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅπï·—}ëÖ—îÄÙÅ}ç…µ}…ï±Öπçï}ëÖ—î†(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêπùï–†âπï·—}ëÖ—îà§∞Å›ïï≠ëÖÂÕ}Ωπ±‰ıQ…’î∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅï·çï¡–ÅYÖ±’ï……Ω»ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÅÕ—»°ï·å•Ù§∞Ä–¿¿(ÄÄÄÅ•òÅπΩ–Åπï·—}ëÖ—îË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ°Ω•Õ•ÕÕïËÅ±ÑÅëÖ—îÅëîÅ±ÑÅ¡…Ωç°Ö•πîÅ…ï±Öπçî∏âÙ§∞Ä–¿¿((ÄÄÄÅ}ç…µ}çΩµ¡±ï—ï}…ï±Öπçî°çΩπ—Öç–∞Å…ï±Öπçî∞ÄâπΩ}ÖπÕ›ï»à§(ÄÄÄÅπï·—}…ï±Öπçî∞Å|ÄÙÅ}ç…µ}Õç°ïë’±ï}…ï±Öπçî†(ÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÅπï·—}ëÖ—î∞(ÄÄÄÄÄÄÄÅÕΩ’…çîÙâπΩ}ÖπÕ›ï»à∞(ÄÄÄÄÄÄÄÅ¡Ö…ïπ—}…ï±Öπçï}•êı…ï±Öπçï}•ê∞(ÄÄÄÄÄÄÄÅµΩ—•òı…ï±Öπçîπùï–†âµΩ—•òà§ÅΩ»ÄâM’•—îÅ…ï±ÖπçîÅÕÖπÃÅÀ•¡ΩπÕîà∞(ÄÄÄÄ§(ÄÄÄÅΩ±ë}Õ—Ö—’ÃÄÙÅçΩπ—Öç–πùï–†âÕ—Ö—’–à§(ÄÄÄÅçΩπ—Öç—lâÕ—Ö—’–âtÄÙÄâÅ…ï±Öπçï»à(ÄÄÄÅ•òÅΩ±ë}Õ—Ö—’ÃÄÑÙÄâÅ…ï±Öπçï»àË(ÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞ÄâÕ—Ö—’–à∞ÄâM—Ö—’–ÄËÅÅ…ï±Öπçï»à∞Åòâπç•ï∏ÅÕ—Ö—’–ÄËÅÌΩ±ë}Õ—Ö—’ÕÙà§(ÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰†(ÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄâ…ï±Öπçîà∞(ÄÄÄÄÄÄÄÄâIï±ÖπçîÅÕÖπÃÅÀ•¡ΩπÕîà∞(ÄÄÄÄÄÄÄÅòâ9Ω’Ÿï±±îÅ…ï±ÖπçîÅ¡…Ωù…Öµ∑•îÅ±îÅÌπï·—}ëÖ—ïÙà∞(ÄÄÄÄ§((ÄÄÄÅëï±•Ÿï…‰ÄÙÅ}ç…µ}Õïπë}Ö¡¡Ω•π—µïπ—}ôΩ±±Ω›’¿°ëÖ—Ñ∞ÅçΩπ—Öç–∞ÄâAÖÃÅëîÅÀ•¡ΩπÕîÅ…ï±Öπçîà§(ÄÄÄÅ…ï±Öπçîπ’¡ëÖ—î°Ï(ÄÄÄÄÄÄÄÄâµïÕÕÖùï}—ïµ¡±Ö—îàËÄâAÖÃÅëîÅÀ•¡ΩπÕîÅ…ï±Öπçîà∞(ÄÄÄÄÄÄÄÄâëï±•Ÿï…‰àËÅëï±•Ÿï…‰∞(ÄÄÄÄÄÄÄÄâµïÕÕÖùïÕ}¡…ΩçïÕÕïë}Ö–àËÅ}ç…µ}πΩ‹†§∞(ÄÄÄÅÙ§(ÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄâçΩπ—Öç–àËÅ}ç…µ}çΩπ—Öç—}…ïÕ¡ΩπÕî°çΩπ—Öç–∞ÅëÖ—Ñ§∞(ÄÄÄÄÄÄÄÄâ…ï±ÖπçîàËÅ…ï±Öπçî∞(ÄÄÄÄÄÄÄÄâπï·—}…ï±ÖπçîàËÅπï·—}…ï±Öπçî∞(ÄÄÄÄÄÄÄÄâëï±•Ÿï…‰àËÅëï±•Ÿï…‰∞(ÄÄÄÄÄÄÄÄâë’¡±•çÖ—îàËÅÖ±Õî∞(ÄÄÄÅÙ§(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯Ω¡’â±•çÖ—•ΩπÃà∞Åµï—°ΩëÃılâA=MPât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}¡’â±•Õ°}çΩπ—Öç—}’¡ëÖ—î°çΩπ—Öç—}•ê§Ë(ÄÄÄÄààâA’â±•îÅ’πîÅπΩ—îÅ°Ω…ΩëÖ”•îÅï–ÅÕ•ùª•îÅëÖπÃÅ±îÅô•∞ÅêùÖç—’Ö±•”§ÅëîÅ±ÑÅ¡•Õ—î∏ààà(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§ÏÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅ—ï·–ÄÙÅÕ—»†°…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ§πùï–†â—ï·—îà∞Äàà§§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–Å—ï·–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1îÅ—ï·—îÅëîÅ±ÑÅ¡’â±•çÖ—•Ω∏ÅïÕ–Å…ï≈’•ÃâÙ§∞Ä–¿¿(ÄÄÄÅ¡’â±•çÖ—•Ω∏ÄÙÅÏ(ÄÄÄÄÄÄÄÄâ•êàËÅÕ—»°’’•êπ’’•ê–†§§∞ÄâëÖ—îàËÅ}ç…µ}πΩ‹†§∞Äâ—ï·—îàËÅ—ï·–∞(ÄÄÄÄÄÄÄÄâÖ’—°Ω»àËÄ°ç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ§πùï–†âπÖµîà∞Äã%≈’•¡îÅ%π”•ù…Ö±îà§∞(ÄÄÄÄÄÄÄÄâÖ’—°Ω…}ïµÖ•∞àËÄ°ç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ§πùï–†âïµÖ•∞à∞Äàà§∞(ÄÄÄÄÄÄÄÄâ±•≠ïÃàËÅmt∞ÄâçΩµµïπ—ÃàËÅmt∞(ÄÄÄÅÙ(ÄÄÄÅçΩπ—Öç–πÕï—ëïôÖ’±–†â¡’â±•çÖ—•ΩπÃà∞Åmt§π•πÕï…–†¿∞Å¡’â±•çÖ—•Ω∏§(ÄÄÄÅ}ç…µ}Öëë}µïπ—•Ωπ}πΩ—•ô•çÖ—•ΩπÃ°ëÖ—Ñ∞Å—ï·–∞ÅçΩπ—Öç–∞Å¡’â±•çÖ—•Ω∏§(ÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâ¡’â±•çÖ—•Ω∏àËÅ¡’â±•çÖ—•Ω∏∞ÄâçΩπ—Öç–àËÅçΩπ—Öç—Ù§∞Ä»¿ƒ(()ëïòÅ}ç…µ}¡’â±•çÖ—•Ω∏°çΩπ—Öç–∞Å¡’â±•çÖ—•Ωπ}•ê§Ë(ÄÄÄÅ…ï—’…∏Åπï·–†°•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅçΩπ—Öç–πùï–†â¡’â±•çÖ—•ΩπÃà∞Åmt§Å•òÅ•—ï¥πùï–†â•êà§ÄÙÙÅ¡’â±•çÖ—•Ωπ}•ê§∞Å9Ωπî§(()ëïòÅ}ç…µ}Ω›πÕ}çΩπ—ïπ–°•—ï¥§Ë(ÄÄÄÅ’Õï»ÄÙÅç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ(ÄÄÄÅ…ï—’…∏Å•—ï¥πùï–†âÖ’—°Ω…}ïµÖ•∞à§ÄÙÙÅ’Õï»πùï–†âïµÖ•∞à§ÅΩ»Ä°πΩ–Å•—ï¥πùï–†âÖ’—°Ω…}ïµÖ•∞à§ÅÖπêÅ•—ï¥πùï–†âÖ’—°Ω»à§ÄÙÙÅ’Õï»πùï–†âπÖµîà§§(()ëïòÅ}ç…µ}Öëë}µïπ—•Ωπ}πΩ—•ô•çÖ—•ΩπÃ°ëÖ—Ñ∞Å—ï·–∞ÅçΩπ—Öç–∞Å¡’â±•çÖ—•Ω∏∞Ä®∞Å≠•πêÙâµïπ—•Ω∏à§Ë(ÄÄÄÄààâÀ•îÅ’πîÅπΩ—•ô•çÖ—•Ω∏Å¡…•€•îÅ¡Ω’»Åç°Ö≈’îÅ¡À•πΩ¥ÅçΩππ‘∏ààà(ÄÄÄÅÖ’—°Ω»ÄÙÅç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ(ÄÄÄÅπΩ…µÖ±•ÈîÄÙÅ±ÖµâëÑÅŸÖ±’îËÄààπ©Ω•∏°ç°Ö»ÅôΩ»Åç°Ö»Å•∏Å’π•çΩëïëÖ—ÑππΩ…µÖ±•Èî†â9à∞ÅŸÖ±’îπçÖÕïôΩ±ê†§§Å•òÅπΩ–Å’π•çΩëïëÖ—ÑπçΩµâ•π•πú°ç°Ö»§§(ÄÄÄÅÖ±•ÖÕïÃÄÙÅÌπΩ…µÖ±•Èî°’Õï…lâô•…Õ—}πÖµîât§ËÅ’Õï»ÅôΩ»Å’Õï»Å•∏ÅUMILπŸÖ±’ïÃ†•Ù(ÄÄÄÅµïπ—•ΩπïêÄÙÅÌπΩ…µÖ±•Èî°ŸÖ±’î§ÅôΩ»ÅŸÖ±’îÅ•∏Å…îπô•πëÖ±∞°»â °mqﬂ ∑¸úµt¨§à∞Å—ï·–ÅΩ»Äàà∞Å…îπU9%=•Ù(ÄÄÄÅôΩ»ÅÖ±•ÖÃÅ•∏Åµïπ—•ΩπïêË(ÄÄÄÄÄÄÄÅ…ïç•¡•ïπ–ÄÙÅÖ±•ÖÕïÃπùï–°Ö±•ÖÃ§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å…ïç•¡•ïπ–ÅΩ»Å…ïç•¡•ïπ—lâïµÖ•∞âtÄÙÙÅÖ’—°Ω»πùï–†âïµÖ•∞à§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅëÖ—ÑπÕï—ëïôÖ’±–†âç…µ}πΩ—•ô•çÖ—•ΩπÃà∞Åmt§π•πÕï…–†¿∞ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ•êàËÅÕ—»°’’•êπ’’•ê–†§§∞Äâ…ïç•¡•ïπ—}ïµÖ•∞àËÅ…ïç•¡•ïπ—lâïµÖ•∞ât∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÖ’—°Ω»àËÅÖ’—°Ω»πùï–†âπÖµîà∞Äã%≈’•¡îÅ%π”•ù…Ö±îà§∞ÄâëÖ—îàËÅ}ç…µ}πΩ‹†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ≠•πêàËÅ≠•πê∞Äâ—ï·–àËÅÕ—»°—ï·–§∞Äâ…ïÖêàËÅÖ±Õî∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—Öç—}•êàËÅçΩπ—Öç—lâ•êât∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—Öç—}πÖµîàËÅòâÌçΩπ—Öç–πùï–†ù¡…ïπΩ¥ú∞Äúú•ÙÅÌçΩπ—Öç–πùï–†ùπΩ¥ú∞Äúú•ÙàπÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡’â±•çÖ—•Ωπ}•êàËÅ¡’â±•çÖ—•Ωπlâ•êât∞(ÄÄÄÄÄÄÄÅÙ§(()ëïòÅ}ç…µ}Öëë}ô’πë•πù}…ïô’ÕÖ±}πΩ—•ô•çÖ—•ΩπÃ°ëÖ—Ñ∞ÅçΩπ—Öç–∞ÅÕ—Öâ±ï}•ê§Ë(ÄÄÄÄààâ±ï…—îÅç°Ö≈’îÅçΩµ¡—îÅI4Å’πîÅôΩ•ÃÅ±Ω…ÃÅêù’∏ÅπΩ’ŸïÖ‘Å…ïô’ÃÅ]=∏ààà(ÄÄÄÅçΩπ—Öç—}πÖµîÄÙÄàÄàπ©Ω•∏†(ÄÄÄÄÄÄÄÅ¡Ö…–ÅôΩ»Å¡Ö…–Å•∏ÅmçΩπ—Öç–πùï–†â¡…ïπΩ¥à§∞ÅçΩπ—Öç–πùï–†âπΩ¥à•t(ÄÄÄÄÄÄÄÅ•òÅÕ—»°¡Ö…–ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄ§πÕ—…•¿†§ÅΩ»ÄâA•Õ—îÅÕÖπÃÅπΩ¥à(ÄÄÄÅπΩ—•ô•çÖ—•ΩπÃÄÙÅëÖ—ÑπÕï—ëïôÖ’±–†âç…µ}πΩ—•ô•çÖ—•ΩπÃà∞Åmt§(ÄÄÄÅôΩ»Å’Õï»Å•∏ÅUMILπŸÖ±’ïÃ†§Ë(ÄÄÄÄÄÄÄÅ…ïç•¡•ïπ—}ïµÖ•∞ÄÙÅÕ—»°’Õï»πùï–†âïµÖ•∞à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å…ïç•¡•ïπ—}ïµÖ•∞Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅπΩ—•ô•çÖ—•ΩπÃπ•πÕï…–†¿∞ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ•êàËÅÕ—»°’’•êπ’’•ê–†§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ïç•¡•ïπ—}ïµÖ•∞àËÅ…ïç•¡•ïπ—}ïµÖ•∞∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÖ’—°Ω»àËÄâ…ÖπçîÅQ…ÖŸÖ•∞à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâëÖ—îàËÅ}ç…µ}πΩ‹†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ≠•πêàËÄâô’πë•πù}…ïô’Õïêà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ—ï·–àËÅòâ1îÅô•πÖπçïµïπ–Å…ÖπçîÅQ…ÖŸÖ•∞ÅëîÅÌçΩπ—Öç—}πÖµïÙÅÑÉ•”§Å…ïô’œ§∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ïÖêàËÅÖ±Õî∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—Öç—}•êàËÅçΩπ—Öç—lâ•êât∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—Öç—}πÖµîàËÅçΩπ—Öç—}πÖµî∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕΩ’…çï}›ïëΩô}ôΩ±ëï…}•êàËÅÕ—Öâ±ï}•ê∞(ÄÄÄÄÄÄÄÅÙ§(()ëïòÅ}ç…µ}πΩ—•ô•çÖ—•ΩπÕ}¡ÖÂ±ΩÖê°ëÖ—Ñ∞ÅïµÖ•∞§Ë(ÄÄÄÅ…ï—’…∏Ål(ÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅëÖ—Ñπùï–†âç…µ}πΩ—•ô•çÖ—•ΩπÃà∞Åmt§(ÄÄÄÄÄÄÄÅ•òÅ•—ï¥πùï–†â…ïç•¡•ïπ—}ïµÖ•∞à§ÄÙÙÅïµÖ•∞(ÄÄÄÅt(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩπΩ—•ô•çÖ—•ΩπÃà∞Åµï—°ΩëÃılâPà∞ÄâAQ ât§)±Ωù•π}…ï≈’•…ïê)}ç…µ}Õï…•Ö±•Èïê)ëïòÅç…µ}πΩ—•ô•çÖ—•ΩπÃ†§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§ÏÅïµÖ•∞ÄÙÄ°ç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ§πùï–†âïµÖ•∞à∞Äàà§(ÄÄÄÅ•—ïµÃÄÙÅ}ç…µ}πΩ—•ô•çÖ—•ΩπÕ}¡ÖÂ±ΩÖê°ëÖ—Ñ∞ÅïµÖ•∞§(ÄÄÄÅ•òÅ…ï≈’ïÕ–πµï—°ΩêÄÙÙÄâAQ àË(ÄÄÄÄÄÄÄÅπΩ—•ô•çÖ—•Ωπ}•êÄÙÅÕ—»†°…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ§πùï–†â•êà§ÅΩ»Äàà§(ÄÄÄÄÄÄÄÅôΩ»Å•—ï¥Å•∏Å•—ïµÃË(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–ÅπΩ—•ô•çÖ—•Ωπ}•êÅΩ»Å•—ï¥πùï–†â•êà§ÄÙÙÅπΩ—•ô•çÖ—•Ωπ}•êË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•—ïµlâ…ïÖêâtÄÙÅQ…’î(ÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°•—ïµÃ§(()Ö¡¿π…Ω’—î†(ÄÄÄÄàΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯Ω¡’â±•çÖ—•ΩπÃºÒ¡’â±•çÖ—•Ωπ}•ê¯à∞(ÄÄÄÅµï—°ΩëÃılâAQ à∞Äâ1Qât∞(§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}ëï±ï—ï}¡’â±•çÖ—•Ω∏°çΩπ—Öç—}•ê∞Å¡’â±•çÖ—•Ωπ}•ê§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§ÏÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ¡’â±•çÖ—•Ω∏ÄÙÅ}ç…µ}¡’â±•çÖ—•Ω∏°çΩπ—Öç–∞Å¡’â±•çÖ—•Ωπ}•ê§Å•òÅçΩπ—Öç–Åï±ÕîÅ9Ωπî(ÄÄÄÅ•òÅπΩ–Å¡’â±•çÖ—•Ω∏ËÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâA’â±•çÖ—•Ω∏Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅ•òÅπΩ–Å}ç…µ}Ω›πÕ}çΩπ—ïπ–°¡’â±•çÖ—•Ω∏§Ë(ÄÄÄÄÄÄÄÅÖç—•Ω∏ÄÙÄâµΩë•ô•ï»àÅ•òÅ…ï≈’ïÕ–πµï—°ΩêÄÙÙÄâAQ àÅï±ÕîÄâÕ’¡¡…•µï»à(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÅòâYΩ’ÃÅπîÅ¡Ω’ŸïËÅÌÖç—•ΩπÙÅ≈’îÅŸΩÃÅ¡’â±•çÖ—•ΩπÃâÙ§∞Ä–¿Ã(ÄÄÄÅ•òÅ…ï≈’ïÕ–πµï—°ΩêÄÙÙÄâAQ àË(ÄÄÄÄÄÄÄÅ—ï·–ÄÙÅÕ—»†°…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ§πùï–†â—ï·—îà∞Äàà§§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å—ï·–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1îÅ—ï·—îÅëîÅ±ÑÅ¡’â±•çÖ—•Ω∏ÅïÕ–Å…ï≈’•ÃâÙ§∞Ä–¿¿(ÄÄÄÄÄÄÄÅ¡’â±•çÖ—•Ωπlâ—ï·—îâtÄÙÅ—ï·–(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâ¡’â±•çÖ—•Ω∏àËÅ¡’â±•çÖ—•Ω∏∞ÄâçΩπ—Öç–àËÅçΩπ—Öç—Ù§(ÄÄÄÅçΩπ—Öç—lâ¡’â±•çÖ—•ΩπÃâtπ…ïµΩŸî°¡’â±•çÖ—•Ω∏§ÏÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§ÏÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Äàà∞Ä»¿–(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯Ω¡’â±•çÖ—•ΩπÃºÒ¡’â±•çÖ—•Ωπ}•ê¯Ω±•≠îà∞Åµï—°ΩëÃılâA=MPât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}±•≠ï}¡’â±•çÖ—•Ω∏°çΩπ—Öç—}•ê∞Å¡’â±•çÖ—•Ωπ}•ê§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§ÏÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ¡’â±•çÖ—•Ω∏ÄÙÅ}ç…µ}¡’â±•çÖ—•Ω∏°çΩπ—Öç–∞Å¡’â±•çÖ—•Ωπ}•ê§Å•òÅçΩπ—Öç–Åï±ÕîÅ9Ωπî(ÄÄÄÅ•òÅπΩ–Å¡’â±•çÖ—•Ω∏ËÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâA’â±•çÖ—•Ω∏Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅïµÖ•∞ÄÙÄ°ç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ§πùï–†âïµÖ•∞à∞Äàà§(ÄÄÄÅ±•≠ïÃÄÙÅ¡’â±•çÖ—•Ω∏πÕï—ëïôÖ’±–†â±•≠ïÃà∞Åmt§(ÄÄÄÅ±•≠ïÃπ…ïµΩŸî°ïµÖ•∞§Å•òÅïµÖ•∞Å•∏Å±•≠ïÃÅï±ÕîÅ±•≠ïÃπÖ¡¡ïπê°ïµÖ•∞§(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâ¡’â±•çÖ—•Ω∏àËÅ¡’â±•çÖ—•Ω∏∞ÄâçΩπ—Öç–àËÅçΩπ—Öç—Ù§(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯Ω¡’â±•çÖ—•ΩπÃºÒ¡’â±•çÖ—•Ωπ}•ê¯ΩçΩµµïπ—Ãà∞Åµï—°ΩëÃılâA=MPât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çΩµµïπ—}¡’â±•çÖ—•Ω∏°çΩπ—Öç—}•ê∞Å¡’â±•çÖ—•Ωπ}•ê§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§ÏÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ¡’â±•çÖ—•Ω∏ÄÙÅ}ç…µ}¡’â±•çÖ—•Ω∏°çΩπ—Öç–∞Å¡’â±•çÖ—•Ωπ}•ê§Å•òÅçΩπ—Öç–Åï±ÕîÅ9Ωπî(ÄÄÄÅ•òÅπΩ–Å¡’â±•çÖ—•Ω∏ËÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâA’â±•çÖ—•Ω∏Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅ—ï·–ÄÙÅÕ—»†°…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ§πùï–†â—ï·—îà∞Äàà§§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–Å—ï·–ËÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1îÅçΩµµïπ—Ö•…îÅïÕ–Å…ï≈’•ÃâÙ§∞Ä–¿¿(ÄÄÄÅ’Õï»ÄÙÅç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ(ÄÄÄÅçΩµµïπ–ÄÙÅÏâ•êàËÅÕ—»°’’•êπ’’•ê–†§§∞ÄâëÖ—îàËÅ}ç…µ}πΩ‹†§∞Äâ—ï·—îàËÅ—ï·–∞ÄâÖ’—°Ω»àËÅ’Õï»πùï–†âπÖµîà∞Äã%≈’•¡îÅ%π”•ù…Ö±îà§∞ÄâÖ’—°Ω…}ïµÖ•∞àËÅ’Õï»πùï–†âïµÖ•∞à∞Äàà•Ù(ÄÄÄÅ¡’â±•çÖ—•Ω∏πÕï—ëïôÖ’±–†âçΩµµïπ—Ãà∞Åmt§πÖ¡¡ïπê°çΩµµïπ–§(ÄÄÄÅ}ç…µ}Öëë}µïπ—•Ωπ}πΩ—•ô•çÖ—•ΩπÃ°ëÖ—Ñ∞Å—ï·–∞ÅçΩπ—Öç–∞Å¡’â±•çÖ—•Ω∏∞Å≠•πêÙâ…ï¡±‰à§(ÄÄÄÅ•òÅ¡’â±•çÖ—•Ω∏πùï–†âÖ’—°Ω…}ïµÖ•∞à§ÅÖπêÅ¡’â±•çÖ—•Ω∏πùï–†âÖ’—°Ω…}ïµÖ•∞à§ÄÑÙÅ’Õï»πùï–†âïµÖ•∞à§Ë(ÄÄÄÄÄÄÄÅëÖ—ÑπÕï—ëïôÖ’±–†âç…µ}πΩ—•ô•çÖ—•ΩπÃà∞Åmt§π•πÕï…–†¿∞ÅÏâ•êàËÅÕ—»°’’•êπ’’•ê–†§§∞Äâ…ïç•¡•ïπ—}ïµÖ•∞àËÅ¡’â±•çÖ—•ΩπlâÖ’—°Ω…}ïµÖ•∞ât∞ÄâÖ’—°Ω»àËÅ’Õï»πùï–†âπÖµîà∞Äã%≈’•¡îÅ%π”•ù…Ö±îà§∞ÄâëÖ—îàËÅ}ç…µ}πΩ‹†§∞Äâ≠•πêàËÄâ…ï¡±‰à∞Äâ—ï·–àËÅ—ï·–∞Äâ…ïÖêàËÅÖ±Õî∞ÄâçΩπ—Öç—}•êàËÅçΩπ—Öç—lâ•êât∞ÄâçΩπ—Öç—}πÖµîàËÅòâÌçΩπ—Öç–πùï–†ù¡…ïπΩ¥ú∞Äúú•ÙÅÌçΩπ—Öç–πùï–†ùπΩ¥ú∞Äúú•ÙàπÕ—…•¿†§∞Äâ¡’â±•çÖ—•Ωπ}•êàËÅ¡’â±•çÖ—•Ωπlâ•êâuÙ§(ÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§ÏÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâçΩµµïπ–àËÅçΩµµïπ–∞ÄâçΩπ—Öç–àËÅçΩπ—Öç—Ù§∞Ä»¿ƒ(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯Ω¡’â±•çÖ—•ΩπÃºÒ¡’â±•çÖ—•Ωπ}•ê¯ΩçΩµµïπ—ÃºÒçΩµµïπ—}•ê¯à∞Åµï—°ΩëÃılâ1Qât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}ëï±ï—ï}¡’â±•çÖ—•Ωπ}çΩµµïπ–°çΩπ—Öç—}•ê∞Å¡’â±•çÖ—•Ωπ}•ê∞ÅçΩµµïπ—}•ê§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§ÏÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ¡’â±•çÖ—•Ω∏ÄÙÅ}ç…µ}¡’â±•çÖ—•Ω∏°çΩπ—Öç–∞Å¡’â±•çÖ—•Ωπ}•ê§Å•òÅçΩπ—Öç–Åï±ÕîÅ9Ωπî(ÄÄÄÅçΩµµïπ–ÄÙÅπï·–†°•—ï¥ÅôΩ»Å•—ï¥Å•∏Å¡’â±•çÖ—•Ω∏πùï–†âçΩµµïπ—Ãà∞Åmt§Å•òÅ•—ï¥πùï–†â•êà§ÄÙÙÅçΩµµïπ—}•ê§∞Å9Ωπî§Å•òÅ¡’â±•çÖ—•Ω∏Åï±ÕîÅ9Ωπî(ÄÄÄÅ•òÅπΩ–ÅçΩµµïπ–ËÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩµµïπ—Ö•…îÅ•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅ•òÅπΩ–Å}ç…µ}Ω›πÕ}çΩπ—ïπ–°çΩµµïπ–§ËÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâYΩ’ÃÅπîÅ¡Ω’ŸïËÅÕ’¡¡…•µï»Å≈’îÅŸΩÃÅçΩµµïπ—Ö•…ïÃâÙ§∞Ä–¿Ã(ÄÄÄÅ¡’â±•çÖ—•ΩπlâçΩµµïπ—Ãâtπ…ïµΩŸî°çΩµµïπ–§ÏÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§ÏÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Äàà∞Ä»¿–(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯ΩçΩπŸï…—•»à∞Åµï—°ΩëÃılâA=MPât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çΩπŸï…—}çΩπ—Öç–°çΩπ—Öç—}•ê§Ë(ÄÄÄÄààâΩπŸï…—•–Å’πîÅ¡•Õ—îÅëÖπÃÅ±îÅI4ÅÕÖπÃÅì•¡ïπë…îÅêù’∏ÅÕï…Ÿ•çîÅï·—ï…πî∏ààà(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§ÏÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–ËÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅΩ±ë}Õ—Ö—’ÃÄÙÅçΩπ—Öç–πùï–†âÕ—Ö—’–à∞Äâ9Ω’ŸïÖ’‡à§(ÄÄÄÅ•òÅΩ±ë}Õ—Ö—’ÃÄÙÙÄâΩπŸï…—§àË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâçΩπ—Öç–àËÅçΩπ—Öç—Ù§(ÄÄÄÅç°Öπùïë}Ö–ÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅçΩπ—Öç—lâÕ—Ö—’–âtÄÙÄâΩπŸï…—§à(ÄÄÄÅçΩπ—Öç—lâÕ—Ö—’Õ}ç°Öπùïë}Ö–âtÄÙÅç°Öπùïë}Ö–(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–πùï–†âçΩπŸï…—ïë}Ö–à§Ë(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâçΩπŸï…—ïë}Ö–âtÄÙÅç°Öπùïë}Ö–(ÄÄÄÅçΩπ—Öç—lâë•Õ≈’Ö±•ô•çÖ—•Ωπ}…ïÖÕΩ∏âtÄÙÄàà(ÄÄÄÅçΩπ—Öç—lâë•Õ≈’Ö±•ô•çÖ—•Ωπ}ëï—Ö•∞âtÄÙÄàà(ÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞ÄâÕ—Ö—’–à∞ÄâM—Ö—’–ÄËÅΩπŸï…—§à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâπç•ï∏ÅÕ—Ö—’–ÄËÅÌΩ±ë}Õ—Ö—’ÕÙà§(ÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅç°Öπùïë}Ö–(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâçΩπ—Öç–àËÅçΩπ—Öç—Ù§(()}I5}I159Q%I}!ÄÙÅÌÙ)}I5}I159Q%I}!}1=,ÄÙÅ—°…ïÖë•πúπI1Ωç¨†§)}I5}I159Q%I}IEUMQ}1=-LÄÙÅÌÙ)}I5}I159Q%I}IEUMQ}1=-M}UIÄÙÅ—°…ïÖë•πúπ1Ωç¨†§(()ëïòÅ}ç…µ}…ïù±ïµïπ—Ö•…ï}çÖç°ï}≠ï‰°çΩπ—Öç–§Ë(ÄÄÄÄààâ%πŸÖ±•ëÖ—îÅ—°îÅÕ°Ω…–µ±•ŸïêÅçÖç°îÅ›°ï∏Å•ëïπ—•—‰Ω—…Ö•π•πúÅç°ÖπùïÃ∏ààà(ÄÄÄÅ…ï—’…∏Ä†(ÄÄÄÄÄÄÄÅΩÃπ¡Ö—†πÖâÕ¡Ö—†°Q}%1§∞(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†â•êà§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†â’¡ëÖ—ïë}Ö–à§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†âëïÕ¡}—Â¡îà§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†â¡…ïπΩ¥à§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†âπΩ¥à§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†âµÖ•∞à§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†â—ï±ï¡°Ωπîà§ÅΩ»Äàà§∞(ÄÄÄÄ§(()ëïòÅ}ç…µ}…ïù±ïµïπ—Ö•…ï}çÖç°ï}——∞†§Ë(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅµÖ‡†ƒ‘∏¿∞Åô±ΩÖ–°ΩÃπùï—ïπÿ†âI5}I159Q%I}!}QQ0à∞ÄàÃ¿¿à§§§(ÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÄÃ¿¿∏¿(()ëïòÅ}ç…µ}…ïù±ïµïπ—Ö•…ï}çÖç°ïê°çΩπ—Öç–§Ë(ÄÄÄÅ≠ï‰ÄÙÅ}ç…µ}…ïù±ïµïπ—Ö•…ï}çÖç°ï}≠ï‰°çΩπ—Öç–§(ÄÄÄÅ›•—†Å}I5}I159Q%I}!}1=,Ë(ÄÄÄÄÄÄÄÅçÖç°ïêÄÙÅ}I5}I159Q%I}!πùï–°≠ï‰§(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅçÖç°ïêË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(ÄÄÄÄÄÄÄÅ•òÅ—•µîπµΩπΩ—Ωπ•å†§Ä¥ÅçÖç°ïëlâÕ—Ω…ïë}Ö–âtÄ¯Å}ç…µ}…ïù±ïµïπ—Ö•…ï}çÖç°ï}——∞†§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ}I5}I159Q%I}!π¡Ω¿°≠ï‰∞Å9Ωπî§(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å9Ωπî(ÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅçΩ¡‰πëïï¡çΩ¡‰°çÖç°ïëlâ¡ÖÂ±ΩÖêât§(ÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëlâçÖç°ïêâtÄÙÅQ…’î(ÄÄÄÄÄÄÄÅ…ï—’…∏Å¡ÖÂ±ΩÖê∞ÅçÖç°ïëlâÕ—Ö—’Õ}çΩëîât(()ëïòÅ}ç…µ}Õ—Ω…ï}…ïù±ïµïπ—Ö•…ï}çÖç°î°çΩπ—Öç–∞Å¡ÖÂ±ΩÖê∞ÅÕ—Ö—’Õ}çΩëî§Ë(ÄÄÄÅ≠ï‰ÄÙÅ}ç…µ}…ïù±ïµïπ—Ö•…ï}çÖç°ï}≠ï‰°çΩπ—Öç–§(ÄÄÄÅ›•—†Å}I5}I159Q%I}!}1=,Ë(ÄÄÄÄÄÄÄÅ}I5}I159Q%I}!m≠ïÂtÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡ÖÂ±ΩÖêàËÅçΩ¡‰πëïï¡çΩ¡‰°¡ÖÂ±ΩÖê§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö—’Õ}çΩëîàËÅÕ—Ö—’Õ}çΩëî∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ω…ïë}Ö–àËÅ—•µîπµΩπΩ—Ωπ•å†§∞(ÄÄÄÄÄÄÄÅÙ(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯Ω…ïù±ïµïπ—Ö•…îà§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çΩπ—Öç—}…ïù±ïµïπ—Ö•…î°çΩπ—Öç—}•ê§Ë(ÄÄÄÄààâ·¡ΩÕîÅ±îÅÕ’•Ÿ§ÅÀ•ù±ïµïπ—Ö•…îÅ¡Ö…—Öü§ÅÕÖπÃÅ—…ÖπÕµï——…îÅ±îÅÕïç…ï–∏ààà(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅ…ïô…ïÕ°}…ï≈’ïÕ—ïêÄÙÅÕ—»°…ï≈’ïÕ–πÖ…ùÃπùï–†â…ïô…ïÕ†à§ÅΩ»Äàà§πÕ—…•¿†§π±Ω›ï»†§Å•∏ÅÏ(ÄÄÄÄÄÄÄÄàƒà∞Äâ—…’îà∞ÄâÂïÃà∞ÄâΩ’§à∞(ÄÄÄÅÙ(ÄÄÄÅ•òÅπΩ–Å…ïô…ïÕ°}…ï≈’ïÕ—ïêË(ÄÄÄÄÄÄÄÅçÖç°ïêÄÙÅ}ç…µ}…ïù±ïµïπ—Ö•…ï}çÖç°ïê°çΩπ—Öç–§(ÄÄÄÄÄÄÄÅ•òÅçÖç°ïêË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°çÖç°ïël¡t§∞ÅçÖç°ïël≈t((ÄÄÄÅ›•—†Å}I5}I159Q%I}IEUMQ}1=-M}UIË(ÄÄÄÄÄÄÄÅ±Ωç¨ÄÙÅ}I5}I159Q%I}IEUMQ}1=-LπÕï—ëïôÖ’±–°çΩπ—Öç—}•ê∞Å—°…ïÖë•πúπ1Ωç¨†§§(ÄÄÄÅ›•—†Å±Ωç¨Ë(ÄÄÄÄÄÄÄÄåÅA±’Õ•ï’…ÃÅΩπù±ï—ÃÅ¡ï’Ÿïπ–ÅΩ’Ÿ…•»Å±ÑÅ∑©µîÅô•ç°îÅÖ‘Å∑©µîÅ•πÕ—Öπ–∏Å1î(ÄÄÄÄÄÄÄÄåÅ¡…ïµ•ï»ÅôÖ•–Å∞ùÖ¡¡ï∞Åë•Õ—Öπ–∞Å±ïÃÅÕ’•ŸÖπ—ÃÅÀ•’—•±•Õïπ–ÅÕΩ∏ÅÀ•Õ’±—Ö–∏(ÄÄÄÄÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å…ïô…ïÕ°}…ï≈’ïÕ—ïêË(ÄÄÄÄÄÄÄÄÄÄÄÅçÖç°ïêÄÙÅ}ç…µ}…ïù±ïµïπ—Ö•…ï}çÖç°ïê°çΩπ—Öç–§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅçÖç°ïêË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°çÖç°ïël¡t§∞ÅçÖç°ïël≈t((ÄÄÄÄÄÄÄÅô…Ω¥Åç…µ}çπÖ¡Õ}—…Öç≠•πúÅ•µ¡Ω…–Å¡…Ω·Â}…ïù±ïµïπ—Ö•…î∞ÅÕçΩ…•πù}ÕπÖ¡Õ°Ω—}ô…Ωµ}…ïµΩ—î(ÄÄÄÄÄÄÄÅ…ïµΩ—îÄÙÅ¡…Ω·Â}…ïù±ïµïπ—Ö•…î°Ö¡¿∞ÅçΩπ—Öç–∞Å°——¡}ùï–ı…ï≈’ïÕ—Ãπùï–§(ÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕîÄÙÅ…ïµΩ—ïl¡tÅ•òÅ•Õ•πÕ—Öπçî°…ïµΩ—î∞Å—’¡±î§Åï±ÕîÅ…ïµΩ—î(ÄÄÄÄÄÄÄÅÕ—Ö—’Õ}çΩëîÄÙÅ…ïµΩ—ïl≈tÅ•òÅ•Õ•πÕ—Öπçî°…ïµΩ—î∞Å—’¡±î§Åï±ÕîÅ…ïÕ¡ΩπÕîπÕ—Ö—’Õ}çΩëî(ÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ…ïÕ¡ΩπÕîπùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(ÄÄÄÄÄÄÄÅÕπÖ¡Õ°Ω—ÃÄÙÅëÖ—ÑπÕï—ëïôÖ’±–†âç…µ}çπÖ¡Õ}ÕçΩ…•πù}ÕπÖ¡Õ°Ω—Ãà∞ÅÌÙ§(ÄÄÄÄÄÄÄÅ¡…ïŸ•Ω’ÃÄÙÅÕπÖ¡Õ°Ω—Ãπùï–°Õ—»°çΩπ—Öç—}•ê§§(ÄÄÄÄÄÄÄÅ•òÅÕ—Ö—’Õ}çΩëîÄÙÙÄ»¿¿ÅΩ»Ä°Õ—Ö—’Õ}çΩëîÄÙÙÄ–¿–ÅÖπêÅ¡ÖÂ±ΩÖêπùï–†â…ïÖÕΩ∏à§ÄÙÙÄâçπÖ¡Õ}πΩ—}ôΩ’πêà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÕπÖ¡Õ°Ω–ÄÙÅÕçΩ…•πù}ÕπÖ¡Õ°Ω—}ô…Ωµ}…ïµΩ—î°¡ÖÂ±ΩÖê∞Å°——¡}Õ—Ö—’ÃıÕ—Ö—’Õ}çΩëî§(ÄÄÄÄÄÄÄÄÄÄÄÅΩ±ë}ÕçΩ…îÄÙÅçÖ±ç’±Ö—ï}çÖπë•ëÖ—ï}•π—ïù…Ö—•Ωπ}ÕçΩ…î°çΩπ—Öç–∞Å¡…ïŸ•Ω’Ã§(ÄÄÄÄÄÄÄÄÄÄÄÅÕπÖ¡Õ°Ω—ÕmÕ—»°çΩπ—Öç—}•ê•tÄÙÅÕπÖ¡Õ°Ω–(ÄÄÄÄÄÄÄÄÄÄÄÅπï›}ÕçΩ…îÄÙÅçÖ±ç’±Ö—ï}çÖπë•ëÖ—ï}•π—ïù…Ö—•Ωπ}ÕçΩ…î°çΩπ—Öç–∞ÅÕπÖ¡Õ°Ω–§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅπΩ–Å¡…ïŸ•Ω’ÃÅΩ»Å¡…ïŸ•Ω’Ãπùï–†âπΩ…µÖ±•Èïë}Õ—Ö—’Ãà§ÄÑÙÅÕπÖ¡Õ°Ω–πùï–†âπΩ…µÖ±•Èïë}Õ—Ö—’Ãà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ±Öâï±ÃÄÙÅÏâÖççï¡—ïêàËÄâççï¡”§à∞Äâ—…ÖπÕµ•——ïêàËÄâQ…ÖπÕµ•Ãà∞Äâ•π}…ïŸ•ï‹àËÄâ∏Å•πÕ—…’ç—•Ω∏à∞Äâ…ïù•Õ—ï…ïêàËÄâπ…ïù•Õ—À§à∞Äâ…ïô’ÕïêàËÄâIïô’œ§à∞ÄâπΩ}…ïÕ’±–àËÄâ’ç’∏ÅÀ•Õ’±—Ö–à∞Äâ’π≠πΩ›∏àËÄâ%πçΩππ‘âÙ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅÕπÖ¡Õ°Ω—lâπΩ…µÖ±•Èïë}Õ—Ö—’ÃâtÄÙÙÄâÖççï¡—ïêàË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—•—±îÄÙÄâ’—Ω…•ÕÖ—•Ω∏Å9ALÅÖççï¡”•îà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—•—±îÄÙÅòâM’•Ÿ§Å9ALÅ¡ÖÕœ§ÅëîÅÌ±Öâï±Ãπùï–†°¡…ïŸ•Ω’ÃÅΩ»ÅÌÙ§πùï–†ùπΩ…µÖ±•Èïë}Õ—Ö—’Ãú§∞Äù%πçΩππ‘ú•ÙÉÄÅÌ±Öâï±ÕmÕπÖ¡Õ°Ω—lùπΩ…µÖ±•Èïë}Õ—Ö—’ÃùuuÙà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞ÄâÕçΩ…îà∞Å—•—±î§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅΩ±ë}ÕçΩ…îπùï–†â±ïŸï∞à§ÄÑÙÅπï›}ÕçΩ…îπùï–†â±ïŸï∞à§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞ÄâÕçΩ…îà∞ÅòâMçΩ…îÅìäe•π”•ù…Ö—•Ω∏Å¡ÖÕœ§ÅëîÅÌΩ±ë}ÕçΩ…îπùï–†ùÕçΩ…îú•ÙÉÄÅÌπï›}ÕçΩ…îπùï–†ùÕçΩ…îú•ÙÄËÅÌπï›}ÕçΩ…îπùï–†ù±Öâï∞ú•Ùà§(ÄÄÄÄÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëlâ•π—ïù…Ö—•Ωπ}ÕçΩ…îâtÄÙÅπï›}ÕçΩ…î(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëlâÕçΩ…•πù}ÕπÖ¡Õ°Ω–âtÄÙÅÕπÖ¡Õ°Ω–(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëlâçÖç°ïêâtÄÙÅÖ±Õî(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}Õ—Ω…ï}…ïù±ïµïπ—Ö•…ï}çÖç°î°çΩπ—Öç–∞Å¡ÖÂ±ΩÖê∞ÅÕ—Ö—’Õ}çΩëî§(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°¡ÖÂ±ΩÖê§∞ÅÕ—Ö—’Õ}çΩëî(ÄÄÄÄÄÄÄÄåÅUπîÅ¡ÖππîÅë•Õ—Öπ—îÅπîÅì•—…’•–Å©ÖµÖ•ÃÅ±îÅëï…π•ï»ÅôÖ•–ÅÀ•’ÕÕ§∏(ÄÄÄÄÄÄÄÅ•òÅ¡…ïŸ•Ω’ÃË(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëlâ•π—ïù…Ö—•Ωπ}ÕçΩ…îâtÄÙÅçÖ±ç’±Ö—ï}çÖπë•ëÖ—ï}•π—ïù…Ö—•Ωπ}ÕçΩ…î°çΩπ—Öç–∞Å¡…ïŸ•Ω’Ã§(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖëlâÕçΩ…•πù}ÕπÖ¡Õ°Ω–âtÄÙÅÏ®©¡…ïŸ•Ω’Ã∞Äâ…ïô…ïÕ°}ôÖ•±ïêàËÅQ…’ïÙ(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°¡ÖÂ±ΩÖê§∞ÅÕ—Ö—’Õ}çΩëî(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥Ω…ïôΩ…µ’±ï»à∞Åµï—°ΩëÃılâA=MPât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}…ï¡°…ÖÕî†§Ë(ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(ÄÄÄÅ—ï·–ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†â—ï·—îà∞Äàà§§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–Å—ï·–ËÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâQï·—îÅŸ•ëîâÙ§∞Ä–¿¿(ÄÄÄÅ•òÅπΩ–ÅΩÃπùï—ïπÿ†â=A9%}A%}-dà§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ=A9%}A%}-dÅπΩ∏ÅçΩπô•ù’À•îâÙ§∞Ä‘¿Ã(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ•òÅ¡ÖÂ±ΩÖêπùï–†âµΩëîà§ÄÙÙÄâçΩ……ïç—•Ωπ}ë•ç—ïîàË(ÄÄÄÄÄÄÄÄÄÄÄÅÕÂÕ—ïµ}¡…Ωµ¡–ÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâΩ……•ùîÅ’π•≈’ïµïπ–Å≥äeΩ…—°Ωù…Ö¡°î∞Å±ïÃÅÖççΩ…ëÃ∞Å±ÑÅçΩπ©’ùÖ•ÕΩ∏∞Äà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ±ÑÅ¡Ωπç—’Ö—•Ω∏∞Å±ïÃÅÀ•√•—•—•ΩπÃÅ•πŸΩ±Ωπ—Ö•…ïÃÅï–Å±ïÃÅï……ï’…ÃÄà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄã•Ÿ•ëïπ—ïÃÅëîÅ—…ÖπÕç…•¡—•Ω∏ÅŸΩçÖ±îÅëîÅçï——îÅπΩ—îÅI4∏ÅAÀ•Õï…ŸîÄà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÕ—…•ç—ïµïπ–Å±îÅÕïπÃ∞Å±ïÃÅôÖ•—Ã∞Å±ïÃÅπΩµÃ∞Å±ïÃÅëÖ—ïÃ∞Å±ïÃÄà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâµΩπ—Öπ—ÃÅï–Å±ïÃÅÖç…ΩπÂµïÃÅ∑•—•ï»∏Å;äeÖ©Ω’—îÅÖ’ç’πîÅ•πôΩ…µÖ—•Ω∏∏à(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÅÕÂÕ—ïµ}¡…Ωµ¡–ÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâIïôΩ…µ’±îÅ±ÑÅπΩ—îÅI4Åï∏Åô…ÖªùÖ•ÃÅ¡…ΩôïÕÕ•Ωππï∞∞Åç±Ö•»Åï–Äà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâôÖç—’ï∞∏Å9îÅ…Ö©Ω’—îÅÖ’ç’πîÅ•πôΩ…µÖ—•Ω∏∏à(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ…ïôΩ…µ’±Ö—•Ω∏ÄÙÅ}ç…µ}Ö§°ÕÂÕ—ïµ}¡…Ωµ¡–∞Å—ï·–§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâ—ï·—îàËÅ…ïôΩ…µ’±Ö—•ΩπÙ§(ÄÄÄÅï·çï¡–Å·çï¡—•Ω∏ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ¡…•π–†â……ï’»Å…ïôΩ…µ’±Ö—•Ω∏ÅI4Ëà∞Åï·å§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1ÑÅ…ïôΩ…µ’±Ö—•Ω∏ÅïÕ–ÅµΩµïπ—Öª•µïπ–Å•πë•Õ¡Ωπ•â±îâÙ§∞Ä‘¿»(()ëïòÅ}ç…µ}ô…Öπçï}—…ÖŸÖ•±}…ï≈’ïÕ—}çΩπ—ï·–°çΩπ—Öç–∞Å¡ÖÂ±ΩÖê§Ë(ÄÄÄÄààâ	’•±êÅ—°îÅÕ—…•ç—±‰ÅôÖç—’Ö∞ÅçΩπ—ï·–Å’ÕïêÅâ‰Å—°îÅPÅ›…•—•πúÅÖÕÕ•Õ—Öπ–∏ààà(ÄÄÄÅëïòÅ—ï·–°≠ï‰∞Å±•µ•–§Ë(ÄÄÄÄÄÄÄÅŸÖ±’îÄÙÅ…îπÕ’à°»âqÃ¨à∞ÄàÄà∞ÅÕ—»°¡ÖÂ±ΩÖêπùï–°≠ï‰§ÅΩ»Äàà§§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅŸÖ±’ïlÈ±•µ•—t((ÄÄÄÅôΩ…µÖ—•Ωπ}çΩëîÄÙÅ}ç…µ}ôΩ…µÖ—•Ωπ}çΩëî°çΩπ—Öç–§(ÄÄÄÅôΩ…µÖ—•Ω∏ÄÙÅMIQI%Q}=I5Q%=9Lπùï–°ôΩ…µÖ—•Ωπ}çΩëî∞ÅÌÙ§(ÄÄÄÅçïπ—…ï}çΩëîÄÙÅ}πΩ…µÖ±•Èï}çïπ—…ï}çΩëî°çΩπ—Öç–πùï–†â±•ï‘à§§(ÄÄÄÅÕïç’…•—Â}çΩëïÃÄÙÅÏâÕ@à∞ÄâALà∞ÄâMM%@à∞ÄâMA}%9%Pà∞ÄâMA}YâÙ(ÄÄÄÅçïπ—…ïÃÄÙÅÏ(ÄÄÄÄÄÄÄÄâçΩ—ï}ÖÈ’»àËÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄâ%π”•ù…Ö±îÅO•ç’…•”§ÅΩ…µÖ—•ΩπÃÉÄÅA’ùï–µÕ’»µ…ùïπÃÄ°YÖ»§à(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅôΩ…µÖ—•Ωπ}çΩëîÅ•∏ÅÕïç’…•—Â}çΩëïÃ(ÄÄÄÄÄÄÄÄÄÄÄÅï±ÕîÄâ%π”•ù…Ö±îÅçÖëïµ‰ÉÄÅA’ùï–µÕ’»µ…ùïπÃÄ°YÖ»§à(ÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄâÖ’Ÿï…ùπîàËÄâ%π”•ù…Ö±îÅçÖëïµ‰ÅQï……ïÃÅìäe’Ÿï…ùπîà∞(ÄÄÄÄÄÄÄÄâ¡Ö…•ÃàËÄâ%π”•ù…Ö±îÅçÖëïµ‰ÅAÖ…•Ãà∞(ÄÄÄÅÙ(ÄÄÄÅçÖπë•ëÖ—ï}πÖµîÄÙÄàÄàπ©Ω•∏°ô•±—ï»°9Ωπî∞Ä†(ÄÄÄÄÄÄÄÅ}ç…µ}ôΩ…µÖ—}ô•…Õ—}πÖµî°çΩπ—Öç–πùï–†â¡…ïπΩ¥à§§∞(ÄÄÄÄÄÄÄÅ}ç…µ}ôΩ…µÖ—}±ÖÕ—}πÖµî°çΩπ—Öç–πùï–†âπΩ¥à§§∞(ÄÄÄÄ§§§(ÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄâçÖπë•ëÖ–àËÅÏâπΩµ}çΩµ¡±ï–àËÅçÖπë•ëÖ—ï}πÖµïÙ∞(ÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏àËÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâπΩ¥àËÅ}ç…µ}ôΩ…µÖ—•Ωπ}±Öâï∞°çΩπ—Öç–§ÅΩ»ÄâΩ…µÖ—•Ω∏ÅÕΩ’°Ö•”•îà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçïπ—…îàËÅçïπ—…ïÃπùï–°çïπ—…ï}çΩëî∞Äâ%π”•ù…Ö±îÅçÖëïµ‰à§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕïÕÕ•Ωπ}ÕΩ’°Ö•—ïîàËÅÕ—»°çΩπ—Öç–πùï–†âëÖ—ïÕ}ôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâë’…ïîàËÅôΩ…µÖ—•Ω∏πùï–†âë’…Ö—•Ω∏à∞Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâôΩ…µÖ–àËÅôΩ…µÖ—•Ω∏πùï–†âôΩ…µÖ–à∞Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâΩâ©ïç—•òàËÅôΩ…µÖ—•Ω∏πùï–†â¡’…¡ΩÕîà∞Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ¡ïç•Ö±•ÕÖ—•Ωπ}ë’}çïπ—…îàËÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâïπ—…îÅÕ√•ç•Ö±•œ§ÅëÖπÃÅ±ïÃÅ∑•—•ï…ÃÅëîÅ±ÑÅ¡…Ω—ïç—•Ω∏Å…Ö¡¡…Ωç£•î∏à(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅôΩ…µÖ—•Ωπ}çΩëîÄÙÙÄâÕ@à(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅï±ÕîÄâïπ—…îÅÕ√•ç•Ö±•œ§ÅëÖπÃÅ±ïÃÅ∑•—•ï…ÃÅëîÅ±ÑÅœ•ç’…•”§Å¡…•€•î∏à(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅôΩ…µÖ—•Ωπ}çΩëîÅ•∏ÅÕïç’…•—Â}çΩëïÃÅï±ÕîÄàà(ÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÄÄÄÄÄâô•πÖπçïµïπ–àËÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâç¡ô}çΩπÕ’±—îàËÅ}ÂïÃ°çΩπ—Öç–πùï–†âç¡òà§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâµΩπ—Öπ—}ç¡ô}ë•Õ¡Ωπ•â±ï}ï’…ΩÃàËÅÕ—»°çΩπ—Öç–πùï–†âç¡ô}µΩπ—Öπ–à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÄÄÄÄÄâ¡…Ωô•∞àËÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâÖπç•ïπ}µ•±•—Ö•…îàËÅ}ÂïÃ°¡ÖÂ±ΩÖêπùï–†âÖπç•ïπ}µ•±•—Ö•…îà§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçÖ…—ï}¡…ΩôïÕÕ•Ωππï±±ï}çπÖ¡ÃàËÅ}ÂïÃ°¡ÖÂ±ΩÖêπùï–†âçÖ…—ï}¡…ΩôïÕÕ•Ωππï±±îà§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâï·¡ï…•ïπçï}ïπ}Õïç’…•—ï}¡…•ŸïîàËÅ}ÂïÃ°¡ÖÂ±ΩÖêπùï–†âï·¡ï…•ïπçï}Õïç’…•—îà§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâïπ}…ïçΩπŸï…Õ•Ωπ}¡…ΩôïÕÕ•Ωππï±±îàËÅ}ÂïÃ°¡ÖÂ±ΩÖêπùï–†â…ïçΩπŸï…Õ•Ω∏à§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡ï…µ•Õ}â}ï—}µΩâ•±•—îàËÅ}ÂïÃ°¡ÖÂ±ΩÖêπùï–†â¡ï…µ•Õ}â}µΩâ•±•—îà§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡ï…Õ¡ïç—•ŸïÕ}ïµâÖ’ç°ï}•ëïπ—•ô•ïïÃàËÅ}ÂïÃ°¡ÖÂ±ΩÖêπùï–†â¡ï…Õ¡ïç—•ŸïÕ}ïµâÖ’ç°îà§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡Ö…çΩ’…Õ}ï—}ï·¡ï…•ïπçîàËÅ—ï·–†â¡Ö…çΩ’…Ãà∞Äƒ‘¿¿§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡…Ω©ï—}¡…ΩôïÕÕ•Ωππï∞àËÅ—ï·–†â¡…Ω©ï—}¡…ΩôïÕÕ•Ωππï∞à∞Äƒ‘¿¿§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…Ö•ÕΩπÕ}ë’}ç°Ω•·}ôΩ…µÖ—•Ωπ}çïπ—…îàËÅ—ï·–†âç°Ω•·}ôΩ…µÖ—•Ωπ}çïπ—…îà∞Äƒ»¿¿§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡ï…Õ¡ïç—•ŸïÕ}ïµ¡±Ω§àËÅ—ï·–†â¡ï…Õ¡ïç—•ŸïÕ}ïµ¡±Ω§à∞Äƒ»¿¿§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâµΩ—•ŸÖ—•Ωπ}ï—}ï±ïµïπ—Õ}çΩµ¡±ïµïπ—Ö•…ïÃàËÅ—ï·–†âµΩ—•ŸÖ—•Ω∏à∞Äƒ‡¿¿§∞(ÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÅÙ(()I5}I9}QIY%1}IEUMQ}5a}!IQILÄÙÄ»¿¿¿(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯Ωùïπï…ï»µëïµÖπëîµô–à∞Åµï—°ΩëÃılâA=MPât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}ùïπï…Ö—ï}ô…Öπçï}—…ÖŸÖ•±}…ï≈’ïÕ–°çΩπ—Öç—}•ê§Ë(ÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°±ΩÖë}ëÖ—Ñ†§∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅ•òÅπΩ–ÅÕ—»°çΩπ—Öç–πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâIïπÕï•ùπïËÅìäeÖâΩ…êÅ±ÑÅôΩ…µÖ—•Ω∏ÅÕΩ’°Ö•”•îâÙ§∞Ä–»»(ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§(ÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°¡ÖÂ±ΩÖê∞Åë•ç–§Ë(ÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅÌÙ(ÄÄÄÅôÖç—ÃÄÙÅ}ç…µ}ô…Öπçï}—…ÖŸÖ•±}…ï≈’ïÕ—}çΩπ—ï·–°çΩπ—Öç–∞Å¡ÖÂ±ΩÖê§(ÄÄÄÅÕÂÕ—ïµ}¡…Ωµ¡–ÄÙÄààâK•ë•ùîÅï∏Åô…ÖªùÖ•ÃÅ’πîÅëïµÖπëîÅëîÅô•πÖπçïµïπ–Å¡ï…Õ’ÖÕ•Ÿî∞ÅçÀ•ë•â±îÅï–Åë•…ïç—ïµïπ–ÅÖë…ïÕœ•îÉÄÅ’∏ÅçΩπÕï•±±ï»Å…ÖπçîÅQ…ÖŸÖ•∞∞ÅÖ‘ÅπΩ¥Åë‘ÅçÖπë•ëÖ–Åï–ÉÄÅ±ÑÅ¡…ïµß°…îÅ¡ï…ÕΩππî∏()1îÅ—ï·—îÅëΩ•–É©—…îÅ¡À©–ÉÄÅçΩ¡•ï»µçΩ±±ï»ÄËÅçΩµµïπçîÅ¡Ö»É
¨Å	Ωπ©Ω’»∞É
Ï∞ÅπîÅµï—ÃÅπ§ÅΩâ©ï–∞Åπ§Å—•—…î∞Åπ§Å5Ö…≠ëΩ›∏∞Åï–Å—ï…µ•πîÅ¡Ö»Å’πîÅôΩ…µ’±îÅëîÅë•Õ¡Ωπ•â•±•”§∞ÅëïÃÅ…ïµï…ç•ïµïπ—Ã∞É
¨Å	•ï∏ÅçΩ…ë•Ö±ïµïπ–∞É
ÏÅ¡’•ÃÅ±îÅπΩ¥ÅçΩµ¡±ï–Åë‘ÅçÖπë•ëÖ–Å±Ω…Õ≈◊äe•∞ÅïÕ–Å…ïπÕï•ùª§∏Å1îÅ—ï·—îÅô•πÖ∞ÅπîÅëΩ•–Å©ÖµÖ•ÃÅì•¡ÖÕÕï»Ä»Ä¿¿¿ÅçÖ…Öç”°…ïÃ∞ÅïÕ¡ÖçïÃÅçΩµ¡…•Ã∏ÅY•ÕîÄƒÄ‘¿¿ÉÄÄƒÄ‡¿¿ÅçÖ…Öç”°…ïÃ∞ÅÖŸïåÅëïÃÅ¡Ö…Öù…Ö¡°ïÃÅçΩ’…—Ã∏()·¡±•≈’îÅç±Ö•…ïµïπ–Å±ÑÅôΩ…µÖ—•Ω∏ÅÕΩ±±•ç•”•î∞Å±îÅç°Ω•‡Åë‘Åçïπ—…î∞Å±îÅ¡…Ω©ï–Å¡…ΩôïÕÕ•Ωππï∞∞Å±ÑÅçΩ£•…ïπçîÅë‘Å¡Ö…çΩ’…Ã∞Å≥äe’—•±•”§ÅçΩπçÀ°—îÅëîÅ±ÑÅôΩ…µÖ—•Ω∏Å¡Ω’»Å≥äeÖçè°ÃÉÄÅ≥äeïµ¡±Ω§Åï–Å±ÑÅµΩ—•ŸÖ—•Ω∏Åë‘ÅçÖπë•ëÖ–∏ÅYÖ±Ω…•ÕîÅ±ïÃÅÖ—Ω’—ÃÅçΩç£•ÃÅ’π•≈’ïµïπ–Å±Ω…Õ≈◊äe•±ÃÅÕΩπ–ÅŸ…Ö•Ã∏ÅM§É
¨ÅÖπç•ïπ}µ•±•—Ö•…îÉ
ÏÅïÕ–ÅŸ…Ö§∞ÅÕΩ’±•ùπîÅ±ïÃÅçΩµ√•—ïπçïÃÅ—…ÖπÕõ•…Öâ±ïÃÅÕÖπÃÅ•πŸïπ—ï»ÅìäeÖ…∑•î∞ÅëîÅù…Öëî∞ÅëîÅµ•ÕÕ•Ω∏Åπ§ÅëîÅë’À•î∏ÅM§É
¨ÅçÖ…—ï}¡…ΩôïÕÕ•Ωππï±±ï}çπÖ¡ÃÉ
ÏÅïÕ–ÅŸ…Ö§∞Åµïπ—•ΩππîÅ’πîÅçÖ…—îÅ¡…ΩôïÕÕ•Ωππï±±îÅ9ALÅÕÖπÃÅ•πŸïπ—ï»ÅÕÑÅçÖ”•ùΩ…•îÅπ§ÅÕΩ∏ÅÖπç•ïππï”§∏ÅM§ÅëïÃÅ¡ï…Õ¡ïç—•ŸïÃÅìäeïµâÖ’ç°îÅÕΩπ–Å•πë•≈◊•ïÃ∞Å…ïÕ—îÅï·Öç—ïµïπ–ÅÖ‘Åπ•ŸïÖ‘ÅëîÅ¡À•ç•Õ•Ω∏ÅôΩ’…π§∏();äe•πŸïπ—îÅÖ’ç’∏ÅôÖ•–∞Åç°•ôô…îÅëîÅµÖ…ç£§∞Åïµ¡±ΩÂï’»∞Å¡…ΩµïÕÕîÅìäeïµâÖ’ç°î∞ÅÕÖ±Ö•…î∞Åë•¡≥—µî∞Åπ•ŸïÖ‘ÅëîÅçï…—•ô•çÖ—•Ω∏∞ÅÖπç•ïππï”§∞Å…ïçΩππÖ•ÕÕÖπçîÅΩôô•ç•ï±±îÅΩ‘ÅùÖ…Öπ—•îÅëîÅ…ïç…’—ïµïπ–∏Å;äe’—•±•ÕîÅ¡ÖÃÅ±ïÃÅç°Öµ¡ÃÅŸ•ëïÃÅï–ÅπîÅ—…ÖπÕôΩ…µîÅ©ÖµÖ•ÃÅ’πîÅÕ•µ¡±îÅ•π—ïπ—•Ω∏Åï∏Åì•µÖ…ç°îÅì•´ÄÅÖççΩµ¡±•î∏Å9îÅµïπ—•ΩππîÅ¡ÖÃÅ±ïÃÅçΩπÕ•ùπïÃÅëîÅÀ•ëÖç—•Ω∏Åπ§Å±ïÃÅëΩπª•ïÃÅÕ—…’ç—’À•ïÃ∏ààà(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅùïπï…Ö—ïêÄÙÅ}ç…µ}Ö§†(ÄÄÄÄÄÄÄÄÄÄÄÅÕÂÕ—ïµ}¡…Ωµ¡–∞(ÄÄÄÄÄÄÄÄÄÄÄÅ©ÕΩ∏πë’µ¡Ã°Ïâ•πôΩ…µÖ—•ΩπÕ}ôÖç—’ï±±ïÕ}Ö’—Ω…•ÕïïÃàËÅôÖç—ÕÙ∞ÅïπÕ’…ï}ÖÕç•§ıÖ±Õî§∞(ÄÄÄÄÄÄÄÄÄÄÄÄƒƒ¿¿∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅùïπï…Ö—ïêÄÙÅÕ—»°ùïπï…Ö—ïêÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅ±ï∏°ùïπï…Ö—ïê§Ä¯ÅI5}I9}QIY%1}IEUMQ}5a}!IQILË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâ1îÅ—ï·—îÅü•ª•À§Åì•¡ÖÕÕîÅ±ÑÅ±•µ•—îÅëîÄ»Ä¿¿¿ÅçÖ…Öç”°…ïÃ∏ÅK•ü•ª•…ïËÅ’πîÅŸï…Õ•Ω∏Å¡±’ÃÅçΩ’…—î∏à(ÄÄÄÄÄÄÄÄÄÄÄÅÙ§∞Ä–»»(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâ—ï·—îàËÅùïπï…Ö—ïê∞(ÄÄÄÄÄÄÄÄÄÄÄÄâµÖ·}ç°Ö…Öç—ï…ÃàËÅI5}I9}QIY%1}IEUMQ}5a}!IQIL∞(ÄÄÄÄÄÄÄÅÙ§(ÄÄÄÅï·çï¡–Å·çï¡—•Ω∏ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅÖ¡¿π±Ωùùï»π›Ö…π•πú†(ÄÄÄÄÄÄÄÄÄÄÄÄâô…Öπçï}—…ÖŸÖ•±}…ï≈’ïÕ—}ùïπï…Ö—•Ω∏ÅçΩπ—Öç–ÙïÃÅï……Ω»ÙïÃà∞(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—}•ê∞Å—Â¡î°ï·å§π}}πÖµï}|∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1ÑÅü•ª•…Ö—•Ω∏ÅëîÅ±ÑÅëïµÖπëîÅ…ÖπçîÅQ…ÖŸÖ•∞ÅïÕ–ÅµΩµïπ—Öª•µïπ–Å•πë•Õ¡Ωπ•â±îâÙ§∞Ä‘¿»(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯ΩÕÂπ—°ïÕîà∞Åµï—°ΩëÃılâA=MPât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çΩπ—Öç—}Õ’µµÖ…‰°çΩπ—Öç—}•ê§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–ËÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅëΩÕÕ•ï»ÄÙÅÌ≠ï‰ËÅçΩπ—Öç–πùï–°≠ï‰∞Äàà§ÅôΩ»Å≠ï‰Å•∏Ä†(ÄÄÄÄÄÄÄÄâ¡…ïπΩ¥à∞ÄâπΩ¥à∞ÄâôΩ…µÖ—•Ω∏à∞Äâ±•ï‘à∞ÄâëÖ—ïÕ}ôΩ…µÖ—•Ω∏à∞ÄâÕ—Ö—’–à∞Äâç¡òà∞(ÄÄÄÄÄÄÄÄâô•πÖπçïµïπ—}ô–à∞ÄâçÖ…—ï}¡…ºà∞ÄâÖπ—ïçïëïπ—Ãà∞ÄâçΩµµïπ—Ö•…ïÃà•Ù(ÄÄÄÅëΩÕÕ•ï…lâëï…π•ï…ïÕ}Öç—•Ÿ•—ïÃâtÄÙÄ°çΩπ—Öç–πùï–†âÖç—•Ÿ•—•ïÃà§ÅΩ»Åmt•lËƒ¡t(ÄÄÄÅÖ¡¡Ω•π—µïπ—ÃÄÙÅm•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅëÖ—Ñπùï–†âç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Ãà∞Åmt§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ•—ï¥πùï–†âçΩπ—Öç—}•êà§ÄÙÙÅçΩπ—Öç—}•ët(ÄÄÄÅëΩÕÕ•ï…lâ…ïπëïÈ}ŸΩ’Õ}çÖ±ïπë±‰âtÄÙÅÕΩ…—ïê°Ö¡¡Ω•π—µïπ—Ã∞(ÄÄÄÄÄÄÄÅ≠ï‰ı±ÖµâëÑÅ•—ï¥ËÅ•—ï¥πùï–†âÕ—Ö…—}—•µîà§ÅΩ»Äàà∞Å…ïŸï…ÕîıQ…’î•lËƒ¡t(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ—ï·—îÄÙÅ}ç…µ}Ö§†âK•ë•ùîÅ’πîÅÕÂπ—£°ÕîÅI4Åì•—Ö•±≥•îÅï–ÅÕ—…’ç—’À•îÅï∏Åô…ÖªùÖ•ÃÄ†ÿÉÄÄƒ¿Å¡°…ÖÕïÃ§∏Äà(ÄÄÄÄÄÄÄÄÄÄÄÄâ%πë•≈’îÅï·¡±•ç•—ïµïπ–Å±îÅ¡…Ωç°Ö•∏Å…ïπëïËµŸΩ’ÃÅ¡À•Ÿ‘Ä°ëÖ—î∞Å°ï’…îÅï–ÅΩâ©ï–§ÅΩ‘Å≈‘ùÖ’ç’∏Å…ïπëïËµŸΩ’ÃÅ∏ùïÕ–Å¡À•Ÿ‘∏Äà(ÄÄÄÄÄÄÄÄÄÄÄÄâE’Ö±•ô•îÅ±îÅœ•…•ï’‡Åï–Å±ÑÅµÖ—’…•”§Åë‘Å¡…ΩÕ¡ïç–Å’π•≈’ïµïπ–ÉÄÅ¡Ö…—•»ÅëîÅÕ•ùπÖ’‡ÅôÖç—’ï±ÃÄ£•ç°ÖπùïÃ∞ÅÕ—Ö—’–∞Åô•πÖπçïµïπ–∞Å…ïπëïËµŸΩ’Ã∞ÅçΩµ¡≥•—’ëî§∞Äà(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡À•Õïπ—îÅ±ïÃÅ¡Ω•π—ÃÅôΩ…—Ã∞Å±ïÃÅâ±ΩçÖùïÃÅΩ‘Å•πôΩ…µÖ—•ΩπÃÅµÖπ≈’Öπ—ïÃÅï–Å—ï…µ•πîÅ¡Ö»Å±ïÃÅ¡…Ωç°Ö•πïÃÅÖç—•ΩπÃÅçΩπçÀ°—ïÃ∏Äà(ÄÄÄÄÄÄÄÄÄÄÄÄâ8ù•πŸïπ—îÅÖ’ç’πîÅ•πôΩ…µÖ—•Ω∏Åï–ÅÕ•ùπÖ±îÅç±Ö•…ïµïπ–ÅçîÅ≈’§Å∏ùïÕ–Å¡ÖÃÅ…ïπÕï•ùª§∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÅ©ÕΩ∏πë’µ¡Ã°ëΩÕÕ•ï»∞ÅïπÕ’…ï}ÖÕç•§ıÖ±Õî§∞Äÿ¿¿§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâ—ï·—îàËÅ—ï·—ïÙ§(ÄÄÄÅï·çï¡–Å·çï¡—•Ω∏ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ¡…•π–†â……ï’»ÅÕÂπ—£°ÕîÅI4Ëà∞Åï·å§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1ÑÅÕÂπ—£°ÕîÅïÕ–ÅµΩµïπ—Öª•µïπ–Å•πë•Õ¡Ωπ•â±îâÙ§∞Ä‘¿»(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯ΩÖ§µÖπÖ±ÂÕ•Ãà∞Åµï—°ΩëÃılâPà∞ÄâA=MPât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çÖπë•ëÖ—ï}Ö•}ÖπÖ±ÂÕ•Ã°çΩπ—Öç—}•ê§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅ•òÅπΩ–Å}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅ•òÅ…ï≈’ïÕ–πµï—°ΩêÄÙÙÄâPàË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ùï—}çÖπë•ëÖ—ï}Ö•}ÖπÖ±ÂÕ•Õ}Õ—Ö—î°çΩπ—Öç—}•ê∞ÅëÖ—Ñ§§(ÄÄÄÅôΩ…çîÄÙÅâΩΩ∞†°…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ§πùï–†âôΩ…çîà∞ÅÖ±Õî§§(ÄÄÄÅ›•—†Å}I5}%}91eM%M}1=-M}UIË(ÄÄÄÄÄÄÄÅ±Ωç¨ÄÙÅ}I5}%}91eM%M}1=-LπÕï—ëïôÖ’±–°çΩπ—Öç—}•ê∞Å—°…ïÖë•πúπ1Ωç¨†§§(ÄÄÄÅ›•—†Å±Ωç¨Ë(ÄÄÄÄÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÄÄÄÄÄåÅUπîÅü•ª•…Ö—•Ω∏Å%ÅïÕ–Åì•´ÄÅ’πîÅΩ√•…Ö—•Ω∏Å±Ωπù’îÄËÅï±±îÅπîÅëΩ•–Å¡ÖÃ(ÄÄÄÄÄÄÄÄåÅÖ©Ω’—ï»Å’∏ÅÕïçΩπêÅÖ¡¡ï∞ÅÀ•ÕïÖ‘ÅÕÂπç°…ΩπîÅŸï…ÃÅïÕ—•Ω∏ÅM—Öù•Ö•…ïÃ∏(ÄÄÄÄÄÄÄÅçΩπ—ï·–ÄÙÅâ’•±ë}çÖπë•ëÖ—ï}Ö•}çΩπ—ï·–°çΩπ—Öç—}•ê∞ÅëÖ—Ñ∞Åôï—ç°}±•Ÿï}ŸÖîıÖ±Õî§(ÄÄÄÄÄÄÄÅµïÖπ•πùô’∞ÄÙÄ°Öπ‰°çΩπ—ï·–πùï–°≠ï‰§ÅôΩ»Å≠ï‰Å•∏Ä†âôΩ…µÖ—•Ω∏à∞Äâô’πë•πúà∞Äâ•π—ïù…Ö—•Ωπ}ÕçΩ…ï}…ïÖë}Ωπ±‰à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ïù’±Ö—Ω…Â}ëïç±Ö…Ö—•ΩπÕ}…ïÖë}Ωπ±‰à∞Äâµï—Ö}ôΩ…µ}ÖπÕ›ï…Õ}’π—…’Õ—ïêà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ïçïπ—}πΩ—ïÕ}’π—…’Õ—ïêà∞Äâ…ïçïπ—}Öç—•Ÿ•—•ïÕ}’π—…’Õ—ïêà∞Äâ›ïëΩòà∞ÄâŸÖï}—…Öç≠•πù}…ïÖë}Ωπ±‰à§§(ÄÄÄÄÄÄÄÄÄÄÄÅΩ»ÅâΩΩ∞°çΩπ—ï·–πùï–†âÖ¡¡Ω•π—µïπ—Ãà∞ÅÌÙ§πùï–†â—Ω—Ö±}çΩ’π–à§§§(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅµïÖπ•πùô’∞Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâÕ—Ö—’ÃàËÄâ•πÕ’ôô•ç•ïπ—}ëÖ—Ñà∞ÄâµïÕÕÖùîàËÄâ1ïÃÅ•πôΩ…µÖ—•ΩπÃÅëîÅ±ÑÅ¡•Õ—îÅÕΩπ–Å•πÕ’ôô•ÕÖπ—ïÃÅ¡Ω’»Åü•ª•…ï»Å’πîÅÖπÖ±ÂÕîÅ’—•±î∏âÙ§∞Ä–»»(ÄÄÄÄÄÄÄÅÕΩ’…çï}°ÖÕ†ÄÙÅçΩµ¡’—ï}çÖπë•ëÖ—ï}Ö•}ÕΩ’…çï}°ÖÕ†°çΩπ—ï·–§(ÄÄÄÄÄÄÄÅÕ—Ω…ïêÄÙÅëÖ—ÑπÕï—ëïôÖ’±–†âç…µ}Ö•}çÖπë•ëÖ—ï}ÖπÖ±ÂÕïÃà∞ÅÌÙ§πùï–°çΩπ—Öç—}•ê§(ÄÄÄÄÄÄÄÅ•òÄ°Õ—Ω…ïêÅÖπêÅÕ—Ω…ïêπùï–†âÕΩ’…çï}°ÖÕ†à§ÄÙÙÅÕΩ’…çï}°ÖÕ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅÕ—Ω…ïêπùï–†âÖπÖ±ÂÕ•Õ}Ÿï…Õ•Ω∏à§ÄÙÙÅ%}9%Q}91eM%M}YIM%=8(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅÕ—Ω…ïêπùï–†â¡…Ωµ¡—}Ÿï…Õ•Ω∏à§ÄÙÙÅ%}9%Q}AI=5AQ}YIM%=8ÅÖπêÅπΩ–ÅôΩ…çî§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕîÄÙÅùï—}çÖπë•ëÖ—ï}Ö•}ÖπÖ±ÂÕ•Õ}Õ—Ö—î°çΩπ—Öç—}•ê∞ÅëÖ—Ñ§(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕïlâçÖç°ïêâtÄÙÅQ…’î(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°…ïÕ¡ΩπÕî§(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕ’±–ÄÙÅùïπï…Ö—ï}çÖπë•ëÖ—ï}Ö•}ÖπÖ±ÂÕ•Ã°çΩπ—ï·–§(ÄÄÄÄÄÄÄÅï·çï¡–Å·çï¡—•Ω∏ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÄåÅ9îÅ©Ω’…πÖ±•Õï»Åπ§ÅçΩπ—ï·—îÅπ§Å¡…Ωµ¡–∏ÅUπîÅÖπÖ±ÂÕîÅ±ΩçÖ±îÅì•—ï…µ•π•Õ—î(ÄÄÄÄÄÄÄÄÄÄÄÄåÅ…ïµ¡±ÖçîÅ±ÑÅÕΩ…—•îÅôΩ’…π•ÕÕï’»ÅÖô•∏ÅëîÅπîÅ©ÖµÖ•ÃÅÀ•Öôô•ç°ï»Å’∏ÅçΩπ—ïπ‘Å√•…•∑§∏(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö—’Õ}çΩëîÄÙÅùï—Ö——»°ï·å∞ÄâÕ—Ö—’Õ}çΩëîà∞Å9Ωπî§(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÖÕΩ∏ÄÙÅùï—Ö——»°ï·å∞ÄâçΩëîà∞Å9Ωπî§Å•òÅ•Õ•πÕ—Öπçî°ï·å∞ÅÖπë•ëÖ—ï%IïÕ¡ΩπÕï……Ω»§Åï±ÕîÅ9Ωπî(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï≈’ïÕ—}•êÄÙÅùï—Ö——»°ï·å∞Äâ…ï≈’ïÕ—}•êà∞Å9Ωπî§(ÄÄÄÄÄÄÄÄÄÄÄÅµΩëï∞ÄÙÅΩÃπùï—ïπÿ†â=A9%}5=0à∞Äâù¡–¥—ºµµ•π§à§(ÄÄÄÄÄÄÄÄÄÄÄÅÖ¡¿π±Ωùùï»π›Ö…π•πú†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâçÖπë•ëÖ—ï}Ö•}ÖπÖ±ÂÕ•ÃÅçΩπ—Öç–ÙïÃÅï……Ω»ÙïÃÅ…ïÖÕΩ∏ÙïÃÅ¡…ΩŸ•ëï…}Õ—Ö—’ÃÙïÃÅ…ï≈’ïÕ—}•êÙïÃÅµΩëï∞ÙïÃÅôΩ…µÖ–ı©ÕΩπ}Õç°ïµÑà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç—}•ê∞Å—Â¡î°ï·å§π}}πÖµï}|∞Å…ïÖÕΩ∏ÅΩ»Äâ¡…ΩŸ•ëï…}ï……Ω»à∞ÅÕ—Ö—’Õ}çΩëîÅΩ»ÄâπΩπîà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï≈’ïÕ—}•êÅΩ»ÄâπΩπîà∞ÅµΩëï∞§(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕ’±–ÄÙÅâ’•±ë}çÖπë•ëÖ—ï}Ö•}ôÖ±±âÖç¨°çΩπ—ï·–§(ÄÄÄÄÄÄÄÅ±Ö—ïÕ–ÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°±Ö—ïÕ–∞ÅçΩπ—Öç—}•ê§(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÄÄÄÄÅ’Õï»ÄÙÅç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ(ÄÄÄÄÄÄÄÅ±Ö—ïÕ–πÕï—ëïôÖ’±–†âç…µ}Ö•}çÖπë•ëÖ—ï}ÖπÖ±ÂÕïÃà∞ÅÌÙ•mçΩπ—Öç—}•ëtÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâÖπÖ±ÂÕ•Õ}Ÿï…Õ•Ω∏àËÅ%}9%Q}91eM%M}YIM%=8∞Äâ¡…Ωµ¡—}Ÿï…Õ•Ω∏àËÅ%}9%Q}AI=5AQ}YIM%=8∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕΩ’…çï}°ÖÕ†àËÅÕΩ’…çï}°ÖÕ†∞Äâùïπï…Ö—ïë}Ö–àËÅ}ç…µ}πΩ‹†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâùïπï…Ö—ïë}âÂ}’Õï…}•êàËÅ’Õï»πùï–†âïµÖ•∞à∞Äàà§∞Äâùïπï…Ö—ïë}âÂ}πÖµîàËÅ’Õï»πùï–†âπÖµîà∞Äã%≈’•¡îÅ%π”•ù…Ö±îà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡…ΩŸ•ëï»àËÄâ±ΩçÖ±}ôÖ±±âÖç¨àÅ•òÅ…ïÕ’±–πùï–†âôÖ±±âÖç¨à§Åï±ÕîÄâΩ¡ïπÖ§à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâµΩëï∞àËÄâëï—ï…µ•π•Õ—•åàÅ•òÅ…ïÕ’±–πùï–†âôÖ±±âÖç¨à§Åï±ÕîÅΩÃπùï—ïπÿ†â=A9%}5=0à∞Äâù¡–¥—ºµµ•π§à§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ïÕ’±–àËÅ…ïÕ’±—Ù(ÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞ÄâÖ•}ÖπÖ±ÂÕ•Ãà∞ÄâπÖ±ÂÕîÅ%Åë‘ÅçÖπë•ëÖ–ÅÖç—’Ö±•œ•îà∞(ÄÄÄÄÄÄÄÄÄÄÄÅòâA…•Ω…•”§Å¡…Ω¡Ωœ•îÄËÅÌ…ïÕ’±—lù¡…•Ω…•—Â}±Öâï∞ùuÙà§(ÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°±Ö—ïÕ–§(ÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕîÄÙÅùï—}çÖπë•ëÖ—ï}Ö•}ÖπÖ±ÂÕ•Õ}Õ—Ö—î°çΩπ—Öç—}•ê∞Å±Ö—ïÕ–§(ÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕïlâçÖç°ïêâtÄÙÅÖ±Õî(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°…ïÕ¡ΩπÕî§(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯Ωùïπï…ï»µµïÕÕÖùîà∞Åµï—°ΩëÃılâA=MPât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}ùïπï…Ö—ï}µïÕÕÖùî°çΩπ—Öç—}•ê§Ë(ÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°±ΩÖë}ëÖ—Ñ†§∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–ËÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙÏÅ≠•πêÄÙÅ¡ÖÂ±ΩÖêπùï–†â—Â¡îà§(ÄÄÄÅ•òÅ≠•πêÅπΩ–Å•∏ÅÏâïµÖ•∞à∞ÄâÕµÃâÙËÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâQÂ¡îÅ•πŸÖ±•ëîâÙ§∞Ä–¿¿(ÄÄÄÅçΩπ—ï·–ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†â•πÕ—…’ç—•ΩπÃà§ÅΩ»ÄâA…Ω¡ΩÕï»Å’∏ÅÕ’•Ÿ§ÅÖëÖ¡”§ÅÖ‘ÅëΩÕÕ•ï»∏à§πÕ—…•¿†§(ÄÄÄÅôÖç—ÃÄÙÅÌ¨ËÅçΩπ—Öç–πùï–°¨∞Äàà§ÅôΩ»Å¨Å•∏Ä†â¡…ïπΩ¥à∞ÄâôΩ…µÖ—•Ω∏à∞Äâ±•ï‘à∞ÄâÕ—Ö—’–à∞ÄâçΩµµïπ—Ö•…ïÃà•Ù(ÄÄÄÅçΩπÕ—…Ö•π–ÄÙÄâK•ë•ùîÅ’π•≈’ïµïπ–Å±îÅçΩ…¡ÃÅêù’∏ÅîµµÖ•∞Å¡…ΩôïÕÕ•Ωππï∞àÅ•òÅ≠•πêÄÙÙÄâïµÖ•∞àÅï±ÕîÄâK•ë•ùîÅ’∏ÅM5LÅ¡…ΩôïÕÕ•Ωππï∞ÅëîÄÃ»¿ÅçÖ…Öç”°…ïÃÅµÖ·•µ’¥à(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ—ï·—îÄÙÅ}ç…µ}Ö§°òâÌçΩπÕ—…Ö•π—Ù∞Åç°Ö±ï’…ï’‡Åï–Åë•…ïç—ïµïπ–Å’—•±•ÕÖâ±î∏Å8ù•πŸïπ—îÅÖ’ç’πîÅ•πôΩ…µÖ—•Ω∏∏à∞ÅòâΩÕÕ•ï»ËÅÌ©ÕΩ∏πë’µ¡Ã°ôÖç—Ã∞ÅïπÕ’…ï}ÖÕç•§ıÖ±Õî•ıqπ=â©ïç—•òËÅÌçΩπ—ï·—Ùà§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâ—ï·—îàËÅ—ï·—ïÙ§(ÄÄÄÅï·çï¡–Å·çï¡—•Ω∏ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ¡…•π–†â……ï’»Åü•ª•…Ö—•Ω∏ÅµïÕÕÖùîÅI4Ëà∞Åï·å§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1ÑÅü•ª•…Ö—•Ω∏ÅïÕ–ÅµΩµïπ—Öª•µïπ–Å•πë•Õ¡Ωπ•â±îâÙ§∞Ä‘¿»(()ëïòÅ}ç…µ}—ïµ¡±Ö—ïÕ}¡ÖÂ±ΩÖê°ëÖ—Ñ§Ë(ÄÄÄÄààâΩπÕ—…’•–Å±ÑÅâ•â±•Ω—£°≈’îÅëîÅµΩì°±ïÃÅëï¡’•ÃÅ’∏Å•πÕ—Öπ—Öª§Åï·•Õ—Öπ–∏ààà(ÄÄÄÅ›…Ö¡¡ï…}¡Ö—†ÄÙÅΩÃπ¡Ö—†π©Ω•∏°Ö¡¿π…ΩΩ—}¡Ö—†∞Äâ—ïµ¡±Ö—ïÃà∞Äâç…µ}ïµÖ•±}›…Ö¡¡ï»π°—µ∞à§(ÄÄÄÅ›•—†ÅΩ¡ï∏°›…Ö¡¡ï…}¡Ö—†∞ÅïπçΩë•πúÙâ’—ò¥‡à§ÅÖÃÅ›…Ö¡¡ï…}ô•±îË(ÄÄÄÄÄÄÄÅïµÖ•±}Õ—Ö…—ï»ÄÙÅ›…Ö¡¡ï…}ô•±îπ…ïÖê†§π…ï¡±Öçî†(ÄÄÄÄÄÄÄÄÄÄÄÄâÌÏÅçΩπ—ïπ’ÒÕÖôîÅıÙà∞(ÄÄÄÄÄÄÄÄÄÄÄÄàÑ¥¥Å5%1}=9Q9Q}MQIPÄ¥¥¯Ò¿˚%ç…•ŸïËÅ•ç§Å±îÅçΩπ—ïπ‘ÅëîÅŸΩ—…îÅîµµÖ•∞∏Ω¿¯Ñ¥¥Å5%1}=9Q9Q}9Ä¥¥¯à∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅÖ’—ΩµÖ—•ç}ïµÖ•∞ÄÙÅl(ÄÄÄÄÄÄÄÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ•êàËÄâÖ’—ΩµÖ—•åµëïÕ¿µŸÖîà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâπΩ¥àËÄâYÅM@à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏àËÄâMA}Yà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ’©ï–àËÄã¬~NtÅYÉäLÅ•…•ùïÖπ–Åìäeπ—…ï¡…•ÕîÅëîÅO•ç’…•”§ÅA…•€•îÄ°I9@–¿Ã‡‘§à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—ïπ‘àËÅâ’•±ë}ŸÖï}ëïÕ¡}ïµÖ•±}°—µ∞†âÌÏÅ¡…ïπΩ¥ÅıÙà∞ÄâÌÏÅ±•ïπ}ëïŸ•ÃÅıÙà§∞(ÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÄÄÄÄÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ•êàËÄâÖ’—ΩµÖ—•åµÑÕ¿à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâπΩ¥àËÄâÕ@ÉäLÅ	ΩëÂù’Ö…êà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏àËÄâÕ@à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ’©ï–àËÄã¬~Fªä7äfæ‚<ÅΩ…µÖ—•Ω∏Åùïπ–ÅëîÅA…Ω—ïç—•Ω∏ÅA°ÂÕ•≈’îÅëïÃÅAï…ÕΩππïÃÄ°Õ@§à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—ïπ‘àËÅâ’•±ë}ÑÕ¡}ïµÖ•±}°—µ∞†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÌÏÅ¡…ïπΩ¥ÅıÙà∞Äàà∞ÄâçΩ—ï}ÖÈ’»à∞ÄâÌÏÅ±•ïπ}ëïŸ•ÃÅıÙà∞ÅëÖ—Ñ∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÄÄÄÄÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ•êàËÄâÖ’—ΩµÖ—•åµÖ¡Ãà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâπΩ¥àËÄâALÉäLÅùïπ–ÅëîÅœ•ç’…•”§Å¡…•€•îà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏àËÄâALà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ’©ï–àËÄã¬~Fªä7äfæ‚<ÅΩ…µÖ—•Ω∏Åùïπ–ÅëîÅO•ç’…•”§ÅA…•€•îÄ°AL§à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—ïπ‘àËÅâ’•±ë}Ö¡Õ}ïµÖ•±}°—µ∞†âÌÏÅ¡…ïπΩ¥ÅıÙà∞Äàà∞ÄâçΩ—ï}ÖÈ’»à∞ÄâÌÏÅ±•ïπ}ëïŸ•ÃÅıÙà§∞(ÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÄÄÄÄÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ•êàËÄâÖ’—ΩµÖ—•åµÕÕ•Ö¿ƒà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâπΩ¥àËÄâMM%@ÄƒÉäLÅO•ç’…•”§Å•πçïπë•îà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏àËÄâMM%@à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ’©ï–àËÄã¬~RîÅΩ…µÖ—•Ω∏Åùïπ–ÅëîÅœ•ç’…•”§Å•πçïπë•îÅMM%@Äƒà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—ïπ‘àËÅâ’•±ë}ÕÕ•Ö¿≈}ïµÖ•±}°—µ∞†âÌÏÅ¡…ïπΩ¥ÅıÙà∞Äàà∞ÄâçΩ—ï}ÖÈ’»à∞ÄâÌÏÅ±•ïπ}ëïŸ•ÃÅıÙà∞ÄâΩ’§à§∞(ÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÄÄÄÄÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ•êàËÄâÖ’—ΩµÖ—•åµŸ—åà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâπΩ¥àËÄâ°Ö’ôôï’»ÅYQà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏àËÄâYQà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ’©ï–àËÄã¬~j\ÅΩ…µÖ—•Ω∏Å°Ö’ôôï’»ÅYQà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—ïπ‘àËÅâ’•±ë}Ÿ—ç}ïµÖ•±}°—µ∞†âÌÏÅ¡…ïπΩ¥ÅıÙà∞ÄâçΩ—ï}ÖÈ’»à∞ÄâÌÏÅ±•ïπ}ëïŸ•ÃÅıÙà§∞(ÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÄÄÄÄÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ•êàËÄâÖ’—ΩµÖ—•åµëïÕ¿µ•π•—•Ö∞à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâπΩ¥àËÄâM@Å•π•—•Ö∞ÉäLÅ——îÅìäeÈ’»à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏àËÄâMA}%9%Pà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ’©ï–àËÄâYΩ—…îÅëïµÖπëîÅëîÅ…ïπÕï•ùπïµïπ—ÃÉäLÅΩ…µÖ—•Ω∏ÅM@Å•π•—•Ö∞à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—ïπ‘àËÅâ’•±ë}ëïÕ¡}•π•—}ïµÖ•±}°—µ∞†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÌÏÅ¡…ïπΩ¥ÅıÙà∞Äàà∞ÄâçΩ—ï}ÖÈ’»à∞ÄâÌÏÅ±•ïπ}ëïŸ•ÃÅıÙà∞ÅëÖ—Ñ∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÄÄÄÄÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ•êàËÄâÖ’—ΩµÖ—•åµëïÕ¿µ•π•—•Ö∞µ¡Ö…•Ãà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâπΩ¥àËÄâM@Å•π•—•Ö∞ÉäLÅAÖ…•Ãà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏àËÄâMA}%9%Pà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ’©ï–àËÄâYΩ—…îÅëïµÖπëîÅëîÅ…ïπÕï•ùπïµïπ—ÃÉäLÅΩ…µÖ—•Ω∏ÅM@Å•π•—•Ö∞à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—ïπ‘àËÅâ’•±ë}ëïÕ¡}•π•—}ïµÖ•±}°—µ∞†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâÌÏÅ¡…ïπΩ¥ÅıÙà∞Äàà∞Äâ¡Ö…•Ãà∞ÄâÌÏÅ±•ïπ}ëïŸ•ÃÅıÙà∞ÅëÖ—Ñ∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÅt(ÄÄÄÅµï—Ö}ÑÕ¡}Õ’â©ïç–∞Å|∞Åµï—Ö}ÑÕ¡}°—µ∞ÄÙÅ}ÑÕ¡}•πôΩ…µÖ—•Ωπ}ïµÖ•±}çΩπ—ïπ–†(ÄÄÄÄÄÄÄÄâÌÏÅ¡…ïπΩ¥ÅıÙà∞Äàà∞ÄâçΩ—ï}ÖÈ’»à∞Äàà∞ÅëÖ—Ñ∞(ÄÄÄÄÄÄÄÅ¡…Ωµ•πïπ—}¡°Ωπï}âΩΩ≠•πúıQ…’î∞(ÄÄÄÄ§(ÄÄÄÅÖ’—ΩµÖ—•ç}µï—ÑÄÙÅl(ÄÄÄÄÄÄÄÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ•êàËÄâÖ’—ΩµÖ—•åµµï—ÑµÑÕ¿µïµÖ•∞à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ—Â¡îàËÄâïµÖ•∞à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâπΩ¥àËÄâ5QÅÕ@ÉäLÅµµÖ•∞Åìäe•πôΩ…µÖ—•Ω∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏àËÄâÕ@à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ’©ï–àËÅµï—Ö}ÑÕ¡}Õ’â©ïç–∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—ïπ‘àËÅµï—Ö}ÑÕ¡}°—µ∞∞(ÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÄÄÄÄÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ•êàËÄâÖ’—ΩµÖ—•åµµï—ÑµÑÕ¿µÕµÃà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ—Â¡îàËÄâÕµÃà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâπΩ¥àËÄâ5QÅÕ@ÉäLÅM5LÅëîÅÕ’•Ÿ§à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏àËÄâÕ@à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ’©ï–àËÄàà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—ïπ‘àËÅâ’•±ë}—…Ö•π•πù}•πôΩ…µÖ—•Ωπ}ÕµÕ}—ï·–†âÕ@à§∞(ÄÄÄÄÄÄÄÅÙ∞(ÄÄÄÅt(ÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄâïµÖ•∞àËÅëÖ—Ölâç…µ}ïµÖ•±}—ïµ¡±Ö—ïÃât∞(ÄÄÄÄÄÄÄÄâÕµÃàËÅëÖ—Ölâç…µ}ÕµÕ}—ïµ¡±Ö—ïÃât∞(ÄÄÄÄÄÄÄÄâÖ’—ΩµÖ—•ç}ïµÖ•∞àËÅÖ’—ΩµÖ—•ç}ïµÖ•∞∞(ÄÄÄÄÄÄÄÄâÖ’—ΩµÖ—•ç}µï—ÑàËÅÖ’—ΩµÖ—•ç}µï—Ñ∞(ÄÄÄÄÄÄÄÄâïµÖ•±}Õ—Ö…—ï»àËÅïµÖ•±}Õ—Ö…—ï»∞(ÄÄÄÄÄÄÄÄâïµÖ•±}ô…ïï}Õ—Ö…—ï»àËÅ…ïπëï…}—ïµ¡±Ö—î†(ÄÄÄÄÄÄÄÄÄÄÄÄâç…µ}ïµÖ•±}›…Ö¡¡ï»π°—µ∞à∞Å¡…ïπΩ¥ÙâÌÏÅ¡…ïπΩ¥ÅıÙà∞(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ…µÖ—•Ω∏ÙâÌÏÅôΩ…µÖ—•Ω∏ÅıÙà∞(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—ïπ‘ÙàÑ¥¥Å5%1}=9Q9Q}MQIPÄ¥¥¯Ñ¥¥Å5%1}=9Q9Q}9Ä¥¥¯à∞(ÄÄÄÄÄÄÄÄ§∞(ÄÄÄÅÙ(()ëïòÅ}ç…µ}…ï≈’ïÕ—}¡ÖÂ±ΩÖê†§Ë(ÄÄÄÅ•òÅ…ï≈’ïÕ–πµ•µï—Â¡îÄÙÙÄâµ’±—•¡Ö…–ΩôΩ…¥µëÖ—ÑàË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å…ï≈’ïÕ–πôΩ…¥π—Ω}ë•ç–°ô±Ö–ıQ…’î§(ÄÄÄÅ…ï—’…∏Å…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(()ëïòÅ}ç…µ}¡ÖÂ±ΩÖë}âΩΩ±ïÖ∏°ŸÖ±’î∞ÅëïôÖ’±–ıÖ±Õî§Ë(ÄÄÄÅ•òÅŸÖ±’îÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅëïôÖ’±–(ÄÄÄÅ…ï—’…∏ÅÕ—»°ŸÖ±’î§πÕ—…•¿†§πçÖÕïôΩ±ê†§Å•∏ÅÏàƒà∞Äâ—…’îà∞ÄâΩ’§à∞ÄâÂïÃà∞ÄâΩ∏âÙ(()ëïòÅ}ç…µ}Ö——Öç°µïπ—}ï……Ω…}…ïÕ¡ΩπÕî°ï·å§Ë(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÅÕ—»°ï·å•Ù§∞Ä–ƒÃÅ•òÅ•Õ•πÕ—Öπçî°ï·å∞Å=Ÿï…ô±Ω›……Ω»§Åï±ÕîÄ–¿¿(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥Ω—ïµ¡±Ö—ïÃà∞Åµï—°ΩëÃılâPà∞ÄâA=MPât§)±Ωù•π}…ï≈’•…ïê)}ç…µ}Õï…•Ö±•Èïê)ëïòÅç…µ}—ïµ¡±Ö—ïÃ†§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅ•òÅ…ï≈’ïÕ–πµï—°ΩêÄÙÙÄâPàË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°}ç…µ}—ïµ¡±Ö—ïÕ}¡ÖÂ±ΩÖê°ëÖ—Ñ§§(ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ}ç…µ}…ï≈’ïÕ—}¡ÖÂ±ΩÖê†§ÏÅ≠•πêÄÙÅ¡ÖÂ±ΩÖêπùï–†â—Â¡îà§(ÄÄÄÅ•òÅ≠•πêÅπΩ–Å•∏ÅÏâïµÖ•∞à∞ÄâÕµÃâÙËÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâQÂ¡îÅ•πŸÖ±•ëîâÙ§∞Ä–¿¿(ÄÄÄÅ’¡±ΩÖëïêÄÙÅ…ï≈’ïÕ–πô•±ïÃπùï–†âÖ——Öç°µïπ–à§(ÄÄÄÅ•òÅ≠•πêÄÙÙÄâÕµÃàÅÖπêÅ’¡±ΩÖëïêÅÖπêÅ’¡±ΩÖëïêπô•±ïπÖµîË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1ïÃÅ¡ß°çïÃÅ©Ω•π—ïÃÅÕΩπ–ÅÀ•Õï…€•ïÃÅÖ’‡ÅîµµÖ•±Ã∏âÙ§∞Ä–¿¿(ÄÄÄÅ•—ï¥ÄÙÅÏâ•êàËÅÕ—»°’’•êπ’’•ê–†§§∞ÄâπΩ¥àËÅÕ—»°¡ÖÂ±ΩÖêπùï–†âπΩ¥à∞ÄâMÖπÃÅ—•—…îà§§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ’©ï–àËÅÕ—»°¡ÖÂ±ΩÖêπùï–†âÕ’©ï–à∞Äàà§§πÕ—…•¿†§∞ÄâçΩπ—ïπ‘àËÅÕ—»°¡ÖÂ±ΩÖêπùï–†âçΩπ—ïπ‘à∞Äàà§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçÖ—ïùΩ…•îàËÅÕ—»°¡ÖÂ±ΩÖêπùï–†âçÖ—ïùΩ…•îà∞Äâ•ª•…Ö∞à§§πÕ—…•¿†§ÅΩ»Äâ•ª•…Ö∞à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ’ÕÖùï}çΩ’π–àËÄ¿∞ÄâŸï…Õ•ΩπÃàËÅmt∞Äâç…ïÖ—ïë}Ö–àËÅ}ç…µ}πΩ‹†•Ù(ÄÄÄÅÕ—Ω…ïë}Ö——Öç°µïπ–ÄÙÅ9Ωπî(ÄÄÄÅ•òÅ’¡±ΩÖëïêÅÖπêÅ’¡±ΩÖëïêπô•±ïπÖµîË(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ω…ïë}Ö——Öç°µïπ–ÄÙÅ}ç…µ}Õ—Ω…ï}ïµÖ•±}Ö——Öç°µïπ–°’¡±ΩÖëïê§(ÄÄÄÄÄÄÄÅï·çï¡–Ä°YÖ±’ï……Ω»∞Å=Ÿï…ô±Ω›……Ω»§ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å}ç…µ}Ö——Öç°µïπ—}ï……Ω…}…ïÕ¡ΩπÕî°ï·å§(ÄÄÄÄÄÄÄÅ•—ïµlâ¡•ïçï}©Ω•π—îâtÄÙÅÕ—Ω…ïë}Ö——Öç°µïπ–(ÄÄÄÅëÖ—Ömòâç…µ}Ì≠•πëı}—ïµ¡±Ö—ïÃâtπÖ¡¡ïπê°•—ï¥§(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅï·çï¡–Å·çï¡—•Ω∏Ë(ÄÄÄÄÄÄÄÅ}ç…µ}ëï±ï—ï}ïµÖ•±}Ö——Öç°µïπ–°Õ—Ω…ïë}Ö——Öç°µïπ–§(ÄÄÄÄÄÄÄÅ…Ö•Õî(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°•—ï¥§∞Ä»¿ƒ(()I5}11	-}A9%9ÄÙÄâ¡ïπë•πúà)I5}11	-}AI=MMÄÙÄâ¡…ΩçïÕÕïêà)I5}11	-}MQQUMLÄÙÅÌI5}11	-}A9%9∞ÅI5}11	-}AI=MMÙ(()ëïòÅ}ç…µ}çÖ±±âÖç≠}Õ—Ö—’Õ}—•µïÕ—Öµ¿†§Ë(ÄÄÄÅ…ï—’…∏ÅëÖ—ï—•µîπëÖ—ï—•µîππΩ‹†(ÄÄÄÄÄÄÄÅ¡Â—Ëπ—•µïÈΩπî†â’…Ω¡îΩAÖ…•Ãà§∞(ÄÄÄÄ§π•ÕΩôΩ…µÖ–°—•µïÕ¡ïåÙâµ•ç…ΩÕïçΩπëÃà§(()ëïòÅ}ç…µ}çÖ±±âÖç≠}…ï≈’ïÕ—}Õ—Ö—’Ã°ïπ—…‰§Ë(ÄÄÄÄààâIï—’…∏Å—°îÅëïë•çÖ—ïêÅÕ—Ö—’ÃÏÅ±ïùÖç‰Åùïπï…•åÉ
¨ÅQ…Ö•”§É
ÏÅÕ—ÖÂÃÅ¡ïπë•πú∏ààà(ÄÄÄÅ…Ö›}Õ—Ö—’ÃÄÙÅÕ—»°ïπ—…‰πùï–†âçÖ±±âÖç≠}Õ—Ö—’Ãà§ÅΩ»Äàà§πÕ—…•¿†§πçÖÕïôΩ±ê†§(ÄÄÄÅ•òÅ…Ö›}Õ—Ö—’ÃÅ•∏ÅÏâ¡…ΩçïÕÕïêà∞Äâ—…Ö•—îà∞Äâ—…Ö•”§âÙË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅI5}11	-}AI=MM(ÄÄÄÅ…ï—’…∏ÅI5}11	-}A9%9(()ëïòÅ}ç…µ}çÖ±±âÖç≠}…ï≈’ïÕ—}ëï—Ö•∞°ïπ—…‰§Ë(ÄÄÄÅπΩ—ïÃÄÙÅÕ—»°ïπ—…‰πùï–†âπΩ—ïÃà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅÖ¡¡Ω•π—µïπ–ÄÙÅÕ—»°ïπ—…‰πùï–†â…ëÿà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ…ï—’…∏Äâq∏àπ©Ω•∏°l(ÄÄÄÄÄÄÄÅòâïµÖπëîÄËÅÌπΩ—ïÃÅΩ»Äù’ç’πîÅ¡À•ç•Õ•Ω∏Å…ïπÕï•ùª•î∏ùÙà∞(ÄÄÄÄÄÄÄÅòâIïπëïËµŸΩ’ÃÄËÅÌÖ¡¡Ω•π—µïπ–ÅΩ»Äù9Ω∏Å…ïπÕï•ùª§ùÙà∞(ÄÄÄÅt§(()ëïòÅ}ç…µ}±ïùÖçÂ}çÖ±±âÖç≠}…ï≈’ïÕ—}ëï—Ö•∞°ïπ—…‰§Ë(ÄÄÄÄààâIïâ’•±êÅ—°îÅëï—Ö•∞Å›…•——ï∏ÅâïôΩ…îÅçÖ±±âÖç¨ÅÖç—•Ÿ•—•ïÃÅ°ÖêÅ—°ï•»ÅΩ›∏Å≠•πê∏ààà(ÄÄÄÅ…ï—’…∏Äâq∏àπ©Ω•∏°ô•±—ï»°9Ωπî∞Ål(ÄÄÄÄÄÄÄÅÕ—»°ïπ—…‰πùï–†âπΩ—ïÃà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅΩ»ÄâïµÖπëîÅëîÅ…Ö¡¡ï∞Å—…ÖπÕµ•ÕîÅ¡Ö»Å±îÅÕïçÀ•—Ö…•Ö–∏à∞(ÄÄÄÄÄÄÄÅòâIïπëïËµŸΩ’ÃÄËÅÌïπ—…Âlù…ëÿùuÙàÅ•òÅïπ—…‰πùï–†â…ëÿà§Åï±ÕîÄàà∞(ÄÄÄÅt§§(()ëïòÅ}ç…µ}çÖ±±âÖç≠}…ï≈’ïÕ—}çΩπ—Öç–°ëÖ—Ñ∞Åïπ—…‰§Ë(ÄÄÄÅÕ—Ω…ïë}çΩπ—Öç—}•êÄÙÅÕ—»°ïπ—…‰πùï–†âç…µ}çΩπ—Öç—}•êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅÕ—Ω…ïë}çΩπ—Öç—}•ê§Å•òÅÕ—Ω…ïë}çΩπ—Öç—}•êÅï±ÕîÅ9Ωπî(ÄÄÄÅ…ï—’…∏ÅçΩπ—Öç–ÅΩ»Å}Õïç…ï—Ö…•Ö—}ï·•Õ—•πù}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞Åïπ—…‰§(()ëïòÅ}ç…µ}°Âë…Ö—ï}çÖ±±âÖç≠}…ï≈’ïÕ—}Ö¡¡Ω•π—µïπ–°ëÖ—Ñ∞Åïπ—…‰∞ÅçΩπ—Öç–§Ë(ÄÄÄÄààâ-ïï¿Å—°îÅçÖ±±âÖç¨Å…Ω‹ÅÖ±•ùπïêÅ›•—†Å•—ÃÅπï·–ÅçΩπô•…µïêÅ¡°ΩπîÅâΩΩ≠•πú∏ààà(ÄÄÄÅµÖ—ç†ÄÙÅ}ç…µ}πï·—}¡°Ωπï}Ö¡¡Ω•π—µïπ–°ëÖ—Ñ∞Åïπ—…‰∞ÅçΩπ—Öç–§(ÄÄÄÅ•òÅπΩ–ÅµÖ—ç†Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÖ±Õî(ÄÄÄÅÕ—Ö…–∞ÅÖ¡¡Ω•π—µïπ–ÄÙÅµÖ—ç†(ÄÄÄÅï·¡ïç—ïêÄÙÅÏ(ÄÄÄÄÄÄÄÄâ…ëÿàËÅ}ç…µ}çÖ±ïπë±Â}ëÖ—ï—•µï}±Öâï∞°Ö¡¡Ω•π—µïπ–πùï–†âÕ—Ö…—}—•µîà§§∞(ÄÄÄÄÄÄÄÄâ…ëŸ}Õ—Ö—’ÃàËÄâÕç°ïë’±ïêà∞(ÄÄÄÄÄÄÄÄâ…ëŸ}ëÖ—îàËÅÕ—Ö…–πÕ—…ô—•µî†àïêºï¥ºïdà§∞(ÄÄÄÄÄÄÄÄâ…ëŸ}—•µîàËÅÕ—Ö…–πÕ—…ô—•µî†àï Ëï4à§∞(ÄÄÄÄÄÄÄÄâ…ëŸ}µΩëîàËÄâ¡¡ï∞Å”•≥•¡°Ωπ•≈’îà∞(ÄÄÄÄÄÄÄÄâ…ëŸ}πÖµîàËÅÖ¡¡Ω•π—µïπ–πùï–†âπÖµîà§ÅΩ»ÄâIïπëïËµŸΩ’ÃÅ”•≥•¡°Ωπ•≈’îà∞(ÄÄÄÄÄÄÄÄâ…ëŸ}°ΩÕ—}πÖµîàËÅÖ¡¡Ω•π—µïπ–πùï–†â°ΩÕ—}πÖµîà§ÅΩ»Äàà∞(ÄÄÄÄÄÄÄÄâ…ëŸ}ÕΩ’…çîàËÄâçÖ±ïπë±‰à∞(ÄÄÄÅÙ(ÄÄÄÅç°ÖπùïêÄÙÅÖ±Õî(ÄÄÄÅôΩ»Å≠ï‰∞ÅŸÖ±’îÅ•∏Åï·¡ïç—ïêπ•—ïµÃ†§Ë(ÄÄÄÄÄÄÄÅ•òÅïπ—…‰πùï–°≠ï‰§ÄÑÙÅŸÖ±’îË(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…Âm≠ïÂtÄÙÅŸÖ±’î(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅ…ï—’…∏Åç°Öπùïê(()ëïòÅ}ç…µ}ïπÕ’…ï}çÖ±±âÖç≠}…ï≈’ïÕ—}Öç—•Ÿ•—‰°çΩπ—Öç–∞Åïπ—…‰§Ë(ÄÄÄÄààâ…ïÖ—îÅΩ»Å…ï¡Ö•»Å—°îÅ©Ω’…πÖ∞Åïπ—…‰Å±•π≠ïêÅ—ºÅΩπîÅçÖ±±âÖç¨Å…ï≈’ïÕ–∏ààà(ÄÄÄÅ…ï≈’ïÕ—}•êÄÙÅÕ—»°ïπ—…‰πùï–†â•êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅÖç—•Ÿ•—•ïÃÄÙÅçΩπ—Öç–πÕï—ëïôÖ’±–†âÖç—•Ÿ•—•ïÃà∞Åmt§(ÄÄÄÅÖç—•Ÿ•—‰ÄÙÅπï·–††(ÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅÖç—•Ÿ•—•ïÃ(ÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°•—ï¥∞Åë•ç–§(ÄÄÄÄÄÄÄÅÖπêÅÕ—»°•—ï¥πùï–†âçÖ±±âÖç≠}…ï≈’ïÕ—}•êà§ÅΩ»Äàà§ÄÙÙÅ…ï≈’ïÕ—}•ê(ÄÄÄÄÄÄÄÅÖπêÅ•—ï¥πùï–†â—•—±îà§ÄÙÙÄâïµÖπëîÅëîÅ…Ö¡¡ï∞Å…óù’îà(ÄÄÄÄÄÄÄÅÖπêÅÕ—»°•—ï¥πùï–†âçÖ±±âÖç≠}ïŸïπ–à§ÅΩ»Äâ…ïçï•Ÿïêà§ÄÙÙÄâ…ïçï•Ÿïêà(ÄÄÄÄ§∞Å9Ωπî§(ÄÄÄÅ•òÅÖç—•Ÿ•—‰Å•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅ±ïùÖçÂ}ëï—Ö•±ÃÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}çÖ±±âÖç≠}…ï≈’ïÕ—}ëï—Ö•∞°ïπ—…‰§∞(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}±ïùÖçÂ}çÖ±±âÖç≠}…ï≈’ïÕ—}ëï—Ö•∞°ïπ—…‰§∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÅÖç—•Ÿ•—‰ÄÙÅπï·–††(ÄÄÄÄÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅÖç—•Ÿ•—•ïÃ(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°•—ï¥∞Åë•ç–§(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅ•—ï¥πùï–†â—•—±îà§ÄÙÙÄâïµÖπëîÅëîÅ…Ö¡¡ï∞Å…óù’îà(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅπΩ–Å•—ï¥πùï–†âçÖ±±âÖç≠}…ï≈’ïÕ—}•êà§(ÄÄÄÄÄÄÄÄÄÄÄÅÖπêÅÕ—»°•—ï¥πùï–†âëï—Ö•∞à§ÅΩ»Äàà§Å•∏Å±ïùÖçÂ}ëï—Ö•±Ã(ÄÄÄÄÄÄÄÄ§∞Å9Ωπî§((ÄÄÄÅÕ—Ö—’ÃÄÙÅ}ç…µ}çÖ±±âÖç≠}…ï≈’ïÕ—}Õ—Ö—’Ã°ïπ—…‰§(ÄÄÄÅï·¡ïç—ïêÄÙÅÏ(ÄÄÄÄÄÄÄÄâ≠•πêàËÄâëïµÖπëï}…Ö¡¡ï∞à∞(ÄÄÄÄÄÄÄÄâ—•—±îàËÄâïµÖπëîÅëîÅ…Ö¡¡ï∞Å…óù’îà∞(ÄÄÄÄÄÄÄÄâëï—Ö•∞àËÅ}ç…µ}çÖ±±âÖç≠}…ï≈’ïÕ—}ëï—Ö•∞°ïπ—…‰§∞(ÄÄÄÄÄÄÄÄâçÖ±±âÖç≠}…ï≈’ïÕ—}•êàËÅ…ï≈’ïÕ—}•ê∞(ÄÄÄÄÄÄÄÄâçÖ±±âÖç≠}Õ—Ö—’ÃàËÅÕ—Ö—’Ã∞(ÄÄÄÄÄÄÄÄâçÖ±±âÖç≠}ïŸïπ–àËÄâ…ïçï•Ÿïêà∞(ÄÄÄÅÙ(ÄÄÄÅ•òÅÖç—•Ÿ•—‰Å•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÅï·¡ïç—ïëlâ≠•πêât∞(ÄÄÄÄÄÄÄÄÄÄÄÅï·¡ïç—ïëlâ—•—±îât∞(ÄÄÄÄÄÄÄÄÄÄÄÅï·¡ïç—ïëlâëï—Ö•∞ât∞(ÄÄÄÄÄÄÄÄÄÄÄÅÖ’—°Ω…}πÖµîÙâMïçÀ•—Ö…•Ö–à∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅÖç—•Ÿ•—‰ÄÙÅÖç—•Ÿ•—•ïÕl¡t(ÄÄÄÄÄÄÄÅ•òÅïπ—…‰πùï–†âç…ïÖ—ïë}Ö–à§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÖç—•Ÿ•—ÂlâëÖ—îâtÄÙÅÕ—»°ïπ—…Âlâç…ïÖ—ïë}Ö–ât§(ÄÄÄÄÄÄÄÅÖç—•Ÿ•—‰π’¡ëÖ—î°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâçÖ±±âÖç≠}…ï≈’ïÕ—}•êàËÅ…ï≈’ïÕ—}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçÖ±±âÖç≠}Õ—Ö—’ÃàËÅÕ—Ö—’Ã∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçÖ±±âÖç≠}ïŸïπ–àËÄâ…ïçï•Ÿïêà∞(ÄÄÄÄÄÄÄÅÙ§(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅQ…’î((ÄÄÄÅç°ÖπùïêÄÙÅÖ±Õî(ÄÄÄÅôΩ»Å≠ï‰∞ÅŸÖ±’îÅ•∏Åï·¡ïç—ïêπ•—ïµÃ†§Ë(ÄÄÄÄÄÄÄÅ•òÅÖç—•Ÿ•—‰πùï–°≠ï‰§ÄÑÙÅŸÖ±’îË(ÄÄÄÄÄÄÄÄÄÄÄÅÖç—•Ÿ•—Âm≠ïÂtÄÙÅŸÖ±’î(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅ…ï—’…∏Åç°Öπùïê(()ëïòÅ}ç…µ}¡…ï¡Ö…ï}çÖ±±âÖç≠}…ï≈’ïÕ–°ëÖ—Ñ∞Åïπ—…‰§Ë(ÄÄÄÄààâ9Ω…µÖ±•ÈîÅΩπîÅ…ï≈’ïÕ–∞Å¡…ïÕï…ŸîÅ•—ÃÅ±ïÖêÅ±•π¨ÅÖπêÅ…ï¡Ö•»Å•—ÃÅ©Ω’…πÖ∞∏ààà(ÄÄÄÅç°ÖπùïêÄÙÅÖ±Õî(ÄÄÄÅ•òÅπΩ–ÅÕ—»°ïπ—…‰πùï–†â•êà§ÅΩ»Äàà§πÕ—…•¿†§Ë(ÄÄÄÄÄÄÄÅïπ—…Âlâ•êâtÄÙÅÕ—»°’’•êπ’’•ê–†§§(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅÕ—Ö—’ÃÄÙÅ}ç…µ}çÖ±±âÖç≠}…ï≈’ïÕ—}Õ—Ö—’Ã°ïπ—…‰§(ÄÄÄÅ•òÅïπ—…‰πùï–†âçÖ±±âÖç≠}Õ—Ö—’Ãà§ÄÑÙÅÕ—Ö—’ÃË(ÄÄÄÄÄÄÄÅïπ—…ÂlâçÖ±±âÖç≠}Õ—Ö—’ÃâtÄÙÅÕ—Ö—’Ã(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅ•òÅπΩ–Åïπ—…‰πùï–†âçÖ±±âÖç≠}Õ—Ö—’Õ}’¡ëÖ—ïë}Ö–à§Ë(ÄÄÄÄÄÄÄÅ•π•—•Ö±}Õ—Ö—’Õ}ëÖ—îÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…‰πùï–†âçÖ±±âÖç≠}¡…ΩçïÕÕïë}Ö–à§ÅΩ»Åïπ—…‰πùï–†âç…ïÖ—ïë}Ö–à§ÅΩ»Äàà(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ•òÅ•π•—•Ö±}Õ—Ö—’Õ}ëÖ—îË(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…ÂlâçÖ±±âÖç≠}Õ—Ö—’Õ}’¡ëÖ—ïë}Ö–âtÄÙÅÕ—»°•π•—•Ö±}Õ—Ö—’Õ}ëÖ—î§(ÄÄÄÄÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅï·¡ïç—ïë}ùïπï…•ç}Õ—Ö—’ÃÄÙÄ†(ÄÄÄÄÄÄÄÄâQ…Ö•”§àÅ•òÅÕ—Ö—’ÃÄÙÙÅI5}11	-}AI=MMÅï±ÕîÄã Å—…Ö•—ï»à(ÄÄÄÄ§(ÄÄÄÅ•òÅïπ—…‰πùï–†âÕ—Ö—’–à§ÄÑÙÅï·¡ïç—ïë}ùïπï…•ç}Õ—Ö—’ÃË(ÄÄÄÄÄÄÄÅïπ—…ÂlâÕ—Ö—’–âtÄÙÅï·¡ïç—ïë}ùïπï…•ç}Õ—Ö—’Ã(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î((ÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çÖ±±âÖç≠}…ï≈’ïÕ—}çΩπ—Öç–°ëÖ—Ñ∞Åïπ—…‰§(ÄÄÄÅçΩπ—Öç—}•êÄÙÅÕ—»°çΩπ—Öç–πùï–†â•êà§ÅΩ»Äàà§Å•òÅçΩπ—Öç–Åï±ÕîÄàà(ÄÄÄÅ•òÅÕ—»°ïπ—…‰πùï–†âç…µ}çΩπ—Öç—}•êà§ÅΩ»Äàà§ÄÑÙÅçΩπ—Öç—}•êË(ÄÄÄÄÄÄÄÅïπ—…Âlâç…µ}çΩπ—Öç—}•êâtÄÙÅçΩπ—Öç—}•ê(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅ•òÅ}ç…µ}°Âë…Ö—ï}çÖ±±âÖç≠}…ï≈’ïÕ—}Ö¡¡Ω•π—µïπ–°ëÖ—Ñ∞Åïπ—…‰∞ÅçΩπ—Öç–§Ë(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅ•òÅçΩπ—Öç–ÅÖπêÅ}ç…µ}ïπÕ’…ï}çÖ±±âÖç≠}…ï≈’ïÕ—}Öç—•Ÿ•—‰°çΩπ—Öç–∞Åïπ—…‰§Ë(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅQ…’î(ÄÄÄÅ…ï—’…∏Åç°Öπùïê∞ÅçΩπ—Öç–(()ëïòÅ}ç…µ}âÖç≠ô•±±}çÖ±±âÖç≠}…ï≈’ïÕ—Ã°ëÖ—Ñ§Ë(ÄÄÄÄààâIï¡Ö•»ÅçÖ±±âÖç¨ÅÕ—Ö—’ÕïÃÅÖπêÅ©Ω’…πÖ±ÃÅ›•—°Ω’–Åç…ïÖ—•πúÅÖπ‰ÅI4ÅçΩπ—Öç–∏ààà(ÄÄÄÅç°ÖπùïêÄÙÅÖ±Õî(ÄÄÄÅôΩ»Åïπ—…‰Å•∏ÅëÖ—Ñπùï–†âÕïç…ï—Ö…•Ö—}ëïµÖπëïÃà∞Åmt§Ë(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°ïπ—…‰∞Åë•ç–§ÅΩ»Åïπ—…‰πùï–†â—Â¡îà§ÄÑÙÄâÖ’—…îàË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅïπ—…Â}ç°Öπùïê∞Å|ÄÙÅ}ç…µ}¡…ï¡Ö…ï}çÖ±±âÖç≠}…ï≈’ïÕ–°ëÖ—Ñ∞Åïπ—…‰§(ÄÄÄÄÄÄÄÅç°ÖπùïêÄÙÅïπ—…Â}ç°ÖπùïêÅΩ»Åç°Öπùïê(ÄÄÄÅ…ï—’…∏Åç°Öπùïê(()ëïòÅ}ç…µ}çÖ±±âÖç≠}…ï≈’ïÕ—Õ}¡ÖÂ±ΩÖê°ëÖ—Ñ§Ë(ÄÄÄÄààâ·¡ΩÕîÅΩπ±‰ÅÕïç…ï—Ö…•Ö–É
¨ÅΩ—°ï»Å…ï≈’ïÕ—ÃÉ
ÏÅ—ºÅ—°îÅçÖ±±âÖç¨Å›Ω…≠Õ¡Öçî∏ààà(ÄÄÄÅçΩπ—Öç—Õ}âÂ}•êÄÙÅÏ(ÄÄÄÄÄÄÄÅÕ—»°çΩπ—Öç–πùï–†â•êà§ÅΩ»Äàà§ËÅçΩπ—Öç–(ÄÄÄÄÄÄÄÅôΩ»ÅçΩπ—Öç–Å•∏ÅëÖ—Ñπùï–†âç…µ}çΩπ—Öç—Ãà∞Åmt§(ÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç–πùï–†â•êà§(ÄÄÄÅÙ(ÄÄÄÅ…Ω›ÃÄÙÅmt(ÄÄÄÅôΩ»Åïπ—…‰Å•∏ÅëÖ—Ñπùï–†âÕïç…ï—Ö…•Ö—}ëïµÖπëïÃà∞Åmt§Ë(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å•Õ•πÕ—Öπçî°ïπ—…‰∞Åë•ç–§ÅΩ»Åïπ—…‰πùï–†â—Â¡îà§ÄÑÙÄâÖ’—…îàË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅÕ—Ω…ïë}çΩπ—Öç—}•êÄÙÅÕ—»°ïπ—…‰πùï–†âç…µ}çΩπ—Öç—}•êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÄÙÅçΩπ—Öç—Õ}âÂ}•êπùï–°Õ—Ω…ïë}çΩπ—Öç—}•ê§(ÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç–Å•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–ÄÙÅ}Õïç…ï—Ö…•Ö—}ï·•Õ—•πù}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞Åïπ—…‰§(ÄÄÄÄÄÄÄÅçΩπ—Öç—}•êÄÙÅÕ—»°çΩπ—Öç–πùï–†â•êà§ÅΩ»Äàà§Å•òÅçΩπ—Öç–Åï±ÕîÄàà(ÄÄÄÄÄÄÄÅë•Õ¡±ÖÂ}πÖµîÄÙÅÕ—»°ïπ—…‰πùï–†âπΩ¥à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Åë•Õ¡±ÖÂ}πÖµîË(ÄÄÄÄÄÄÄÄÄÄÄÅë•Õ¡±ÖÂ}πÖµîÄÙÄàÄàπ©Ω•∏°ô•±—ï»°9Ωπî∞Ål(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—»°ïπ—…‰πùï–†â¡…ïπΩ¥à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—»°ïπ—…‰πùï–†âπΩµ}ôÖµ•±±îà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄÄÄÄÅt§§(ÄÄÄÄÄÄÄÅ…Ω›ÃπÖ¡¡ïπê°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâ•êàËÅÕ—»°ïπ—…‰πùï–†â•êà§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâç…ïÖ—ïë}Ö–àËÅÕ—»°ïπ—…‰πùï–†âç…ïÖ—ïë}Ö–à§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâëÖ—îàËÅÕ—»°ïπ—…‰πùï–†âëÖ—îà§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâë•Õ¡±ÖÂ}πÖµîàËÅë•Õ¡±ÖÂ}πÖµîÅΩ»Äâ¡¡ï±Öπ–ÅπΩ∏Å…ïπÕï•ùª§à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ—ï±ï¡°ΩπîàËÅÕ—»°ïπ—…‰πùï–†â—ï±ï¡°Ωπîà§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâïµÖ•∞àËÅÕ—»°ïπ—…‰πùï–†âïµÖ•∞à§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâπΩ—ïÃàËÅÕ—»°ïπ—…‰πùï–†âπΩ—ïÃà§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ëÿàËÅÕ—»°ïπ—…‰πùï–†â…ëÿà§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ëŸ}Õ—Ö—’ÃàËÅÕ—»°ïπ—…‰πùï–†â…ëŸ}Õ—Ö—’Ãà§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ëŸ}ëÖ—îàËÅÕ—»°ïπ—…‰πùï–†â…ëŸ}ëÖ—îà§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ëŸ}—•µîàËÅÕ—»°ïπ—…‰πùï–†â…ëŸ}—•µîà§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ëŸ}µΩëîàËÅÕ—»°ïπ—…‰πùï–†â…ëŸ}µΩëîà§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ëŸ}πÖµîàËÅÕ—»°ïπ—…‰πùï–†â…ëŸ}πÖµîà§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ…ëŸ}°ΩÕ—}πÖµîàËÅÕ—»°ïπ—…‰πùï–†â…ëŸ}°ΩÕ—}πÖµîà§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩµµïπ–àËÅÕ—»°ïπ—…‰πùï–†âçÖ±±âÖç≠}çΩµµïπ–à§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩµµïπ—}’¡ëÖ—ïë}Ö–àËÅÕ—»†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅïπ—…‰πùï–†âçÖ±±âÖç≠}çΩµµïπ—}’¡ëÖ—ïë}Ö–à§ÅΩ»Äàà(ÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩµµïπ—}’¡ëÖ—ïë}â‰àËÅÕ—»†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅïπ—…‰πùï–†âçÖ±±âÖç≠}çΩµµïπ—}’¡ëÖ—ïë}â‰à§ÅΩ»Äàà(ÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâç…µ}çΩπ—Öç—}•êàËÅçΩπ—Öç—}•ê∞(ÄÄÄÄÄÄÄÄÄÄÄÄâç…µ}çΩπ—Öç—}πÖµîàËÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâÌçΩπ—Öç–πùï–†ù¡…ïπΩ¥ú∞Äúú•ÙÅÌçΩπ—Öç–πùï–†ùπΩ¥ú∞Äúú•ÙàπÕ—…•¿†§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç–Åï±ÕîÄàà(ÄÄÄÄÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâç…µ}çΩπ—Öç—}Õ—Ö—’ÃàËÅÕ—»°çΩπ—Öç–πùï–†âÕ—Ö—’–à§ÅΩ»Äàà§Å•òÅçΩπ—Öç–Åï±ÕîÄàà∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ—Ö—’ÃàËÅ}ç…µ}çÖ±±âÖç≠}…ï≈’ïÕ—}Õ—Ö—’Ã°ïπ—…‰§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡…ΩçïÕÕïë}Ö–àËÅÕ—»°ïπ—…‰πùï–†âçÖ±±âÖç≠}¡…ΩçïÕÕïë}Ö–à§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡…ΩçïÕÕïë}â‰àËÅÕ—»°ïπ—…‰πùï–†âçÖ±±âÖç≠}¡…ΩçïÕÕïë}â‰à§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÅÙ§(ÄÄÄÅ…ï—’…∏ÅÕΩ…—ïê†(ÄÄÄÄÄÄÄÅ…Ω›Ã∞(ÄÄÄÄÄÄÄÅ≠ï‰ı±ÖµâëÑÅ…Ω‹ËÄ°…Ω‹πùï–†âç…ïÖ—ïë}Ö–à§ÅΩ»Äàà∞Å…Ω‹πùï–†âëÖ—îà§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÅ…ïŸï…ÕîıQ…’î∞(ÄÄÄÄ§(()ëïòÅ}ç…µ}çÖ±±âÖç≠}¡ïπë•πù}çΩ’π–°ëÖ—Ñ§Ë(ÄÄÄÄààâΩ’π–ÅΩπ±‰Å’π¡…ΩçïÕÕïêÅÕïç…ï—Ö…•Ö–É
¨ÅΩ—°ï»Å…ï≈’ïÕ—ÃÉ
Ï∏ààà(ÄÄÄÅ…ï—’…∏ÅÕ’¥†(ÄÄÄÄÄÄÄÄƒ(ÄÄÄÄÄÄÄÅôΩ»Åïπ—…‰Å•∏ÅëÖ—Ñπùï–†âÕïç…ï—Ö…•Ö—}ëïµÖπëïÃà∞Åmt§(ÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°ïπ—…‰∞Åë•ç–§(ÄÄÄÄÄÄÄÅÖπêÅïπ—…‰πùï–†â—Â¡îà§ÄÙÙÄâÖ’—…îà(ÄÄÄÄÄÄÄÅÖπêÅ}ç…µ}çÖ±±âÖç≠}…ï≈’ïÕ—}Õ—Ö—’Ã°ïπ—…‰§ÄÑÙÅI5}11	-}AI=MM(ÄÄÄÄ§(()Ö¡¿πùï–†àΩÖ¡§Ωç…¥ΩâΩΩ—Õ—…Ö¿à§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}âΩΩ—Õ—…Ö¿†§Ë(ÄÄÄÄààâ°Ö…ùîÅ—Ω’–Å∞ùïÕ¡ÖçîÅI4ÅÖŸïåÅ’πîÅÕï’±îÅ±ïç—’…îÅë‘Åô•ç°•ï»Å)M=8∏((ÄÄÄÅ0ùÖπç•ï∏Åì•µÖ……ÖùîÅ±ÖªùÖ•–ÅÕ•‡Å…ï≈◊©—ïÃÅï∏Å¡Ö…Ö±≥°±î∏Å°Ö≈’îÅ…ï≈◊©—î(ÄÄÄÅ…ï¡Ö…ÕÖ•–Å±îÅ∑©µîÅô•ç°•ï»ÅçΩµ¡±ï–∞ÅçîÅ≈’§Åµ’±—•¡±•Ö•–Å±îÅ¡•åÅ∑•µΩ•…î∏(ÄÄÄÄààà(ÄÄÄÅÕïç—•Ω∏ÄÙÅ…ï≈’ïÕ–πÖ…ùÃπùï–†âÕïç—•Ω∏à∞Äàà§(ÄÄÄÅ’Õï…}ïµÖ•∞ÄÙÄ°ç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ§πùï–†âïµÖ•∞à∞Äàà§(ÄÄÄÅ•òÅÕïç—•Ω∏ÄÙÙÄâëïµÖπëïÃµ…Ö¡¡ï∞àË(ÄÄÄÄÄÄÄÄåÅ=πîÅπΩ∏µëïÕ—…’ç—•ŸîÅ¡ÖÕÃÅ…ï¡Ö•…ÃÅï·•Õ—•πúÅ…ï≈’ïÕ—ÃÅÖπêÅµÖ≠ïÃÅ—°ï•»(ÄÄÄÄÄÄÄÄåÅ©Ω’…πÖ∞Åïπ—…‰ÅŸ•Õ•â±îÅâïôΩ…îÅ—°îÅçÖ±±âÖç¨Å›Ω…≠Õ¡ÖçîÅ•ÃÅ…ï—’…πïê∏(ÄÄÄÄÄÄÄÅ›•—†Å}MIQI%Q}1%YIe}1=,∞Å}I5}I=9%1%Q%=9}1=,Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ω…ïë}ëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ}ç…µ}âÖç≠ô•±±}çÖ±±âÖç≠}…ï≈’ïÕ—Ã°Õ—Ω…ïë}ëÖ—Ñ§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°Õ—Ω…ïë}ëÖ—Ñ§(ÄÄÄÅ…ïÖë}µΩëï±}≠ï‰ÄÙÅ}ç…µ}…ïÖë}µΩëï±}≠ï‰†§(ÄÄÄÅâΩΩ—Õ—…Ö¡}ï—ÖúÄÙÅ°ÖÕ°±•àπÕ°Ñ»‘ÿ°…ï¡»††(ÄÄÄÄÄÄÄÅI5}MMQ}YIM%=8∞(ÄÄÄÄÄÄÄÅÕïç—•Ω∏∞(ÄÄÄÄÄÄÄÅ’Õï…}ïµÖ•∞∞(ÄÄÄÄÄÄÄÅ…ïÖë}µΩëï±}≠ï‰∞(ÄÄÄÄ§§πïπçΩëî†â’—ò¥‡à§§π°ï·ë•ùïÕ–†§(ÄÄÄÅ•òÅ…ï≈’ïÕ–π•ô}πΩπï}µÖ—ç†πçΩπ—Ö•πÃ°âΩΩ—Õ—…Ö¡}ï—Öú§Ë(ÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕîÄÙÅÖ¡¿π…ïÕ¡ΩπÕï}ç±ÖÕÃ°Õ—Ö—’ÃÙÃ¿–§(ÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕîπÕï—}ï—Öú°âΩΩ—Õ—…Ö¡}ï—Öú§(ÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕîπ°ïÖëï…ÕlâÖç°îµΩπ—…Ω∞âtÄÙÄâ¡…•ŸÖ—î∞ÅπºµçÖç°îà(ÄÄÄÄÄÄÄÅ…ï—’…∏Å…ïÕ¡ΩπÕî(ÄÄÄÅëÖ—ÑÄÙÅ}ç…µ}¡…ï¡Ö…ïë}…ïÖë}µΩëï∞†§(ÄÄÄÅçΩπ—Öç—Ã∞Å|ÄÙÅ}ç…µ}çΩπ—Öç—}Õ’µµÖ…•ïÕ}¡ÖÂ±ΩÖê†(ÄÄÄÄÄÄÄÅëÖ—Ñ∞ÅÕïç—•Ω∏ıÕïç—•Ω∏∞Å¡…ï¡Ö…ïêıQ…’î∞(ÄÄÄÄ§(ÄÄÄÅÕï——•πùÃÄÙÅ}ç…µ}Õï——•πùÕ}¡ÖÂ±ΩÖê°ëÖ—Ñ§(ÄÄÄÅÖ¡¡Ω•π—µïπ—ÃÄÙÅ}ç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Õ}¡ÖÂ±ΩÖê°ëÖ—Ñ§(ÄÄÄÅ…ïÕ¡ΩπÕîÄÙÅ©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄâçΩπ—Öç—ÃàËÅçΩπ—Öç—Ã∞(ÄÄÄÄÄÄÄÄâ—ïµ¡±Ö—ïÃàËÅ}ç…µ}—ïµ¡±Ö—ïÕ}¡ÖÂ±ΩÖê°ëÖ—Ñ§∞(ÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ωπ}ÕïÕÕ•ΩπÃàËÅùï—}’¡çΩµ•πù}ôΩ…µÖ—•Ωπ}ÕïÕÕ•ΩπÃ°ëÖ—Ñ§∞(ÄÄÄÄÄÄÄÄâπΩ—•ô•çÖ—•ΩπÃàËÅ}ç…µ}πΩ—•ô•çÖ—•ΩπÕ}¡ÖÂ±ΩÖê†(ÄÄÄÄÄÄÄÄÄÄÄÅëÖ—Ñ∞Å’Õï…}ïµÖ•∞∞(ÄÄÄÄÄÄÄÄ§∞(ÄÄÄÄÄÄÄÄâÖ¡¡Ω•π—µïπ—ÃàËÅÖ¡¡Ω•π—µïπ—ÕlâÖ¡¡Ω•π—µïπ—Ãât∞(ÄÄÄÄÄÄÄÄâçÖ±ïπë±Â}•π—ïù…Ö—•Ω∏àËÅÖ¡¡Ω•π—µïπ—Õlâ•π—ïù…Ö—•Ω∏ât∞(ÄÄÄÄÄÄÄÄâÕï——•πùÃàËÅÕï——•πùÃ∞(ÄÄÄÄÄÄÄÄâçÖ±±âÖç≠}¡ïπë•πù}çΩ’π–àËÅ}ç…µ}çÖ±±âÖç≠}¡ïπë•πù}çΩ’π–°ëÖ—Ñ§∞(ÄÄÄÄÄÄÄÄâçÖ±±âÖç≠}…ï≈’ïÕ—ÃàËÄ†(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}çÖ±±âÖç≠}…ï≈’ïÕ—Õ}¡ÖÂ±ΩÖê°ëÖ—Ñ§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅÕïç—•Ω∏ÄÙÙÄâëïµÖπëïÃµ…Ö¡¡ï∞àÅï±ÕîÅmt(ÄÄÄÄÄÄÄÄ§∞(ÄÄÄÅÙ§(ÄÄÄÅô•πÖ±}µΩëï±}≠ï‰ÄÙÅ}ç…µ}…ïÖë}µΩëï±}≠ï‰†§(ÄÄÄÅ•òÅô•πÖ±}µΩëï±}≠ï‰ÄÑÙÅ…ïÖë}µΩëï±}≠ï‰Ë(ÄÄÄÄÄÄÄÅâΩΩ—Õ—…Ö¡}ï—ÖúÄÙÅ°ÖÕ°±•àπÕ°Ñ»‘ÿ°…ï¡»††(ÄÄÄÄÄÄÄÄÄÄÄÅI5}MMQ}YIM%=8∞(ÄÄÄÄÄÄÄÄÄÄÄÅÕïç—•Ω∏∞(ÄÄÄÄÄÄÄÄÄÄÄÅ’Õï…}ïµÖ•∞∞(ÄÄÄÄÄÄÄÄÄÄÄÅô•πÖ±}µΩëï±}≠ï‰∞(ÄÄÄÄÄÄÄÄ§§πïπçΩëî†â’—ò¥‡à§§π°ï·ë•ùïÕ–†§(ÄÄÄÅ…ïÕ¡ΩπÕîπÕï—}ï—Öú°âΩΩ—Õ—…Ö¡}ï—Öú§(ÄÄÄÅ…ïÕ¡ΩπÕîπ°ïÖëï…ÕlâÖç°îµΩπ—…Ω∞âtÄÙÄâ¡…•ŸÖ—î∞ÅπºµçÖç°îà(ÄÄÄÅ…ï—’…∏Å…ïÕ¡ΩπÕî(()Ö¡¿πùï–†àΩÖ¡§Ωç…¥ΩçÖ±±âÖç¨µ…ï≈’ïÕ—Ãà§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}çÖ±±âÖç≠}…ï≈’ïÕ—Ã†§Ë(ÄÄÄÄààâIïç°Ö…ùîÅ’π•≈’ïµïπ–Å∞ùïÕ¡ÖçîÅëïÃÅëïµÖπëïÃÅëîÅ…Ö¡¡ï∞∏ààà(ÄÄÄÅ›•—†Å}MIQI%Q}1%YIe}1=,∞Å}I5}I=9%1%Q%=9}1=,Ë(ÄÄÄÄÄÄÄÅÕ—Ω…ïë}ëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÄÄÄÄÅ•òÅ}ç…µ}âÖç≠ô•±±}çÖ±±âÖç≠}…ï≈’ïÕ—Ã°Õ—Ω…ïë}ëÖ—Ñ§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°Õ—Ω…ïë}ëÖ—Ñ§(ÄÄÄÅëÖ—ÑÄÙÅ}ç…µ}¡…ï¡Ö…ïë}…ïÖë}µΩëï∞†§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄâçÖ±±âÖç≠}…ï≈’ïÕ—ÃàËÅ}ç…µ}çÖ±±âÖç≠}…ï≈’ïÕ—Õ}¡ÖÂ±ΩÖê°ëÖ—Ñ§∞(ÄÄÄÄÄÄÄÄâçÖ±±âÖç≠}¡ïπë•πù}çΩ’π–àËÅ}ç…µ}çÖ±±âÖç≠}¡ïπë•πù}çΩ’π–°ëÖ—Ñ§∞(ÄÄÄÅÙ§(()Ö¡¿π¡ΩÕ–†àΩÖ¡§Ωç…¥ΩçÖ±±âÖç¨µ…ï≈’ïÕ—ÃºÒ…ï≈’ïÕ—}•ê¯ΩçΩπŸï…–à§)±Ωù•π}…ï≈’•…ïê)}Õï…•Ö±•Èï}Õïç…ï—Ö…•Ö—}ëï±•Ÿï…‰)}ç…µ}Õï…•Ö±•Èïê)ëïòÅç…µ}çΩπŸï…—}çÖ±±âÖç≠}…ï≈’ïÕ–°…ï≈’ïÕ—}•ê§Ë(ÄÄÄÄààâ…ïÖ—îÅÖπêÅ±•π¨ÅΩπîÅI4Å±ïÖêÅ›°•±îÅ¡…ïÕï…Ÿ•πúÅ—°îÅçÖ±±âÖç¨Å…ï≈’ïÕ–∏ààà(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅïπ—…‰ÄÙÅπï·–††(ÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅëÖ—Ñπùï–†âÕïç…ï—Ö…•Ö—}ëïµÖπëïÃà∞Åmt§(ÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°•—ï¥∞Åë•ç–§(ÄÄÄÄÄÄÄÅÖπêÅ•—ï¥πùï–†â—Â¡îà§ÄÙÙÄâÖ’—…îà(ÄÄÄÄÄÄÄÅÖπêÅÕ—»°•—ï¥πùï–†â•êà§ÅΩ»Äàà§ÄÙÙÅÕ—»°…ï≈’ïÕ—}•ê§(ÄÄÄÄ§∞Å9Ωπî§(ÄÄÄÅ•òÅïπ—…‰Å•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâïµÖπëîÅëîÅ…Ö¡¡ï∞Å•π—…Ω’ŸÖâ±î∏âÙ§∞Ä–¿–((ÄÄÄÅ|∞ÅçΩπ—Öç–ÄÙÅ}ç…µ}¡…ï¡Ö…ï}çÖ±±âÖç≠}…ï≈’ïÕ–°ëÖ—Ñ∞Åïπ—…‰§(ÄÄÄÅç…ïÖ—ïêÄÙÅÖ±Õî(ÄÄÄÅ•òÅçΩπ—Öç–Å•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅ…Ö›}πÖµîÄÙÅÕ—»°ïπ—…‰πùï–†âπΩ¥à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅπÖµï}¡Ö…—ÃÄÙÅ…Ö›}πÖµîπÕ¡±•–°9Ωπî∞Äƒ§(ÄÄÄÄÄÄÄÅô•…Õ—}πÖµîÄÙÅÕ—»°ïπ—…‰πùï–†â¡…ïπΩ¥à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ±ÖÕ—}πÖµîÄÙÅÕ—»°ïπ—…‰πùï–†âπΩµ}ôÖµ•±±îà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Åô•…Õ—}πÖµîÅÖπêÅ±ï∏°πÖµï}¡Ö…—Ã§Ä¯ÄƒË(ÄÄÄÄÄÄÄÄÄÄÄÅô•…Õ—}πÖµîÄÙÅπÖµï}¡Ö…—Õl¡t(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å±ÖÕ—}πÖµîË(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅô•…Õ—}πÖµîË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ±ÖÕ—}πÖµîÄÙÅ…Ö›}πÖµî(ÄÄÄÄÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ±ÖÕ—}πÖµîÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅπÖµï}¡Ö…—Õl≈tÅ•òÅ±ï∏°πÖµï}¡Ö…—Ã§Ä¯Äƒ(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅï±ÕîÄ°πÖµï}¡Ö…—Õl¡tÅ•òÅπÖµï}¡Ö…—ÃÅï±ÕîÄâMÖπÃÅπΩ¥à§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅç…µ}¡ÖÂ±ΩÖêÄÙÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâ¡…ïπΩ¥àËÅô•…Õ—}πÖµî∞(ÄÄÄÄÄÄÄÄÄÄÄÄâπΩ¥àËÅ±ÖÕ—}πÖµî∞(ÄÄÄÄÄÄÄÄÄÄÄÄâµÖ•∞àËÅÕ—»°ïπ—…‰πùï–†âïµÖ•∞à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ—ï±ï¡°ΩπîàËÅÕ—»°ïπ—…‰πùï–†â—ï±ï¡°Ωπîà§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâôΩ…µÖ—•Ω∏àËÅÕ—»°ïπ—…‰πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕΩ’…çï}ôΩ…µ’±Ö•…îàËÄâÖÕÕ•Õ—Öπ–µÕïç…ï—Ö…•Ö–à∞(ÄÄÄÄÄÄÄÄÄÄÄÄâΩ…•ù•πîàËÄâMïçÀ•—Ö…•Ö–à∞(ÄÄÄÄÄÄÄÅÙ(ÄÄÄÄÄÄÄÅçΩπ—Öç—}çΩ’π—}âïôΩ…îÄÙÅ±ï∏°ëÖ—Ñπùï–†âç…µ}çΩπ—Öç—Ãà∞Åmt§§(ÄÄÄÄÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}ç…ïÖ—ï}çΩπ—Öç—}ô…Ωµ}Õïç…ï—Ö…•Ö–†(ÄÄÄÄÄÄÄÄÄÄÄÅëÖ—Ñ∞Åïπ—…‰∞Åç…µ}¡ÖÂ±ΩÖê∞Åç…ïÖ—ï}Ωπ}Öµâ•ù’•—‰ıQ…’î∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅç…ïÖ—ïêÄÙÅ±ï∏°ëÖ—Ñπùï–†âç…µ}çΩπ—Öç—Ãà∞Åmt§§Ä¯ÅçΩπ—Öç—}çΩ’π—}âïôΩ…î(ÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç–Å•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâ1ÑÅëïµÖπëîÅπîÅ¡ï’–Å¡ÖÃÉ©—…îÅçΩπŸï…—•îÅï∏Åô•ç°îÅI4∏à∞(ÄÄÄÄÄÄÄÄÄÄÄÅÙ§∞Ä–¿‰((ÄÄÄÅïπ—…Âlâç…µ}çΩπ—Öç—}•êâtÄÙÅÕ—»°çΩπ—Öç–πùï–†â•êà§ÅΩ»Äàà§(ÄÄÄÅ}ç…µ}¡…ï¡Ö…ï}çÖ±±âÖç≠}…ï≈’ïÕ–°ëÖ—Ñ∞Åïπ—…‰§(ÄÄÄÅ}ç…µ}ïπÕ’…ï}Õïç…ï—Ö…•Ö—}¡’â±•çÖ—•Ω∏°çΩπ—Öç–∞Åïπ—…‰§(ÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…Ω‹ÄÙÅπï·–†(ÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏Å}ç…µ}çÖ±±âÖç≠}…ï≈’ïÕ—Õ}¡ÖÂ±ΩÖê°ëÖ—Ñ§(ÄÄÄÄÄÄÄÅ•òÅ•—ïµlâ•êâtÄÙÙÅÕ—»°…ï≈’ïÕ—}•ê§(ÄÄÄÄ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄâ…ï≈’ïÕ–àËÅ…Ω‹∞(ÄÄÄÄÄÄÄÄâçΩπ—Öç–àËÅ}ç…µ}çΩπ—Öç—}ëï—Ö•±}…ïÕ¡ΩπÕî°çΩ¡‰πëïï¡çΩ¡‰°çΩπ—Öç–§∞ÅëÖ—Ñ§∞(ÄÄÄÄÄÄÄÄâç…ïÖ—ïêàËÅç…ïÖ—ïê∞(ÄÄÄÄÄÄÄÄâçÖ±±âÖç≠}¡ïπë•πù}çΩ’π–àËÅ}ç…µ}çÖ±±âÖç≠}¡ïπë•πù}çΩ’π–°ëÖ—Ñ§∞(ÄÄÄÅÙ§∞Ä»¿ƒÅ•òÅç…ïÖ—ïêÅï±ÕîÄ»¿¿(()Ö¡¿π¡Ö—ç††àΩÖ¡§Ωç…¥ΩçÖ±±âÖç¨µ…ï≈’ïÕ—ÃºÒ…ï≈’ïÕ—}•ê¯à§)±Ωù•π}…ï≈’•…ïê)}Õï…•Ö±•Èï}Õïç…ï—Ö…•Ö—}ëï±•Ÿï…‰)}ç…µ}Õï…•Ö±•Èïê)ëïòÅç…µ}çÖ±±âÖç≠}…ï≈’ïÕ–°…ï≈’ïÕ—}•ê§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅïπ—…‰ÄÙÅπï·–††(ÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅëÖ—Ñπùï–†âÕïç…ï—Ö…•Ö—}ëïµÖπëïÃà∞Åmt§(ÄÄÄÄÄÄÄÅ•òÅ•Õ•πÕ—Öπçî°•—ï¥∞Åë•ç–§(ÄÄÄÄÄÄÄÅÖπêÅ•—ï¥πùï–†â—Â¡îà§ÄÙÙÄâÖ’—…îà(ÄÄÄÄÄÄÄÅÖπêÅÕ—»°•—ï¥πùï–†â•êà§ÅΩ»Äàà§ÄÙÙÅÕ—»°…ï≈’ïÕ—}•ê§(ÄÄÄÄ§∞Å9Ωπî§(ÄÄÄÅ•òÅïπ—…‰Å•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâïµÖπëîÅëîÅ…Ö¡¡ï∞Å•π—…Ω’ŸÖâ±î∏âÙ§∞Ä–¿–((ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(ÄÄÄÅÕ—Ö—’Õ}…ï≈’ïÕ—ïêÄÙÄâÕ—Ö—’ÃàÅ•∏Å¡ÖÂ±ΩÖê(ÄÄÄÅçΩµµïπ—}…ï≈’ïÕ—ïêÄÙÄâçΩµµïπ–àÅ•∏Å¡ÖÂ±ΩÖê(ÄÄÄÅ•òÅπΩ–ÅÕ—Ö—’Õ}…ï≈’ïÕ—ïêÅÖπêÅπΩ–ÅçΩµµïπ—}…ï≈’ïÕ—ïêË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâ’ç’πîÅµΩë•ô•çÖ—•Ω∏ÅëîÅ±ÑÅëïµÖπëîÅ∏ùÑÉ•”§Å—…ÖπÕµ•Õî∏à∞(ÄÄÄÄÄÄÄÅÙ§∞Ä–¿¿((ÄÄÄÅ¡…ïŸ•Ω’Õ}Õ—Ö—’ÃÄÙÅ}ç…µ}çÖ±±âÖç≠}…ï≈’ïÕ—}Õ—Ö—’Ã°ïπ—…‰§(ÄÄÄÅ…ï≈’ïÕ—ïë}Õ—Ö—’ÃÄÙÅ¡…ïŸ•Ω’Õ}Õ—Ö—’Ã(ÄÄÄÅ•òÅÕ—Ö—’Õ}…ï≈’ïÕ—ïêË(ÄÄÄÄÄÄÄÅ…ï≈’ïÕ—ïë}Õ—Ö—’ÃÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âÕ—Ö—’Ãà§ÅΩ»Äàà§πÕ—…•¿†§π±Ω›ï»†§(ÄÄÄÅ•òÅÕ—Ö—’Õ}…ï≈’ïÕ—ïêÅÖπêÅ…ï≈’ïÕ—ïë}Õ—Ö—’ÃÅπΩ–Å•∏ÅI5}11	-}MQQUMLË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1îÅÕ—Ö—’–ÅëîÅ±ÑÅëïµÖπëîÅïÕ–Å•πŸÖ±•ëî∏âÙ§∞Ä–¿¿(ÄÄÄÅçΩµµïπ–ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âçΩµµïπ–à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅçΩµµïπ—}…ï≈’ïÕ—ïêÅÖπêÅ±ï∏°çΩµµïπ–§Ä¯Ä»¿¿¿Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâ1îÅçΩµµïπ—Ö•…îÅ•π—ï…πîÅπîÅ¡ï’–Å¡ÖÃÅì•¡ÖÕÕï»Ä»Ä¿¿¿ÅçÖ…Öç”°…ïÃ∏à∞(ÄÄÄÄÄÄÄÅÙ§∞Ä–¿¿((ÄÄÄÅπΩ‹ÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅ’Õï»ÄÙÅç’……ïπ—}’Õï»†§ÅΩ»ÅÌÙ(ÄÄÄÅÖç—Ω»ÄÙÅ’Õï»πùï–†âπÖµîà§ÅΩ»Å’Õï»πùï–†âïµÖ•∞à§ÅΩ»Äã%≈’•¡îÅ%π”•ù…Ö±îà(ÄÄÄÅ•òÅÕ—Ö—’Õ}…ï≈’ïÕ—ïêË(ÄÄÄÄÄÄÄÅïπ—…ÂlâçÖ±±âÖç≠}Õ—Ö—’ÃâtÄÙÅ…ï≈’ïÕ—ïë}Õ—Ö—’Ã(ÄÄÄÄÄÄÄÅïπ—…ÂlâçÖ±±âÖç≠}Õ—Ö—’Õ}’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}çÖ±±âÖç≠}Õ—Ö—’Õ}—•µïÕ—Öµ¿†§(ÄÄÄÄÄÄÄÅ•òÅ…ï≈’ïÕ—ïë}Õ—Ö—’ÃÄÙÙÅI5}11	-}AI=MMË(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ¡…ïŸ•Ω’Õ}Õ—Ö—’ÃÄÑÙÅ…ï≈’ïÕ—ïë}Õ—Ö—’ÃÅΩ»ÅπΩ–Åïπ—…‰πùï–†âçÖ±±âÖç≠}¡…ΩçïÕÕïë}Ö–à§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅïπ—…ÂlâçÖ±±âÖç≠}¡…ΩçïÕÕïë}Ö–âtÄÙÅπΩ‹(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅïπ—…ÂlâçÖ±±âÖç≠}¡…ΩçïÕÕïë}â‰âtÄÙÅÖç—Ω»(ÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…ÂlâçÖ±±âÖç≠}¡…ΩçïÕÕïë}Ö–âtÄÙÄàà(ÄÄÄÄÄÄÄÄÄÄÄÅïπ—…ÂlâçÖ±±âÖç≠}¡…ΩçïÕÕïë}â‰âtÄÙÄàà(ÄÄÄÅ•òÅçΩµµïπ—}…ï≈’ïÕ—ïêË(ÄÄÄÄÄÄÄÅïπ—…ÂlâçÖ±±âÖç≠}çΩµµïπ–âtÄÙÅçΩµµïπ–(ÄÄÄÄÄÄÄÅïπ—…ÂlâçÖ±±âÖç≠}çΩµµïπ—}’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}çÖ±±âÖç≠}Õ—Ö—’Õ}—•µïÕ—Öµ¿†§(ÄÄÄÄÄÄÄÅïπ—…ÂlâçÖ±±âÖç≠}çΩµµïπ—}’¡ëÖ—ïë}â‰âtÄÙÅÖç—Ω»Å•òÅçΩµµïπ–Åï±ÕîÄàà((ÄÄÄÅ|∞ÅçΩπ—Öç–ÄÙÅ}ç…µ}¡…ï¡Ö…ï}çÖ±±âÖç≠}…ï≈’ïÕ–°ëÖ—Ñ∞Åïπ—…‰§(ÄÄÄÅ•òÅÕ—Ö—’Õ}…ï≈’ïÕ—ïêÅÖπêÅ¡…ïŸ•Ω’Õ}Õ—Ö—’ÃÄÑÙÅ…ï≈’ïÕ—ïë}Õ—Ö—’ÃÅÖπêÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÅ—•—±îÄÙÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄâïµÖπëîÅëîÅ…Ö¡¡ï∞Å—…Ö•”•îà(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ…ï≈’ïÕ—ïë}Õ—Ö—’ÃÄÙÙÅI5}11	-}AI=MM(ÄÄÄÄÄÄÄÄÄÄÄÅï±ÕîÄâïµÖπëîÅëîÅ…Ö¡¡ï∞Å…Ω’Ÿï…—îà(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰†(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–∞(ÄÄÄÄÄÄÄÄÄÄÄÄâëïµÖπëï}…Ö¡¡ï∞à∞(ÄÄÄÄÄÄÄÄÄÄÄÅ—•—±î∞(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}çÖ±±âÖç≠}…ï≈’ïÕ—}ëï—Ö•∞°ïπ—…‰§∞(ÄÄÄÄÄÄÄÄÄÄÄÅÖ’—°Ω…}πÖµîıÖç—Ω»∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâÖç—•Ÿ•—•ïÃâul¡tπ’¡ëÖ—î°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâçÖ±±âÖç≠}…ï≈’ïÕ—}•êàËÅÕ—»°ïπ—…‰πùï–†â•êà§ÅΩ»Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçÖ±±âÖç≠}Õ—Ö—’ÃàËÅ…ï≈’ïÕ—ïë}Õ—Ö—’Ã∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçÖ±±âÖç≠}ïŸïπ–àËÅ…ï≈’ïÕ—ïë}Õ—Ö—’Ã∞(ÄÄÄÄÄÄÄÅÙ§(ÄÄÄÄÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅπΩ‹((ÄÄÄÅçΩπ—Öç—}…ïÕ¡ΩπÕîÄÙÄ†(ÄÄÄÄÄÄÄÅ}ç…µ}çΩπ—Öç—}ëï—Ö•±}…ïÕ¡ΩπÕî°çΩ¡‰πëïï¡çΩ¡‰°çΩπ—Öç–§∞ÅëÖ—Ñ§(ÄÄÄÄÄÄÄÅ•òÅçΩπ—Öç–Åï±ÕîÅ9Ωπî(ÄÄÄÄ§(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…Ω‹ÄÙÅπï·–†(ÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏Å}ç…µ}çÖ±±âÖç≠}…ï≈’ïÕ—Õ}¡ÖÂ±ΩÖê°ëÖ—Ñ§(ÄÄÄÄÄÄÄÅ•òÅ•—ïµlâ•êâtÄÙÙÅÕ—»°…ï≈’ïÕ—}•ê§(ÄÄÄÄ§(ÄÄÄÅ…ïÕ¡ΩπÕîÄÙÅÏ(ÄÄÄÄÄÄÄÄâ…ï≈’ïÕ–àËÅ…Ω‹∞(ÄÄÄÄÄÄÄÄâçÖ±±âÖç≠}¡ïπë•πù}çΩ’π–àËÅ}ç…µ}çÖ±±âÖç≠}¡ïπë•πù}çΩ’π–°ëÖ—Ñ§∞(ÄÄÄÅÙ(ÄÄÄÅ•òÅçΩπ—Öç—}…ïÕ¡ΩπÕîË(ÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕïlâçΩπ—Öç–âtÄÙÅçΩπ—Öç—}…ïÕ¡ΩπÕî(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°…ïÕ¡ΩπÕî§(()Ö¡¿πùï–†àΩÖ¡§Ωç…¥Ω—ïµ¡±Ö—ïÃºÒ—ïµ¡±Ö—ï}•ê¯ΩÖ——Öç°µïπ–à§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}—ïµ¡±Ö—ï}Ö——Öç°µïπ–°—ïµ¡±Ö—ï}•ê§Ë(ÄÄÄÅ—ïµ¡±Ö—îÄÙÅπï·–††(ÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏Å±ΩÖë}ëÖ—Ñ†§πùï–†âç…µ}ïµÖ•±}—ïµ¡±Ö—ïÃà∞Åmt§(ÄÄÄÄÄÄÄÅ•òÅ•—ï¥πùï–†â•êà§ÄÙÙÅ—ïµ¡±Ö—ï}•ê(ÄÄÄÄ§∞Å9Ωπî§(ÄÄÄÅ•òÅπΩ–Å—ïµ¡±Ö—îË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ5Ωì°±îÅ•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅµï—ÖëÖ—ÑÄÙÅ—ïµ¡±Ö—îπùï–†â¡•ïçï}©Ω•π—îà§(ÄÄÄÅ¡Ö—†ÄÙÅ}ç…µ}ïµÖ•±}Ö——Öç°µïπ—}¡Ö—†°µï—ÖëÖ—Ñ§(ÄÄÄÅ•òÅπΩ–Å¡Ö—†Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâAß°çîÅ©Ω•π—îÅ•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅ…ï—’…∏ÅÕïπë}ô•±î†(ÄÄÄÄÄÄÄÅ¡Ö—†∞(ÄÄÄÄÄÄÄÅÖÕ}Ö——Öç°µïπ–ıQ…’î∞(ÄÄÄÄÄÄÄÅëΩ›π±ΩÖë}πÖµîıµï—ÖëÖ—Ñπùï–†âπΩ¥à§ÅΩ»ÅΩÃπ¡Ö—†πâÖÕïπÖµî°¡Ö—†§∞(ÄÄÄÄÄÄÄÅµ•µï—Â¡îıµï—ÖëÖ—Ñπùï–†â—Â¡îà§ÅΩ»ÄâÖ¡¡±•çÖ—•Ω∏ΩΩç—ï–µÕ—…ïÖ¥à∞(ÄÄÄÄÄÄÄÅçΩπë•—•ΩπÖ∞ıQ…’î∞(ÄÄÄÄ§(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥Ω—ïµ¡±Ö—ïÃºÒ—ïµ¡±Ö—ï}•ê¯à∞Åµï—°ΩëÃılâAQ à∞Äâ1Qât§)±Ωù•π}…ï≈’•…ïê)}ç…µ}Õï…•Ö±•Èïê)ëïòÅç…µ}—ïµ¡±Ö—î°—ïµ¡±Ö—ï}•ê§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅôΩ»Å≠•πêÅ•∏Ä†âïµÖ•∞à∞ÄâÕµÃà§Ë(ÄÄÄÄÄÄÄÅ•—ïµÃÄÙÅëÖ—Ömòâç…µ}Ì≠•πëı}—ïµ¡±Ö—ïÃât(ÄÄÄÄÄÄÄÅ•—ï¥ÄÙÅπï·–†°çÖπë•ëÖ—îÅôΩ»ÅçÖπë•ëÖ—îÅ•∏Å•—ïµÃÅ•òÅçÖπë•ëÖ—îπùï–†â•êà§ÄÙÙÅ—ïµ¡±Ö—ï}•ê§∞Å9Ωπî§(ÄÄÄÄÄÄÄÅ•òÅπΩ–Å•—ï¥Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ•òÅ…ï≈’ïÕ–πµï—°ΩêÄÙÙÄâ1QàË(ÄÄÄÄÄÄÄÄÄÄÄÅΩ±ë}Ö——Öç°µïπ–ÄÙÅ•—ï¥πùï–†â¡•ïçï}©Ω•π—îà§(ÄÄÄÄÄÄÄÄÄÄÄÅ•—ïµÃπ…ïµΩŸî°•—ï¥§(ÄÄÄÄÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}ëï±ï—ï}ïµÖ•±}Ö——Öç°µïπ–°Ω±ë}Ö——Öç°µïπ–§(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Äàà∞Ä»¿–(ÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ}ç…µ}…ï≈’ïÕ—}¡ÖÂ±ΩÖê†§(ÄÄÄÄÄÄÄÅπÖµîÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âπΩ¥à∞Å•—ï¥πùï–†âπΩ¥à∞Äàà§§§πÕ—…•¿†§(ÄÄÄÄÄÄÄÅçΩπ—ïπ–ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âçΩπ—ïπ‘à∞Å•—ï¥πùï–†âçΩπ—ïπ‘à∞Äàà§§§(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅπÖµîË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1îÅπΩ¥Åë‘ÅµΩì°±îÅïÕ–ÅΩâ±•ùÖ—Ω•…îâÙ§∞Ä–¿¿(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅçΩπ—ïπ–πÕ—…•¿†§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1îÅçΩπ—ïπ‘Åë‘ÅµΩì°±îÅïÕ–ÅΩâ±•ùÖ—Ω•…îâÙ§∞Ä–¿¿(ÄÄÄÄÄÄÄÅ’¡±ΩÖëïêÄÙÅ…ï≈’ïÕ–πô•±ïÃπùï–†âÖ——Öç°µïπ–à§(ÄÄÄÄÄÄÄÅ•òÅ≠•πêÄÙÙÄâÕµÃàÅÖπêÅ’¡±ΩÖëïêÅÖπêÅ’¡±ΩÖëïêπô•±ïπÖµîË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1ïÃÅ¡ß°çïÃÅ©Ω•π—ïÃÅÕΩπ–ÅÀ•Õï…€•ïÃÅÖ’‡ÅîµµÖ•±Ã∏âÙ§∞Ä–¿¿(ÄÄÄÄÄÄÄÅπï›}Ö——Öç°µïπ–ÄÙÅ9Ωπî(ÄÄÄÄÄÄÄÅ•òÅ≠•πêÄÙÙÄâïµÖ•∞àÅÖπêÅ’¡±ΩÖëïêÅÖπêÅ’¡±ΩÖëïêπô•±ïπÖµîË(ÄÄÄÄÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅπï›}Ö——Öç°µïπ–ÄÙÅ}ç…µ}Õ—Ω…ï}ïµÖ•±}Ö——Öç°µïπ–°’¡±ΩÖëïê§(ÄÄÄÄÄÄÄÄÄÄÄÅï·çï¡–Ä°YÖ±’ï……Ω»∞Å=Ÿï…ô±Ω›……Ω»§ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å}ç…µ}Ö——Öç°µïπ—}ï……Ω…}…ïÕ¡ΩπÕî°ï·å§(ÄÄÄÄÄÄÄÅΩ±ë}Ö——Öç°µïπ–ÄÙÅ•—ï¥πùï–†â¡•ïçï}©Ω•π—îà§(ÄÄÄÄÄÄÄÅ•—ï¥πÕï—ëïôÖ’±–†âŸï…Õ•ΩπÃà∞Åmt§π•πÕï…–†¿∞ÅÏ(ÄÄÄÄÄÄÄÄÄÄÄÄâπΩ¥àËÅ•—ï¥πùï–†âπΩ¥à∞Äàà§∞ÄâÕ’©ï–àËÅ•—ï¥πùï–†âÕ’©ï–à∞Äàà§∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—ïπ‘àËÅ•—ï¥πùï–†âçΩπ—ïπ‘à∞Äàà§∞ÄâëÖ—îàËÅ•—ï¥πùï–†â’¡ëÖ—ïë}Ö–à§ÅΩ»Å•—ï¥πùï–†âç…ïÖ—ïë}Ö–à§ÅΩ»Å}ç…µ}πΩ‹†§∞(ÄÄÄÄÄÄÄÅÙ§(ÄÄÄÄÄÄÄÅ•—ïµlâŸï…Õ•ΩπÃâtÄÙÅ•—ïµlâŸï…Õ•ΩπÃâulË»¡t(ÄÄÄÄÄÄÄÅ•—ï¥π’¡ëÖ—î°ÏâπΩ¥àËÅπÖµî∞ÄâÕ’©ï–àËÅÕ—»°¡ÖÂ±ΩÖêπùï–†âÕ’©ï–à∞Å•—ï¥πùï–†âÕ’©ï–à∞Äàà§§§πÕ—…•¿†§Å•òÅ≠•πêÄÙÙÄâïµÖ•∞àÅï±ÕîÄàà∞ÄâçΩπ—ïπ‘àËÅçΩπ—ïπ–∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâçÖ—ïùΩ…•îàËÅÕ—»°¡ÖÂ±ΩÖêπùï–†âçÖ—ïùΩ…•îà∞Å•—ï¥πùï–†âçÖ—ïùΩ…•îà∞Äâ•ª•…Ö∞à§§§πÕ—…•¿†§ÅΩ»Äâ•ª•…Ö∞à∞Äâ’¡ëÖ—ïë}Ö–àËÅ}ç…µ}πΩ‹†•Ù§(ÄÄÄÄÄÄÄÅ•òÅπï›}Ö——Öç°µïπ–Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ•—ïµlâ¡•ïçï}©Ω•π—îâtÄÙÅπï›}Ö——Öç°µïπ–(ÄÄÄÄÄÄÄÅï±•òÅ≠•πêÄÙÙÄâïµÖ•∞àÅÖπêÅ}ç…µ}¡ÖÂ±ΩÖë}âΩΩ±ïÖ∏°¡ÖÂ±ΩÖêπùï–†â…ïµΩŸï}Ö——Öç°µïπ–à§§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ•—ï¥π¡Ω¿†â¡•ïçï}©Ω•π—îà∞Å9Ωπî§(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÄÄÄÄÅï·çï¡–Å·çï¡—•Ω∏Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}ëï±ï—ï}ïµÖ•±}Ö——Öç°µïπ–°πï›}Ö——Öç°µïπ–§(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•Õî(ÄÄÄÄÄÄÄÅ•òÅΩ±ë}Ö——Öç°µïπ–ÅÖπêÅΩ±ë}Ö——Öç°µïπ–ÄÑÙÅ•—ï¥πùï–†â¡•ïçï}©Ω•π—îà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ}ç…µ}ëï±ï—ï}ïµÖ•±}Ö——Öç°µïπ–°Ω±ë}Ö——Öç°µïπ–§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°•—ï¥§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ5Ωì°±îÅ•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(()I5}UA=5%9}QM}YI%	1ÄÙÄâÌÌ¡…Ωç°Ö•πïÕ}ëÖ—ïÕıÙà)I5}191e}UI0ÄÙÄâ°——¡ÃËºΩçÖ±ïπë±‰πçΩ¥Ω•π—ïù…Ö±ïÖçÖëïµ‰ΩôΩ…µÖ—•Ω∏à(()ëïòÅ}ç…µ}ôΩ…µÖ—•Ωπ}çΩëî°çΩπ—Öç–§Ë(ÄÄÄÄààâQ…ÖπÕ±Ö—îÅ—°îÅI4ùÃÅ°’µÖ∏Å±Öâï±ÃÅ—ºÅ—°îÅÕïÕÕ•Ω∏ÅÖëµ•π•Õ—…Ö—•Ω∏ÅçΩëïÃ∏ààà(ÄÄÄÅôΩ…µÖ—•Ω∏ÄÙÅÕ—»°çΩπ—Öç–πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§π±Ω›ï»†§(ÄÄÄÅ•òÅôΩ…µÖ—•Ω∏ÄÙÙÄâëïÕ¿àË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÄâMA}YàÅ•òÅÕ—»°çΩπ—Öç–πùï–†âëïÕ¡}—Â¡îà§ÅΩ»Äàà§πÕ—…•¿†§π’¡¡ï»†§ÄÙÙÄâYàÅï±ÕîÄâMA}%9%Pà(ÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄâÖ¡ÃàËÄâALà∞ÄâÑÕ¿àËÄâÕ@à∞ÄâÕÕ•Ö¿àËÄâMM%@à∞ÄâÕÕ•Ö¿ÄƒàËÄâMM%@à∞(ÄÄÄÄÄÄÄÄâç°Ö’ôôï’»ÅŸ—åàËÄâYQà∞ÄâŸ—åàËÄâYQà∞(ÄÄÄÅÙπùï–°ôΩ…µÖ—•Ω∏∞ÅÕ—»°çΩπ—Öç–πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§π’¡¡ï»†§§(()ëïòÅ}ç…µ}’¡çΩµ•πù}ëÖ—ïÃ°çΩπ—Öç–∞Å°—µ∞ıÖ±Õî∞ÅëÖ—Ö}Õ—Ω…îı9Ωπî§Ë(ÄÄÄÅçïπ—…îÄÙÅ}πΩ…µÖ±•Èï}çïπ—…ï}çΩëî°çΩπ—Öç–πùï–†â±•ï‘à§§(ÄÄÄÅôΩ…µÖ—•Ω∏ÄÙÅ}ç…µ}ôΩ…µÖ—•Ωπ}çΩëî°çΩπ—Öç–§(ÄÄÄÅ…Ω›ÃÄÙÅùï—}’¡çΩµ•πù}ôΩ…µÖ—•Ωπ}ÕïÕÕ•ΩπÃ°ëÖ—Ö}Õ—Ω…î§πùï–°çïπ—…î∞ÅÌÙ§πùï–°ôΩ…µÖ—•Ω∏∞Åmt§(ÄÄÄÅ±Öâï±ÃÄÙÅl(ÄÄÄÄÄÄÄÅÕ—»°…Ω‹πùï–†â±Öâï∞à§ÅΩ»Äàà§πÕ—…•¿†§π…ï¡±Öçî†àÄ¥Åï·Öµï∏Å±îÄà∞ÄàÉäPÅï·Öµï∏Å±îÄà§(ÄÄÄÄÄÄÄÅôΩ»Å…Ω‹Å•∏Å…Ω›ÃÅ•òÅ•Õ•πÕ—Öπçî°…Ω‹∞Åë•ç–§ÅÖπêÅÕ—»°…Ω‹πùï–†â±Öâï∞à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅt(ÄÄÄÅ•òÅπΩ–Å±Öâï±ÃË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÄâÖ—ïÃÉÄÅŸïπ•»Å¡…Ωç°Ö•πïµïπ–Ä°çΩπ—Öç—ïËµπΩ’ÃÅ¡Ω’»Å±ïÃÅçΩππáπ—…î§∏à(ÄÄÄÅ•òÅπΩ–Å°—µ∞Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Äâq∏àπ©Ω•∏°òãäàÅÌ±Öâï±ÙàÅôΩ»Å±Öâï∞Å•∏Å±Öâï±Ã§(ÄÄÄÅ…ï—’…∏ÄúÒ’∞ÅÕ—Â±îÙâµÖ…ù•∏Ë·¡‡Ä¿Ä·¡‡Ä»¡¡‡Ì¡Öëë•πúË¿Ïà¯úÄ¨Äààπ©Ω•∏†(ÄÄÄÄÄÄÄÅòúÒ±§ÅÕ—Â±îÙâµÖ…ù•∏Ë¿Ä¿ÄŸ¡‡Ä¿Ïà¯ÒÕ—…Ωπú˘Ì°—µ±}µΩë’±îπïÕçÖ¡î°±Öâï∞•ÙΩÕ—…Ωπú¯Ω±§¯ú(ÄÄÄÄÄÄÄÅôΩ»Å±Öâï∞Å•∏Å±Öâï±Ã(ÄÄÄÄ§Ä¨ÄàΩ’∞¯à(()ëïòÅ}ç…µ}çÖ±ïπë±Â}’…∞°çΩπ—Öç–§Ë(ÄÄÄÄààâIï—’…∏Å—°îÅâΩΩ≠•πúÅ¡ÖùîÅµÖ—ç°•πúÅ—°îÅ—…Ö•π•πúÅÕï±ïç—ïêÅΩ∏Å—°îÅçΩπ—Öç–∏ààà(ÄÄÄÅôΩ…µÖ—•Ω∏ÄÙÅMIQI%Q}=I5Q%=9Lπùï–°}ç…µ}ôΩ…µÖ—•Ωπ}çΩëî°çΩπ—Öç–§∞ÅÌÙ§(ÄÄÄÅ…ï—’…∏ÅôΩ…µÖ—•Ω∏πùï–†âçÖ±ïπë±‰à§ÅΩ»ÅI5}191e}UI0(()ëïòÅ}ç…µ}ôΩ…µÖ—•Ωπ}±Öâï∞°çΩπ—Öç–§Ë(ÄÄÄÄààâIï—’…∏Å—°îÅçΩµ¡±ï—î∞Åç’Õ—Ωµï»µôÖç•πúÅπÖµîÅ’ÕïêÅ•∏ÅµïÕÕÖùîÅ—ïµ¡±Ö—ïÃ∏ààà(ÄÄÄÅôΩ…µÖ—•Ωπ}çΩëîÄÙÅ}ç…µ}ôΩ…µÖ—•Ωπ}çΩëî°çΩπ—Öç–§(ÄÄÄÅ±Öâï±ÃÄÙÅÏ(ÄÄÄÄÄÄÄÄâÕ@àËÄâùïπ–ÅëîÅ¡…Ω—ïç—•Ω∏Å¡°ÂÕ•≈’îÅëïÃÅ¡ï…ÕΩππïÃÄ°Õ@§à∞(ÄÄÄÄÄÄÄÄâALàËÄâùïπ–ÅëîÅ¡À•Ÿïπ—•Ω∏Åï–ÅëîÅœ•ç’…•”§Ä°AL§à∞(ÄÄÄÄÄÄÄÄâMM%@àËÄâùïπ–ÅëîÅœ•ç’…•”§Å•πçïπë•îÄ°MM%@Äƒ§à∞(ÄÄÄÄÄÄÄÄâYQàËÄâ°Ö’ôôï’»ÅëîÅ—…ÖπÕ¡Ω…–ÅÖŸïåÅç°Ö’ôôï’»Ä°YQ§à∞(ÄÄÄÄÄÄÄÄâMA}%9%PàËÄâ•…•ùïÖπ–Åìäeïπ—…ï¡…•ÕîÅëîÅœ•ç’…•”§Å¡…•€•îÄ°M@ÉäLÅ•π•—•Ö∞§à∞(ÄÄÄÄÄÄÄÄâMA}YàËÄâ•…•ùïÖπ–Åìäeïπ—…ï¡…•ÕîÅëîÅœ•ç’…•”§Å¡…•€•îÄ°M@ÉäLÅY§à∞(ÄÄÄÅÙ(ÄÄÄÅ…ï—’…∏Å±Öâï±Ãπùï–°ôΩ…µÖ—•Ωπ}çΩëî§ÅΩ»ÅÕ—»°çΩπ—Öç–πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§(()ëïòÅ}ç…µ}—ΩëÖÂ}Ö¡¡Ω•π—µïπ—}ŸÖ…•Öâ±ïÃ°çΩπ—Öç–∞ÅëÖ—Ö}Õ—Ω…îı9Ωπî∞ÅπΩ‹ı9Ωπî§Ë(ÄÄÄÄààâIï—’…∏Å—°îÅëÖ—îΩ—•µîÅΩòÅ—°îÅ±Ö—ïÕ–ÅÖ¡¡Ω•π—µïπ–ÅµÖ…≠ïêÅ’πÖπÕ›ï…ïê∏ààà(ÄÄÄÅ¡Ö…•ÃÄÙÅ¡Â—Ëπ—•µïÈΩπî†â’…Ω¡îΩAÖ…•Ãà§(ÄÄÄÅπΩ‹ÄÙÅπΩ‹ÅΩ»ÅëÖ—ï—•µîπëÖ—ï—•µîππΩ‹°¡Ö…•Ã§(ÄÄÄÅ•òÅπΩ‹π—È•πôºÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅπΩ‹ÄÙÅ¡Ö…•Ãπ±ΩçÖ±•Èî°πΩ‹§(ÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÅπΩ‹ÄÙÅπΩ‹πÖÕ—•µïÈΩπî°¡Ö…•Ã§(ÄÄÄÅçΩπ—Öç—}•êÄÙÅÕ—»°çΩπ—Öç–πùï–†â•êà§ÅΩ»Äàà§(ÄÄÄÅµÖ—ç°ïÃÄÙÅmt(ÄÄÄÅôΩ»ÅÖ¡¡Ω•π—µïπ–Å•∏Ä°ëÖ—Ö}Õ—Ω…îÅΩ»ÅÌÙ§πùï–†âç…µ}çÖ±ïπë±Â}Ö¡¡Ω•π—µïπ—Ãà∞Åmt§Ë(ÄÄÄÄÄÄÄÅ•òÅÕ—»°Ö¡¡Ω•π—µïπ–πùï–†âçΩπ—Öç—}•êà§ÅΩ»Äàà§ÄÑÙÅçΩπ—Öç—}•êË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ•òÅÕ—»°Ö¡¡Ω•π—µïπ–πùï–†âÕ—Ö—’Ãà§ÅΩ»ÄâÖç—•Ÿîà§πçÖÕïôΩ±ê†§Å•∏ÅÏâçÖπçï±ïêà∞ÄâçÖπçï±±ïêâÙË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ•òÅÖ¡¡Ω•π—µïπ–πùï–†â…ïÕ¡ΩπÕï}Õ—Ö—’Ãà§ÄÑÙÄâπΩ}ÖπÕ›ï»àË(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö…–ÄÙÅëÖ—ï—•µîπëÖ—ï—•µîπô…Ωµ•ÕΩôΩ…µÖ–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—»°Ö¡¡Ω•π—µïπ–πùï–†âÕ—Ö…—}—•µîà§ÅΩ»Äàà§π…ï¡±Öçî†âhà∞Äà¨¿¿Ë¿¿à§(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅÕ—Ö…–π—È•πôºÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö…–ÄÙÅ¡Â—ËπUQπ±ΩçÖ±•Èî°Õ—Ö…–§(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö…–ÄÙÅÕ—Ö…–πÖÕ—•µïÈΩπî°¡Ö…•Ã§(ÄÄÄÄÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—•π’î(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö—’Õ}’¡ëÖ—ïë}Ö–ÄÙÅëÖ—ï—•µîπëÖ—ï—•µîπô…Ωµ•ÕΩôΩ…µÖ–†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—»°Ö¡¡Ω•π—µïπ–πùï–†â…ïÕ¡ΩπÕï}Õ—Ö—’Õ}’¡ëÖ—ïë}Ö–à§ÅΩ»Äàà§π…ï¡±Öçî†âhà∞Äà¨¿¿Ë¿¿à§(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅÕ—Ö—’Õ}’¡ëÖ—ïë}Ö–π—È•πôºÅ•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö—’Õ}’¡ëÖ—ïë}Ö–ÄÙÅ¡Â—ËπUQπ±ΩçÖ±•Èî°Õ—Ö—’Õ}’¡ëÖ—ïë}Ö–§(ÄÄÄÄÄÄÄÅï·çï¡–Ä°QÂ¡ï……Ω»∞ÅYÖ±’ï……Ω»§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅÕ—Ö—’Õ}’¡ëÖ—ïë}Ö–ÄÙÅÕ—Ö…–(ÄÄÄÄÄÄÄÅµÖ—ç°ïÃπÖ¡¡ïπê†°Õ—Ö—’Õ}’¡ëÖ—ïë}Ö–∞ÅÕ—Ö…–§§(ÄÄÄÅ•òÅπΩ–ÅµÖ—ç°ïÃË(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏâëÖ—ï}…ëŸ}ë’}©Ω’»àËÄàà∞Äâ°ï’…ï}…ëŸ}ë’}©Ω’»àËÄàà∞ÄâëÖ—ï}°ï’…ï}…ëŸ}ë’}©Ω’»àËÄàâÙ((ÄÄÄÄåÅÅÅ…ïÕ¡ΩπÕï}Õ—Ö—’Õ}’¡ëÖ—ïë}Ö—ÅÄÅ•ëïπ—•ô•ïÃÅ—°îÅÖ¡¡Ω•π—µïπ–ÅΩ∏Å›°•ç†Å—°î(ÄÄÄÄåÅ’Õï»ÅµΩÕ–Å…ïçïπ—±‰Åç±•ç≠ïêÉäqMÖπÃÅÀ•¡ΩπÕóät∞Å…ïùÖ…ë±ïÕÃÅΩòÅ•—ÃÅÖùî∏(ÄÄÄÅÕ—Ö…–ÄÙÅµÖ‡°µÖ—ç°ïÃ∞Å≠ï‰ı±ÖµâëÑÅ•—ï¥ËÅ•—ïµl¡t•l≈t(ÄÄÄÅµΩπ—°ÃÄÙÄ†â©ÖπŸ•ï»à∞Äâõ•Ÿ…•ï»à∞ÄâµÖ…Ãà∞ÄâÖŸ…•∞à∞ÄâµÖ§à∞Äâ©’•∏à∞Äâ©’•±±ï–à∞ÄâÖøÌ–à∞ÄâÕï¡—ïµâ…îà∞ÄâΩç—Ωâ…îà∞ÄâπΩŸïµâ…îà∞Äâì•çïµâ…îà§(ÄÄÄÅëÖ—ï}±Öâï∞ÄÙÅòâÌÕ—Ö…–πëÖÂÙÅÌµΩπ—°ÕmÕ—Ö…–πµΩπ—†Ä¥Ä≈uÙÅÌÕ—Ö…–πÂïÖ…Ùà(ÄÄÄÅ—•µï}±Öâï∞ÄÙÅòâÌÕ—Ö…–π°Ω’…ı†àÄ¨Ä°òâÌÕ—Ö…–πµ•π’—îË¿…ëÙàÅ•òÅÕ—Ö…–πµ•π’—îÅï±ÕîÄàà§(ÄÄÄÅ…ï—’…∏ÅÏ(ÄÄÄÄÄÄÄÄâëÖ—ï}…ëŸ}ë’}©Ω’»àËÅëÖ—ï}±Öâï∞∞(ÄÄÄÄÄÄÄÄâ°ï’…ï}…ëŸ}ë’}©Ω’»àËÅ—•µï}±Öâï∞∞(ÄÄÄÄÄÄÄÄâëÖ—ï}°ï’…ï}…ëŸ}ë’}©Ω’»àËÅòâÌëÖ—ï}±Öâï±ÙÉÄÅÌ—•µï}±Öâï±Ùà∞(ÄÄÄÅÙ(()ëïòÅ}ç…µ}…ïÕΩ±Ÿï}µïÕÕÖùï}ŸÖ…•Öâ±ïÃ°çΩπ—ïπ–∞ÅçΩπ—Öç–∞Å°—µ∞ıÖ±Õî∞ÅëÖ—Ö}Õ—Ω…îı9Ωπî§Ë(ÄÄÄÄààâIïÕΩ±ŸîÅI4Å—ïµ¡±Ö—îÅŸÖ…•Öâ±ïÃÅÖ–Å¡…ïŸ•ï‹ΩÕïπêÅ—•µî∞ÅπïŸï»Å›°ï∏ÅÕÖŸ•πú∏ààà(ÄÄÄÅ…ïÕΩ±ŸïêÄÙÅÕ—»°çΩπ—ïπ–ÅΩ»Äàà§π…ï¡±Öçî†(ÄÄÄÄÄÄÄÅI5}UA=5%9}QM}YI%	1∞(ÄÄÄÄÄÄÄÅ}ç…µ}’¡çΩµ•πù}ëÖ—ïÃ°çΩπ—Öç–∞Å°—µ∞ı°—µ∞∞ÅëÖ—Ö}Õ—Ω…îıëÖ—Ö}Õ—Ω…î§∞(ÄÄÄÄ§(ÄÄÄÅŸÖ…•Öâ±ïÃÄÙÅÏ(ÄÄÄÄÄÄÄÄâ¡…ïπΩ¥àËÅçΩπ—Öç–πùï–†â¡…ïπΩ¥à§∞ÄâπΩ¥àËÅçΩπ—Öç–πùï–†âπΩ¥à§∞(ÄÄÄÄÄÄÄÄâïµÖ•∞àËÅçΩπ—Öç–πùï–†âµÖ•∞à§∞ÄâµÖ•∞àËÅçΩπ—Öç–πùï–†âµÖ•∞à§∞(ÄÄÄÄÄÄÄÄâ—ï±ï¡°ΩπîàËÅçΩπ—Öç–πùï–†â—ï±ï¡°Ωπîà§∞ÄâôΩ…µÖ—•Ω∏àËÅ}ç…µ}ôΩ…µÖ—•Ωπ}±Öâï∞°çΩπ—Öç–§∞(ÄÄÄÄÄÄÄÄâ±•ï‘àËÅçΩπ—Öç–πùï–†â±•ï‘à§∞ÄâÕ—Ö—’–àËÅçΩπ—Öç–πùï–†âÕ—Ö—’–à§∞(ÄÄÄÄÄÄÄÄâëÖ—ïÕ}ôΩ…µÖ—•Ω∏àËÅçΩπ—Öç–πùï–†âëÖ—ïÕ}ôΩ…µÖ—•Ω∏à§∞(ÄÄÄÄÄÄÄÄâ±•ïπ}…ëŸ}çÖ±ïπë±‰àËÅ}ç…µ}çÖ±ïπë±Â}’…∞°çΩπ—Öç–§∞(ÄÄÄÄÄÄÄÄ®©}ç…µ}—ΩëÖÂ}Ö¡¡Ω•π—µïπ—}ŸÖ…•Öâ±ïÃ°çΩπ—Öç–∞ÅëÖ—Ö}Õ—Ω…îıëÖ—Ö}Õ—Ω…î§∞(ÄÄÄÅÙ(ÄÄÄÅôΩ»ÅπÖµî∞ÅŸÖ±’îÅ•∏ÅŸÖ…•Öâ±ïÃπ•—ïµÃ†§Ë(ÄÄÄÄÄÄÄÅŸÖ±’îÄÙÅÕ—»°ŸÖ±’îÅΩ»Äàà§(ÄÄÄÄÄÄÄÅ•òÅ°—µ∞Ë(ÄÄÄÄÄÄÄÄÄÄÄÅŸÖ±’îÄÙÅ°—µ±}µΩë’±îπïÕçÖ¡î°ŸÖ±’î§(ÄÄÄÄÄÄÄÅôΩ»ÅŸÖ…•Öâ±îÅ•∏Ä°òâÌÌÌÏÅÌπÖµïÙÅıııÙà∞ÅòâÌÌÌÌÌπÖµïııııÙà§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕΩ±ŸïêÄÙÅ…ïÕΩ±Ÿïêπ…ï¡±Öçî°ŸÖ…•Öâ±î∞ÅŸÖ±’î§(ÄÄÄÅ…ï—’…∏Å…ïÕΩ±Ÿïê(()ëïòÅ}ç…µ}ïµÖ•±}°—µ∞°âΩë‰∞ÅçΩπ—Öç–§Ë(ÄÄÄÄààâ-ïï¿ÅçΩµ¡±ï—îÅç’Õ—Ω¥ÅîµµÖ•±ÃÅ•π—Öç–ÅÖπêÅâ…ÖπêÅâΩë‰µΩπ±‰ÅµïÕÕÖùïÃ∏ààà(ÄÄÄÅâΩë‰ÄÙÅÕ—»°âΩë‰ÅΩ»Äàà§(ÄÄÄÅ•òÅ…îπÕïÖ…ç†°»à†¸ËÖëΩç—Â¡ïÒ°—µ∞•qàà∞ÅâΩë‰∞Å…îπ%9=IM§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅâΩë‰(ÄÄÄÅ•òÅπΩ–Å…îπÕïÖ…ç†°»àº˝mÑµÈumx˘t®¯à∞ÅâΩë‰∞Å…îπ%9=IM§Ë(ÄÄÄÄÄÄÄÅπΩ…µÖ±•ÈïêÄÙÅ°—µ±}µΩë’±îπ’πïÕçÖ¡î°âΩë‰§π…ï¡±Öçî†âq…q∏à∞Äâq∏à§π…ï¡±Öçî†âq»à∞Äâq∏à§(ÄÄÄÄÄÄÄÅ¡Ö…Öù…Ö¡°ÃÄÙÅl(ÄÄÄÄÄÄÄÄÄÄÄÅ¡Ö…Öù…Ö¡†πÕ—…•¿†§(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»Å¡Ö…Öù…Ö¡†Å•∏Å…îπÕ¡±•–°»âqπlÅq—t©q∏¨à∞ÅπΩ…µÖ±•Èïê§(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ¡Ö…Öù…Ö¡†πÕ—…•¿†§(ÄÄÄÄÄÄÄÅt(ÄÄÄÄÄÄÄÅâΩë‰ÄÙÄààπ©Ω•∏†(ÄÄÄÄÄÄÄÄÄÄÄÄúÒ¿ÅÕ—Â±îÙâµÖ…ù•∏Ë¿Ä¿ÄƒŸ¡‡à¯ú(ÄÄÄÄÄÄÄÄÄÄÄÄ¨Å°—µ±}µΩë’±îπïÕçÖ¡î°¡Ö…Öù…Ö¡†§π…ï¡±Öçî†âq∏à∞ÄàÒâ»¯à§(ÄÄÄÄÄÄÄÄÄÄÄÄ¨ÄàΩ¿¯à(ÄÄÄÄÄÄÄÄÄÄÄÅôΩ»Å¡Ö…Öù…Ö¡†Å•∏Å¡Ö…Öù…Ö¡°Ã(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅ…ï—’…∏Å…ïπëï…}—ïµ¡±Ö—î†(ÄÄÄÄÄÄÄÄâç…µ}ïµÖ•±}›…Ö¡¡ï»π°—µ∞à∞Å¡…ïπΩ¥ıçΩπ—Öç–πùï–†â¡…ïπΩ¥à§∞ÅçΩπ—ïπ‘ıâΩë‰(ÄÄÄÄ§()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯ΩçπÖ¡ÃµôΩ…¥à∞Åµï—°ΩëÃılâA=MPât§)±Ωù•π}…ï≈’•…ïê)}ç…µ}Õï…•Ö±•Èïê)ëïòÅç…µ}Õïπë}çπÖ¡Õ}ôΩ…¥°çΩπ—Öç—}•ê§Ë(ÄÄÄÄààâMïπêÅ—°îÅçΩπô•ù’…ïêÅΩçÃÅUPÅîµµÖ•∞ÅÖπêÅ…ïµïµâï»ÅΩπ±‰ÅÕ’ççïÕÕô’∞Åëï±•Ÿï…•ïÃ∏ààà(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅôΩ…µÖ—•Ω∏ÄÙÅÕ—»°çΩπ—Öç–πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§π’¡¡ï»†§(ÄÄÄÅ•òÅôΩ…µÖ—•Ω∏ÅπΩ–Å•∏ÅÏâALà∞ÄâÕ@âÙÅΩ»ÅÕ—»°çΩπ—Öç–πùï–†âçÖ…—ï}¡…ºà§ÅΩ»Äàà§πÕ—…•¿†§π’¡¡ï»†§ÄÑÙÄâ9=8àË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1îÅôΩ…µ’±Ö•…îÅ9ALÅïÕ–ÅÀ•Õï…€§ÅÖ’‡Å¡•Õ—ïÃÅALΩÕ@ÅÕÖπÃÅçÖ…—îÅ¡…ΩôïÕÕ•Ωππï±±î∏âÙ§∞Ä–¿‰(ÄÄÄÅ…ïç•¡•ïπ–ÄÙÅÕ—»°çΩπ—Öç–πùï–†âµÖ•∞à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–Å…ïç•¡•ïπ–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâIïπÕï•ùπïËÅ≥äeÖë…ïÕÕîÅîµµÖ•∞Åë‘ÅçΩπ—Öç–ÅÖŸÖπ–Å≥äeïπŸΩ§∏âÙ§∞Ä–¿‰(ÄÄÄÅ—ïµ¡±Ö—îÄÙÅ}ç…µ}πÖµïë}—ïµ¡±Ö—î°ëÖ—Ñ∞ÄâïµÖ•∞à∞ÄâΩçÃÅUPà§(ÄÄÄÅ•òÅπΩ–Å—ïµ¡±Ö—îË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1îÅµΩì°±îÅîµµÖ•∞É
¨ÅΩçÃÅUPÉ
ÏÅïÕ–Å•π—…Ω’ŸÖâ±îÅëÖπÃÄΩç…¥ΩµΩëï±ïÃ∏âÙ§∞Ä–¿‰((ÄÄÄÅâΩë‰ÄÙÅ}ç…µ}…ïÕΩ±Ÿï}µïÕÕÖùï}ŸÖ…•Öâ±ïÃ†(ÄÄÄÄÄÄÄÅ—ïµ¡±Ö—îπùï–†âçΩπ—ïπ‘à§∞ÅçΩπ—Öç–∞Å°—µ∞ıQ…’î∞ÅëÖ—Ö}Õ—Ω…îıëÖ—Ñ(ÄÄÄÄ§(ÄÄÄÅÕ’â©ïç–ÄÙÅ}ç…µ}…ïÕΩ±Ÿï}µïÕÕÖùï}ŸÖ…•Öâ±ïÃ†(ÄÄÄÄÄÄÄÅ—ïµ¡±Ö—îπùï–†âÕ’©ï–à§ÅΩ»ÄâΩçÃÅUPà∞ÅçΩπ—Öç–∞ÅëÖ—Ö}Õ—Ω…îıëÖ—Ñ(ÄÄÄÄ§(ÄÄÄÅâ…ÖπëïêÄÙÅ}ç…µ}ïµÖ•±}°—µ∞°âΩë‰∞ÅçΩπ—Öç–§(ÄÄÄÅ¡±Ö•∏ÄÙÅ°—µ±}µΩë’±îπ’πïÕçÖ¡î†(ÄÄÄÄÄÄÄÅ…îπÕ’à°»âqÃ¨à∞ÄàÄà∞Å…îπÕ’à°»àÒmx˘t¨¯à∞ÄàÄà∞ÅâΩë‰§§(ÄÄÄÄ§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–Å}ç…µ}Õïπë}ïµÖ•±}°—µ∞†(ÄÄÄÄÄÄÄÅ…ïç•¡•ïπ–∞ÅÕ’â©ïç–∞Å¡±Ö•∏∞Åâ…Öπëïê∞Å—ïµ¡±Ö—îı—ïµ¡±Ö—î∞(ÄÄÄÄ§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ3äeïπŸΩ§Åë‘ÅôΩ…µ’±Ö•…îÅÑÉ•ç°Ω◊§∏Å[•…•ô•ïËÅ±ÑÅçΩπô•ù’…Ö—•Ω∏Åï–Å≥äeÖë…ïÕÕîÅîµµÖ•∞∏âÙ§∞Ä‘¿»((ÄÄÄÅÕïπ—}Ö–ÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞ÄâïµÖ•∞à∞ÄâµµÖ•∞É
¨ÅΩçÃÅUPÉ
ÏÅïπŸΩÁ§à∞ÅÕ’â©ïç–∞Åâ…Öπëïê§(ÄÄÄÅ—ïµ¡±Ö—ïlâ’ÕÖùï}çΩ’π–âtÄÙÅ•π–°—ïµ¡±Ö—îπùï–†â’ÕÖùï}çΩ’π–à§ÅΩ»Ä¿§Ä¨Äƒ(ÄÄÄÅ—ïµ¡±Ö—ïlâ±ÖÕ—}’Õïë}Ö–âtÄÙÅÕïπ—}Ö–(ÄÄÄÅçΩπ—Öç—lâçπÖ¡Õ}ôΩ…µ}Õïπ—}Ö–âtÄÙÅÕïπ—}Ö–(ÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅÕïπ—}Ö–(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°çΩπ—Öç–§(()ëïòÅ}ç…µ}ô…Öπçï}—…ÖŸÖ•±}ô’πë•πù}—ïµ¡±Ö—î°çΩπ—Öç–§Ë(ÄÄÄÄààâIï—’…∏Å—°îÅï·Öç–ÅµΩëï∞ÅçΩπô•ù’…ïêÅôΩ»ÅÖ∏Åï±•ù•â±îÅPÅô’πë•πúÅô•±î∏ààà(ÄÄÄÅôΩ…µÖ—•Ω∏ÄÙÅÕ—»°çΩπ—Öç–πùï–†âôΩ…µÖ—•Ω∏à§ÅΩ»Äàà§πÕ—…•¿†§π’¡¡ï»†§(ÄÄÄÅ©Ω’…πï‰ÄÙÅÕ—»°çΩπ—Öç–πùï–†âëïÕ¡}—Â¡îà§ÅΩ»Äàà§πÕ—…•¿†§π’¡¡ï»†§(ÄÄÄÅ•òÅôΩ…µÖ—•Ω∏ÄÙÙÄâÕ@àË(ÄÄÄÄÄÄÄÅ…ï—’…∏Äâ•πÖπçïµïπ–ÅPÅÕ@à(ÄÄÄÅ•òÅôΩ…µÖ—•Ω∏ÄÙÙÄâM@àÅÖπêÅ©Ω’…πï‰ÄÙÙÄâ%9%Q%0àË(ÄÄÄÄÄÄÄÅ…ï—’…∏Äâ•πÖπçïµïπ–ÅPÅM@à(ÄÄÄÅ…ï—’…∏Äàà(()Ö¡¿π…Ω’—î†(ÄÄÄÄàΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯Ωô…Öπçîµ—…ÖŸÖ•∞µô’πë•πúµô•±îà∞(ÄÄÄÅµï—°ΩëÃılâA=MPât∞(§)±Ωù•π}…ï≈’•…ïê)}ç…µ}Õï…•Ö±•Èïê)ëïòÅç…µ}Õïπë}ô…Öπçï}—…ÖŸÖ•±}ô’πë•πù}ô•±î°çΩπ—Öç—}•ê§Ë(ÄÄÄÄààâMïπêÅ—°îÅçΩπô•ù’…ïêÅÕ@ÅΩ»ÅM@Å•π•—•Ö∞Å…ÖπçîÅQ…ÖŸÖ•∞Åô•±î∏ààà(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅ•òÅÕ—»°çΩπ—Öç–πùï–†âô•πÖπçïµïπ—}ô–à§ÅΩ»Äàà§πÕ—…•¿†§π’¡¡ï»†§ÄÑÙÄâ=U$àË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâ%πë•≈’ïËÅìäeÖâΩ…êÅ≈’îÅ±ÑÅ¡ï…ÕΩππîÅÕΩ’°Ö•—îÅ’∏Åô•πÖπçïµïπ–Å…ÖπçîÅQ…ÖŸÖ•∞∏à(ÄÄÄÄÄÄÄÅÙ§∞Ä–¿‰(ÄÄÄÅ—ïµ¡±Ö—ï}πÖµîÄÙÅ}ç…µ}ô…Öπçï}—…ÖŸÖ•±}ô’πë•πù}—ïµ¡±Ö—î°çΩπ—Öç–§(ÄÄÄÅ•òÅπΩ–Å—ïµ¡±Ö—ï}πÖµîË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâï–ÅïπŸΩ§ÅïÕ–ÅÀ•Õï…€§ÅÖ’‡ÅôΩ…µÖ—•ΩπÃÅÕ@Åï–ÅM@Å•π•—•Ö∞∏à(ÄÄÄÄÄÄÄÅÙ§∞Ä–¿‰(ÄÄÄÅ…ïç•¡•ïπ–ÄÙÅÕ—»°çΩπ—Öç–πùï–†âµÖ•∞à§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–Å…ïç•¡•ïπ–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâIïπÕï•ùπïËÅ≥äeÖë…ïÕÕîÅîµµÖ•∞Åë‘ÅçΩπ—Öç–ÅÖŸÖπ–Å≥äeïπŸΩ§∏à(ÄÄÄÄÄÄÄÅÙ§∞Ä–¿‰(ÄÄÄÅ—ïµ¡±Ö—îÄÙÅ}ç…µ}πÖµïë}—ïµ¡±Ö—î°ëÖ—Ñ∞ÄâïµÖ•∞à∞Å—ïµ¡±Ö—ï}πÖµî§(ÄÄÄÅ•òÅπΩ–Å—ïµ¡±Ö—îË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâ1îÅµΩì°±îÅîµµÖ•∞É
¨ÅÌ—ïµ¡±Ö—ï}πÖµïÙÉ
ÏÅïÕ–Å•π—…Ω’ŸÖâ±îÅëÖπÃÄà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄàΩç…¥ΩµΩëï±ïÃ∏à(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅÙ§∞Ä–¿‰(ÄÄÄÅâΩë‰ÄÙÅ}ç…µ}…ïÕΩ±Ÿï}µïÕÕÖùï}ŸÖ…•Öâ±ïÃ†(ÄÄÄÄÄÄÄÅ—ïµ¡±Ö—îπùï–†âçΩπ—ïπ‘à§∞ÅçΩπ—Öç–∞Å°—µ∞ıQ…’î∞ÅëÖ—Ö}Õ—Ω…îıëÖ—Ñ∞(ÄÄÄÄ§(ÄÄÄÅ•òÅπΩ–ÅâΩë‰πÕ—…•¿†§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÅòâ1îÅµΩì°±îÅîµµÖ•∞É
¨ÅÌ—ïµ¡±Ö—ï}πÖµïÙÉ
ÏÅïÕ–ÅŸ•ëî∏à(ÄÄÄÄÄÄÄÅÙ§∞Ä–¿‰(ÄÄÄÅÕ’â©ïç–ÄÙÅ}ç…µ}…ïÕΩ±Ÿï}µïÕÕÖùï}ŸÖ…•Öâ±ïÃ†(ÄÄÄÄÄÄÄÅ—ïµ¡±Ö—îπùï–†âÕ’©ï–à§ÅΩ»Å—ïµ¡±Ö—ï}πÖµî∞ÅçΩπ—Öç–∞ÅëÖ—Ö}Õ—Ω…îıëÖ—Ñ∞(ÄÄÄÄ§(ÄÄÄÅâ…ÖπëïêÄÙÅ}ç…µ}ïµÖ•±}°—µ∞°âΩë‰∞ÅçΩπ—Öç–§(ÄÄÄÅ¡±Ö•∏ÄÙÅ°—µ±}µΩë’±îπ’πïÕçÖ¡î†(ÄÄÄÄÄÄÄÅ…îπÕ’à°»âqÃ¨à∞ÄàÄà∞Å…îπÕ’à°»àÒmx˘t¨¯à∞ÄàÄà∞ÅâΩë‰§§(ÄÄÄÄ§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–Å}ç…µ}Õïπë}ïµÖ•±}°—µ∞†(ÄÄÄÄÄÄÄÅ…ïç•¡•ïπ–∞ÅÕ’â©ïç–∞Å¡±Ö•∏∞Åâ…Öπëïê∞Å—ïµ¡±Ö—îı—ïµ¡±Ö—î∞(ÄÄÄÄ§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅòâ3äeïπŸΩ§Åë‘ÅµΩì°±îÉ
¨ÅÌ—ïµ¡±Ö—ï}πÖµïÙÉ
ÏÅÑÉ•ç°Ω◊§∏Äà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ[•…•ô•ïËÅ±ÑÅçΩπô•ù’…Ö—•Ω∏Åï–Å≥äeÖë…ïÕÕîÅîµµÖ•∞∏à(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅÙ§∞Ä‘¿»((ÄÄÄÅÕïπ—}Ö–ÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰†(ÄÄÄÄÄÄÄÅçΩπ—Öç–∞ÄâïµÖ•∞à∞ÅòâµµÖ•∞É
¨ÅÌ—ïµ¡±Ö—ï}πÖµïÙÉ
ÏÅïπŸΩÁ§à∞ÅÕ’â©ïç–∞Åâ…Öπëïê∞(ÄÄÄÄ§(ÄÄÄÅ—ïµ¡±Ö—ïlâ’ÕÖùï}çΩ’π–âtÄÙÅ•π–°—ïµ¡±Ö—îπùï–†â’ÕÖùï}çΩ’π–à§ÅΩ»Ä¿§Ä¨Äƒ(ÄÄÄÅ—ïµ¡±Ö—ïlâ±ÖÕ—}’Õïë}Ö–âtÄÙÅÕïπ—}Ö–(ÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅÕïπ—}Ö–(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâçΩπ—Öç–àËÅçΩπ—Öç–∞Äâ—ïµ¡±Ö—ï}πÖµîàËÅ—ïµ¡±Ö—ï}πÖµïÙ§(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯ΩµïÕÕÖùîà∞Åµï—°ΩëÃılâA=MPât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}Õïπë}µïÕÕÖùî°çΩπ—Öç—}•ê§Ë(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§ÏÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–ËÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ}ç…µ}…ï≈’ïÕ—}¡ÖÂ±ΩÖê†§ÏÅ≠•πêÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†â—Â¡îà§ÅΩ»Äàà§πÕ—…•¿†§π±Ω›ï»†§(ÄÄÄÅ—ïµ¡±Ö—ï}•êÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†â—ïµ¡±Ö—ï}•êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅÕï±ïç—ïë}—ïµ¡±Ö—îÄÙÅπï·–††(ÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏ÅëÖ—Ñπùï–°òâç…µ}Ì≠•πëı}—ïµ¡±Ö—ïÃà∞Åmt§(ÄÄÄÄÄÄÄÅ•òÅ•—ï¥πùï–†â•êà§ÄÙÙÅ—ïµ¡±Ö—ï}•ê(ÄÄÄÄ§∞Å9Ωπî§Å•òÅ≠•πêÅ•∏ÅÏâïµÖ•∞à∞ÄâÕµÃâÙÅÖπêÅ—ïµ¡±Ö—ï}•êÅï±ÕîÅ9Ωπî(ÄÄÄÅâΩë‰ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âçΩπ—ïπ‘à∞Äàà§§πÕ—…•¿†§ÏÅÕ’â©ïç–ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âÕ’©ï–à∞Äâ%π”•ù…Ö±îÅçÖëïµ‰à§§πÕ—…•¿†§(ÄÄÄÅ•òÅ≠•πêÄÙÙÄâïµÖ•∞àË(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅµÖπ’Ö±}Ö——Öç°µïπ—ÃÄÙÅ}ç…µ}…ïÖë}ïµÖ•±}Ö——Öç°µïπ—Ã†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…ï≈’ïÕ–πô•±ïÃπùï—±•Õ–†âÖ——Öç°µïπ–à§(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅï·çï¡–Ä°YÖ±’ï……Ω»∞Å=Ÿï…ô±Ω›……Ω»§ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å}ç…µ}Ö——Öç°µïπ—}ï……Ω…}…ïÕ¡ΩπÕî°ï·å§(ÄÄÄÄÄÄÄÅâΩë‰ÄÙÅ}ç…µ}…ïÕΩ±Ÿï}µïÕÕÖùï}ŸÖ…•Öâ±ïÃ°âΩë‰∞ÅçΩπ—Öç–∞Å°—µ∞ıQ…’î∞ÅëÖ—Ö}Õ—Ω…îıëÖ—Ñ§(ÄÄÄÄÄÄÄÅÕ’â©ïç–ÄÙÅ}ç…µ}…ïÕΩ±Ÿï}µïÕÕÖùï}ŸÖ…•Öâ±ïÃ°Õ’â©ïç–∞ÅçΩπ—Öç–∞ÅëÖ—Ö}Õ—Ω…îıëÖ—Ñ§(ÄÄÄÄÄÄÄÅâ…ÖπëïêÄÙÅ}ç…µ}ïµÖ•±}°—µ∞°âΩë‰∞ÅçΩπ—Öç–§(ÄÄÄÄÄÄÄÅ•òÅµÖπ’Ö±}Ö——Öç°µïπ—ÃË(ÄÄÄÄÄÄÄÄÄÄÄÅ›•—†Å—ïµ¡ô•±îπQïµ¡Ω…Ö…Â•…ïç—Ω…‰°¡…ïô•‡Ùâç…¥µïµÖ•∞µÖ——Öç°µïπ–¥à§ÅÖÃÅÖ——Öç°µïπ—}ë•»Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖ——Öç°µïπ—}¡Ö—°ÃÄÙÅmt(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅôΩ»Å•πëï‡∞ÅÖ——Öç°µïπ–Å•∏Åïπ’µï…Ö—î°µÖπ’Ö±}Ö——Öç°µïπ—Ã§Ë(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅô•±ï}ë•»ÄÙÅΩÃπ¡Ö—†π©Ω•∏°Ö——Öç°µïπ—}ë•»∞ÅÕ—»°•πëï‡§§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩÃπµÖ≠ïë•…Ã°ô•±ï}ë•»∞Åï·•Õ—}Ω¨ıÖ±Õî§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖ——Öç°µïπ—}¡Ö—†ÄÙÅΩÃπ¡Ö—†π©Ω•∏°ô•±ï}ë•»∞ÅÖ——Öç°µïπ—lâô•±ïπÖµîât§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ›•—†ÅΩ¡ï∏°Ö——Öç°µïπ—}¡Ö—†∞Äâ·àà§ÅÖÃÅÖ——Öç°µïπ—}ô•±îË(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖ——Öç°µïπ—}ô•±îπ›…•—î°Ö——Öç°µïπ—lâçΩπ—ïπ–ât§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖ——Öç°µïπ—}¡Ö—°ÃπÖ¡¡ïπê°Ö——Öç°µïπ—}¡Ö—†§(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅΩ¨ÄÙÅ}ç…µ}Õïπë}ïµÖ•±}°—µ∞†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–πùï–†âµÖ•∞à§∞ÅÕ’â©ïç–∞ÅâΩë‰∞Åâ…Öπëïê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—ïµ¡±Ö—îıÕï±ïç—ïë}—ïµ¡±Ö—î∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖ——Öç°µïπ—Õ}¡Ö—°ÃıÖ——Öç°µïπ—}¡Ö—°Ã∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅï±ÕîË(ÄÄÄÄÄÄÄÄÄÄÄÅ•πç±’ëï}—ïµ¡±Ö—ï}Ö——Öç°µïπ–ÄÙÅ}ç…µ}¡ÖÂ±ΩÖë}âΩΩ±ïÖ∏†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêπùï–†â•πç±’ëï}—ïµ¡±Ö—ï}Ö——Öç°µïπ–à§∞ÅëïôÖ’±–ıQ…’î∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÄÄÄÄÅΩ¨ÄÙÅ}ç…µ}Õïπë}ïµÖ•±}°—µ∞†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩπ—Öç–πùï–†âµÖ•∞à§∞ÅÕ’â©ïç–∞ÅâΩë‰∞Åâ…Öπëïê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ—ïµ¡±Ö—îıÕï±ïç—ïë}—ïµ¡±Ö—î∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÖ——Öç°µïπ—Õ}¡Ö—°Ãı9ΩπîÅ•òÅ•πç±’ëï}—ïµ¡±Ö—ï}Ö——Öç°µïπ–Åï±ÕîÅmt∞(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ¡…ïŸ•ï‹ÄÙÅâ…Öπëïê(ÄÄÄÅï±•òÅ≠•πêÄÙÙÄâÕµÃàË(ÄÄÄÄÄÄÄÅ’¡±ΩÖëïêÄÙÅ…ï≈’ïÕ–πô•±ïÃπùï—±•Õ–†âÖ——Öç°µïπ–à§(ÄÄÄÄÄÄÄÅ•òÅÖπ‰°•—ï¥ÅÖπêÅ•—ï¥πô•±ïπÖµîÅôΩ»Å•—ï¥Å•∏Å’¡±ΩÖëïê§Ë(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1ïÃÅ¡ß°çïÃÅ©Ω•π—ïÃÅÕΩπ–ÅÀ•Õï…€•ïÃÅÖ’‡ÅîµµÖ•±Ã∏âÙ§∞Ä–¿¿(ÄÄÄÄÄÄÄÅâΩë‰ÄÙÅ}ç…µ}…ïÕΩ±Ÿï}µïÕÕÖùï}ŸÖ…•Öâ±ïÃ°âΩë‰∞ÅçΩπ—Öç–∞ÅëÖ—Ö}Õ—Ω…îıëÖ—Ñ§(ÄÄÄÄÄÄÄÅΩ¨ÄÙÅÕïπë}ÕµÃ°çΩπ—Öç–πùï–†â—ï±ï¡°Ωπîà§∞ÅâΩë‰§ÏÅ¡…ïŸ•ï‹ÄÙÅâΩë‰(ÄÄÄÅï±ÕîËÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâQÂ¡îÅ•πŸÖ±•ëîâÙ§∞Ä–¿¿(ÄÄÄÅ•òÅπΩ–ÅΩ¨ËÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ3äeïπŸΩ§ÅÑÉ•ç°Ω◊§∏Å[•…•ô•ïËÅ±ÑÅçΩπô•ù’…Ö—•Ω∏Åï–Å±ïÃÅçΩΩ…ëΩπª•ïÃ∏âÙ§∞Ä‘¿»(ÄÄÄÅµï—Ö}ÑÕ¡}—ïµ¡±Ö—îÄÙÅ—ïµ¡±Ö—ï}•êÄÙÙÅòâÖ’—ΩµÖ—•åµµï—ÑµÑÕ¿µÌ≠•πëÙà(ÄÄÄÅÖç—•Ÿ•—Â}—•—±îÄÙÄ†(ÄÄÄÄÄÄÄÅòâÏùµµÖ•∞úÅ•òÅ≠•πêÄÙÙÄùïµÖ•∞úÅï±ÕîÄùM5LùÙÅ5QÅÕ@ÅïπŸΩÁ§ÅµÖπ’ï±±ïµïπ–à(ÄÄÄÄÄÄÄÅ•òÅµï—Ö}ÑÕ¡}—ïµ¡±Ö—îÅï±Õî(ÄÄÄÄÄÄÄÄ†âµµÖ•∞ÅïπŸΩÁ§àÅ•òÅ≠•πêÄÙÙÄâïµÖ•∞àÅï±ÕîÄâM5LÅïπŸΩÁ§à§(ÄÄÄÄ§(ÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰°çΩπ—Öç–∞Å≠•πê∞ÅÖç—•Ÿ•—Â}—•—±î∞ÅÕ’â©ïç–Å•òÅ≠•πêÄÙÙÄâïµÖ•∞àÅï±ÕîÅâΩë‰∞Å¡…ïŸ•ï‹§(ÄÄÄÅ•òÅÕï±ïç—ïë}—ïµ¡±Ö—îË(ÄÄÄÄÄÄÄÅÕï±ïç—ïë}—ïµ¡±Ö—ïlâ’ÕÖùï}çΩ’π–âtÄÙÅ•π–°Õï±ïç—ïë}—ïµ¡±Ö—îπùï–†â’ÕÖùï}çΩ’π–à§ÅΩ»Ä¿§Ä¨Äƒ(ÄÄÄÄÄÄÄÅÕï±ïç—ïë}—ïµ¡±Ö—ïlâ±ÖÕ—}’Õïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅ}ç…µ}πΩ‹†§ÏÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°çΩπ—Öç–§(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯Ω≈’•ç¨µ…ïµ•πëï»à∞Åµï—°ΩëÃılâA=MPât§)±Ωù•π}…ï≈’•…ïê)}ç…µ}Õï…•Ö±•Èïê)ëïòÅç…µ}Õïπë}≈’•ç≠}…ïµ•πëï»°çΩπ—Öç—}•ê§Ë(ÄÄÄÄààâMïπêÅ—°îÅçΩπô•ù’…ïêÅô•Ÿîµµ•π’—îÅ…ïµ•πëï»Åô…Ω¥ÅÑÅçΩπ—Öç–ÅÕ°ïï–∏ààà(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅ•òÅπΩ–ÅÕ—»°çΩπ—Öç–πùï–†â—ï±ï¡°Ωπîà§ÅΩ»Äàà§πÕ—…•¿†§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâIïπÕï•ùπïËÅ±îÅπ’∑•…ºÅëîÅ”•≥•¡°ΩπîÅÖŸÖπ–ÅìäeïπŸΩÂï»Å±îÅ…Ö¡¡ï∞∏à(ÄÄÄÄÄÄÄÅÙ§∞Ä–¿‰(ÄÄÄÅ—ïµ¡±Ö—îÄÙÅ}ç…µ}πÖµïë}—ïµ¡±Ö—î°ëÖ—Ñ∞ÄâÕµÃà∞ÅI5}EU%-}I5%9I}Q5A1Q§(ÄÄÄÅ•òÅπΩ–Å—ïµ¡±Ö—îË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄ†(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ1îÅµΩì°±îÅM5LÉ
¨ÅIÖ¡¡ï∞ÅëÖπÃÄ’µ•∏É
ÏÅïÕ–Å•π—…Ω’ŸÖâ±îÅëÖπÃÄà(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄàΩç…¥ΩµΩëï±ïÃ∏à(ÄÄÄÄÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅÙ§∞Ä–¿‰(ÄÄÄÅâΩë‰ÄÙÅ}ç…µ}…ïÕΩ±Ÿï}µïÕÕÖùï}ŸÖ…•Öâ±ïÃ†(ÄÄÄÄÄÄÄÅ—ïµ¡±Ö—îπùï–†âçΩπ—ïπ‘à§∞ÅçΩπ—Öç–∞ÅëÖ—Ö}Õ—Ω…îıëÖ—Ñ(ÄÄÄÄ§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–ÅâΩë‰Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâ1îÅµΩì°±îÅM5LÉ
¨ÅIÖ¡¡ï∞ÅëÖπÃÄ’µ•∏É
ÏÅïÕ–ÅŸ•ëî∏à(ÄÄÄÄÄÄÄÅÙ§∞Ä–¿‰(ÄÄÄÅ•òÅπΩ–ÅÕïπë}ÕµÃ°çΩπ—Öç–πùï–†â—ï±ï¡°Ωπîà§∞ÅâΩë‰§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâï……Ω»àËÄâ3äeïπŸΩ§Åë‘ÅM5LÉ
¨ÅIÖ¡¡ï∞ÅëÖπÃÄ’µ•∏É
ÏÅÑÉ•ç°Ω◊§∏à(ÄÄÄÄÄÄÄÅÙ§∞Ä‘¿»((ÄÄÄÅπΩ‹ÄÙÅ}ç…µ}πΩ‹†§(ÄÄÄÅ}ç…µ}Öç—•Ÿ•—‰†(ÄÄÄÄÄÄÄÅçΩπ—Öç–∞ÄâÕµÃà∞ÅòâM5LÉ
¨ÅÌI5}EU%-}I5%9I}Q5A1QÙÉ
ÏÅïπŸΩÁ§à∞(ÄÄÄÄÄÄÄÅâΩë‰∞ÅâΩë‰∞(ÄÄÄÄ§(ÄÄÄÅ—ïµ¡±Ö—ïlâ’ÕÖùï}çΩ’π–âtÄÙÅ•π–°—ïµ¡±Ö—îπùï–†â’ÕÖùï}çΩ’π–à§ÅΩ»Ä¿§Ä¨Äƒ(ÄÄÄÅ—ïµ¡±Ö—ïlâ±ÖÕ—}’Õïë}Ö–âtÄÙÅπΩ‹(ÄÄÄÅçΩπ—Öç—lâ’¡ëÖ—ïë}Ö–âtÄÙÅπΩ‹(ÄÄÄÅÕÖŸï}ëÖ—Ñ°ëÖ—Ñ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°çΩπ—Öç–§(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥ΩçΩπ—Öç—ÃºÒçΩπ—Öç—}•ê¯ΩµïÕÕÖùîµ¡…ïŸ•ï‹à∞Åµï—°ΩëÃılâA=MPât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}µïÕÕÖùï}¡…ïŸ•ï‹°çΩπ—Öç—}•ê§Ë(ÄÄÄÄààâIïÕΩ±ŸîÅÑÅµïÕÕÖùîÅï·Öç—±‰ÅÖÃÅ•–Å›•±∞ÅâîÅÕïπ–∞Å›•—°Ω’–ÅÕ•ëîÅïôôïç—Ã∏ààà(ÄÄÄÅëÖ—ÑÄÙÅ±ΩÖë}ëÖ—Ñ†§(ÄÄÄÅçΩπ—Öç–ÄÙÅ}ç…µ}çΩπ—Öç–°ëÖ—Ñ∞ÅçΩπ—Öç—}•ê§(ÄÄÄÅ•òÅπΩ–ÅçΩπ—Öç–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâΩπ—Öç–Å•π—…Ω’ŸÖâ±îâÙ§∞Ä–¿–(ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(ÄÄÄÅ≠•πêÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†â—Â¡îà§ÅΩ»ÄâïµÖ•∞à§πÕ—…•¿†§π±Ω›ï»†§(ÄÄÄÅ…Ö›}âΩë‰ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âçΩπ—ïπ‘à§ÅΩ»Äàà§(ÄÄÄÅ•òÅ≠•πêÄÙÙÄâïµÖ•∞àË(ÄÄÄÄÄÄÄÅÕ’â©ïç–ÄÙÅ}ç…µ}…ïÕΩ±Ÿï}µïÕÕÖùï}ŸÖ…•Öâ±ïÃ†(ÄÄÄÄÄÄÄÄÄÄÄÅ¡ÖÂ±ΩÖêπùï–†âÕ’©ï–à∞Äàà§∞ÅçΩπ—Öç–∞ÅëÖ—Ö}Õ—Ω…îıëÖ—Ñ(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅâΩë‰ÄÙÅ}ç…µ}…ïÕΩ±Ÿï}µïÕÕÖùï}ŸÖ…•Öâ±ïÃ†(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö›}âΩë‰∞ÅçΩπ—Öç–∞Å°—µ∞ıQ…’î∞ÅëÖ—Ö}Õ—Ω…îıëÖ—Ñ(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ï(ÄÄÄÄÄÄÄÄÄÄÄÄâ—Â¡îàËÅ≠•πê∞(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ’©ï–àËÅÕ’â©ïç–∞(ÄÄÄÄÄÄÄÄÄÄÄÄâçΩπ—ïπ‘àËÅâΩë‰∞(ÄÄÄÄÄÄÄÄÄÄÄÄâ°—µ∞àËÅ}ç…µ}ïµÖ•±}°—µ∞°âΩë‰∞ÅçΩπ—Öç–§∞(ÄÄÄÄÄÄÄÅÙ§(ÄÄÄÅ•òÅ≠•πêÄÙÙÄâÕµÃàË(ÄÄÄÄÄÄÄÅâΩë‰ÄÙÅ}ç…µ}…ïÕΩ±Ÿï}µïÕÕÖùï}ŸÖ…•Öâ±ïÃ†(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö›}âΩë‰∞ÅçΩπ—Öç–∞ÅëÖ—Ö}Õ—Ω…îıëÖ—Ñ(ÄÄÄÄÄÄÄÄ§(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâ—Â¡îàËÅ≠•πê∞ÄâÕ’©ï–àËÄàà∞ÄâçΩπ—ïπ‘àËÅâΩëÂÙ§(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâQÂ¡îÅ•πŸÖ±•ëîâÙ§∞Ä–¿¿(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥Ω—ïÕ–µïµÖ•∞à∞Åµï—°ΩëÃılâA=MPât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}Õïπë}—ïÕ—}ïµÖ•∞†§Ë(ÄÄÄÄààâMïπêÅÑÅ—ïµ¡±Ö—îÅ¡…ïŸ•ï‹Å›•—°Ω’–Åç…ïÖ—•πúÅÖ∏ÅÖç—•Ÿ•—‰ÅΩ∏ÅÑÅçΩπ—Öç–∏ààà(ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(ÄÄÄÅ…ïç•¡•ïπ–ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âëïÕ—•πÖ—Ö•…îà∞Äàà§§πÕ—…•¿†§(ÄÄÄÅÕ’â©ïç–ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âÕ’©ï–à∞Äâ%π”•ù…Ö±îÅçÖëïµ‰à§§πÕ—…•¿†§(ÄÄÄÅâΩë‰ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âçΩπ—ïπ‘à∞Äàà§§(ÄÄÄÅ—ïµ¡±Ö—ï}•êÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†â—ïµ¡±Ö—ï}•êà§ÅΩ»Äàà§πÕ—…•¿†§(ÄÄÄÅ—ïµ¡±Ö—îÄÙÅπï·–††(ÄÄÄÄÄÄÄÅ•—ï¥ÅôΩ»Å•—ï¥Å•∏Å±ΩÖë}ëÖ—Ñ†§πùï–†âç…µ}ïµÖ•±}—ïµ¡±Ö—ïÃà∞Åmt§(ÄÄÄÄÄÄÄÅ•òÅ•—ï¥πùï–†â•êà§ÄÙÙÅ—ïµ¡±Ö—ï}•ê(ÄÄÄÄ§∞Å9Ωπî§Å•òÅ—ïµ¡±Ö—ï}•êÅï±ÕîÅ9Ωπî(ÄÄÄÅ•òÅπΩ–Å…îπô’±±µÖ—ç†°»âmyqÕt≠myqÕt≠pπmyqÕt¨à∞Å…ïç•¡•ïπ–§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâIïπÕï•ùπïËÅ’πîÅÖë…ïÕÕîÅîµµÖ•∞ÅŸÖ±•ëî∏âÙ§∞Ä–¿¿(ÄÄÄÅ•òÅπΩ–ÅâΩë‰πÕ—…•¿†§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1îÅçΩπ—ïπ‘ÅëîÅ≥äeîµµÖ•∞ÅïÕ–ÅŸ•ëî∏âÙ§∞Ä–¿¿(ÄÄÄÅ¡±Ö•∏ÄÙÅ…îπÕ’à°»àÒmx˘t¨¯à∞ÄàÄà∞ÅâΩë‰§(ÄÄÄÅ¡±Ö•∏ÄÙÅ°—µ±}µΩë’±îπ’πïÕçÖ¡î°…îπÕ’à°»âqÃ¨à∞ÄàÄà∞Å¡±Ö•∏§§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–Å}ç…µ}Õïπë}ïµÖ•±}°—µ∞†(ÄÄÄÄÄÄÄÅ…ïç•¡•ïπ–∞ÅÕ’â©ïç–ÅΩ»Äâ%π”•ù…Ö±îÅçÖëïµ‰à∞Å¡±Ö•∏∞ÅâΩë‰∞(ÄÄÄÄÄÄÄÅ—ïµ¡±Ö—îı—ïµ¡±Ö—î∞(ÄÄÄÄ§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ3äeïπŸΩ§Åë‘ÅµÖ•∞ÅëîÅ—ïÕ–ÅÑÉ•ç°Ω◊§∏âÙ§∞Ä‘¿»(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâµïÕÕÖùîàËÄâµµÖ•∞ÅëîÅ—ïÕ–ÅïπŸΩÁ§âÙ§(()Ö¡¿π…Ω’—î†àΩÖ¡§Ωç…¥Ω—ïÕ–µÕµÃà∞Åµï—°ΩëÃılâA=MPât§)±Ωù•π}…ï≈’•…ïê)ëïòÅç…µ}Õïπë}—ïÕ—}ÕµÃ†§Ë(ÄÄÄÄààâMïπêÅÖ∏ÅM5LÅ—ïµ¡±Ö—îÅ¡…ïŸ•ï‹Å›•—°Ω’–Åç…ïÖ—•πúÅÑÅçΩπ—Öç–ÅÖç—•Ÿ•—‰∏ààà(ÄÄÄÅ¡ÖÂ±ΩÖêÄÙÅ…ï≈’ïÕ–πùï—}©ÕΩ∏°Õ•±ïπ–ıQ…’î§ÅΩ»ÅÌÙ(ÄÄÄÅ…ïç•¡•ïπ–ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âëïÕ—•πÖ—Ö•…îà∞Äàà§§πÕ—…•¿†§(ÄÄÄÅâΩë‰ÄÙÅÕ—»°¡ÖÂ±ΩÖêπùï–†âçΩπ—ïπ‘à∞Äàà§§πÕ—…•¿†§(ÄÄÄÅ•òÅπΩ–Å}πΩ…µÖ±•Õï…}—ï±ï¡°Ωπï}ÕµÃ°…ïç•¡•ïπ–§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâIïπÕï•ùπïËÅ’∏Åπ’∑•…ºÅëîÅ”•≥•¡°ΩπîÅŸÖ±•ëî∏âÙ§∞Ä–¿¿(ÄÄÄÅ•òÅπΩ–ÅâΩë‰Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ1îÅçΩπ—ïπ‘Åë‘ÅM5LÅïÕ–ÅŸ•ëî∏âÙ§∞Ä–¿¿(ÄÄÄÅ•òÅπΩ–ÅÕïπë}ÕµÃ°…ïç•¡•ïπ–∞ÅâΩë‰§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°Ïâï……Ω»àËÄâ3äeïπŸΩ§Åë‘ÅM5LÅëîÅ—ïÕ–ÅÑÉ•ç°Ω◊§∏âÙ§∞Ä‘¿»(ÄÄÄÅ…ï—’…∏Å©ÕΩπ•ô‰°ÏâµïÕÕÖùîàËÄâM5LÅëîÅ—ïÕ–ÅïπŸΩÁ§âÙ§(((åÅ1ÑÅ¡±Ö—ïôΩ…µîÅ¡ï’–Åç°Ö…ùï»Åë•…ïç—ïµïπ–ÅÅÅÖ¡¿ÈÖ¡¡ÅÄÅÕÖπÃÅ¡ÖÕÕï»Å¡Ö»Å±î(åÅ¡Ω•π–Åêùïπ—À•îÅÅÅç…µ}Ö¡¡ÅÄ∏Åπ…ïù•Õ—…ï»Å∞ùï·—ïπÕ•Ω∏Å•ç§ÅùÖ…Öπ—•–ÅÖ±Ω…ÃÅ≈’î(åÅ∞ùA$ÅÖôô•ç£•îÅëÖπÃÅ±îÅI4ÅïÕ–Åâ•ï∏Åë•Õ¡Ωπ•â±îÅ≈’ï∞Å≈’îÅÕΩ•–Å±îÅì•µÖ……Öùî∏)ô…Ω¥Åç…µ}ÕÖ±ïÕôΩ…çï}•µ¡Ω…–Å•µ¡Ω…–Å…ïù•Õ—ï…}ÕÖ±ïÕôΩ…çï}•µ¡Ω…–()…ïù•Õ—ï…}ÕÖ±ïÕôΩ…çï}•µ¡Ω…–†(ÄÄÄÅÖ¡¿∞(ÄÄÄÅç’……ïπ—}’Õï…}ô∏ıç’……ïπ—}’Õï»∞(ÄÄÄÅ±ΩÖë}ëÖ—Ö}ô∏ı±ΩÖë}ëÖ—Ñ∞(ÄÄÄÅ±Ωù•π}…ï≈’•…ïë}ô∏ı±Ωù•π}…ï≈’•…ïê∞(ÄÄÄÅÕÖŸï}ëÖ—Ö}ô∏ıÕÖŸï}ëÖ—Ñ∞(§(()Ö¡¿πâïôΩ…ï}…ï≈’ïÕ–)ëïòÅÕ—Ö…—}ç…µ}…ï≈’ïÕ—}—•µ•πú†§Ë(ÄÄÄÅ•òÅ…ï≈’ïÕ–π¡Ö—†πÕ—Ö…—Õ›•—††àΩÖ¡§Ωç…¥ºà§Ë(ÄÄÄÄÄÄÄÅ…ï≈’ïÕ–πïπŸ•…Ωπlâ•π—ïù…Ö±îπç…µ}Õ—Ö…—ïë}Ö–âtÄÙÅ—•µîπ¡ï…ô}çΩ’π—ï»†§(()Ö¡¿πÖô—ï…}…ï≈’ïÕ–)ëïòÅ…ï¡Ω…—}Õ±Ω›}ç…µ}…ï≈’ïÕ—Ã°…ïÕ¡ΩπÕî§Ë(ÄÄÄÅÕ—Ö…—ïë}Ö–ÄÙÅ…ï≈’ïÕ–πïπŸ•…Ω∏πùï–†â•π—ïù…Ö±îπç…µ}Õ—Ö…—ïë}Ö–à§(ÄÄÄÅ•òÅÕ—Ö…—ïë}Ö–Å•ÃÅ9ΩπîË(ÄÄÄÄÄÄÄÅ…ï—’…∏Å…ïÕ¡ΩπÕî(ÄÄÄÅë’…Ö—•Ωπ}µÃÄÙÄ°—•µîπ¡ï…ô}çΩ’π—ï»†§Ä¥ÅÕ—Ö…—ïë}Ö–§Ä®Äƒ¿¿¿(ÄÄÄÅ…ïÕ¡ΩπÕîπ°ïÖëï…ÕlâMï…Ÿï»µQ•µ•πúâtÄÙÅòâÖ¡¿Ìë’»ıÌë’…Ö—•Ωπ}µÃË∏≈ôÙà(ÄÄÄÅ•òÅë’…Ö—•Ωπ}µÃÄ¯ÙÄƒ¿¿¿Ë(ÄÄÄÄÄÄÄÅÖ¡¿π±Ωùùï»π›Ö…π•πú†(ÄÄÄÄÄÄÄÄÄÄÄÄâÕ±Ω›}ç…µ}…ï≈’ïÕ–Åµï—°ΩêÙïÃÅ¡Ö—†ÙïÃÅÕ—Ö—’ÃÙïÃÅë’…Ö—•Ωπ}µÃÙî∏≈òÅâÂ—ïÃÙïÃà∞(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï≈’ïÕ–πµï—°Ωê∞(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï≈’ïÕ–π¡Ö—†∞(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕîπÕ—Ö—’Õ}çΩëî∞(ÄÄÄÄÄÄÄÄÄÄÄÅë’…Ö—•Ωπ}µÃ∞(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕîπçÖ±ç’±Ö—ï}çΩπ—ïπ—}±ïπù—††§ÅΩ»Ä¿∞(ÄÄÄÄÄÄÄÄ§(ÄÄÄÅ…ï—’…∏Å…ïÕ¡ΩπÕî(()Ö¡¿πÖô—ï…}…ï≈’ïÕ–)ëïòÅçΩµ¡…ïÕÕ}ç…µ}©ÕΩ∏°…ïÕ¡ΩπÕî§Ë(ÄÄÄÄààâ-ïï¿Å—°îÅçΩµ¡Öç–ÅI4Å¡ÖÂ±ΩÖêÅôÖÕ–ÅΩ∏ÅµΩâ•±îº—ÅçΩππïç—•ΩπÃ∏ààà(ÄÄÄÅ•òÄ°πΩ–Å…ï≈’ïÕ–π¡Ö—†πÕ—Ö…—Õ›•—††àΩÖ¡§Ωç…¥ºà§(ÄÄÄÄÄÄÄÄÄÄÄÅΩ»Å…ïÕ¡ΩπÕîπÕ—Ö—’Õ}çΩëîÄÄ»¿¿ÅΩ»Å…ïÕ¡ΩπÕîπÕ—Ö—’Õ}çΩëîÄ¯ÙÄÃ¿¿(ÄÄÄÄÄÄÄÄÄÄÄÅΩ»Å…ïÕ¡ΩπÕîπµ•µï—Â¡îÄÑÙÄâÖ¡¡±•çÖ—•Ω∏Ω©ÕΩ∏à(ÄÄÄÄÄÄÄÄÄÄÄÅΩ»Å…ïÕ¡ΩπÕîπ°ïÖëï…Ãπùï–†âΩπ—ïπ–µπçΩë•πúà§(ÄÄÄÄÄÄÄÄÄÄÄÅΩ»ÄâùÈ•¿àÅπΩ–Å•∏Å…ï≈’ïÕ–π°ïÖëï…Ãπùï–†âççï¡–µπçΩë•πúà∞Äàà§π±Ω›ï»†§§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å…ïÕ¡ΩπÕî(ÄÄÄÅâΩë‰ÄÙÅ…ïÕ¡ΩπÕîπùï—}ëÖ—Ñ†§(ÄÄÄÅ•òÅ±ï∏°âΩë‰§ÄÄƒ¿»–Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å…ïÕ¡ΩπÕî(ÄÄÄÅçΩµ¡…ïÕÕïêÄÙÅùÈ•¿πçΩµ¡…ïÕÃ°âΩë‰∞ÅçΩµ¡…ïÕÕ±ïŸï∞ÙÃ§(ÄÄÄÅ•òÅ±ï∏°çΩµ¡…ïÕÕïê§Ä¯ÙÅ±ï∏°âΩë‰§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏Å…ïÕ¡ΩπÕî(ÄÄÄÅ…ïÕ¡ΩπÕîπÕï—}ëÖ—Ñ°çΩµ¡…ïÕÕïê§(ÄÄÄÅ…ïÕ¡ΩπÕîπ°ïÖëï…ÕlâΩπ—ïπ–µπçΩë•πúâtÄÙÄâùÈ•¿à(ÄÄÄÅ…ïÕ¡ΩπÕîπ°ïÖëï…ÕlâΩπ—ïπ–µ1ïπù—†âtÄÙÅÕ—»°±ï∏°çΩµ¡…ïÕÕïê§§(ÄÄÄÅ…ïÕ¡ΩπÕîπ°ïÖëï…ÕlâYÖ…‰âtÄÙÄâççï¡–µπçΩë•πúà(ÄÄÄÅ…ï—’…∏Å…ïÕ¡ΩπÕî(()Ö¡¿πÖô—ï…}…ï≈’ïÕ–)ëïòÅçÖç°ï}Ÿï…Õ•Ωπïë}Õ—Ö—•ç}ÖÕÕï—Ã°…ïÕ¡ΩπÕî§Ë(ÄÄÄÄààâYï…Õ•ΩπïêÅÖÕÕï—ÃÅÖ…îÅ•µµ’—Öâ±îÅÖπêÅµ’Õ–ÅπΩ–Åùïπï…Ö—îÅÑÄÃ¿–Å¡ï»Å¡Öùî∏ààà(ÄÄÄÅ•òÅ…ï≈’ïÕ–π¡Ö—†πÕ—Ö…—Õ›•—††àΩÕ—Ö—•åºà§ÅÖπêÅ…ï≈’ïÕ–πÖ…ùÃπùï–†âÿà§Ë(ÄÄÄÄÄÄÄÅ…ïÕ¡ΩπÕîπ°ïÖëï…ÕlâÖç°îµΩπ—…Ω∞âtÄÙÄâ¡’â±•å∞ÅµÖ‡µÖùîÙÃƒ‘Ãÿ¿¿¿∞Å•µµ’—Öâ±îà(ÄÄÄÅ…ï—’…∏Å…ïÕ¡ΩπÕî((()}Õ—Ö…—}›ïëΩô}âÖç≠ù…Ω’πë}ÕÂπå†§(()•òÅ}}πÖµï}|ÄÙÙÄâ}}µÖ•π}|àË(ÄÄÄÅ¡Ω…–ÄÙÅ•π–°ΩÃπïπŸ•…Ω∏πùï–†âA=IPà∞Äƒ¿¿¿¿§§(ÄÄÄÅÖ¡¿π…’∏°°ΩÕ–Ùà¿∏¿∏¿∏¿à∞Å¡Ω…–ı¡Ω…–∞Åëïâ’úıÖ±Õî§