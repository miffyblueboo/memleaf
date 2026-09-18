"""Read-only validation for the unmerged memory_updates/v1 candidate format.

Only exact frozen requests may resume through the current shared writer.
This module never creates, changes, deletes or replays a journal.
"""
from __future__ import annotations

from copy import deepcopy

import hashlib

import json

from pathlib import Path

import re

from typing import Any, Mapping

from .incremental_dates import parse_source_time, selected_calendar

from .incremental_scopes import registry_view





from .models import Memory, MemoryVersionError, utc_now

from .query_scan import ensure_scan_current

from .scope_state import normalize_scopes

from .turn_plan import MAX_PLAN_BYTES, revision_digest

from .validation import parse_strict_json

from .vault import safe_component

_FIELDS = frozenset({'title', 'body', 'tags', 'aliases', 'keywords', 'scopes',
                     'status', 'completed_at', 'assignee', 'waiting_on', 'deadline', 'validity'})

_GROUP = {'title': 'content', 'body': 'content', 'status': 'status',
          'completed_at': 'status', 'assignee': 'responsibility',
          'waiting_on': 'responsibility', 'scopes': 'scope',
          'deadline': 'deadline', 'validity': 'validity'}

_RECEIPTS = ('explicit_update_operation_id', 'explicit_update_result_digest',
             'retraction_operation_id', 'retraction_result_digest')

