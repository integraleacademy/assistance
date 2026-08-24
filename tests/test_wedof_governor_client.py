import pytest

import wedof_governor_client as governor


class Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}

    def json(self):
        return self._payload


def test_reservation_identifies_the_crm_and_uses_derived_auth(monkeypatch):
    monkeypatch.setenv("WEDOF_GOVERNOR_ENABLED", "true")
    monkeypatch.setenv("WEDOF_GOVERNOR_SECRET", "shared-secret")
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return Response(payload={"ok": True, "enabled": True})

    monkeypatch.setattr(governor.requests, "post", fake_post)
    result = governor.reserve_wedof_request(
        operation="get_registration_folder",
        method="GET",
        path="/api/registrationFolders/:id",
    )

    assert result["ok"] is True
    assert calls[0][0].endswith("/internal/wedof/governor/reserve")
    assert calls[0][1]["json"]["origin"] == "crm"
    assert calls[0][1]["headers"]["X-Wedof-Governor-Token"]
    assert "shared-secret" not in str(calls)


def test_quota_or_unavailable_governor_blocks_the_wedof_request(monkeypatch):
    monkeypatch.setenv("WEDOF_GOVERNOR_ENABLED", "true")
    monkeypatch.setenv("WEDOF_GOVERNOR_SECRET", "shared-secret")
    monkeypatch.setattr(
        governor.requests, "post", lambda *_args, **_kwargs: Response(429),
    )
    with pytest.raises(governor.WedofQuotaExceeded):
        governor.reserve_wedof_request(
            operation="list_registration_folders", method="GET", path="/api/registrationFolders",
        )

    monkeypatch.setattr(
        governor.requests, "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            governor.requests.ConnectionError("offline")
        ),
    )
    with pytest.raises(governor.WedofGovernorError):
        governor.reserve_wedof_request(
            operation="list_registration_folders", method="GET", path="/api/registrationFolders",
        )


def test_governor_is_disabled_locally_by_default(monkeypatch):
    for name in (
        "WEDOF_GOVERNOR_ENABLED", "RENDER", "RENDER_SERVICE_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        governor.requests, "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("no central call expected in local tests")
        ),
    )
    assert governor.reserve_wedof_request(
        operation="test", method="GET", path="/api/organisms/me",
    )["enabled"] is False
