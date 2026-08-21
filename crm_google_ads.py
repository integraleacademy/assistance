"""Reliable Google Ads offline conversion uploads for the legacy CRM.

The extension is intentionally inert until the required Render environment
variables are configured. A CRM conversion is queued only when the CRM emits a
real conversion activity (status changed to ``Converti`` or an inscription is
opened), so editing an already converted contact cannot upload an old sale.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, MutableMapping

import pytz
import requests
from dateutil import parser as date_parser
from flask import jsonify

GOOGLE_ADS_STATE_KEY = "google_ads_offline_conversion"
_GOOGLE_ADS_TERMINAL_STATUSES = {"sent", "validated"}
_TRUE_VALUES = {"1", "true", "yes", "oui", "on"}
_PARIS_TZ = pytz.timezone("Europe/Paris")
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_PROCESS_LOCK = threading.RLock()


class GoogleAdsConfigurationError(RuntimeError):
    """Raised when the Google Ads integration is not fully configured."""


class GoogleAdsContactDataError(ValueError):
    """Raised when a converted CRM contact lacks required conversion data."""


class GoogleAdsUploadError(RuntimeError):
    """Raised when Google Ads rejects an upload or cannot be reached."""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in _TRUE_VALUES


def _env_int(name: str, default: int, minimum: int = 1, maximum: int = 300) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _digits(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _clean_api_version(value: str) -> str:
    version = str(value or "v25").strip().lower()
    if not version.startswith("v"):
        version = f"v{version}"
    return version


def _clean_consent(value: Any) -> str:
    consent = str(value or "UNSPECIFIED").strip().upper()
    if consent not in {"GRANTED", "DENIED", "UNSPECIFIED"}:
        return "UNSPECIFIED"
    return consent


@dataclass(frozen=True)
class GoogleAdsConfig:
    enabled: bool
    api_version: str
    customer_id: str
    login_customer_id: str
    developer_token: str
    client_id: str
    client_secret: str
    refresh_token: str
    conversion_action: str
    currency_code: str
    send_user_identifiers: bool
    require_click_id: bool
    ad_user_data_consent: str
    validate_only: bool
    timeout_seconds: int
    max_attempts: int
    retry_delay_minutes: int
    default_phone_country_code: str
    default_conversion_value: float | None

    @classmethod
    def from_env(cls) -> "GoogleAdsConfig":
        raw_default_value = (os.getenv("GOOGLE_ADS_DEFAULT_CONVERSION_VALUE") or "").strip()
        default_value: float | None = None
        if raw_default_value:
            try:
                parsed = float(raw_default_value.replace(" ", "").replace(",", "."))
                if math.isfinite(parsed) and parsed > 0:
                    default_value = parsed
            except ValueError:
                default_value = None

        return cls(
            enabled=_env_bool("GOOGLE_ADS_OFFLINE_CONVERSIONS_ENABLED", False),
            api_version=_clean_api_version(os.getenv("GOOGLE_ADS_API_VERSION", "v25")),
            customer_id=_digits(os.getenv("GOOGLE_ADS_CUSTOMER_ID")),
            login_customer_id=_digits(os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID")),
            developer_token=(os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN") or "").strip(),
            client_id=(os.getenv("GOOGLE_ADS_CLIENT_ID") or "").strip(),
            client_secret=(os.getenv("GOOGLE_ADS_CLIENT_SECRET") or "").strip(),
            refresh_token=(os.getenv("GOOGLE_ADS_REFRESH_TOKEN") or "").strip(),
            conversion_action=(os.getenv("GOOGLE_ADS_CONVERSION_ACTION_ID") or "").strip(),
            currency_code=(os.getenv("GOOGLE_ADS_CURRENCY", "EUR") or "EUR").strip().upper(),
            send_user_identifiers=_env_bool("GOOGLE_ADS_SEND_USER_IDENTIFIERS", False),
            require_click_id=_env_bool("GOOGLE_ADS_REQUIRE_CLICK_ID", True),
            ad_user_data_consent=_clean_consent(
                os.getenv("GOOGLE_ADS_AD_USER_DATA_CONSENT", "UNSPECIFIED")
            ),
            validate_only=_env_bool("GOOGLE_ADS_VALIDATE_ONLY", False),
            timeout_seconds=_env_int("GOOGLE_ADS_TIMEOUT_SECONDS", 10, 2, 60),
            max_attempts=_env_int("GOOGLE_ADS_MAX_ATTEMPTS", 5, 1, 20),
            retry_delay_minutes=_env_int("GOOGLE_ADS_RETRY_DELAY_MINUTES", 15, 1, 1440),
            default_phone_country_code=(
                _digits(os.getenv("GOOGLE_ADS_DEFAULT_PHONE_COUNTRY_CODE", "33")) or "33"
            ),
            default_conversion_value=default_value,
        )

    @property
    def conversion_action_resource(self) -> str:
        raw = self.conversion_action.strip()
        if raw.startswith("customers/"):
            return raw
        return f"customers/{self.customer_id}/conversionActions/{_digits(raw)}"

    def missing_environment_variables(self) -> list[str]:
        missing: list[str] = []
        required = {
            "GOOGLE_ADS_CUSTOMER_ID": self.customer_id,
            "GOOGLE_ADS_DEVELOPER_TOKEN": self.developer_token,
            "GOOGLE_ADS_CLIENT_ID": self.client_id,
            "GOOGLE_ADS_CLIENT_SECRET": self.client_secret,
            "GOOGLE_ADS_REFRESH_TOKEN": self.refresh_token,
            "GOOGLE_ADS_CONVERSION_ACTION_ID": self.conversion_action,
        }
        missing.extend(name for name, value in required.items() if not value)
        if self.send_user_identifiers and self.ad_user_data_consent != "GRANTED":
            missing.append("GOOGLE_ADS_AD_USER_DATA_CONSENT=GRANTED")
        return missing

    @property
    def ready(self) -> bool:
        return self.enabled and not self.missing_environment_variables()


def normalize_email_for_google_ads(value: Any) -> str:
    """Normalize an email before SHA-256 hashing, following Google guidance."""
    email = re.sub(r"\s+", "", str(value or "")).casefold()
    if email.count("@") != 1:
        return ""
    local, domain = email.split("@", 1)
    if not local or not domain:
        return ""
    if domain == "googlemail.com":
        domain = "gmail.com"
    if domain == "gmail.com":
        local = local.replace(".", "")
    return f"{local}@{domain}"


def normalize_phone_for_google_ads(value: Any, default_country_code: str = "33") -> str:
    """Normalize a phone number to E.164 before SHA-256 hashing."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    raw = re.sub(r"[^\d+]", "", raw)
    if raw.startswith("00"):
        raw = f"+{raw[2:]}"
    elif raw.startswith("+"):
        raw = f"+{_digits(raw)}"
    else:
        digits = _digits(raw)
        country = _digits(default_country_code) or "33"
        if digits.startswith(country) and len(digits) > len(country) + 6:
            raw = f"+{digits}"
        elif digits.startswith("0") and len(digits) >= 9:
            raw = f"+{country}{digits[1:]}"
        elif len(digits) >= 8:
            raw = f"+{country}{digits}"
        else:
            return ""
    digits_only = _digits(raw)
    if not 8 <= len(digits_only) <= 15:
        return ""
    return f"+{digits_only}"


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_money(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    else:
        text = str(value).strip().replace("\u00a0", "").replace(" ", "")
        text = text.replace("€", "").replace(",", ".")
        text = re.sub(r"[^0-9.\-]", "", text)
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return round(parsed, 2)


def _parse_datetime(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            parsed = dt.datetime.now(_PARIS_TZ)
        else:
            try:
                parsed = date_parser.parse(text, dayfirst=True)
            except (TypeError, ValueError, OverflowError) as exc:
                raise GoogleAdsContactDataError(
                    "La date de conversion de la fiche est invalide."
                ) from exc
    if parsed.tzinfo is None:
        parsed = _PARIS_TZ.localize(parsed)
    return parsed.astimezone(_PARIS_TZ)


def format_google_ads_datetime(value: Any) -> str:
    parsed = _parse_datetime(value)
    offset = parsed.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}"
    return f"{parsed.strftime('%Y-%m-%d %H:%M:%S')}{offset}"


def _click_identifier(contact: Mapping[str, Any]) -> tuple[str, str]:
    preferred_type = str(contact.get("google_ads_identifier_type") or "").strip().casefold()
    preferred_value = str(contact.get("google_ads_identifier") or "").strip()
    if preferred_type in {"gclid", "gbraid", "wbraid"} and preferred_value:
        return preferred_type, preferred_value
    for key in ("gclid", "gbraid", "wbraid"):
        value = str(contact.get(key) or "").strip()
        if value:
            return key, value
    return "", ""


def _contact_consent(contact: Mapping[str, Any], config: GoogleAdsConfig) -> str:
    return _clean_consent(
        contact.get("google_ads_ad_user_data_consent") or config.ad_user_data_consent
    )


def _build_user_identifiers(
    contact: Mapping[str, Any], config: GoogleAdsConfig
) -> list[dict[str, str]]:
    if not config.send_user_identifiers:
        return []
    consent = _contact_consent(contact, config)
    if consent != "GRANTED":
        return []
    identifiers: list[dict[str, str]] = []
    email = normalize_email_for_google_ads(contact.get("mail") or contact.get("email"))
    if email:
        identifiers.append(
            {"hashedEmail": sha256_hex(email), "userIdentifierSource": "FIRST_PARTY"}
        )
    phone = normalize_phone_for_google_ads(
        contact.get("telephone") or contact.get("phone"),
        config.default_phone_country_code,
    )
    if phone:
        identifiers.append(
            {
                "hashedPhoneNumber": sha256_hex(phone),
                "userIdentifierSource": "FIRST_PARTY",
            }
        )
    return identifiers


def _order_id(contact: Mapping[str, Any]) -> str:
    contact_id = str(contact.get("id") or "").strip()
    if not contact_id:
        raise GoogleAdsContactDataError("La fiche CRM ne possède pas d’identifiant interne.")
    return f"crm-{contact_id}"


def build_click_conversion(
    contact: Mapping[str, Any], config: GoogleAdsConfig
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build one Google Ads ClickConversion and a secret-free audit summary."""
    if not config.ready:
        missing = config.missing_environment_variables()
        if not config.enabled:
            raise GoogleAdsConfigurationError("L’intégration Google Ads est désactivée.")
        raise GoogleAdsConfigurationError(
            "Configuration Google Ads incomplète : " + ", ".join(missing)
        )

    identifier_type, identifier_value = _click_identifier(contact)
    user_identifiers = _build_user_identifiers(contact, config)
    if config.require_click_id and not identifier_value:
        raise GoogleAdsContactDataError(
            "Aucun GCLID, GBRAID ou WBRAID n’est enregistré sur cette fiche."
        )
    if not identifier_value and not user_identifiers:
        raise GoogleAdsContactDataError(
            "Aucun identifiant Google Ads ni identifiant client autorisé n’est disponible."
        )

    value = _parse_money(contact.get("prix_vente"))
    if value is None:
        value = config.default_conversion_value
    if value is None:
        raise GoogleAdsContactDataError(
            "Le montant de vente (prix_vente) est manquant ou invalide."
        )

    converted_at = (
        contact.get("converted_at")
        or contact.get("status_changed_at")
        or contact.get("updated_at")
        or contact.get("created_at")
    )
    conversion_datetime = format_google_ads_datetime(converted_at)
    consent = _contact_consent(contact, config)

    conversion: dict[str, Any] = {
        "conversionAction": config.conversion_action_resource,
        "conversionDateTime": conversion_datetime,
        "conversionValue": value,
        "currencyCode": config.currency_code,
        "orderId": _order_id(contact),
    }
    if identifier_type and identifier_value:
        conversion[identifier_type] = identifier_value
    if user_identifiers:
        conversion["userIdentifiers"] = user_identifiers
    if consent in {"GRANTED", "DENIED"}:
        conversion["consent"] = {"adUserData": consent}

    identifier_mode = "+".join(
        part
        for part in (
            identifier_type,
            "email" if any("hashedEmail" in row for row in user_identifiers) else "",
            "phone" if any("hashedPhoneNumber" in row for row in user_identifiers) else "",
        )
        if part
    )
    audit = {
        "order_id": conversion["orderId"],
        "conversion_action": conversion["conversionAction"],
        "conversion_date_time": conversion_datetime,
        "conversion_value": value,
        "currency_code": config.currency_code,
        "identifier_mode": identifier_mode,
        "validate_only": config.validate_only,
    }
    return conversion, audit


def _safe_response_json(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _google_error_message(response: requests.Response, payload: Mapping[str, Any]) -> str:
    error = payload.get("error") if isinstance(payload, Mapping) else None
    if isinstance(error, Mapping):
        message = str(error.get("message") or "").strip()
        if message:
            return message
    text = str(getattr(response, "text", "") or "").strip()
    if text:
        return text[:800]
    return f"Google Ads a répondu avec le statut HTTP {response.status_code}."


class GoogleAdsUploader:
    """Small REST client with an in-memory OAuth access-token cache."""

    def __init__(
        self,
        config: GoogleAdsConfig,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self._access_token = ""
        self._access_token_expires_at = 0.0
        self._token_lock = threading.Lock()

    def _get_access_token(self) -> str:
        if self._access_token and time.monotonic() < self._access_token_expires_at - 60:
            return self._access_token
        with self._token_lock:
            if self._access_token and time.monotonic() < self._access_token_expires_at - 60:
                return self._access_token
            response = self.session.post(
                _TOKEN_URL,
                data={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "refresh_token": self.config.refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=self.config.timeout_seconds,
            )
            payload = _safe_response_json(response)
            if not 200 <= response.status_code < 300:
                raise GoogleAdsUploadError(_google_error_message(response, payload))
            token = str(payload.get("access_token") or "").strip()
            if not token:
                raise GoogleAdsUploadError("Google OAuth n’a renvoyé aucun jeton d’accès.")
            try:
                expires_in = max(120, int(payload.get("expires_in") or 3600))
            except (TypeError, ValueError):
                expires_in = 3600
            self._access_token = token
            self._access_token_expires_at = time.monotonic() + expires_in
            return token

    def upload(self, conversion: Mapping[str, Any]) -> dict[str, Any]:
        token = self._get_access_token()
        url = (
            f"https://googleads.googleapis.com/{self.config.api_version}/customers/"
            f"{self.config.customer_id}:uploadClickConversions"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "developer-token": self.config.developer_token,
            "Content-Type": "application/json",
        }
        if self.config.login_customer_id:
            headers["login-customer-id"] = self.config.login_customer_id
        response = self.session.post(
            url,
            headers=headers,
            json={
                "conversions": [dict(conversion)],
                "partialFailure": True,
                "validateOnly": self.config.validate_only,
            },
            timeout=self.config.timeout_seconds,
        )
        payload = _safe_response_json(response)
        if not 200 <= response.status_code < 300:
            raise GoogleAdsUploadError(_google_error_message(response, payload))
        partial_failure = payload.get("partialFailureError")
        if isinstance(partial_failure, Mapping) and (
            partial_failure.get("message") or partial_failure.get("code")
        ):
            raise GoogleAdsUploadError(
                str(partial_failure.get("message") or "Conversion refusée par Google Ads.")
            )
        results = payload.get("results")
        first_result = results[0] if isinstance(results, list) and results else {}
        return {
            "job_id": str(payload.get("jobId") or ""),
            "result": {
                "conversion_action": str(
                    first_result.get("conversionAction") or conversion.get("conversionAction") or ""
                ),
                "conversion_date_time": str(
                    first_result.get("conversionDateTime")
                    or conversion.get("conversionDateTime")
                    or ""
                ),
            },
        }


def _now_iso() -> str:
    return dt.datetime.now(_PARIS_TZ).isoformat(timespec="seconds")


def _is_conversion_activity(kind: Any, title: Any) -> bool:
    kind_normalized = str(kind or "").strip().casefold()
    title_normalized = str(title or "").strip().casefold()
    return kind_normalized == "conversion" or (
        kind_normalized == "statut" and "converti" in title_normalized
    )


def queue_google_ads_conversion(
    contact: MutableMapping[str, Any], *, force: bool = False
) -> bool:
    """Queue one converted contact while preserving sent-conversion idempotency."""
    if str(contact.get("statut") or "").strip().casefold() != "converti":
        return False
    current = contact.get(GOOGLE_ADS_STATE_KEY)
    state = dict(current) if isinstance(current, Mapping) else {}
    if state.get("status") == "sent":
        return False
    if state.get("status") == "pending" and not force:
        return False
    now = _now_iso()
    if not contact.get("converted_at"):
        contact["converted_at"] = now
    state.update(
        {
            "status": "pending",
            "queued_at": now,
            "order_id": state.get("order_id") or _order_id(contact),
            "last_error": "",
            "blocked_reason": "",
            "next_retry_at": "",
        }
    )
    contact[GOOGLE_ADS_STATE_KEY] = state
    return True


def _append_system_activity(contact: MutableMapping[str, Any], title: str, detail: str) -> None:
    contact.setdefault("activities", []).insert(
        0,
        {
            "id": str(uuid.uuid4()),
            "date": _now_iso(),
            "kind": "google_ads",
            "title": title,
            "detail": detail,
            "preview": "",
            "author": "Google Ads",
        },
    )


def _state_retry_due(state: Mapping[str, Any], config: GoogleAdsConfig) -> bool:
    status = str(state.get("status") or "")
    if status == "pending":
        return True
    if status == "blocked":
        return state.get("blocked_reason") == "configuration" and config.ready
    if status != "failed":
        return False
    try:
        attempts = int(state.get("attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    if attempts >= config.max_attempts:
        return False
    next_retry = str(state.get("next_retry_at") or "").strip()
    if not next_retry:
        return True
    try:
        return _parse_datetime(next_retry) <= dt.datetime.now(_PARIS_TZ)
    except GoogleAdsContactDataError:
        return True


def _data_has_due_conversion(data: Mapping[str, Any], config: GoogleAdsConfig) -> bool:
    contacts = data.get("crm_contacts", [])
    if not isinstance(contacts, list):
        return False
    for contact in contacts:
        if not isinstance(contact, Mapping):
            continue
        state = contact.get(GOOGLE_ADS_STATE_KEY)
        if isinstance(state, Mapping) and _state_retry_due(state, config):
            return True
    return False


def _sanitize_error(exc: BaseException) -> str:
    message = re.sub(r"\s+", " ", str(exc or "Erreur inconnue")).strip()
    return message[:800]


def _attempt_contact_upload(
    contact: MutableMapping[str, Any],
    config: GoogleAdsConfig,
    uploader: GoogleAdsUploader,
) -> None:
    state_raw = contact.get(GOOGLE_ADS_STATE_KEY)
    state = dict(state_raw) if isinstance(state_raw, Mapping) else {}
    now = _now_iso()
    try:
        attempts = int(state.get("attempts") or 0) + 1
    except (TypeError, ValueError):
        attempts = 1
    state.update({"attempts": attempts, "last_attempt_at": now, "next_retry_at": ""})
    contact[GOOGLE_ADS_STATE_KEY] = state

    if not config.ready:
        missing = config.missing_environment_variables()
        reason = (
            "L’intégration Google Ads est désactivée."
            if not config.enabled
            else "Configuration Google Ads incomplète : " + ", ".join(missing)
        )
        state.update(
            {
                "status": "blocked",
                "blocked_reason": "configuration",
                "last_error": reason,
            }
        )
        return

    try:
        conversion, audit = build_click_conversion(contact, config)
        response_summary = uploader.upload(conversion)
    except GoogleAdsContactDataError as exc:
        state.update(
            {
                "status": "blocked",
                "blocked_reason": "contact_data",
                "last_error": _sanitize_error(exc),
            }
        )
        _append_system_activity(
            contact,
            "Conversion Google Ads non envoyée",
            state["last_error"],
        )
        return
    except (GoogleAdsConfigurationError, GoogleAdsUploadError, requests.RequestException) as exc:
        next_retry = dt.datetime.now(_PARIS_TZ) + dt.timedelta(
            minutes=config.retry_delay_minutes
        )
        state.update(
            {
                "status": "failed",
                "blocked_reason": "",
                "last_error": _sanitize_error(exc),
                "next_retry_at": next_retry.isoformat(timespec="seconds"),
            }
        )
        if attempts >= config.max_attempts:
            state.update(
                {
                    "status": "blocked",
                    "blocked_reason": "max_attempts",
                    "next_retry_at": "",
                }
            )
            _append_system_activity(
                contact,
                "Conversion Google Ads en échec",
                state["last_error"],
            )
        return
    except Exception as exc:  # Defensive: CRM persistence must never be interrupted.
        state.update(
            {
                "status": "failed",
                "blocked_reason": "",
                "last_error": _sanitize_error(exc),
            }
        )
        return

    terminal_status = "validated" if config.validate_only else "sent"
    state.update(
        {
            "status": terminal_status,
            "blocked_reason": "",
            "last_error": "",
            "next_retry_at": "",
            "sent_at": "" if config.validate_only else now,
            "validated_at": now if config.validate_only else "",
            "api_job_id": response_summary.get("job_id", ""),
            "api_result": response_summary.get("result", {}),
            **audit,
        }
    )
    title = (
        "Conversion Google Ads validée en mode test"
        if config.validate_only
        else "Conversion Google Ads transmise"
    )
    _append_system_activity(
        contact,
        title,
        f"{audit['conversion_value']:.2f} {audit['currency_code']} · {audit['identifier_mode']}",
    )


def _merge_terminal_states_from_persisted(
    data: MutableMapping[str, Any], persisted: Mapping[str, Any]
) -> None:
    persisted_contacts = persisted.get("crm_contacts", [])
    if not isinstance(persisted_contacts, list):
        return
    terminal_by_id: dict[str, Mapping[str, Any]] = {}
    for row in persisted_contacts:
        if not isinstance(row, Mapping):
            continue
        state = row.get(GOOGLE_ADS_STATE_KEY)
        if isinstance(state, Mapping) and state.get("status") in _GOOGLE_ADS_TERMINAL_STATUSES:
            terminal_by_id[str(row.get("id") or "")] = state
    contacts = data.get("crm_contacts", [])
    if not isinstance(contacts, list):
        return
    for row in contacts:
        if not isinstance(row, MutableMapping):
            continue
        contact_id = str(row.get("id") or "")
        terminal = terminal_by_id.get(contact_id)
        if terminal:
            row[GOOGLE_ADS_STATE_KEY] = dict(terminal)


def _process_due_conversions(
    data: MutableMapping[str, Any],
    config: GoogleAdsConfig,
    uploader: GoogleAdsUploader,
) -> None:
    contacts = data.get("crm_contacts", [])
    if not isinstance(contacts, list):
        return
    for contact in contacts:
        if not isinstance(contact, MutableMapping):
            continue
        state = contact.get(GOOGLE_ADS_STATE_KEY)
        if not isinstance(state, Mapping) or not _state_retry_due(state, config):
            continue
        _attempt_contact_upload(contact, config, uploader)


def _integration_counts(data: Mapping[str, Any]) -> dict[str, int]:
    counts = {
        "pending": 0,
        "sent": 0,
        "validated": 0,
        "failed": 0,
        "blocked": 0,
    }
    contacts = data.get("crm_contacts", [])
    if not isinstance(contacts, list):
        return counts
    for contact in contacts:
        if not isinstance(contact, Mapping):
            continue
        state = contact.get(GOOGLE_ADS_STATE_KEY)
        status = str(state.get("status") or "") if isinstance(state, Mapping) else ""
        if status in counts:
            counts[status] += 1
    return counts


def register_google_ads_offline_conversions(legacy_app: Any) -> None:
    """Patch the legacy CRM and register protected diagnostics/retry endpoints."""
    if getattr(legacy_app, "_google_ads_offline_conversions_registered", False):
        return

    flask_app = legacy_app.app
    original_activity = legacy_app._crm_activity
    original_save_data = legacy_app.save_data
    original_load_data = legacy_app.load_data
    login_required = legacy_app.login_required
    uploader_cache: dict[str, Any] = {"fingerprint": None, "uploader": None}

    def get_uploader(config: GoogleAdsConfig) -> GoogleAdsUploader:
        fingerprint = (
            config.api_version,
            config.customer_id,
            config.login_customer_id,
            config.developer_token,
            config.client_id,
            config.client_secret,
            config.refresh_token,
            config.conversion_action,
            config.validate_only,
        )
        if uploader_cache["fingerprint"] != fingerprint:
            uploader_cache["fingerprint"] = fingerprint
            uploader_cache["uploader"] = GoogleAdsUploader(config)
        return uploader_cache["uploader"]

    def patched_crm_activity(contact: MutableMapping[str, Any], kind: str, title: str,
                             detail: str = "", preview: str = "") -> Any:
        result = original_activity(contact, kind, title, detail, preview)
        if _is_conversion_activity(kind, title):
            try:
                queue_google_ads_conversion(contact)
            except GoogleAdsContactDataError:
                # A malformed legacy contact must not break its CRM activity.
                pass
        return result

    def patched_save_data(data: MutableMapping[str, Any]) -> Any:
        config = GoogleAdsConfig.from_env()
        if not _data_has_due_conversion(data, config):
            return original_save_data(data)
        with _PROCESS_LOCK:
            try:
                persisted = original_load_data()
                if isinstance(persisted, Mapping):
                    _merge_terminal_states_from_persisted(data, persisted)
                if _data_has_due_conversion(data, config):
                    _process_due_conversions(data, config, get_uploader(config))
            except Exception as exc:  # Never block the CRM because of Ads telemetry.
                print(
                    "Erreur intégration Google Ads hors ligne:",
                    _sanitize_error(exc),
                )
            return original_save_data(data)

    legacy_app._crm_activity = patched_crm_activity
    legacy_app.save_data = patched_save_data

    @flask_app.get("/api/crm/google-ads/status")
    @login_required
    def crm_google_ads_status():
        config = GoogleAdsConfig.from_env()
        data = original_load_data()
        return jsonify(
            {
                "enabled": config.enabled,
                "ready": config.ready,
                "api_version": config.api_version,
                "validate_only": config.validate_only,
                "send_user_identifiers": config.send_user_identifiers,
                "require_click_id": config.require_click_id,
                "ad_user_data_consent": config.ad_user_data_consent,
                "customer_id_configured": bool(config.customer_id),
                "login_customer_id_configured": bool(config.login_customer_id),
                "conversion_action_configured": bool(config.conversion_action),
                "missing_environment_variables": config.missing_environment_variables(),
                "counts": _integration_counts(data),
            }
        )

    @flask_app.post("/api/crm/google-ads/contacts/<contact_id>/retry")
    @login_required
    def crm_google_ads_retry(contact_id: str):
        data = original_load_data()
        contacts = data.get("crm_contacts", []) if isinstance(data, Mapping) else []
        contact = next(
            (
                row
                for row in contacts
                if isinstance(row, MutableMapping) and str(row.get("id")) == contact_id
            ),
            None,
        )
        if contact is None:
            return jsonify({"error": "Fiche CRM introuvable."}), 404
        state = contact.get(GOOGLE_ADS_STATE_KEY)
        if isinstance(state, Mapping) and state.get("status") == "sent":
            return jsonify(
                {
                    "error": "Cette conversion a déjà été transmise.",
                    "google_ads_offline_conversion": state,
                }
            ), 409
        if str(contact.get("statut") or "").strip().casefold() != "converti":
            return jsonify({"error": "La fiche doit être au statut Converti."}), 409
        try:
            queue_google_ads_conversion(contact, force=True)
        except GoogleAdsContactDataError as exc:
            return jsonify({"error": str(exc)}), 422
        legacy_app.save_data(data)
        return jsonify(
            {
                "contact_id": contact_id,
                "google_ads_offline_conversion": contact.get(GOOGLE_ADS_STATE_KEY, {}),
            }
        )

    legacy_app._google_ads_offline_conversions_registered = True
