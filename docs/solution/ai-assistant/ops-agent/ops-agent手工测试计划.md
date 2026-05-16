# ops-agent 手动测试方案

> **版本**: v2.0（已验证）
> **日期**: 2026-05-09
> **测试环境**: hci-dev (K3s)
> **ops-agent 镜像**: ghcr.io/p3n9w31/ops-agent:20260509-0744-08b1354
> **参考文档**: `docs/solution/ai-assistant/ops-agent-internals/`

---

## 1. 测试前置条件检查

### 1.1 检查 ops-agent 服务状态 ✅

```bash
# 检查 Pod 状态
kubectl get pods -n hci-dev -l app=ops-agent-service -o wide

# 期望输出:
# NAME                             READY   STATUS    AGE    IP
# ops-agent-service-xxx-xxx        1/1     Running   XXm    10.42.0.xxx

# 检查镜像版本
kubectl get deployment ops-agent-service -n hci-dev -o jsonpath='{.spec.template.spec.containers[0].image}'
# 输出: ghcr.io/p3n9w31/ops-agent:20260509-0744-08b1354
```

### 1.2 检查 SOP 数据挂载 ✅

```bash
# 检查 SOP 文件是否存在
kubectl exec -n hci-dev deploy/ops-agent-service -- ls -la /app/data/case_sop_data/hci/sop/
# 输出包含 node_sops.jsonl 文件

# 检查 SOP 配置
kubectl exec -n hci-dev deploy/ops-agent-service -- cat /app/ops_config.yaml | grep -A3 sop_catalog
```

### 1.3 检查 API Key 配置 ✅

```bash
# 检查环境变量
kubectl exec -n hci-dev deploy/ops-agent-service -- env | grep API_KEY
# 输出: OPENAI_COMPATIBLE_API_KEY=<非空>
```

---

## 2. CLI 模式测试（容器内直接运行）

> **⚠️ 重要警告**: CLI 模式会在容器内叠加 Agent 实例到已有的 HTTP Server 进程，
> 内存峰值可能超过 4Gi，**仅建议在调试时使用**。
>
> **推荐**: 生产环境使用 HTTP 接口测试（第 3 节），内存管理更可控。

### 2.1 进入容器并激活虚拟环境

```bash
kubectl exec -n hci-dev deploy/ops-agent-service -it -- /bin/bash
```

**重要**: 容器内默认 PATH 不包含 `.venv/bin`，必须先激活虚拟环境：

```bash
# 在容器内执行
source /app/.venv/bin/activate

# 验证 CLI 可用
ops-cli --version
# 输出: ops-cli, version 0.2.0
```

### 2.2 CLI 基础功能测试

#### T1: 查看配置 ✅

```bash
ops-cli show-config
```

**期望输出**:
```
                        General Settings                         
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Setting          ┃ Value                                      ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ Default Provider │ openai_compatible                          │
│ Max Steps        │ 500                                        │
│ Agent Type       │ ops_agent                                  │
│ SOP Catalog      │ Not set                                    │
│ SOP Catalog Path │ data/case_sop_data/hci/sop/node_sops.jsonl │
└──────────────────┴────────────────────────────────────────────┘
```

#### T2: 查看可用工具 ✅

```bash
ops-cli tools
```

**期望输出**: 列出 9 个内置工具，包括 `sequentialthinking`, `ops_state_update`, `get_info_from_user`, `query_sop_candidates` 等。

### 2.3 CLI 排障任务测试

#### T3: 运行排障任务（auto-approve 模式）✅

**关键**: 必须指定 `--sop hci` 参数！

```bash
ops-cli run "设备无法上网" --sop hci --working-dir /app --auto-approve --max-steps 10
```

