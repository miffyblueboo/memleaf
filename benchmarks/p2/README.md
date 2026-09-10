# P2 prompt/input slimming

This experiment starts from P1 corrected B0 and changes only model input representation/prompt wording. It does not change Gate candidate semantics, evidence validation, coverage rules, writer commit semantics, model-call stages, retry caps, or review requirements.

## User priority

API price is non-blocking. Provider + exact model remain A/B control variables, but tariff verification and monetary caps are not prerequisites for P2 work. Primary goals are lower prompt/input token use and less prompt-induced unnecessary reasoning.

## Phase 1: canonical Gate conversation text

Before P2, each Gate batch serialized full visible turn-event content and also serialized the same conversation text again inside Evidence units. Multi-batch turns repeated the complete event body for every Gate call.

P2 keeps event identity/timing metadata in the event envelope but makes Evidence units the single model-visible source of conversation text. Tool bodies remain excluded. Candidate/coverage/evidence-binding contracts are unchanged.

`static_prompt_audit.py` reproduces the corrected-B0 envelope and compares it with the current P2 envelope using synthetic data. It is a UTF-8 byte proxy, not a tokenizer and not a real-model latency claim. It makes zero model calls.

## Next phase

After Phase 1 is structurally green, reduce Summary context to admitted evidence + fixed candidate + the single selected UPDATE target (if any), then trim duplicated system-prompt wording while preserving executable protocol contracts. Product thinking defaults remain `low`; do not switch to `disabled` without quality evidence.
