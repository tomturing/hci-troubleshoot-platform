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
