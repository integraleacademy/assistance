"""Livraison des rappels d'arrivée sur le disque persistant du CRM."""

from contextlib import closing
from datetime import timedelta
from pathlib import Path
import sqlite3


class DeliveryUncertain(Exception):
    """Le transport n'a pas confirmé si le message a été accepté."""


def deliver_due(reservations, now, data_file, arrival_for, send, logger):
    today = now.date()
    if now.hour < 8:
        return
    for booking in reservations:
        arrival = arrival_for(booking)
        if not arrival or not today < arrival <= today + timedelta(days=2):
            continue
        if not booking.get("id") or booking.get("cle_etat") in {"Donnee", "Restituee"}:
            continue
        address = str(booking.get("mail") or "").strip()
        if not address or any(char in address for char in ",;\r\n ") or "@" not in address:
            continue
        key = (booking["id"], arrival.isoformat())
        with closing(sqlite3.connect(Path(data_file).with_name("hebergement-rappels.sqlite3"))) as db:
            with db:
                db.execute("""CREATE TABLE IF NOT EXISTS rappel (
                    reservation TEXT, arrivee TEXT, etat TEXT,
                    PRIMARY KEY(reservation, arrivee))""")
                claimed = db.execute(
                    "INSERT OR IGNORE INTO rappel VALUES (?, ?, 'en cours')", key
                ).rowcount
            if not claimed:
                continue
            # La réservation de l'envoi est durable avant de contacter le transport.
            try:
                sent = send(booking, arrival, address)
            except Exception:
                logger.exception("Rappel hébergement à vérifier, réservation %s", key[0])
                with db:
                    db.execute("UPDATE rappel SET etat='à vérifier' WHERE reservation=? AND arrivee=?", key)
                continue
            with db:
                if sent:
                    db.execute("UPDATE rappel SET etat='envoyé' WHERE reservation=? AND arrivee=?", key)
                    logger.info("Rappel hébergement envoyé, réservation %s, arrivée %s", *key)
                else:
                    db.execute("DELETE FROM rappel WHERE reservation=? AND arrivee=?", key)
                    logger.warning("Rappel hébergement à réessayer, réservation %s", key[0])
