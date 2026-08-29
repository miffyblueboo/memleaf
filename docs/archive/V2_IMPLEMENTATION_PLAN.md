# v2 检索与记忆维护实施计划

日期：2026-08-28。依据用户批准的《memleaf-记忆检索与注入线路设计-最终版-v2(2).md》及补充边界。

## 范围

- 保持 v74 的本地 Markdown Vault、Core、MCP、宿主适配分层。
- v0.1 仅接入 Hermes；Codex、反重力不检测、不安装、不配置、不扫描模型。下文 Codex 实施记录作为历史保留，不属于本版交付范围。
- 本轮仅修改工作区；不重新安装 Codex、不部署 `$HOME/memleaf`、不修改真实 Vault、不提交或推送 Git。
- 保留既有工作区修改和历史 knowledge。本次不批量清理、自动 forget 或扩大 MERGE/SPLIT 维护范围。

## 1. 检索协议

正常可见用户消息是一个逻辑轮次。工具回调、子代理、定时任务、内部维护、空消息，以及 Stop 纠正续跑，不算新用户轮次。

流程：Scope 清单 → Agent 按完整会话选择 Scope/Query → 至少一次 search → 按需 read → 回答。

自动注入仅含 Scope ID、父级、别名与必要协议提示，不含 memory_id、记忆标题或正文。不另加模型门控，不由 memleaf 拼接宿主会话历史。清单未覆盖全部时明确提示分页，不静默丢弃其他 Scope。

| 接口 | 新行为 | 预算与兼容 |
| --- | --- | --- |
| `scope_catalog(cursor?, limit?)` | `scopes:[{scope,parent,aliases}], has_more, next_cursor` | 每页最多 20 项、2000 字符；Scope ID 不截断 |
| `search(query, scope?, cursor?, limit?)` | 默认 `status:found/no_match, results:[{memory_id,title,scopes}], has_more, next_cursor` | 每页最多 20 项、4000 字符；不再受旧 context 的 3 项限制 |
| `read(memory_id, offset?, max_chars?, expected_version?, retrieval_id?)` | 保留版本化分页正文 | 每页最多 2000 正文字符；受管理轮次累计最多 3 个 ID、6000 字符 |
| Python `search(view='full')`、旧 `context` | 保留兼容调用 | 不用于新的自动注入；显式兼容不是自动读预算绕过方式 |

补充约束：

- 默认只检索 active；旧版本仅显式历史检索。搜索不增加正文命中次数。
- `error` 是失败，不能包装成空列表或 `no_match`。Scope 明显冲突返回 `scope_mismatch`；不猜测代词指向。
- 分页须稳定、可继续；失效游标报错并要求重新查询，不能无提示漏项。
- 标题只是候选线索，不得据此断言业务事实。默认先读最相关的一条，不为筛选无关项读全部。
- 不提供模型可自行开启的无限预算参数。超预算明确报告；更多阅读留给后续用户轮次。
- Hermes 插件运行在独立环境，不导入 memleaf Core。它通过 MCP `scope_catalog` 成组传入 `source=hermes/session_id/turn_id` 取得 `retrieval_id`；Scope 数据预算外仅增加这个固定长度控制标识。无身份参数的清单保持纯元数据结构。

## 2. 宿主约束与状态

| 宿主 | 实现方式 | 验收边界 |
| --- | --- | --- |
| Codex | UserPromptSubmit 注入清单；PreToolUse 为 search/read 绑定当前轮标识；PostToolUse 观察实际结果；Stop 有限纠正 | 只有宿主实际加载、信任并执行对应 hooks，才能称该路径 Hard Gate；不是任意工具路径的安全沙箱 |
| Hermes | 原生 MemoryProvider 继续 capture/process；prefetch 改 Scope 清单；系统协议要求 search，利用公开可用的结果观察 | Soft Gate：不宣称确定性阻止 final；缺失或失败必须可诊断 |

状态：`NOT_SEARCHED → FOUND / NO_MATCH / ERROR → DEGRADED`。

- FOUND/NO_MATCH 可放行；尚未搜索或 ERROR 最多触发 2 次 Stop 纠正，仍未解决则明确降级，避免无限阻塞。
- 实际工具结果才可确认搜索成功；按 tool_use_id 去重，错误、伪造标题、旧轮次结果不能充数。
- 纠正续跑不重复捕获用户消息、不刷新读预算；仅最终放行的可见助手答复进入完整回合处理。
- 门控和读预算存独立短期账本，原子写、独立锁、24 小时 TTL、最多 256 轮。只存标识/状态/计数，不存查询或正文；不混入 processed.json/knowledge。
- MCP 的轮次预算仅对有效宿主绑定标识强制执行。Codex 在已启用 PreToolUse 的受支持路径绑定；Hermes 为 Soft Gate。无宿主的旧显式客户端保留逐页上限，不伪称跨轮强约束。
- 不改宿主信任记录、不绕过官方授权、不读写私有宿主内部状态实现强制门控。
- Codex 的 Stop 控制的是轮次完成与纠正续跑，不是尚未生成任何文本之前的拦截器；不承诺撤回已经流式显示的文字。PreToolUse 只改写 memleaf search/read 的参数，不注册 PermissionRequest 自动授权。
- 已核对本机 Hermes：`on_turn_start` 先于 prefetch，但宿主会跳过问候/确认等简短消息的 prefetch。本轮不修改宿主规则；系统协议仍要求 search，此类回合的 Scope 注入与跨调用预算不能声称硬保证。

