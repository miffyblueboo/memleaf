"""The small, non-interactive memleaf initialization CLI."""

from __future__ import annotations

import argparse
import getpass
import json
import os
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
    init.add_argument("--no-codex", action="store_true", help="compatibility no-op; Codex is disabled in v0.1")
    init.add_argument("--no-hermes", action="store_true", help="disable Hermes setup")
    init.add_argument(
        "--no-antigravity",
        action="store_true",
        help="accepted for compatibility; Antigravity is disabled in v0.1",
    )
    init.add_argument(
        "--no-model-discovery",
        action="store_true",
        help="skip host model discovery (an existing memleaf route is still preserved)",
    )
    install = commands.add_parser(
        "install",
        help="fully install and configure memleaf for Hermes",
    )
    install.add_argument("--vault", type=Path, default=None, help="vault directory")
    install.add_argument("--json", action="store_true", help="emit one JSON result")
    host_event = commands.add_parser(
        "host-event",
        help="legacy lifecycle compatibility entry (not installed in v0.1)",
    )
    host_event.add_argument("host", choices=("codex", "antigravity"))
    host_event.add_argument("event", nargs="?", default=None)
    host_event.add_argument("--vault", type=Path, default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            output = _init(args)
        elif args.command == "install":
            from .installer import install_hermes
            output = install_hermes(vault_path=args.vault)
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
            print(json.dumps({"error": "initialization failed"}, ensure_ascii=False))
        else:
            print("memleaf init failed", file=sys.stderr)
        return 1

    if args.command == "host-event":
        print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
        return 0
    if args.command == "install":
        if args.json:
            print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        elif output.get("status") == "configured":
            print(f"memleaf installed for Hermes: {output['vault']}")
            print("Restart Hermes to use memleaf.")
        else:
            print(f"memleaf install failed: {output.get('reason', 'unknown error')}", file=sys.stderr)
        return 0 if output.get("status") == "configured" else 2
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

    # Retain legacy APIs, but never inspect or configure unsupported hosts.
    # Clear stale activation claims in our own index, not the user's hosts.
    for name, config_path in (
        ("codex", home / ".codex" / "config.toml"),
        ("antigravity", home / ".gemini" / "config" / "mcp_config.json"),
    ):
        reason = f"{name} is disabled in v0.1; no detection or configuration performed"
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
        # An existing direct route is a safe idempotency fallback.  It is not
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
