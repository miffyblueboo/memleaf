# 任务办结与永久遗忘

MCP `update_memory` 用于完成、取消或重新打开现有事项。它调用 Core 的 revision-bound 更新接口，保留原正文、未修改字段及历史版本，不调用模型。任务办结不等于删除记忆。

先通过 `search` / `list_todos` 和 `read` 确认目标。使用 `read` 返回的 `revision`，而不是分页用的 `version`：

```json
{
  "memory_id": "mem-example",
  "expected_revision": "<read 返回的 revision>",
  "patch": {"status": "completed"}
}
```

- 已完成或关闭的事项使用 `completed`，放弃的事项使用 `cancelled`。
- 本 MCP 接口仅接受 `patch.status`，不修改正文、类型或范围。
- 重新打开事项使用 `patch.status=active` 和 `reopen=true`，应有用户明确重新打开的要求。
- 只有明确知道事项的完成事件时间时才传 `source_time`（带时区的 ISO 时间）；完成时会保存为 `completed_at`。未知时省略，不使用当前执行时间补造完成时间。
- 相同请求可以安全重试，不重复创建历史。旧 revision 的不同修改会被拒绝，应重新读取目标。
- 混合了项目事实和待办的记忆，办结只改变状态，仍保留项目事实。

`forget_memory` 与 `forget_about` 永久删除选中的记忆及其历史，没有“删除后收档”的历史副本。两个 MCP 工具均要求 `confirm_delete=true`，缺失或不是布尔值 `true` 时服务器拒绝执行。

这个参数只能表示调用方确认用户明确要求永久遗忘。完成、关闭、取消或归档任务不能作为设置它的理由。布尔参数本身不是用户自然语言授权的证明，也不能保证模型永远正确选择工具。需要撤销任务完成状态时，应通过 `update_memory` 重新打开；已永久删除的内容不能用此接口恢复。

这是 MCP 删除接口的兼容性变更：旧客户端应只在已有明确永久遗忘要求时补传 `confirm_delete=true`。Python Core 的 `forget_memory` / `forget_about` 原有删除语义及参数保持不变。
