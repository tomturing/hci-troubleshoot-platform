# KBD data-pipeline 日志契约

这份契约面向两类读者：终端操作者需要在几十秒内知道“做了什么、结果是什么、下一步做什么”；
排障人员需要用稳定字段从 JSONL 还原一次运行。两者不是同一份输出，因此终端只保留决策信息，
JSONL/文本文件保留可检索上下文。

## 第一性原理

1. **状态必须可区分**：`未安排`（任务计划没有选择）、`无需执行`（已满足幂等条件）、
   `完成`、`失败`、`需复核`、`前置阻断` 是六种不同事实。任何“完成 + 全 0”都必须解释为
   未安排/无需执行，不能用成功语义覆盖它。
2. **结果必须与粒度一致**：Vision 的图片计数（`images_done/images_failed`）不能直接当作
   KBD 案例状态；案例状态来自数据库最终结果，并记录不一致事件。
3. **错误必须可行动**：错误记录至少包含 `support_id`、`stage`、稳定 `error_code`、
   `retryable`、底层 `error_detail`（已脱敏/截断）和建议动作；异步任务还必须包含 `job_id`。
4. **关联必须无猜测**：每条日志自动带 `run_id` 和 `trace_id`。同一次调用跨 data-pipeline 与
   kb-service 使用同一个 trace_id；CLI 直接给出 `rg`/`less` 命令。
5. **信息要有决策价值**：轮询心跳只有状态或进度变化时提升到 INFO，重复心跳降为 DEBUG；
   单条案例的逐项进度只在实际有任务时输出。

## 稳定字段

| 字段 | 含义 |
| --- | --- |
| `run_id` | 一次 CLI 执行的本地运行编号 |
| `trace_id` | 跨进程关联编号；查询 kb-service/Tempo/Loki 时使用 |
| `support_id` | 用户可识别的 KBD 案例编号 |
| `stage` | `fetch/import/classify/vision/extract_signals/review_signals` |
| `job_id` | 异步 Vision/Signal Job 编号 |
| `error_code` | 可供重试/告警脚本依赖的稳定码 |
| `error_detail` | 服务端返回的具体原因（脱敏并限制长度） |
| `retryable` | 是否建议修复外部暂态后重试 |

## 严重级别

- `ERROR/CRITICAL`：本次运行无法完成或需要立即处理；终端红色显示。
- `WARNING`：跳过、重试、需人工复核或质量风险；终端黄色显示。
- `INFO`：阶段边界、状态变化和最终摘要；不重复打印无决策价值的心跳。
- `DEBUG`：轮询心跳、异常堆栈和 SDK 细节；写入 `.log`，终端仅在 `--verbose` 显示。

## 操作者判断顺序

先看完成摘要的“总体结果”和每阶段状态；看到 `未安排` 不要重试，先检查任务模式/阶段计划。
看到 `失败/阻断`，先按摘要给出的 `error_code`、`job_id` 和 `error_detail` 定位；暂态错误才用
`task --failed`，前置阻断必须先修复上游。最后才用同一 `run_id` 的 `.log` 查看 DEBUG 堆栈。