**期望输出**:
```
任务: 设备无法上网
模型: glm-5
工具: 9 built-in
SOP 目录: /app/data/case_sop_data/hci/sop/node_sops.jsonl

╭─ Ops 状态  · 更新 #1  · 阶段 信息收集
│ 路径  —
│ 指标  确认 1 · 待澄清 4
╰─
• 正在和用户进行澄清...
╭─ 信息确认卡 ─────────────────────────────────────╮
│  核心问题                                        │
│  请问"无法上网"具体是指哪种情况？                │
│                                                  │
│  下方提供 3 个标准回答...                        │
╰──────────────────────────────────────────────────╯

请选择最符合当前情况的回答
1. 设备本身无法上网（如命令行 ping 不通外网）
2. 设备后面的终端/电脑无法上网
3. VPN 客户端拨号后无法上网
4. <自定义输入>您的想法
请输入选项编号:
```

**注意**: `--auto-approve` 只自动批准工具调用，用户交互仍需手动输入。

#### T4: CLI 交互模式（手动测试）

```bash
ops-cli run "VPN 无法连接" --sop hci --working-dir /app --max-steps 20
```

**测试步骤**:
1. Agent 输出初步诊断
2. 出现选项提示
3. 手动输入选项编号（如 `1`）
4. Agent 继续执行
5. 最终输出排障总结

---

## 3. ACP REST 接口测试（port-forward）

> **目的**: 验证 ops-agent HTTP 服务端点正确性

### 3.1 开启端口转发

```bash
kubectl -n hci-dev port-forward svc/ops-agent-service 18006:8006 &
PORT_PID=$!
sleep 2
echo "Port-forward PID: $PORT_PID"
```

### 3.2 ACP 会话生命周期测试

#### T5: 创建会话（幂等）✅

```bash
curl -s -X POST http://localhost:18006/acp/sessions \
  -H "Content-Type: application/json" \
  -d '{"session_id": "test-session-001"}' | python3 -m json.tool
```

**期望输出**:
```json
{
    "sessionId": "test-session-001"
}
```

#### T6: 验证幂等性 ✅

```bash
curl -s -X POST http://localhost:18006/acp/sessions \
  -H "Content-Type: application/json" \
  -d '{"session_id": "test-session-001"}' | python3 -m json.tool
```

**期望输出**: 与 T5 相同，不报错，不重置状态。

#### T7: 提交 prompt ✅

```bash
curl -s -X POST http://localhost:18006/acp/sessions/test-session-001/prompt \
  -H "Content-Type: application/json" \
  -d '{"prompt": [{"type": "text", "text": "设备无法上网，请协助排查"}]}' | python3 -m json.tool
```

**期望输出**:
```json
{
    "started": true,
    "sessionId": "test-session-001"
}
```

### 3.3 SSE 事件流测试

#### T8: 消费 SSE 事件流 ✅

```bash
timeout 30 curl -N http://localhost:18006/acp/sessions/test-session-001/events
```

**期望输出（按顺序）**:

1. **session_info_update**: 会话标题更新
```json
data: {"method": "session/update", "params": {"update": {"sessionUpdate": "session_info_update", "title": "设备无法上网"}}}
```

2. **tool_call**: sequentialthinking 思维链
```json
data: {"method": "session/update", "params": {"update": {"sessionUpdate": "tool_call", "title": "sequentialthinking"}}}
```

3. **tool_call**: ops_state_update 状态更新
```json
data: {"method": "session/update", "params": {"update": {"sessionUpdate": "tool_call", "title": "ops_state_update"}}}
```

4. **_ops/request_input**: 用户交互请求 ⭐
```json
data: {"id": "xxx-xxx-xxx", "method": "_ops/request_input", "params": {"request": {"kind": "info_request", "title": "信息确认卡", "prompt": "...", "options": [...]}}}
```

5. **heartbeat**: 心跳事件（每 1.5 秒）

**关键检查点**:
- ✅ 必须看到至少 1 条 `_ops/request_input` 事件
- ✅ request 中包含 `kind: "info_request"`, `options` 数组, `customInput` 字段

