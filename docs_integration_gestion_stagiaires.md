# Relier le CRM à Gestion stagiaires

## 1. Endpoint à ajouter dans Gestion stagiaires

L'application Gestion stagiaires doit exposer un endpoint serveur `POST /api/integrations/crm/preremplissage`. Il doit :

1. accepter un jeton secret dans `Authorization: Bearer <jeton>` ;
2. refuser les appels non authentifiés (`401`) ;
3. conserver temporairement les données de préremplissage sans créer de personne ni d'inscription ;
4. répondre en JSON avec HTTP `200` ou `201` : `{"url":"https://.../inscriptions/nouveau?jeton=..."}` ;
5. répondre avec HTTP `400` ou `422` et `{"error":"message lisible"}` si une donnée est invalide.

Le JSON reçu contient `source`, `crm_contact_id`, `prenom`, `nom`, `email`, `telephone`, `formation`, `parcours`, `centre`, `session` et `commentaires`. La correspondance des libellés de formation/centre doit être faite côté Gestion stagiaires si cette application utilise des identifiants internes.

## 2. Variables à configurer sur le CRM

Configurer dans l'environnement de production du CRM :

```text
GESTION_STAGIAIRES_API_URL=https://gestionstagiaires-r5no.onrender.com/api/integrations/crm/preremplissage
GESTION_STAGIAIRES_API_TOKEN=<un-secret-long-et-aleatoire>
```

Configurer exactement la même valeur de jeton côté Gestion stagiaires. Le jeton ne doit jamais être envoyé au navigateur : le CRM effectue l'appel entre serveurs.

## 3. Comportement obtenu

Le clic sur **Converti** ouvre immédiatement un onglet vide, puis le CRM appelle Gestion stagiaires côté serveur. Après réception de l'URL temporaire, le nouvel onglet y est redirigé et le contact passe à **Converti**. À ce stade, le formulaire est seulement prérempli : aucune personne n'est créée. En cas d'erreur, l'onglet vide est fermé, le dossier reste dans son statut actuel et l'utilisateur voit le message renvoyé.
