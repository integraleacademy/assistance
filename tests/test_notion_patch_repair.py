from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import apply_notion_patch as applier


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "app.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "app.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)


def test_repair_hunk_counts_keeps_a_valid_patch_unchanged() -> None:
    patch = (
        "diff --git a/app.txt b/app.txt\n"
        "--- a/app.txt\n"
        "+++ b/app.txt\n"
        "@@ -1,1 +1,1 @@\n"
        "-alpha\n"
        "+ALPHA\n"
    )

    repaired, changed = applier.repair_hunk_counts(patch)

    assert changed is False
    assert repaired == patch


def test_repair_hunk_counts_fixes_codex_counters_and_patch_applies(
    tmp_path: Path, monkeypatch,
) -> None:
    _init_repo(tmp_path)
    malformed = (
        "diff --git a/app.txt b/app.txt\n"
        "--- a/app.txt\n"
        "+++ b/app.txt\n"
        "@@ -1,99 +1,42 @@\n"
        "-alpha\n"
        "+ALPHA\n"
        " beta\n"
        "@@ -3,12 +3,7 @@\n"
        "-gamma\n"
        "+GAMMA\n"
    )

    repaired, changed = applier.repair_hunk_counts(malformed)

    assert changed is True
    assert "@@ -1,2 +1,2 @@" in repaired
    assert "@@ -3,1 +3,1 @@" in repaired

    patch_file = tmp_path / "change.patch"
    patch_file.write_text(repaired, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    paths = applier.inspect_patch(patch_file)
    applier.apply_patch(patch_file, paths)

    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "ALPHA\nbeta\nGAMMA\n"


def test_repair_hunk_counts_preserves_no_newline_marker() -> None:
    malformed = (
        "diff --git a/app.txt b/app.txt\n"
        "--- a/app.txt\n"
        "+++ b/app.txt\n"
        "@@ -1,5 +1,8 @@\n"
        "-alpha\n"
        "+ALPHA\n"
        "\\ No newline at end of file\n"
    )

    repaired, changed = applier.repair_hunk_counts(malformed)

    assert changed is True
    assert "@@ -1,1 +1,1 @@" in repaired
    assert "\\ No newline at end of file" in repaired
