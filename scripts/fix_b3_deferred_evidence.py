from pathlib import Path

path = Path("src/memleaf/single_pass_memory_planner.py")
text = path.read_text(encoding="utf-8")
old = '''            candidate_settled = bool(candidate_ids) and len(statuses) == len(candidate_ids) and all(
                status in {"CREATE", "UPDATE", "NO_CHANGE"} for status in statuses
            )
            if (
                row.get("decision") == "CANDIDATE" and candidate_settled
            ) or (
'''
new = '''            candidate_settled = bool(candidate_ids) and len(statuses) == len(candidate_ids) and all(
                status in {"CREATE", "UPDATE", "NO_CHANGE"} for status in statuses
            )
            if row.get("decision") == "CANDIDATE" and not candidate_settled:
                # A candidate that remains DEFERRED has not settled its source
                # evidence. Preserve that fact in the evidence ledger instead
                # of presenting semantic coverage as a terminal outcome.
                row["decision"] = "DEFERRED"
                row["reason"] = "coverage_unresolved"
                row["candidate_ids"] = []
            if (
                row.get("decision") == "CANDIDATE" and candidate_settled
            ) or (
'''
if text.count(old) != 1:
    raise SystemExit(f"expected one unresolved-evidence anchor, found {text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
