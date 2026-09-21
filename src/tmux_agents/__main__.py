"""`tmux-agents` entry point: run the MCP server over stdio, or a subcommand."""

from __future__ import annotations

import sys


def main() -> None:
    args = sys.argv[1:]
    if "--version" in args:
        from . import __version__

        print(f"tmux-agents {__version__}")
        return
    if args and args[0] == "hook":
        from .hook import run

        sys.exit(run(args[1:]))
    from .server import mcp

    mcp.run()


if __name__ == "__main__":
    main()
