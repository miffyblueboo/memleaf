# memleaf v0.1 实施计划

基线：历史 v74 设计。`README.md` 与 `README.en.md` 作为对外行为契约；实现不得把 README 中尚未完成的目标描述成已发布能力。

发布范围更新（2026-08-28）：v0.1 仅 Hermes。下文 Codex/反重力适配记录仅为历史，不属于本版安装或验收范围。

2026-08-28 已批准的新检索与维护方案见 [v2 实施计划](V2_IMPLEMENTATION_PLAN.md)。其 Scope-only 注入、每轮 search、按需 read 和候选级维护规则取代下文旧的 3 项目录自动注入方案；旧条目保留为历史验收记录，不代表运行目录已部署新版。

## 实施原则

- Python 3.11+，核心运行时仅使用标准库。
- `knowledge/` Markdown 是真实数据源；JSON 索引必须可重建。
- MCP 为薄适配层，业务逻辑只存在于 core。
- 所有写入采用同目录临时文件、`fsync`、原子替换；并发写通过 vault 锁串行化。
- 处理成功前不推进轮次水位，不清理 inbox；模型失败必须可重试。
- `memleaf init` 是一次性安装/配置入口，不引入常驻进程。
- 保持 v74 计划文档骨架，仅在实现事实发生变化时做最小补充；README 同样只做必要更新。
- 用户决策（2026-08-25）：推荐源码和运行根固定为 `$HOME/memleaf`，不增加 `work` 层；从其他物理目录运行 `install.sh` 必须安全失败，只有显式设置 `MEMLEAF_INSTALL_ROOT` 才允许隔离测试/开发。
- 用户决策（2026-08-25）：只有可靠检测到 `hermes` 可执行文件时，才安装并激活官方外部 `MemoryProvider`；未检测到时不创建 Hermes provider 插件或配置，只输出诊断。
- 用户决策（2026-08-28）：v0.1 命令安装和 `memleaf init` 仅接入 Hermes；Codex、Antigravity 保留兼容代码但不检测、不配置、不扫描模型，`--all` 也不得绕过该边界。
- 已审核方案（2026-08-27）：自动 `context` 只返回记忆目录（ID、短标题、项目），最多 3 项，整个自动注入不超过 600 字符；不返回正文、来源或正文摘录。MCP `search` 默认返回目录，显式完整查询保留兼容；模型按需 `read`，长正文分段读取。目录曝光不计命中，正文实际读取才计命中；不改变 process 候选提炼、scope、状态更新/history 或 Markdown 存储语义。
- 用户决策（2026-08-25）：当前只做本地完整测试，不提交、不推送、不发布 Git/PyPI。

## 阶段 A：本地核心纵切

交付：可安装包、vault 初始化、受限 YAML/frontmatter、事件捕获与脱敏、Markdown 记忆 CRUD、索引重建、标签/全文/作用域检索、forget、统计。

建议模块：

```text
src/memleaf/
├── __init__.py
├── config.py
├── models.py
├── vault.py
├── frontmatter.py
├── redaction.py
├── locking.py
├── capture.py
├── index.py
├── retrieval.py
└── service.py
```

验收：

- 相同 `event_id` 重复 capture 不产生重复事件；同 session 恢复后可追加新轮次。
- 用户预先声明“不记录”的文本不进入 inbox；常见密钥、Cookie、私钥等在落盘前被脱敏。
- 标签多命中采用并集，随后按 scope 继承/覆盖过滤；标签无命中时才全文回退。
- `include_history=false` 不读取历史区；todo 默认仅返回 active。
- `forget_memory` 精确目标直接删除且不进 history；`forget_about` 含糊时只返回候选，不误删。
- 手工修改/删除 Markdown 后，`rebuild_index()` 只反映当前文件系统状态。
- 并发 capture 不丢事件，损坏索引可重建。

## 阶段 B：记忆处理与模型路由

交付：候选提取、逐条 gate、summarize、remember、当前状态更新、history、处理水位、24 小时清理、压缩、Model Router。

建议模块：

```text
src/memleaf/
├── prompts.py
├── processing.py
├── memory_writer.py
├── compaction.py
└── llm/
    ├── base.py
    ├── router.py
    ├── openai_compatible.py
    ├── claude_compatible.py
    └── gemini.py
```

