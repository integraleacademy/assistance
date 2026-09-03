"""Client minimal et testable pour l'API Yousign v3.

Les secrets restent exclusivement côté serveur et sont lus depuis les mêmes
variables d'environnement que la plateforme de gestion des sessions.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional


logger = logging.getLogger("yousign")

DEFAULT_YOUSIGN_API_BASE_URL = "https://api.yousign.app/v3"
YOUSIGN_EXTERNAL_ID_MAX_LENGTH = 180


class YousignError(RuntimeError):
    """Erreur lisible levée lorsqu'un appel Yousign échoue."""

    def __init__(self, message: str, status_code: Optional[int] = None,
                 payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


@dataclass(frozen=True)
class YousignConfig:
    api_key: str
    base_url: str
    webhook_secret: str = ""
    signature_level: str = "electronic_signature"
    authentication_mode: str = "no_otp"
    delivery_mode: str = "email"
    workspace_id: str = ""


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def get_yousign_config() -> YousignConfig:
    base_url = (
        _env("YOUSIGN_BASE_URL")
        or _env("YOUSIGN_API_BASE_URL", DEFAULT_YOUSIGN_API_BASE_URL)
    ).rstrip("/")
    return YousignConfig(
        api_key=_env("YOUSIGN_API_KEY"),
        base_url=base_url,
        webhook_secret=_env("YOUSIGN_WEBHOOK_SECRET"),
        signature_level=_env(
            "YOUSIGN_SIGNATURE_LEVEL", "electronic_signature"
        ),
        authentication_mode=_env("YOUSIGN_AUTHENTICATION_MODE", "no_otp"),
        delivery_mode=_env("YOUSIGN_DELIVERY_MODE", "email"),
        workspace_id=_env("YOUSIGN_WORKSPACE_ID"),
    )


def is_yousign_configured() -> bool:
    return bool(get_yousign_config().api_key)


def is_yousign_sandbox() -> bool:
    return "api-sandbox.yousign.app" in get_yousign_config().base_url.lower()


def sanitize_yousign_external_id(value: str,
                                 fallback: str = "hebergement") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_\-@.%+ ]+", "-", value or "")
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip(" -")
    return cleaned[:YOUSIGN_EXTERNAL_ID_MAX_LENGTH] or fallback


def normalize_french_mobile(phone: str) -> str:
    """Normalise un mobile français au format E.164 exigé pour l'OTP SMS."""
    cleaned = re.sub(r"[\s.\-()]+", "", phone or "")
    if cleaned.startswith("0033"):
        cleaned = "+33" + cleaned[4:]
    if cleaned.startswith("06") or cleaned.startswith("07"):
        cleaned = "+33" + cleaned[1:]
    if cleaned.startswith("+33") and re.fullmatch(r"\+33[67]\d{8}", cleaned):
        return cleaned
    raise YousignError(
        "Le numéro de téléphone portable est absent ou invalide pour le code "
        "SMS Yousign."
    )


# Alias conservé pour rester cohérent avec l'intégration de la plateforme Sessions.
normalizeFrenchPhoneNumber = normalize_french_mobile


def yousign_service_access_message(status_code: Optional[int],
                                   payload: Any = None) -> str:
    message = payload.get("message") if isinstance(payload, dict) else ""
    if status_code == 401:
        return "Clé API Yousign invalide ou absente."
    if status_code == 403:
        return (
            "Yousign refuse l'accès au service de signature. Vérifiez la clé "
            "API, l'environnement, le workspace et l'abonnement Yousign."
        )
    return message or (
        f"Erreur API Yousign ({status_code})"
        if status_code else "Impossible de joindre Yousign."
    )


