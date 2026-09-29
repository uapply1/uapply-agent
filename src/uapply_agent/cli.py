"""The `uapply-agent` command: setup, login, case binding, headless task runs and the MCP server."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict
from enum import IntEnum
from pathlib import Path

from . import __version__, cases
from .api import ApiError, UApplyApi
from .auth import LoginError, device_login, token_login
from .chat.base import ChatError
from .config import Credentials, Settings
from .context import ToolError
from .folder import WorkingFolder
from .integrate import PLUGIN_DIR, SetupError, install_claude_plugin, run_setup
from .runners import RunnerError, detect_runtimes, get_runner
from .updater import maybe_delegate, resolve_target, running_sha

logger = logging.getLogger(__name__)


class ExitCode(IntEnum):
    OK = 0
    FAILED = 1           # the command ran and reports a negative result (e.g. no Acrobat)
    USAGE = 2            # bad arguments (argparse uses 2 too)
    ERROR = 3            # backend, runtime or chat-source error
    PLAN_LIMITED = 4     # the runtime's subscription limit was reached; run again later
    RUNTIME_UNAVAILABLE = 5
    INTERRUPTED = 130


class UsageError(Exception):
    pass


def _folder(args) -> WorkingFolder:
    return WorkingFolder(Path(args.folder or os.environ.get("UAPPLY_FOLDER") or os.getcwd()))


def _bound_folder(args) -> WorkingFolder:
    folder = _folder(args)
    if not folder.survey_id:
        raise UsageError("this folder has no case; run `uapply-agent init --survey <id>` first")
    return folder


def _print_json(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


# ---------- commands ----------

def cmd_update(args, settings):
    target, reason = resolve_target(settings, wait_s=600, say=print)
    print(f"uapply-agent: {reason}" + (f" -> {target}" if target else ""))
    return ExitCode.OK


def cmd_setup(args, settings):
    """Register the MCP server with every runtime found, then log in unless a token exists."""
    if sha := os.environ.get("UAPPLY_INSTALLED_SHA", "").strip():
        settings.installed_sha = sha
        settings.save()
    run_setup(settings=settings, login=not args.no_login)
    if Credentials.get_token():
        print("uApply login: already signed in")
    elif args.no_login:
        print("uApply login: skipped")
    else:
        try:
            device_login(settings)
        except LoginError as e:
            print(f"login failed: {e}; run `uapply-agent login` later", file=sys.stderr)
    print("Done. Open Claude Code or Codex in a client folder and type /uapply:run")
    return ExitCode.OK


def cmd_login(args, settings):
    if args.token:
        token_login(args.token)
    elif args.token_stdin:
        token_login(sys.stdin.read())
    else:
        device_login(settings)
    return ExitCode.OK


def cmd_logout(args, settings):
    Credentials.clear()
    print("credentials removed")
    return ExitCode.OK


def cmd_init(args, settings):
    folder = _folder(args)
    with UApplyApi(settings) as api:
        case, warning = cases.bind_existing(api, settings, folder, args.survey, args.llm_mode)
    _print_json(case)
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    found = [r["path"] for r in detect_runtimes(settings) if not r.get("error")]
    print(f"runtimes: {', '.join(found) or 'none; install Claude Code or Codex'}", file=sys.stderr)
    return ExitCode.OK


def cmd_status(args, settings):
    folder = _bound_folder(args)
    with UApplyApi(settings) as api:
        _print_json(api.agent_status(folder.survey_id))
    return ExitCode.OK


def cmd_run(args, settings):
    maybe_delegate(args.argv, settings, wait_s=180)
    from .executor import Executor
    folder = _bound_folder(args)
    runner = get_runner(args.runtime or settings.runtime, args.model or settings.model, settings)
    with UApplyApi(settings) as api:
        ex = Executor(api, folder, runner, force_ocr=args.force_ocr or settings.force_ocr,
                      timeout_s=settings.task_timeout_s)
        print(f"executor: {runner.name}, session {ex.session_id}", file=sys.stderr)
        while True:
            stats = ex.run(max_tasks=args.max_tasks, workers=args.workers or settings.workers, kinds=args.kinds)
            print(json.dumps(stats.as_dict()), file=sys.stderr)
            if stats.plan_limited:
                print("plan limit reached; run again later", file=sys.stderr)
                return ExitCode.PLAN_LIMITED
            if stats.runtime_error:
                print(f"stopped: {stats.runtime_error}", file=sys.stderr)
                return ExitCode.RUNTIME_UNAVAILABLE
            if not args.follow:
                break
            if api.agent_wait(folder.survey_id, "processing", 45).get("done") and stats.remaining == 0:
                break
        _print_json(api.agent_status(folder.survey_id))
    return ExitCode.OK


def _refresh_plugin() -> None:
    """A new version may ship new /uapply:* commands: rewrite them when the plugin is installed."""
    home = Path(os.environ.get("UAPPLY_HOME") or Path.home())
    if (home / PLUGIN_DIR).exists():
        try:
            install_claude_plugin(home)
        except OSError:
            logger.warning("could not refresh the Claude Code plugin", exc_info=True)


def cmd_mcp(args, settings):
    maybe_delegate(args.argv, settings)
    _refresh_plugin()
    if args.folder:
        os.environ["UAPPLY_FOLDER"] = str(Path(args.folder).resolve())
    from .mcp_server import main as serve
    serve()
    return ExitCode.OK


def cmd_chat(args, settings):
    from .chat.anychat import AnyChatSource
    from .chat.store import ChatStore
    src = AnyChatSource(binary=settings.anychat_bin)
    if args.chat_cmd == "sources":
        _print_json(src.available().as_dict())
    elif args.chat_cmd == "find":
        _print_json([c.public() for c in src.resolve(args.name)])
    else:
        folder = _folder(args)
        transcript = src.fetch(args.contact, args.days or settings.chat_default_days, folder.chat)
        ChatStore(folder).record(transcript)
        _print_json(transcript.public())
        print("Next: bind the folder (`uapply-agent init --survey <id>`) and file it with the chat_upload tool.",
              file=sys.stderr)
    return ExitCode.OK


def cmd_clean(args, settings):
    print(f"removed {_folder(args).clean()} cached files")
    return ExitCode.OK


def cmd_acrobat(args, settings):
    """Check Acrobat Pro automation; with --pdf, test-fill one field of that form."""
    from . import acrobat
    found = acrobat.detect(probe=True)
    _print_json(found)
    if not found["available"]:
        return ExitCode.FAILED
    if args.pdf:
        src = Path(args.pdf).resolve()
        out = src.with_name(f"{src.stem}_uapply_test.pdf")
        ops = [{"op": "set", "node": args.node, "value": args.value, "event": "change"}]
        try:
            _print_json(acrobat.fill(src, ops, out))
        except acrobat.AcrobatError as e:
            print(f"test fill failed: {e}", file=sys.stderr)
            return ExitCode.FAILED
        print(f"saved {out}")
    return ExitCode.OK


def cmd_config(args, settings):
    for item in args.set or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise UsageError(f"expected KEY=VALUE, got {item!r}")
        try:
            settings.set(key, value)
        except ValueError as e:
            raise UsageError(str(e)) from e
    if args.set:
        settings.save()
    _print_json(asdict(settings))
    return ExitCode.OK


# ---------- parser ----------

class _VersionAction(argparse.Action):
    """Resolves the running commit only when --version is asked for."""

    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, help="show the version and the running commit")

    def __call__(self, parser, namespace, values, option_string=None):
        print(f"uapply-agent {__version__} ({(running_sha(Settings.load()) or 'unknown')[:7]})")
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="uapply-agent", description="Run uApply cases from Claude Code or Codex.")
    p.add_argument("--version", action=_VersionAction)
    p.add_argument("-v", "--verbose", action="store_true", help="log progress to stderr")
    folder = argparse.ArgumentParser(add_help=False)
    folder.add_argument("--folder", help="client folder (default: UAPPLY_FOLDER or the current directory)")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def command(name, fn, help_text, parents=()):
        c = sub.add_parser(name, help=help_text, description=help_text, parents=list(parents))
        c.set_defaults(fn=fn)
        return c

    command("update", cmd_update, "install the latest version now (it also happens when mcp or run starts)")
    c = command("setup", cmd_setup, "register the MCP server with Claude Code and Codex, then log in")
    c.add_argument("--no-login", action="store_true", help="skip the uApply and Claude sign-in steps")
    c = command("login", cmd_login, "log in to uApply (device login, or a pasted token)")
    c.add_argument("--token", help="an access token to store instead of the device login")
    c.add_argument("--token-stdin", action="store_true", help="read the access token from stdin")
    command("logout", cmd_logout, "remove the stored uApply credentials")
    c = command("init", cmd_init, "bind the client folder to an existing uApply case", [folder])
    c.add_argument("--survey", required=True, help="the case's survey id")
    c.add_argument("--llm-mode", default="local_agent", choices=cases.LLM_MODES)
    command("status", cmd_status, "show the bound case's status", [folder])
    c = command("run", cmd_run, "run the case's queued tasks headlessly", [folder])
    c.add_argument("--runtime", choices=["auto", "claude-code", "codex"])
    c.add_argument("--model", help="runtime model (default: the runtime's own)")
    c.add_argument("--workers", type=int, help="parallel headless processes (1-4)")
    c.add_argument("--max-tasks", type=int)
    c.add_argument("--kinds", nargs="*", help="only these task kinds")
    c.add_argument("--follow", action="store_true", help="keep going until processing is done")
    c.add_argument("--force-ocr", action="store_true", help="ignore PDF text layers; always OCR with the model")
    command("mcp", cmd_mcp, "serve the MCP tools over stdio", [folder])
    c = command("chat", cmd_chat, "read the local chat archive (AnyChat)")
    chat = c.add_subparsers(dest="chat_cmd", required=True, metavar="SUBCOMMAND")
    chat.add_parser("sources", help="whether the chat archive is installed and signed in")
    chat.add_parser("find", help="find a contact by name").add_argument("name")
    fetch = chat.add_parser("fetch", help="save a contact's chat history in the client folder", parents=[folder])
    fetch.add_argument("contact")
    fetch.add_argument("--days", type=int, help="how far back (default: chat_default_days)")
    command("clean", cmd_clean, "remove cached files from the client folder", [folder])
    c = command("acrobat", cmd_acrobat, "check Adobe Acrobat Pro auto-fill on this PC (Windows)")
    c.add_argument("--pdf", help="a blank IMM 5709 to test-fill (writes <name>_uapply_test.pdf next to it)")
    c.add_argument("--node", default="form1[0].Page1[0].PersonalDetails[0].Name[0].FamilyName[0]",
                   help="XFA field to fill")
    c.add_argument("--value", default="TEST")
    c = command("config", cmd_config, "show settings, or change them with --set KEY=VALUE")
    c.add_argument("--set", nargs="*", metavar="KEY=VALUE")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")   # Windows consoles cannot print every name
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    args.argv = list(argv) if argv is not None else sys.argv[1:]
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        return int(args.fn(args, Settings.load()))
    except KeyboardInterrupt:
        return ExitCode.INTERRUPTED
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return ExitCode.USAGE
    except (ApiError, ChatError, LoginError, RunnerError, SetupError, ToolError) as e:
        hint = getattr(e, "hint", "")
        print(f"error: {e}" + (f" ({hint})" if hint else ""), file=sys.stderr)
        return ExitCode.ERROR


if __name__ == "__main__":
    sys.exit(main())
