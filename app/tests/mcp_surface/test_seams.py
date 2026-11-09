"""Architecture guard for app/mcp: every patchable seam has exactly one home.

Tests patch ``app.mcp.security.authorize_workspace`` and
``app.mcp.support.services.<factory>``. That only works if tools reach those names through
the module at call time. A ``from app.mcp.security import authorize_workspace`` binds a private
copy: patching the seam would then silently not affect that tool, and the test would hit real
authorization or a real service. These checks fail the build when that happens.
"""

import ast
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[2] / "mcp"
SEAM_MODULES = {"app.mcp.security", "app.mcp.support.services"}
SEAM_HOMES = {MCP_DIR / "security.py", MCP_DIR / "support" / "services.py"}


def _modules():
    return [p for p in MCP_DIR.rglob("*.py") if "__pycache__" not in p.parts]


def test_nothing_imports_names_out_of_a_seam_module():
    offenders = []
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module in SEAM_MODULES:
                names = ", ".join(a.name for a in node.names)
                offenders.append(f"{path.relative_to(MCP_DIR)}: from {node.module} import {names}")
    assert not offenders, (
        "import the module and call through it (security.authorize_workspace(...), "
        "services.tax_service(...)):\n" + "\n".join(offenders)
    )


def test_service_factories_are_defined_only_in_the_services_module():
    offenders = []
    for path in _modules():
        if path in SEAM_HOMES:
            continue
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.FunctionDef) and node.name.endswith("_service"):
                offenders.append(f"{path.relative_to(MCP_DIR)}: def {node.name}")
    assert not offenders, "service factories belong in app/mcp/support/services.py:\n" + "\n".join(
        offenders
    )


def test_authorization_is_never_called_by_a_bare_name():
    offenders = []
    for path in _modules():
        if path in SEAM_HOMES:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"authorize_workspace", "authorize_user", "validate_page"}
            ):
                offenders.append(f"{path.relative_to(MCP_DIR)}:{node.lineno} {node.func.id}(")
    assert not offenders, "\n".join(offenders)