验收：

- 一个完整轮次可生成 0～N 条原子记忆；assistant 未经用户确认的建议不能写成事实。
- 显式 remember 跳过 worth gate，但仍执行整理、查重和 scope 归属。
- 同一事实重复处理不重复写；状态变化时旧版本进入 history，新版本成为唯一活跃状态。
- 任一模型调用、解析、knowledge 写入或索引更新失败时不推进水位、不删除 inbox。
- 成功轮次经过 24 小时安全期后，仅在下一次自然触发时清理。
- 达到阈值时只选低优先级约 30% 为候选；原始记忆移入 history，不做机械删除。
- `api` 模式覆盖 OpenAI-compatible、Claude-compatible、Gemini；新安装按用户决策将密钥直接写入权限为 `0600` 的本地配置，旧 `api_key_env` 仅兼容读取。
- `host` 使用显式注入的宿主回调；stdio MCP 无宿主回调时返回可诊断的不可用状态，`auto` 仅在已配置 API 时回退，禁止静默选择未知远程模型。

## 阶段 C：MCP、初始化与宿主适配

交付：按需 stdio MCP、一次性 init CLI、Codex/Hermes 探测与配置适配器、兼容保留的 Antigravity 适配器、示例、CI 与打包。

建议模块：

```text
src/memleaf/
├── mcp_server.py
├── cli.py
└── adapters/
    ├── base.py
    ├── codex.py
    ├── hermes.py
    └── antigravity.py
```

MCP tools：`capture`、`context`、`search`、`read`、`process`、`remember`、`forget_memory`、`forget_about`、`rebuild_index`、`stats`。

验收：

- `python -m memleaf.mcp_server` 可通过 stdio 完成 initialize、tools/list、tools/call；stdout 只输出协议消息，日志写 stderr/文件。
- 每个 MCP tool 只是 core API 的参数校验和结果封装，不复制业务规则。
- `memleaf init --all --defaults` 可重复执行且不重复写配置。
- Codex 适配遵循官方当前配置：本地 STDIO server 通过命令启动，配置位于用户级或受信任项目级 `config.toml`；自动修改外部配置前先备份并采用原子写入。
- Codex/Hermes 只在真机探测结果可靠时自动配置；Antigravity 在 v0.1 不检测、不配置（包括 `--all`），仅保留兼容适配器代码。
- 构建 wheel/sdist、全量单元测试、MCP 端到端 smoke test 均通过。

## 待办与问题记录：Hermes 重连后自动捕获与提炼中断

