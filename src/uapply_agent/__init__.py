"""uapply-agent: MCP server, CLI and task executor for running uApply cases locally."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("uapply-agent")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0"
