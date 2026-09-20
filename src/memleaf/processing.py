"""Compatibility tombstone for the removed legacy processing engine.

memleaf v0.2.67 and later execute memory extraction only through the
incremental runtime.  This module remains importable so older integrations fail
with an explicit migration error instead of a ModuleNotFoundError, but it no
longer contains or executes the legacy engine.
"""
from __future__ import annotations

from typing import Any

from .process_common import ProcessingError


class Processor:
    """Removed legacy processor compatibility surface."""

    def __init__(self, service: Any):
        raise ProcessingError("legacy_pipeline_removed")


__all__ = ["Processor", "ProcessingError"]
