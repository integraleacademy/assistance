from datetime import datetime, timezone

from crm_cnaps_tracking import scoring_snapshot_from_remote


PRODUCTION_PAYLOAD = {
    "found": True,
    "linked": True,
    "cnaps": {"cnaps_status": "TRANSMIS", "nub": "1084892", "titles": []},
}


def test_production_payload_preserves_transmitted_status_without_titles():
    snapshot = scoring_snapshot_from_remote(
        PRODUCTION_PAYLOAD, now=datetime(2026, 8, 4, tzinfo=timezone.utc))
    assert snapshot["normalized_status"] == "transmitted"
    assert snapshot["raw_status"] == "TRANSMIS"
    assert snapshot["has_active_professional_title"] is False
    assert snapshot["has_expired_professional_title"] is False


def test_successful_remote_snapshot_replaces_stale_unknown():
    old_snapshot = {"normalized_status": "unknown"}
    new_snapshot = scoring_snapshot_from_remote({"cnaps": {"cnaps_status": "TRANSMIS", "titles": []}})
    assert old_snapshot["normalized_status"] == "unknown"
    assert new_snapshot["normalized_status"] == "transmitted"
    assert new_snapshot["raw_status"] == "TRANSMIS"
