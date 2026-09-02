"""Local-first Markdown memory core for AI agents."""

__version__ = "0.2.14"

from .config import DEFAULT_CONFIG, default_config, load_config, save_config
from .frontmatter import FrontmatterError, dump_frontmatter, dump_yaml, load_yaml, parse_frontmatter
from .models import CaptureResult, ForgetAboutResult, Memory
from .redaction import redact_secrets, redact_text
from .retrieval import RetrievalError
from .service import Core, Memleaf, MemoryService
from .vault import Vault, safe_component

__all__ = [
    "__version__",
    "CaptureResult",
    "Core",
    "DEFAULT_CONFIG",
    "ForgetAboutResult",
    "FrontmatterError",
    "Memleaf",
    "Memory",
    "MemoryService",
    "RetrievalError",
    "Vault",
    "default_config",
    "dump_frontmatter",
    "dump_yaml",
    "load_config",
    "load_yaml",
    "parse_frontmatter",
    "redact_secrets",
    "redact_text",
    "safe_component",
    "save_config",
]
