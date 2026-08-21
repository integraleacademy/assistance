# Remontée des inscriptions CRM vers Google Ads

Cette intégration transmet une conversion hors ligne à Google Ads lorsqu’une fiche CRM passe réellement au statut **Converti** ou lorsqu’un dossier d’inscription est ouvert dans Gestion stagiaires.

Elle ne dépend ni d’un export Excel, ni de Zapier, ni de Make. Elle utilise directement l’API Google Ads et reste totalement inactive tant que sa configuration Render n’est pas terminée.

## Données envoyées

Pour chaque nouvelle conversion :

- action de conversion Google Ads ;
- date et heure `converted_at`, avec le fuseau Europe/Paris ;
- montant `prix_vente` ;
- devise EUR ;
- identifiant de clic disponible : GCLID, GBRAID ou WBRAID ;
- identifiant de commande déterministe `crm-<contact_id>` pour éviter les doublons ;
- en option, e-mail et téléphone normalisés puis hachés en SHA-256.

Aucune clé Google, aucun jeton OAuth et aucune donnée client en clair ne sont enregistrés dans les journaux de l’intégration.

## Protection contre les doublons et les erreurs

- Une simple modification d’une ancienne fiche déjà convertie ne déclenche rien.
- Une fiche déjà marquée `sent` ne peut pas être renvoyée par la route de relance.
- Google reçoit toujours le même `orderId` pour une même fiche CRM.
- Une panne Google Ads ne bloque jamais l’enregistrement de la fiche CRM.
- Les échecs temporaires sont retentés automatiquement, avec un nombre maximal d’essais.
- Le résultat est conservé dans `contact.google_ads_offline_conversion`.

Statuts possibles : `pending`, `sent`, `validated`, `failed` et `blocked`.

## 1. Créer l’action dans Google Ads

Dans le compte Google Ads concerné :

1. Ouvrir **Objectifs > Conversions > Récapitulatif**.
2. Créer une action de conversion issue d’un **import / CRM / conversion hors ligne**.
3. Choisir une action de type **Importation à partir de clics**.
4. Nom conseillé : `Inscription formation - CRM`.
5. Configurer la valeur pour utiliser une valeur différente à chaque conversion.
6. Configurer le comptage sur **Une** conversion par interaction.
7. Noter l’identifiant numérique de l’action de conversion.

L’action doit accepter les conversions importées depuis les clics. Pour utiliser l’e-mail et le téléphone comme solution de correspondance supplémentaire, activer également les conversions améliorées pour les prospects dans Google Ads.

## 2. Préparer l’accès API Google Ads

Il faut disposer de :

- l’identifiant du compte Google Ads cible, sans tirets ;
- éventuellement l’identifiant du compte administrateur MCC, sans tirets ;
- un jeton développeur Google Ads ;
- un client OAuth Google (`client_id` et `client_secret`) ;
- un `refresh_token` OAuth autorisé avec l’accès Google Ads ;
- l’identifiant de l’action de conversion créée à l’étape précédente.

Le jeton développeur se récupère dans le Centre API du compte administrateur Google Ads. Le compte Google utilisé pour produire le `refresh_token` doit avoir accès au compte publicitaire cible.

## 3. Variables d’environnement Render

Ajouter les variables suivantes sur le service Render `assistance` :

```text
GOOGLE_ADS_OFFLINE_CONVERSIONS_ENABLED=true
GOOGLE_ADS_API_VERSION=v25
GOOGLE_ADS_CUSTOMER_ID=1234567890
GOOGLE_ADS_LOGIN_CUSTOMER_ID=0987654321
GOOGLE_ADS_DEVELOPER_TOKEN=...
GOOGLE_ADS_CLIENT_ID=...
GOOGLE_ADS_CLIENT_SECRET=...
GOOGLE_ADS_REFRESH_TOKEN=...
GOOGLE_ADS_CONVERSION_ACTION_ID=...
GOOGLE_ADS_CURRENCY=EUR
GOOGLE_ADS_VALIDATE_ONLY=true
```

`GOOGLE_ADS_LOGIN_CUSTOMER_ID` est facultatif lorsque le compte n’est pas utilisé par l’intermédiaire d’un MCC.

Pour un premier essai, conserver `GOOGLE_ADS_VALIDATE_ONLY=true`. L’API contrôle alors toute la requête sans enregistrer une vraie conversion. Une fois le test validé, passer cette variable à `false`, redéployer, puis relancer la fiche de test.

## 4. E-mail et téléphone hachés

Par sécurité, les identifiants clients sont désactivés par défaut. Ils ne doivent être activés qu’après vérification du consentement applicable et de l’information fournie dans le formulaire et la politique de confidentialité.

Après cette vérification, ajouter :

```text
GOOGLE_ADS_SEND_USER_IDENTIFIERS=true
GOOGLE_ADS_AD_USER_DATA_CONSENT=GRANTED
GOOGLE_ADS_REQUIRE_CLICK_ID=false
GOOGLE_ADS_DEFAULT_PHONE_COUNTRY_CODE=33
```

Avec `GOOGLE_ADS_REQUIRE_CLICK_ID=false`, une inscription dont le GCLID a été perdu peut encore être rapprochée à l’aide de l’e-mail et/ou du téléphone haché. Sans consentement `GRANTED`, l’intégration n’envoie jamais ces identifiants.

Pour rester uniquement sur le GCLID/GBRAID/WBRAID :

```text
GOOGLE_ADS_SEND_USER_IDENTIFIERS=false
GOOGLE_ADS_REQUIRE_CLICK_ID=true
GOOGLE_ADS_AD_USER_DATA_CONSENT=UNSPECIFIED
```

## 5. Réglages facultatifs

```text
GOOGLE_ADS_TIMEOUT_SECONDS=10
GOOGLE_ADS_MAX_ATTEMPTS=5
GOOGLE_ADS_RETRY_DELAY_MINUTES=15
GOOGLE_ADS_DEFAULT_CONVERSION_VALUE=
```

Le montant de la fiche `prix_vente` est toujours prioritaire. `GOOGLE_ADS_DEFAULT_CONVERSION_VALUE` ne sert que de secours lorsqu’un ancien dossier ne possède aucun montant.

## 6. Contrôle et relance

Une fois connecté au CRM :

- état global : `GET /api/crm/google-ads/status` ;
- relance d’une fiche en erreur : `POST /api/crm/google-ads/contacts/<contact_id>/retry`.

L’état global ne retourne aucun secret. Il indique si l’intégration est prête, les variables manquantes et le nombre de conversions par statut.

Une conversion déjà envoyée retourne une erreur HTTP 409 lors d’une tentative de relance, afin d’empêcher un double comptage.

## Procédure de recette conseillée

1. Configurer l’action Google Ads et les variables Render avec `GOOGLE_ADS_VALIDATE_ONLY=true`.
2. Utiliser une fiche de test contenant un GCLID de test, un prix de vente et un statut différent de Converti.
3. Passer la fiche à **Converti**.
4. Vérifier que son état devient `validated` et que l’activité « Conversion Google Ads validée en mode test » apparaît.
5. Passer `GOOGLE_ADS_VALIDATE_ONLY=false` puis redéployer.
6. Utiliser une nouvelle fiche de test réelle ou relancer la fiche validée par l’endpoint prévu.
7. Vérifier l’état `sent`, puis contrôler le diagnostic de l’action de conversion dans Google Ads.

Ne pas utiliser une fiche déjà réellement inscrite pour la première recette afin d’éviter d’introduire une conversion historique non souhaitée.
