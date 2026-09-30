"""Explicit, revision-bound recovery of failed automatic transport runs.

Preview never opens a Vault lock, calls a model or reconstructs persisted
requests. Apply rebuilds a current request for unchanged source under the same
run and budget, retaining the original failed attempt and a bounded audit.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .extraction_work_state import _read_budget_state_unlocked, ExtractionWorkStateError
from .incremental_commit import _window
from .incremental_execution import (TRANSIENT, RETRY_SYSTEM, _guard_legacy, _protocol_digest,
                                    resume_incremental_run)
from .incremental_journal import digest, load_work
from .incremental_preview import _prepare_incremental_unlocked
from .incremental_prompts import INCREMENTAL_SYSTEM
from .incremental_protocol import MAX_BYTES
from .incremental_run_state import TERMINAL, MAX_RUNS, COMPACT_VERSION, load_run, owner_live, public_result, save_run
from .llm.base import HTTP_RETRYABLE_STATUSES
from .models import utc_now
from .process_common import _read_processed
from .recording_policy import recording_allowed
from .turn_plan import input_digest, turn_identity_key
from .receipt_codec import is_compact, ledger_usage


class FailedRecoveryError(ValueError):
    """Bounded public rejection, without private provider/source text."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _reject(code: str):
    raise FailedRecoveryError(code)


def _plan(service: Any, run_id: str, *, allow_legacy_http: bool):
    processed = _read_processed(service.vault.processed_state_path)
    run = load_run(processed, run_id)
    if run is None:
        _reject('incremental_run_not_found')
    if (is_compact(processed['incremental_runs'][run_id], COMPACT_VERSION)
            and ledger_usage(processed['incremental_runs'], compact_version=COMPACT_VERSION)['full'] >= MAX_RUNS):
        _reject('active_run_capacity_unavailable')
    if turn_identity_key(run['source'], run['session_id'], run['turn_key']) in processed.get('pending_turn_plans', {}):
        _reject('legacy_pending_plan')
    if run['status'] != 'failed' or 'terminal_recovery' in run:
        _reject('terminal_recovery_not_available')
    if run.get('request_kind', 'automatic') != 'automatic' or run['arguments'].get('selection'):
        _reject('explicit_retention_requires_new_authorization')
    if owner_live(processed):
        _reject('incremental_model_busy')
    _guard_legacy(service, processed)
    attempts = run['attempts']
    # Only a known failed first transport dispatch. Unknown reservations,
    # responses, partial commits and exhausted attempts cannot be reopened.
    if (len(attempts) != 1 or attempts[0]['ordinal'] != 1
            or attempts[0]['outcome'] != run.get('code')
            or run['reserved_requests'] != 1 or run.get('partial_used')):
        _reject('terminal_recovery_attempt_not_eligible')
    code = run.get('code')
    status = attempts[0].get('http_status')
    legacy = code == 'model_http_error' and status is None
    if code == 'model_http_error':
        if legacy and not allow_legacy_http:
            _reject('legacy_http_status_unknown')
        if not legacy and status not in HTTP_RETRYABLE_STATUSES:
            _reject('http_error_not_retryable')
    elif code not in TRANSIENT:
        _reject('failure_not_retryable')
    if not recording_allowed(processed, run['source'], run['session_id'], run['turn_key']):
        _reject('source_recording_revoked')
    if run['commit_work_id'] in processed.get('incremental_commits', {}):
        _reject('source_already_owned')
    turn, window = _window(service, run['source'], run['session_id'], run['turn_key'])
    if not turn.processable or input_digest(turn) != run['source_digest']:
        _reject('source_changed')
    from .extraction_work_state import extraction_work_id
    if extraction_work_id(turn, request_kind='automatic', intent_id='automatic') != run['budget_id']:
        _reject('source_budget_identity_changed')
    session = processed.get('sessions', {}).get(f"{run['source']}/{run['session_id']}", {})
    if any(isinstance(entry, dict) and entry.get('turn_key') == run['turn_key']
           for entry in session.get('processed_turns', [])):
        _reject('source_already_processed')
    for key in processed.get('incremental_commits', {}):
        work = load_work(processed, key)
        if (work['source'] == run['source'] and work['session_id'] == run['session_id']
                and work['turn_key'] == run['turn_key']
                and work.get('request_kind', 'automatic') == 'automatic'):
            _reject('source_already_owned')
    for key in processed.get('incremental_runs', {}):
        other = load_run(processed, key)
        if (key != run_id and other['source'] == run['source']
                and other['session_id'] == run['session_id'] and other['turn_key'] == run['turn_key']
                and other['status'] not in TERMINAL):
            _reject('source_already_owned')
    try:
        budget = _read_budget_state_unlocked(service.vault)
    except ExtractionWorkStateError:
        _reject('remaining_request_budget_unavailable')
    work_budget = budget['works'].get(run['budget_id'], {})
    row = work_budget.get('turns', {}).get(run['turn_budget_id'])
    if (not row or work_budget.get('retired') or row['completed'] or run.get('budget_finalized')
            or row['requests'] != 1 or row['request_limit_at_creation'] <= row['requests']):
        _reject('remaining_request_budget_unavailable')
    snapshot = _prepare_incremental_unlocked(service, **run['arguments'])
    request = {'system': INCREMENTAL_SYSTEM,
               'user': json.dumps(snapshot.model_input(), ensure_ascii=False, separators=(',', ':'))}
    if sum(len(s.encode('utf-8')) for s in request.values()) + len(RETRY_SYSTEM.encode('utf-8')) > MAX_BYTES:
        _reject('blocked_context')
    # Preview has no advisory lock. Confirm the independently read planning
    # state/budget stayed stable; apply repeats this inside the existing lock.
    if (processed != _read_processed(service.vault.processed_state_path)
            or budget != _read_budget_state_unlocked(service.vault)):
        _reject('recovery_preview_changed')
    revision = digest({'processed': processed, 'budget': budget, 'source_window': window,
                       'snapshot': snapshot.snapshot_id, 'protocol': _protocol_digest(),
                       'allow_legacy_http': allow_legacy_http, 'run_id': run_id})
    result = {'run_id': run_id, 'recoverable': True, 'read_only': True, 'model_calls': 0,
              'expected_revision': revision, 'remaining_requests': 1,
              'http_status': status, 'legacy_http_status_unknown': legacy,
              'source_unchanged': True, 'source_window_changed': window != run['source_window'],
              'snapshot_changed': snapshot.snapshot_id != run['snapshot_id'],
              'new_events': sum(e['use'] == 'new' for e in snapshot.state()['evidence']),
              'context_events': sum(e['use'] == 'context' for e in snapshot.state()['evidence'])}
    return processed, run, snapshot, window, request, result


