"""Scoring déterministe de la maturité financière des pistes CRM."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
import unicodedata

CANDIDATE_SCORING_VERSION = 1

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


def calculate_candidate_integration_score(contact):
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
    if score >= 90: level, label, indication = "excellent", "Très bon profil", "Priorité haute"
    elif score >= 75: level, label, indication = "good", "Bon profil", "À contacter rapidement"
    elif score >= 55: level, label, indication = "qualify", "Profil à qualifier", "Des éléments restent à sécuriser"
    else: level, label, indication = "fragile", "Profil fragile", "Financement ou démarches insuffisamment sécurisés"

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
