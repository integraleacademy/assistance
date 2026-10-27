from pathlib import Path
import subprocess

import pytest

import app as application


ROOT = Path(__file__).resolve().parents[1]
CRM_JS = ROOT / "static" / "crm.js"
UNAVAILABLE_ERROR = (
    "Les relances ne peuvent pas être programmées le week-end, un jour férié "
    "ou pendant la fermeture du 24 décembre 2026 au 3 janvier 2027."
)


@pytest.mark.parametrize("value", ["2026-09-14", "2026-09-18", "2027-01-04"])
def test_user_followup_date_accepts_available_weekdays(value):
    assert application._crm_relance_date(value, weekdays_only=True) == value


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-19",
        "2026-09-20",
        "2026-07-14",
        "2026-04-06",
        "2026-05-14",
        "2026-05-25",
        "2026-12-24",
        "2026-12-31",
        "2027-01-03",
    ],
)
def test_user_followup_date_rejects_unavailable_days(value):
    with pytest.raises(ValueError, match="week-end, un jour férié"):
        application._crm_relance_date(value, weekdays_only=True)


def test_closure_is_inclusive_and_stops_on_january_fourth():
    assert application.CRM_RELANCE_CLOSURES == (
        (application.datetime.date(2026, 12, 24), application.datetime.date(2027, 1, 3)),
    )
    assert application._crm_relance_date_is_unavailable(
        application.datetime.date(2027, 1, 3)
    )
    assert not application._crm_relance_date_is_unavailable(
        application.datetime.date(2027, 1, 4)
    )


def test_historical_unavailable_followup_remains_readable():
    assert application._crm_relance_date("2026-09-19") == "2026-09-19"
    assert application._crm_relance_date("2026-12-25") == "2026-12-25"
    assert application._crm_relance_date("") == ""


def test_browser_rejects_unavailable_days_and_uses_expected_defaults():
    javascript = CRM_JS.read_text(encoding="utf-8")
    start = javascript.index("const CRM_RELANCE_UNAVAILABLE_MESSAGE")
    end = javascript.index("function relaunchModal", start)
    helpers = javascript[start:end]
    script = helpers + r"""
if(!relanceDateIsUnavailable('2026-09-19'))throw new Error('Saturday accepted');
if(!relanceDateIsUnavailable('2026-07-14'))throw new Error('Fixed holiday accepted');
if(!relanceDateIsUnavailable('2026-04-06'))throw new Error('Easter Monday accepted');
if(!relanceDateIsUnavailable('2026-12-24'))throw new Error('Closure start accepted');
if(!relanceDateIsUnavailable('2027-01-03'))throw new Error('Closure end accepted');
if(relanceDateIsUnavailable('2027-01-04'))throw new Error('Available Monday rejected');
const friday=new Date('2026-09-18T12:00:00Z');
if(nextRelanceWeekday(1,friday)!=='2026-09-21')throw new Error('Bad weekend default');
const mondayBeforeHoliday=new Date('2026-07-13T12:00:00Z');
if(nextRelanceWeekday(1,mondayBeforeHoliday)!=='2026-07-15')throw new Error('Bad holiday default');
const beforeClosure=new Date('2026-12-23T12:00:00Z');
if(nextRelanceWeekday(1,beforeClosure)!=='2027-01-04')throw new Error('Bad closure default');
console.log('CRM available relances: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script], cwd=ROOT, check=True,
        capture_output=True, text=True,
    )
    assert "CRM available relances: OK" in completed.stdout
    assert javascript.count(UNAVAILABLE_ERROR) == 1
    assert javascript.count("relanceDateIsUnavailable(") >= 3
    assert "const defaultDate=nextRelanceWeekday(7)" in javascript
    assert "weekdays_only=True" in (ROOT / "app.py").read_text(encoding="utf-8")
