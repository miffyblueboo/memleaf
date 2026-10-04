"""Compile one model merge into ordered, recoverable existing UPDATE operations.

Identity/semantic equivalence is reviewed by the existing model pass. Core
checks authority and dependencies; it never guesses duplicates from a title.
"""
from copy import deepcopy


def expand_merges(items, state):
    expanded, groups = [], []
    claimed = set()
    for row in items:
        if not isinstance(row, dict) or str(row.get('action', '')).strip().upper() != 'MERGE':
            expanded.append(row)
            continue
        try:
            from .incremental_protocol import _normalize_model_row, _keys, _BRANCHES, MAX_ITEMS
            row, _ = _normalize_model_row(row, state, only_row=False)
            required, allowed = _BRANCHES['MERGE']
            _keys(row, {'action', 'evidence'} | required, {'action', 'evidence'} | allowed, path='row')
            target, duplicates, patch = row['target'], row['duplicates'], row['patch']
            if (not isinstance(duplicates, list) or not duplicates or len(duplicates) >= MAX_ITEMS
                    or any(not isinstance(ref, str) for ref in duplicates)
                    or len(set(duplicates)) != len(duplicates) or target in duplicates
                    or not isinstance(patch, dict) or not isinstance(patch.get('body'), str) or not patch['body'].strip()
                    or patch.get('validity', 'valid') != 'valid'):
                raise ValueError('invalid_merge')
            members = [target, *duplicates]
            if set(members) & claimed:
                raise ValueError('overlapping_merge')
            records = [state['targets'][ref] for ref in members]
            anchor = records[0]['memory']
            if any(not item['writable'] or item.get('native') or item['memory'].get('validity', 'valid') != 'valid'
                   or item['memory']['type'] != anchor['type']
                   or set(item['memory'].get('scopes', [])) != set(anchor.get('scopes', [])) for item in records):
                raise ValueError('incompatible_merge_targets')
            survivor = {k: deepcopy(v) for k, v in row.items() if k != 'duplicates'}
            survivor['action'] = 'UPDATE'
            expanded.append(survivor)
            for ref in duplicates:
                expanded.append({'action': 'UPDATE', 'target': ref, 'evidence': deepcopy(row['evidence']),
                                 **({'at': row['at']} if 'at' in row else {}),
                                 'patch': {'validity': 'retracted', 'body': 'Merged into ' + anchor['title']}})
            groups.append(members)
            claimed.update(members)
        except (KeyError, TypeError, ValueError):
            # The normal compiler reports this row and isolates its target.
            expanded.append(row)
    return expanded, groups


def bind_merges(operations, groups, state, issues):
    for members in groups:
        identities = [state['targets'][ref]['memory']['memory_id'] for ref in members]
        updates = {op.get('target'): op for op in operations if op['action'] == 'UPDATE'}
        if any(identity not in updates for identity in identities):
            operations[:] = [op for op in operations if op.get('target') not in identities]
            issues.append({'row': None, 'code': 'merge_precondition_failed', 'evidence': []})
            continue
        survivor = updates[identities[0]]
        survivor['merge_from'] = identities[1:]
        for identity in identities[1:]:
            retired = updates[identity]
            retired['merged_into'] = identities[0]
            retired['memory']['merged_into'] = identities[0]
    # A preceding NO_CHANGE can establish a group's insertion order before
    # the MERGE row. Commit dependency order must not depend on model row order.
    roots = [op for op in operations if not op.get('merged_into')]
    retirees = [op for op in operations if op.get('merged_into')]
    operations[:] = roots + retirees
