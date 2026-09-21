"""`tmux-agents` entry point: run the MCP server over stdio."""

from __future__ import annotations

import sys


def main() -> None:
    if "--version" in sys.argv[1:]:
        from . import __version__

        print(f"tmux-agents {__version__}")
        return
    from .server import mcp

    mcp.run()


if __name__ == "__main__":
    main()
