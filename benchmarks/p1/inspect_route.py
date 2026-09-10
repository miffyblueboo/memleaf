#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memleaf.config import load_config
from memleaf.credentials import credential_text
from memleaf.llm import ModelRouter
from benchmarks.p1.fixture import evaluation_template


def endpoint_class(base_url: Any) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        return "unconfigured"
    try:
        host = (urlsplit(base_url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return "invalid"
    if not host:
        return "invalid"

    known_hosts = {
        "api.deepseek.com": "deepseek-direct",
        "openrouter.ai": "openrouter",
        "api.openai.com": "openai-direct",
        "api.anthropic.com": "anthropic-direct",
        "generativelanguage.googleapis.com": "gemini-direct",
        "inference-api.nousresearch.com": "nous",
    }
    if host in known_hosts:
        return known_hosts[host]
    return "custom-or-other"


def inspect_config(config_path: Path) -> dict[str, Any]:
    source = load_config(config_path)
    template = evaluation_template(source)
    llm = template.get("llm")
    if not isinstance(llm, Mapping):
        return {
            "status": "blocked",
            "reason": "missing_llm_section",
            "model_calls": 0,
        }

    env_name = llm.get("api_key_env")
    direct_key = credential_text(llm.get("api_key"))
    # Do not read or serialize arbitrary environment values here. ModelRouter
    # performs the same normal credential resolution used by the product.
    router = ModelRouter.from_config(template, mode="api")
    api_ready = router.api is not None
    base_url = llm.get("base_url")

    value = {
        "status": "ready" if api_ready else "blocked",
        "provider": llm.get("provider") if isinstance(llm.get("provider"), str) else None,
        "protocol": llm.get("protocol") if isinstance(llm.get("protocol"), str) else None,
        "model": llm.get("model") if isinstance(llm.get("model"), str) else None,
        "mode": "api",
        "thinking": "low",
        "endpoint_class": endpoint_class(base_url),
        "base_url_configured": isinstance(base_url, str) and bool(base_url.strip()),
        "credential_reference_configured": direct_key is not None or (
            isinstance(env_name, str) and bool(env_name.strip())
        ),
        "api_route_ready": api_ready,
        "model_calls": 0,
    }
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect a local memleaf Model Route without model calls or secret/URL output"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path.home() / ".memleaf" / "config.yaml",
        help="memleaf config path (default: ~/.memleaf/config.yaml)",
    )
    args = parser.parse_args(argv)
    value = inspect_config(args.config)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value.get("status") == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