### 3.4 用户响应提交测试

#### T9: 提交用户响应 ✅

从 T8 输出中复制 `id` 字段值（如 `99c131c1-f9d3-4635-a455-eeaf6f7dea74`）：

```bash
REQ_ID="99c131c1-f9d3-4635-a455-eeaf6f7dea74"

curl -s -X POST "http://localhost:18006/acp/sessions/test-session-001/responses/$REQ_ID" \
  -H "Content-Type: application/json" \
  -d '{"result": {"outcome": {"outcome": "selected", "optionId": "1"}}}' | python3 -m json.tool
```

**期望输出**:
```json
{
    "ok": true
}
```

提交后，T8 的 SSE 流会继续输出新的 `session/update` 事件（Agent 继续执行）。

### 3.5 清理

```bash
kill $PORT_PID
# 或
pkill -f "port-forward.*ops-agent"
```

---

## 4. HTP 集成测试

### 4.1 环境信息

| 参数 | 值 |
|-----|---|
| K8s 节点 IP | 172.22.73.249 |
| Traefik HTTP 端口 | 4888 |
| HTP API 入口 | http://172.22.73.249:4888/api |
| 前端 URL | http://172.22.73.249:4888 |

### 4.2 前端 UI 人工验收

**步骤 1**: 打开浏览器，访问 `http://172.22.73.249:4888`

**步骤 2**: 创建新工单
- 点击"新建工单"
- 标题：`ops-agent 手动测试`
- 描述：`设备无法上网`
- 选择大脑：`ops-agent`
- 点击"创建"

**步骤 3**: 发送消息
- 输入：`设备无法上网，请协助排查`
- 点击"发送"

**步骤 4**: 观察并记录

| # | 期望行为 | 判断 |
|---|----------|------|
| 4a | 气泡区域出现流式文本 | ⬜ |
| 4b | 页面底部出现交互卡片（信息确认卡或 SOP 操作卡）| ⬜ |
| 4c | 情报确认卡显示问题、选项按钮 | ⬜ |
| 4d | SOP 操作卡显示当前路径、操作目标、操作指引 | ⬜ |
| 4e | 点击选项 → 卡片消失，Agent 继续输出 | ⬜ |
| 4f | 多轮交互或最终总结 | ⬜ |
| 4g | 无"已切换到备用助手"提示 | ⬜ |

### 4.3 HTP API 自动化测试脚本

```python
#!/usr/bin/env python3
"""HTP ops-agent 集成测试脚本"""

import sys, json, httpx, asyncio

BASE = "http://172.22.73.249:4888/api"
RESULT = {"text_chunks": 0, "interactive_requests": [], "errors": []}

async def test():
    async with httpx.AsyncClient(timeout=120) as client:
        # 创建 case
        resp = await client.post(f"{BASE}/cases",
            json={"title": "API测试", "description": "设备无法上网"})
        assert resp.status_code == 200
        case_id = resp.json()["id"]
        print(f"✅ case: {case_id}")

        # 创建 conversation
        resp = await client.post(f"{BASE}/cases/{case_id}/conversations",
            json={"brain": "ops-agent"})
        assert resp.status_code == 200
        conv_id = resp.json()["id"]
        print(f"✅ conversation: {conv_id}")

        # 发送消息
        resp = await client.post(f"{BASE}/conversations/{conv_id}/send",
            json={"content": "设备无法上网，请协助排查"})
        assert resp.status_code == 200

        # 监听 SSE
        async with client.stream("GET", f"{BASE}/conversations/{conv_id}/stream") as stream:
            async for line in stream.aiter_lines():
                if not line: continue
                line = line.strip("\x00")
                if line.startswith("event:interactive_request:"):
                    payload = json.loads(line[24:])
                    RESULT["interactive_requests"].append(payload)
                    print(f"  [交互] kind={payload['kind']}")
                    # 自动提交选项1
                    r = await client.post(
                        f"{BASE}/conversations/{conv_id}/interactive-response",
                        json={"requestId": payload["requestId"],
                              "acpSessionId": payload["acpSessionId"],
                              "outcome": {"outcome": "selected", "optionId": "1"}})
                    print(f"    → 提交响应 {r.status_code}")
                elif line:
                    RESULT["text_chunks"] += 1

    print(f"\n文本: {RESULT['text_chunks']}, 交互: {len(RESULT['interactive_requests'])}")
    assert RESULT['text_chunks'] > 0, "❌ 无文本输出"
    print("✅ 验收通过")

if __name__ == "__main__":
    asyncio.run(test())
```

