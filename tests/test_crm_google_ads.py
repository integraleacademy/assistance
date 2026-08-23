from __future__ import annotations

import hashlib
from types import SimpleNamespace

import crm_google_ads as mod


def configured_env(monkeypatch, **overrides):
    values = {
        "GOOGLE_ADS_OFFLINE_CONVERSIONS_ENABLED": "true",
        "GOOGLE_ADS_API_VERSION": "v25",
        "GOOGLE_ADS_CUSTOMER_ID": "123-456-7890",
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "999-888-7777",
        "GOOGLE_ADS_DEVELOPER_TOKEN": "developer-token",
        "GOOGLE_ADS_CLIENT_ID": "client-id",
        "GOOGLE_ADS_CLIENT_SECRET": "client-secret",
        "GOOGLE_ADS_REFRESH_TOKEN": "refresh-token",
        "GOOGLE_ADS_CONVERSION_ACTION_ID": "987654321",
        "GOOGLE_ADS_CURRENCY": "EUR",
        "GOOGLE_ADS_SEND_USER_IDENTIFIERS": "true",
        "GOOGLE_ADS_REQUIRE_CLICK_ID": "false",
        "GOOGLE_ADS_AD_USER_DATA_CONSENT": "GRANTED",
        "GOOGLE_ADS_VALIDATE_ONLY": "false",
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return mod.GoogleAdsConfig.from_env()


def test_normalizes_and_hashes_google_customer_identifiers():
    assert mod.normalize_email_for_google_ads(" Jean.Dupont@GoogleMail.com ") == "jeandupont@gmail.com"
    assert mod.normalize_phone_for_google_ads("06 12 34 56 78") == "+33612345678"
    assert mod.sha256_hex("test") == hashlib.sha256(b"test").hexdigest()


def test_build_click_conversion_combines_click_and_hashed_identifiers(monkeypatch):
    config = configured_env(monkeypatch)
    contact = {
        "id": "contact-123",
        "statut": "Converti",
        "gclid": "TEST-GCLID",
        "mail": "Jean.Dupont@gmail.com",
        "telephone": "06 12 34 56 78",
        "prix_vente": "1 650,00 €",
        "converted_at": "2026-08-21T09:15:00+02:00",
    }

    conversion, audit = mod.build_click_conversion(contact, config)

    assert conversion["conversionAction"] == (
        "customers/1234567890/conversionActions/987654321"
    )
    assert conversion["gclid"] == "TEST-GCLID"
    assert conversion["conversionValue"] == 1650.0
    assert conversion["currencyCode"] == "EUR"
    assert conversion["conversionDateTime"] == "2026-08-21 09:15:00+02:00"
    assert conversion["orderId"] == "crm-contact-123"
    assert conversion["consent"] == {"adUserData": "GRANTED"}
    assert len(conversion["userIdentifiers"]) == 2
    assert conversion["userIdentifiers"][0]["hashedEmail"] == mod.sha256_hex(
        "jeandupont@gmail.com"
    )
    assert conversion["userIdentifiers"][1]["hashedPhoneNumber"] == mod.sha256_hex(
        "+33612345678"
    )
    assert audit["identifier_mode"] == "gclid+email+phone"


def test_click_identifier_is_required_by_default(monkeypatch):
    config = configured_env(
        monkeypatch,
        GOOGLE_ADS_SEND_USER_IDENTIFIERS="false",
        GOOGLE_ADS_REQUIRE_CLICK_ID="true",
        GOOGLE_ADS_AD_USER_DATA_CONSENT="UNSPECIFIED",
    )
    contact = {
        "id": "contact-123",
        "statut": "Converti",
        "prix_vente": 1650,
        "converted_at": "2026-08-21T09:15:00+02:00",
    }

    try:
        mod.build_click_conversion(contact, config)
    except mod.GoogleAdsContactDataError as exc:
        assert "GCLID" in str(exc)
    else:
        raise AssertionError("A contact without click id should have been rejected")


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url == mod._TOKEN_URL:
            return FakeResponse(200, {"access_token": "access", "expires_in": 3600})
        return FakeResponse(
            200,
            {
                "jobId": "jobs/42",
                "results": [
                    {
                        "conversionAction": (
                            "customers/1234567890/conversionActions/987654321"
                        ),
                        "conversionDateTime": "2026-08-21 09:15:00+02:00",
                    }
                ],
            },
        )


def test_uploader_uses_oauth_headers_and_partial_failure(monkeypatch):
    config = configured_env(monkeypatch)
    session = FakeSession()
    uploader = mod.GoogleAdsUploader(config, session=session)
    conversion = {
        "conversionAction": config.conversion_action_resource,
        "gclid": "TEST-GCLID",
        "conversionDateTime": "2026-08-21 09:15:00+02:00",
        "conversionValue": 1650.0,
        "currencyCode": "EUR",
        "orderId": "crm-contact-123",
    }

    result = uploader.upload(conversion)

    assert result["job_id"] == "jobs/42"
    assert len(session.calls) == 2
    upload_url, upload_call = session.calls[1]
    assert upload_url.endswith(
        "/v25/customers/1234567890:uploadClickConversions"
    )
    assert upload_call["headers"]["Authorization"] == "Bearer access"
    assert upload_call["headers"]["developer-token"] == "developer-token"
    assert upload_call["headers"]["login-customer-id"] == "9998887777"
    assert upload_call["json"]["partialFailure"] is True
    assert upload_call["json"]["validateOnly"] is False


def test_registration_queues_only_real_conversion_activity(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_OFFLINE_CONVERSIONS_ENABLED", "false")

    class FakeApp:
        def get(self, _path):
            return lambda func: func

        def post(self, _path):
            return lambda func: func

    app = FakeApp()
    persisted = {"crm_contacts": []}

    def activity(contact, kind, title, detail="", preview="", **options):
        contact.setdefault("activities", []).append(
            {
                "kind": kind,
                "title": title,
                "detail": detail,
                "preview": preview,
                **options,
            }
        )

    def load_data():
        return persisted

    def save_data(data):
        persisted.clear()
        persisted.update(data)

    def login_required(func):
        return func

    legacy = SimpleNamespace(
        app=app,
        _crm_activity=activity,
        load_data=load_data,
        save_data=save_data,
        login_required=login_required,
    )
    mod.register_google_ads_offline_conversions(legacy)

    contact = {"id": "1", "statut": "Converti", "activities": []}
    legacy._crm_activity(
        contact,
        "email",
        "E-mail envoyé",
        author_name="France Travail",
    )
    assert mod.GOOGLE_ADS_STATE_KEY not in contact
    assert contact["activities"][0]["author_name"] == "France Travail"

    legacy._crm_activity(contact, "statut", "Statut : Converti")
    assert contact[mod.GOOGLE_ADS_STATE_KEY]["status"] == "pending"

    data = {"crm_contacts": [contact]}
    legacy.save_data(data)
    assert contact[mod.GOOGLE_ADS_STATE_KEY]["status"] == "blocked"
    assert contact[mod.GOOGLE_ADS_STATE_KEY]["blocked_reason"] == "configuration"
