"""Moteur déterministe du score d'intégration (sans réseau ni IA)."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
import unicodedata

CANDIDATE_SCORING_VERSION = 5

FT_REQUEST_PROGRESS_POINTS = {
    "aucune_demande": 0,
    "a_preparer": 8,
    "transmise": 17,
    "en_cours_instruction": 22,
    "acceptee": 30,
    "refusee": 0,
    "annulee": 0,
}

FT_REQUEST_STATUSES = set(FT_REQUEST_PROGRESS_POINTS)

# Les bornes sont exprimées en centimes. ``None`` signifie que la borne haute
# est inconnue ; elle est alors plafonnée au tarif de la formation pour les
# calculs d'affichage. Le score utilise toujours la borne basse, afin de ne
# jamais transformer une déclaration par tranche en montant exact.
CPF_TIER_RANGES_CENTS = (
    (("0", "1000"), 0, 100_000),
    (("1000", "2000"), 100_000, 200_000),
    (("2000", "3000"), 200_000, 300_000),
    (("3000", "4000"), 300_000, 400_000),
)

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


def parse_cpf_tier(value):
    """Retourne les bornes d'un palier CPF sans inventer de montant exact."""
    text = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"(?<=\d)\s+(?=\d)", "", text)
    compact = re.sub(r"[^a-z0-9]+", " ", text).strip()
    numbers = [token for token in re.findall(r"\d+", compact)]
    if any(marker in compact for marker in ("plus de", "au moins", "et plus")):
        lower = int(numbers[0]) * 100 if numbers else 400_000
        return {"label": str(value or "").strip(), "min_cents": lower,
                "max_cents": None, "estimated": True}
    if any(marker in compact for marker in ("moins de", "jusqu")) and numbers:
        return {"label": str(value or "").strip(), "min_cents": 0,
                "max_cents": int(numbers[-1]) * 100, "estimated": True}
    if len(numbers) >= 2:
        pair = (numbers[0], numbers[1])
        for expected, minimum, maximum in CPF_TIER_RANGES_CENTS:
            if pair == expected:
                return {"label": str(value or "").strip(), "min_cents": minimum,
                        "max_cents": maximum, "estimated": True}
        minimum, maximum = sorted((int(pair[0]) * 100, int(pair[1]) * 100))
        return {"label": str(value or "").strip(), "min_cents": minimum,
                "max_cents": maximum, "estimated": True}
    return None


