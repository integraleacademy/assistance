import pytest

from yousign_service import (
    YousignClient,
    YousignConfig,
    YousignError,
    normalize_french_mobile,
    sanitize_yousign_external_id,
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("06 12 34 56 78", "+33612345678"),
        ("07.12.34.56.78", "+33712345678"),
        ("+33 6 12 34 56 78", "+33612345678"),
        ("0033 7 12 34 56 78", "+33712345678"),
    ],
)
def test_normalize_french_mobile_for_yousign_otp(source, expected):
    assert normalize_french_mobile(source) == expected


def test_normalize_french_mobile_rejects_landline():
    with pytest.raises(YousignError, match="portable"):
        normalize_french_mobile("04 94 00 00 00")


def test_yousign_signer_always_uses_email_and_sms_otp():
    class RecordingClient(YousignClient):
        def request(self, method, path, payload=None, headers=None):
            self.recorded = (method, path, payload, headers)
            return {"id": "signer-1"}

    client = RecordingClient(YousignConfig(
        api_key="test",
        base_url="https://api-sandbox.yousign.app/v3",
    ))

    signer = client.add_signer(
        "request-1",
        "Lina",
        "Martin",
        "lina@example.com",
        "06 12 34 56 78",
    )

    assert signer == {"id": "signer-1"}
    payload = client.recorded[2]
    assert payload["delivery_mode"] == "email"
    assert payload["signature_level"] == "electronic_signature"
    assert payload["signature_authentication_mode"] == "otp_sms"
    assert payload["info"]["phone_number"] == "+33612345678"


def test_yousign_signer_rejects_a_downgraded_authentication_mode():
    class DowngradedClient(YousignClient):
        def request(self, method, path, payload=None, headers=None):
            return {
                "id": "signer-1",
                "signature_authentication_mode": "no_otp",
            }

    client = DowngradedClient(YousignConfig(
        api_key="test",
        base_url="https://api-sandbox.yousign.app/v3",
    ))

    with pytest.raises(YousignError, match="code SMS"):
        client.add_signer(
            "request-1",
            "Lina",
            "Martin",
            "lina@example.com",
            "06 12 34 56 78",
        )


def test_yousign_external_id_is_sanitized_and_limited():
    value = sanitize_yousign_external_id("hébergement/<Martin> " + "x" * 300)

    assert "<" not in value
    assert "/" not in value
    assert len(value) <= 180