class YousignClient:
    def __init__(self, config: Optional[YousignConfig] = None,
                 timeout: int = 20):
        self.config = config or get_yousign_config()
        self.timeout = timeout

    def _headers(self, content_type: Optional[str] = "application/json") \
            -> Dict[str, str]:
        if not self.config.api_key:
            raise YousignError(
                "Configuration Yousign incomplète : YOUSIGN_API_KEY est "
                "manquante."
            )
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Accept": "application/json",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _url(self, path: str) -> str:
        return f"{self.config.base_url}/{path.lstrip('/')}"

    def request_with_http_status(self, method: str, path: str,
                                 payload: Any = None,
                                 headers: Optional[Dict[str, str]] = None):
        body = None
        request_headers = self._headers()
        if headers:
            request_headers.update(headers)
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        url = self._url(path)
        req = urllib.request.Request(
            url, data=body, headers=request_headers, method=method.upper()
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
                response_payload = {}
                if raw:
                    charset = response.headers.get_content_charset() or "utf-8"
                    response_payload = json.loads(raw.decode(charset))
                return response_payload, response.status, url
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                error_payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                error_payload = {"raw": raw[:500]}
            logger.warning(
                "Yousign API error status=%s path=%s response=%r",
                exc.code, path, error_payload,
            )
            message = (
                error_payload.get("message")
                if isinstance(error_payload, dict) else None
            )
            raise YousignError(
                message or f"Erreur API Yousign ({exc.code})",
                exc.code,
                error_payload,
            ) from exc
        except urllib.error.URLError as exc:
            logger.warning(
                "Yousign network error path=%s reason=%s", path, exc.reason
            )
            raise YousignError("Impossible de joindre l'API Yousign.") from exc

    def request(self, method: str, path: str, payload: Any = None,
                headers: Optional[Dict[str, str]] = None) -> Any:
        response, _status, _url = self.request_with_http_status(
            method, path, payload, headers
        )
        return response

    def create_signature_request(self, name: str,
                                 external_id: str = "") -> Any:
        payload = {
            "name": name[:128],
            "delivery_mode": self.config.delivery_mode,
        }
        if self.config.workspace_id:
            payload["workspace_id"] = self.config.workspace_id
        if external_id:
            payload["external_id"] = sanitize_yousign_external_id(external_id)
        return self.request("POST", "signature_requests", payload)

    def upload_file(self, signature_request_id: str, pdf_bytes: bytes,
                    filename: str, parse_anchors: bool = False) -> Any:
        boundary = "----assistance-hebergement-yousign-boundary"
        safe_filename = filename.replace('"', "") or "convention.pdf"
        parts = [
            (
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="file"; filename="{safe_filename}"\r\n'
                "Content-Type: application/pdf\r\n\r\n"
            ).encode(),
            pdf_bytes,
            (
                f"\r\n--{boundary}\r\nContent-Disposition: form-data; "
                'name="nature"\r\n\r\nsignable_document\r\n'
            ).encode(),
            (
                f"--{boundary}\r\nContent-Disposition: form-data; "
                'name="parse_anchors"\r\n\r\n'
                f"{str(bool(parse_anchors)).lower()}\r\n--{boundary}--\r\n"
            ).encode(),
        ]
        req = urllib.request.Request(
            self._url(
                "signature_requests/"
                f"{urllib.parse.quote(signature_request_id)}/documents"
            ),
            data=b"".join(parts),
            headers=self._headers(
                f"multipart/form-data; boundary={boundary}"
            ),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(
                    response.read().decode(
                        response.headers.get_content_charset() or "utf-8"
                    )
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            logger.warning(
                "Yousign upload failed status=%s response=%s",
                exc.code, raw[:2000],
            )
            raise YousignError(
                "Échec de l'envoi du PDF à Yousign.", exc.code, raw[:2000]
            ) from exc

    def add_signer(self, signature_request_id: str, first_name: str,
                   last_name: str, email: str, phone_number: str) -> Any:
        normalized_phone = normalize_french_mobile(phone_number)
        payload = {
            "info": {
                "first_name": first_name or "Stagiaire",
                "last_name": last_name or first_name or "Intégrale",
                "email": email,
                "phone_number": normalized_phone,
                "locale": "fr",
            },
            "signature_level": "electronic_signature",
            "signature_authentication_mode": "otp_sms",
            "delivery_mode": "email",
        }
        signer = self.request(
            "POST",
            "signature_requests/"
            f"{urllib.parse.quote(signature_request_id)}/signers",
            payload,
        )
        if isinstance(signer, dict):
            returned_mode = str(
                signer.get("signature_authentication_mode") or ""
            ).strip()
            if returned_mode and returned_mode != "otp_sms":
                raise YousignError(
                    "Yousign n'a pas confirmé l'authentification du stagiaire "
                    "par code SMS.",
                    payload=signer,
                )
        return signer

    def add_signature_field(self, signature_request_id: str,
                            document_id: str, signer_id: str, page: int,
                            x: int, y: int, width: int = 170,
                            height: int = 45) -> Any:
        payload = {
            "type": "signature",
            "signer_id": signer_id,
            "page": int(page),
            "x": int(x),
            "y": int(y),
            "width": int(width),
            "height": int(height),
        }
        return self.request(
            "POST",
            "signature_requests/"
            f"{urllib.parse.quote(signature_request_id)}/documents/"
            f"{urllib.parse.quote(document_id)}/fields",
            payload,
        )

    def activate_signature_request(self, signature_request_id: str) -> Any:
        return self.request(
            "POST",
            "signature_requests/"
            f"{urllib.parse.quote(signature_request_id)}/activate",
        )

    def cancel_signature_request(self, signature_request_id: str,
                                 custom_note: str = "") -> Any:
        payload = {"reason": "errors_in_document"}
        if custom_note:
            payload["custom_note"] = custom_note[:500]
        return self.request(
            "POST",
            "signature_requests/"
            f"{urllib.parse.quote(signature_request_id)}/cancel",
            payload,
        )

    def send_signer_reminder(self, signature_request_id: str,
                             signer_id: str) -> Any:
        return self.request(
            "POST",
            "signature_requests/"
            f"{urllib.parse.quote(signature_request_id)}/signers/"
            f"{urllib.parse.quote(signer_id)}/send_reminder",
        )

    def get_signature_request(self, signature_request_id: str) -> Any:
        return self.request(
            "GET",
            "signature_requests/"
            f"{urllib.parse.quote(signature_request_id)}",
        )

    def get_signature_request_signers(self,
                                      signature_request_id: str) -> Any:
        return self.request(
            "GET",
            "signature_requests/"
            f"{urllib.parse.quote(signature_request_id)}/signers",
        )

    def download_signed_documents(self, signature_request_id: str) -> bytes:
        req = urllib.request.Request(
            self._url(
                "signature_requests/"
                f"{urllib.parse.quote(signature_request_id)}/documents/download"
            ),
            headers=self._headers(None),
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise YousignError(
                "Téléchargement du document signé impossible.",
                exc.code,
                raw[:1000],
            ) from exc
