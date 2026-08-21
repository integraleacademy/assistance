# Automatisation Notion → Codex → pull request CRM

## Objectif

Lorsqu’une page de la base Notion **🧠 Mon cerveau** respecte les trois conditions suivantes :

- `Domaine = Développement web` ;
- `Plateforme = CRM` ;
- `Statut = À faire` ;

GitHub la prend en charge dans les cinq minutes. Le titre, le contenu complet de la page, ses propriétés métier et les commentaires Notion ouverts accessibles à l’intégration sont copiés dans une issue GitHub. Codex prépare ensuite une proposition de code et GitHub crée une pull request brouillon après validation.

L’application CRM de production n’est pas sollicitée par cette automatisation : tout s’exécute dans GitHub Actions.

## Champs Notion utilisés

| Propriété | Usage |
| --- | --- |
| `Tâche GitHub` | lien vers l’issue contenant la copie figée de la demande |
| `PR GitHub` | lien vers la pull request brouillon |
| `Branche GitHub` | branche dédiée à la demande |
| `Run GitHub` | exécution GitHub Actions en cours ou en erreur |
| `Compte rendu IA` | résumé final fourni par Codex |
| `Conversation ChatGPT` | lien vers la conversation ChatGPT Work créée par le Workspace Agent, lorsqu’il est activé |
| `Run Agent ChatGPT` | identifiant du run Workspace Agent en bêta |
| `Erreur automatisation` | motif précis d’un blocage |
| `ID automatisation` | verrou anti-doublon |
| `Dernier traitement IA` | date du dernier passage de l’automatisation |

## Cycle de vie

1. La page passe sur `À faire`.
2. Le workflow planifié la réserve et place son statut sur `En cours`.
3. Une issue GitHub est créée avec le texte original et un marqueur Notion unique.
4. Lorsque l’option Workspace Agent est configurée, la même copie figée est envoyée à ChatGPT Work et le lien de conversation est enregistré dans Notion.
5. Un premier runner prépare le prompt Codex et vérifie qu’aucune PR n’existe déjà.
6. Un deuxième runner exécute Codex sans jeton GitHub ni Notion. Codex travaille sur une copie isolée et retourne uniquement un patch textuel structuré.
7. Un troisième runner, sans aucun secret, applique le patch puis exécute les contrôles et tests ciblés.
8. Un quatrième runner propre réapplique exactement le même patch, effectue une validation statique, crée le commit, pousse la branche et ouvre une PR brouillon.
9. Un dernier runner, basé uniquement sur `main`, renvoie le lien et le compte rendu dans Notion.
10. Une fusion manuelle place automatiquement la page Notion sur `Terminé`.
11. Une fermeture sans fusion ou un échec place la page sur `En attente` avec le détail du problème.

Aucune fusion automatique n’est configurée.

## Configuration unique à effectuer

### 1. Créer l’intégration Notion

Créer une intégration interne dans les paramètres Notion, puis lui accorder :

- lecture du contenu ;
- mise à jour du contenu ;
- lecture des commentaires ;
- insertion de commentaires.

Partager ensuite la base **🧠 Mon cerveau** avec cette intégration. Copier son jeton dans le secret GitHub `NOTION_API_TOKEN`.

La source de données utilisée par les workflows est :

```text
7f12fe92-dbc4-40c8-af4e-77578b5dbfc0
```

### 2. Ajouter la clé OpenAI

Ajouter dans les secrets GitHub du dépôt :

```text
OPENAI_API_KEY
```

Cette clé est transmise uniquement à l’action officielle `openai/codex-action`, épinglée sur un commit vérifié. Codex utilise le profil `:workspace`, sans réseau direct. Son runner termine immédiatement après l’action Codex et n’obtient jamais les jetons GitHub ou Notion.

### 3. Activer la conversation ChatGPT Work — facultatif

Cette partie nécessite que **Workspace Agents** soit disponible dans l’espace ChatGPT concerné. Elle n’est pas obligatoire pour que Codex prépare les pull requests.

1. Créer et publier un Workspace Agent nommé par exemple **Développeur CRM — Intégrale Academy**.
2. Lui ajouter un canal API et récupérer l’identifiant public `agtch_...`.
3. Créer un token d’accès Workspace Agent limité au périmètre Workspace Agents.
4. Ajouter dans les secrets GitHub :