## 3. 提炼与维护

- 按未来复用价值准入，不按每个句子/工具过程建记忆。普通对话通常 0～1 条，多条必须对应独立未来用途。
- 每个候选按自身主题、可靠 Scope 检查相关 active，不能只用整轮一次粗检索。
- 决策优先 UPDATE / NO_CHANGE，再 CREATE。update_memory_id 只能引用相关 active 的同一事项。
- 同一事项甲→乙复用 ID，甲进入 history；同义复述、纯查询不追加来源、不制造相同 history。
- 标题稳定表达主体与主题，正文自包含，去除签名、临时路径、过程碎片与默认无用测试/审计总结。
- 项目归属不明不得猜测 global 后新建；安全暂缓，保留可重试来源，并明确状态，不把已失败或未处理结果报为成功。真正全局偏好允许 global。
- 模型格式错误继续有限重试；最终失败保留 inbox、水位和真实错误。不能直接写库伪装自动 process 成功。
- 本轮不删除现有记忆；历史清理仍采用候选清单供审核。

## 4. 分工

- Luna / retrieval_core：Scope 清单、候选分页、Scope 校验、检索回归。
- Luna / host_gate：Codex hooks、Hermes Provider、短期门控与正文预算、宿主回归。
- Luna / maintenance：候选级查重、UPDATE/NO_CHANGE、准入与 Scope 暂缓、维护回归。
- 主代理：MCP 接口、协议文档、跨模块审查与隔离验收。

## 5. 验收矩阵

- 清单注入不含任何具体记忆 ID/title/body；超过一页可发现并继续获取。
- 未选项目询问具体项目、别名、多项目切换、无匹配、明确 Scope 冲突、大候选集均有测试。
- search 正常/空结果/真实错误严格区分；只 read 返回正文；分页版本变化与预算耗尽安全失败。
- Codex 未搜索、搜索成功、no_match、连续错误、重复回调、跨会话、Stop 续跑和信任待审均覆盖。
- Hermes 不再自动取具体记忆；保留原生 capture/process，Soft Gate 证据不等同硬阻断。
- 甲→乙、查询乙、同义复述、独立项目 ID、多候选相关性、噪音混合业务事实、归属待定均覆盖。
- 执行定向测试、完整 unittest、真实 stdio MCP 隔离进程与宿主事件链路回放。
- 真实 Hermes / Codex 聊天验收单列；在未部署/未启用 hooks 时不得填“已通过”。

## 当前证据

- 修改前基线：`PYTHONPATH=src $HOME/memleaf/.venv/bin/python -m unittest discover -s tests`，300 tests，全部通过。
- 当前工作区完整回归：同一命令共 345 tests，全部通过；`compileall` 与 `git diff --check` 通过。
- 宿主/门控定向回归共 69 tests，覆盖 Codex continuation、实际搜索结果观察、失败降级、待补 Scope 提示、Hermes Soft Gate 与清单失败提示；短期账本 9 tests 覆盖 24 小时 TTL、256 轮上限、身份隔离及并发 3 ID/6000 字符预算。
- 真实 stdio MCP 隔离测试覆盖 Scope 清单、候选分页、Hermes 轮次标识、进程重连后共享预算、畸形结果与安全错误；宿主事件回放不是实际 Codex 聊天。
- 真实模型隔离回归：使用现有模型连接、仅虚构项目数据，甲→乙复用 ID，旧状态进入 history；第三轮纯查询写入 0 条，knowledge/history 字节保持不变。三轮 process 均成功。这不是 Hermes 聊天验收。
- 真正独立的 Hermes venv 加载检查通过：环境中没有 memleaf Core 包、无 PYTHONPATH，使用公开 MemoryProvider 接口成功导入工作区插件；未启动聊天、未修改宿主配置。
- 状态更新批次在 knowledge/history 已写而 processed 最终提交失败时采用前向恢复：重试识别已应用事件，不回放旧状态或重复 history。该实现不是跨整个 Vault 的文件系统事务，失败期间仍以 `processed=failed` 和保留 inbox 如实表示可重试状态。
- 工作区代码与隔离验收已完成；真实 Hermes / Codex 聊天仍须在后续部署、启用并信任对应宿主集成后单独验收。本轮未重新安装 Codex、未部署 `$HOME/memleaf`、未修改真实 Vault、未提交或推送 Git。

Codex hooks 协议依据：[OpenAI Hooks 文档](https://learn.chatgpt.com/docs/hooks)。必须以实际宿主版本和加载状态再验收。
