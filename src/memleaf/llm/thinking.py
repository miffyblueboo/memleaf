"""Provider-neutral thinking policy and protocol capability mapping."""

from __future__ import annotations

import re
from typing import Any, Mapping


THINKING_PURPOSES = frozenset({"gate", "summarize", "compact"})
THINKING_MODES = frozenset({"default", "disabled", "low", "high", "max"})
THINKING_EFFECTIVE_MODES = frozenset(
    {"provider_default", "unsupported", "disabled", "minimal", "low", "high", "max"}
)
THINKING_CONTROLS = frozenset(
    {
        "provider_default",
        "unsupported",
        "openai_reasoning_effort",
        "deepseek_thinking_effort",
        "anthropic_effort",
        "anthropic_adaptive_effort",
        "gemini_thinking_level",
        "gemini_thinking_budget",
    }
)


def requested_thinking_mode(settings: Any, purpose: str) -> str:
    """Return memleaf's requested stage policy, defaulting every model stage to low."""

    if purpose not in THINKING_PURPOSES:
        return "default"
    value = settings.get(purpose, "low") if isinstance(settings, Mapping) else "low"
    return value if isinstance(value, str) and value in THINKING_MODES else "low"


def thinking_metrics(requested: str, effective: str, control: str) -> dict[str, str]:
    requested = requested if requested in THINKING_MODES else "low"
    effective = effective if effective in THINKING_EFFECTIVE_MODES else "unsupported"
    control = control if control in THINKING_CONTROLS else "unsupported"
    return {
        "thinking_mode": requested,
        "thinking_effective": effective,
        "thinking_control": control,
    }


def _model_leaf(model: Any) -> str:
    if not isinstance(model, str):
        return ""
    return model.casefold().strip().replace("_", "-").rsplit("/", 1)[-1]


def _openai_reasoning_model(model: Any) -> bool:
    leaf = _model_leaf(model)
    return leaf.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4", "gpt-oss"))


def _openai_none_supported(model: Any) -> bool:
    leaf = _model_leaf(model)
    return leaf.startswith(("gpt-5.1", "gpt-5.2", "gpt-5.3", "gpt-5.4", "gpt-5.5", "gpt-5.6"))


def _openai_max_supported(model: Any) -> bool:
    leaf = _model_leaf(model)
    return leaf.startswith(("gpt-6", "gpt-5.6"))


def openai_chat_controls(
    provider_name: Any,
    model: Any,
    requested: str,
) -> tuple[dict[str, Any], dict[str, str], bool]:
    """Map one policy request to an OpenAI-format Chat Completions payload.

    The final bool says that sampling temperature must be omitted because the
    selected reasoning mode/model does not accept or use it safely.
    """

    provider = provider_name.casefold().strip() if isinstance(provider_name, str) else ""
    if requested == "default":
        return {}, thinking_metrics(requested, "provider_default", "provider_default"), False

    if provider == "deepseek":
        if requested == "disabled":
            return (
                {"thinking": {"type": "disabled"}},
                thinking_metrics(requested, "disabled", "deepseek_thinking_effort"),
                False,
            )
        effort = requested if requested in {"low", "high", "max"} else "low"
        return (
            {"thinking": {"type": "enabled"}, "reasoning_effort": effort},
            thinking_metrics(requested, effort, "deepseek_thinking_effort"),
            True,
        )

    # Unknown OpenAI-compatible services are deliberately not assumed to
    # accept reasoning_effort merely because they implement Chat Completions.
    if "openai" not in provider or not _openai_reasoning_model(model):
        return {}, thinking_metrics(requested, "unsupported", "unsupported"), False

    if requested == "disabled":
        if not _openai_none_supported(model):
            return {}, thinking_metrics(requested, "unsupported", "unsupported"), True
        return (
            {"reasoning_effort": "none"},
            thinking_metrics(requested, "disabled", "openai_reasoning_effort"),
            True,
        )
    if requested == "max" and not _openai_max_supported(model):
        return (
            {"reasoning_effort": "high"},
            thinking_metrics(requested, "high", "openai_reasoning_effort"),
            True,
        )
    effort = requested if requested in {"low", "high", "max"} else "low"
    return (
        {"reasoning_effort": effort},
        thinking_metrics(requested, effort, "openai_reasoning_effort"),
        True,
    )


