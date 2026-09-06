# Performance and long-run scale

This benchmark measures memleaf's local Markdown Vault and derived indexes. It does not call an LLM, so model/provider latency is intentionally excluded.

## Methodology

Datasets contain global plus 24 project scopes, tags, aliases, keywords, wikilinks, active todos, historical versions, bounded high-frequency provenance, Chinese and English text, and mixed short/long bodies. CREATE/UPDATE/NO_CHANGE use `MemoryWriter` plus the real derived-index rebuild boundary; lifecycle measurements use the real retention and compaction primitives.

The hosted benchmark reports a process-cold first search and warm repeated searches. It does **not** claim a true OS cold-cache measurement because hosted CI cannot safely or reproducibly drop the kernel page cache.

The 50,000-active-memory dataset is deliberately an extreme stress boundary. It is not an assumption that a healthy Vault should normally retain 50,000 useful active memories. The dataset has 20% global memories and spreads the rest across 24 project scopes, so even one project scope contains roughly 1.6k-1.7k active records at 50k total.

Normal product retrieval remains `Scope Map -> scope-constrained search -> read`; full-vault full-text search is a fallback/special case rather than the primary injection path.

Platform: `Linux-6.17.0-1022-azure-x86_64-with-glibc2.39`
Python: `3.13.15`
Run UTC: `2026-09-06T03:49:07Z`

## Latency results

All latency values are milliseconds.

| Active memories | Metric | median | p95 | max | samples |
|---:|---|---:|---:|---:|---:|
| 1,000 | `vault_initialization` | 0.667 | 0.799 | 0.799 | 3 |
| 1,000 | `rebuild_index` | 390.421 | 398.467 | 398.467 | 3 |
| 1,000 | `search_candidate_process_cold` | 388.067 | 388.067 | 388.067 | 1 |
| 1,000 | `search_candidate_warm` | 386.328 | 388.011 | 388.011 | 3 |
| 1,000 | `exact_memory_id_lookup` | 385.124 | 386.845 | 386.845 | 3 |
| 1,000 | `fulltext_search` | 385.229 | 389.206 | 389.206 | 3 |
| 1,000 | `scope_filtered_search` | 369.218 | 370.537 | 370.537 | 3 |
| 1,000 | `list_todos` | 337.284 | 363.616 | 363.616 | 3 |
| 1,000 | `read` | 337.300 | 338.413 | 338.413 | 3 |
| 1,000 | `create` | 1149.732 | 1218.470 | 1218.470 | 3 |
| 1,000 | `update` | 1068.737 | 1132.795 | 1132.795 | 3 |
| 1,000 | `no_change` | 1109.159 | 1185.680 | 1185.680 | 3 |
| 1,000 | `history_write` | 3.047 | 3.392 | 3.392 | 3 |
| 1,000 | `closed_todo_retirement` | 835.599 | 843.429 | 843.429 | 3 |
| 1,000 | `history_pruning` | 572.614 | 578.825 | 578.825 | 3 |
| 1,000 | `compaction_snapshot` | 356.001 | 370.681 | 370.681 | 3 |
| 1,000 | `vault_lock_hold_search` | 381.196 | 381.196 | 381.196 | 1 |
| 10,000 | `vault_initialization` | 0.710 | 0.835 | 0.835 | 3 |
| 10,000 | `rebuild_index` | 4058.684 | 4157.842 | 4157.842 | 3 |
| 10,000 | `search_candidate_process_cold` | 3809.702 | 3809.702 | 3809.702 | 1 |
| 10,000 | `search_candidate_warm` | 3700.780 | 3752.602 | 3752.602 | 3 |
| 10,000 | `exact_memory_id_lookup` | 3925.242 | 3934.726 | 3934.726 | 3 |
| 10,000 | `fulltext_search` | 3726.453 | 3854.321 | 3854.321 | 3 |
| 10,000 | `scope_filtered_search` | 3643.267 | 3805.982 | 3805.982 | 3 |
| 10,000 | `list_todos` | 3480.108 | 3481.024 | 3481.024 | 3 |
| 10,000 | `read` | 3419.434 | 3502.751 | 3502.751 | 3 |
| 10,000 | `create` | 10926.382 | 11013.296 | 11013.296 | 3 |
| 10,000 | `update` | 10950.698 | 11030.074 | 11030.074 | 3 |
| 10,000 | `no_change` | 10976.892 | 11187.753 | 11187.753 | 3 |
| 10,000 | `history_write` | 14.652 | 40.393 | 40.393 | 3 |
| 10,000 | `closed_todo_retirement` | 7271.686 | 7683.019 | 7683.019 | 3 |
| 10,000 | `history_pruning` | 4214.574 | 5079.721 | 5079.721 | 3 |
| 10,000 | `compaction_snapshot` | 3625.947 | 3808.937 | 3808.937 | 3 |
| 10,000 | `vault_lock_hold_search` | 3721.875 | 3721.875 | 3721.875 | 1 |
| 50,000 | `vault_initialization` | 0.600 | 0.759 | 0.759 | 3 |
| 50,000 | `rebuild_index` | 20435.382 | 20560.472 | 20560.472 | 2 |
| 50,000 | `search_candidate_process_cold` | 19243.749 | 19243.749 | 19243.749 | 1 |
| 50,000 | `search_candidate_warm` | 19148.126 | 19553.817 | 19553.817 | 3 |
| 50,000 | `exact_memory_id_lookup` | 19556.986 | 19834.327 | 19834.327 | 3 |
| 50,000 | `fulltext_search` | 19855.403 | 20002.997 | 20002.997 | 3 |
| 50,000 | `scope_filtered_search` | 18576.396 | 18639.123 | 18639.123 | 3 |
| 50,000 | `list_todos` | 18160.793 | 18328.362 | 18328.362 | 3 |
| 50,000 | `read` | 17648.840 | 18197.055 | 18197.055 | 3 |
| 50,000 | `create` | 56061.953 | 56745.539 | 56745.539 | 3 |
| 50,000 | `update` | 55893.789 | 56715.012 | 56715.012 | 3 |
| 50,000 | `no_change` | 55934.604 | 56323.055 | 56323.055 | 3 |
| 50,000 | `history_write` | 5.971 | 70.687 | 70.687 | 3 |
| 50,000 | `closed_todo_retirement` | 37710.833 | 37795.840 | 37795.840 | 3 |
| 50,000 | `history_pruning` | 21802.002 | 21843.263 | 21843.263 | 3 |
| 50,000 | `compaction_snapshot` | 19330.915 | 19619.114 | 19619.114 | 3 |
| 50,000 | `vault_lock_hold_search` | 19650.941 | 19650.941 | 19650.941 | 1 |

