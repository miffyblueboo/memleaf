from pathlib import Path


def patch_admission() -> None:
    path = Path("src/memleaf/admission.py")
    text = path.read_text(encoding="utf-8")

    first = '''                    evidence_check="coverage_terminal_witness",
                )
            if not units[uid].can_support'''
    first_replacement = '''                    evidence_check="coverage_terminal_witness",
                )
            if set(row) != {"unit_id", "decision", "candidate_ids"}:
                raise ModelOutputError(
                    "invalid coverage candidate shape",
                    validation_detail="invalid_evidence",
                    evidence_check="coverage_shape",
                )
            if not units[uid].can_support'''
    if first not in text:
        raise SystemExit("candidate coverage insertion point not found")
    text = text.replace(first, first_replacement, 1)

    second = '''                    evidence_check="coverage_terminal_witness",
                )
            # Normalize only the model's declared reason.'''
    second_replacement = '''                    evidence_check="coverage_terminal_witness",
                )
            expected_fields = ({"unit_id", "decision", "reason", "memory_id"}
                               if reason == "already_completed"
                               else {"unit_id", "decision", "reason"})
            if set(row) != expected_fields:
                raise ModelOutputError(
                    "invalid coverage decision shape",
                    validation_detail="invalid_evidence",
                    evidence_check="coverage_shape",
                )
            # Normalize only the model's declared reason.'''
    if second not in text:
        raise SystemExit("terminal coverage insertion point not found")
    text = text.replace(second, second_replacement, 1)
    path.write_text(text, encoding="utf-8")


def patch_stage_b1() -> None:
    path = Path("tests/test_stage_b1.py")
    text = path.read_text(encoding="utf-8")

    old_name = "def test_openai_whitespace_content_keeps_diagnostics_and_allows_third_attempt(self):"
    new_name = "def test_openai_whitespace_content_keeps_diagnostics_and_legacy_gate_allows_third_attempt(self):"
    if old_name not in text:
        raise SystemExit("legacy Gate regression name not found")
    text = text.replace(old_name, new_name, 1)

    start = text.index(new_name)
    end = text.index("    def test_unknown_openai_compatible_provider_keeps_legacy_request_shape", start)
    block = text[start:end]
    marker = '''            provider_name="deepseek",
        )
        with tempfile.TemporaryDirectory() as temporary:'''
    replacement = '''            provider_name="deepseek",
        )
        # This regression protects the legacy P3 Gate executor's three-attempt
        # empty-content behavior. Fixed API routes now intentionally use B3,
        # whose independent budget is capped at one primary call plus one repair.
        backend.single_pass_safe = False
        with tempfile.TemporaryDirectory() as temporary:'''
    if marker not in block:
        raise SystemExit("legacy Gate safety override insertion point not found")
    block = block.replace(marker, replacement, 1)
    text = text[:start] + block + text[end:]
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    patch_admission()
    patch_stage_b1()
