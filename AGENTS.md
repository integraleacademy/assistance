# Instructions permanentes pour les agents de développement

Ce dépôt contient le CRM de production d’Intégrale Academy. Les demandes automatisées provenant de Notion doivent être traitées avec un périmètre strict.

## Règles obligatoires

1. Inspecter le code réellement utilisé avant de modifier quoi que ce soit, en particulier lorsqu’une ancienne et une nouvelle vue coexistent.
2. Ne réaliser que la demande décrite dans l’issue GitHub liée. Ne pas ajouter d’améliorations connexes non demandées.
3. Préserver les anciennes fiches, les formats de données existants et les intégrations externes.
4. Ajouter ou adapter au moins un test de non-régression pour toute modification Python, JavaScript, HTML ou CSS du CRM.
5. Exécuter les tests ciblés et les vérifications de syntaxe avant de conclure.
6. Ne jamais modifier ni exposer les secrets, les données de production ou les fichiers contenant des données réelles.
7. Ne jamais fusionner automatiquement une pull request.
8. Pour une demande Notion, retourner un patch Git textuel complet ; le runner Codex n’est jamais réutilisé pour publier le code.

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

Le patch est analysé avant application, puis contrôlé une deuxième fois avant tout commit.

## Qualité attendue

- changements minimaux et réversibles ;
- erreurs utilisateur explicites ;
- aucune requête réseau ajoutée au chargement d’une page sans nécessité ;
- aucune migration destructive ;
- tests placés sous `tests/test_*.py` ;
- aucun fichier binaire, lien symbolique, sous-module ou renommage Git ;
- résumé final indiquant les fichiers modifiés et les tests exécutés.
