"""Golden snapshot of the MCP public surface.

Every tool's name, title, description, input and output schema, annotations, tags and
meta, plus every resource and resource template, must match ``golden_catalog.json``
byte for byte. Refactoring ``app/mcp`` (moving tools between modules, extracting
helpers) is only allowed to change the code, never what a client sees; this test is the
proof. A deliberate change to the surface regenerates the file:

    UPDATE_GOLDEN=1 uv run pytest app/tests/mcp_surface -q

and the diff of ``golden_catalog.json`` is then part of the review.
"""

import json
import os
from pathlib import Path

import pytest
from pydantic_core import to_jsonable_python

from app.config import settings
from app.mcp.server import create_mcp_server

GOLDEN = Path(__file__).with_name("golden_catalog.json")


@pytest.fixture(autouse=True)
def _mcp_base_url(monkeypatch):
    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.lifestack.test")


async def _surface() -> dict:
    server = create_mcp_server()
    tools = sorted(await server.list_tools(), key=lambda tool: tool.name)
    resources = sorted(await server.list_resources(), key=lambda res: str(res.uri))
    templates = sorted(await server.list_resource_templates(), key=lambda res: res.uri_template)
    return to_jsonable_python({
        "tools": [
            {
                "name": tool.name,
                "title": tool.title,
                "description": tool.description,
                "input_schema": tool.parameters,
                "output_schema": tool.output_schema,
                "annotations": tool.annotations,
                "tags": sorted(tool.tags or []),
                "meta": tool.meta,
            }
            for tool in tools
        ],
        "resources": [
            {
                "uri": str(res.uri),
                "name": res.name,
                "description": res.description,
                "mime_type": res.mime_type,
            }
            for res in resources
        ],
        "resource_templates": [
            {
                "uri_template": res.uri_template,
                "name": res.name,
                "description": res.description,
                "mime_type": res.mime_type,
            }
            for res in templates
        ],
    })


async def test_mcp_surface_matches_golden_snapshot():
    surface = await _surface()
    rendered = json.dumps(surface, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(rendered)
    assert GOLDEN.exists(), "golden_catalog.json missing; run once with UPDATE_GOLDEN=1"
    golden = json.loads(GOLDEN.read_text())
    current = json.loads(rendered)

    assert [t["name"] for t in current["tools"]] == [t["name"] for t in golden["tools"]], (
        "tool set changed"
    )
    for now, was in zip(current["tools"], golden["tools"], strict=True):
        assert now == was, f"tool {now['name']!r} changed its public surface"
    assert current["resources"] == golden["resources"]
    assert current["resource_templates"] == golden["resource_templates"]


async def test_every_tool_has_a_description_and_an_object_input_schema():
    surface = await _surface()
    assert len(surface["tools"]) >= 67
    for tool in surface["tools"]:
        assert tool["description"], f"{tool['name']} has no description"
        assert tool["input_schema"]["type"] == "object", tool["name"]
