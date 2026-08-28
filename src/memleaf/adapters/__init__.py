"""Host detection and MCP configuration adapters."""

from .antigravity import AntigravityAdapter
from .base import CommandResult, ConfigureResult, Detection
from .codex import CodexAdapter
from .hermes import HermesAdapter

__all__ = [
    "AntigravityAdapter",
    "CodexAdapter",
    "CommandResult",
    "ConfigureResult",
    "Detection",
    "HermesAdapter",
]
