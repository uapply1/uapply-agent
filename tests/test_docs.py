"""The MCP tool reference must list exactly the tools the server registers."""
import asyncio
import re
from pathlib import Path

REFERENCE = Path(__file__).resolve().parents[1] / "docs" / "reference" / "mcp-tools.md"


def _documented() -> set[str]:
    text = REFERENCE.read_text(encoding="utf-8")
    implemented = text.split("## Planned", 1)[0]          # tools listed as planned are not implemented
    return set(re.findall(r"^\| `([a-z_]+)` \|", implemented, re.M))


def test_tool_reference_matches_the_server():
    from uapply_agent import mcp_server
    served = {t.name for t in asyncio.run(mcp_server.server.list_tools())}
    documented = _documented()
    assert not served - documented, f"tools missing from mcp-tools.md: {sorted(served - documented)}"
    assert not documented - served, f"documented tools that do not exist: {sorted(documented - served)}"
