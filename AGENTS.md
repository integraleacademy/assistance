# Instructions permanentes pour les agents de développement

Ce dépôt contient le CRM de production d’Intégrale Academy. Les demandes automatisées provenant de Notion doivent être traitées avec un périmètre strict.

## Règles obligatoires

1. Inspecter uniquement le code réellement utilisé et strictement nécessaire à la demande, en particulier lorsqu’une ancienne et une nouvelle vue coexistent.
2. Ne réaliser que la demande décrite dans l’issue GitHub liée. Ne pas ajouter d’améliorations connexes non demandées.
3. Préserver les anciennes fiches, les formats de données existants et les intégrations externes.
4. Ajouter ou adapter au moins un test de non-régression pour toute modification Python, JavaScript, HTML ou CSS du CRM lorsque cela est pertinent pour la demande.
5. Pour une demande Notion automatisée, la phase Codex d’implémentation **ne lance pas pytest, la suite globale ni de commande longue** : elle modifie les fichiers puis rend immédiatement la main. Un runner de validation séparé exécute ensuite les tests ciblés et les vérifications de syntaxe avant toute création de PR. Pour un développement manuel hors workflow Notion, exécuter les tests pertinents normalement.
6. Ne jamais modifier ni exposer les secrets, les données de production ou les fichiers contenant des données réelles.
7. Ne jamais fusionner automatiquement une pull request.
8. Pour une demande Notion automatisée, **modifier directement les fichiers du workspace et ne pas reconstruire de patch Git textuel dans la réponse finale**. Le workflow capture lui-même le diff du workspace, le valide, le reteste sur un runner séparé puis crée la PR brouillon.

## Fichiers protégés dans les demandes Notion

Les agents déclenchés depuis Notion ne doivent pas modifier :

- `.github/workflows/` ;
- `.git/`, `.codex/` et `.agents/` ;
- `AGENTS.md` et tout `AGENTS.override.md` ;
- `notion_crm_automation.py` ;
- `notion_crm_lib/` ;
- `scripts/apply_notion_patch.py` ;
- `scripts/validate_notion_change.py` ;
- `scripts/stage_notion_changes.py` ;
- `data.json` ;
- les manifests de dépendances et de déploiement ;
- les fichiers `.env`, clés, certificats ou jetons.

Le diff du workspace est capturé par le workflow, analysé avant application, puis contrôlé une deuxième fois avant tout commit.

## Qualité attendue

- changements minimaux et réversibles ;
- erreurs utilisateur explicites ;
- aucune requête réseau ajoutée au chargement d’une page sans nécessité ;
- aucune migration destructive ;
- tests placés sous `tests/test_*.py` lorsque des tests sont ajoutés ;
- aucun fichier binaire, lien symbolique, sous-module ou renommage Git ;
- pour le workflow Notion, résumé final concis indiquant les fichiers modifiés et laissant les tests au runner de validation séparé ;
- pour un développement manuel hors workflow Notion, résumé final indiquant les fichiers modifiés et les tests exécutés.
