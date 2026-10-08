# Memleaf host-v2 协议与接入边界

host-v2 让宿主 Agent 使用自己的模型理解来源并提交结构化提案；Memleaf 做确定性校验、持久化和恢复。Memleaf 不调用独立模型、不寻找模型 Key，不使用 sampling 或隐藏模型兜底。协议版本为 `memleaf-host-v2.0-rc1`，保留 RC1 消息结构，并补齐下述兼容规则。运行时支持范围以 `memory_capabilities` 为准，合同资产存在不代表所有能力已经实现或验收。

## 首次配置与每次使用

本地 owner 配置一次 Vault、读写 scope、自动记录策略、可接受的来源信任和权限。MCP 启动凭据用于绑定 principal、authorization domain、grant 和 epoch，属于本地服务身份凭据，不能替代或冒充用户决定，也不是模型 API Key。默认不支持用模型传来的 `principal_id`、scope 声明或 approval 文字取得权限。同一操作系统账户的终端权限不属于该凭据能隔离的威胁边界。

一次普通工作按以下顺序执行：

1. `memory_capabilities` 获取动作、来源模式、大小限制和验收状态。
2. `prepare_memory(mode=new)` 将完整可见来源绑定为一个 work。`automatic` 使用完整可见 user/assistant 轮次；`explicit` 可使用明确保留要求的 user 内容。原始工具输出、邮件、附件和隐藏指令不能伪装成可见对话。
3. `memory_work` 分页读取真正需要的 sources/targets。目录和标题只是线索，不能据此虚构正文。来源确认与写权限分别检查。
4. 宿主一次生成覆盖所有来源的提案，`submit_memory` 提交稳定的 item ID、snapshot、work revision 和 request ID。证据 quote、字段依据、目标 revision 与范围必须匹配。
5. 查看累计 receipt。只有 `applied=true` 且 `settled=true` 的保存项才证明已保存；`DEFERRED` 表示需要后续依据。
6. 对未解决项在同一 work 内刷新 snapshot 并修正，不重交已完成项。冻结的 I/O 中断调用 `resume_memory`；恢复不重新推理，不产生模型调用。放弃未写项使用 `cancel_memory`。

显式要求记住的内容不得用 `NO_MEMORY` 丢弃，运行时返回 `EXPLICIT_RETENTION_REQUIRED`。已有同义内容用 `NO_CHANGE`；有新增或变化用 `CREATE`/`UPDATE`；缺少安全写入依据用 `DEFERRED`。普通自动来源允许有充分理由的 `NO_MEMORY`。`purpose` 位于已持久化的 work，而不是 submit 消息，因此该限制是跨消息的运行关系，不能只靠 submit Schema 表达。

## 内容类型与行动属性的兼容修订

`type` 描述内容类别，`actionable` 独立描述这条记忆是否为一个可跟踪行动；项目、事件和其他类型都可携带行动状态和责任字段。`todo` 创建仍需 `status`。`CreateMemory`、`FieldPatch`、`ReadFields`、`FieldEvidence` 均包含 `actionable` 与 `waiting_on`。不将非 todo 一律降级成不可行动，也不因正文出现一个人名或日期就推断 actionable。

`assignee` 表示执行负责人，`waiting_on` 表示当前依赖方；一个依赖不自动产生用户的个人待办。清空这两个字段必须明确提交 `null` 并提供支持清空的依据；UPDATE 省略字段继承原值。来源没有明确责任或日期时保持未知。宿主不能把自己的建议、执行结果或报告当作用户批准、负责人变更或状态决定。

Schema 仅接受字段类型和白名单；业务校验仍需核对 actionable 与状态一致性、来源证据及历史状态。例如把 `project` 改为一个独立行动需有明确的行动/责任依据，不能仅设置通用 `active`。已有额外 metadata 在普通更新中继承，不能静默删除。

## 状态、身份与预算

