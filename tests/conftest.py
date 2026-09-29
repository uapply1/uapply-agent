"""Shared fixtures: a bound client folder and an MCP server context with a fake backend."""
from __future__ import annotations

import pytest

from uapply_agent.config import Settings
from uapply_agent.context import ServerContext
from uapply_agent.folder import WorkingFolder


@pytest.fixture
def case_folder(tmp_path) -> WorkingFolder:
    """A client folder bound to survey s-1."""
    folder = WorkingFolder(tmp_path / "client")
    folder.root.mkdir()
    folder.init_case("s-1", "https://api.example")
    return folder


@pytest.fixture
def folder(case_folder) -> WorkingFolder:
    return case_folder


@pytest.fixture
def use_server(monkeypatch):
    """Install a fresh ServerContext (own settings, folder and API) on the MCP server module."""
    from uapply_agent import mcp_server

    def install(folder: WorkingFolder, api, settings: Settings | None = None):
        ctx = ServerContext(settings or Settings(), folder, _api=api)
        monkeypatch.setattr(mcp_server, "ctx", ctx)
        return ctx
    return install
