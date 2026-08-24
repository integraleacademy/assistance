from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[1]
WORKSPACE_JS = ROOT / "static" / "crm_workspace.js"
WORKSPACE_CSS = ROOT / "static" / "crm_workspace.css"


def test_relances_view_exposes_active_leads_without_a_planned_date():
    javascript = WORKSPACE_JS.read_text(encoding="utf-8")
    stylesheet = WORKSPACE_CSS.read_text(encoding="utf-8")

    status_helper = javascript[
        javascript.index("function reminderDate"):
        javascript.index("function reminderCardControl")
    ]
    period_helper = javascript[
        javascript.index("function reminderPeriodMatches"):
        javascript.index("function remindersPage")
    ]
    script = f"""
const dayKey=value=>value instanceof Date?value.toISOString().slice(0,10):value;
const isOverdue=contact=>Boolean(contact.relance_date&&contact.relance_date<'2026-08-24');
{status_helper}
{period_helper}
const assert=(condition,message)=>{{if(!condition)throw new Error(message)}};
const missing={{relance_date:''}};
const overdue={{relance_date:'2026-08-23'}};
const today={{relance_date:'2026-08-24'}};
const future={{relance_date:'2026-09-04'}};
const invalid={{relance_date:'2026-02-30'}};
assert(reminderStatus(missing)[1]==='missing','missing date gets a dedicated status');
assert(reminderStatus(invalid)[1]==='missing','invalid date is handled like the pipeline missing state');
assert(hasReminderStatus({{statut:'A relancer'}}),'primary follow-up status is included');
assert(hasReminderStatus({{statut:'Nouveaux',statut_secondaire:'A relancer'}}),'secondary follow-up status is included');
assert(reminderPeriodMatches(missing,'missing','2026-08-24'),'missing mode includes undated leads');
assert(reminderPeriodMatches(invalid,'missing','2026-08-24'),'missing mode includes invalid dates');
assert(!reminderPeriodMatches(today,'missing','2026-08-24'),'missing mode excludes dated leads');
assert(reminderPeriodMatches(overdue,'overdue','2026-08-24'),'overdue mode is preserved');
assert(reminderPeriodMatches(today,'date','2026-08-24'),'daily mode is preserved');
assert(reminderPeriodMatches(future,'planned','2026-08-24'),'planned mode includes dated leads');
assert(!reminderPeriodMatches(missing,'planned','2026-08-24'),'planned mode excludes undated leads');
console.log('CRM missing follow-ups: OK');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "CRM missing follow-ups: OK" in completed.stdout
    missing_metric = 'class="metric-action missing-metric" id="reminderMissing"'
    overdue_metric = 'class="metric-action overdue-metric" id="reminderOverdue"'
    assert missing_metric in javascript
    assert javascript.index(missing_metric) < javascript.index(overdue_metric)
    assert "const followUpContacts=ctx.contacts.filter(contact=>hasReminderStatus(contact)&&!contact.archived_at)" in javascript
    assert "const missing=followUpContacts.filter(contact=>!reminderDate(contact))" in javascript
    assert "periodMode==='missing'?'Pistes sans relance programmée'" in javascript
    assert "'<button class=\"btn blue\" data-reminder-reschedule>Planifier</button>" in javascript
    assert ".reminder-metrics{grid-template-columns:repeat(5,minmax(0,1fr))}" in stylesheet
    assert ".workspace-metrics article.missing-metric.active" in stylesheet
