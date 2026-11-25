from datetime import date

from crm_app import legacy_app as app
import crm_managed_sessions as managed


def source(**kwargs):
    return dict(dict(id="platform-session", slug="aps", centre="puget", start="2098-02-05", end="2098-03-12",
                     exam="2098-03-13", visibility="public", aliases=[], capacity=12, sold=7), **kwargs)


def test_catalogue_form_crm_and_admin(monkeypatch, tmp_path):
    rows=[source()]
    monkeypatch.setattr(managed,"read",lambda: rows)
    monkeypatch.setattr(app,"DATA_FILE",str(tmp_path/"store.json"))
    monkeypatch.setitem(app.app.config,"CRM_USE_PUBLIC_CALENDAR",False)
    assert b"5 places restantes" in app.app.test_client().get("/demande-informations-formations").data
    local=app.get_formation_sessions({})
    assert any(r.get("catalogue_id")=="platform-session" for r in local["cote_azur"]["APS"])
    assert all("catalogue_id" not in r for r in app.get_stored_formation_sessions({})["cote_azur"]["APS"])
    rows[0]["sold"]=12
    assert b"platform-session" not in app.app.test_client().get("/demande-informations-formations").data
    rows[0]["visibility"]="internal"
    assert all(r.get("catalogue_id")!="platform-session" for r in app.get_formation_sessions({})["cote_azur"]["APS"])
    assert any(r.get("catalogue_id")=="platform-session" for r in app.get_formation_sessions({},include_internal=True)["cote_azur"]["APS"])


def test_moves_and_tombstones_keep_existing_choices(monkeypatch):
    old=source()
    local={"cote_azur":{"APS":[{"label":"Du 5 février 2098 au 12 mars 2098", "date_examen":"2098-03-13"}]}}
    def result(row): return managed.options([row],local,app._session_date_range)["cote_azur"]["APS"]
    moved=source(start="2098-06-05",end="2098-07-12",exam="2098-07-13",aliases=[old])
    only,=result(moved)
    assert only["label"]=="Du 5 juin 2098 au 12 juillet 2098" and only["date_examen"]=="2098-07-13"
    assert not result({**moved,"visibility":"closed"})
    assert not managed.options([source(visibility="match")],{},app._session_date_range)


def test_legacy_links_and_duplicate_reschedules():
    first=source(slug="ssiap-3")
    choice={"id":"old-website-id","formation":"SSIAP3","label":"Du 5 février 2098 au 12 mars 2098","date_examen":first["exam"]}
    verified=managed.website_selection(choice,[first],app._session_date_range)
    assert verified["label"] == choice["label"] and verified["badge"] == "5 places restantes"
    for change in ({"sold":12},{"visibility":"closed"},{"visibility":"internal"},{"start":"2098-04-03","end":"2098-05-04","exam":"2098-05-05","aliases":[first]}):
        assert managed.website_selection(choice,[{**first,**change}],app._session_date_range) is None
    future=source(start="2098-05-05",end="2098-06-12",exam="2098-06-13",aliases=[source()])
    local={"cote_azur":{"APS":[{"label":choice["label"],"date_examen":first["exam"]},
                                 {"label":"Du 5 mai 2098 au 12 juin 2098","date_examen":future["exam"]}]}}
    assert len(managed.options([future],local,app._session_date_range)["cote_azur"]["APS"])==1


def test_ssiap_start_only_move_prefers_exact_previous_key():
    original=source(slug="ssiap-1")
    moved={**original,"start":"2098-02-06","aliases":[original]}
    local={"cote_azur":{"SSIAP":[{"label":"Du 5 février 2098 au 12 mars 2098 (sans SST)","date_examen":original["exam"]}]}}
    only,=managed.options([moved],local,app._session_date_range)["cote_azur"]["SSIAP"]
    assert only["label"].startswith("Du 6 février")


def test_two_ssiap_start_dates_use_one_count_and_correct_fee(monkeypatch, tmp_path):
    import json
    import re
    group = source(slug="ssiap-1", standard_start="2098-02-09", sold=11)
    monkeypatch.setattr(managed, "read", lambda: [group])
    monkeypatch.setattr(app, "DATA_FILE", str(tmp_path/"form.json"))
    monkeypatch.setitem(app.app.config, "CRM_USE_PUBLIC_CALENDAR", False)
    html = app.app.test_client().get("/demande-informations-formations").get_data(as_text=True)
    choices = json.loads(re.search(r"const sessions = (.+?);\n", html)[1])["cote_azur"]["SSIAP"]
    assert [(r["formula"],r["start_date"],r["badge"]) for r in choices] == [
        ("ssiap-1-sst","2098-02-05","1 place restante"), ("ssiap-1","2098-02-09","1 place restante")]
    assert "computeDynamicPlaces" not in html and "ssiap-1-sst" in html
    choice = dict(id="published", formation="SSIAP", label=choices[1]["label"], date_examen=group["exam"], formula="ssiap-1")
    assert managed.website_selection(choice,[group],app._session_date_range)["badge"] == "1 place restante"
    assert managed.website_selection({**choice,"formula":"ssiap-1-sst"},[group],app._session_date_range) is None

