from __future__ import annotations

from pathlib import Path
import runpy

path = Path("_v0238_patch.py")
text = path.read_text(encoding="utf-8")

# Align the staged patch with the exact v0.2.37 import list.
text = text.replace(
    "from .prompts import COVERAGE_ALREADY_COMPLETED_CORRECTION, COVERAGE_CANDIDATE_CORRECTION, COVERAGE_CORRECTION, GATE_SYSTEM, SUMMARIZE_SYSTEM, gate_prompt, summarize_prompt",
    "from .prompts import COVERAGE_ALREADY_COMPLETED_CORRECTION, COVERAGE_CORRECTION, GATE_SYSTEM, SUMMARIZE_SYSTEM, gate_prompt, summarize_prompt",
).replace(
    "from .prompts import COVERAGE_ALREADY_COMPLETED_CORRECTION, COVERAGE_CANDIDATE_CORRECTION, COVERAGE_CORRECTION, GATE_COVERAGE_SYSTEM, GATE_SYSTEM, SUMMARIZE_SYSTEM, coverage_repair_prompt, gate_prompt, summarize_prompt",
    "from .prompts import COVERAGE_ALREADY_COMPLETED_CORRECTION, COVERAGE_CORRECTION, GATE_COVERAGE_SYSTEM, GATE_SYSTEM, SUMMARIZE_SYSTEM, coverage_repair_prompt, gate_prompt, summarize_prompt",
)

# UPDATE and CREATE semantic-review text have slightly different line wrapping.
marker = "replace_count(path, old, new, 2)"
if marker not in text:
    raise SystemExit("semantic review patch marker missing")
create_patch = r'''replace_count(path, old, new, 1)
regex_once(
    path,
    r"broader product/platform/system where it is implemented, preserve that\ndistinction; do not replace the owning subject with the implementation context\.\nExisting memories are comparison context",
    "broader product/platform/system where it is implemented, preserve that\n"
    "distinction; do not replace the owning subject with the implementation context.\n"
    "When proposed_summary carries a project:<name> Scope with scope_source=model,\n"
    "that Scope is itself a claimed project affiliation: ACCEPT only when the\n"
    "admitted source supports that affiliation for this candidate. A mere mention of\n"
    "the same name as a product, platform, system, notification source, comparison,\n"
    "or implementation location is insufficient. If admitted source explicitly\n"
    "assigns the item to another project, the fixed proposed Scope cannot be repaired\n"
    "in this review; use DEFERRED rather than ACCEPT or silently changing Scope.\n"
    "Existing memories are comparison context",
)
'''
text = text.replace(marker, create_patch, 1)
path.write_text(text, encoding="utf-8")

runpy.run_path(str(path), run_name="__main__")

# _v0238_patch.py self-deletes on success; remove this adapter too.
Path(__file__).unlink(missing_ok=True)