def _claude_profile(model: Any) -> str:
    """Return only capabilities documented for a recognizable Claude family."""

    leaf = _model_leaf(model)
    if not leaf.startswith("claude-"):
        return "unsupported"
    if any(name in leaf for name in ("fable-5", "mythos-5", "mythos-preview")):
        return "always_adaptive"
    if re.match(r"claude-(?:opus|sonnet)-5(?:-|$)", leaf):
        return "default_adaptive"
    if re.match(r"claude-(?:opus|sonnet)-4-(?:6|7|8|9)(?:-|$)", leaf):
        return "explicit_adaptive"
    if re.match(r"claude-opus-4-5(?:-|$)", leaf):
        return "effort_only"
    return "unsupported"


def claude_messages_controls(
    model: Any,
    requested: str,
) -> tuple[dict[str, Any], dict[str, str], bool]:
    """Map policy effort to current Claude Messages capabilities."""

    profile = _claude_profile(model)
    if profile == "unsupported":
        return {}, thinking_metrics(requested, "unsupported", "unsupported"), False

    # Claude 4.7+ and adaptive-thinking requests require the default sampling
    # temperature. Opus 4.5 can use effort without enabling extended thinking.
    omit_temperature = profile != "effort_only"
    if requested == "default":
        return {}, thinking_metrics(requested, "provider_default", "provider_default"), omit_temperature

    if requested == "disabled":
        if profile == "always_adaptive":
            return (
                {"output_config": {"effort": "low"}},
                thinking_metrics(requested, "low", "anthropic_effort"),
                omit_temperature,
            )
        if profile == "effort_only":
            return (
                {"output_config": {"effort": "low"}},
                thinking_metrics(requested, "disabled", "anthropic_effort"),
                omit_temperature,
            )
        return (
            {"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}},
            thinking_metrics(requested, "disabled", "anthropic_effort"),
            omit_temperature,
        )

    effort = requested if requested in {"low", "high", "max"} else "low"
    if profile in {"explicit_adaptive", "effort_only"} and effort == "max":
        # Low is the default requested path. For legacy effort-capable families,
        # avoid sending a higher enum unless the current family documents it.
        effort = "high"
    payload: dict[str, Any] = {"output_config": {"effort": effort}}
    control = "anthropic_effort"
    if profile == "explicit_adaptive":
        payload["thinking"] = {"type": "adaptive"}
        control = "anthropic_adaptive_effort"
    return payload, thinking_metrics(requested, effort, control), omit_temperature


def _gemini_version(model: Any) -> tuple[int, int] | None:
    leaf = _model_leaf(model)
    match = re.match(r"gemini-(\d+)(?:\.(\d+))?", leaf)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2) or 0)


def _gemini3_low_level(model: Any) -> tuple[str, str]:
    """Return the lowest safe Gemini 3.x level for a recognized exception."""

    leaf = _model_leaf(model)
    # Gemini 3.1 Flash-Lite Image documents minimal/high but not low. Minimal
    # is lower than the requested low policy and therefore the safe latency
    # preserving fallback instead of an invalid low value.
    if leaf.startswith("gemini-3.1-flash-lite-image"):
        return "minimal", "minimal"
    return "low", "low"


def gemini_generate_controls(
    model: Any,
    requested: str,
) -> tuple[dict[str, Any], dict[str, str], bool]:
    """Map policy effort to native Gemini generateContent thinkingConfig.

    The final bool asks the adapter to omit explicit temperature for Gemini 3.x,
    following the current API guidance for thinking models.
    """

    version = _gemini_version(model)
    if requested == "default":
        omit_temperature = bool(version and version[0] >= 3)
        return {}, thinking_metrics(requested, "provider_default", "provider_default"), omit_temperature
    if version is None:
        return {}, thinking_metrics(requested, "unsupported", "unsupported"), False

    major, minor = version
    leaf = _model_leaf(model)
    if major >= 3:
        if requested in {"disabled", "low"}:
            level, effective = _gemini3_low_level(model)
        else:
            level, effective = "high", "high"
        return (
            {"thinkingConfig": {"thinkingLevel": level}},
            thinking_metrics(requested, effective, "gemini_thinking_level"),
            True,
        )

    if major == 2 and minor == 5:
        if requested == "disabled":
            if "pro" in leaf:
                budget = 1024
                effective = "low"
            else:
                budget = 0
                effective = "disabled"
        elif requested == "low":
            budget = 1024
            effective = "low"
        else:
            budget = 24576
            effective = "high"
        return (
            {"thinkingConfig": {"thinkingBudget": budget}},
            thinking_metrics(requested, effective, "gemini_thinking_budget"),
            False,
        )
    return {}, thinking_metrics(requested, "unsupported", "unsupported"), False
