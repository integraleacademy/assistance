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
    assert managed.website_selection(choice,[first],app._session_date_range)==choice
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