- 状态：Hermes 已验证连续多轮自动 capture→process→knowledge 写入；30 秒模型请求超时、gate/summarize 契约与白名单校验诊断、DeepSeek 结构抽取与空内容有限重试自动化修复完成。下一次真实 cron 禁用验收与干净 HOME 安装验收仍待执行；历史缺失轮次不会自动回填。
- 真实验收曾覆盖首轮 gate+summarize 成功写入，以及后续模型空内容时保留 inbox 和处理水位；当前 `diagnostic_logging=false` 且不会默认创建诊断文件。
- 现象：`auto_process=true`，首段对话捕获了 5 个完整轮次（10 个事件），但 Hermes 在 14:39、14:41、14:43 完成的后续轮次没有继续写入 inbox；`knowledge/` 和 `history/` 均未生成记忆。
- 证据：目标 inbox 文件最后更新于 14:30:13；`_index/processed.json` 仍停留在第 1 轮 `processing`，没有成功处理水位；Hermes 日志记录 14:19:59 的 `auto-process failed`。Hermes 自身模型请求均成功，MCP `stats` 入口可用。
- 初步根因：Hermes provider 的 MCP 超时配置为 5 秒，首轮处理在超时后留下未清理的 processing 标记；随后 UI/provider 重连后的完整可见轮次未恢复自动捕获/处理。当前 provider 对底层 MCP 异常只记录摘要，无法直接区分超时与重连生命周期故障。
- [x] 修复 MCP 调用超时、失败重试和 processing 标记恢复，确保模型处理较慢时 inbox 与水位仍可安全重试。短请求默认仍为 5 秒，`process` 独立使用默认 300 秒（上限 900 秒）；超时会关闭坏 MCP 子进程，下一次调用重建连接；processing marker 写入 `owner_pid`，死亡 owner 立即恢复，旧 marker 仅保留 10 分钟兼容窗口。
- [x] 确保 Hermes UI/provider 重连或恢复同一 session 后，每个完整可见轮次仍会捕获并触发自动提炼。`on_turn_start` 的 turn number 与 user+assistant 可见文本短哈希共同组成 turn_id；provider 仅保留有界 user 指纹队列，重复完整轮次复用相同 ID 幂等；`on_session_switch` 只在 reset/rewound 时清理旧会话状态。
- [x] 增加可诊断日志，记录 capture/process 的阶段、耗时和错误类型，但不得记录对话正文或密钥。覆盖 initialize/stats/capture_user/capture_assistant/process/context，并区分 TimeoutError、MCP tool error 与子进程退出。
- [x] 修复自动提炼模型调用的内部 HTTP 30 秒超时与不安全错误透传：`llm.request_timeout` 默认 120 秒、范围 1～240 秒；失败按安全 code/stage 透传到 MCP、日志和 processing marker，失败后保留 inbox、不推进水位并可重试。
- [x] 修复 DeepSeek/OpenAI JSON mode 与 gate/summarize 契约：受支持 provider 使用 JSON mode 和固定 `max_tokens`；DeepSeek gate/summarize 关闭 thinking 以避免结构抽取被推理预算占用，严格契约失败最多重试一次，空内容额外允许第 3 次；最终失败仅透传白名单 `validation_reason`、`attempt_count`，不记录模型原始输出。
- [x] 对齐 gate/summarize 的类型、scope、todo、证据 event_key 与 reason 约束；失败增加白名单 `validation_detail`，并支持默认关闭、开启后有界 `logs/model-diagnostics.jsonl` 的结构统计诊断，不记录 prompt、正文、字段值、密钥、URL 或异常文本。
- [x] 按 Hermes lifecycle 同时识别 `platform` 与 `agent_context`：`platform=cron` 以及 `agent_context=cron/flush/subagent` 不召回、不捕获、不执行 process；正常 primary 交互会话保持原行为。
- [x] 保留已有 Hermes cron 历史数据；上述边界只阻断后续非用户会话污染，不删除或回填既有记录。
- [x] 连续多轮 Hermes 交互自动 capture→process→knowledge 已由隔离真机会话验证。
- [ ] 在干净 HOME 完成安装验收，确认上述自动链路无需手工调用 `process`/`remember`。
- [ ] 用下一次真实 Hermes cron 运行验证其不再触发 context、capture 或 process，且不影响正常交互会话。

阶段自动化曾覆盖完整 unittest、compileall 与 diff 检查，包括慢 process 超时边界、结构化输出重试、模型错误分类、重连、进程恢复、白名单诊断、敏感日志和宿主 Hook 状态。最终发布结论以当前 CI 和发布审计为准。

## 本任务状态：Codex 自动宿主 Hook；Antigravity 兼容代码暂不启用

- [x] 保持 v74 Core API、MCP 与 vault 数据骨架不变；新增共用 host-event 入口，复用 capture/context/process。
- [x] 可靠检测到 Codex 时由 `init` 默认配置 Hook；配置合并可重复、先备份、原子写入，命令使用安装解释器绝对路径。Hermes 使用其原生 provider/MCP 接入。
- [x] 保留 Codex/Antigravity 可见内容 host-event 兼容代码；v0.1 `init`/安装不检测、不配置 Antigravity，过滤与幂等逻辑不改变 Core/MCP 骨架。
- [x] 正常 Stop 同步 process；失败保留 inbox 与待处理状态，Hook 错误不阻塞宿主结束。
- [x] 使用脱敏合成 fixture 完成定向与全量单元测试；Codex 真机已验证可见捕获、process 与 context 调用链。安装后 Codex 状态先为 `pending_user_review`，用户需打开 `/hooks` 审核；首个合法成功 Hook 后记录 `active`/`trusted`。
- [x] Antigravity 适配器及其官方 Hook 解析代码继续保留作兼容性测试，但 v0.1 安装不会写入 Antigravity MCP/Hook 配置，也不会声称其已接入。
- [ ] Codex 非空跨宿主记忆召回专项验收；Antigravity 接入与真实宿主 E2E 延后到后续版本。
