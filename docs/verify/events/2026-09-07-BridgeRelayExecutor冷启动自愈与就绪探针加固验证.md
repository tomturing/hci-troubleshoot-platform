# BridgeRelayExecutor 冷启动并发自愈与探针加固验证记录

## 1. 背景与根因

在节点/集群统一重启（冷启动）后，所有 Pod 并发拉起。由于 `redis-0` 或 CoreDNS 的网络就绪通常较 `agent-service` 稍有数秒时差，原代码在 FastAPI `lifespan` 中仅尝试一次 Redis 连接：
1. 启动瞬间连接抛出异常，进入降级分支设置 `redis_manager = None`；
2. 导致 `investigation_tool_executor._bridge_executor` 为 `None`，跳过了 `set_executor(...)` 全局注册；
3. 进程在整个生命周期内缺乏运行时自愈机制，即使几秒后 Redis 恢复健康，全局 `_executor` 恒定为 `None`；
4. 用户在后续执行任何 KBD 关键信号排障（`qkv_alert`、`qkv_task`、`qfk_system`、`qfk_log` 等）时，100% 报错：`BridgeRelayExecutor 尚未初始化`；
5. 同时 `/v1/agent/health` 探针盲目返回 200 OK，未对执行器与关键依赖状态做可观测性暴露，掩盖了真实故障。

## 2. 根治落地改造

### 2.1 启动期有限退避重试 (Lifespan Backoff Retry)
- 在 `app/main.py` 的 Redis 初始化流程中，引入有限退避重试机制（最多 5 次，指数退避 1s, 2s, 4s...）。
- 平滑抹平节点重启时各 Pod 间的启动时差，大部分冷启动抖动均能在启动期直接自愈。

### 2.2 运行时惰性自愈获取器 (`get_or_init_executor`)
- 在 `app/tools/acli/executor.py` 中引入线程/协程安全的 `get_or_init_executor()` 异步方法，采用双重检查锁（Double-Checked Locking, DCL）。
- 当全局 `_executor` 为 `None` 时，首次调用工具或信号判定时自动读取基础配置（`REDIS_URL`、`CONVERSATION_SERVICE_URL`、`INTERNAL_API_TOKEN`），动态创建 `RedisManager` 并实例化 `BridgeRelayExecutor`，完成全局自愈注册。
- 注入唯一调用链追踪日志 `bridge_relay_executor_self_healed` / `bridge_relay_executor_self_heal_failed`。
- 将 `app/tools/qkv/engine.py`、`app/tools/qfk/engine.py`、`app/adapters/agents/htp/kbd_differential.py` 以及 `acli_exec`/`bash_exec` 的硬编码 `_executor` 统一收敛至 `await get_or_init_executor()`。

### 2.3 健康检查与可观测性透传
- 在 `/v1/agent/health` 接口中暴露 `bridge_relay_executor: bool` 状态字段，运维巡检及 Prometheus 探针可即时洞察执行器就绪度。

## 3. 测试与验证

### 3.1 自动化测试
1. `backend/agent-service/tests/tools/test_executor.py`：
   - `test_get_or_init_executor_when_already_set`：验证已有实例时直接复用。
   - `test_get_or_init_executor_missing_config`：验证缺少核心配置时安全降级返回 `None`。
   - `test_get_or_init_executor_self_heals_success`：验证在未注入时全自动惰性自愈连接并注入全局变量。
   - `test_get_or_init_executor_connect_failure`：验证连接异常时不抛崩溃未捕获异常。
   - `test_get_or_init_executor_concurrency`：验证多协程并发调用时的互斥锁安全性与单次建连幂等性。
2. `backend/agent-service/tests/unit/test_qkv.py` 与 `test_qfk.py`：
   - 全量 QKV 与 QFK 信号执行、完整物理流缓存及变量池派生测试 100% 通过（75 passed, 36 passed）。

## 4. 结论
通过“启动退避重试 + 运行时惰性自愈 + 健康检查可观测性”三层防护，彻底根除了执行器单点初始化脆弱性，保证了排障系统的全天候韧性。
