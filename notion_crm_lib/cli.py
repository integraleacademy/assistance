"""Interface en ligne de commande appelée par GitHub Actions."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .clients import GitHubClient, NotionClient, WorkspaceAgentClient
from .core import AutomationError, DEFAULT_DATA_SOURCE_ID, dashed_page_id
from .service import process_queue, render_codex_prompt, tracking_properties


UPDATE_STATUSES = (
    "À faire",
    "À valider",
    "Prêt à coder",
    "En cours",
    "PR disponible",
    "En attente",
    "Terminé",
    "Publié",
)


def effective_update_status(status: str | None, pr_url: str | None) -> str | None:
    """Distingue le développement en cours d'une PR déjà prête à relire."""

    if status == "En cours" and str(pr_url or "").strip():
        return "PR disponible"
    return status


def env_required(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise AutomationError(f"La variable d'environnement {name} est absente.")
    return value


def command_queue(args: argparse.Namespace) -> int:
    notion = NotionClient(env_required("NOTION_API_TOKEN"))
    github = GitHubClient(env_required("GITHUB_TOKEN"), env_required("GITHUB_REPOSITORY"))
    data_source_id = str(
        args.data_source_id
        or os.environ.get("NOTION_DATA_SOURCE_ID")
        or DEFAULT_DATA_SOURCE_ID
    )
    run_url = str(args.run_url or os.environ.get("GITHUB_RUN_URL") or "")
    max_tasks = int(args.max_tasks or os.environ.get("MAX_TASKS") or 3)

    agent_token = str(os.environ.get("CHATGPT_WORKSPACE_AGENT_TOKEN") or "").strip()
    agent_trigger_id = str(os.environ.get("CHATGPT_WORKSPACE_AGENT_TRIGGER_ID") or "").strip()
    workspace_agent = None
    if agent_token and agent_trigger_id:
        workspace_agent = WorkspaceAgentClient(agent_token, agent_trigger_id)
    elif agent_token or agent_trigger_id:
        print(
            "Avertissement : configurez à la fois CHATGPT_WORKSPACE_AGENT_TOKEN et "
            "CHATGPT_WORKSPACE_AGENT_TRIGGER_ID pour créer les conversations ChatGPT Work.",
            file=sys.stderr,
        )

    result = process_queue(
        notion,
        github,
        data_source_id=data_source_id,
        run_url=run_url,
        max_tasks=max(1, min(max_tasks, 10)),
        workspace_agent=workspace_agent,
    )
    return 1 if result["failures"] else 0


def command_render_prompt(args: argparse.Namespace) -> int:
    github = GitHubClient(env_required("GITHUB_TOKEN"), env_required("GITHUB_REPOSITORY"))
    issue = github.get_issue(int(args.issue_number))
    prompt, metadata = render_codex_prompt(issue)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(prompt, encoding="utf-8")
    metadata_output = Path(args.metadata_output)
    metadata_output.parent.mkdir(parents=True, exist_ok=True)
    metadata_output.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metadata, ensure_ascii=False))
    return 0


def command_update(args: argparse.Namespace) -> int:
    notion = NotionClient(env_required("NOTION_API_TOKEN"))
    report = args.report
    if args.report_file:
        report = Path(args.report_file).read_text(encoding="utf-8")
    comment = args.comment
    if args.comment_file:
        file_comment = Path(args.comment_file).read_text(encoding="utf-8")
        comment = f"{comment}\n\n{file_comment}".strip() if comment else file_comment
    properties = tracking_properties(
        status=effective_update_status(args.status, args.pr_url),
        automation_id=args.automation_id,
        issue_url=args.issue_url,
        pr_url=args.pr_url,
        branch=args.branch,
        run_url=args.run_url,
        report=report,
        chatgpt_url=args.chatgpt_url,
        chatgpt_run_id=args.chatgpt_run_id,
        error=args.error,
        clear_error=bool(args.clear_error),
    )
    notion.update_page(args.page_id, properties)
    if comment:
        notion.safe_add_comment(args.page_id, comment)
    print(
        json.dumps(
            {
                "page_id": dashed_page_id(args.page_id),
                "updated_properties": sorted(properties),
            },
            ensure_ascii=False,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    queue_parser = subparsers.add_parser("queue", help="Prendre en charge les pages Notion prêtes")
    queue_parser.add_argument("--data-source-id", default="")
    queue_parser.add_argument("--run-url", default="")
    queue_parser.add_argument("--max-tasks", type=int, default=0)
    queue_parser.set_defaults(func=command_queue)

    prompt_parser = subparsers.add_parser("render-prompt", help="Créer le prompt Codex depuis une issue")
    prompt_parser.add_argument("--issue-number", type=int, required=True)
    prompt_parser.add_argument("--output", required=True)
    prompt_parser.add_argument("--metadata-output", required=True)
    prompt_parser.set_defaults(func=command_render_prompt)

    update_parser = subparsers.add_parser("update", help="Synchroniser l'état technique vers Notion")
    update_parser.add_argument("--page-id", required=True)
    update_parser.add_argument("--status", choices=UPDATE_STATUSES)
    update_parser.add_argument("--automation-id")
    update_parser.add_argument("--issue-url")
    update_parser.add_argument("--pr-url")
    update_parser.add_argument("--branch")
    update_parser.add_argument("--run-url")
    update_parser.add_argument("--report")
    update_parser.add_argument("--report-file")
    update_parser.add_argument("--chatgpt-url")
    update_parser.add_argument("--chatgpt-run-id")
    update_parser.add_argument("--error")
    update_parser.add_argument("--clear-error", action="store_true")
    update_parser.add_argument("--comment")
    update_parser.add_argument("--comment-file")
    update_parser.set_defaults(func=command_update)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except AutomationError as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrompu.", file=sys.stderr)
        return 130
