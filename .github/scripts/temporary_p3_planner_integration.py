from __future__ import annotations

from pathlib import Path


def main() -> None:
    path = Path("src/memleaf/memory_planner.py")
    source = path.read_text(encoding="utf-8")

    old_import = "from .parallel_model import run_ordered_keyed_jobs\n"
    new_import = "from .summary_batch import run_summary_jobs_with_create_batching\n"
    assert source.count(old_import) == 1
    source = source.replace(old_import, new_import, 1)

    old_job = '''            job_index = len(summary_jobs)
            summary_jobs.append({
                "key": target_key,
                "call": run_summary,
                "candidate": dict(candidate),
                "candidate_related": candidate_related,
                "candidate_native_refs": candidate_native_refs,
                "correction_plan": correction_plan,
                "gate_update_target": gate_update_target,
                "target_revisions": target_revisions,
            })
'''
    new_job = '''            job_index = len(summary_jobs)
            summary_jobs.append({
                "key": target_key,
                "call": run_summary,
                "batchable": gate_update_target is None and correction_plan is None,
                "item_id": f"summary-{job_index}",
                "prompt": summary_prompt_value,
                "parser": parse_summary,
                "diagnostic_context": diagnostic_context,
                "candidate": dict(candidate),
                "candidate_related": candidate_related,
                "candidate_native_refs": candidate_native_refs,
                "correction_plan": correction_plan,
                "gate_update_target": gate_update_target,
                "target_revisions": target_revisions,
            })
'''
    assert source.count(old_job) == 1
    source = source.replace(old_job, new_job, 1)

    old_exec = '''        summary_outcomes = run_ordered_keyed_jobs(
            self.model,
            backend,
            [(job["key"], job["call"]) for job in summary_jobs],
        )
'''
    new_exec = '''        summary_outcomes = run_summary_jobs_with_create_batching(
            self.model,
            backend,
            summary_jobs,
        )
'''
    assert source.count(old_exec) == 1
    source = source.replace(old_exec, new_exec, 1)
    path.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    main()
