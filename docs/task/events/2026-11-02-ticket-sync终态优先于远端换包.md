# ticket-sync：人工放弃优先于远端换包（2026-11-02）

## 问题

两个独立合入的机制落在同一张 `retries` 表上，语义打架：

| 机制 | 来源 | 实现 |
|---|---|---|
| 远端换包自动重同步 | #1414 | 发现远端归档 `(mtime, size)` 与 `.sync-meta.json` 记录不同 → 清掉失败退避并重拉 |
| 放弃自动重试 | #1416 | 把 `retries.failures` 推到 `MAX_RETRY_ATTEMPTS`，`exhausted=True` 即"不再自动试" |

`create_auto` 在「本地已有产物 + 远端换包」分支里无条件 `_clear_retry()`，于是：

1. 用户在界面点「放弃自动重试」；
2. 客户重传了同名 zip（时间或体积变化）；
3. 自动扫描判定"远端换了包"，把放弃标记清掉并重新拉取。

更糟的是它同时绕开了 #1416 特意加的护栏：`_note_failure` 遇到 `exhausted` 会
"不改写退避状态"，但标记在失败**之前**就被清掉了，护栏拦不住。

## 决策（2026-11-02，方案 A）

**人工放弃优先于远端换包。** 理由：显式人工操作必须压过自动启发式，否则"放弃"
按钮在"该工单已有产物目录"的场景下就是摆设；而恢复成本很低（点一下"恢复自动重试"
或手动同步），这本就是"放弃"的含义。

## 实现

`create_auto` 在进入任何自动判据之前先看终态：

```python
final_dir = self.cfg.root / archive.ticket_id
if final_dir.is_dir():
    state = self._retries.get(archive.ticket_id)
    if state is not None and state.exhausted:
        return None          # 不重同步，也不清标记
    ...
```

- 覆盖两条清退避的路径：远端换包分支与"远端未变"分支（后者原本也会顺手把放弃标记清掉）。
- "重试用尽"与"人工放弃"共用同一判据，行为一致：都只在**人工**动作下恢复。
- 不改表结构；不改变手动 `POST /jobs` 的放行语义（它本来就不看终态）。

## 验证

```
cd tools/ticket-sync
PYTHONPATH=. uv run --frozen pytest -o addopts="" -p no:cacheprovider -q
# 150 passed
```

新增/改写的用例（`tools/ticket-sync/tests/test_auto_resync.py`）：

| 用例 | 断言 |
|---|---|
| `test_abandoned_ticket_is_not_resynced_even_if_remote_changed` | 放弃后远端换包 → 不入队，`exhausted` 仍在 |
| `test_abandoned_ticket_stays_terminal_when_remote_unchanged` | 远端未变分支同样不清标记 |
| `test_exhausted_by_failures_also_blocks_remote_resync` | 自动用尽与人工放弃同判据 |
| `test_resume_then_remote_change_resyncs` | 恢复后远端换包重新生效（放弃不是死刑） |
| `test_manual_create_ignores_abandon` | 手动点击不受放弃约束 |

## 现网影响

部署前实测：`retries` 表 2 条终态记录（`Q2026102900692`、`Q2026102802029`），
**两者都没有本地产物目录**，而本次冲突只发生在"目录存在 + 终态"的交集，
因此线上没有已发生的误重拉，属潜在缺陷的提前收口。

## 后续

宿主机 `hci-ticket-sync` 为 systemd 用户级部署，合并后需重跑
`tools/ticket-sync/deploy/install-user.sh` 生效。
