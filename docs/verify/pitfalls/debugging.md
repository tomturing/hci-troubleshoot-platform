# 通用排查原则

## 原则一：先确认进程/服务真正在跑

不要假设服务在运行，先 `ps aux | grep <name>` 或 `systemctl status` 确认。

## 原则二：先看日志末尾，再看全量

`journalctl -u <service> -n 50 --no-pager` 或 `kubectl logs --tail=50`，定位最近一次错误。

## 原则三：区分"历史错误"和"当前错误"

重启次数多（如 RESTARTS > 10）不代表当前异常，看"最后一次重启时间"和当前状态。

## 原则四：网络类问题先排除代理/防火墙干扰

容器/K8s 网络异常时，第一优先检查是否有 VPN/代理（如 Clash TUN）劫持路由，再查 NetworkPolicy、iptables 规则。

## 原则五：前端报"internal error"时的三步定位法

**第一步：先读避坑指南确认排查方向**（CLAUDE.md 强制要求，不可跳过）
- 读 debugging.md 原则一~四：先看日志/进程、区分历史错误
- 读 network-service-check.md：排除网络/Clash TUN 等基础设施故障，再聚焦代码逻辑层

**第二步：全仓库精确搜索错误文案，定位源头**
```bash
# 用报错原文搜索代码，找到是哪个文件/行号生成这条消息
grep -RIn "internal error\|Error: internal error" backend/ frontend/ adapters/ \
  --include="*.py" --include="*.ts" --include="*.js"
```
这一步能直接命中根源文件，避免盲目翻日志。

**第三步：顺数据流往上追调用链（结合运行时日志验证）**
1. Pod 全 Running → 排除崩溃，是运行时/代码逻辑错误
2. 从源头服务日志确认错误触发：`kubectl logs --since=2h | grep -v "GET /health" | grep -i "error\|400\|500" | tail -30`
3. 顺 SSE/HTTP 数据流逐层检查：下游服务 → 中间层（conversation-service）→ api-gateway → 前端
4. **特别注意**：错误信息可能不是通过 `event:error` SSE 帧发出，而是混入普通 `data:` 帧的 assistant 内容里，后端直接透传，前端无感知地追加显示 —— 此时运行时日志层看不到 4xx/5xx，必须读源码数据流

## 原则六：K8s ConfigMap subPath 挂载会导致目录 root 权限

ConfigMap 以 subPath 方式挂载单个文件时（如 `/home/node/.openclaw/openclaw.json`），
Kubernetes 会以 root:755 创建父目录 `/home/node/.openclaw/`，导致非 root 进程无法在该目录下创建子目录。

现象：`EACCES: permission denied, mkdir '/home/node/.openclaw/workspace'`

解决方案：用 busybox initContainer 代替 subPath：
- ConfigMap 挂到只读路径（如 `/etc/app-config/`）
- initContainer 执行 `mkdir -p + cp + chmod`，写入 emptyDir
- 主容器从 emptyDir 读，进程拥有完整写权限

---

## 高频场景：工单创建 500 / `case.close_reason` 字段缺失（迁自 network-service-check.md）

**现象：**
- 前端提示：`创建工单失败: Request failed with status code 500`
- `api-gateway` 日志：`POST /api/cases/ ... 500`、`JSONDecodeError: Expecting value`
- `case-service` 日志：`UndefinedColumnError: column "close_reason" of relation "case" does not exist`

**根因：** 应用代码已升级依赖 `case.close_reason` 字段，但数据库**未执行** `database/migrate_evaluation_v1.sql`。

**快速修复：**
```bash
cd /aihci/hci-troubleshoot-platform
cat database/migrate_evaluation_v1.sql \
  | kubectl exec -i -n hci-troubleshoot postgres-0 -- psql -U hci_admin -d hci_troubleshoot
```

**验证（期望返回 201）：**
```bash
python3 - <<'PY'
import json, urllib.request
u = 'http://127.0.0.1:4888/api/cases/'
p = {"title": "回归验证", "description": "回归验证",
     "client_id": "client-regression-verify", "assistant_type": "productionclaw"}
req = urllib.request.Request(u, data=json.dumps(p).encode(),
      headers={'Content-Type': 'application/json'}, method='POST')
with urllib.request.urlopen(req, timeout=8) as resp:
    print(resp.getcode())
PY
```

**预防：** 每次发布后校验 `case.close_reason` 字段是否存在；将数据库迁移纳入发布门禁。

---

## V-004：命令输出形态变化后不能复用旧行筛选条件

**症状：** 命令在 terminal_bridge 日志中 `exit_code=0` 且 `stdout_raw_len>0`，Agent 却报告 `QFK_OUTPUT_EMPTY` 或 `QFK_NO_MATCH`。把命令从 `ps -p PID -o pid=,args=` 改为 `ps -p PID -o cmd=` 后尤其容易出现。

**根因：** 只修改了命令，没有同步审查 `extract.include/exclude/column/cardinality`。`cmd=` 输出只含命令行，不含 PID；旧的 `include=[PID]` 会在边缘安全筛选阶段丢弃唯一一行。错误中的“stdout 为空”指筛选后逻辑流为空，不代表远端物理 stdout 为空。

**修复：** 逐层核对 remote exit code、raw stdout、filtered stdout 和 extractor 结果。对于 `ps -p {{PID}} -o cmd=`，由命令参数完成 PID 定向，extract 使用空 include/exclude、`exactly_one + whole` 取得 CMD。

**预防：** 命令参数或输出列发生变化时，把整套 extract 视为同一个契约重新评审；golden test 除 PASS 状态外必须断言实际产出值，并保留旧筛选条件的失败反例。禁止运行时静默忽略人工配置的 include。

---

## V-006：QFK 产出变量模式不得被 Matcher 前置校验短路

**症状：** Bridge/远端命令已经返回 `exit_code=0`，筛选后的 stdout 也能按 KBD 的 `produces` 规则取到值，但 `tool_result` 却失败为 `QFK_MATCHER_MISSING: QFK 判定必须配置新版 match.extract`；下游信号随后显示 `blocked_dependency`，例如缺少 `PID`。KBD27123 的 `lsof -> PID -> ps` 链在 `Q2026080323483` 中即出现该现象：lsof 从约 413462 行筛出两行，现场复放可取得 `PID=8369`，但变量池没有该值。

**根因：** QFK 有两种不同的语义：`match` 模式用于判断证据是否命中，`match=null + orchestrate.produces` 的 producer 模式用于从成功输出中产出变量。若 QFK 引擎在命令成功后无条件要求 `matcher`，producer 会在调用提取函数前被错误短路。此时不能依据工具总状态推断远端命令执行失败，必须同时检查 remote exit、筛选后输出、QFK 引擎错误和变量池。

**修复：** 由调用方显式传入 `execution_mode="produce" | "match"`。producer 成功时返回受输出上限保护的完整结果给 `produces` 提取；仅 match 模式要求 matcher。调用链必须在 producer 成功后执行变量池填充，并将“命令执行”“结果处理/变量提取”“下游依赖”作为独立状态记录和展示。

**预防：** 为完整链路建立端到端测试：执行 `qfk_system lsof`、按 VM 字面量筛选、从第 2 列提取 PID、再执行 `ps -p {{PID}} -o cmd=`。测试同时断言实际 PID、变量池内容、下游调度和各阶段状态；不能只覆盖 `_fill_pool_from_qfk()` 等局部辅助函数。保存/发布校验必须把 producer 与 matcher 作为互斥的、均可执行的模式验证。