def normalize_ft_request_status(value):
    normalized = _normalized_text(value).casefold().replace(" ", "_")
    aliases = {
        "aucune": "aucune_demande", "aucune_demande": "aucune_demande",
        "non_demandee": "aucune_demande", "a_preparer": "a_preparer",
        "transmis": "transmise", "transmise": "transmise",
        "en_cours": "en_cours_instruction",
        "en_instruction": "en_cours_instruction",
        "en_cours_instruction": "en_cours_instruction",
        "accepte": "acceptee", "acceptee": "acceptee",
        "refuse": "refusee", "refusee": "refusee",
        "annule": "annulee", "annulee": "annulee",
    }
    status = aliases.get(normalized, normalized)
    return status if status in FT_REQUEST_STATUSES else ""


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
    """Calcule la maturité financière, quelle que soit l'origine de la piste."""
    cpf = _boolean(contact.get("cpf"))
    created = _boolean(contact.get("identite_creation"))
    functional = _boolean(contact.get("identite_ok"))
    wants_ft = _boolean(contact.get("financement_ft"))
    registered = _boolean(contact.get("inscrit_ft"))
    legacy_fallback = _boolean(contact.get("refus_ft_perso"))
    personal_capacity = _boolean(contact.get("financement_perso_possible"))
    if personal_capacity is None:
        personal_capacity = legacy_fallback
    personal_remainder = _boolean(contact.get("reste_a_charge_perso"))
    ft_status = normalize_ft_request_status(contact.get("statut_demande_financement_ft"))
    code = resolve_training_code(contact)
    price_cents = TRAINING_PRICES_CENTS.get(code)
    warnings, blockers, actions = [], [], []

    exact_text = str(contact.get("cpf_montant") or "").strip()
    exact_known = bool(exact_text)
    declared_cents = _amount_cents(exact_text) if exact_known else 0
    tier = parse_cpf_tier(contact.get("cpf_palier")) if not exact_known else None
    if exact_known:
        cpf_min_cents = cpf_max_cents = declared_cents
    elif tier:
        cpf_min_cents = tier["min_cents"]
        cpf_max_cents = tier["max_cents"]
    else:
        cpf_min_cents = cpf_max_cents = 0
    # Une absence de montant ne vaut jamais 0 €. Le zéro n'est considéré comme
    # connu que lorsque le candidat indique explicitement ne pas utiliser de CPF.
    cpf_amount_known = exact_known or tier is not None or cpf is False
    reliable_remainder = exact_known or cpf is False
    personal_remainder_confirmed = personal_remainder is True and reliable_remainder

    if cpf is False:
        if exact_known or tier:
            warnings.append("Un montant ou palier CPF est enregistré alors que le compte CPF est indiqué NON.")
        useful_min_cents = useful_max_cents = 0
    elif cpf is True or exact_known or tier:
        useful_min_cents = cpf_min_cents
        useful_max_cents = cpf_max_cents
        if cpf is None:
            warnings.append("Un montant CPF est présent, mais la consultation du compte CPF reste à confirmer.")
            actions.append("Confirmer la consultation du compte CPF")
    else:
        useful_min_cents = useful_max_cents = 0

    if cpf is True and not exact_known and not tier:
        warnings.append("Compte CPF consulté, mais montant ou palier CPF non renseigné.")
        actions.append("Demander le montant ou le palier CPF disponible")
    if tier:
        warnings.append("Le CPF est renseigné par palier : la couverture affichée reste une estimation prudente.")
        actions.append("Confirmer le montant CPF exact avant l’inscription")
    if personal_remainder is not None and not reliable_remainder:
        warnings.append("Une réponse sur le reste à charge est enregistrée sans montant exact fiable ; elle n’est pas comptée comme financement confirmé.")
        actions.append("Calculer puis confirmer le reste à charge exact")
    if functional is True and created is False:
        warnings.append("L’identité numérique est fonctionnelle alors que sa création est indiquée NON.")
    if ft_status and wants_ft is False and ft_status not in {"aucune_demande", "annulee"}:
        warnings.append("Un statut France Travail est enregistré alors que le financement France Travail est indiqué NON.")

    if price_cents is None:
        warnings.append(f"Tarif non configuré pour la formation « {contact.get('formation') or 'inconnue'} » (code résolu : {code or 'aucun'}).")
        actions.append("Configurer le tarif de cette formation")
        return {"version": CANDIDATE_SCORING_VERSION, "score": None, "max_score": 100,
                "level": None, "label": "Score indisponible — tarif de la formation non configuré",
                "indication": "Tarif de référence requis", "operational_status": "action_required",
                "score_complete": False, "training_code": code, "training_price_eur": None,
                "cpf_amount_eur": _euros(declared_cents) if exact_known else None,
                "cpf_amount_min_eur": _euros(cpf_min_cents) if cpf_amount_known else None,
                "cpf_amount_max_eur": (_euros(cpf_max_cents)
                                        if cpf_amount_known and cpf_max_cents is not None else None),
                "cpf_amount_estimated": bool(tier), "cpf_range_open_ended": bool(tier and tier["max_cents"] is None),
                "cpf_coverage_percent": None, "cpf_coverage_min_percent": None,
                "cpf_coverage_max_percent": None, "remaining_to_finance_eur": None,
                "remaining_to_finance_min_eur": None, "remaining_to_finance_max_eur": None,
                "personal_remainder_applicable": False, "personal_remainder_amount_eur": None,
                "personal_remainder_status": "not_applicable", "funding_solution_status": "unknown",
                "unsecured_amount_eur": None, "financial_data_confidence_percent": 0,
                "breakdown": [], "blockers": blockers, "warnings": _unique(warnings),
                "next_actions": _unique(actions)}

    useful_min_cents = min(useful_min_cents, price_cents)
    useful_max_for_display = min(
        price_cents,
        price_cents if useful_max_cents is None else useful_max_cents,
    )
    coverage_min = min(100, int((Decimal(useful_min_cents * 100) / price_cents).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
    coverage_max = min(100, int((Decimal(useful_max_for_display * 100) / price_cents).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
    remaining_min = max(price_cents - useful_max_for_display, 0)
    remaining_max = max(price_cents - useful_min_cents, 0)

    ft_amount_text = str(contact.get("montant_accorde_ft") or "").strip()
    ft_amount_known = bool(ft_amount_text)
    ft_amount_cents = _amount_cents(ft_amount_text) if ft_amount_known else 0
    secured_cents = useful_min_cents
    if ft_status == "acceptee" and ft_amount_known:
        secured_cents += min(ft_amount_cents, max(price_cents - secured_cents, 0))
    if personal_remainder_confirmed:
        secured_cents = price_cents
    secured_cents = min(secured_cents, price_cents)
    unsecured_cents = max(price_cents - secured_cents, 0)
    coverage_points = min(50, int((Decimal(secured_cents * 50) / price_cents).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))

    cpf_route = (cpf is True or exact_known or tier is not None) and useful_max_for_display > 0
    conservative_cpf_full = useful_min_cents >= price_cents
    ft_route = (wants_ft is True or ft_status in FT_REQUEST_STATUSES) and ft_status not in {
        "refusee", "annulee", "aucune_demande",
    }
    personal_route = personal_remainder_confirmed or personal_capacity is True

    personal_progress = 30 if personal_remainder_confirmed else 18 if personal_capacity is True else 0
    if conservative_cpf_full:
        progress_points = 30
        funding_status = "fully_covered_by_cpf"
    elif secured_cents >= price_cents and personal_remainder_confirmed:
        progress_points = 30
        funding_status = "secured_personal"
    elif secured_cents >= price_cents and ft_status == "acceptee":
        progress_points = 30
        funding_status = "secured_france_travail"
    elif ft_status == "acceptee":
        progress_points = 30 if ft_amount_known else 26
        funding_status = "accepted_france_travail"
    elif ft_status in {"en_cours_instruction", "transmise", "a_preparer"}:
        progress_points = max(FT_REQUEST_PROGRESS_POINTS[ft_status], min(personal_progress, 18))
        funding_status = "pending_france_travail"
    elif ft_status in {"refusee", "annulee"}:
        progress_points = personal_progress
        funding_status = "fallback_declared" if personal_capacity is True else "unsecured"
    elif wants_ft is True:
        progress_points = max(5, min(personal_progress, 18))
        funding_status = "france_travail_to_prepare"
    elif wants_ft is False:
        progress_points = personal_progress
        funding_status = "personal_capacity_declared" if personal_capacity is True else "unsecured"
    else:
        progress_points = personal_progress
        funding_status = "personal_capacity_declared" if personal_capacity is True else "unknown"

    route_scores = []
    if conservative_cpf_full:
        cpf_operational = (8 if created is True else 0) + (12 if functional is True else 0)
        route_scores.append(cpf_operational)
    else:
        if cpf_route:
            cpf_operational = (8 if created is True else 0) + (12 if functional is True else 0)
            route_scores.append(cpf_operational)
        if ft_route:
            registration_points = 8 if registered is True or ft_status in {
                "transmise", "en_cours_instruction", "acceptee",
            } else 0
            request_points = {
                "a_preparer": 4, "transmise": 8,
                "en_cours_instruction": 10, "acceptee": 12,
            }.get(ft_status, 2 if wants_ft is True else 0)
            route_scores.append(min(20, registration_points + request_points))
        elif personal_route:
            route_scores.append(20 if personal_remainder_confirmed else 10)
    operational_points = int((Decimal(sum(route_scores)) / len(route_scores)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if route_scores else 0

    if cpf_route:
        if created is None:
            actions.append("Vérifier si l’identité numérique La Poste est créée")
        elif created is False:
            actions.append("Accompagner le candidat dans la création de son identité numérique")
        if functional is None:
            actions.append("Vérifier le fonctionnement de l’identité numérique La Poste")
        elif functional is False:
            actions.append("Finaliser le fonctionnement de l’identité numérique La Poste")

    if remaining_max > 0 and wants_ft is True and not ft_status:
        actions.append("Préparer la demande de financement France Travail")
    if ft_route and ft_status not in {"transmise", "en_cours_instruction", "acceptee"}:
        if registered is None:
            actions.append("Vérifier l’inscription du candidat à France Travail")
        elif registered is False:
            actions.append("Accompagner l’inscription du candidat à France Travail")
    if ft_status == "acceptee" and not ft_amount_known and remaining_max > 0:
        warnings.append("La demande France Travail est acceptée, mais le montant accordé n’est pas renseigné.")
        actions.append("Confirmer le montant accordé par France Travail")
    if (ft_status in {"refusee", "annulee"} and cpf_amount_known
            and remaining_max > 0):
        if personal_remainder_confirmed:
            pass
        elif personal_capacity is True:
            warnings.append("La solution personnelle est déclarée, mais le reste à charge exact doit être confirmé.")
            actions.append("Confirmer la prise en charge personnelle du reste à charge")
        else:
            blockers.append("La demande France Travail n’aboutit pas et aucune solution personnelle n’est confirmée pour le reste à charge.")
            actions.append("Identifier une nouvelle solution de financement")
    elif wants_ft is False and cpf_amount_known and remaining_max > 0:
        if personal_remainder_confirmed:
            pass
        elif personal_capacity is True:
            warnings.append("La capacité de financement personnel est déclarée, mais le reste exact doit être confirmé.")
            actions.append("Confirmer la prise en charge personnelle du reste à charge")
        elif personal_capacity is False:
            blockers.append("Le candidat ne sollicite pas France Travail et indique ne pas pouvoir financer personnellement le reste à charge.")
            actions.append("Identifier une autre solution de financement")
        else:
            warnings.append("La capacité de financement personnel du reste à charge n’est pas renseignée.")
            actions.append("Vérifier la capacité de financement personnel")
    elif (wants_ft is None and cpf_amount_known and remaining_max > 0
          and personal_capacity is not True):
        warnings.append("La stratégie de financement du reste à charge reste à définir.")
        actions.append("Définir la solution de financement du reste à charge")

    if 0 < useful_max_for_display < price_cents:
        warnings.append("Le CPF ne couvre qu’une partie du prix de la formation.")

    personal_applicable = reliable_remainder and remaining_max > 0 and (
        wants_ft is False or ft_status in {"refusee", "annulee"}
    )
    if not personal_applicable:
        personal_status = "not_applicable"
    elif personal_remainder is True:
        personal_status = "confirmed"
    elif personal_remainder is False:
        personal_status = "refused"
    else:
        personal_status = "unknown"

    confidence_checks = [price_cents is not None, cpf is not None, wants_ft is not None]
    if cpf is True:
        confidence_checks.append(exact_known or tier is not None)
    if cpf_route:
        confidence_checks.extend((created is not None, functional is not None))
    if wants_ft is True:
        confidence_checks.append(bool(ft_status))
        if ft_status not in {"transmise", "en_cours_instruction", "acceptee"}:
            confidence_checks.append(registered is not None)
    if cpf_amount_known and unsecured_cents > 0:
        confidence_checks.append(personal_capacity is not None or personal_remainder is not None)
    confidence = int((Decimal(sum(bool(item) for item in confidence_checks) * 100) / len(confidence_checks)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    breakdown = [
        ("funding_coverage", "Couverture financière sécurisée ou estimée", coverage_points, 50),
        ("funding_progress", "Avancement de la solution de financement", progress_points, 30),
        ("route_readiness", "Démarches nécessaires à la solution choisie", operational_points, 20),
    ]
    has_financing_fact = any((
        cpf is not None, exact_known, tier is not None, wants_ft is not None,
        bool(ft_status), personal_capacity is not None,
        personal_remainder is not None,
    ))
    coverage_basis_known = cpf_amount_known or (
        ft_status == "acceptee" and ft_amount_known
    )
    score = (min(100, sum(row[2] for row in breakdown))
             if has_financing_fact and coverage_basis_known else None)
    if score is None:
        level, label, indication = None, "Score incomplet — financement à renseigner", "Informations de financement requises"
    else:
        level, label, indication = _score_level(score)
    pending_funding = ft_status in {
        "a_preparer", "transmise", "en_cours_instruction",
    } or (ft_status == "acceptee" and unsecured_cents > 0)
    status = ("blocked" if blockers else
              "action_required" if warnings or actions or pending_funding else
              "ready")
    return {"version": CANDIDATE_SCORING_VERSION, "score": score, "max_score": 100,
            "level": level, "label": label, "indication": indication,
            "operational_status": status, "score_complete": score is not None and confidence == 100,
            "training_code": code, "training_price_eur": _euros(price_cents),
            "cpf_amount_eur": _euros(declared_cents) if exact_known else None,
            "cpf_amount_min_eur": _euros(cpf_min_cents) if cpf_amount_known else None,
            "cpf_amount_max_eur": (_euros(cpf_max_cents)
                                    if cpf_amount_known and cpf_max_cents is not None else None),
            "cpf_amount_estimated": bool(tier),
            "cpf_range_open_ended": bool(tier and tier["max_cents"] is None),
            "cpf_coverage_percent": coverage_min if cpf_amount_known else None,
            "cpf_coverage_min_percent": coverage_min if cpf_amount_known else None,
            "cpf_coverage_max_percent": coverage_max if cpf_amount_known else None,
            "remaining_to_finance_eur": _euros(remaining_max) if cpf_amount_known else None,
            "remaining_to_finance_min_eur": _euros(remaining_min) if cpf_amount_known else None,
            "remaining_to_finance_max_eur": _euros(remaining_max) if cpf_amount_known else None,
            "personal_remainder_applicable": personal_applicable,
            "personal_remainder_amount_eur": _euros(remaining_max) if personal_applicable and remaining_min == remaining_max else None,
            "personal_remainder_status": personal_status,
            "personal_funding_capacity": personal_capacity,
            "france_travail_request_status": ft_status or "aucune_demande",
            "france_travail_awarded_amount_eur": _euros(ft_amount_cents) if ft_amount_known else None,
            "funding_solution_status": funding_status,
            "unsecured_amount_eur": (_euros(unsecured_cents)
                                      if cpf_amount_known or secured_cents >= price_cents else None),
            "financial_data_confidence_percent": confidence,
            "breakdown": [{"key": k, "label": l, "points": p, "max_points": m} for k, l, p, m in breakdown],
            "blockers": _unique(blockers), "warnings": _unique(warnings),
            "next_actions": _unique(actions)}


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
    """Calcule l'avancement APS/A3P sans confondre déclaration et vérification."""
    snapshot = cnaps_snapshot if isinstance(cnaps_snapshot, dict) else {}
    if resolve_training_code(contact) not in {"APS", "A3P"}:
        return {"applicable": False, "score": None, "max_score": 100,
                "score_complete": True, "data_confidence_percent": 100,
                "status": "unknown", "label": "Non applicable",
                "source": "not_applicable", "breakdown": [], "blockers": [],
                "warnings": [], "next_actions": [],
                "normalized_cnaps_status": "unknown"}

    cnaps_status = normalize_cnaps_tracking_status(snapshot)
    card = _boolean(contact.get("carte_pro"))
    account = _boolean(contact.get("compte_cnaps"))
    active_title = snapshot.get("has_active_professional_title") is True
    expires_before_training = snapshot.get("title_expires_before_training") is True
    expired_without_active = (
        snapshot.get("has_expired_professional_title") is True and not active_title
    )
    warnings, blockers, actions = [], [], []

    antecedents = _boolean(contact.get("antecedents"))
    custody = _boolean(contact.get("garde_vue"))
    if antecedents is True:
        warnings.append("Des antécédents sont déclarés : une vérification réglementaire humaine est nécessaire, sans préjuger de la décision du CNAPS.")
        actions.append("Faire vérifier la situation réglementaire par l’équipe")
    if custody is True:
        warnings.append("Une garde à vue ou prise d’empreintes est déclarée : une vérification humaine est nécessaire, sans conclusion automatique sur l’éligibilité.")
        actions.append("Faire vérifier la situation réglementaire par l’équipe")

    stay = _normalized_text(contact.get("titre_sejour_cnaps"))
    validated_path = active_title or cnaps_status == "accepted"
    if stay == "NON_CONFORME" and not validated_path:
        blockers.append("Une condition administrative liée au titre de séjour est indiquée comme non remplie. Vérification humaine obligatoire.")
        actions.append("Vérifier la situation administrative avant toute poursuite")

    if cnaps_status == "refused":
        blockers.append("Le suivi CNAPS indique un refus. Vérification humaine obligatoire avant toute poursuite de l’inscription.")
        actions.append("Vérifier le motif et la situation du dossier CNAPS")
        score, status, label, source, confidence = 0, "blocked", "Dossier CNAPS refusé", "cnaps_tracking", 100
    elif expires_before_training or expired_without_active:
        score, status, label, source, confidence = 55, "in_progress", "Titre CNAPS à renouveler", "cnaps_tracking", 100
        warnings.append("Le titre professionnel CNAPS est expiré ou expire avant le début de la formation.")
        actions.append("Vérifier le renouvellement du titre professionnel CNAPS")
    elif active_title:
        score, status, label, source, confidence = 100, "ready", "Carte professionnelle vérifiée", "verified_cnaps_title", 100
    elif cnaps_status == "accepted":
        score, status, label, source, confidence = 100, "ready", "Autorisation CNAPS acceptée", "cnaps_tracking", 100
    elif card is True:
        score, status, label, source, confidence = 70, "in_progress", "Carte professionnelle déclarée à vérifier", "candidate_declaration", 70
        warnings.append("La carte professionnelle est déclarée par le candidat mais n’a pas encore été vérifiée dans le suivi CNAPS.")
        actions.append("Vérifier la carte professionnelle et sa validité jusqu’à la formation")
    elif cnaps_status == "in_review":
        score, status, label, source, confidence = 55, "in_progress", "Demande CNAPS en instruction", "cnaps_tracking", 100
    elif cnaps_status == "transmitted":
        score, status, label, source, confidence = 40, "in_progress", "Demande CNAPS transmise", "cnaps_tracking", 100
    elif cnaps_status == "registered" or account is True:
        score, status, label, source, confidence = 25, "in_progress", "Démarche CNAPS créée", "cnaps_tracking" if cnaps_status == "registered" else "candidate_declaration", 80 if cnaps_status == "registered" else 60
        actions.append("Finaliser et transmettre la demande d’autorisation CNAPS")
    else:
        score, status, label, source, confidence = None, "unknown", "Situation réglementaire à compléter", "unknown", 0
        if card is None:
            warnings.append("La détention d’une carte professionnelle n’est pas renseignée.")
            actions.append("Vérifier si le candidat possède une carte professionnelle")
        elif card is False:
            actions.append("Préparer la démarche d’autorisation CNAPS")
        if account is None:
            actions.append("Vérifier si un compte CNAPS est déjà créé")
        elif account is False:
            actions.append("Accompagner la création du compte CNAPS")
        if cnaps_status in {"no_result", "unknown"}:
            warnings.append("Aucun résultat CNAPS exploitable n’est disponible.")

    if not validated_path and stay not in {"", "NON_CONCERNE", "CONFORME", "NON_CONFORME"}:
        warnings.append("La situation administrative liée au titre de séjour reste à vérifier.")
        actions.append("Vérifier les justificatifs liés au titre de séjour")

    breakdown = [] if score is None else [{
        "key": "regulatory_progress", "label": label,
        "detail": CNAPS_PROGRESS_LABELS.get(cnaps_status, "Situation inconnue"),
        "points": score, "max_points": 100,
    }]
    return {"applicable": True, "score": score, "max_score": 100,
            "score_complete": score is not None,
            "data_confidence_percent": confidence, "status": status,
            "label": label, "source": source, "breakdown": breakdown,
            "blockers": _unique(blockers), "warnings": _unique(warnings),
            "next_actions": _unique(actions),
            "normalized_cnaps_status": cnaps_status}


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
    regulatory_score = regulatory.get("score")
    if financial_score is None:
        global_score = None
    elif applicable and regulatory_score is None:
        global_score = None
    elif applicable:
        global_score = max(0, min(100, int((Decimal(financial_score) * Decimal("0.60") + Decimal(regulatory_score) * Decimal("0.40")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))))
    else:
        global_score = financial_score
    financial_confidence = financial.get("financial_data_confidence_percent", 0)
    regulatory_confidence = regulatory.get("data_confidence_percent", 100 if not applicable else 0)
    confidence = int((Decimal(financial_confidence) * Decimal("0.60") + Decimal(regulatory_confidence) * Decimal("0.40")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if applicable else financial_confidence
    result.update({
        "score": global_score, "financial_score": financial_score,
        "financial_weight": 60 if applicable else 100,
        "financial_contribution": None if financial_score is None else int((Decimal(financial_score) * Decimal("0.60" if applicable else "1")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
        "financial_breakdown": financial.get("breakdown", []),
        "regulatory_applicable": applicable, "regulatory_score": regulatory_score,
        "regulatory_weight": 40 if applicable else 0,
        "regulatory_contribution": (None if regulatory_score is None else int((Decimal(regulatory_score) * Decimal("0.40")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))) if applicable else 0,
        "regulatory_status": regulatory["status"], "regulatory_label": regulatory["label"],
        "regulatory_source": regulatory.get("source"),
        "regulatory_data_confidence_percent": regulatory_confidence,
        "regulatory_breakdown": regulatory["breakdown"], "normalized_cnaps_status": regulatory["normalized_cnaps_status"],
        "blockers": _unique(financial.get("blockers", []) + regulatory["blockers"]),
        "warnings": _unique(financial.get("warnings", []) + regulatory["warnings"]),
        "next_actions": _unique(financial.get("next_actions", []) + regulatory["next_actions"]),
        "data_confidence_percent": confidence,
        "score_complete": bool(financial.get("score_complete")) and bool(regulatory.get("score_complete")),
        "score_estimated": bool(financial.get("cpf_amount_estimated")) or confidence < 100,
    })
    if global_score is not None:
        result["level"], result["label"], result["indication"] = _score_level(global_score)
    elif financial_score is not None and applicable and regulatory_score is None:
        result["level"] = None
        result["label"] = "Score incomplet — situation réglementaire à compléter"
        result["indication"] = "Vérification réglementaire requise"
    if result["blockers"]:
        result["operational_status"] = "blocked"
    elif global_score is None:
        result["operational_status"] = "action_required"
    elif result["warnings"] or result["next_actions"]:
        result["operational_status"] = "action_required"
    elif applicable and regulatory["status"] != "ready":
        result["operational_status"] = "action_required"
    elif financial.get("operational_status") != "ready":
        result["operational_status"] = financial.get("operational_status", "action_required")
    else:
        result["operational_status"] = "ready"
    return result
