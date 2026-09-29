"""Shared fixtures: a bound client folder and an MCP server context with a fake backend."""
from __future__ import annotations

import keyring
import pytest
from keyring.backend import KeyringBackend

from uapply_agent.config import Settings
from uapply_agent.context import ServerContext
from uapply_agent.folder import WorkingFolder


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path, monkeypatch):
    """Tests never read or change the real OS keychain or config: an in-memory keyring and a
    temporary config directory for every test."""
    previous = keyring.get_keyring()
    keyring.set_keyring(MemoryKeyring())
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("UAPPLY_TOKEN", raising=False)
    yield
    keyring.set_keyring(previous)


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