def test_ssiap_without_exam_rejects_closed_or_outdated_variants_by_url_and_dates():
    group = source(slug="ssiap-1", standard_start="2098-02-09", exam="", sold=5)
    label = "Du 9 février 2098 au 12 mars 2098 (sans SST)"
    for identifier in (group["id"]+"-sans-sst", "ancienne-fiche"):
        link = dict(id=identifier, formation="SSIAP", label=label, formula="ssiap-1", date_examen="")
        assert managed.website_selection(link, [group], app._session_date_range)["badge"] == "7 places restantes"
        for result in ({"sold":12}, {"visibility":"internal"}, {"visibility":"closed"},
                       {"standard_start":"2098-02-10","aliases":[group]}):
            assert managed.website_selection(link, [{**group,**result}], app._session_date_range) is None
    assert managed.website_selection({**link,"id":group["id"]+"-sans-sst"}, [], app._session_date_range) is None
    view = managed.options([group], {"cote_azur":{"SSIAP":[{"label":label}]}}, app._session_date_range)
    assert len(view["cote_azur"]["SSIAP"]) == 2

def test_named_legacy_formula_without_separate_start_keeps_its_fee():
    row=source(slug="ssiap-1", visibility="match")
    local={"cote_azur":{"SSIAP":[{"label":"Du 5 février 2098 au 12 mars 2098 (sans SST)"},
                               {"label":"Du 5 février 2098 au 12 mars 2098 (avec SST)"},
                               {"label":"Du 5 février 2098 au 12 mars 2098"}]}}
    choices=managed.options([row], local, app._session_date_range)["cote_azur"]["SSIAP"]
    assert [r.get("formula") for r in choices]==["ssiap-1","ssiap-1-sst",None]


def test_retrait_remplacement_et_visibilite_sur_crm_formulaire_et_secretariat(monkeypatch):
    groupe = source(name="Promotion Azur", venue="Salle Atlas", managed=True, edited=True)
    local = {"cote_azur":{"APS":[{"label":"Du 5 février 2098 au 12 mars 2098","date_examen":groupe["exam"]}]}}
    def choices(rows, staff=False):
        return managed.options(rows, local, app._session_date_range, include_internal=staff)["cote_azur"]["APS"]
    ferme = {**groupe, "visibility":"closed"}
    supprime = {**ferme, "deleted":True}
    ancienne_fiche = {**groupe, "id":"gestion-ancienne", "managed":False, "visibility":"match"}
    for staff in (True, False):
        assert choices([ferme, ancienne_fiche], staff) == choices([supprime, ancienne_fiche], staff) == []
    nouveau = {**groupe,"id":"platform-nouveau","sold":2}
    for staff in (False, True):
        assert [r["catalogue_id"] for r in choices([ferme, nouveau], staff)] == [nouveau["id"]]
    for rows in ([supprime,nouveau], [nouveau,supprime,ancienne_fiche]):
        choix, = choices(rows)
        assert (choix["catalogue_id"],choix["session_name"],choix["venue"],choix["badge"]) == (
            nouveau["id"],"Promotion Azur","Salle Atlas","10 places restantes")
        old_url = dict(id=groupe["id"], formation="APS",label=choix["label"],date_examen=groupe["exam"])
        assert managed.website_selection(old_url,rows,app._session_date_range) is None
        assert managed.website_selection({**old_url, "id":"ancienne-fiche-cms"},rows,app._session_date_range) is None
        assert managed.website_selection({**old_url, "id":nouveau["id"]},rows,app._session_date_range)["catalogue_id"] == nouveau["id"]
    report = {**groupe, "visibility":"match", "start":"2098-04-03","end":"2098-05-05","exam":"","aliases":[groupe]}
    choix, = choices([report])
    assert choix["label"] == "Du 3 avril 2098 au 5 mai 2098" and not choix["date_examen"]
    monkeypatch.setattr(managed, "read", lambda:[ferme])
    monkeypatch.setattr(app.crm_public_sessions, "_published_sessions", lambda:{})
    public = app.get_upcoming_formation_sessions({"formation_sessions":local})["cote_azur"]["APS"]
    assert public == []
    desk = next(c for c in app._secretariat_catalogue({"formation_sessions":local}) if c["code"]=="APS")
    assert not any(s.get("session_name")=="Promotion Azur" for c in desk["centres"] for s in c["sessions"])


def test_filter_hides_retired_contact_choice_without_changing_its_record():
    labels = ["Du 5 février 2098 au 12 mars 2098", "Du 4 avril 2098 au 8 mai 2098"]
    contacts = [dict(formation="APS", lieu="Côte d’Azur", dates_formation=label) for label in labels]
    old = source(managed=True)
    moved = {**old, "start":"2098-04-04", "end":"2098-05-08", "aliases":[old]}
    assert managed.withdrawn_contact_filters(contacts,[moved],app._session_date_range) == [["APS","Côte d’Azur",labels[0]]]
    assert len(managed.withdrawn_contact_filters(contacts,[{**moved,"visibility":"closed"}],app._session_date_range)) == 2
    assert contacts[0]["dates_formation"] == labels[0]
