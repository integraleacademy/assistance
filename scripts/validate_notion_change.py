#!/usr/bin/env python3
"""Valide les modifications produites par Codex avant tout commit GitHub."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Sequence

FORBIDDEN_EXACT = {
    ".gitattributes",
    ".gitignore",
    ".gitmodules",
    "AGENTS.md",
    "Dockerfile",
    "Procfile",
    "data.json",
    "notion_crm_automation.py",
    "package-lock.json",
    "package.json",
    "pnpm-lock.yaml",
    "pyproject.toml",
    "pytest.ini",
    "render.yaml",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
    "yarn.lock",
    "scripts/validate_notion_change.py",
    "scripts/stage_notion_changes.py",
}
FORBIDDEN_PREFIXES = (
    ".github/",
    ".git/",
    "notion_crm_lib/",
)
FORBIDDEN_NAME_PATTERNS = (
    re.compile(r"(^|/)\.env($|\.)", re.IGNORECASE),
    re.compile(r"\.(pem|key|p12|pfx)$", re.IGNORECASE),
    re.compile(r"(^|/)(id_rsa|id_ed25519)$", re.IGNORECASE),
)
PRODUCT_SUFFIXES = {".py", ".js", ".html", ".css"}
MAX_CHANGED_FILES = 40
MAX_CHANGED_LINES = 6_000
GENERATED_PATH_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


class ValidationError(RuntimeError):
    pass


def run(
    command: Sequence[str],
    *,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def _zsplit(raw: str) -> list[str]:
    return [item for item in raw.split("\0") if item]


def _git_paths(*args: str) -> set[str]:
    result = run(("git", *args, "-z"), check=False)
    if result.returncode not in {0, 1}:
        raise ValidationError(result.stderr.strip() or f"Commande git échouée : {' '.join(args)}")
    return set(_zsplit(result.stdout))


def changed_paths(base: str = "main") -> list[str]:
    """Retourne les fichiers commis, modifiés, indexés ou non suivis."""

    paths: set[str] = set()
    base_ref = base
    base_exists = run(("git", "rev-parse", "--verify", base_ref), check=False).returncode == 0
    if base_exists:
        paths |= _git_paths("diff", "--name-only", "--diff-filter=ACMRD", f"{base_ref}...HEAD")
    paths |= _git_paths("diff", "--name-only", "--diff-filter=ACMRD")
    paths |= _git_paths("diff", "--cached", "--name-only", "--diff-filter=ACMRD")
    paths |= _git_paths("ls-files", "--others", "--exclude-standard")
    paths.discard(".codex-notion-task.md")
    paths.discard(".codex-notion-metadata.json")
    paths.discard(".codex-final-message.md")
    return sorted(
        path
        for path in paths
        if path
        and not any(part in GENERATED_PATH_PARTS for part in Path(path).parts)
        and not path.endswith((".pyc", ".pyo"))
    )


def validate_paths(paths: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    for raw_path in paths:
        path = str(raw_path).replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        if not path or path.startswith("../") or "/../" in path:
            raise ValidationError(f"Chemin de fichier invalide : {raw_path!r}")
        if path in FORBIDDEN_EXACT or any(path.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
            raise ValidationError(f"Codex n'est pas autorisé à modifier : {path}")
        if any(pattern.search(path) for pattern in FORBIDDEN_NAME_PATTERNS):
            raise ValidationError(f"Fichier sensible interdit : {path}")
        normalized.append(path)
    if not normalized:
        raise ValidationError("Codex n'a produit aucune modification de code.")
    if len(normalized) > MAX_CHANGED_FILES:
        raise ValidationError(
            f"Modification trop vaste : {len(normalized)} fichiers (maximum {MAX_CHANGED_FILES})."
        )
    return sorted(set(normalized))


def diff_line_count(base: str, paths: Iterable[str]) -> tuple[int, list[str]]:
    """Compte le diff, y compris les fichiers nouveaux non suivis."""

    commands = [
        ("git", "diff", "--numstat", f"{base}...HEAD"),
        ("git", "diff", "--numstat"),
        ("git", "diff", "--cached", "--numstat"),
    ]
    total = 0
    binary: list[str] = []
    counted_paths: set[str] = set()
    for command in commands:
        result = run(command, check=False)
        if result.returncode not in {0, 1}:
            continue
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, deleted, path = parts
            if path in counted_paths:
                continue
            counted_paths.add(path)
            if added == "-" or deleted == "-":
                binary.append(path)
                continue
            total += int(added) + int(deleted)

    for path in paths:
        if path in counted_paths or not Path(path).is_file():
            continue
        tracked = run(("git", "ls-files", "--error-unmatch", "--", path), check=False)
        if tracked.returncode == 0:
            continue
        raw = Path(path).read_bytes()
        if b"\x00" in raw:
            binary.append(path)
            continue
        total += raw.count(b"\n") + (1 if raw and not raw.endswith(b"\n") else 0)
    return total, sorted(set(binary))


def is_product_file(path: str) -> bool:
    if path.startswith("tests/") or path.startswith("docs/"):
        return False
    return Path(path).suffix.lower() in PRODUCT_SUFFIXES


def is_test_file(path: str) -> bool:
    return path.startswith("tests/test_") and path.endswith(".py")


def existing_files(paths: Iterable[str], suffix: str | None = None) -> list[str]:
    result: list[str] = []
    for path in paths:
        if suffix and not path.endswith(suffix):
            continue
        if Path(path).is_file():
            result.append(path)
    return result


def validate(base: str = "main", *, run_tests: bool = True) -> list[str]:
    paths = validate_paths(changed_paths(base))
    total_lines, binary = diff_line_count(base, paths)
    if binary:
        raise ValidationError(
            "Les fichiers binaires ne sont pas autorisés dans une PR automatisée : " + ", ".join(binary)
        )
    if total_lines > MAX_CHANGED_LINES:
        raise ValidationError(
            f"Modification trop volumineuse : {total_lines} lignes (maximum {MAX_CHANGED_LINES})."
        )

    product_files = [path for path in paths if is_product_file(path)]
    test_files = [path for path in paths if is_test_file(path) and Path(path).is_file()]
    if product_files and not test_files:
        raise ValidationError(
            "Une modification du CRM doit inclure au moins un test Python dans tests/test_*.py."
        )

    checks = [
        run(("git", "diff", "--check", f"{base}...HEAD"), check=False),
        run(("git", "diff", "--check"), check=False),
        run(("git", "diff", "--cached", "--check"), check=False),
    ]
    for check in checks:
        if check.returncode != 0:
            raise ValidationError(
                check.stdout.strip() or check.stderr.strip() or "git diff --check a échoué"
            )

    python_files = existing_files(paths, ".py")
    if python_files:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", *python_files],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPYCACHEPREFIX": "/tmp/notion-crm-pycache"},
        )
        if result.returncode != 0:
            raise ValidationError(result.stderr.strip() or "Compilation Python échouée")

    javascript_files = existing_files(paths, ".js")
    if javascript_files and shutil.which("node"):
        for path in javascript_files:
            result = run(("node", "--check", path), check=False)
            if result.returncode != 0:
                raise ValidationError(result.stderr.strip() or f"Syntaxe JavaScript invalide : {path}")

    if run_tests and test_files:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *test_files],
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if result.returncode != 0:
            raise ValidationError("Les tests ciblés ont échoué.")

    print("Fichiers validés :")
    for path in paths:
        print(f"- {path}")
    print(f"Volume du diff : {total_lines} lignes")
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="main")
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args(argv)
    try:
        validate(args.base, run_tests=not args.skip_tests)
    except ValidationError as exc:
        print(f"ERREUR DE VALIDATION : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
