from pathlib import Path
import subprocess

import pytest

import app as application


ROOT = Path(__file__).resolve().parents[1]
CRM_JS = ROOT / "static" / "crm.js"
WEEKEND_ERROR = (
    "Les relances ne peuvent pas être programmées le samedi ou le dimanche."
)


@pytest.mark.parametrize("value", ["2026-09-14", "2026-09-18"])
def test_user_followup_date_accepts_weekdays(value):
    assert application._crm_relance_date(value, weekdays_only=True) == value


@pytest.mark.parametrize("value", ["2026-09-19", "2026-09-20"])
def test_user_followup_date_rejects_weekends(value):
    with pytest.raises(ValueError, match="samedi ou le dimanche"):
        application._crm_relance_date(value, weekdays_only=True)


def test_historical_weekend_followup_remains_readable():
    assert application._crm_relance_date("2026-09-19") == "2026-09-19"
    assert application._crm_relance_date("") == ""


def test_browser_rejects_weekends_and_defaults_to_the_next_weekday():
    javascript = CRM_JS.read_text(encoding="utf-8")
    start = javascript.index("const CRM_RELANCE_WEEKEND_MESSAGE")
    end = javascript.index("function relaunchModal", start)
    helpers = javascript[start:end]
    script = helpers + r"""
if(!relanceDateIsWeekend('2026-09-19'))throw new Error('Saturday accepted');
if(!relanceDateIsWeekend('2026-09-20'))throw new Error('Sunday accepted');
if(relanceDateIsWeekend('2026-09-18'))throw new Error('Friday rejected');
const friday=new Date('2026-09-18T12:00:00Z');
if(nextRelanceWeekday(1,friday)!=='2026-09-21')throw new Error('Bad Saturday default');
if(nextRelanceWeekday(2,friday)!=='2026-09-21')throw new Error('Bad Sunday default');
console.log('CRM weekday relances: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script], cwd=ROOT, check=True,
        capture_output=True, text=True,
    )
    assert "CRM weekday relances: OK" in completed.stdout
    assert javascript.count(WEEKEND_ERROR) == 1
    assert "weekdays_only=True" in (ROOT / "app.py").read_text(encoding="utf-8")
