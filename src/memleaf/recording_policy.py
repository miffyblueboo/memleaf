"""Explicit recording controls, evaluated before capture or tool persistence.

This is a small command recognizer, not a semantic memory extractor. It only
recognizes direct, leading user controls; examples, quoted text and tool output
cannot change permissions. Hosts can always use the existing record=False API
for other language forms. Policy stores identifiers, never the private text.
"""
from __future__ import annotations
import re
from typing import Any, Mapping

_RESUME = re.compile(r"^(?:请\s*)?(?:恢复|重新开启|开启|继续)(?:自动)?(?:记录|记忆)(?:[。！!，,:：\s]|$)|"
                     r"^(?:please\s+)?(?:resume|enable|turn on)\s+(?:memleaf\s+)?(?:recording|memory)\b", re.I)
_SESSION = re.compile(r"^(?:请\s*)?(?:接下来|本次会话|这个会话|后面的对话|从现在起)[，,：:\s]*(?:不要|别|不)(?:再)?(?:记录|记住|保存)|"
                      r"^(?:请\s*)?(?:不要|别)(?:记录|记住|保存)(?:接下来|本次会话|这个会话|后面的对话)|"
                      r"^(?:please\s+)?(?:do not|don't|stop)\s+(?:record|remember|store|save|recording)\s+"
                      r"(?:anything|this session|from now on|the rest of this session)\b", re.I)
_TURN = re.compile(r"^(?:请\s*)?(?:(?:这个|这段|这条|这次|本轮|当前)(?:内容|对话|消息)?[，,：:\s]*(?:不要|别|不)(?:被)?(?:记录|记住|保存)|"
                   r"(?:不要|别)(?:记录|记住|保存)(?:这段|这条|这次|以下|下面|本轮|当前))|"
                   r"^(?:please\s+)?(?:do not|don't)\s+(?:record|remember|store|save)\s+(?:this|the following|what follows)\b", re.I)


def _policy(processed: Mapping[str, Any], source: str, session_id: str) -> Mapping[str, Any]:
    state = processed.get('sessions', {}).get(f'{source}/{session_id}', {})
    value = state.get('capture_policy', {}) if isinstance(state, Mapping) else {}
    if (not isinstance(value, Mapping) or type(value.get('enabled', True)) is not bool
        or not isinstance(value.get('private_turns', []), list)
        or not isinstance(value.get('applied_controls', []), list)):
        raise ValueError('invalid recording policy; capture refused')
    return value


def recording_allowed(processed: Mapping[str, Any], source: str, session_id: str, turn_key: str) -> bool:
    value = _policy(processed, source, session_id)
    return value.get('enabled', True) and turn_key not in value.get('private_turns', [])


def apply_control(processed: dict[str, Any], *, source: str, session_id: str,
                  turn_key: str, event_key: str, role: str, content: str, record: bool = True) -> tuple[bool, bool]:
    """Return (allowed, policy_changed). Caller owns the Vault lock."""
    value = dict(_policy(processed, source, session_id))
    changed = False
    if not record and turn_key not in value.get('private_turns', []):
        value['private_turns'] = [*value.get('private_turns', []), turn_key]
        changed = True
    if record and role == 'user' and event_key not in value.get('applied_controls', []):
        text = content.lstrip()
        mode = ('on' if _RESUME.search(text) else 'off' if _SESSION.search(text)
                else 'private' if _TURN.search(text) else None)
        if mode:
            value['applied_controls'] = [*value.get('applied_controls', []), event_key]
            if mode in {'on', 'off'}:
                value['enabled'] = mode == 'on'
            if mode != 'on':
                value['private_turns'] = sorted(set(value.get('private_turns', [])) | {turn_key})
            changed = True
    if not value.get('enabled', True) and role == 'user' and turn_key not in value.get('private_turns', []):
        value['private_turns'] = [*value.get('private_turns', []), turn_key]
        changed = True
    if changed:
        state = processed.setdefault('sessions', {}).setdefault(f'{source}/{session_id}', {})
        state['capture_policy'] = value
    return recording_allowed(processed, source, session_id, turn_key), changed
