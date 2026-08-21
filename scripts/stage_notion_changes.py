#!/usr/bin/env python3
"""Indexe uniquement les chemins d'un manifeste produit par le validateur."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

MAX_PATHS = 30


class StagingError(RuntimeError):
    pass


def normalize_path(raw_path: Any) -> str:
    if not isinstance(raw_path, str):
        raise StagingError("Le manifeste contient un chemin non textuel.")
    path = raw_path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if not path or path.startswith("/") or path.startswith("../") or ".." in Path(path).parts:
        raise StagingError(f"Chemin invalide dans le manifeste : {raw_path!r}")
    if any(ord(char) < 32 for char in path):
        raise StagingError(f"Caractère de contrôle dans le chemin : {raw_path!r}")
    return path


def load_manifest(path: str) -> list[str]:
    try:
        payload: Any = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StagingError("Le manifeste de validation est illisible.") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise StagingError("Version de manifeste invalide.")
    raw_paths = payload.get("paths")
    if not isinstance(raw_paths, list):
        raise StagingError("La liste des fichiers validés est absente.")
    paths = sorted(set(normalize_path(item) for item in raw_paths))
    if not paths or len(paths) > MAX_PATHS:
        raise StagingError("Nombre de fichiers invalide dans le manifeste.")
    return paths


def staged_paths() -> set[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRD"],
        check=True,
        stdout=subprocess.PIPE,
    )
    return {
        item.decode("utf-8", errors="strict")
        for item in result.stdout.split(b"\x00")
        if item
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)
    try:
        paths = load_manifest(args.manifest)
        subprocess.run(["git", "add", "--", *paths], check=True)
        indexed = staged_paths()
        if indexed != set(paths):
            raise StagingError(
                "Les fichiers indexés ne correspondent pas exactement au manifeste validé."
            )
    except (StagingError, subprocess.CalledProcessError, UnicodeError) as exc:
        print(f"ERREUR D'INDEXATION : {exc}", file=sys.stderr)
        return 1

    print("Fichiers indexés explicitement :")
    for path in paths:
        print(f"- {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
