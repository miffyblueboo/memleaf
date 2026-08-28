#!/usr/bin/env python3
"""Small, offline memleaf API example.

The example uses only the Python standard library in addition to memleaf.  It
uses a temporary vault unless ``--vault`` is supplied, and never contacts a
model or network service.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


def _load_memleaf() -> object:
    """Import the source checkout when the example is run before installation."""

    try:
        from memleaf import Memleaf
    except ModuleNotFoundError:
        source_root = Path(__file__).resolve().parents[1] / "src"
        if not source_root.is_dir():
            raise
        sys.path.insert(0, str(source_root))
        from memleaf import Memleaf

    return Memleaf


def _memory_summary(memory: object) -> dict[str, object]:
    return {
        "memory_id": memory.memory_id,
        "title": memory.title,
        "body": memory.body,
    }


def _run(vault_path: Path) -> dict[str, object]:
    Memleaf = _load_memleaf()
    service = Memleaf.initialize(vault_path)
    created = service.create_memory(
        title="Offline example note",
        body="memleaf stores local Markdown memory for later search and context.",
        tags=["example", "local-first"],
        keywords=["Markdown", "search"],
        type="fact",
    )
    searched = service.search("Markdown", limit=5)
    contextual = service.context("local")
    page = service.read_page(contextual[0].memory_id) if contextual else None
    return {
        "vault": str(vault_path),
        "created": _memory_summary(created),
        "search": [_memory_summary(memory) for memory in searched],
        "context": [entry.to_dict() for entry in contextual],
        "read": page,
        "stats": service.stats(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vault",
        type=Path,
        help="vault directory to use; defaults to a temporary directory",
    )
    args = parser.parse_args(argv)

    if args.vault is not None:
        result = _run(args.vault.expanduser())
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    with tempfile.TemporaryDirectory(prefix="memleaf-example-") as temporary:
        result = _run(Path(temporary) / "vault")
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
