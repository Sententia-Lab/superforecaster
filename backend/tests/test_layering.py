"""`superforecaster/` is a library. This is the test that keeps it one.

The package used to import `config` from the backend root — a module the wheel never
shipped — and to configure Logfire at import time, which could block five seconds on an
HTTP token probe. Both are the kind of mistake that is invisible until someone tries to
install the thing, so both are asserted here rather than remembered.

The import direction is one way:

    api -> app -> superforecaster
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parent.parent / "superforecaster"

FORBIDDEN = {
    # The layers above. Core is imported by them, never the reverse.
    "app",
    "api",
    # Storage, interface, and process concerns. A consumer brings their own.
    "sqlite3",
    "typer",
    "click",
    "fastapi",
    "starlette",
    "sse_starlette",
    "uvicorn",
    "apscheduler",
    "dotenv",
}


def _imported_roots(path: Path) -> set[str]:
    """Every top-level module name this file imports, relative imports excluded."""
    tree = ast.parse(path.read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize(
    "path", sorted(CORE.rglob("*.py")), ids=lambda p: str(p.relative_to(CORE))
)
def test_core_module_imports_nothing_from_above_it(path: Path):
    offenders = _imported_roots(path) & FORBIDDEN
    assert not offenders, (
        f"{path.relative_to(CORE.parent)} imports {sorted(offenders)}. "
        "Core is a library: storage, HTTP, the CLI, and the scheduler live in `app`."
    )


def test_importing_core_has_no_side_effects():
    """No network, no `.env` read, no Logfire configuration.

    Run in a subprocess because the damage is done at first import, and pytest has
    already imported the package by the time any assertion in this process runs.
    """
    probe = """
import socket, sys
socket.socket.connect = lambda self, addr: sys.exit("core opened a socket on import")

import superforecaster
import logfire

if logfire.DEFAULT_LOGFIRE_INSTANCE.config._initialized:
    sys.exit("core configured logfire on import")
print(superforecaster.__version__)
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=CORE.parent,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip() == "0.3.0"
