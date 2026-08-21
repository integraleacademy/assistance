# Préparation des demandes CRM par ChatGPT Work

## Objectif

Conserver la qualité du travail effectué dans ChatGPT Work tout en automatisant la suite :

```text
Notion — À faire
    ↓
ChatGPT Work — préparation du cahier des charges
    ↓
Notion — À valider
    ↓
Validation manuelle de Clément — Prêt à coder
    ↓
GitHub Actions + Codex — code, tests et PR brouillon
```

Le workflow GitHub ne doit jamais traiter directement une note brute en statut `À faire`.

## Propriétés Notion utilisées

- `Domaine` : `Développement web`
- `Plateforme` : `CRM`
- `Statut` : `À faire`, `À valider`, `Prêt à coder`, `En cours`, `En attente` ou `Terminé`
- `Résumé Work` : synthèse courte de la préparation
- `Préparation Work terminée` : case cochée uniquement par Work après préparation complète
- `Préparé par Work le` : date et heure de la préparation
- `Erreur automatisation` : blocage ou information manquante

La spécification détaillée doit être ajoutée directement dans le contenu de la page Notion sous un titre `## Spécification préparée par Work`. Le workflow GitHub récupère ensuite tout le contenu de la page et les commentaires ouverts.

## Prompt de la tâche programmée Work

Créer une tâche programmée ChatGPT Work qui s’exécute toutes les heures avec le texte suivant :

```text
Toutes les heures, consulte dans @Notion la base « 🧠 Mon cerveau ».

Recherche au maximum trois pages qui remplissent exactement toutes les conditions suivantes :
- Domaine = Développement web
- Plateforme = CRM
- Statut = À faire
- Préparation Work terminée = non cochée

Pour chaque page trouvée :

1. Lis le titre, toutes les propriétés, l’intégralité du contenu de la page, tous les commentaires ouverts et les pièces jointes textuelles accessibles. Les captures d’écran doivent être prises en compte lorsqu’elles sont accessibles ; ne prétends jamais les avoir analysées si elles ne le sont pas.

2. Inspecte dans @GitHub le dépôt `integraleacademy/assistance` afin d’identifier l’écran réellement utilisé, les fichiers probablement concernés, les mécanismes existants, les tests pertinents et les risques de régression. Ne modifie aucun fichier, ne crée aucune branche, aucun commit, aucune issue et aucune pull request à cette étape.

3. Prépare une spécification exploitable par Codex en restant strictement fidèle aux informations écrites par Clément. N’ajoute pas de fonctionnalité non demandée. Lorsque plusieurs interprétations sont possibles, choisis l’option la plus prudente et indique clairement l’hypothèse.

4. La spécification doit contenir :
- le besoin utilisateur reformulé sans perdre les détails ;
- le comportement actuel constaté ou supposé ;
- le comportement attendu ;
- les écrans, routes ou composants concernés ;
- les règles métier à préserver ;
- les cas limites ;
- les risques de régression ;
- les critères d’acceptation vérifiables ;
- les tests à créer ou à exécuter ;
- les informations restant éventuellement à confirmer.

5. Dans la page Notion, ajoute ou remplace une section intitulée exactement :

## Spécification préparée par Work

Insère dessous la spécification complète. Ne supprime ni le contenu original, ni les commentaires, ni les pièces jointes.

6. Renseigne la propriété `Résumé Work` avec une synthèse de moins de 1 800 caractères.

7. Si la demande est suffisamment précise pour être codée :
- coche `Préparation Work terminée` ;
- renseigne `Préparé par Work le` avec la date et l’heure actuelles ;
- vide `Erreur automatisation` ;
- passe le statut sur `À valider`.

8. Si une information indispensable manque ou si la demande est dangereuse, trop vaste ou contradictoire :
- ne coche pas `Préparation Work terminée` ;
- renseigne `Erreur automatisation` avec les questions ou le blocage précis ;
- passe le statut sur `En attente` ;
- ne passe jamais la page sur `Prêt à coder`.

9. Ne passe jamais toi-même une page sur `Prêt à coder`. Cette validation appartient toujours à Clément.

10. À la fin du passage, notifie Clément uniquement s’au moins une page a été préparée ou bloquée. Indique pour chacune le titre, le nouveau statut et un résumé très court.

Ne retraite jamais une page déjà cochée `Préparation Work terminée`, une page qui n’est plus sur `À faire`, ou une page qui possède déjà un `ID automatisation`.
```

## Validation manuelle

Clément ouvre la page passée sur `À valider`, relit la section préparée par Work et ajoute si nécessaire des commentaires ou corrections.

Lorsqu’il valide la spécification, il change uniquement :

```text
Statut = Prêt à coder
```

Le workflow GitHub vérifie alors simultanément :

- `Domaine = Développement web` ;
- `Plateforme = CRM` ;
- `Statut = Prêt à coder` ;
- `Préparation Work terminée = cochée` ;
- `Préparé par Work le = renseigné` ;
- `ID automatisation = vide`.

Si une condition manque, aucun code n’est produit.

## Relancer une préparation

Pour demander à Work de refaire entièrement la préparation :

1. replacer le statut sur `À faire` ;
2. décocher `Préparation Work terminée` ;
3. vider `Préparé par Work le` ;
4. ajouter les nouvelles précisions dans la page ou les commentaires.

Lors du passage horaire suivant, Work reprendra la demande.
