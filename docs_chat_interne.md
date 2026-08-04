# Chat interne — exploitation et recette

## Persistance et déploiement Render

Le schéma (`chat_conversations`, `chat_participants`, `chat_messages`) est créé de
façon idempotente au démarrage. Il est volontairement séparé de `data.json`.

Ordre de sélection du stockage : `CHAT_DATABASE_URL`, `DATABASE_URL`, puis SQLite
dans `CHAT_DB_PATH` ou sur le disque déjà déclaré (`DATA_DIR`,
`RENDER_DISK_PATH`/`RENDER_DISK_MOUNT_PATH`). En production, définir l'une des
deux URL PostgreSQL est recommandé si aucun disque persistant n'est monté.
`REDIS_URL` est facultatif dans la configuration mono-worker actuelle ; lorsqu'il
est présent, Flask-SocketIO l'utilise comme file de messages.

Commande Render :

```sh
gunicorn crm_app:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads ${GUNICORN_THREADS:-16} --timeout 120 --max-requests 0
```

## Recette manuelle à deux comptes

1. Se connecter avec A et B dans deux navigateurs et ouvrir le widget sur chacun.
2. Vérifier les points « En ligne », envoyer dans « Équipe », puis contrôler la
   réception immédiate, le son, le badge du salon, le badge global et le titre.
3. Depuis A, cliquer B, envoyer plusieurs messages privés et vérifier qu'un
   troisième compte ne les reçoit pas.
4. Actualiser les deux pages, puis naviguer vers une autre page privée : vérifier
   l'historique, le brouillon/l'état restaurés et l'absence de faux hors-ligne.
5. Fermer tous les onglets de B : son statut doit devenir hors ligne après la
   grâce d'environ 15 secondes. Avec un onglet B restant, il doit rester en ligne.
6. Couper le réseau : le panneau doit annoncer la reconnexion sans perdre le
   brouillon. Rétablir le réseau et vérifier la resynchronisation sans doublon ni
   rafale sonore.
7. Dans les outils réseau, filtrer `socket.io` et confirmer le transport
   `websocket` (un repli HTTP initial peut apparaître si un intermédiaire bloque
   temporairement WebSocket).
