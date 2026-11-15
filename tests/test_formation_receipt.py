from urllib.parse import parse_qs, urlsplit

import pytest

import app as application


FORM = "/demande-informations-formations"
RECEIPT = "/confirmation-demande-informations"


@pytest.mark.parametrize(("formation", "hot", "action"), [
    ("DESP_INIT", False, "Consulter le dossier"),
    ("DESP_INIT", True, "Réserver mon rendez-vous téléphonique"),
    ("DESP_VAE", True, "Accéder à mon espace VAE"),
])
def test_ajax_receipt_is_returned_after_submission(monkeypatch, formation, hot, action):
    store = {}
    monkeypatch.setattr(application, "load_data", lambda: store)
    monkeypatch.setattr(application, "save_data", lambda *_: None)
    monkeypatch.setattr(application, "creer_piste_salesforce", lambda *_: None)
    monkeypatch.setattr(application, "send_email_html", lambda *_: True)
    monkeypatch.setattr(application, "envoyer_sms_demande_infos_formation", lambda *_: True)
    monkeypatch.setattr(application, "get_upcoming_formation_sessions", lambda *args, **kwargs: {})
    client = application.app.test_client()
    fields = {"formation": formation, "souhaite_devis": "OUI", "mail": "prospect@example.invalid"}
    if hot:
        fields.update(cpf_consulte="OUI", france_travail="NON", financement_perso="OUI", identite_numerique="OUI")
    response = client.post(FORM, data=fields, headers={"Accept": "application/json"})
    assert response.status_code == 200
    assert sum(row.get("source") == "demande_infos_formations" for row in store["demandes"]) == 1
    url = response.get_json()["confirmation_url"]
    assert urlsplit(url).path == RECEIPT
    page = client.get(url).get_data(as_text=True)
    assert application.PLAN_FORMATIONS[formation] in page and action in page
    assert "Un membre de notre équipe va bientôt vous contacter pour vous présenter nos formations en détails." in page
    if formation == "DESP_VAE":
        assert application.DESP_VAE_START_URL in page and 'id="vaeForward"' in page
        assert client.post(FORM, data=fields).location == application.DESP_VAE_START_URL
    booking = formation != "DESP_VAE" and hot
    assert ('class="receipt-calendar calendly-inline-widget"' in page) == booking
    assert ('href="#rendez-vous"' in page) == booking
    scene_url = f"{application.PUBLIC_CATALOGUE}/images/illustrations-2026/campus-accompagnement.webp"
    assert (f'src="{scene_url}"' in page) == booking
    if booking:
        assert 'data-resize="true"' in page
        assert f'href="{application.FORMATION_BOOKING_URLS[formation]}"' in page


