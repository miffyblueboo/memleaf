"""The small, non-interactive memleaf initialization CLI."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .adapters.base import (
    ConfigureResult,
    Detection,
    result_from_detection,
    update_agents_index,
)
from .adapters.hermes import HermesAdapter
from .credentials import credential_text
from .config import load_config
from .model_discovery import ModelCandidate, discover_models, manual_candidate, write_model_config
from .vault import Vault


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memleaf")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="initialize a vault and the Hermes MCP adapter")
    init.add_argument("--vault", type=Path, default=None, help="vault directory")
    init.add_argument("--all", action="store_true", help="diagnose all supported hosts, even uncertain ones")
    init.add_argument(
        "--defaults",
        action="store_true",
        help="use non-interactive defaults (accepted for scriptable setup)",
    )
    init.add_argument("--dry-run", action="store_true", help="show planned changes without writing")
    init.add_argument("--json", action="store_true", help="emit one JSON result")
    init.add_argument(
        "--no-codex",
        action="store_true",
        help="compatibility no-op; use install --host codex for explicit Codex setup",
    )
    init.add_argument("--no-hermes", action="store_true", help="disable Hermes setup")
    init.add_argument(
        "--no-antigravity",
        action="store_true",
        help="accepted for compatibility; Antigravity is currently unsupported",
    )
    init.add_argument(
        "--no-model-discovery",
        action="store_true",
        help="skip host model discovery (an existing memleaf route is still preserved)",
    )
    install = commands.add_parser(
        "install",
        help="install memleaf for Hermes (default) or an explicitly selected host",
    )
    install.add_argument(
        "--host",
        choices=("hermes", "codex"),
        default="hermes",
        help="host to configure (default: hermes)",
    )
    install.add_argument("--vault", type=Path, default=None, help="vault directory")
    install.add_argument(
        "--mcp-runtime",
        choices=("auto", "current", "existing"),
        default="auto",
        help=(
            "Hermes only: fail on a second memleaf runtime (auto), migrate to "
            "this installation (current), or retain a version-matched configured runtime (existing)"
        ),
    )
    install.add_argument("--json", action="store_true", help="emit one JSON result")
    audit = commands.add_parser("audit", help="inspect an existing Vault without changing it")
    audit.add_argument("--vault", type=Path, default=None)
    audit.add_argument("--json", action="store_true")
    process = commands.add_parser("process", help="process captured turns or preview on an isolated copy")
    process.add_argument("--vault", type=Path, default=None)
    process.add_argument("--source", default=None)
    process.add_argument("--session-id", default=None)
    process.add_argument("--scope", default=None)
    process.add_argument("--dry-run", action="store_true", help="no source Vault writes; may call the configured model")
    process.add_argument("--json", action="store_true")
    host_event = commands.add_parser(
        "host-event",
        help="host lifecycle hook entry",
    )
    host_event.add_argument("host", choices=("codex", "antigravity"))
    host_event.add_argument("event", nargs="?", default=None)
    host_event.add_argument("--vault", type=Path, default=None, help=argparse.SUPPRESS)
    return parser


def _configure_stdio_utf8() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="strict")
        except (OSError, ValueError):
            continue


def _format_command(command: Sequence[object]) -> str:
    values = [str(item) for item in command]
    return subprocess.list2cmdline(values) if os.name == "nt" else shlex.join(values)


def _print_install_failure(output: dict, *, host: str) -> None:
    print(
        f"memleaf install failed: {output.get('reason', 'unknown error')}",
        file=sys.stderr,
    )
    stage = output.get("stage")
    if isinstance(stage, str) and stage:
        print(f"failed stage: {stage}", file=sys.stderr)

    if host == "hermes" and output.get("core_version") is not None:
        provider_version = output.get("provider_version")
        if output.get("provider_updated"):
            provider_text = provider_version or "unknown"
        elif provider_version:
            provider_text = f"not changed (rejected candidate {provider_version})"
        else:
            provider_text = "not changed"
        print(
            "memleaf versions: "
            f"core={output.get('core_version') or 'unknown'}, "
            f"Hermes provider={provider_text}",
            file=sys.stderr,
        )

    runtime = output.get("mcp_runtime")
    if isinstance(runtime, dict):
        config_path = runtime.get("config_path")
        configured = runtime.get("configured_command")
        current = runtime.get("current_command") or runtime.get("expected_command")
        existing_version = runtime.get("existing_version")
        if config_path:
            print(f"Hermes config: {config_path}", file=sys.stderr)
        if configured:
            print(f"configured MCP runtime: {configured}", file=sys.stderr)
        if current:
            print(f"current memleaf runtime: {current}", file=sys.stderr)
        if existing_version:
            print(f"configured MCP version: {existing_version}", file=sys.stderr)

    mcp = output.get("mcp")
    if isinstance(mcp, dict):
        mcp_reason = mcp.get("reason")
        if isinstance(mcp_reason, str) and mcp_reason and mcp_reason != output.get("reason"):
            print(f"MCP detail: {mcp_reason}", file=sys.stderr)
        config_path = mcp.get("config_path")
        if config_path and not (
            isinstance(runtime, dict) and config_path == runtime.get("config_path")
        ):
            print(f"Hermes config: {config_path}", file=sys.stderr)
        backup_path = mcp.get("backup_path")
        if backup_path:
            print(f"config backup: {backup_path}", file=sys.stderr)

    rollback = output.get("rollback_status")
    if rollback == "completed":
        print("Hermes configuration was restored to its pre-install state.", file=sys.stderr)
    elif rollback == "failed":
        print(
            "WARNING: automatic rollback failed; inspect the reported Hermes paths before restarting.",
            file=sys.stderr,
        )

    user_action = output.get("user_action")
    if output.get("user_action_required") and isinstance(user_action, str) and user_action:
        print(f"action required: {user_action}", file=sys.stderr)

    commands = output.get("recovery_commands")
    if isinstance(commands, list) and commands:
        print("recovery commands:", file=sys.stderr)
        for command in commands:
            if isinstance(command, list) and command:
                print(f"  {_format_command(command)}", file=sys.stderr)

    diagnostic = ["python", "-m", "memleaf", "install", "--host", host]
    if host == "hermes" and isinstance(runtime, dict):
        policy = runtime.get("policy")
        if policy in {"current", "existing"}:
            diagnostic.extend(["--mcp-runtime", policy])
    diagnostic.append("--json")
    print(
        f"Run `{_format_command(diagnostic)}` for the complete diagnostic result.",
        file=sys.stderr,
    )


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdio_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            output = _init(args)
        elif args.command == "install":
            from .installer import install_codex, install_hermes

            if args.host == "codex":
                if args.mcp_runtime != "auto":
                    output = {
                        "status": "failure",
                        "stage": "arguments",
                        "reason": "--mcp-runtime applies only to Hermes installations",
                        "vault": str(args.vault) if args.vault is not None else None,
                    }
                else:
                    output = install_codex(vault_path=args.vault)
            else:
                install_kwargs = {"vault_path": args.vault}
                if args.mcp_runtime != "auto":
                    install_kwargs["mcp_runtime"] = args.mcp_runtime
                output = install_hermes(**install_kwargs)
        elif args.command in {"audit", "process"}:
            from .inspection import audit_vault, existing_root, preview_process
            if args.command == "audit":
                output = audit_vault(args.vault)
            elif args.dry_run:
                output = preview_process(args.vault, source=args.source, session_id=args.session_id, scope=args.scope)
            else:
                from .service import Memleaf
                output = Memleaf(existing_root(args.vault)).process(
                    source=args.source, session_id=args.session_id, scope=args.scope)
        elif args.command == "host-event":
            output = _host_event(args)
        else:  # pragma: no cover - argparse requires a known subcommand.
            raise ValueError("unknown command")
    except Exception:
        if getattr(args, "command", None) == "host-event":
            if (
                getattr(args, "host", None) == "antigravity"
                and isinstance(getattr(args, "event", None), str)
                and getattr(args, "event").casefold() == "stop"
            ):
                print('{"decision":"stop"}')
            else:
                print("{}")
            return 0
        if getattr(args, "json", False):
            print(json.dumps({"error": "operation failed" if args.command in {"audit", "process"}
                              else "initialization failed", "stage": args.command}, ensure_ascii=False))
        else:
            action = getattr(args, "command", "init")
            print(f"memleaf {action} failed unexpectedly", file=sys.stderr)
        return 1

    if args.command in {"audit", "process"}:
        print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=None if args.json else 2))
        return 0
    if args.command == "host-event":
        print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
        return 0
    if args.command == "install":
        if args.json:
            print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        elif output.get("status") in {"configured", "already_configured"}:
            print(f"memleaf installed for {args.host}: {output['vault']}")
            if args.host == "hermes" and (
                output.get("core_version") is not None
                or output.get("provider_version") is not None
            ):
                print(
                    "memleaf versions: "
                    f"core={output.get('core_version') or 'unknown'}, "
                    f"Hermes provider={output.get('provider_version') or 'unknown'}"
                )
            if output.get("vault_source") == "hermes_config":
                print("Preserved the Vault from the existing Hermes memleaf configuration.")
            if args.host == "hermes":
                runtime = output.get("mcp_runtime")
                if isinstance(runtime, dict) and runtime.get("selected_command"):
                    print(f"Hermes MCP runtime: {runtime['selected_command']}")
                print("Restart Hermes to use memleaf.")
            elif output.get("user_action_required"):
                print(f"Codex action required: {output.get('user_action')}")
            if output.get("model", {}).get("status") == "not_configured":
                print("Configure a memleaf model route before using automatic processing.")
        else:
            _print_install_failure(output, host=args.host)
        return 0 if output.get("status") in {"configured", "already_configured"} else 2
    if args.json:
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    else:
        _print_human_result(output)
    if any(result["status"] == "failure" for result in output["agents"].values()) or output.get("model", {}).get("status") == "failure":
        return 2
    return 0


def _host_event(args: argparse.Namespace) -> dict:
    from .host_events import _antigravity_stop_response, _is_antigravity_stop_event, handle_event

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeError, OSError):
        return (
            _antigravity_stop_response()
            if args.host == "antigravity" and _is_antigravity_stop_event({}, args.event)
            else {}
        )
    if not isinstance(payload, dict):
        return (
            _antigravity_stop_response()
            if args.host == "antigravity" and _is_antigravity_stop_event(payload, args.event)
            else {}
        )
    return handle_event(args.host, payload, vault=args.vault, event_name=args.event)


def _init(args: argparse.Namespace) -> dict:
    home = _home_from_environment()
    requested_vault = args.vault if args.vault is not None else home / ".memleaf"
    if args.dry_run:
        vault = Vault(requested_vault, create=False)
    else:
        # Initialization is deliberately the first mutating operation.
        vault = Vault.initialize(requested_vault)
    model_result = _prepare_model_route(
        vault.root,
        home=home,
        dry_run=args.dry_run,
        non_interactive=bool(args.defaults),
        skip_discovery=bool(args.no_model_discovery),
    )

    adapters = [
        ("hermes", HermesAdapter(home=home, env=os.environ), args.no_hermes),
    ]
    results: dict[str, ConfigureResult] = {}
    for name, adapter, disabled in adapters:
        if disabled:
            detection = Detection(
                agent=name,
                detected=False,
                confidence="none",
                reason="disabled by command-line flag",
                config_path=str(adapter.config_path),
                status="disabled",
            )
            results[name] = result_from_detection(
                detection,
                status="disabled",
                reason="disabled by command-line flag",
                dry_run=args.dry_run,
            )
            continue
        detection = adapter.detect()
        results[name] = adapter.configure(
            detection,
            vault.root,
            dry_run=args.dry_run,
            attempt=args.all,
        )

    # Retain legacy init result slots without implicitly configuring hosts.
    # Codex is supported only through the explicit install command; Antigravity
    # remains unsupported. Clear stale activation claims in our own index only.
    legacy_slots = (
        (
            "codex",
            home / ".codex" / "config.toml",
            "Codex is not configured by init; use install --host codex",
        ),
        (
            "antigravity",
            home / ".gemini" / "config" / "mcp_config.json",
            "Antigravity is currently unsupported; no detection or configuration performed",
        ),
    )
    for name, config_path, reason in legacy_slots:
        results[name] = result_from_detection(
            Detection(
                agent=name, detected=False, confidence="none", reason=reason,
                config_path=str(config_path), status="disabled",
            ),
            status="disabled",
            reason=reason,
            dry_run=args.dry_run,
            hook_trust_status="disabled",
            hook_activation_status="disabled",
            hook_definition_hash="",
            user_action_required=False,
            user_action="",
        )

    agents = {name: result.to_dict() for name, result in results.items()}
    agents_index_written = False
    if not args.dry_run:
        agents_index_written = update_agents_index(vault.agents_index_path, agents)

    return {
        "version": 1,
        "vault": str(vault.root),
        "agents_index_path": str(vault.agents_index_path),
        "agents_index_written": agents_index_written,
        "dry_run": bool(args.dry_run),
        "agents": agents,
        "model": model_result,
    }


def _prepare_model_route(
    requested_vault: Path,
    *,
    home: Path,
    dry_run: bool,
    non_interactive: bool,
    skip_discovery: bool,
) -> dict:
    """Discover/configure the standalone process model without touching core processing."""

    if skip_discovery:
        existing = _existing_memleaf_route(requested_vault / "config.yaml")
        if existing is None:
            return {
                "status": "not_configured",
                "reason": "model discovery disabled and no complete memleaf route exists",
                "selected": None,
                "candidates": [],
                "diagnostics": [],
            }
        return {
            "status": "already_configured",
            "reason": "model discovery disabled; existing memleaf route preserved",
            "selected": existing.public_dict(),
            "candidates": [],
            "diagnostics": [],
        }

    discovery = discover_models(home=home, env=os.environ, include_codex=False)
    candidate = discovery.selected
    diagnostics = list(discovery.diagnostics)
    if candidate is None:
        # An existing direct route is a safe idempotency fallback. It is not
        # reported as a host discovery result and never appears with its key.
        candidate = _existing_memleaf_route(requested_vault / "config.yaml")
    if candidate is None and not dry_run and not non_interactive and sys.stdin.isatty():
        try:
            candidate = _prompt_for_model()
        except (EOFError, KeyboardInterrupt, ValueError) as error:
            # ``ValueError`` only contains route-shape diagnostics; never echo
            # prompt input or an exception containing a credential.
            diagnostics.append(f"manual model configuration failed ({error})")
    if candidate is None:
        status = "diagnostic" if dry_run else "failure"
        reason = (
            "no complete callable chat model route discovered; interactive model configuration is required"
            if not dry_run
            else "no complete callable chat model route discovered"
        )
        return {
            "status": status,
            "reason": reason,
            "selected": None,
            "candidates": [item.public_dict() for item in discovery.candidates],
            "diagnostics": diagnostics,
        }
    if not dry_run:
        try:
            public = write_model_config(requested_vault / "config.yaml", candidate, vault=requested_vault)
        except Exception:
            return {
                "status": "failure",
                "reason": "could not write model route configuration",
                "selected": candidate.public_dict(),
                "candidates": [item.public_dict() for item in discovery.candidates],
                "diagnostics": diagnostics,
            }
        status = "already_configured" if candidate.source == "memleaf" else "configured"
    else:
        public = candidate.public_dict()
        status = "would_configure"
    return {
        "status": status,
        "reason": "complete callable chat model route selected",
        "selected": public,
        "candidates": [item.public_dict() for item in discovery.candidates],
        "diagnostics": diagnostics,
    }


def _existing_memleaf_route(path: Path) -> ModelCandidate | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        config = load_config(path, vault=path.parent)
    except (OSError, ValueError):
        return None
    llm = config.get("llm")
    if not isinstance(llm, dict):
        return None
    key = credential_text(llm.get("api_key"))
    if key is None:
        env_name = llm.get("api_key_env")
        key = credential_text(
            os.environ.get(env_name)
            if isinstance(env_name, str) and env_name.strip()
            else None
        )
    if key is None:
        return None
    try:
        return ModelCandidate(
            source="memleaf",
            provider=str(llm.get("provider", "")),
            protocol=str(llm.get("protocol", "openai")),
            base_url=str(llm.get("base_url", "")),
            model=str(llm.get("model", "")),
            api_key=key,
            context_window=int(llm.get("context_window", 200000)),
            source_detail="existing memleaf route",
        )
    except (TypeError, ValueError):
        return None


def _prompt_for_model() -> ModelCandidate:
    print("memleaf: no complete callable chat model was found; configure one for processing.", file=sys.stderr)
    provider = input("Provider (openai/claude/gemini): ").strip()
    protocol = input("Protocol (openai/claude/gemini): ").strip()
    base_url = input("Base URL: ").strip()
    model = input("Model: ").strip()
    api_key = getpass.getpass("API key (input hidden): ")
    return manual_candidate(
        provider=provider,
        protocol=protocol,
        base_url=base_url,
        model=model,
        api_key=api_key,
    )


def _home_from_environment() -> Path:
    raw_home = os.environ.get("HOME")
    return (Path(raw_home).expanduser() if raw_home else Path.home()).resolve()


def _print_human_result(output: dict) -> None:
    prefix = "dry-run" if output["dry_run"] else "initialized"
    print(f"memleaf {prefix}: {output['vault']}")
    for name, result in output["agents"].items():
        status = result["status"]
        suffix = " (changed)" if result["changed"] else ""
        print(f"{name}: {status}{suffix}")
        user_action = result.get("user_action")
        if result.get("user_action_required") and isinstance(user_action, str) and user_action:
            print(f"{name}: action required: {user_action}")
    model = output.get("model")
    if isinstance(model, dict):
        selected = model.get("selected") or {}
        if isinstance(selected, dict) and selected.get("provider") and selected.get("model"):
            print(f"model: {model.get('status')} {selected['provider']}/{selected['model']}")
        else:
            print(f"model: {model.get('status')}")
    if output["dry_run"]:
        print(f"agents index not written: {output['agents_index_path']}")
    else:
        print(f"agents index: {output['agents_index_path']}")


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess smoke tests.
    raise SystemExit(main())
