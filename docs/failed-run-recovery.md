# HTTP 失败与历史任务恢复

HTTP 传输层保留经过校验的数字 `http_status`，不保存响应正文、URL、请求头或密钥。
408、500、502、503、504 可在原预算仍有余额时进入 `retryable`；429 使用现有
限流处理。400、401、403、404、501 等不因新的 HTTP 分类而获得重试。
缺少状态码的历史 `model_http_error` 保持终态，普通 `process --recover` 不会重开它。

## 按 run_id 预览及执行

```sh
# 预览：只读，无模型调用。旧 HTTP 回执必须明确接受其状态码未知。
memleaf recover-run --vault /path/to/vault --run-id inc-run-... \
  --allow-legacy-http --json

# 仅当 recoverable=true 时，使用此次预览返回的完整 expected_revision。
memleaf recover-run --vault /path/to/vault --run-id inc-run-... \
  --allow-legacy-http --apply --expected-revision PREVIEW_REVISION --json
```

`--allow-legacy-http` 不放行已知的永久 HTTP 错误，也不增加预算。执行使用当前
Vault 配置的固定 API 模型路由；预览不解析模型路由、不初始化 Vault、不创建锁文件。
MCP 对应工具为 `recover_failed_run`，参数为 `run_id`、`allow_legacy_http`、
`apply`（默认 false）及 `expected_revision`。

Python API：

```python
preview = service.recover_failed_run(run_id, allow_legacy_http=True)
if preview["recoverable"]:
    result = service.recover_failed_run(
        run_id, dry_run=False, allow_legacy_http=True,
        expected_revision=preview["expected_revision"],
    )
```

## 恢复边界

- 只处理自动捕获任务的已知第一次传输失败，原请求预算为已用 1、尚有余额。
  显式 remember 的授权正文已清除时，不能借此重新取得保留授权。
- 原轮必须完整且可处理，来源摘要及原预算身份必须一致。来源被删除、修改、禁录，
  已有消费记录、自动提交、旧流程待提交计划或其他活动所有者时拒绝恢复。
- 原 run_id、budget_id、turn_budget_id、commit identity 和第一次尝试记录全部保留。
  执行在锁内绑定此次预览版本，并记录旧失败、旧来源窗口、旧快照及恢复时间。
  不修改原额度、不新建 run、不删除旧回执。
- 原请求正文已清除，因此执行根据当前记忆重新生成快照和请求。完整后续对话仅以
  `use=context` 参与比较，不独立创建新事项，也不随本轮结算。已有日期、来源及
  `stale_observation` 校验继续生效；不能证明安全的修改留下未解决项。
- 预览后状态、预算、来源或比较目标变化，需要重新预览。绑定后的调用前/提交前
  校验不允许再次悄悄更新快照。
- 第二次请求仍先预留再派发。崩溃后的未知调用保守计入预算；重复 apply 使用同一
  恢复绑定，完成后只返回旧回执。保存的回复/提交可在没有模型配置时恢复。
- compact 回执重开需要一个可用 full 槽位；容量不足时预览明确拒绝，不能删除
  回执来换取执行机会。

真实模型可能输出 NO_MEMORY、NO_CHANGE、UPDATE、CREATE 或未解决项；入口可执行
不等于模型语义正确，也不承诺所有历史内容都应保存。
