"""Owner control-plane commands; never exposed as Agent MCP tools."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from .host_v2_auth import HostAuthorization, APPROVAL_KINDS
from .host_v2_common import V2Error

DEFAULT_PERMISSIONS = ["memory.search", "memory.read", "source.capture", "source.read", "work.read",
                       "memory.write", "memory.retract", "work.resume", "work.cancel"]


def add_commands(commands) -> None:
    for name in ("host-grant", "host-revoke", "host-approve", "host-account", "host-recovery-plan", "host-source-confirm", "host-processing-plan"):
        parser = commands.add_parser(name, help="owner control for the model-free host MCP profile")
        parser.add_argument("--vault", type=Path, required=True)
        parser.add_argument("--json", action="store_true")
        if name == "host-grant":
            parser.add_argument("--principal", required=True)
            parser.add_argument("--domain")
            parser.add_argument("--read-scope", action="append", default=None)
            parser.add_argument("--write-scope", action="append", default=None)
            parser.add_argument("--permission", action="append", default=None)
            parser.add_argument("--accept-caller-asserted", action="store_true")
            parser.add_argument("--allow-maintenance", action="store_true")
            parser.add_argument("--allow-delete", action="store_true")
            parser.add_argument("--allow-other-work-resume", action="store_true")
            parser.add_argument("--no-automatic-recording", action="store_true")
            parser.add_argument("--credential-file", type=Path)
        if name in {"host-revoke", "host-approve", "host-source-confirm"}:
            parser.add_argument("--grant-id", required=True)
        if name == "host-approve":
            parser.add_argument("--kind", choices=sorted(APPROVAL_KINDS), required=True)
            parser.add_argument("--plan-digest", required=True)
            parser.add_argument("--work-id")
        if name in {"host-account", "host-recovery-plan", "host-processing-plan"}:
            parser.add_argument("--work-id", required=True)
        if name == "host-source-confirm":
            parser.add_argument("--messages-file", type=Path, required=True)


def run(args) -> dict:
    from .service import Memleaf
    from .host_v2 import HostMemory
    service = Memleaf.initialize(args.vault) if args.command == "host-grant" else Memleaf(args.vault)
    auth = HostAuthorization(service.vault)
    if args.command == "host-grant":
        permissions = list(args.permission or DEFAULT_PERMISSIONS)
        if args.allow_maintenance:
            permissions += ["memory.maintain", "memory.read_history"]
        if args.allow_delete:
            permissions += ["memory.delete", "memory.read_history"]
        grant = auth.grant(args.principal, sorted(set(permissions)), args.read_scope or ["global"], args.write_scope or ["global"],
                           accept_caller_asserted=args.accept_caller_asserted, authorization_domain=args.domain,
                           allow_other_work_resume=args.allow_other_work_resume,
                           allow_automatic_recording=not args.no_automatic_recording)
        path = args.credential_file or service.vault._inside("_state", "host_credentials", args.principal + ".token")
        path = path.expanduser().absolute()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(grant["token"] + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            auth.revoke(grant["grant"]["grant_id"])
            raise V2Error("RESOURCE_UNAVAILABLE", "Credential file could not be created; existing files were preserved") from None
        return {"status": "success", "grant": grant["grant"], "credential_file": str(path),
                "mcpServers": {"memleaf": {"command": sys.executable, "args": ["-m", "memleaf.mcp_server", "--vault", str(service.vault.root), "--profile", "host", "--token-file", str(path)]}}}
    if args.command == "host-revoke":
        return {"status": "success", "grant": auth.revoke(args.grant_id)}
    if args.command == "host-approve":
        return {"status": "success", "approval": auth.approve(args.grant_id, args.kind, args.plan_digest, args.work_id)}
    host = HostMemory(service, "")
    if args.command == "host-account":
        from .host_v2_owner import account_work
        return {"status": "success", "work": account_work(host, args.work_id)}
    if args.command == "host-recovery-plan":
        from .host_v2_owner import recovery_plan
        return {"status": "success", "plan": recovery_plan(host, args.work_id)}
    if args.command == "host-processing-plan":
        from .host_v2_owner import processing_plan
        return {"status": "success", "plan": processing_plan(host, args.work_id)}
    if args.command == "host-source-confirm":
        from .host_v2_owner import register_confirmed_sources
        messages = json.loads(args.messages_file.read_text(encoding="utf-8"))
        return {"status": "success", **register_confirmed_sources(host, args.grant_id, messages)}
    raise V2Error("INVALID_SCHEMA")
