from pathlib import Path

path = Path("tests/test_stage_b1.py")
text = path.read_text(encoding="utf-8")
old_name = "    def test_openai_whitespace_content_keeps_diagnostics_and_allows_third_attempt(self):\n"
new_name = "    def test_openai_whitespace_content_keeps_diagnostics_and_legacy_gate_allows_third_attempt(self):\n"
if text.count(old_name) != 1:
    raise SystemExit(f"expected one legacy whitespace test name, found {text.count(old_name)}")
text = text.replace(old_name, new_name, 1)
old_backend = '''        backend = OpenAICompatibleBackend(
            base_url="https://provider.invalid/v1",
            api_key="key",
            model="model",
            opener=opener,
            json_mode=True,
            provider_name="deepseek",
        )
        with tempfile.TemporaryDirectory() as temporary:
'''
new_backend = '''        backend = OpenAICompatibleBackend(
            base_url="https://provider.invalid/v1",
            api_key="key",
            model="model",
            opener=opener,
            json_mode=True,
            provider_name="deepseek",
        )
        # This regression protects the legacy P3 Gate executor's three-attempt
        # empty-content behavior. Fixed API routes now intentionally use B3,
        # whose independent budget is capped at one primary call plus one repair.
        backend.single_pass_safe = False
        with tempfile.TemporaryDirectory() as temporary:
'''
if text.count(old_backend) != 2:
    # The same constructor shape appears in the preceding direct adapter test.
    # Patch only the occurrence inside this named test by slicing from its marker.
    marker = text.index(new_name)
    tail = text[marker:]
    if tail.count(old_backend) < 1:
        raise SystemExit("legacy whitespace backend anchor missing")
    tail = tail.replace(old_backend, new_backend, 1)
    text = text[:marker] + tail
else:
    marker = text.index(new_name)
    tail = text[marker:]
    tail = tail.replace(old_backend, new_backend, 1)
    text = text[:marker] + tail
path.write_text(text, encoding="utf-8")
