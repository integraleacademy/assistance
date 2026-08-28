import app as application


def test_cote_azur_admin_displays_ssiap_session_add_form(monkeypatch):
    monkeypatch.setattr(application, "load_data", lambda: {})
    client = application.app.test_client()
    with client.session_transaction() as session:
        session["user_email"] = "clement@integraleacademy.com"

    response = client.get("/admin/formation-sessions")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    panel_start = page.index('<section id="panel-cote_azur"')
    panel_end = page.index("</section>", panel_start)
    cote_azur_panel = page[panel_start:panel_end]

    assert "SSIAP 1 – Agent de sécurité incendie" in cote_azur_panel
    assert 'name="formation" value="SSIAP"' in cote_azur_panel
