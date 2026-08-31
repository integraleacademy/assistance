#!/usr/bin/env python3
"""Clean inconsistent CRM residence-permit assessments from a JSON snapshot."""

import argparse
import json
import os
from pathlib import Path
import stat
import tempfile


YES_VALUES = {"OUI", "YES", "TRUE", "1"}


def is_yes(value):
    return str(value or "").strip().upper() in YES_VALUES


def cleanup_payload(payload):
    cleaned = 0
    for contact in payload.get("crm_contacts", []):
        if not isinstance(contact, dict):
            continue
        if is_yes(contact.get("titre_sejour")):
            continue
        if not str(contact.get("titre_sejour_cnaps") or "").strip():
            continue
        contact["titre_sejour_cnaps"] = ""
        cleaned += 1
    return cleaned


def write_atomically(path, payload):
    mode = stat.S_IMODE(path.stat().st_mode)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        temporary = Path(stream.name)
    try:
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Purge titre_sejour_cnaps lorsque le contact n’est pas titulaire "
            "d’un titre de séjour. Le mode par défaut est une simulation."
        )
    )
    parser.add_argument("json_path", type=Path, help="Chemin explicite du fichier JSON CRM")
    parser.add_argument(
        "--write", action="store_true", help="Appliquer le nettoyage de façon atomique"
    )
    args = parser.parse_args(argv)
    path = args.json_path.expanduser().resolve(strict=True)
    payload = json.loads(path.read_text(encoding="utf-8"))
    cleaned = cleanup_payload(payload)
    if args.write and cleaned:
        write_atomically(path, payload)
    mode = "appliqué" if args.write else "simulation"
    print(f"Nettoyage {mode} : {cleaned} contact(s) incohérent(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
