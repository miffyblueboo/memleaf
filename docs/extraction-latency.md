# 提炼耗时与请求超时

## 性能目标与失败条件分开

日常自动提炼以 10 秒内完成为性能目标，但到第 10 秒不会主动报失败，也不会丢弃已正常返回并通过 Core 校验的结果。超过目标说明本次处理偏慢，不说明记忆无效或没有需要保存的信息。

v0.2.42 / v0.2.43 的固定 6 秒首请求、8 秒准备加模型窗口、10 秒提交前拒绝属于错误的目标实现，当前修正移除了这些时间闸门。修正本身不代表真实模型的 P50/P95 已达标。

HTTP 请求使用已有配置：

```yaml
llm:
  request_timeout: 120
```

这个参数仍按现有规则校验（1～240 秒，可用小数），并传到 HTTP transport。single-pass 不再静默把它缩短为 6 秒。第二次结构修复请求使用相同的配置，不再只分到原来 8 秒窗口的剩余时间。请求超时是 transport 的等待限制，不是整个后台任务的端到端完成保证；host/Python callback 仍由其宿主负责请求超时与取消。

真正的请求超时、网络故障、无效模型输出、目标版本冲突、写入失败仍然是失败或未完成，不能伪装为 `NO_CHANGE`。相应 inbox、journal 和恢复锚点保留，是否提交仍由 Core 校验决定。

## 保留的调用与提交约束

普通 B3 自动提炼仍以一次结构化模型调用为正常路径，格式/结构修复最多一次。固定安全路由保留实际 outbound 请求计数和持久化预占，不增加隐藏 host-to-API fallback。并未恢复 Gate → Summary → Review 多阶段主路径。

每轮仍先读取持久状态、规划、校验、提交，再处理下一轮；一轮超过目标不会占用下一轮的“时间许可”。compaction 仍不在普通提炼的关键路径上。

旧 `extraction_request_budget.json` 中的 `started_at_epoch` 可以兼容读取，但不再作为请求或提交的过期时间。已消耗的 `requests` 不清零，损坏的计数账本也不会被当作空文件重建。FrozenTurn 的恢复不需要重新申请模型时间窗口；现有内容摘要、版本、所有权和写入校验保持不变。

这不是一个自动重放所有历史失败任务的迁移。旧 inbox 不会被清空；需要通过正常处理/重试入口继续处理，不能删除正确性状态来强行重新获取请求额度。

## 只记录结构化耗时

处理结果新增 `extraction_metrics`，后台 `process_status` 的 result/error 和尝试汇总保留同样的数字字段：

| 字段 | 含义 |
| --- | --- |
| `target_duration_ms` | 固定观测目标 10000 毫秒，不参与失败判断 |
| `turn_count` | 本次测量的 turn 尝试数量 |
| `failed_turn_count` | 抛出处理异常的 turn 尝试数量 |
| `over_target_turn_count` | 耗时超过目标的 turn 尝试数量，可能成功也可能失败 |
| `successful_within_target_count` | 无处理异常且在目标内结算的尝试数量 |
| `total_duration_ms` | 被测 turn 尝试的耗时之和 |
| `max_turn_duration_ms` | 最慢一次 turn 尝试耗时 |
| `planning_duration_ms` | 读取状态、准备上下文、模型调用和本地计划校验耗时 |
| `commit_duration_ms` | 从进入提交阶段到提交和预算清理返回的耗时 |

每轮计时从选中该 turn 后、读取其处理状态前开始。它不包括排队、worker 启动及 `process()` 开头的整批 inbox claim 扫描；不能把该指标冒充用户可见端到端耗时。跨 worker 重启会产生新的尝试，旧进程的 wall-clock 起点不再限制新进程执行。

“无处理异常”包括合法 `NO_CHANGE` 和正常返回的部分处理；语义完整性仍由既有 `coverage_status`、deferred 数量、`memories_written` 和任务状态判断。因此耗时统计不替代提炼质量验收。快速失败不计入 `successful_within_target_count`。

`model_metrics` 继续分别提供模型请求耗时、调用/修复次数和输入输出长度等信息。以上结构化统计不新增正文、prompt、response 或凭证存储。