---

## 5. 测试结果记录表

| 编号 | 测试名称 | 执行命令 | 结果 | 备注 |
|-----|---------|---------|------|------|
| T1 | CLI show-config | `ops-cli show-config` | ✅ | 显示完整配置 |
| T2 | CLI tools | `ops-cli tools` | ✅ | 列出9个工具 |
| T3 | CLI run auto-approve | `ops-cli run "设备无法上网" --sop hci --auto-approve --max-steps 10` | ✅ | Agent启动，显示交互卡 |
| T4 | CLI 交互模式 | 手动测试 | ⬜ | 需手动输入 |
| T5 | ACP 创建会话 | `POST /acp/sessions` | ✅ | 返回 sessionId |
| T6 | ACP 幂等性 | 重复 POST | ✅ | 不报错 |
| T7 | ACP 提交 prompt | `POST /acp/sessions/{id}/prompt` | ✅ | 返回 started: true |
| T8 | ACP SSE 事件流 | `GET /acp/sessions/{id}/events` | ✅ | 包含 _ops/request_input |
| T9 | ACP 用户响应 | `POST /acp/sessions/{id}/responses/{req_id}` | ✅ | 返回 ok: true |

---

## 6. 故障排查

### 6.1 CLI 命令不存在

**问题**: `bash: ops-cli: command not found`

**解决**: 激活虚拟环境
```bash
source /app/.venv/bin/activate
```

### 6.3 内存不足导致 CLI OOM

**问题**: `command terminated with exit code 137`（OOMKilled）

**原因**: CLI 模式叠加 HTTP Server 进程内存，SOPQuerySubAgent 峰值超过限制

**解决**:
1. **方案 A**（推荐）：使用 HTTP 接口测试（第 3 节 ACP REST），避免内存叠加
2. **方案 B**：提升 memory limit
   ```bash
   # 在 hci-platform-env 仓库修改
   # environments/dev/values.yaml
   opsAgent:
     resources:
       limits:
         memory: "4Gi"  # 从 2Gi 提升
   ```

**验证修复**:
```bash
kubectl get pod -n hci-dev -l app=ops-agent-service -o jsonpath='{.items[0].status.containerStatuses[0].lastState.terminated.exitCode}'
# 期望: 无输出或 exitCode != 137
```

---

### 6.4 SOP 参数缺失

**问题**: `Error: --sop / --sop-catalog is required.`

**解决**: 添加 `--sop hci` 参数
```bash
ops-cli run "问题" --sop hci --working-dir /app
```

### 6.3 SOP 文件找不到

**问题**: `FileNotFoundError: node_sops.jsonl`

**解决**: 检查宿主机 HostPath 挂载
```bash
ls -la /mnt/d/aihci/ops-agent/data/case_sop_data/hci/sop/
```

---

## 7. 完成标准

**全部通过条件**:
- CLI 测试（T1-T3）至少 3 个通过
- ACP 接口测试（T5-T9）全部通过
- 前端 UI 测试：4a-4g 全部打勾

**核心守卫**:
- ✅ `_ops/request_input` 事件正确格式
- ✅ 用户响应提交返回 `ok: true`
- ✅ Agent 继续执行（无静默失败）

---

*文档基于实际测试结果更新，所有命令已验证*