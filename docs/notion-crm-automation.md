# Automatisation Notion → Codex → pull request CRM

## Objectif

Lorsqu’une page de la base Notion **🧠 Mon cerveau** respecte les trois conditions suivantes :

- `Domaine = Développement web` ;
- `Plateforme = CRM` ;
- `Statut = À faire` ;

GitHub la prend en charge dans les cinq minutes. Le titre, le contenu complet de la page, ses propriétés métier et ses commentaires de niveau page sont copiés dans une issue GitHub. Codex travaille ensuite sur une branche isolée, exécute les contrôles et crée une pull request brouillon.

L’application CRM de production n’est pas sollicitée par cette automatisation : tout s’exécute dans GitHub Actions.

## Champs Notion utilisés

Les propriétés techniques suivantes ont été ajoutées à la base :

| Propriété | Usage |
| --- | --- |
| `Tâche GitHub` | lien vers l’issue contenant la copie figée de la demande |
| `PR GitHub` | lien vers la pull request brouillon |
| `Branche GitHub` | branche dédiée à la demande |
| `Run GitHub` | exécution GitHub Actions en cours ou en erreur |
| `Compte rendu IA` | résumé final fourni par Codex |
| `Erreur automatisation` | motif précis d’un blocage |
| `ID automatisation` | verrou anti-doublon |
| `Dernier traitement IA` | date du dernier passage de l’automatisation |

## Cycle de vie

1. La page passe sur `À faire`.
2. Le workflow planifié la réserve et place son statut sur `En cours`.
3. Une issue GitHub est créée avec le texte original et un marqueur Notion unique.
4. Codex analyse le dépôt et réalise le changement sur `agent/notion-crm-<identifiant>`.
5. Le validateur bloque les fichiers sensibles, exige un test et exécute les contrôles ciblés.
6. Une pull request brouillon est créée et son lien est écrit dans Notion.
7. Une fusion manuelle place automatiquement la page Notion sur `Terminé`.
8. Une fermeture sans fusion ou un échec place la page sur `En attente` avec le détail du problème.

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

Cette clé est transmise uniquement à l’action officielle `openai/codex-action`. Codex fonctionne avec le profil d’autorisation `:workspace`, sans accès réseau direct et sans accès au jeton GitHub utilisé ensuite pour pousser la branche.

### 3. Autoriser la création de pull requests par GitHub Actions

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

Interroge Notion toutes les cinq minutes. Il ne prend que trois demandes à la fois et utilise un verrou de concurrence afin d’éviter les doublons.

### `.github/workflows/notion-crm-implement.yml`

Transforme l’issue en mission Codex, protège les secrets, valide le diff, crée le commit, pousse la branche puis ouvre la pull request brouillon.

### `.github/workflows/notion-crm-pr-sync.yml`

Synchronise la fermeture ou la fusion de la pull request vers Notion.

## Sécurité

Le contenu Notion est traité comme une spécification non fiable. Il ne peut pas autoriser Codex à :

- lire ou exposer des secrets ;
- modifier les workflows, `notion_crm_automation.py`, `notion_crm_lib/` ou les garde-fous ;
- intervenir sur `data.json` ou des données de production ;
- utiliser le réseau ;
- fusionner une pull request ;
- élargir le périmètre hors du CRM.

Le workflow n’enregistre les identifiants GitHub qu’après la fin de l’étape Codex. Les fichiers temporaires contenant la demande et le compte rendu ne sont jamais commités. Avant de lancer Codex, GitHub fige également une copie des scripts de validation hors de l’espace de travail modifiable ; c’est cette copie immuable qui contrôle le diff et choisit les fichiers à indexer.

## Relancer une demande

Une demande en erreur conserve son issue ou sa pull request pour le diagnostic. Après correction du problème de configuration :

1. fermer la pull request défectueuse lorsqu’elle existe ;
2. vider `ID automatisation`, `Tâche GitHub`, `PR GitHub`, `Branche GitHub` et `Erreur automatisation` dans Notion ;
3. remettre le statut sur `À faire`.

Elle sera reprise lors du prochain passage du workflow planifié.

## Vérification initiale conseillée

Créer une petite demande CRM sans risque, avec un titre précis, le comportement attendu et un critère de validation. La passer sur `À faire`, puis contrôler successivement les liens `Tâche GitHub`, `Run GitHub` et `PR GitHub` dans la même page Notion.
