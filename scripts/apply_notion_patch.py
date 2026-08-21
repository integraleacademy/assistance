#!/usr/bin/env python3
"""Valide puis applique le patch textuel produit par Codex."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

MAX_PATCH_CHARS = 400_000
MAX_REPORT_CHARS = 12_000
FORBIDDEN_EXACT = {
    ".codex",
    ".gitattributes",
    ".gitignore",
    ".gitmodules",
    "AGENTS.md",
    "AGENTS.override.md",
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
    "scripts/apply_notion_patch.py",
    "scripts/validate_notion_change.py",
    "scripts/stage_notion_changes.py",
}
FORBIDDEN_PREFIXES = (
    ".agents/",
    ".codex/",
    ".git/",
    ".github/",
    "notion_crm_lib/",
)
FORBIDDEN_BASENAMES = {"AGENTS.md", "AGENTS.override.md"}
FORBIDDEN_NAME_PATTERNS = (
    re.compile(r"(^|/)\.env($|\.)", re.IGNORECASE),
    re.compile(r"\.(pem|key|p12|pfx)$", re.IGNORECASE),
    re.compile(r"(^|/)(id_rsa|id_ed25519)$", re.IGNORECASE),
)
FORBIDDEN_PATCH_MARKERS = (
    "GIT binary patch",
    "Binary files ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "similarity index ",
    "dissimilarity index ",
    "new file mode 120000",
    "old mode 120000",
    "new file mode 160000",
    "old mode 160000",
    "Subproject commit ",
)


class PatchError(RuntimeError):
    pass


def run(command: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        list(command),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise PatchError(detail or f"Commande échouée : {' '.join(command)}")
    return result


def normalize_path(raw_path: str) -> str:
    path = str(raw_path or "").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    parts = Path(path).parts
    if (
        not path
        or path.startswith("/")
        or path.startswith("../")
        or ".." in parts
        or any(part in {".git", ".codex", ".agents"} for part in parts)
        or "\x00" in path
        or any(ord(char) < 32 for char in path)
    ):
        raise PatchError(f"Chemin de patch invalide : {raw_path!r}")
    return path


def validate_paths(paths: Iterable[str]) -> list[str]:
    validated: list[str] = []
    for raw_path in paths:
        path = normalize_path(raw_path)
        if (
            path in FORBIDDEN_EXACT
            or Path(path).name in FORBIDDEN_BASENAMES
            or any(path.startswith(prefix) for prefix in FORBIDDEN_PREFIXES)
        ):
            raise PatchError(f"Le patch n'est pas autorisé à modifier : {path}")
        if any(pattern.search(path) for pattern in FORBIDDEN_NAME_PATTERNS):
            raise PatchError(f"Le patch contient un fichier sensible interdit : {path}")
        validated.append(path)
    if not validated:
        raise PatchError("Le patch ne contient aucun fichier exploitable.")
    return sorted(set(validated))


def parse_codex_result(raw: str) -> tuple[str, str]:
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PatchError("La réponse finale de Codex n'est pas un JSON valide.") from exc
    if not isinstance(payload, dict):
        raise PatchError("La réponse finale de Codex doit être un objet JSON.")

    blocked = payload.get("blocked")
    blocker = payload.get("blocker")
    patch = payload.get("patch")
    report = payload.get("report")
    if not isinstance(blocked, bool):
        raise PatchError("Le champ Codex `blocked` est invalide.")
    if not isinstance(blocker, str) or not isinstance(patch, str) or not isinstance(report, str):
        raise PatchError("Les champs textuels de la réponse Codex sont invalides.")
    if blocked:
        raise PatchError(blocker.strip() or "Codex a signalé que la demande ne pouvait pas être traitée.")
    if blocker.strip():
        raise PatchError("Codex a fourni un blocage tout en déclarant la demande non bloquée.")

    patch = patch.replace("\r\n", "\n")
    if not patch.strip():
        raise PatchError("Codex n'a fourni aucun patch.")
    if len(patch) > MAX_PATCH_CHARS:
        raise PatchError(
            f"Le patch dépasse la limite de {MAX_PATCH_CHARS:,} caractères."
        )
    if "\x00" in patch:
        raise PatchError("Le patch contient un octet nul interdit.")
    if not patch.lstrip().startswith("diff --git "):
        raise PatchError("Le patch doit commencer par un diff Git unifié.")
    return patch, report[:MAX_REPORT_CHARS].strip()


def inspect_patch(patch_file: Path) -> list[str]:
    patch = patch_file.read_text(encoding="utf-8")
    for marker in FORBIDDEN_PATCH_MARKERS:
        if marker in patch:
            raise PatchError(f"Type de modification interdit dans le patch : {marker.strip()}")

    result = run(("git", "apply", "--numstat", "-z", "--", str(patch_file)))
    paths: list[str] = []
    for record in result.stdout.split(b"\x00"):
        if not record:
            continue
        parts = record.split(b"\t", 2)
        if len(parts) != 3:
            raise PatchError("Git n'a pas pu analyser correctement les chemins du patch.")
        paths.append(parts[2].decode("utf-8", errors="strict"))
    return validate_paths(paths)


def apply_patch(patch_file: Path, paths: Sequence[str]) -> None:
    run(("git", "apply", "--check", "--whitespace=error-all", "--", str(patch_file)))
    run(("git", "apply", "--whitespace=error-all", "--", str(patch_file)))

    root = Path.cwd().resolve()
    for path in paths:
        candidate = Path(path)
        if candidate.is_symlink():
            raise PatchError(f"Les liens symboliques sont interdits : {path}")
        if candidate.exists():
            resolved = candidate.resolve()
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise PatchError(f"Le patch a écrit hors du dépôt : {path}") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--patch-output", required=True)
    parser.add_argument("--report-output", required=True)
    args = parser.parse_args(argv)

    try:
        raw_result = Path(args.result_file).read_text(encoding="utf-8")
        patch, report = parse_codex_result(raw_result)
        patch_file = Path(args.patch_output)
        patch_file.write_text(patch, encoding="utf-8")
        Path(args.report_output).write_text(report, encoding="utf-8")
        paths = inspect_patch(patch_file)
        apply_patch(patch_file, paths)
    except (OSError, UnicodeError, PatchError) as exc:
        print(f"ERREUR DE PATCH : {exc}", file=sys.stderr)
        return 1

    print("Patch Codex appliqué aux fichiers suivants :")
    for path in paths:
        print(f"- {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
