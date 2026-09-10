from __future__ import annotations

import gzip
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.config import save_config


def load_cases(path: Path) -> dict[str, Any]:
    raw = gzip.decompress(path.read_bytes()).decode("utf-8") if path.suffix == ".gz" else path.read_text(encoding="utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get("cases"), list):
        raise ValueError("invalid P1 case file")
    ids: set[str] = set()
    for case in value["cases"]:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise ValueError("invalid P1 case")
        if case["id"] in ids:
            raise ValueError("duplicate P1 case id")
        ids.add(case["id"])
        if not isinstance(case.get("events"), list) or not case["events"]:
            raise ValueError(f"{case['id']}: events are required")
        if not isinstance(case.get("expected"), list) or not case["expected"]:
            raise ValueError(f"{case['id']}: expected rubric is required")
    return value


def selected_cases(data: Mapping[str, Any], requested: Iterable[str]) -> list[dict[str, Any]]:
    requested_set = {item for item in requested if item}
    cases = [case for case in data["cases"] if isinstance(case, dict)]
    if not requested_set:
        return cases
    by_id = {case["id"]: case for case in cases}
    unknown = sorted(requested_set - set(by_id))
    if unknown:
        raise ValueError("unknown case id(s): " + ", ".join(unknown))
    return [case for case in cases if case["id"] in requested_set]


def build_plan(data: Mapping[str, Any], cases: list[Mapping[str, Any]], repetitions: int) -> dict[str, Any]:
    return {
        "status": data.get("status"),
        "baseline_refs": data.get("baseline_refs", {}),
        "case_count": len(cases),
        "repetitions": repetitions,
        "planned_process_runs": len(cases) * repetitions,
        "thinking": "low",
        "cases": [case["id"] for case in cases],
        "note": "No model call occurs unless --execute is supplied.",
    }


def evaluation_template(template: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact experiment config without changing the caller's mapping."""

    config = deepcopy(dict(template))
    llm = config.get("llm")
    if not isinstance(llm, Mapping):
        raise ValueError("config template has no llm section")
    llm = dict(llm)
    if not isinstance(llm.get("provider"), str) or not llm.get("provider"):
        raise ValueError("real-model execution requires an explicit llm.provider")
    if not isinstance(llm.get("model"), str) or not llm.get("model"):
        raise ValueError("real-model execution requires an exact llm.model")
    llm["diagnostic_logging"] = False
    thinking = llm.get("thinking")
    thinking = dict(thinking) if isinstance(thinking, Mapping) else {}
    thinking.update({"gate": "low", "summarize": "low", "compact": "low"})
    llm["thinking"] = thinking
    config["llm"] = llm
    return config


def _scope_registry(case: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = case.get("scope_registry", [])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"{case.get('id', '<case>')}: invalid scope_registry")
    return {item: {} for item in raw}


def _prepare_config(service: Memleaf, template: Mapping[str, Any], case: Mapping[str, Any]) -> None:
    config = evaluation_template(template)
    config["vault"] = str(service.vault.root)
    config["scopes"] = _scope_registry(case)
    save_config(service.vault.config_path, config)


def _seed_case(service: Memleaf, case: Mapping[str, Any], fixed_time: str) -> None:
    raw_seed = case.get("seed", [])
    if not isinstance(raw_seed, list):
        raise ValueError(f"{case.get('id', '<case>')}: seed must be a list")
    for raw in raw_seed:
        if not isinstance(raw, Mapping):
            raise ValueError("seed memory must be an object")
        item = dict(raw)
        item.setdefault("created", fixed_time)
        item.setdefault("updated", fixed_time)
        item.setdefault("sources", [])
        item.setdefault("scope_source", "user")
        service.write_memory(item)


def _capture_case(service: Memleaf, case: Mapping[str, Any]) -> None:
    source = case["source"]
    session_id = case["session_id"]
    for index, event in enumerate(case["events"], start=1):
        if not isinstance(event, Mapping):
            raise ValueError("event must be an object")
        role, content, timestamp = event.get("role"), event.get("content"), event.get("timestamp")
        if role not in {"user", "assistant"} or not isinstance(content, str) or not isinstance(timestamp, str):
            raise ValueError(f"{case['id']}: invalid event")
        event_id = f"p1/{case['id']}/{index}/{role}"
        with patch("memleaf.capture._timestamp", return_value=timestamp):
            service.capture(source, session_id, "t1", role, content, event_id=event_id)


def prepare_case_vault(root: Path, case: Mapping[str, Any], *, template: Mapping[str, Any], fixed_time: str) -> Memleaf:
    service = Memleaf.initialize(root)
    _prepare_config(service, template, case)
    _seed_case(service, case, fixed_time)
    _capture_case(service, case)
    return service