work/root source 身份由服务绑定。短 source/target/fragment ref 只在对应 work/snapshot 中有效，不是授权 token。caller_asserted 的来源去重基于角色和文本，换连接、event ID、request ID 或 scope 不能产生新的语义预算；没有新来源或 owner 授权，不能把失败 work 拆成新 work 重试。

完整轮次可能同时包含已处理来源和真正的新来源。例如 user 的显式保留已完成，稍后自动处理包含同一 user 与新 assistant 的完整轮次：新 work 通过 `related_work_id` 关联旧 work；旧 user descriptor 带可选 `covered_by_work_id`，仅作为完整可见上下文，不再归入新的 coverage。coverage 只映射尚未被覆盖的新来源，旧 source 的 owner、旧 work 的预算和已落盘项保持不变。新增预算必须以真正的新来源为依据；重复同一完整来源集合返回该关联 work，不再创建新的预算根。

每个 work 默认最多 3 次被接受的 submit。服务应先检查可路由请求壳、当前授权、work CAS 和 request 幂等性，再预留预算并校验完整 item 结构。错误 item、证据或目标 CAS 消耗已接受的尝试；壳错误、权限错误和 work CAS 失败不消耗。`validate_request(..., routable_only=True)` 保留 items/coverage/basis_coverage 的存在要求，但故意不预先校验其完整内容，避免无效提案绕过预算。

同一 request ID 与同一规范 JSON 重放相同累计结果；换 payload 返回 `IDEMPOTENCY_CONFLICT`。重放和分页前仍检查当前权限。已完成 item 不得修改，冻结 operation 的 payload 不得改变。部分成功时保留完成项，只修正失败项；冻结失败要先恢复或确认未落盘后取消。

`captured/pending/submitted/partial/completed/failed/cancelled` 与 `terminal` 分开表达：partial 可修正，也可预算耗尽终止。`applied=null` 代表无法确认落盘，不能伪装成成功或干净取消。receipt 必须区分 prepared、recovery_required、saved、unchanged、not_recorded、cancelled 和 invalid。

## 读、更新与恢复

所有 work view、cursor、sources、targets、proposal、receipt 和幂等重放都受当前 principal/grant/epoch 与相应读权限约束。cursor 绑定过滤条件、view、work/snapshot/revision；每页重查权限。字段更新需读取对应字段与 revision；正文局部补丁需完整读取引用片段，唯一 old_text、无重叠、同一 base revision。全文替换不能通过局部补丁入口绕过维护批准。

写入基于授权锁、target CAS、冻结 payload 和耐久 operation marker。history 写入先于 head；中断后仅依据 before/after revision、marker 和 history 恢复。完整 after 已写时只补账，不再写同一 history/head；第三版本、缺失 marker 或损坏状态失败关闭，不能凭当前正文猜测成功。

撤权与知识写入共用 mutation lock，持久化 epoch 和 barrier 后才报告撤权成功。撤权后 resume 不得新增知识或 history；已落盘但未结算项只由 owner/service 启动进行核验和补账，部分 merge 留隔离 barrier。重新使用相同名称或 grant 不能复活旧 work。恢复冻结操作需 owner 的精确 authorized_recovery 批准，继承原 operation 身份和预算；新语义预算要单独授权。

## 维护与删除

普通可靠动作是 CREATE、单目标 UPDATE、NO_CHANGE、NO_MEMORY、DEFERRED。完整 v2 还包括有范围权限和故障恢复的 MERGE、撤回/恢复、维护 COMPACT、旧数据与 Hermes 兼容、真实多 Agent 验收。能力未实现时明确返回 `ACTION_NOT_SUPPORTED`，不能静默换操作。

MERGE 保留参与内容的确定性并集，只有完全相同内容去重；字段冲突保留未知或要求明确用户解决。COMPACT 需完整依据与精确 owner approval；所有旧片段都有 mapping，不能为无来源的新内容背书。`forget_memory` preview 形成精确目标和依赖计划，apply 核验当前计划 revision 与 owner approval，先取消/阻断相关冻结提案再删除 head、history 和待执行 plaintext，不能用删除消除不确定 group 的其他 barrier。原始共享来源、备份和日志不是默认附带删除范围。