def recover_failed_run(service: Any, run_id: str, *, dry_run: bool = True,
                       expected_revision: str | None = None, allow_legacy_http: bool = False,
                       model: Any = None, router: Any = None) -> dict[str, Any]:
    if type(dry_run) is not bool or type(allow_legacy_http) is not bool:
        _reject('invalid_recovery_options')
    if dry_run:
        try:
            return _plan(service, run_id, allow_legacy_http=allow_legacy_http)[-1]
        except (ValueError, OSError, ExtractionWorkStateError) as error:
            # Known local validators emit reason codes, never source bodies.
            code = error.code if isinstance(error, FailedRecoveryError) else 'recovery_preflight_failed'
            return {'run_id': run_id, 'recoverable': False, 'read_only': True,
                    'model_calls': 0, 'code': code}
    if not isinstance(expected_revision, str) or re.fullmatch(r'[0-9a-f]{64}', expected_revision) is None:
        _reject('expected_recovery_revision_required')
    from .incremental_runtime import _resolve
    # A repeated apply is the same original run, not another authorization.
    with service.vault.lock():
        stored = load_run(_read_processed(service.vault.processed_state_path), run_id)
        audit = stored.get('terminal_recovery') if stored else None
        if audit is not None:
            if audit['expected_revision'] != expected_revision or audit['allow_legacy_http'] != allow_legacy_http:
                _reject('recovery_already_bound')
            if stored['status'] in TERMINAL:
                return public_result(stored)
            # Active/resumable same-intent work uses the existing owner guard.
            already_bound = True
        else:
            already_bound = False
        if not already_bound:
            processed, run, snapshot, window, request, preview = _plan(
                service, run_id, allow_legacy_http=allow_legacy_http)
            if preview['expected_revision'] != expected_revision:
                _reject('stale_recovery_preview')
            backend = _resolve(service, model, router)
            audit = {'version': 1, 'expected_revision': expected_revision,
                     'allow_legacy_http': allow_legacy_http, 'previous_code': run['code'],
                     'previous_snapshot_id': run['snapshot_id'], 'previous_source_window': run['source_window'],
                     'requested_at': utc_now()}
            run.update(status='ready', code=None, snapshot_id=snapshot.snapshot_id, source_window=window,
                       request=request, request_digest=digest(request), protocol_digest=_protocol_digest(),
                       source_keys=[e['event_key'] for e in snapshot.state()['evidence']],
                       target_ids=[t['memory']['memory_id'] for t in snapshot.state()['targets'].values()],
                       terminal_recovery=audit)
            save_run(service, processed, run)
    if already_bound:
        # Resolve only if a dispatch is still needed. Saved responses/commits
        # remain recoverable with zero model calls and no API configuration.
        backend = (_resolve(service, model, router) if stored['status'] in {'ready', 'retryable', 'dispatching'}
                   else None)
    return resume_incremental_run(service, run_id, backend=backend)