def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def _text(value: Any, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\0' in value:
        raise ValueError('invalid_update_text')

def normalize_request(patch: Mapping[str, Any], *, reopen: bool, source_time: str | None) -> dict:
    """Validate caller-owned values before any journal or business write."""
    if not isinstance(patch, Mapping) or not patch or set(patch) - _FIELDS:
        raise ValueError('invalid_update_fields')
    if type(reopen) is not bool:
        raise ValueError('invalid_reopen')
    parse_source_time(source_time)  # Unknown stays unknown, never replaced by now.
    fields = deepcopy(dict(patch))
    for key in ('title', 'body'):
        if key in fields:
            _text(fields[key], 256 if key == 'title' else 16384)
    for key in ('tags', 'aliases', 'keywords'):
        if key in fields:
            value = fields[key]
            if not isinstance(value, list) or len(value) > 128:
                raise ValueError('invalid_update_list')
            for item in value:
                _text(item, 256)
    if 'scopes' in fields:
        if not isinstance(fields['scopes'], list) or len(fields['scopes']) > 32:
            raise ValueError('invalid_update_scopes')
        fields['scopes'] = normalize_scopes(fields['scopes'])
    if 'status' in fields and fields['status'] not in ('active', 'completed', 'cancelled'):
        raise ValueError('invalid_status')
    if 'validity' in fields and fields['validity'] != 'valid':
        raise ValueError('use_retract_memory_for_retraction')
    for key in ('assignee', 'waiting_on'):
        if key in fields and fields[key] is not None:
            _text(fields[key], 512)
    if fields.get('completed_at') is not None:
        parse_source_time(fields['completed_at'])
    if 'deadline' in fields:
        value = fields['deadline']
        if not isinstance(value, dict):
            raise ValueError('invalid_update_deadline')
        if set(value) == {'clear'} and value['clear'] is True:
            pass
        elif set(value) == {'text'}:
            _text(value['text'], 512)
        else:
            raise ValueError('invalid_update_deadline')
    if reopen and fields.get('status') != 'active':
        raise ValueError('invalid_reopen')
    request = {'patch': fields, 'reopen': reopen, 'source_time': source_time}
    if len(_json(request).encode('utf-8')) > 128 * 1024:
        raise ValueError('update_request_too_large')
    return request

class LegacyMemoryUpdateReader:
    def __init__(self, service: Any):
        self.service = service

    def _path(self, memory_id: str) -> Path:
        safe_component(memory_id, 'memory id')
        root = self.service.vault.state_path / 'memory_updates'
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise ValueError('unsafe_update_state_path')
        path = root / f'{memory_id}.json'
        if path.is_symlink():
            raise ValueError('unsafe_update_journal')
        return path

    @staticmethod
    def _operation_id(identity: str, expected: str, request: dict) -> str:
        return 'update-' + hashlib.sha256(_json([identity, expected, request]).encode('utf-8')).hexdigest()

    @staticmethod
    def _replacement(current: Memory, request: dict, now: str, operation_id: str) -> Memory | None:
        fields = deepcopy(request['patch'])
        if current.type != 'todo' and fields.keys() & {'status', 'completed_at', 'assignee', 'waiting_on', 'deadline'}:
            raise ValueError('todo_fields_on_non_todo')
        if current.validity == 'retracted' and (fields.get('validity') != 'valid' or not fields.get('body')):
            raise ValueError('explicit_restore_required')
        if current.status in {'completed', 'cancelled'} and fields.get('status') == 'active' and not request['reopen']:
            raise ValueError('explicit_reopen_required')
        old = current.to_dict()
        value = deepcopy(old)
        deadline = fields.pop('deadline', None)
        value.update(fields)
        if 'status' in fields and fields['status'] != current.status and 'completed_at' not in fields:
            value.pop('completed_at', None)
        if value.get('completed_at') is not None and value.get('status') != 'completed':
            raise ValueError('completion_time_requires_completed')
        basis = {'source': 'explicit_update', 'event_key': operation_id, 'observed_at': now}
        if request['source_time'] is not None:
            basis['source_time'] = request['source_time']
        if deadline is not None:
            if deadline.get('clear'):
                value.update(due_date=None, due_text=None, due_status='cleared')
            else:
                selected = selected_calendar(deadline['text'], {'text': deadline['text'], 'source_time': request['source_time']})
                value.update(due_date=selected['date'], due_text=selected['text'], due_status=selected['status'])
            # Compare calendar meaning before assigning the new operation basis.
            existing_anchor = old.get('due_anchor', {})
            if not isinstance(existing_anchor, dict):
                existing_anchor = {}
            anchor = deepcopy(existing_anchor)
            if request['source_time'] is not None:
                anchor['source_time'] = request['source_time']
            elif not value.get('due_date') and deadline.get('text'):
                anchor.pop('source_time', None)
            value['due_anchor'] = anchor
        # Freeze the same canonical body that the Markdown reader will return.
        value = Memory.from_markdown(Memory.from_mapping(value).to_markdown()).to_dict()
        if revision_digest(value) == revision_digest(old):
            return None
        changed = {key for key in fields if fields[key] != old.get(key)}
        if deadline is not None:
            changed.add('deadline')
            value['due_anchor'] = deepcopy(basis)
        for key in _RECEIPTS:
            value.pop(key, None)
        if current.validity == 'retracted':
            value.pop('retracted_at', None)
            value.pop('retraction_reason', None)
        if 'scopes' in changed:
            value['scope_source'] = 'explicit'
        previous_basis = value.get('field_basis', {})
        if not isinstance(previous_basis, dict):
            raise ValueError('invalid_existing_field_basis')
        value['field_basis'] = {**previous_basis, **{_GROUP[k]: deepcopy(basis) for k in changed if k in _GROUP}}
        value['updated'] = now
        value['explicit_update_operation_id'] = operation_id
        after = Memory.from_mapping(value)
        after.extra['explicit_update_result_digest'] = revision_digest(after)
        return after

    def _load(self, identity: str) -> dict | None:
        path = self._path(identity)
        if not path.exists():
            return None
        if not path.is_file() or path.stat().st_size > MAX_PLAN_BYTES * 2:
            raise ValueError('update_journal_too_large')
        with path.open('rb') as stream:
            raw = stream.read(MAX_PLAN_BYTES * 2 + 1)
        if len(raw) > MAX_PLAN_BYTES * 2:
            raise ValueError('update_journal_too_large')
        stored = parse_strict_json(raw.decode('utf-8'))
        if not isinstance(stored, dict) or type(stored.get('schema_version')) is not int or stored['schema_version'] != 1:
            raise ValueError('invalid_update_journal_version')
        payload = stored.get('payload')
        if (not isinstance(payload, str) or len(payload.encode('utf-8')) > MAX_PLAN_BYTES
                or hashlib.sha256(payload.encode('utf-8')).hexdigest() != stored.get('checksum')):
            raise ValueError('invalid_update_journal_checksum')
        plan = parse_strict_json(payload)
        keys = {'memory_id', 'operation_id', 'expected_revision', 'replacement_revision',
                'before', 'after', 'prepared_at', 'request', 'scope_guard'}
        if not isinstance(plan, dict) or set(plan) != keys or plan['memory_id'] != identity:
            raise ValueError('invalid_update_journal_shape')
        request = plan['request']
        if not isinstance(request, dict) or set(request) != {'patch', 'reopen', 'source_time'}:
            raise ValueError('invalid_update_request')
        if normalize_request(request['patch'], reopen=request['reopen'], source_time=request['source_time']) != request:
            raise ValueError('invalid_update_request')
        for key in ('expected_revision', 'replacement_revision'):
            if not isinstance(plan[key], str) or re.fullmatch(r'[0-9a-f]{64}', plan[key]) is None:
                raise ValueError('invalid_update_revision')
        if parse_source_time(plan['prepared_at']) is None:
            raise ValueError('invalid_update_prepare_time')
        before, after = Memory.from_markdown(plan['before']), Memory.from_markdown(plan['after'])
        operation = self._operation_id(identity, plan['expected_revision'], request)
        replacement = self._replacement(before, request, plan['prepared_at'], operation)
        # The checksummed Markdown stays frozen, but mapping insertion order is
        # not business state. Compare ALL parsed fields (including read counters),
        # not a second rendering whose key order can differ after JSON/restart.
        if (before.memory_id != identity or after.memory_id != identity
                or revision_digest(before) != plan['expected_revision']
                or revision_digest(after) != plan['replacement_revision']
                or plan['operation_id'] != operation or replacement is None
                or replacement.to_dict() != after.to_dict()):
            raise ValueError('invalid_update_frozen_state')
        if plan['scope_guard'] is not None:
            from .incremental_scopes import validate_guard
            validate_guard(plan['scope_guard'])
        return plan
