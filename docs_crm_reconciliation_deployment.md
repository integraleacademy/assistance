# Déploiement du rapprochement CRM

Cette évolution ne contient **aucune migration destructive** : `crm_inbound_requests`
est une collection additive du fichier JSON, créée à la première nouvelle demande.
Les contacts et leurs UUID existants ne sont ni parcourus pour être réécrits, ni
fusionnés, ni supprimés.

## Sauvegarde obligatoire

1. Mettre temporairement les soumissions en maintenance.
2. Identifier `DATA_FILE` dans l'environnement Render et copier le fichier sur un
   stockage externe horodaté :
   `cp "$DATA_FILE" "$DATA_FILE.pre-reconciliation-$(date -u +%Y%m%dT%H%M%SZ)"`.
3. Vérifier la copie avec
   `python -m json.tool "$DATA_FILE" >/dev/null` puis
   `sha256sum "$DATA_FILE" "$DATA_FILE.pre-reconciliation-<horodatage>"`.
4. Télécharger également cette copie hors du disque éphémère Render. Ne pas
   déployer tant que la copie complète n'a pas été vérifiée.

`save_data` conserve en plus automatiquement la version immédiatement précédente
dans `data.json.bak` et remplace le fichier par écriture temporaire atomique. Cette
copie locale ne remplace pas la sauvegarde externe préalable.

## Déploiement et contrôles

1. Déployer le commit applicatif sans modifier les données.
2. Soumettre une demande de test unique, puis vérifier la présence de sa ligne dans
   `crm_inbound_requests` et de son activité sur la fiche.
3. Soumettre le même événement externe et vérifier qu'une seule demande existe.
4. Vérifier les messages, notifications, fiche convertie et anciennes fiches.
5. Réactiver les soumissions.

## Retour arrière

Remettre l'application en maintenance, redéployer le commit précédent et restaurer
la copie vérifiée avec `cp <sauvegarde> "$DATA_FILE"`. Conserver séparément le
fichier post-déploiement pour ne perdre aucune demande reçue pendant la fenêtre et
les réintégrer manuellement après analyse. La clé additive `crm_inbound_requests`
peut aussi rester présente : les anciennes versions l'ignorent.
