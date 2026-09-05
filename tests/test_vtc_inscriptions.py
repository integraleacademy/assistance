import re

import app as application


def test_vtc_inscriptions_page_is_public_and_contains_the_official_cpf_offer():
    response = application.app.test_client().get("/inscriptions")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Démarrez votre projet <em>Chauffeur VTC</em>" in page
    assert application.VTC_CPF_REGISTRATION_URL in page
    assert 'id="cpfRegistrationLink"' in page
    assert 'target="_blank" rel="noopener noreferrer"' in page


def test_vtc_inscriptions_gates_cpf_and_digital_identity_before_the_cpf_link():
    page = application.app.test_client().get("/inscriptions").get_data(as_text=True)

    assert re.search(r'id="financingPanel" data-step="1">', page)
    assert re.search(r'id="identityPanel" data-step="2" hidden>', page)
    assert re.search(r'id="cpfPanel" data-step="3" hidden>', page)
    assert set(re.findall(r'name="cpf_full" value="([^"]+)"', page)) == {"yes", "no"}
    assert set(re.findall(r'name="digital_identity" value="([^"]+)"', page)) == {"yes", "no"}
    assert "radio.value === 'yes' ? 'identityPanel' : 'financingStopPanel'" in page
    assert "radio.value === 'yes' ? 'cpfPanel' : 'identityStopPanel'" in page


def test_both_blocked_paths_send_the_candidate_to_the_information_form_for_vtc():
    page = application.app.test_client().get("/inscriptions").get_data(as_text=True)

    assert page.count('/demande-informations-formations?formation=VTC') == 2
    assert page.count("Le parcours automatique s’arrête ici.") == 2
    assert "Un échange téléphonique est nécessaire avant l’inscription" in page
    assert "Votre Identité Numérique doit d’abord être opérationnelle" in page


def test_final_step_explains_the_second_cpf_confirmation_and_messages():
    page = application.app.test_client().get("/inscriptions").get_data(as_text=True)

    assert "Surveillez vos e-mails et vos SMS" in page
    assert "validez définitivement l’inscription" in page
    assert "Votre dossier passera au statut « Accepté »" in page


def test_information_form_can_be_preselected_from_the_vtc_registration_path():
    page = application.app.test_client().get(
        "/demande-informations-formations?formation=VTC"
    ).get_data(as_text=True)

    preselection = "const preselectedFormation = secretariatParams.get('formation');"
    assert preselection in page
    assert page.index(preselection) > page.index("if (secretariatParams.get('secretariat') === '1')")
    assert "formation.value = preselectedFormation;" in page
