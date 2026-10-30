"""Evidence used by the CRM's automatic France Travail notifications.

WEDOF's SolicitationFundingType documents POLE_EMPLOI = 9. A folder state
(including waitingAcceptation or refusedByFinancer) does not identify its funder.
"""

import copy
import datetime
import re
import unicodedata


def normalize(value):
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize(
        "NFD", str(value or "")
    ).encode("ascii", "ignore").decode().lower())


def timestamp(value):
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def excludes_france_travail(contact):
    secondary = normalize(contact.get("statut_secondaire"))
    return (secondary.startswith("c2p")
            or secondary in {"transitionpro", "defmob"}
            or normalize(contact.get("financement_ft")) == "non")


def _funding_type(value):
    if isinstance(value, dict):
        value = next((value.get(key) for key in ("code", "id", "name", "label")
                      if value.get(key) is not None), "")
    return normalize(value)


def solicitation_decisions(payload, *, ft_only=True):
    """Read decisions with their own funder, including grouped API responses."""
    info = payload.get("trainingActionInfo") or {}
    result = []

    def visit(value, funder="", submitted=""):
        if isinstance(value, (list, tuple)):
            for nested in value:
                visit(nested, funder, submitted)
        elif isinstance(value, dict):
            if "fundingType" in value:
                funder = _funding_type(value["fundingType"])
            submitted = value.get("submissionDate") or submitted
            status = next((value.get(key) for key in ("status", "state", "result")
                           if isinstance(value.get(key), (str, int))), "")
            if status and (not ft_only or funder in {
                    "9", "poleemploi", "francetravail"}):
                result.append({
                    "status": normalize(status),
                    "submissionDate": submitted,
                    "returnDate": value.get("returnDate") or "",
                })
            for key, nested in value.items():
                if key != "fundingType" and isinstance(nested, (dict, list, tuple)):
                    visit(nested, funder, submitted)

    if isinstance(info, dict):
        visit(info.get("solicitations"))
    return result


def france_travail_decision(payload):
    decisions = solicitation_decisions(payload)
    if not decisions:
        return None

    def recency(item):
        return (timestamp(item["submissionDate"]) or 0,
                timestamp(item["returnDate"]) or 0)

    latest = max(decisions, key=recency)
    # Conflicting undated/tied responses cannot safely identify a decision.
    if any(item["status"] != latest["status"] and (
            recency(item) == recency(latest) or recency(item) == (0, 0)
    ) for item in decisions):
        return None
    statuses = {
        "refused": "refusee", "rejected": "refusee", "refuse": "refusee",
        "accepted": "acceptee", "approved": "acceptee",
        "cancelled": "annulee", "canceled": "annulee",
        "pending": "en_cours_instruction", "submitted": "en_cours_instruction",
        "waiting": "en_cours_instruction", "inprogress": "en_cours_instruction",
        "waitingacceptation": "en_cours_instruction",
    }
    status = statuses.get(latest["status"], "")
    if not status:
        return None
    submitted_at = timestamp(latest["submissionDate"])
    return {
        "status": status,
        # No arbitrary payload hash: enrichment must not create a new decision.
        "cycle": str(submitted_at) if submitted_at is not None else "undated",
        "submitted_at": submitted_at,
    }


def modified_at(payload):
    return next((payload.get(key) for key in (
        "updatedAt", "updatedOn", "lastUpdate", "modifiedAt", "dateUpdated",
    ) if payload.get(key)), None)


def merge_folder(previous, incoming):
    """Keep rich evidence when a list/webhook omits it; reject older versions."""
    old_version = timestamp(modified_at(previous))
    new_version = timestamp(modified_at(incoming))
    if (old_version is not None and new_version is not None
            and new_version < old_version):
        return copy.deepcopy(previous)

    def merge(old, new):
        result = copy.deepcopy(old)
        for key, value in new.items():
            if key in {"history", "stateHistory", "events", "solicitations"}:
                if not value:
                    continue
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
        return result

    result = merge(previous, incoming)
    old_info = previous.get("trainingActionInfo") or {}
    new_info = incoming.get("trainingActionInfo") or {}
    if isinstance(old_info, dict) and isinstance(new_info, dict):
        old_decision = france_travail_decision(previous)
        new_decision = france_travail_decision(incoming)
        incomplete = not solicitation_decisions(incoming, ft_only=False)
        older_cycle = (old_decision and new_decision
                       and old_decision["submitted_at"] is not None
                       and (new_decision["submitted_at"] or 0)
                       < old_decision["submitted_at"])
        if old_info.get("solicitations") and (incomplete or older_cycle):
            if not isinstance(result.get("trainingActionInfo"), dict):
                result["trainingActionInfo"] = {}
            result["trainingActionInfo"]["solicitations"] = copy.deepcopy(
                old_info["solicitations"]
            )
    return result
