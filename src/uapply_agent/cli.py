"""uapply-agent CLI: setup | login | init | status | run | mcp | chat | clean | config."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .api import ApiError, UApplyApi
from .auth import LoginError, device_login, token_login
from .chat.base import ChatError
from .config import Credentials, Settings
from .folder import WorkingFolder
from .integrate import run_setup
from .updater import maybe_delegate, resolve_target, running_sha
from .runners import RunnerError, detect_runtimes


_ARGV: list[str] = []


def _argv() -> list[str]:
    return _ARGV or sys.argv[1:]


def _refresh_plugin() -> None:
    """A new version may ship new /uapply:* commands; rewrite them if the plugin is installed."""
    try:
        from .integrate import PLUGIN_DIR, install_claude_plugin
        home = Path(os.environ.get("UAPPLY_HOME") or Path.home())
        if (home / PLUGIN_DIR).exists():
            install_claude_plugin(home)
    except Exception:
        pass


def cmd_update(args, settings):
    target, reason = resolve_target(settings, wait_s=600, say=print)
    print(f"uapply-agent: {reason}" + (f" -> {target}" if target else ""))
    return 0


def _folder(args) -> WorkingFolder:
    return WorkingFolder(Path(args.folder or os.environ.get("UAPPLY_FOLDER") or os.getcwd()))


def cmd_login(args, settings):
    try:
        if args.token:
            token_login(args.token)
        elif args.token_stdin:
            token_login(sys.stdin.read())
        else:
            device_login(settings)
    except LoginError as e:
        print(f"login failed: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_setup(args, settings):
    """Register the MCP server with every runtime found, then log in unless a token exists."""
    if sha := os.environ.get("UAPPLY_INSTALLED_SHA", "").strip():
        settings.installed_sha = sha
        settings.save()
    run_setup(settings=settings, login=not args.no_login)
    if args.no_login or Credentials.get_token():
        print("uApply login: already signed in" if Credentials.get_token() else "uApply login: skipped")
    else:
        try:
            device_login(settings)
        except LoginError as e:
            print(f"login failed: {e} — run `uapply-agent login` later", file=sys.stderr)
    print("Done. Open Claude Code or Codex in a client folder and type /uapply:run")
    return 0


def cmd_logout(args, settings):
    Credentials.clear()
    print("credentials removed")
    return 0


def cmd_init(args, settings):
    api = UApplyApi(settings)
    f = _folder(args)
    s = api.survey(args.survey)
    api.set_llm_mode(args.survey, args.llm_mode)
    deps = [{"survey_id": d.get("id"), "name": d.get("name"), "relationship": d.get("relationship")}
            for d in (s.get("dependents") or []) if isinstance(d, dict)]
    case = f.init_case(args.survey, settings.backend_url, args.llm_mode, name=s.get("name", ""), dependents=deps)
    print(json.dumps(case, indent=2))
    rt = detect_runtimes()
    print(f"runtimes detected: {[r['path'] for r in rt] or 'none — install Claude Code or Codex'}", file=sys.stderr)
    return 0


def cmd_status(args, settings):
    f = _folder(args)
    if not f.survey_id:
        print("folder has no case; run `uapply-agent init --survey <id>`", file=sys.stderr)
        return 2
    print(json.dumps(UApplyApi(settings).agent_status(f.survey_id), indent=2))
    return 0


def cmd_run(args, settings):
    maybe_delegate(_argv(), settings, wait_s=180)
    from .executor import Executor
    f = _folder(args)
    api = UApplyApi(settings)
    ex = Executor(api, f, runtime=args.runtime or settings.runtime, model=args.model or settings.model,
                  force_ocr=args.force_ocr or settings.force_ocr)
    print(f"executor: {ex.runner.name} session {ex.session_id}", file=sys.stderr)
    while True:
        stats = ex.run(max_tasks=args.max_tasks, workers=args.workers or settings.workers, kinds=args.kinds)
        print(json.dumps(stats.as_dict()), file=sys.stderr)
        if stats.plan_limited:
            print("plan limit reached; run again later", file=sys.stderr)
            return 3
        if stats.runtime_error:
            print(f"stopped: {stats.runtime_error}", file=sys.stderr)
            return 4
        if not args.follow:
            break
        st = api.agent_wait(f.survey_id, "processing", 45)
        if st.get("done") and stats.remaining == 0:
            break
    print(json.dumps(api.agent_status(f.survey_id), indent=2))
    return 0


def cmd_mcp(args, settings):
    maybe_delegate(_argv(), settings)
    _refresh_plugin()
    if args.folder:
        os.environ["UAPPLY_FOLDER"] = str(Path(args.folder).resolve())
    from .mcp_server import main as mcp_main
    mcp_main()
    return 0


def cmd_chat(args, settings):
    from .chat.anychat import AnyChatSource
    from .chat.store import ChatStore
    src = AnyChatSource(binary=settings.anychat_bin)
    if args.chat_cmd == "sources":
        print(json.dumps(src.available().as_dict(), indent=2, ensure_ascii=False))
        return 0
    if args.chat_cmd == "find":
        print(json.dumps([c.public() for c in src.resolve(args.name)], indent=2, ensure_ascii=False))
        return 0
    f = _folder(args)
    t = src.fetch(args.contact, args.days or settings.chat_default_days, f.chat)
    ChatStore(f).record(t)
    print(json.dumps(t.public(), indent=2, ensure_ascii=False))
    print("Next: `uapply-agent init --survey <id>` (or the create_case tool) then chat_upload, or use the MCP tools.", file=sys.stderr)
    return 0


def cmd_clean(args, settings):
    print(f"removed {_folder(args).clean()} cached files")
    return 0


def cmd_config(args, settings):
    if args.set:
        for kv in args.set:
            k, _, v = kv.partition("=")
            if not hasattr(settings, k):
                print(f"unknown setting {k}", file=sys.stderr)
                return 2
            cur = getattr(settings, k)
            if isinstance(cur, bool):   # bool("false") is True
                val = v.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(cur, dict):
                val = json.loads(v)
            else:
                val = type(cur)(v)
            setattr(settings, k, val)
        settings.save()
    print(json.dumps(settings.__dict__, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="uapply-agent", description="Run uApply cases from Claude Code / Codex")
    p.add_argument("--version", action="version", version=f"{__version__} ({(running_sha(Settings.load()) or 'unknown')[:7]})")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("update", help="install the latest version now (it also happens at mcp/run start)").set_defaults(fn=cmd_update)

    s = sub.add_parser("setup", help="register the MCP server with Claude Code / Codex and log in")
    s.add_argument("--no-login", action="store_true"); s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("login", help="Auth0 device login, or --token to paste a JWT")
    s.add_argument("--token"); s.add_argument("--token-stdin", action="store_true")
    s.set_defaults(fn=cmd_login)
    sub.add_parser("logout").set_defaults(fn=cmd_logout)

    s = sub.add_parser("init", help="bind this folder to a survey")
    s.add_argument("--survey", required=True); s.add_argument("--folder")
    s.add_argument("--llm-mode", default="local_agent", choices=["local_agent", "server"])
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("status"); s.add_argument("--folder"); s.set_defaults(fn=cmd_status)

    s = sub.add_parser("run", help="execute queued agent tasks headlessly")
    s.add_argument("--folder"); s.add_argument("--runtime", choices=["auto", "claude-code", "codex"])
    s.add_argument("--model"); s.add_argument("--workers", type=int); s.add_argument("--max-tasks", type=int)
    s.add_argument("--kinds", nargs="*"); s.add_argument("--follow", action="store_true", help="keep going until processing is done")
    s.add_argument("--force-ocr", action="store_true", help="ignore PDF text layers; always OCR with the model")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("mcp", help="serve the MCP tools over stdio"); s.add_argument("--folder"); s.set_defaults(fn=cmd_mcp)
    s = sub.add_parser("chat", help="local chat archive (AnyChat): sources | find <name> | fetch <contact>")
    cs = s.add_subparsers(dest="chat_cmd", required=True)
    cs.add_parser("sources")
    c = cs.add_parser("find"); c.add_argument("name")
    c = cs.add_parser("fetch"); c.add_argument("contact"); c.add_argument("--days", type=int); c.add_argument("--folder")
    s.set_defaults(fn=cmd_chat)
    s = sub.add_parser("clean"); s.add_argument("--folder"); s.set_defaults(fn=cmd_clean)
    s = sub.add_parser("config"); s.add_argument("--set", nargs="*", metavar="KEY=VALUE"); s.set_defaults(fn=cmd_config)
    return p


def main(argv=None) -> int:
    global _ARGV
    _ARGV = list(argv) if argv is not None else []
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings.load()
    try:
        return args.fn(args, settings) or 0
    except ApiError as e:
        print(f"error: {e}" + (f" — {e.hint}" if e.hint else ""), file=sys.stderr)
        return 2
    except RunnerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except ChatError as e:
        print(f"error: {e}" + (f" — {e.hint}" if e.hint else ""), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
