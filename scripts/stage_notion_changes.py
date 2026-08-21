#!/usr/bin/env python3
"""Indexe uniquement les fichiers explicitement validés de la demande Notion."""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Sequence

from validate_notion_change import ValidationError, changed_paths, validate_paths


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="main")
    args = parser.parse_args(argv)
    try:
        paths = validate_paths(changed_paths(args.base))
        subprocess.run(["git", "add", "--", *paths], check=True)
    except (ValidationError, subprocess.CalledProcessError) as exc:
        print(f"ERREUR D'INDEXATION : {exc}", file=sys.stderr)
        return 1
    print("Fichiers indexés explicitement :")
    for path in paths:
        print(f"- {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