## 合同资产与验证范围

`src/memleaf/host_v2_contracts/` 提供 7 个独立 MCP 工具的 input/outputSchema、协议 registry、admin-records 和 read-integrity Schema。结构样例和测试留在本地开发目录，不随公开源码或安装包发布。`host_v2_schema` 提供不增加运行依赖的确定性 shape validator；它仅实现这些合同使用的 Schema 关键字，遇到未支持关键字失败关闭。开发环境可用 jsonschema 对照检查；不要求普通用户安装 jsonschema 或运行开发诊断。

部分宿主把 `$ref` 或 `oneOf/anyOf` 对象分支显示为 `unknown`。`tool_definitions()` 因此仅对客户端展示的 inputSchema 生成确定性、无引用的平铺超集：合并分支 properties，取 required 交集，保留字段类型、枚举和最宽分支的长度/数量界限；工具描述明确各 action 的必填项。输出 Schema 保留精确关系。该展示适配不能代替 Core 合同校验：`validate_request()` 始终读取原始资产，保留完整分支、来源、权限和预算规则，超集中不符合实际 action 的请求仍拒绝。Schema 错误只反馈已知字段路径和合同要求的缺失字段，不回显提交值或未知输入字段名。

shape 测试只证明字段类型、分支、枚举、长度和拒绝形状，不能证明授权、来源真实性、状态机、故障恢复或真实模型行为。各阶段分别报告：合同结构测试、确定性 Core/故障注入、真实 Agent 端到端、Hermes 回归及跨平台验证。接受真实闭环时记录 Memleaf 模型调用次数（应为 0），宿主可取得的调用次数/token；取不到的数据保持 null，不写成 0。

## 本地所有者入口

`host-grant --vault PATH --principal NAME --accept-caller-asserted --json` 创建客户端授权并输出 MCP 配置，不输出凭据正文。默认范围为 global；`--read-scope`/`--write-scope` 可重复，维护和永久删除权限分别由 `--allow-maintenance`/`--allow-delete` 显式授予。客户端启动时传 `--profile host --token-file PATH`；普通工具参数不能传 token 或审批授权声明。

严格模式用 `host-source-confirm --grant-id ID --messages-file PATH --vault PATH --json` 确认规范消息数组，再由客户端用 returned source IDs 的 bound 来源准备工作；未提供真实时间仍保持未知。`host-revoke` 立即使旧凭据失效，`host-account` 只核对已落盘内容或取消明确未写项。冻结恢复先 `host-recovery-plan`，再对精确 digest 执行 `host-approve --kind resume_after_revocation --plan-digest DIGEST --work-id ID --grant-id NEW_GRANT`，客户端据 approval_ref 准备 authorized_recovery 子工作。

预算耗尽后的再次语义处理是独立授权：`host-processing-plan` 获取当前精确计划，再 `host-approve --kind new_processing_budget`。该审批建立拥有新预算的唯一关联子工作，继承已成功项；原预算不减，原来源 owner 不变。pending frozen I/O 必须先恢复/核账，不能借新语义预算绕过隔离。

删除和压缩审批分别绑定 `permanent_delete`/`body_compaction` 的精确计划摘要及授权。旧历史所属 scope 也必须获准删除；预览后新增历史或依赖使原计划失效。永久删除的范围包含当前版本、相应历史和引用它的待执行 plaintext，不连带删除原始共享来源或用户备份。

客户端兼容层将工具 Schema 的本地 `$ref` 内联并展示联合分支的构造字段。Core 继续用原始合同做严格校验，不因客户端显示限制放宽输入。`memory_capabilities.acceptance` 是该安装的证据状态；源码本地测试结果分别记录在开发目录，不能把 capabilities 的静态值当作实时验收证明。
