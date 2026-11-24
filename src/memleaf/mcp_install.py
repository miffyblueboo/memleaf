"""Install the host-profile MCP server into local agents.

Every agent except Hermes uses the same stdio server entry; only where and how
the entry is written differs. Hermes keeps its native provider installation.
Existing memleaf entries that this installer did not create (for example a
model-profile setup) are reported and left unchanged, so no agent ends up with
two routes processing the same conversation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
from typing import Any, Callable, Mapping

from .locking import atomic_write_text

SERVER_NAME = "memleaf"
MCP_AGENTS = ("claude-code", "codex", "cursor", "vscode", "gemini", "junie", "windsurf", "claude-desktop")
LABELS = {
    "claude-code": "Claude Code", "codex": "Codex", "cursor": "Cursor", "vscode": "VS Code (Copilot)",
    "gemini": "Gemini CLI", "junie": "JetBrains Junie", "windsurf": "Windsurf / Devin Desktop",
    "claude-desktop": "Claude Desktop", "hermes": "Hermes",
}


@dataclass
class Context:
    home: Path
    env: Mapping[str, str]
    platform: str = sys.platform
    runner: Callable[..., Any] = subprocess.run


@dataclass
class Found:
    detected: bool
    reason: str
    config_path: Path | None = None
    executable: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def server_entry(vault: Path, token_file: Path, python: str | None = None) -> dict[str, Any]:
    return {"command": python or sys.executable,
            "args": ["-m", "memleaf.mcp_server", "--vault", str(vault), "--profile", "host",
                     "--token-file", str(token_file)]}


def _managed(entry: Any) -> bool:
    """An entry this installer owns: the memleaf server in host profile."""
    args = entry.get("args") if isinstance(entry, Mapping) else None
    return (isinstance(args, list) and "memleaf.mcp_server" in args
            and "--profile" in args and args[args.index("--profile") + 1:args.index("--profile") + 2] == ["host"])


def _which(name: str, ctx: Context) -> str | None:
    from .adapters.base import resolve_executable
    return resolve_executable(name, env=dict(ctx.env))


def _first_existing(*paths: Path) -> Path | None:
    return next((path for path in paths if path.exists()), None)


# --------------------------------------------------------------------------- detection

def _claude_code_cli(ctx: Context) -> str | None:
    explicit = ctx.env.get("CLAUDE_CODE_CLI_PATH")
    if explicit and Path(explicit).is_file():
        return explicit
    found = _which("claude", ctx)
    if found:
        return found
    if ctx.platform == "darwin":  # CLI bundled with the Claude desktop app
        root = ctx.home / "Library" / "Application Support" / "Claude" / "claude-code"
        candidates = sorted(root.glob("*/*/claude.app/Contents/MacOS/claude"),
                            key=lambda path: [int(p) if p.isdigit() else 0 for p in path.parts[-6].split(".")])
        if candidates:
            return str(candidates[-1])
    return None


def detect(agent: str, ctx: Context) -> Found:
    home, env = ctx.home, ctx.env
    if agent == "claude-code":
        cli = _claude_code_cli(ctx)
        config = home / ".claude.json"
        if cli:
            return Found(True, "claude executable found", config, cli)
        if config.exists() or (home / ".claude").is_dir():
            return Found(False, "Claude Code configuration found but its CLI is unavailable", config)
        return Found(False, "not found")
    if agent == "codex":
        root = Path(env.get("CODEX_HOME") or home / ".codex")
        if _which("codex", ctx) or root.is_dir():
            return Found(True, "Codex home found", root / "config.toml", _which("codex", ctx),
                         {"hooks": root / "hooks.json"})
        return Found(False, "not found")
    if agent == "cursor":
        root = home / ".cursor"
        return Found(root.is_dir(), "Cursor home found" if root.is_dir() else "not found", root / "mcp.json")
    if agent == "vscode":
        root = Path(env.get("COPILOT_HOME") or home / ".copilot")
        apps = (Path("/Applications/Visual Studio Code.app"), Path("/Applications/Visual Studio Code - Insiders.app"))
        found = root.is_dir() or bool(_which("code", ctx)) or (ctx.platform == "darwin" and any(p.exists() for p in apps))
        return Found(found, "VS Code or Copilot home found" if found else "not found", root / "mcp-config.json")
    if agent == "gemini":
        config = home / ".gemini" / "settings.json"
        found = bool(_which("gemini", ctx)) or config.is_file()
        return Found(found, "Gemini CLI found" if found else "not found", config)
    if agent == "junie":
        root = home / ".junie"
        return Found(root.is_dir(), "Junie home found" if root.is_dir() else "not found", root / "mcp" / "mcp.json")
    if agent == "windsurf":
        legacy, current = home / ".codeium" / "windsurf", home / ".config" / "devin"
        root = _first_existing(legacy, current)
        if root is None:
            return Found(False, "not found")
        return Found(True, f"{root} found", root / "mcp_config.json")
    if agent == "claude-desktop":
        if ctx.platform == "darwin":
            root = home / "Library" / "Application Support" / "Claude"
        elif ctx.platform.startswith("win"):
            root = Path(env.get("APPDATA") or home / "AppData" / "Roaming") / "Claude"
        else:
            root = home / ".config" / "Claude"
        return Found(root.is_dir(), "Claude Desktop data found" if root.is_dir() else "not found",
                     root / "claude_desktop_config.json")
    raise ValueError(f"unknown agent: {agent}")


# --------------------------------------------------------------------------- writers

def _result(agent: str, status: str, reason: str, found: Found | None = None, **extra: Any) -> dict[str, Any]:
    row = {"agent": agent, "label": LABELS[agent], "status": status, "reason": reason}
    if found is not None and found.config_path is not None:
        row["config_path"] = str(found.config_path)
    row.update(extra)
    return row


def _write_json_entry(agent: str, found: Found, entry: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    path = found.config_path
    if path.is_symlink():
        return _result(agent, "diagnostic", "configuration path is a symlink; not modified", found)
    data: Any = {}
    original = None
    if path.exists():
        try:
            original = path.read_text(encoding="utf-8")
            data = json.loads(original) if original.strip() else {}
        except (OSError, UnicodeError, ValueError):
            return _result(agent, "diagnostic", "configuration is not valid JSON; not modified", found)
    if not isinstance(data, dict) or not isinstance(data.setdefault("mcpServers", {}), dict):
        return _result(agent, "diagnostic", "configuration has an unexpected shape; not modified", found)
    current = data["mcpServers"].get(SERVER_NAME)
    if current == entry:
        return _result(agent, "already_configured", "memleaf entry is current", found)
    if current is not None and not _managed(current):
        return _result(agent, "kept_existing", "an existing memleaf entry was not created by this installer; left unchanged", found)
    if dry_run:
        return _result(agent, "dry_run", ("would update" if current else "would add") + " the memleaf entry", found)
    data["mcpServers"][SERVER_NAME] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    if original is not None:
        atomic_write_text(path.with_name(path.name + ".memleaf-backup"), original)
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", mode=0o600)
    return _result(agent, "configured", ("updated" if current else "added") + " the memleaf entry", found,
                   backup=str(path.with_name(path.name + ".memleaf-backup")) if original is not None else None)


def _toml_string(value: str) -> str:
    return json.dumps(value)  # a JSON string is a valid TOML basic string


def _codex(found: Found, entry: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    path, hooks = found.config_path, found.extra["hooks"]
    if hooks.is_file():
        try:
            if "memleaf.cli host-event codex" in hooks.read_text(encoding="utf-8"):
                return _result("codex", "kept_existing", "Codex already has memleaf model-profile hooks; left unchanged "
                               "(remove them before switching to the host profile)", found)
        except (OSError, UnicodeError):
            return _result("codex", "diagnostic", "Codex hooks file is unreadable; not modified", found)
    if path.is_symlink():
        return _result("codex", "diagnostic", "configuration path is a symlink; not modified", found)
    text = ""
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
            parsed = tomllib.loads(text)
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            return _result("codex", "diagnostic", "config.toml is not valid TOML; not modified", found)
        current = parsed.get("mcp_servers", {}).get(SERVER_NAME)
        if current is not None:
            same = current.get("command") == entry["command"] and current.get("args") == entry["args"]
            if same:
                return _result("codex", "already_configured", "memleaf entry is current", found)
            reason = ("an existing memleaf entry differs; update it to the command shown or remove it and rerun"
                      if _managed(current) else "an existing memleaf entry was not created by this installer; left unchanged")
            return _result("codex", "kept_existing", reason, found)
    if dry_run:
        return _result("codex", "dry_run", "would add the memleaf entry", found)
    block = (f"\n[mcp_servers.{SERVER_NAME}]\ncommand = {_toml_string(entry['command'])}\n"
             f"args = [{', '.join(_toml_string(arg) for arg in entry['args'])}]\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    if text:
        atomic_write_text(path.with_name(path.name + ".memleaf-backup"), text)
    new_text = (text if not text or text.endswith("\n") else text + "\n") + block
    tomllib.loads(new_text)  # never write a file Codex cannot parse
    atomic_write_text(path, new_text, mode=0o600)
    return _result("codex", "configured", "added the memleaf entry", found)


def _claude_code(found: Found, entry: dict[str, Any], ctx: Context, *, dry_run: bool) -> dict[str, Any]:
    current = None
    config = found.config_path
    if config is not None and config.is_file():
        try:  # read-only: Claude Code owns this file and rewrites it while running
            current = (json.loads(config.read_text(encoding="utf-8")).get("mcpServers") or {}).get(SERVER_NAME)
        except (OSError, UnicodeError, ValueError, AttributeError):
            current = None
    if current is not None:
        comparable = {k: current.get(k) for k in ("command", "args")}
        if comparable == entry:
            return _result("claude-code", "already_configured", "memleaf entry is current", found)
        if not _managed(current):
            return _result("claude-code", "kept_existing", "an existing memleaf entry was not created by this installer; left unchanged", found)
    command = [found.executable, "mcp", "add", "--scope", "user", SERVER_NAME, "--", entry["command"], *entry["args"]]
    if dry_run:
        return _result("claude-code", "dry_run", ("would update" if current else "would add") + " the memleaf entry via the claude CLI", found)
    if current is not None:
        ctx.runner([found.executable, "mcp", "remove", "--scope", "user", SERVER_NAME],
                   capture_output=True, text=True, timeout=60)
    done = ctx.runner(command, capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        return _result("claude-code", "failed", "claude mcp add failed", found,
                       detail=(done.stderr or done.stdout or "").strip()[:300])
    return _result("claude-code", "configured", ("updated" if current else "added") + " the memleaf entry via the claude CLI", found)


def configure(agent: str, found: Found, entry: dict[str, Any], ctx: Context, *, dry_run: bool) -> dict[str, Any]:
    if agent == "claude-code":
        return _claude_code(found, entry, ctx, dry_run=dry_run)
    if agent == "codex":
        return _codex(found, entry, dry_run=dry_run)
    return _write_json_entry(agent, found, entry, dry_run=dry_run)


# --------------------------------------------------------------------------- grants and verification

def ensure_credential(vault: Any, principal: str, *, dry_run: bool) -> tuple[Path, str]:
    """Reuse a valid per-agent credential or create one; never overwrite a file."""
    from .host_v2_auth import HostAuthorization
    from .host_v2_cli import DEFAULT_PERMISSIONS
    path = vault._inside("_state", "host_credentials", principal + ".token")
    if not path.exists() and dry_run:
        return path, "would_create"
    auth = HostAuthorization(vault)
    if path.exists():
        try:
            auth.authenticate(path.read_text(encoding="utf-8").strip())
            return path, "existing"
        except Exception:  # revoked or damaged: report, never replace user data
            return path, "invalid"
    grant = auth.grant(principal, list(DEFAULT_PERMISSIONS), ["global"], ["global"], accept_caller_asserted=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(grant["token"] + "\n")
    except OSError:
        auth.revoke(grant["grant"]["grant_id"])
        raise
    return path, "created"


def verify_entry(entry: dict[str, Any], env: Mapping[str, str], runner: Callable[..., Any] = subprocess.run) -> bool:
    """Start the exact configured command once and require the host tools."""
    messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "memleaf-installer", "version": "1"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    try:
        done = runner([entry["command"], *entry["args"]], input="\n".join(map(json.dumps, messages)) + "\n",
                      capture_output=True, text=True, timeout=30, env=dict(env))
        rows = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
        names = {tool["name"] for row in rows if row.get("id") == 2 for tool in row["result"]["tools"]}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return False
    return {"memory_capabilities", "prepare_memory", "submit_memory", "search", "read"} <= names


# --------------------------------------------------------------------------- entry point

def resolve_vault(explicit: Path | None, ctx: Context) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    from .installer import _existing_hermes_vault, _hermes_home
    existing = _existing_hermes_vault(_hermes_home(ctx.home), ctx.home)
    if existing is not None:
        return existing
    if ctx.env.get("MEMLEAF_VAULT"):
        return Path(ctx.env["MEMLEAF_VAULT"]).expanduser().resolve()
    return ctx.home / ".memleaf"


def install_mcp_agents(agents: list[str], *, vault_path: Path | None = None, dry_run: bool = False,
                       only_detected: bool = False, ctx: Context | None = None) -> dict[str, Any]:
    from .vault import Vault
    ctx = ctx or Context(home=Path(os.environ.get("HOME") or Path.home()).resolve(), env=os.environ)
    try:
        vault_root = resolve_vault(vault_path, ctx)
    except RuntimeError as error:
        return {"status": "failure", "reason": str(error), "vault": None, "agents": []}
    vault = Vault(vault_root, create=not dry_run)
    rows = []
    for agent in agents:
        found = detect(agent, ctx)
        if not found.detected:
            if not only_detected:
                rows.append(_result(agent, "not_detected", found.reason, found))
            elif "unavailable" in found.reason:
                rows.append(_result(agent, "diagnostic", found.reason, found))
            continue
        try:
            token_file, credential = ensure_credential(vault, agent, dry_run=dry_run)
        except Exception as error:  # pragma: no cover - surfaced as a row
            rows.append(_result(agent, "failed", f"could not create a credential: {type(error).__name__}", found))
            continue
        if credential == "invalid":
            rows.append(_result(agent, "diagnostic", f"existing credential {token_file} is no longer valid; "
                                "remove it and rerun to issue a new one", found))
            continue
        entry = server_entry(vault_root, token_file)
        row = configure(agent, found, entry, ctx, dry_run=dry_run)
        row["credential"] = credential
        rows.append(row)
    configured = [row for row in rows if row["status"] in {"configured", "already_configured"}]
    verified = None
    if configured and not dry_run:
        token_file = vault._inside("_state", "host_credentials", configured[0]["agent"] + ".token")
        verified = verify_entry(server_entry(vault_root, token_file), ctx.env)
    failed = [row for row in rows if row["status"] in {"failed", "diagnostic"}]
    status = ("dry_run" if dry_run else "failure" if failed and not configured
              else "partial" if failed or verified is False else "success")
    return {"status": status, "vault": str(vault_root), "agents": rows, "server_verified": verified,
            "caller_trust": "accept_caller_asserted",
            "next_step": "Restart each configured agent so it loads the memleaf MCP server." if configured else None}
