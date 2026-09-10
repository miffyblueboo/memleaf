from __future__ import annotations

import json

from memleaf.prompts import SUMMARIZE_SYSTEM
from memleaf.summary_batch import BATCH_SUMMARIZE_SYSTEM, _batch_prompt


def build_report() -> dict:
    prompts = [
        "P3_CREATE_ONE " + ("alpha " * 180),
        "P3_CREATE_TWO " + ("beta " * 180),
        "P3_CREATE_THREE " + ("gamma " * 180),
        "P3_CREATE_FOUR " + ("delta " * 180),
    ]
    groups = [prompts[index:index + 2] for index in range(0, len(prompts), 2)]
    separate_bytes = sum(
        len(SUMMARIZE_SYSTEM.encode("utf-8")) + len(prompt.encode("utf-8"))
        for prompt in prompts
    )
    batch_bytes = 0
    for group_index, group in enumerate(groups):
        items = [
            {
                "item_id": f"c{group_index * 2 + offset + 1}",
                "prompt": prompt,
            }
            for offset, prompt in enumerate(group)
        ]
        batch_bytes += len(BATCH_SUMMARIZE_SYSTEM.encode("utf-8"))
        batch_bytes += len(_batch_prompt(items).encode("utf-8"))
    return {
        "schema_version": 1,
        "measurement": "synthetic_static_utf8_bytes_and_call_count_zero_model_calls",
        "independent_create_count": len(prompts),
        "batch_size": 2,
        "legacy_summary_model_calls": len(prompts),
        "p3_summary_model_calls": len(groups),
        "model_call_reduction_ratio": 1 - len(groups) / len(prompts),
        "legacy_system_plus_user_bytes": separate_bytes,
        "p3_batch_system_plus_user_bytes": batch_bytes,
        "bytes_removed": separate_bytes - batch_bytes,
        "byte_reduction_ratio": round((separate_bytes - batch_bytes) / separate_bytes, 6),
        "note": "Static proxy only; not tokenizer tokens, latency, or semantic-quality evidence.",
    }


def main() -> int:
    print(json.dumps(build_report(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
