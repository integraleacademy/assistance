"""Behavioural coverage for the synthetic New META pipeline bucket."""
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_new_meta_bucket_separates_new_leads_without_mutating_them():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to exercise the CRM browser helpers")

    source = (ROOT / "static/crm.js").read_text(encoding="utf-8")

    def section(start, end):
        start_at = source.index(start)
        return source[start_at:source.index(end, start_at)]

    production = "\n".join(
        [
            section("const isActiveLead=", "const manualNextActionValue="),
            section("const pipelineOverviewStatuses=", "const timelineButtons="),
            section("const crmOriginFilterValues=", "const dashboardHasContact="),
        ]
    )
    script = r"""
const assert = require('node:assert/strict');
const S = ['Nouveaux', 'A relancer'];
const SECONDARY_STATUSES = ['Transition pro', 'Nouveaux'];
""" + production + r"""

const leads = [
  Object.freeze({id:'meta', statut:'Nouveaux', origine:'META'}),
  Object.freeze({id:'facebook', statut:'Nouveaux', origine:' Facebook '}),
  Object.freeze({id:'instagram', statut:'Nouveaux', source:'Instagram'}),
  Object.freeze({id:'meta-source', statut:'Nouveaux', meta_source:{lead_id:'lead'}}),
  Object.freeze({id:'explicit-google', statut:'Nouveaux', origine:'Google Ads', meta_source:{lead_id:'lead'}}),
  Object.freeze({id:'site', statut:'Nouveaux', origine:'Site internet'}),
  Object.freeze({id:'meta-relance', statut:'A relancer', origine:'META'}),
  Object.freeze({id:'meta-secondary', statut:'Nouveaux', statut_secondaire:'Transition pro', origine:'meta ads'}),
  Object.freeze({id:'converted', statut:'Converti', statut_secondaire:'Nouveaux', origine:'META'}),
  Object.freeze({id:'disqualified', statut:'Disqualifié', statut_secondaire:'Nouveaux', origine:'META'}),
];
const before = JSON.stringify(leads);
const ids = status => leads.filter(lead => contactHasPipelineStatus(lead, status)).map(lead => lead.id);

assert.deepEqual(pipelineOverviewStatuses(), ['Nouveaux', 'Nouveaux META', 'A relancer', 'Transition pro']);
assert.deepEqual(ids('Nouveaux'), ['explicit-google', 'site']);
assert.deepEqual(ids('Nouveaux META'), ['meta', 'facebook', 'instagram', 'meta-source', 'meta-secondary']);
assert.deepEqual(ids('A relancer'), ['meta-relance']);
assert.deepEqual(ids('Transition pro'), ['meta-secondary']);
assert.equal(JSON.stringify(leads), before);
    """
    result = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_pipeline_bucket_controls_expose_their_pressed_state():
    source = (ROOT / "static/crm.js").read_text(encoding="utf-8")

    assert "const NEW_META_PIPELINE_STATUS='Nouveaux META'" in source
    assert 'type="button" class="status-kpi' in source
    assert 'aria-pressed="${statusFilter===s}"' in source
    assert "activeContacts.filter(c=>contactHasPipelineStatus(c,s)).length" in source
