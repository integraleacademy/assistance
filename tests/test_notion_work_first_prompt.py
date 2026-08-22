from __future__ import annotations

from notion_crm_lib.work_first import compact_codex_prompt


def test_work_first_prompt_reuses_prepared_spec_and_targets_files() -> None:
    issue = {
        "body": """<!-- notion-page-id: 3c26e0d1-a86e-80d5-92dc-da3951db2819 -->
## Contenu complet de la page

Je veux que la carte Calendly soit plus fiable.

## Spécification préparée par Work
### Besoin utilisateur
Conserver les rendez-vous en cache lors d'une actualisation lente.
### Écrans, routes, fichiers ou composants concernés
- `static/crm.js` : `loadCalendlyAppointments`
- `app.py` : `crm_contact_calendly_appointments`
- `tests/test_crm_calendly.py`
- Route : `GET /api/crm/contacts/<contact_id>/calendly/appointments`
### Tests à créer ou à exécuter
Exécuter les tests Calendly ciblés.

## Commentaires Notion ouverts

### Commentaire 1
Ne pas casser RDV programmé.

## Pièces jointes détectées

- **PJ 1** — image — capture fiche CRM — source : page

## Analyse visuelle et documentaire des pièces jointes

La liste doit rester visible et l'avertissement doit être non bloquant.

## Règles de traitement
Texte technique.
""",
    }
    metadata = {
        "task_title": "RDV calendly lent",
        "page_url": "https://notion.example/page",
        "issue_url": "https://github.com/integraleacademy/assistance/issues/1",
        "page_id": "3c06e0d1-a86e-80d5-92dc-da3951db2819",
    }

    prompt = compact_codex_prompt(issue, fallback_prompt="FALLBACK", metadata=metadata)

    assert "FALLBACK" not in prompt
    assert "La phase d'analyse approfondie a **déjà été réalisée par ChatGPT Work**" in prompt
    assert "Ne parcours pas le dépôt de manière générale" in prompt
    assert "`static/crm.js`" in prompt
    assert "`app.py`" in prompt
    assert "`tests/test_crm_calendly.py`" in prompt
    assert "`GET /api/crm/contacts/<contact_id>/calendly/appointments`" in prompt
    assert "Conserver les rendez-vous en cache" in prompt
    assert "La liste doit rester visible" in prompt
    assert "Ne pas casser RDV programmé" in prompt
    assert "Je veux que la carte Calendly soit plus fiable" in prompt
    assert "Passe directement à l'implémentation ciblée" in prompt


def test_work_first_prompt_falls_back_without_work_specification() -> None:
    issue = {"body": "## Contenu complet de la page\nDemande sans préparation Work."}
    metadata = {
        "task_title": "Test",
        "page_url": "",
        "issue_url": "",
        "page_id": "3c06e0d1-a86e-80d5-92dc-da3951db2819",
    }

    assert compact_codex_prompt(
        issue,
        fallback_prompt="PROMPT COMPLET DE SECOURS",
        metadata=metadata,
    ) == "PROMPT COMPLET DE SECOURS"


def test_original_notes_do_not_keep_signed_media_markdown() -> None:
    issue = {
        "body": """## Contenu complet de la page
Regarde cette capture : ![fiche](https://files.notion.example/signed-secret.png)
<pdf src="https://files.notion.example/document.pdf">Dossier</pdf>

## Spécification préparée par Work
### Besoin utilisateur
Moderniser la fiche.
"""
    }
    metadata = {
        "task_title": "Design",
        "page_url": "",
        "issue_url": "",
        "page_id": "3c06e0d1-a86e-80d5-92dc-da3951db2819",
    }

    prompt = compact_codex_prompt(issue, fallback_prompt="fallback", metadata=metadata)

    assert "signed-secret.png" not in prompt
    assert "document.pdf" not in prompt
    assert "[capture jointe analysée séparément]" in prompt
    assert "[document joint analysé séparément]" in prompt
