from __future__ import annotations

from pathlib import Path


path = Path("src/memleaf/update_coordinator.py")
source = path.read_text(encoding="utf-8")

import_anchor = "from .admission import summary_evidence\n"
assert source.count(import_anchor) == 1
source = source.replace(
    import_anchor,
    import_anchor + "from .batch_review import MAX_REVIEW_BATCH_ITEMS, review_create_batch, review_update_batch\n",
    1,
)

helper_marker = "    @staticmethod\n    def _make_update_review_parser(\n"
assert source.count(helper_marker) == 1
helper = '''    def _run_batched_review_specs(
        self,
        specs: list[dict[str, Any]],
        *,
        backend: Any,
        update: bool,
    ) -> list[dict[str, Any]]:
        """Batch independent reviews while preserving original result order."""

        if not specs:
            return []
        chunks = [
            specs[index:index + MAX_REVIEW_BATCH_ITEMS]
            for index in range(0, len(specs), MAX_REVIEW_BATCH_ITEMS)
        ]
        jobs: list[Callable[[], Any]] = []
        for chunk in chunks:
            def run_chunk(
                *,
                chunk_value: list[dict[str, Any]] = chunk,
                update_value: bool = update,
            ) -> list[dict[str, Any]]:
                if update_value:
                    return review_update_batch(self.model, backend, chunk_value)
                return review_create_batch(self.model, backend, chunk_value)
            jobs.append(run_chunk)
        chunk_outcomes = self._run_review_jobs(jobs, backend=backend)
        outcomes: list[dict[str, Any]] = []
        for chunk, chunk_result in zip(chunks, chunk_outcomes):
            if not isinstance(chunk_result, list) or len(chunk_result) != len(chunk):
                # The batch kernel normally guarantees shape. Preserve the old
                # fail-closed single path if a custom executor violates it.
                chunk_result = []
                for spec in chunk:
                    kwargs = {
                        "admitted_source": spec["admitted_source"],
                        "proposed_summary": spec["proposed_summary"],
                        "parse_summary": spec["parse_summary"],
                        "diagnostic_context": spec.get("diagnostic_context"),
                    }
                    if update:
                        chunk_result.append(review_update(
                            self.model, backend, target=spec["target"], **kwargs
                        ))
                    else:
                        chunk_result.append(review_create(self.model, backend, **kwargs))
            outcomes.extend(dict(item) for item in chunk_result if isinstance(item, Mapping))
        if len(outcomes) != len(specs):
            raise ProcessingError("semantic review batch result count mismatch")
        return outcomes

'''
source = source.replace(helper_marker, helper + helper_marker, 1)

update_start = source.index("    def _review_final_updates(\n")
update_end = source.index("    def _review_final_creates(\n", update_start)
update_section = source[update_start:update_end]
assert update_section.count("        jobs: list[Callable[[], dict[str, Any]]] = []\n") == 1
update_section = update_section.replace(
    "        jobs: list[Callable[[], dict[str, Any]]] = []\n",
    "        review_specs: list[dict[str, Any]] = []\n",
    1,
)
block_start = update_section.index("            def run_review(\n")
outcome_marker = "        outcomes = self._run_review_jobs(jobs, backend=backend)\n"
block_end = update_section.index(outcome_marker, block_start)
update_block = '''            job_index = len(review_specs)
            review_specs.append({
                "review_id": f"update:{job_index}:{request.get('candidate_id', '')}",
                "target": target,
                "admitted_source": projected,
                "proposed_summary": self._review_content(summary),
                "parse_summary": parser,
                "diagnostic_context": diagnostic_context,
            })
            slots.append({
                "kind": "review",
                "request": request,
                "summary": summary,
                "target_id": target_id,
                "job_index": job_index,
            })

'''
update_section = update_section[:block_start] + update_block + update_section[block_end:]
update_section = update_section.replace(
    outcome_marker,
    "        outcomes = self._run_batched_review_specs(review_specs, backend=backend, update=True)\n",
    1,
)
source = source[:update_start] + update_section + source[update_end:]

create_start = source.index("    def _review_final_creates(\n")
create_end = source.index("    def _defer(self,", create_start)
create_section = source[create_start:create_end]
assert create_section.count("        jobs: list[Callable[[], dict[str, Any]]] = []\n") == 1
create_section = create_section.replace(
    "        jobs: list[Callable[[], dict[str, Any]]] = []\n",
    "        review_specs: list[dict[str, Any]] = []\n",
    1,
)
block_start = create_section.index("            def run_review(\n")
outcome_marker = "        outcomes = self._run_review_jobs(jobs, backend=backend)\n"
block_end = create_section.index(outcome_marker, block_start)
create_block = '''            job_index = len(review_specs)
            review_specs.append({
                "review_id": f"create:{job_index}:{request.get('candidate_id', '')}",
                "admitted_source": projected,
                "proposed_summary": self._review_content(summary),
                "parse_summary": parser,
                "diagnostic_context": diagnostic_context,
            })
            slots.append({
                "kind": "review",
                "request": request,
                "summary": summary,
                "job_index": job_index,
            })

'''
create_section = create_section[:block_start] + create_block + create_section[block_end:]
create_section = create_section.replace(
    outcome_marker,
    "        outcomes = self._run_batched_review_specs(review_specs, backend=backend, update=False)\n",
    1,
)
source = source[:create_start] + create_section + source[create_end:]

path.write_text(source, encoding="utf-8")
