# Modèles d’e-mails — `/demande-informations-formations`

Ce document centralise **tous les modèles envoyés** depuis la route:

- `POST /demande-informations-formations` dans `app.py`.

## Où sont les modèles

Les contenus e-mail sont construits dans `app.py` avec 2 formats:

- `plain` (texte brut)
- `html` (version HTML)

### Sélection du modèle (switch par formation)

Dans `demande_informations_formations()`, le modèle est choisi par `form_data.get("formation")`:

- `DESP_VAE`
  - HTML: `build_vae_desp_email_html(prenom, devis_url)`
  - Sujet: `📝 VAE – Dirigeant d’Entreprise de Sécurité Privée (RNCP40385)`
- `A3P`
  - HTML: `build_a3p_email_html(prenom, dates, centre, devis_url)`
  - Sujet: `👮‍♂️ Formation Agent de Protection Physique des Personnes (A3P)`
- `APS`
  - HTML: `build_aps_email_html(prenom, dates, centre)`
  - Sujet: `👮‍♂️ Formation Agent de Sécurité Privée (APS)`
- `VTC`
  - HTML: `build_vtc_email_html(prenom, centre, devis_url)`
  - Sujet: `🚗 Formation Chauffeur VTC`
- `DESP_INIT`
  - HTML: `build_desp_init_email_html(prenom, dates, centre, devis_url)`
  - Sujet: `Votre demande de renseignements – Formation DESP initial`
- `default` (toute autre formation)
  - HTML généré inline via `_wrap_html(...)`
  - Sujet: `Votre demande de renseignements – Intégrale Academy`


## Formulaires abandonnés

Les relances de formulaires abandonnés sont envoyées par `envoyer_mail_formulaire_formation_abandonne()` et `_envoyer_mail_formulaire_abandonne_depuis_demande()` dans `app.py`.

- Pour `DESP_VAE`, la relance utilise toujours le même modèle HTML que le formulaire soumis : `templates/emails/vae_desp.html` via `build_vae_desp_email_html(prenom, devis_url)`.
- Pour les autres formations, la relance conserve le modèle générique `templates/emails/abandoned_training.html`.

## Fonctions HTML à modifier

Toutes ces fonctions sont dans `app.py`:

- `build_vae_desp_email_html(...)`
- `build_a3p_email_html(...)`
- `build_aps_email_html(...)`
- `build_vtc_email_html(...)`
- `build_desp_init_email_html(...)`

👉 Pour modifier rapidement un template, chercher son nom de fonction puis éditer le bloc HTML retourné.

## Envoi effectif

L’envoi est fait une seule fois, en bas du bloc:

- `send_email_html(form_data.get("mail"), email_subject, plain, html)`

Donc les changements sur `plain`, `html` ou `email_subject` impactent immédiatement les mails envoyés depuis ce formulaire.

## Astuce pour maintenance

Si vous voulez rendre la modification encore plus simple:

1. Déplacer chaque HTML dans un fichier Jinja dédié (`templates/emails/*.html`).
2. Appeler `render_template("emails/...", ...)` au lieu de gros strings dans `app.py`.
3. Garder dans `app.py` uniquement la logique de sélection (`formation -> template + sujet`).

Cela permet de retrouver tous les modèles dans un seul dossier.
