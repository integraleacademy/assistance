# Relier le CRM à Gestion stagiaires

## 1. Endpoint à ajouter dans Gestion stagiaires

L'application Gestion stagiaires doit exposer un endpoint serveur `POST /api/integrations/crm/stagiaires`. Il doit :

1. accepter un jeton secret dans `Authorization: Bearer <jeton>` ;
2. refuser les appels non authentifiés (`401`) ;
3. créer le stagiaire et son inscription dans une transaction ;
4. rendre `Idempotency-Key` unique afin qu'un double clic ne crée jamais deux stagiaires ;
5. répondre en JSON avec HTTP `201` : `{"id":"identifiant", "url":"https://.../stagiaires/identifiant"}` ;
6. répondre avec HTTP `400` ou `422` et `{"error":"message lisible"}` si une donnée est invalide.

Le JSON reçu contient `source`, `crm_contact_id`, `prenom`, `nom`, `email`, `telephone`, `formation`, `parcours`, `centre`, `session` et `commentaires`. La correspondance des libellés de formation/centre doit être faite côté Gestion stagiaires si cette application utilise des identifiants internes.

## 2. Variables à configurer sur le CRM

Configurer dans l'environnement de production du CRM :

```text
GESTION_STAGIAIRES_API_URL=https://gestionstagiaires-r5no.onrender.com/api/integrations/crm/stagiaires
GESTION_STAGIAIRES_API_TOKEN=<un-secret-long-et-aleatoire>
```

Configurer exactement la même valeur de jeton côté Gestion stagiaires. Le jeton ne doit jamais être envoyé au navigateur : le CRM effectue l'appel entre serveurs.

## 3. Comportement obtenu

Le clic sur **Converti** ouvre une modale récapitulative. Après confirmation, le CRM appelle Gestion stagiaires. Le contact ne passe à **Converti** qu'après une réponse valide contenant l'identifiant distant. Cet identifiant, l'URL distante et une activité d'audit sont conservés dans le CRM. En cas d'erreur, le dossier reste dans son statut actuel et l'utilisateur voit le message renvoyé.
