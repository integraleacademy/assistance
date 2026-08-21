"""Normalisation fiable des dates Salesforce pour le CRM français."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytz


_PARIS_TZ = pytz.timezone("Europe/Paris")
_DATE_FORMATS = (
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y, %H:%M",
    "%d/%m/%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%m/%d/%Y, %I:%M %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
)


def install_salesforce_date_guardrails(migration_module) -> None:
    """Stocke toutes les dates importées en ISO et dans le fuseau de Paris."""
    if getattr(migration_module, "_date_guardrails_installed", False):
        return

    original_map_row = migration_module._map_row

    def parse_datetime(value: Any) -> dt.datetime | None:
        raw = migration_module._text(value)
        if not raw:
            return None
        candidate = raw.replace("Z", "+00:00")
        try:
            parsed = dt.datetime.fromisoformat(candidate)
        except ValueError:
            parsed = None
        if parsed is None:
            for date_format in _DATE_FORMATS:
                try:
                    parsed = dt.datetime.strptime(raw, date_format)
                    break
                except ValueError:
                    continue
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = _PARIS_TZ.localize(parsed)
        return parsed.astimezone(_PARIS_TZ)

    def date_bound(value: str, *, end: bool = False) -> dt.datetime | None:
        if not value:
            return None
        try:
            parsed = dt.date.fromisoformat(value[:10])
        except ValueError as exc:
            raise ValueError(f"Date de filtre invalide : {value}") from exc
        boundary = dt.datetime.combine(
            parsed,
            dt.time.max if end else dt.time.min,
        )
        return _PARIS_TZ.localize(boundary)

    def mapped_row(row: dict[str, Any]) -> dict[str, Any]:
        mapped = original_map_row(row)
        now = dt.datetime.now(_PARIS_TZ).isoformat()
        is_converted = (
            mapped.get("statut") == "Converti"
            or bool(mapped.get("salesforce_is_converted"))
        )
        definitions = (
            (
                ("CreatedDate", "Date de création"),
                "created_at",
                "salesforce_created_at",
                True,
            ),
            (
                ("LastModifiedDate", "Date de dernière modification"),
                "updated_at",
                "salesforce_last_modified_at",
                False,
            ),
            (
                ("ConvertedDate", "Date de conversion"),
                "converted_at",
                "salesforce_converted_at",
                False,
            ),
        )
        for aliases, crm_field, salesforce_field, required in definitions:
            raw = migration_module._row_value(row, *aliases)
            if not raw:
                continue
            mapped[f"{salesforce_field}_raw"] = raw

            if crm_field == "converted_at" and not is_converted:
                # On conserve la valeur source pour audit, mais elle ne doit pas
                # transformer indirectement une piste non convertie en inscrit.
                mapped[crm_field] = ""
                mapped[salesforce_field] = ""
                mapped["salesforce_converted_date_without_flag"] = True
                continue

            parsed = parse_datetime(raw)
            if parsed is not None:
                normalized = parsed.isoformat()
                mapped[crm_field] = normalized
                mapped[salesforce_field] = normalized
                if crm_field == "created_at":
                    mapped["received_at"] = normalized
            elif required:
                # Une date source illisible ne doit pas casser les tris du CRM.
                mapped[crm_field] = now
                mapped["received_at"] = now
                mapped[salesforce_field] = ""
                mapped["salesforce_created_at_invalid"] = True
            else:
                mapped[crm_field] = ""
                mapped[salesforce_field] = ""
                mapped[f"{salesforce_field}_invalid"] = True
        return mapped

    migration_module._parse_datetime = parse_datetime
    migration_module._date_bound = date_bound
    migration_module._map_row = mapped_row
    migration_module._date_guardrails_installed = True
