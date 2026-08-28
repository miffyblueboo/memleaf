"""Discover a small, directly callable model route from supported hosts.

The discovery layer is deliberately separate from the processing pipeline.  It
only answers one question: which configured chat model can the standalone
memleaf process call?  It does not select candidates, gate memories, or
summarize a turn.

Only standard-library parsing and host CLIs are used here.  In particular, no
command found in a configuration file is ever executed.  Secrets are kept in
the private :class:`ModelCandidate` value and are omitted from all public
diagnostics and representations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import tomllib
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from .adapters.base import CommandRunner, adapter_environment, adapter_home, run_argv
from .adapters.hermes import HermesAdapter
from .config import DEFAULT_REQUEST_TIMEOUT, load_config, save_config
from .credentials import credential_text, is_redacted_credential


_DEFAULT_CONTEXT_WINDOW = 200_000

# Lower values win.  Keep this ordering explicit and stable: it is part of the
# installation behaviour, not a provider-specific heuristic hidden in a call.
_LIGHT_TOKENS: tuple[tuple[str, int], ...] = (
    ("nano", 0),
    ("micro", 1),
    ("tiny", 2),
    ("mini", 3),
    ("lite", 4),
    ("flash", 5),
    ("haiku", 6),
    ("small", 7),
    ("fast", 8),
)
_HEAVY_TOKENS = frozenset(
    {"max", "opus", "pro", "large", "ultra", "reasoning"}
)
_NON_CHAT_TOKENS = frozenset(
    {
        "embedding",
        "embeddings",
        "embed",
        "tts",
        "image",
        "images",
        "audio",
        "speech",
        "transcription",
        "transcribe",
        "rerank",
        "moderation",
    }
)
_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_REFERENCE_RE = re.compile(r"^(?:\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*))$")
_HERMES_ENV_BY_PROVIDER = {
    "anthropic": "ANTHROPIC_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "google": "GEMINI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "minimax": "MINIMAX_API_KEY",
}


@dataclass(frozen=True)
class ModelCandidate:
    """One complete route that memleaf can call without host callbacks."""

    source: str
    provider: str
    protocol: str
    base_url: str
    model: str
    # ``repr=False`` is an additional guard against accidental secret logging.
    api_key: str = field(repr=False)
    context_window: int = _DEFAULT_CONTEXT_WINDOW
    source_detail: str = ""

    def __post_init__(self) -> None:
        for name in ("source", "provider", "protocol", "base_url", "model", "api_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"model candidate {name} is required")
        if self.protocol not in {"openai", "claude", "gemini"}:
            raise ValueError("unsupported model candidate protocol")
        if type(self.context_window) is not int or self.context_window <= 0:
            raise ValueError("invalid model candidate context window")

    def public_dict(self) -> dict[str, Any]:
        """Return diagnostics-safe fields; never return the API key."""

        return {
            "source": self.source,
            "provider": self.provider,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "model": self.model,
            "context_window": self.context_window,
            "source_detail": self.source_detail,
        }

    to_dict = public_dict

    def config_dict(self) -> dict[str, Any]:
        """Return the llm block for ``~/.memleaf/config.yaml``."""

        return {
            "mode": "api",
            "provider": self.provider,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "api_key": self.api_key,
            # Keep this empty for old readers that know the field.  New routes
            # are intentionally independent of environment-variable names.
            "api_key_env": "",
            "model": self.model,
            "context_window": self.context_window,
            "request_timeout": DEFAULT_REQUEST_TIMEOUT,
            "diagnostic_logging": False,
        }


@dataclass(frozen=True)
class DiscoveryResult:
    """Candidates, selected route, and non-secret diagnostics."""

    candidates: tuple[ModelCandidate, ...] = ()
    selected: ModelCandidate | None = None
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected.public_dict() if self.selected else None,
            "candidates": [item.public_dict() for item in self.candidates],
            "diagnostics": list(self.diagnostics),
        }


def _safe_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _configured_directory(home: Path, value: Any, default_name: str) -> Path:
    configured = _safe_text(value)
    if configured is None:
        return (home / default_name).resolve()
    if configured == "~":
        return home.resolve()
    if configured.startswith("~/"):
        return (home / configured[2:]).resolve()
    path = Path(configured)
    return (path if path.is_absolute() else home / path).resolve()


def _valid_base_url(value: Any) -> str | None:
    text = _safe_text(value)
    if text is None:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    # Credentials in an URL are both unsafe and impossible to diagnose safely.
    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.query or parsed.fragment:
        return None
    return text.rstrip("/")


def _tokens(model: str) -> tuple[str, ...]:
    return tuple(item.casefold() for item in _TOKEN_RE.findall(model))


def is_chat_model(model: str) -> bool:
    """Return whether a model name is plausibly a text-chat model."""

    return not bool(_NON_CHAT_TOKENS.intersection(_tokens(model)))


def lightness_key(candidate: ModelCandidate) -> tuple[int, int, int, str, str, str]:
    """Stable ranking key used by installation and exposed for tests."""

    words = _tokens(candidate.model)
    light_rank = min((rank for token, rank in _LIGHT_TOKENS if token in words), default=50)
    heavy_count = sum(1 for token in words if token in _HEAVY_TOKENS)
    # A named heavy model must not beat an unqualified light/default model.
    score = light_rank + (100 * heavy_count)
    source_rank = {
        "hermes": 0,
        "hermes_custom": 1,
        "codex": 2,
        "antigravity": 3,
        "manual": 4,
    }.get(candidate.source, 5)
    return (
        score,
        heavy_count,
        source_rank,
        candidate.model.casefold(),
        candidate.provider.casefold(),
        candidate.base_url.casefold(),
    )


def select_lightest(candidates: Iterable[ModelCandidate]) -> ModelCandidate | None:
    usable = [item for item in candidates if is_chat_model(item.model)]
    return min(usable, key=lightness_key) if usable else None


def _context_window(value: Any) -> int:
    if isinstance(value, bool):
        return _DEFAULT_CONTEXT_WINDOW
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_CONTEXT_WINDOW
    return parsed if parsed > 0 else _DEFAULT_CONTEXT_WINDOW


def _protocol(provider: Any, value: Any = None) -> str | None:
    explicit = (_safe_text(value) or "").casefold().replace("-", "_")
    if explicit in {"anthropic", "anthropic_messages", "claude"}:
        return "claude"
    if explicit in {"gemini", "google", "generate_content"}:
        return "gemini"
    if explicit in {"openai", "openai_compatible", "chat_completions", "chat"}:
        return "openai"
    name = (_safe_text(provider) or "").casefold()
    if "anthropic" in name or "claude" in name:
        return "claude"
    if "gemini" in name or "google" in name:
        return "gemini"
    if name:
        return "openai"
    return None


def _models_from(value: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """Extract model ids and optional per-model mappings from host config."""

    result: list[tuple[str, Mapping[str, Any]]] = []
    # ``name`` is commonly the provider label in Hermes custom-provider
    # entries, so it must not be mistaken for a callable model id.
    for key in ("default", "model", "model_name", "id"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            result.append((item.strip(), value))
    models = value.get("models")
    if isinstance(models, Mapping):
        for model_id, model_value in models.items():
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            result.append((model_id.strip(), model_value if isinstance(model_value, Mapping) else value))
    elif isinstance(models, list):
        for item in models:
            if isinstance(item, str) and item.strip():
                result.append((item.strip(), value))
            elif isinstance(item, Mapping):
                model_id = _safe_text(item.get("id")) or _safe_text(item.get("name"))
                if model_id:
                    result.append((model_id, item))
    # A custom provider may use ``model`` as a mapping of aliases.
    model_map = value.get("model")
    if isinstance(model_map, Mapping):
        for model_id, model_value in model_map.items():
            if isinstance(model_id, str) and model_id.strip():
                result.append((model_id.strip(), model_value if isinstance(model_value, Mapping) else value))
    seen: set[str] = set()
    unique: list[tuple[str, Mapping[str, Any]]] = []
    for model_id, model_value in result:
        if model_id.casefold() not in seen:
            seen.add(model_id.casefold())
            unique.append((model_id, model_value))
    return unique


def _env_reference(value: Any) -> str | None:
    text = _safe_text(value)
    if text is None:
        return None
    match = _ENV_REFERENCE_RE.fullmatch(text)
    return (match.group(1) or match.group(2)) if match else None


def _dotenv_value(path: Path, name: str) -> str | None:
    if not _ENV_NAME_RE.fullmatch(name) or path.is_symlink() or not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        if key.strip() != name:
            continue
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        # A dotenv file may contain an intentionally empty declaration before
        # the real value (Hermes' generated file can do this).  Keep looking
        # instead of treating the empty declaration as authoritative.
        usable = credential_text(value)
        if usable:
            return usable
    return None


def _candidate_key(
    item: Mapping[str, Any],
    *,
    provider: str,
    env: Mapping[str, str],
    dotenv: Path | None = None,
    extra_env: Mapping[str, Any] | None = None,
) -> tuple[str | None, str]:
    saw_redacted = False
    for key_name in ("api_key", "apiKey", "token", "bearer_token", "experimental_bearer_token"):
        raw = item.get(key_name)
        reference = _env_reference(raw)
        if reference:
            value = credential_text(env.get(reference))
            if not value and extra_env:
                value = credential_text(extra_env.get(reference))
            if not value and dotenv:
                value = credential_text(_dotenv_value(dotenv, reference))
            if value:
                return value, "environment"
        else:
            direct = credential_text(raw)
            if direct:
                return direct, "direct"
            saw_redacted = saw_redacted or is_redacted_credential(raw)

    env_name = None
    for key_name in ("api_key_env", "apiKeyEnv", "env_key", "envKey", "key_env", "token_env"):
        env_name = _safe_text(item.get(key_name))
        if env_name:
            break
    if env_name is None:
        provider_key = provider.casefold().replace("_", "-")
        env_name = _HERMES_ENV_BY_PROVIDER.get(provider_key)
        if env_name is None:
            env_name = _HERMES_ENV_BY_PROVIDER.get(provider_key.split("-", 1)[0])
        if env_name is None and provider:
            guessed = re.sub(r"[^A-Za-z0-9]", "_", provider).upper() + "_API_KEY"
            if _ENV_NAME_RE.fullmatch(guessed):
                env_name = guessed
    if env_name is None or not _ENV_NAME_RE.fullmatch(env_name):
        return None, "missing"
    value = credential_text(env.get(env_name))
    if not value and extra_env:
        value = credential_text(extra_env.get(env_name))
    if not value and dotenv:
        value = credential_text(_dotenv_value(dotenv, env_name))
    if value:
        return value, "environment"
    return None, "redacted" if saw_redacted else "missing"


def _candidate(
    *,
    source: str,
    provider: Any,
    protocol: Any,
    base_url: Any,
    model: Any,
    item: Mapping[str, Any],
    env: Mapping[str, str],
    dotenv: Path | None = None,
    extra_env: Mapping[str, Any] | None = None,
    source_detail: str = "",
) -> tuple[ModelCandidate | None, str | None]:
    provider_text = _safe_text(provider)
    model_text = _safe_text(model)
    route_protocol = _protocol(provider_text, protocol)
    url = _valid_base_url(base_url)
    if provider_text is None or model_text is None or route_protocol is None:
        return None, "missing provider, base URL, protocol, or model"
    if not is_chat_model(model_text):
        return None, "non-chat model"
    if url is None:
        return None, "invalid or missing base URL"
    key, key_source = _candidate_key(item, provider=provider_text, env=env, dotenv=dotenv, extra_env=extra_env)
    if key is None:
        return None, (
            "API credential was redacted and no usable environment credential was found"
            if key_source == "redacted"
            else "missing API credential"
        )
    try:
        built = ModelCandidate(
            source=source,
            provider=provider_text,
            protocol=route_protocol,
            base_url=url,
            model=model_text,
            api_key=key,
            context_window=_context_window(
                item.get("context_window", item.get("context_length", item.get("max_context")))
            ),
            source_detail=source_detail or key_source,
        )
    except ValueError:
        return None, "invalid model route"
    return built, None


def _json_value(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _hermes_json(
    executable: str,
    key: str,
    *,
    env: Mapping[str, str],
    runner: CommandRunner | None,
) -> tuple[Any, bool]:
    result = run_argv(runner, [executable, "config", "get", key, "--json"], env=env)
    if result.returncode != 0:
        return None, False
    parsed = _json_value(result.stdout)
    return parsed, parsed is not None


def _hermes_env_path(
    executable: str,
    *,
    hermes_home: Path,
    env: Mapping[str, str],
    runner: CommandRunner | None,
) -> Path | None:
    result = run_argv(runner, [executable, "config", "env-path"], env=env)
    raw = result.stdout.strip() if result.returncode == 0 else ""
    if not raw:
        candidate = hermes_home / ".env"
    else:
        # The CLI is authoritative for this path; only a single plain line is
        # accepted, so command/config text cannot be interpreted as a path.
        candidate = Path(raw.splitlines()[-1].strip().strip('"\''))
        if not candidate.is_absolute():
            candidate = hermes_home / candidate
    if candidate.is_symlink():
        return None
    try:
        candidate = candidate.resolve()
        candidate.relative_to(hermes_home)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() and not candidate.is_symlink() else None


def discover_hermes(
    *,
    home: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    executable: str | None = None,
    runner: CommandRunner | None = None,
) -> tuple[list[ModelCandidate], list[str]]:
    """Discover Hermes models through its read-only JSON config CLI."""

    effective_home = adapter_home(home)
    environment = adapter_environment(env)
    adapter = HermesAdapter(home=effective_home, env=environment)
    hermes_home = adapter.hermes_home
    command = executable
    if command is None:
        detection = adapter.detect()
        command = detection.executable if detection.confidence == "high" else None
    if command is None:
        return [], ["hermes: executable not found"]
    dotenv = _hermes_env_path(command, hermes_home=hermes_home, env=environment, runner=runner)
    model_value, model_ok = _hermes_json(command, "model", env=environment, runner=runner)
    custom_value, custom_ok = _hermes_json(command, "custom_providers", env=environment, runner=runner)
    if not model_ok and not custom_ok:
        return [], ["hermes: model configuration could not be read through the CLI"]

    model_config = model_value.get("model") if isinstance(model_value, Mapping) and isinstance(model_value.get("model"), Mapping) else model_value
    if not isinstance(model_config, Mapping):
        model_config = {}
    candidates: list[ModelCandidate] = []
    diagnostics: list[str] = []
    for model_id, model_item in _models_from(model_config):
        provider = model_item.get("provider", model_config.get("provider"))
        base_url = model_item.get("base_url", model_config.get("base_url"))
        protocol = model_item.get("protocol", model_item.get("api_mode", model_config.get("api_mode")))
        item = dict(model_config)
        item.update(model_item)
        found, reason = _candidate(
            source="hermes",
            provider=provider,
            protocol=protocol,
            base_url=base_url,
            model=model_id,
            item=item,
            env=environment,
            dotenv=dotenv,
            source_detail="current model",
        )
        if found:
            candidates.append(found)
        elif reason != "non-chat model":
            diagnostics.append(f"hermes: current model route unavailable ({reason})")

    custom = custom_value.get("custom_providers") if isinstance(custom_value, Mapping) and isinstance(custom_value.get("custom_providers"), (Mapping, list)) else custom_value
    if isinstance(custom, Mapping):
        custom_items = list(custom.items())
    elif isinstance(custom, list):
        custom_items = []
        for index, raw_item in enumerate(custom):
            if isinstance(raw_item, Mapping):
                custom_items.append((str(raw_item.get("name", index)), raw_item))
    else:
        custom_items = []
    for custom_name, raw_item in custom_items:
        if not isinstance(custom_name, str) or not isinstance(raw_item, Mapping):
            continue
        provider = raw_item.get("provider", raw_item.get("name", custom_name))
        for model_id, model_item in _models_from(raw_item):
            item = dict(raw_item)
            item.update(model_item)
            found, reason = _candidate(
                source="hermes_custom",
                provider=provider,
                protocol=item.get("protocol", item.get("api_mode")),
                base_url=item.get("base_url", item.get("endpoint")),
                model=model_id,
                item=item,
                env=environment,
                dotenv=dotenv,
                source_detail=f"custom provider {custom_name}",
            )
            if found:
                candidates.append(found)
            elif reason != "non-chat model":
                diagnostics.append(f"hermes: custom provider {custom_name} unavailable ({reason})")
    return candidates, diagnostics


def _codex_env_values(document: Mapping[str, Any]) -> Mapping[str, Any]:
    policy = document.get("shell_environment_policy")
    if not isinstance(policy, Mapping):
        return {}
    values = policy.get("set")
    return values if isinstance(values, Mapping) else {}


def discover_codex(
    *,
    home: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    config_path: Path | str | None = None,
) -> tuple[list[ModelCandidate], list[str]]:
    """Discover only Codex providers with a chat-completions route.

    Codex's documented ``responses`` wire API is not silently treated as
    OpenAI chat completions.  Until memleaf has a tested Responses backend,
    those routes are reported as unsupported and skipped.
    """

    effective_home = adapter_home(home)
    environment = adapter_environment(env)
    if config_path is not None:
        path = Path(config_path).expanduser()
    else:
        codex_home = _configured_directory(effective_home, environment.get("CODEX_HOME"), ".codex")
        path = codex_home / "config.toml"
    if path.is_symlink() or not path.is_file():
        return [], ["codex: user config.toml was not found"]
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return [], ["codex: config.toml could not be parsed"]
    providers = document.get("model_providers")
    if not isinstance(providers, Mapping):
        return [], ["codex: no reusable model provider configuration found"]
    selected_name = _safe_text(document.get("model_provider"))
    top_model = _safe_text(document.get("model"))
    shell_values = _codex_env_values(document)
    candidates: list[ModelCandidate] = []
    diagnostics: list[str] = []
    for name, raw_item in providers.items():
        if not isinstance(name, str) or not isinstance(raw_item, Mapping):
            continue
        wire_api = (_safe_text(raw_item.get("wire_api")) or "").casefold().replace("-", "_")
        if wire_api != "chat_completions":
            if wire_api == "responses":
                diagnostics.append(f"codex: provider {name} uses unsupported responses wire API")
            else:
                diagnostics.append(f"codex: provider {name} has no supported chat wire API")
            continue
        provider = _safe_text(raw_item.get("name")) or name
        model_ids: list[str] = []
        for key in ("model", "default_model"):
            value = _safe_text(raw_item.get(key))
            if value:
                model_ids.append(value)
        if selected_name == name and top_model:
            model_ids.append(top_model)
        if isinstance(raw_item.get("models"), list):
            model_ids.extend(item for item in raw_item["models"] if isinstance(item, str) and item.strip())
        if not model_ids:
            diagnostics.append(f"codex: provider {name} has no model name")
            continue
        seen: set[str] = set()
        for model_id in model_ids:
            if model_id.casefold() in seen:
                continue
            seen.add(model_id.casefold())
            item = dict(raw_item)
            found, reason = _candidate(
                source="codex",
                provider=provider,
                protocol="openai",
                base_url=raw_item.get("base_url"),
                model=model_id,
                item=item,
                env=environment,
                extra_env=shell_values,
                source_detail="selected provider" if selected_name == name else "model provider",
            )
            if found:
                candidates.append(found)
            elif reason != "non-chat model":
                diagnostics.append(f"codex: provider {name} unavailable ({reason})")
    if top_model and not selected_name:
        diagnostics.append("codex: top-level model has no selected reusable provider")
    return candidates, diagnostics


def discover_models(
    *,
    home: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    hermes_executable: str | None = None,
    runner: CommandRunner | None = None,
    include_codex: bool = False,
    include_hermes: bool = True,
) -> DiscoveryResult:
    """Discover and rank complete routes from supported local hosts."""

    candidates: list[ModelCandidate] = []
    diagnostics: list[str] = []
    if include_hermes:
        found, messages = discover_hermes(
            home=home,
            env=env,
            executable=hermes_executable,
            runner=runner,
        )
        candidates.extend(found)
        diagnostics.extend(messages)
    if include_codex:
        found, messages = discover_codex(home=home, env=env)
        candidates.extend(found)
        diagnostics.extend(messages)
    selected = select_lightest(candidates)
    if selected is None:
        diagnostics.append("no complete callable chat model route discovered")
    # Preserve discovery order in the candidate list, but make the selected
    # model deterministic through select_lightest/lightness_key.
    return DiscoveryResult(tuple(candidates), selected, tuple(diagnostics))


def write_model_config(
    config_path: Path | str,
    candidate: ModelCandidate,
    *,
    vault: Path | str | None = None,
) -> dict[str, Any]:
    """Atomically write a discovered route with direct API key storage."""

    path = Path(config_path).expanduser()
    if path.is_symlink():
        raise ValueError("refusing to write symlinked memleaf config")
    current = load_config(path, vault=vault)
    current["llm"] = candidate.config_dict()
    save_config(path, current)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return candidate.public_dict()


def manual_candidate(
    *,
    provider: str,
    protocol: str,
    base_url: str,
    model: str,
    api_key: str,
    context_window: int = _DEFAULT_CONTEXT_WINDOW,
) -> ModelCandidate:
    """Validate an interactive fallback route without emitting its key."""

    candidate, reason = _candidate(
        source="manual",
        provider=provider,
        protocol=protocol,
        base_url=base_url,
        model=model,
        item={"api_key": api_key, "context_window": context_window},
        env={},
        source_detail="user supplied",
    )
    if candidate is None:
        raise ValueError(reason or "invalid model route")
    return candidate


# Friendly aliases for callers that use the shorter terminology.
discover = discover_models
choose_lightest = select_lightest
save_model_config = write_model_config


__all__ = [
    "DiscoveryResult",
    "ModelCandidate",
    "choose_lightest",
    "discover",
    "discover_codex",
    "discover_hermes",
    "discover_models",
    "is_chat_model",
    "lightness_key",
    "manual_candidate",
    "save_model_config",
    "select_lightest",
    "write_model_config",
]