def test_browser_waits_for_receipt_and_recovers_from_error(monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    monkeypatch.setattr(application, "load_data", lambda: {})
    monkeypatch.setattr(application, "get_upcoming_formation_sessions", lambda *_: {})
    client = application.app.test_client()
    pending = []

    def local_page(route):
        url = urlsplit(route.request.url)
        if url.hostname != "formation.test":
            route.abort()
        elif route.request.method == "POST" and url.path == FORM:
            pending.append(route)
        elif route.request.method == "POST":
            route.fulfill(status=204)
        else:
            result = client.get(url.path, query_string=url.query)
            route.fulfill(status=result.status_code, content_type=result.content_type, body=result.get_data())

    with playwright.sync_playwright() as engine:
        try:
            browser = engine.chromium.launch()
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Chromium is not installed")
            raise
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.route("**/*", local_page)
        page.goto("https://formation.test" + FORM + "?formation=DESP_INIT")
        dialog = page.locator("#sendingDialog")
        page.locator("#infosForm").evaluate("(form) => form.requestSubmit()")
        playwright.expect(dialog).to_be_hidden()
        assert not pending
        next_step = page.locator('[data-nav="next"]:visible')
        for field, value in (
            ("nom", "Test"), ("prenom", "Camille"), ("mail", "prospect@example.invalid"),
            ("mail_confirm", "prospect@example.invalid"), ("telephone", "0600000000"),
        ):
            page.locator("#" + field).fill(value)
        next_step.click()
        next_step.click()
        page.get_by_role("button", name="Paris", exact=False).click()
        next_step.click()

        def answer(question, value):
            page.locator(f'[data-for="{question}"] button[data-value="{value}"]').click()

        answer("cpfConsulte", "NON")
        next_step.click()
        answer("franceTravail", "NON")
        answer("financementPerso", "NON")
        next_step.click()
        answer("identiteNumerique", "NON")
        next_step.click()
        answer("souhaiteDevis", "NON")
        submit = page.locator('[data-nav="submit"]:visible')
        submit.click()
        playwright.expect(dialog).to_be_visible()
        playwright.expect(submit).to_be_disabled()
        page.keyboard.press("Escape")
        page.locator("#infosForm").evaluate("(form) => form.requestSubmit()")
        playwright.expect(dialog).to_be_visible()
        assert len(pending) == 1
        assert parse_qs(pending[0].request.post_data)["formation"] == ["DESP_INIT"]
        pending[0].fulfill(status=503)
        playwright.expect(dialog).to_contain_text("Envoi non confirmé")
        page.get_by_role("button", name="Revenir au formulaire").click()
        playwright.expect(dialog).to_be_hidden()
        playwright.expect(page.locator("#mail")).to_have_value("prospect@example.invalid")
        submit.click()
        playwright.expect(dialog).to_be_visible()
        pending[1].fulfill(json={"confirmation_url": RECEIPT + "?formation=DESP_INIT"})
        page.wait_for_url("**" + RECEIPT + "*")
        fireworks = page.locator("#receiptFireworks")
        playwright.expect(fireworks).to_be_visible()
        assert fireworks.bounding_box() == {"x": 0, "y": 0, "width": 390, "height": 844}
        playwright.expect(page.get_by_role("link", name="Consulter le dossier")).to_be_visible()
        playwright.expect(fireworks).to_be_hidden(timeout=6000)
        page.emulate_media(reduced_motion="reduce")
        page.goto("https://formation.test" + RECEIPT + "?hot=1&formation=DESP_INIT")
        playwright.expect(fireworks).to_be_hidden()
        reserve = page.get_by_role("link", name="Réserver mon rendez-vous téléphonique")
        widget = page.locator(".receipt-calendar")
        scene = page.locator(".receipt-welcome-scene")
        for width, height in [(1360, 768), (768, 800), (390, 844), (320, 568)]:
            page.set_viewport_size({"width": width, "height": height})
            page.evaluate("window.scrollTo(0, 0)")
            box = reserve.bounding_box()
            assert 0 <= box["y"] < box["y"] + box["height"] <= height
            illustration = scene.bounding_box()
            assert (
                illustration["x"] >= box["x"] + box["width"]
                or illustration["y"] + illustration["height"] <= box["y"]
            )
            assert page.evaluate("document.documentElement.scrollWidth === innerWidth")
            assert widget.bounding_box()["width"] >= 320
            reserve.click()
            assert page.url.endswith("#rendez-vous")
            playwright.expect(widget).to_be_in_viewport()
            assert page.evaluate("document.activeElement.id") == "rendez-vous"
            # The inline widget applies the height of each booking step on this element.
            for step_height in (1120, 670):
                widget.evaluate("(el, h) => { el.style.height = h + 'px'; }", step_height)
                assert widget.bounding_box()["height"] == step_height
        playwright.expect(page.get_by_role("link", name="Ouvrir l’agenda en plein écran")).to_be_visible()
        page.clock.install()
        vae_receipt = "https://formation.test" + RECEIPT + "?formation=DESP_VAE&poursuivre=1"
        page.goto(vae_receipt)
        page.get_by_role("button", name="Rester sur cette page").click()
        page.clock.fast_forward(7000)
        assert page.url == vae_receipt
        page.goto(vae_receipt + "&hot=1")
        with page.expect_request(application.DESP_VAE_START_URL):
            page.clock.fast_forward(7000)
        browser.close()