## Resource footprint

| Active memories | Peak RSS MiB | Vault MiB | `_index/` MiB | `_state/` MiB |
|---:|---:|---:|---:|---:|
| 1,000 | 50.871 | 2.174 | 0.170 | 0.000 |
| 10,000 | 259.891 | 21.156 | 1.458 | 0.000 |
| 50,000 | 1186.762 | 105.841 | 7.157 | 0.000 |

## Scale interpretation

The 2,000 ms search, 120 s rebuild, and 1,024 MiB RSS values are retained as stress references only. A miss at 50,000 active memories is capacity evidence, not a release blocker and not a reason to introduce a new database/search backend.

**Architecture change required from the 50k stress result: no.** 50k active memories is retained as an extreme stress boundary, not a normal steady-state assumption. The normal retrieval path is Scope Map -> scope-constrained search -> read, while UPDATE/NO_CHANGE, todo retirement, bounded history, and compaction are expected to control active-memory growth. Stress misses are recorded for capacity visibility and do not justify adding another storage/search backend.

If a real Vault grows into the tens of thousands of active memories, first audit CREATE-vs-UPDATE/NO_CHANGE behavior, todo retirement, history retention, duplicate control, and compaction. The product remains Markdown-only in v0.2.28.

## Active-memory lifecycle health

Active memory count is a health signal, not an archival counter. Repeated facts should update existing canonical memories, unchanged observations should be NO_CHANGE, completed/cancelled todos retire from active memory, historical versions are bounded, and compaction reduces redundant active material. A real Vault approaching the 50k stress dataset should therefore trigger lifecycle/quality investigation before search-backend expansion.

## Runtime state growth audit

The retrieval ledger is already TTL- and count-bounded, and per-session pending tool evidence/injection bookkeeping is bounded. The processed-turn/session ledger retains replay/idempotency evidence and is therefore intentionally not destructively pruned in v0.2.28: deleting it without a new durable replay checkpoint would risk duplicate replay. Future state compaction must first define a verified checkpoint that proves older replay evidence is no longer needed.
