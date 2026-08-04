"""Moteur déterministe du score d'intégration (sans réseau ni IA)."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
import unicodedata

CANDIDATE_SCORING_VERSION = 3

CNAPS_PROGRESS_POINTS = {
    "unknown": 0, "no_result": 0, "registered": 5, "transmitted": 15,
    "in_review": 20, "accepted": 30, "refused": 0,
}
CNAPS_PROGRESS_LABELS = {
    "unknown": "Situation CNAPS inconnue", "no_result": "Aucun résultat CNAPS",
    "registered": "Demande CNAPS créée", "transmitted": "Demande CNAPS transmise",
    "in_review": "Demande CNAPS en instruction", "accepted": "Demande CNAPS acceptée",
    "refused": "Demande CNAPS refusée",
}

# Source de vérité des tarifs utilisés par le score (en centimes).
TRAINING_PRICES_CENTS = {
    "APS": 165_000,
    "VTC": 150_000,
    "DESP_INITIAL": 430_000,
    "DESP_VAE": 380_000,
    "A3P": 420_000,
    "SSIAP_1": 98_000,
}


def _boolean(value):
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().casefold()
    if normalized in {"oui", "yes", "true", "1", "o"}:
        return True
    if normalized in {"non", "no", "false", "0", "n"}:
        return False
    return None


def _amount_cents(value):
    if value in (None, ""):
        return 0
    try:
        amount = Decimal(str(value).strip().replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return 0
    return max(0, int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def normalize_cpf_amount(value):
    """Valide un montant saisi et retourne sa représentation décimale exacte."""
    text = str(value or "").strip().replace(" ", "").replace(",", ".")
    if not text:
        return ""
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", text):
        raise ValueError("Le montant CPF doit être positif et comporter au maximum deux décimales.")
    amount = Decimal(text)
    return format(amount.quantize(Decimal("0.01")), "f")


def resolve_training_code(contact):
    raw = str(contact.get("formation_code") or contact.get("formation") or "")
    normalized = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode().upper()
    normalized = re.sub(r"[^A-Z0-9]+", "_", normalized).strip("_")
    if "DESP" in normalized or "DIRIGEANT" in normalized:
        kind = str(contact.get("desp_type") or "").upper()
        return "DESP_VAE" if "VAE" in normalized or kind == "VAE" else "DESP_INITIAL"
    if "SSIAP" in normalized:
        return "SSIAP_1"
    if "CHAUFFEUR" in normalized or normalized == "VTC" or normalized.startswith("VTC_"):
        return "VTC"
    for code in ("A3P", "APS"):
        if normalized == code or normalized.startswith(code + "_"):
            return code
    return None


def _euros(cents):
    return cents // 100 if cents % 100 == 0 else float(Decimal(cents) / 100)


def _money(cents):
    amount = Decimal(cents) / 100
    decimals = f",{amount % 1:.2f}"[2:] if cents % 100 else ""
    return f"{int(amount):,}".replace(",", " ") + decimals + " €"


def calculate_financial_readiness_score(contact):
    """Retourne le score complet sans effet de bord ni dépendance externe."""
    cpf, created, functional = (_boolean(contact.get(k)) for k in ("cpf", "identite_creation", "identite_ok"))
    wants_ft, personal, registered = (_boolean(contact.get(k)) for k in ("financement_ft", "refus_ft_perso", "inscrit_ft"))
    declared_cents = _amount_cents(contact.get("cpf_montant"))
    useful_cents = declared_cents if cpf is True else 0
    code = resolve_training_code(contact)
    price_cents = TRAINING_PRICES_CENTS.get(code)
    warnings, blockers, actions = [], [], []

    if cpf is True and not declared_cents:
        warnings.append("Compte CPF actif, mais montant CPF non renseigné.")
        actions.append("Demander le montant CPF disponible")
    if cpf is False and declared_cents:
        warnings.append("Un montant CPF est enregistré alors que le compte CPF est indiqué NON.")
    if functional is True and created is False:
        warnings.append("L’identité numérique est fonctionnelle alors que sa création est indiquée NON.")
    if created is not True:
        actions.append("Accompagner le candidat dans la création de son identité numérique")
    if functional is not True:
        actions.append("Vérifier le fonctionnement de l’identité numérique La Poste")
    if wants_ft is True and registered is not True:
        warnings.append("Le candidat souhaite un financement France Travail, mais n’est pas inscrit.")
        actions.append("Demander au candidat de s’inscrire à France Travail")
    unknown = [name for name, value in (("compte CPF", cpf), ("création de l’identité numérique", created),
               ("fonctionnement de l’identité numérique", functional), ("financement France Travail", wants_ft),
               ("financement personnel", personal)) if value is None]
    if wants_ft is True and registered is None:
        unknown.append("inscription France Travail")
    if unknown:
        warnings.append("Réponses nécessaires au calcul inconnues : " + ", ".join(unknown) + ".")
    if personal is not True:
        actions.append("Confirmer la possibilité d’un financement personnel")

    if price_cents is None:
        warnings.append(f"Tarif non configuré pour la formation « {contact.get('formation') or 'inconnue'} » (code résolu : {code or 'aucun'}).")
        actions.append("Configurer le tarif de cette formation")
        return {"version": CANDIDATE_SCORING_VERSION, "score": None, "max_score": 100,
                "level": None, "label": "Score indisponible — tarif de la formation non configuré",
                "indication": "Tarif de référence requis", "operational_status": "action_required",
                "training_code": code, "training_price_eur": None,
                "cpf_amount_eur": _euros(declared_cents), "cpf_coverage_percent": None,
                "remaining_to_finance_eur": None, "breakdown": [], "blockers": blockers,
                "warnings": warnings, "next_actions": list(dict.fromkeys(actions))}

    remaining = max(price_cents - useful_cents, 0)
    coverage = min(100, int((Decimal(useful_cents * 100) / price_cents).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
    coverage_points = int((Decimal(useful_cents * 40) / price_cents).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    coverage_points = min(40, coverage_points)
    if 0 < useful_cents < price_cents:
        warnings.append("Le CPF ne couvre qu’une partie du prix de la formation.")
        actions.append(f"Sécuriser le financement du reste à charge de {_money(remaining)}")

    ft_points = 15 if wants_ft is False else (10 if registered is True and personal is True else
        6 if registered is True and personal is False else 4 if registered is False and personal is True else 0)
    breakdown = [
        ("cpf_account", "Compte CPF actif", 5 if cpf is True else 0, 5),
        ("cpf_coverage", "Couverture du coût par le CPF", coverage_points, 40),
        ("digital_identity_created", "Identité numérique créée", 10 if created is True else 0, 10),
        ("digital_identity_functional", "Identité numérique fonctionnelle", 15 if functional is True else 0, 15),
        ("personal_funding", "Financement personnel possible", 15 if personal is True else 0, 15),
        ("france_travail_strategy", "Sécurisation France Travail", ft_points, 15),
    ]
    score = min(100, sum(row[2] for row in breakdown))
    if score >= 90: level, label, indication = "excellent", "Dossier très avancé", "Priorité haute"
    elif score >= 75: level, label, indication = "good", "Bon dossier", "À contacter rapidement"
    elif score >= 55: level, label, indication = "qualify", "Dossier à qualifier", "Des éléments restent à sécuriser"
    else: level, label, indication = "fragile", "Dossier fragile", "Financement ou démarches insuffisamment sécurisés"

    if remaining and personal is False and wants_ft is False:
        blockers.append("Le reste à financer n’est couvert ni personnellement ni par France Travail.")
    if remaining and wants_ft is True and registered is False and personal is False:
        blockers.append("Le financement France Travail est nécessaire, mais le candidat n’est pas inscrit et ne peut financer le reste.")
    if useful_cents and not remaining and personal is not True and wants_ft is not True and functional is not True:
        blockers.append("Le CPF est la seule solution déclarée, mais l’identité numérique La Poste n’est pas fonctionnelle.")
    if not useful_cents and personal is not True and wants_ft is not True:
        blockers.append("Aucune solution de financement n’est identifiée.")
    status = "blocked" if blockers else ("action_required" if warnings or actions else "ready")
    # Un dossier entièrement financé et documenté est prêt, même si des actions génériques ont été écartées.
    if not blockers and not warnings and score == 100:
        status = "ready"
    return {"version": CANDIDATE_SCORING_VERSION, "score": score, "max_score": 100,
            "level": level, "label": label, "indication": indication, "operational_status": status,
            "training_code": code, "training_price_eur": _euros(price_cents),
            "cpf_amount_eur": _euros(declared_cents), "cpf_coverage_percent": coverage,
            "remaining_to_finance_eur": _euros(remaining),
            "breakdown": [{"key": k, "label": l, "points": p, "max_points": m} for k, l, p, m in breakdown],
            "blockers": list(dict.fromkeys(blockers)), "warnings": list(dict.fromkeys(warnings)),
            "next_actions": list(dict.fromkeys(actions))}


def _normalized_text(value):
    text = " ".join(str(value or "").strip().split()).upper()
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def normalize_cnaps_tracking_status(snapshot):
    """Normalise toutes les variantes historiques du statut CNAPS."""
    if not isinstance(snapshot, dict):
        snapshot = {"raw_status": snapshot}
    values = []
    cnaps = snapshot.get("cnaps") if isinstance(snapshot.get("cnaps"), dict) else {}
    # Le statut brut est un fait plus récent et plus précis qu'un ancien cache normalisé.
    for source, keys in ((snapshot, ("raw_status",)),
                         (cnaps, ("cnaps_status", "statut_cnaps", "status", "statut")),
                         (snapshot, ("cnaps_status", "statut_cnaps", "status", "statut"))):
        for key in keys:
            if source.get(key) not in (None, ""):
                values.append(_normalized_text(source[key]))
    # Un statut principal explicite est prioritaire sur l'absence de titres.
    mappings = {
        "accepted": {"ACCEPTE", "VALIDE", "AUTORISE"},
        "transmitted": {"TRANSMIS", "DEPOSE"},
        "in_review": {"EN INSTRUCTION", "EN COURS", "EN COURS DE TRAITEMENT"},
        "registered": {"ENREGISTRE", "BROUILLON", "CREE"},
        "refused": {"REFUSE", "REJETE"},
        "no_result": {"AUCUN RESULTAT", "DOSSIER NON TROUVE"},
    }
    for value in values:
        for status, variants in mappings.items():
            if value in variants:
                return status
    if snapshot.get("found") is False or (snapshot.get("http_status") == 404 and snapshot.get("reason") == "cnaps_not_found"):
        return "no_result"
    normalized = snapshot.get("normalized_status")
    if normalized in CNAPS_PROGRESS_POINTS and normalized != "unknown":
        return normalized
    return "unknown"


def _unique(values):
    return list(dict.fromkeys(values))


def calculate_security_regulatory_score(contact, cnaps_snapshot=None):
    """Calcule la faisabilité APS/A3P à partir de faits locaux uniquement."""
    snapshot = cnaps_snapshot if isinstance(cnaps_snapshot, dict) else {}
    if resolve_training_code(contact) not in {"APS", "A3P"}:
        return {"applicable": False, "score": None, "max_score": 100, "status": "unknown",
                "label": "Non applicable", "source": "not_applicable", "breakdown": [],
                "blockers": [], "warnings": [], "next_actions": [], "normalized_cnaps_status": "unknown"}
    cnaps_status = normalize_cnaps_tracking_status(snapshot)
    card = _boolean(contact.get("carte_pro"))
    active_title = snapshot.get("has_active_professional_title") is True
    warnings, blockers, actions, breakdown = [], [], [], []
    if snapshot.get("has_expired_professional_title") or snapshot.get("title_expires_before_training"):
        warnings.append("Un titre professionnel CNAPS est expiré ou expire avant le début de la formation.")
        actions.append("Vérifier le renouvellement du titre professionnel CNAPS")
    if active_title or card is True or cnaps_status == "accepted":
        source = "verified_cnaps_title" if active_title else ("cnaps_tracking" if cnaps_status == "accepted" else "candidate_declaration")
        label = "Carte professionnelle vérifiée" if active_title else ("Autorisation CNAPS acceptée" if cnaps_status == "accepted" else "Carte professionnelle déclarée")
        if card is True and not active_title and cnaps_status != "accepted":
            warnings.append("La carte professionnelle est déclarée par le candidat mais n’a pas encore été vérifiée dans le suivi CNAPS.")
        return {"applicable": True, "score": 100, "max_score": 100, "status": "ready", "label": label,
                "source": source, "breakdown": [{"key": "official_readiness", "label": "Situation réglementaire validée", "points": 100, "max_points": 100}],
                "blockers": [], "warnings": _unique(warnings), "next_actions": _unique(actions),
                "normalized_cnaps_status": cnaps_status}
    progress = CNAPS_PROGRESS_POINTS[cnaps_status]
    status = "in_progress" if cnaps_status in {"registered", "transmitted", "in_review"} else "high_risk"
    label = CNAPS_PROGRESS_LABELS[cnaps_status]
    if cnaps_status == "refused":
        blockers.append("Le suivi CNAPS indique un refus. Vérification humaine obligatoire avant toute poursuite de l’inscription.")
        actions.append("Vérifier le motif et la situation du dossier CNAPS")
        status, label = "blocked", "Dossier CNAPS refusé"
    breakdown.extend([
        {"key": "official_readiness", "label": "Validation réglementaire définitive", "points": 0, "max_points": 40},
        {"key": "cnaps_tracking", "label": "Avancement CNAPS", "detail": CNAPS_PROGRESS_LABELS[cnaps_status],
         "points": progress, "max_points": 30},
    ])
    score = progress
    account = _boolean(contact.get("compte_cnaps"))
    if account is True:
        score += 10
    else:
        if account is None: warnings.append("La création du compte CNAPS n’est pas renseignée.")
        actions.append("Accompagner le candidat dans la création de son compte CNAPS")
    breakdown.append({"key": "cnaps_account", "label": "Compte CNAPS créé", "points": 10 if account is True else 0, "max_points": 10})
    antecedents = _boolean(contact.get("antecedents"))
    if antecedents is False:
        score += 10
    elif antecedents is True:
        status = "high_risk" if status != "blocked" else status
        warnings.append("Des antécédents sont déclarés. Seul le CNAPS peut apprécier leur compatibilité avec l’autorisation sollicitée.")
        actions.append("Vérifier l’avancement de l’instruction CNAPS avec le candidat")
    else:
        warnings.append("Une vérification réglementaire déclarative reste à compléter.")
    breakdown.append({"key": "declarative_check", "label": "Vérification déclarative", "points": 10 if antecedents is False else 0, "max_points": 10})
    stay = _normalized_text(contact.get("titre_sejour_cnaps"))
    if stay in {"NON_CONCERNE", "CONFORME"}:
        score += 10
    elif stay == "NON_CONFORME":
        status = "blocked"
        blockers.append("Une condition administrative liée au titre de séjour est indiquée comme non remplie. Vérification humaine obligatoire.")
    else:
        warnings.append("La conformité du titre de séjour aux conditions CNAPS reste à vérifier.")
        actions.append("Vérifier les justificatifs liés au titre de séjour")
    breakdown.append({"key": "residence_administration", "label": "Situation administrative", "points": 10 if stay in {"NON_CONCERNE", "CONFORME"} else 0, "max_points": 10})
    if cnaps_status in {"no_result", "unknown"}:
        warnings.append("Aucun résultat CNAPS exploitable n’est disponible.")
        actions.append("Retrouver ou déposer la demande d’autorisation CNAPS")
    if cnaps_status == "refused":
        score = 0
    return {"applicable": True, "score": max(0, score), "max_score": 100, "status": status, "label": label,
            "source": "cnaps_tracking", "breakdown": breakdown, "blockers": _unique(blockers),
            "warnings": _unique(warnings), "next_actions": _unique(actions), "normalized_cnaps_status": cnaps_status}


def _score_level(score):
    if score >= 90: return "excellent", "Dossier très avancé", "Priorité haute"
    if score >= 75: return "good", "Bon dossier", "À contacter rapidement"
    if score >= 55: return "qualify", "Dossier à qualifier", "Des éléments restent à sécuriser"
    return "fragile", "Dossier fragile", "Financement ou démarches insuffisamment sécurisés"


def calculate_candidate_integration_score(contact, cnaps_snapshot=None):
    """Assemble les sous-scores; cette fonction pure n'effectue aucun appel externe."""
    financial = calculate_financial_readiness_score(contact)
    regulatory = calculate_security_regulatory_score(contact, cnaps_snapshot)
    result = dict(financial)
    result["version"] = CANDIDATE_SCORING_VERSION
    financial_score = financial.get("score")
    applicable = regulatory["applicable"]
    if financial_score is None:
        global_score = None
    elif applicable:
        global_score = max(0, min(100, int((Decimal(financial_score) * Decimal("0.60") + Decimal(regulatory["score"]) * Decimal("0.40")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))))
    else:
        global_score = financial_score
    result.update({
        "score": global_score, "financial_score": financial_score,
        "financial_weight": 60 if applicable else 100,
        "financial_contribution": None if financial_score is None else int((Decimal(financial_score) * Decimal("0.60" if applicable else "1")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
        "financial_breakdown": financial.get("breakdown", []),
        "regulatory_applicable": applicable, "regulatory_score": regulatory["score"],
        "regulatory_weight": 40 if applicable else 0,
        "regulatory_contribution": int((Decimal(regulatory["score"] or 0) * Decimal("0.40")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if applicable else 0,
        "regulatory_status": regulatory["status"], "regulatory_label": regulatory["label"],
        "regulatory_breakdown": regulatory["breakdown"], "normalized_cnaps_status": regulatory["normalized_cnaps_status"],
        "blockers": _unique(financial.get("blockers", []) + regulatory["blockers"]),
        "warnings": _unique(financial.get("warnings", []) + regulatory["warnings"]),
        "next_actions": _unique(financial.get("next_actions", []) + regulatory["next_actions"]),
    })
    if global_score is not None:
        result["level"], result["label"], result["indication"] = _score_level(global_score)
    if result["blockers"]:
        result["operational_status"] = "blocked"
    elif applicable and regulatory["status"] != "ready":
        result["operational_status"] = "action_required"
    elif financial.get("operational_status") != "ready":
        result["operational_status"] = financial.get("operational_status", "action_required")
    else:
        result["operational_status"] = "ready"
    return result
