"""Guard: every MCP tool that writes must commit.

``postgres.async_session_maker()`` sessions do not autocommit and the services
only ``flush()``, so a write tool without a commit reports success while the
transaction is rolled back when the session closes. Two shipped tools did exactly
that (``set_fx_rate``, ``update_tax_profile``) and went unnoticed because their
tests only looked at the returned payload.

This is a static check over ``app/mcp/**/*.py`` so it keeps working when tools
move between modules. A write tool is one that requests ``mcp:write`` or
``mcp:research`` itself, or delegates to ``_run_capture_tool`` / ``run_capture_tool``
(which commits on success). It must contain ``.commit()``, open ``session.begin()``,
or delegate.
"""

import ast
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1] / "mcp"
WRITE_SCOPES = ('"mcp:write"', '"mcp:research"')
DELEGATES = ("_run_capture_tool", "run_capture_tool")


def uncommitted_write_tools(source: str) -> list[str]:
    """Names of ``@mcp.tool`` functions that write but have no commit path."""
    tree = ast.parse(source)
    lines = source.splitlines()
    missing: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if not any(isinstance(d, ast.Attribute) and d.attr == "tool" for d in node.decorator_list):
            continue
        body = "\n".join(lines[node.lineno - 1 : node.end_lineno])
        delegates = any(name in body for name in DELEGATES)
        writes = delegates or any(scope in body for scope in WRITE_SCOPES)
        commits = ".commit()" in body or "session.begin()" in body
        if writes and not (commits or delegates):
            missing.append(node.name)
    return sorted(missing)


def test_the_guard_detects_a_write_tool_without_a_commit():
    bad = """
@mcp.tool
async def sneaky_write(workspace_id: int):
    async with postgres.async_session_maker() as session:
        await authorize_workspace(session, workspace_id, required_scope="mcp:write", tool="x")
        await service.change(session)
        return {"status": "success"}
"""
    good = bad.replace(
        '        return {"status": "success"}',
        '        await session.commit()\n        return {"status": "success"}',
    ).replace("sneaky_write", "careful_write")
    delegating = """
@mcp.tool
async def passthrough(workspace_id: int):
    return await _run_capture_tool(workspace_id, "mcp:write", "passthrough", {})
"""
    read_only = bad.replace("mcp:write", "mcp:read").replace("sneaky_write", "reader")

    assert uncommitted_write_tools(bad) == ["sneaky_write"]
    assert uncommitted_write_tools(good) == []
    assert uncommitted_write_tools(delegating) == []
    assert uncommitted_write_tools(read_only) == []


@pytest.mark.parametrize(
    "path", sorted(MCP_DIR.rglob("*.py")), ids=lambda p: str(p.relative_to(MCP_DIR))
)
def test_every_mcp_write_tool_commits(path: Path):
    missing = uncommitted_write_tools(path.read_text())
    assert not missing, (
        f"{path.name}: write tools without a commit (their changes are silently rolled back): "
        f"{missing}"
    )