```text
CHATGPT_WORKSPACE_AGENT_TRIGGER_ID
CHATGPT_WORKSPACE_AGENT_TOKEN
```

À chaque demande, l’API reçoit la copie figée du titre, du contenu, des commentaires et des liens Notion/GitHub. Elle renvoie immédiatement un lien de conversation, enregistré dans `Conversation ChatGPT`. L’automatisation utilise aussi une clé d’idempotence basée sur l’identifiant Notion pour éviter les doublons. La réponse de l’agent n’est pas utilisée pour modifier le dépôt : la PR reste préparée par le workflow Codex isolé.

### 4. Autoriser la création de pull requests par GitHub Actions

Dans les paramètres Actions du dépôt :

- utiliser les permissions de workflow en lecture/écriture ;
- autoriser GitHub Actions à créer des pull requests.

Lorsque cette option ne peut pas être activée, créer un jeton GitHub à droits minimaux et l’enregistrer sous :

```text
CRM_AUTOMATION_GITHUB_TOKEN
```

Droits nécessaires sur le seul dépôt `integraleacademy/assistance` : contenu en lecture/écriture, issues en lecture/écriture et pull requests en lecture/écriture.

## Workflows

### `.github/workflows/notion-crm-queue.yml`

Interroge Notion toutes les cinq minutes. Il ne prend que trois demandes à la fois et utilise un verrou de concurrence afin d’éviter les doublons. Tant que `NOTION_API_TOKEN` et `OPENAI_API_KEY` ne sont pas présents, il reste volontairement inactif sans générer une erreur toutes les cinq minutes. Le déclenchement ChatGPT Work est facultatif.

### `.github/workflows/notion-crm-implement.yml`

Sépare la préparation, Codex, les tests, la publication et la synchronisation Notion dans des jobs et runners distincts. Les permissions GitHub sont accordées uniquement au job de publication.

### `.github/workflows/notion-crm-pr-sync.yml`

Synchronise la fermeture ou la fusion de la pull request vers Notion depuis une copie propre de `main`.

## Sécurité

Le contenu Notion est traité comme une spécification non fiable. Il ne peut pas autoriser Codex à :

- lire ou exposer des secrets ;
- modifier les workflows, les instructions d’agent ou le moteur de l’automatisation ;
- intervenir sur `data.json` ou des données de production ;
- modifier les dépendances ou les fichiers de déploiement ;
- utiliser le réseau ;
- écrire dans `.git`, créer des hooks ou modifier l’historique ;
- fusionner une pull request ;
- élargir le périmètre hors du CRM.

Le patch est limité à 30 fichiers, 2 500 lignes et 400 000 caractères. Les binaires, liens symboliques, sous-modules, renommages Git, chemins sortant du dépôt et fichiers sensibles sont refusés avant application.

Les tests s’exécutent dans un job sans secret. Le job de publication réapplique le patch sur un runner neuf, n’exécute pas les tests ou le code modifié, désactive tous les hooks Git et n’expose le jeton GitHub qu’au moment du push et de la création de la PR. La synchronisation Notion s’exécute encore sur un autre runner, depuis `main`, afin qu’un nouveau module ajouté par le patch ne puisse pas intercepter le jeton Notion.

## Relancer une demande

Une demande en erreur conserve son issue ou sa pull request pour le diagnostic. Après correction du problème de configuration :

1. fermer la pull request défectueuse lorsqu’elle existe ;
2. supprimer la branche technique résiduelle lorsqu’elle existe ;
3. vider `ID automatisation`, `Tâche GitHub`, `PR GitHub`, `Branche GitHub` et `Erreur automatisation` dans Notion ;
4. remettre le statut sur `À faire`.

Elle sera reprise lors du prochain passage du workflow planifié.

## Vérification initiale conseillée

Créer une petite demande CRM sans risque, avec un titre précis, le comportement attendu et un critère de validation. La passer sur `À faire`, puis contrôler successivement les liens `Tâche GitHub`, `Run GitHub` et `PR GitHub` dans la même page Notion.
