"""Public Hermes MemoryProvider plugin surface.

Implementation is split by responsibility so this package entry point remains reviewable.
The fallback loader preserves Hermes' standalone plugin-directory loading behavior.
"""
from __future__ import annotations

try:
    from . import _provider as _impl
except (ImportError, ValueError):
    import importlib.util as _importlib_util
    import sys as _sys
    from pathlib import Path as _Path

    _name = "_memleaf_hermes_provider_impl"
    _impl = _sys.modules.get(_name)
    if _impl is None:
        _spec = _importlib_util.spec_from_file_location(_name, _Path(__file__).with_name("_provider.py"))
        if _spec is None or _spec.loader is None:
            raise ImportError("Hermes provider implementation is unavailable")
        _impl = _importlib_util.module_from_spec(_spec)
        _sys.modules[_name] = _impl
        _spec.loader.exec_module(_impl)

MemleafMemoryProvider = _impl.MemleafMemoryProvider
register = _impl.register

__all__ = ["MemleafMemoryProvider", "register"]

def __getattr__(name: str):
    return getattr(_impl, name)

def __dir__():
    return sorted(set(globals()) | set(dir(_impl)))
