from notion_crm_lib.cli import UPDATE_STATUSES, build_parser, effective_update_status


PAGE_ID = "3c26e0d1-a86e-8192-9950-cdf229ada797"


def test_pr_url_turns_en_cours_into_pr_disponible() -> None:
    assert effective_update_status("En cours", "https://github.com/example/repo/pull/1") == "PR disponible"
    assert effective_update_status("En cours", "") == "En cours"
    assert effective_update_status("Publié", "https://github.com/example/repo/pull/1") == "Publié"


def test_cli_accepts_pr_disponible_and_publie() -> None:
    parser = build_parser()
    for status in ("PR disponible", "Publié"):
        args = parser.parse_args(
            ["update", "--page-id", PAGE_ID, "--status", status]
        )
        assert args.status == status
        assert status in UPDATE_STATUSES
