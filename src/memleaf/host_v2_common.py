"""Shared, dependency-free host-executed memory protocol primitives."""
from __future__ import annotations

import hashlib
import json
from typing import Any

PROTOCOL = "memleaf-host-v2.0-rc1"


class V2Error(ValueError):
    def __init__(self, code: str, message: str | None = None, *, retryable: bool = False,
                 next_action: str = "none", budget_charged: bool = False):
        self.code = code
        self.message = (message or code)[:300]
        self.retryable = retryable
        self.next_action = next_action
        self.budget_charged = budget_charged
        super().__init__(self.message)

    def public(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable,
                "next_action": self.next_action, "budget_charged": self.budget_charged}


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def revision(memory: Any) -> str:
    from .models import Memory
    from .turn_plan import revision_digest
    return "sha256:" + revision_digest(memory if isinstance(memory, Memory) else Memory.from_mapping(memory))


def success(data: dict[str, Any]) -> dict[str, Any]:
    return {"protocol_version": PROTOCOL, "ok": True, "data": data}


def failure(error: V2Error, **extra: Any) -> dict[str, Any]:
    return {"protocol_version": PROTOCOL, "ok": False, "error": error.public(), **extra}
