"""Point d’entrée compatible de l’automatisation Notion → GitHub → Codex."""

from notion_crm_lib import *  # noqa: F401,F403
from notion_crm_lib.cli import (
    build_parser,
    command_queue,
    command_render_prompt,
    command_update,
    env_required,
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
