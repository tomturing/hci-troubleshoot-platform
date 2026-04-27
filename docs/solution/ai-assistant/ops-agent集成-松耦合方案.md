# Ops-Agent 集成 - 松耦合方案

## 文档信息

| 项目 | 内容 |
|------|------|
| 文档版本 | v1.0 |
| 创建日期 | 2026-04-27 |
| 设计原则 | 第一性原理、最小侵入、可回滚 |
| 集成模式 | 松耦合、协议适配 |

## 目录

- [一、设计背景与原则](#一设计背景与原则)
- [二、架构设计](#二架构设计)
- [三、OA端实施](#三oa端实施)
- [四、HCI-TP端实施](#四hci-tp端实施)
- [五、部署方案](#五部署方案)
- [六、测试与验证](#六测试与验证)
- [七、回滚方案](#七回滚方案)

---

## 一、设计背景与原则

### 1.1 第一性原理分析

在设计集成方案前，我们首先回归两个项目的**根本目的**：

| 项目 | 根本目的 | 核心价值 | 架构风格 |
|------|---------|---------|---------|
| **HCI-TP** | 企业级AI排障平台 | 工单协作、可管理、可观测 | 微服务、异步、分布式 |
| **Ops-Agent** | SOP引导的本地工具 | 工具化、SOP检索、CLI优先 | 单体、同步、有状态 |

**关键结论**：两个项目解决不同问题，是互补而非替代关系。因此，采用**松耦合集成方案**是最优选择。

### 1.2 设计原则

| 原则 | 说明 |
|------|------|
| **最小侵入** | 尽可能少修改现有代码，主要通过适配器层实现集成 |
| **协议优先** | 复用现有 AIAssistantClient 协议，不引入新协议 |
| **可回滚** | 提供完整的回滚方案，5分钟内完成回滚 |
| **独立演进** | 两个项目仍可独立开发和发布 |
| **可观测** | OA端需接入OpenTelemetry，与HCI-TP统一观测体系 |

### 1.3 关键设计决策

| 决策项 | 方案 | 原因 |
|------|------|------|
| **集成方式** | 松耦合适配器 | 保持两个项目独立，风险低 |
| **通信协议** | OpenAI-compatible API | 复用现有 AIAssistantClient，无需改造 |
| **配置管理** | 环境变量 + Helm Values | 与HCI-TP现有配置方式一致 |
| **状态管理** | HCI-TP全负责，OA无状态 | OA仅提供计算能力，状态由HCI-TP持久化 |

---

## 二、架构设计

### 2.1 整体架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                        HCI-Troubleshoot-Platform                      │
│  ┌─────────────┐  ┌────────────────────┐  ┌──────────────────────┐  │
│  │    Frontend │  │   API Gateway      │  │     KB Service      │  │
│  │  (Customer) │  │      :8000         │  │       :8004         │  │
│  └──────┬──────┘  └──────┬─────────────┘  └──────────────────────┘  │
│         │ WebSocket      │ HTTP Request                             │
│         ▼                ▼                                          │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │           Conversation Service (:8002)                       │  │
│  │ ┌──────────────────────────────────────────────────────────┐  │  │
│  │ │  AIAssistantRegistry                                     │  │  │
│  │ │  ┌────────────────┐  ┌──────────────────────────────────┐│  │  │
│  │ │  │ OpenClawClient │  │ OpsAgentAdapter (NEW)            ││  │  │
│  │ │  │  (现有)       │  │  (复用 OpenAI-compatible)         ││  │  │
│  │ │  └────────────────┘  └──────────────────────────────────┘│  │  │
│  │ └──────────────────────────────────────────────────────────┘  │  │
│  └─────────────────────────────────────────────────────────────────┘
│                         │ HTTP / OpenAI-compatible API
│                         ▼
└─────────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────────┐
│                        Ops-Agent Service                             │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │ FastAPI Server (NEW)                                             │ │
│  │ ┌─────────────────────────────────────────────────────────────┐ ││
│  │ │  /v1/chat/completions  →  Agent.execute_task()              │ ││
│  │ │  /health                 →  Health check                    │ ││
│  │ │  /metrics                →  Prometheus metrics              │ ││
│  │ └─────────────────────────────────────────────────────────────┘ ││
│  │  ┌─────────────────────────────────────────────────────────┐  │ │
│  │  │  OpsAgent  (现有核心逻辑，无需改造)                      │  │ │
│  │  │  ┌─────────────────────────────────────────────────┐   │  │ │
│  │  │  │  SOP Query SubAgent  →  SOP检索与收敛            │  │ │ │
│  │  │  │  Tools Registry    →  工具调用                   │  │ │ │
│  │  │  │  Trajectory Recorder → 轨迹记录（可选）          │  │ │ │
│  │  │  └─────────────────────────────────────────────────┘   │  │ │
│  │  └─────────────────────────────────────────────────────────┘  │ │
│  └─────────────────────────────────────────────────────────────────┘ │
│                        │ Read
│                        ▼
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │  SOP Catalog (可选：从HCI-TP KB同步，或本地文件)                 │ │
│  └─────────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────┘
```

### 2.2 核心设计理念

**关键设计：协议适配而非代码耦合**

```
现有协议：OpenAI-compatible API
├─ Request: {model, messages, stream, user}
└─ Response: SSE stream: data: {"delta": {"content": "..."}}

复用现有AIAssistantClient协议，实现：
├─ 注册 ops-agent 助手类型
├─ 通过HTTP调用OA的OpenAI-compatible端点
└─ 复用所有现有功能（数据库、SSE、审计等）
```

### 2.3 交互时序图

```
User          Frontend      API Gateway  Conversation Svc    OA Service
│               │               │            │                  │
│  发消息       │               │            │                  │
│──────────────>│               │            │                  │
│               │  WebSocket   │            │                  │
│               │──────────────>│            │                  │
│               │               │ POST       │                  │
│               │               │───────────>│                  │
│               │               │            │                  │
│               │               │            │  1. 查注册表      │
│               │               │            │  得OpsAgentClient│
│               │               │            │                  │
│               │               │            │  2. 调用OA端点   │
│               │               │            │─────────────────>│
│               │               │            │  POST /v1/chat/  │
│               │               │            │  completions     │
│               │               │            │                  │
│               │               │            │                  │
│               │               │            │  内部执行Agent  │
│               │               │            │  loop            │
│               │               │            │                  │
│               │               │            │<─────────────────│
│               │               │            │  SSE Stream      │
│               │               │<───────────│                  │
│               │<──────────────│            │                  │
│<──────────────│               │            │                  │
│               │               │            │                  │
│               │               │            │  3. 保存消息      │
│               │               │            │  (后台任务)      │
```

---

## 三、OA端实施

### 3.1 目录结构变更

```
ops-agent/
├── ops_agent/
│   ├── agent/              # 现有，无需改动
│   ├── tools/              # 现有，无需改动
│   ├── utils/              # 现有，无需改动
│   └── server/             # 新增：OA服务端点
│       ├── __init__.py
│       ├── main.py         # FastAPI应用
│       ├── openai_compat.py # OpenAI-compatible API实现
│       └── otel_integration.py # OpenTelemetry集成
└── ops_config.yaml.example # 新增HCI-TP集成配置
```

### 3.2 新增文件实现

#### 3.2.1 ops_agent/server/main.py - FastAPI主应用

```python
"""
Ops-Agent Server - FastAPI主应用
提供OpenAI-compatible API端点供HCI-TP调用
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic_settings import BaseSettings

from ops_agent.agent import Agent
from ops_agent.utils.config import Config
from ops_agent.utils.logger import get_logger

from .otel_integration import init_otel, instrument_app
from .openai_compat import OpenAICompatibleHandler

logger = get_logger("ops-agent-server")


class ServerSettings(BaseSettings):
    """服务配置"""
    SERVICE_NAME: str = "ops-agent-service"
    SERVICE_PORT: int = 8006
    LOG_LEVEL: str = "INFO"

    # OA配置
    OPS_CONFIG_PATH: str = "ops_config.yaml"
    OPS_AGENT_AUTO_APPROVE: bool = True
    OPS_AGENT_DEBUG_LLM: bool = False

    # HCI-TP集成配置
    HCI_TP_ENABLED: bool = False
    HCI_TP_TRACE_HEADER: str = "traceparent"

    class Config:
        env_file = ".env"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    settings = ServerSettings()
    logger.info(
        event="server_starting",
        message=f"Starting {settings.SERVICE_NAME}",
        port=settings.SERVICE_PORT
    )

    # 初始化OpenTelemetry
    if settings.HCI_TP_ENABLED:
        init_otel(settings.SERVICE_NAME)

    # 加载OA配置
    config = Config.create(config_file=settings.OPS_CONFIG_PATH)
    logger.info(
        event="config_loaded",
        message="OA configuration loaded successfully",
        model=config.ops_agent.model.model if config.ops_agent else None
    )

    # 存入app.state
    app.state.settings = settings
    app.state.ops_config = config

    yield

    logger.info(
        event="server_stopping",
        message=f"Stopping {settings.SERVICE_NAME}"
    )


app = FastAPI(
    title="Ops-Agent API",
    description="Ops-Agent OpenAI-compatible API",
    version="0.2.0",
    lifespan=lifespan
)

# OpenTelemetry instrumentation
settings = ServerSettings()
if settings.HCI_TP_ENABLED:
    instrument_app(app)


@app.get("/health")
async def health_check():
    """健康检查端点"""
    return {
        "status": "healthy",
        "service": "ops-agent-service",
        "version": "0.2.0"
    }


@app.get("/health/live")
async def health_live():
    """Liveness探针"""
    return {"status": "alive"}


@app.get("/health/ready")
async def health_ready():
    """Readiness探针"""
    return {"status": "ready"}


@app.get("/metrics")
async def metrics():
    """Prometheus指标端点"""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    OpenAI-compatible聊天补全端点

    Request格式:
    {
        "model": "ops-agent",
        "messages": [{"role": "user", "content": "..."}],
        "stream": true,
        "user": "trace-id"
    }

    Response格式: SSE流
    data: {"id": "...", "delta": {"content": "..."}}
    data: [DONE]
    """
    settings: ServerSettings = request.app.state.settings
    config: Config = request.app.state.ops_config

    handler = OpenAICompatibleHandler(
        config=config,
        auto_approve=settings.OPS_AGENT_AUTO_APPROVE,
        debug_llm=settings.OPS_AGENT_DEBUG_LLM,
        hci_tp_enabled=settings.HCI_TP_ENABLED
    )

    return await handler.handle(request)
```

#### 3.2.2 ops_agent/server/openai_compat.py - OpenAI兼容API处理

```python
"""
OpenAI-compatible API处理器

将OpenAI API请求转换为OA的Agent执行调用
并将OA输出转换为OpenAI SSE格式
"""

import asyncio
import json
import time
from typing import AsyncGenerator
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import StreamingResponse
from shared.utils.logger import get_logger
from shared.utils.otel import get_current_trace_id

from ops_agent.agent import Agent
from ops_agent.utils.config import Config

logger = get_logger("openai-compat-handler")


@dataclass
class ChatCompletionRequest:
    """OpenAI聊天补全请求格式"""
    model: str = "ops-agent"
    messages: list[dict] = None
    stream: bool = True
    user: str | None = None


class OpenAICompatibleHandler:
    """OpenAI-compatible API处理器"""

    def __init__(
        self,
        config: Config,
        auto_approve: bool = True,
        debug_llm: bool = False,
        hci_tp_enabled: bool = False
    ):
        self.config = config
        self.auto_approve = auto_approve
        self.debug_llm = debug_llm
        self.hci_tp_enabled = hci_tp_enabled

    async def handle(self, request: Request) -> StreamingResponse:
        """处理聊天补全请求"""
        body = await request.json()
        req = ChatCompletionRequest(**body)

        logger.info(
            event="chat_completion_request",
            message="Received chat completion request",
            model=req.model,
            message_count=len(req.messages) if req.messages else 0,
            user=req.user
        )

        # 从messages提取用户问题（取最后一条user消息）
        user_query = ""
        if req.messages:
            for msg in reversed(req.messages):
                if msg.get("role") == "user":
                    user_query = msg.get("content", "")
                    break

        async def generate() -> AsyncGenerator[str, None]:
            """SSE响应生成器"""
            start_time = time.time()
            chunk_id = f"chatcmpl-{int(time.time())}"
            created = int(start_time)

            try:
                # 创建Agent
                agent = Agent(
                    agent_type="ops_agent",
                    config=self.config,
                    auto_approve_mode="all" if self.auto_approve else None,
                    debug_llm=self.debug_llm
                )

                # 构建任务参数
                extra_args = {
                    "project_path": "/tmp",  # 可通过配置或扩展参数设置
                    "issue": user_query
                }

                # 执行Agent任务（包装为生成器）
                async for chunk in self._execute_agent_stream(
                    agent, user_query, extra_args, chunk_id, created
                ):
                    yield chunk

                # 任务完成，发送[DONE]
                yield "data: [DONE]\n\n"

            except Exception as e:
                logger.error(
                    event="chat_completion_error",
                    message="Error in chat completion",
                    error_type=type(e).__name__,
                    error_message=str(e)
                )
                yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no"
            }
        )

    async def _execute_agent_stream(
        self, agent: Agent, query: str, extra_args: dict, chunk_id: str, created: int
    ) -> AsyncGenerator[str, None]:
        """
        执行Agent并流式输出

        注意：OA的Agent.run()是单次调用返回最终结果
        为模拟流效果，我们将最终结果分块输出
        """
        # 执行Agent
        execution = await agent.run(query, extra_args)

        # 获取最终结果
        final_result = execution.final_result if execution.final_result else "任务完成"

        # 分块输出（模拟流式效果）
        chunk_size = 3
        for i in range(0, len(final_result), chunk_size):
            chunk = final_result[i:i + chunk_size]
            yield self._build_sse_chunk(chunk, chunk_id, created)
            await asyncio.sleep(0.01)  # 短暂延迟模拟流效果

    def _build_sse_chunk(self, content: str, chunk_id: str, created: int) -> str:
        """构建SSE数据块"""
        payload = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": "ops-agent",
            "choices": [{
                "index": 0,
                "delta": {"content": content},
                "finish_reason": None
            }]
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
```

#### 3.2.3 ops_agent/server/otel_integration.py - OpenTelemetry集成

```python
"""
OpenTelemetry集成

与HCI-TP可观测性体系对齐
"""

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter


def init_otel(service_name: str):
    """初始化OpenTelemetry"""
    resource = Resource.create({
        "service.name": service_name,
        "service.version": "0.2.0"
    })

    provider = TracerProvider(resource=resource)

    # 配置OTLP导出器（与HCI-TP一致）
    import os
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")
    processor = BatchSpanProcessor(
        OTLPSpanExporter(endpoint=otlp_endpoint)
    )
    provider.add_span_processor(processor)

    trace.set_tracer_provider(provider)


def instrument_app(app: FastAPI):
    """Instrument FastAPI应用"""
    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
```

### 3.3 配置文件变更

#### 3.3.1 ops_config.yaml.example - 新增HCI-TP集成配置

```yaml
agents:
  enable_lakeview: false
  ops_agent:
    model: glm47-openrouter
    max_steps: 500
    sop_catalog_path: data/case_sop_data/af/sop/node_sops.jsonl
    query_sub_agent:
      model: glm47-openrouter
      max_steps: 80
      tool_call_budget: 40
      budget_warning_threshold: 10

lakeview:
  model: lakeview_model

model_providers:
  openrouter-example:
    api_key: ""
    base_url: https://openrouter.ai/api/v1
    provider: openrouter

models:
  glm47-openrouter:
    model_provider: openrouter-example
    model: z-ai/glm-4.7
    max_tokens: 8192
    temperature: 0.8
    top_p: 0.95
    top_k: 0
    max_retries: 3
    parallel_tool_calls: false
    enable_thinking: false

  lakeview_model:
    model_provider: openrouter-example
    model: z-ai/glm-4.7
    max_tokens: 4096
    temperature: 0.0
    top_p: 1.0
    top_k: 0
    max_retries: 1
    parallel_tool_calls: false
    enable_thinking: false
```

### 3.4 Dockerfile - OA服务容器化

```dockerfile
# ops-agent/Dockerfile.ops-server
FROM python:3.12-slim AS builder

WORKDIR /app

# 安装uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 复制依赖文件
COPY pyproject.toml uv.lock ./

# 安装依赖
RUN uv venv --python 3.12
RUN .venv/bin/pip install -e .

# 生产镜像
FROM python:3.12-slim AS production

WORKDIR /app

# 复制venv
COPY --from=builder /app/.venv /app/.venv

# 复制代码
COPY ops_agent/ ./ops_agent/
COPY ops_config.yaml ./

# 配置环境变量
ENV PATH="/app/.venv/bin:$PATH"
ENV OPS_CONFIG_PATH="/app/ops_config.yaml"

# 暴露端口
EXPOSE 8006

# 健康检查
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8006/health || exit 1

# 启动命令
CMD ["uvicorn", "ops_agent.server.main:app", "--host", "0.0.0.0", "--port", "8006"]
```

---

## 四、HCI-TP端实施

### 4.1 变更文件清单

| 文件路径 | 变更类型 | 说明 |
|---------|---------|------|
| `backend/conversation-service/app/services/ai_client.py` | 新增代码 | 添加OpsAgentAssistant实现 |
| `backend/conversation-service/app/config.py` | 少量修改 | 添加OA配置项 |
| `backend/conversation-service/app/main.py` | 少量修改 | 注册OA助手到注册表 |
| `deploy/helm/hci-platform/templates/conversation-service/env-configmap.yaml` | 新增 | OA配置 |
| `deploy/helm/hci-platform/templates/ops-agent-service/` | 新增目录 | OA服务K8s资源 |

### 4.2 具体实现

#### 4.2.1 backend/conversation-service/app/services/ai_client.py - 新增OpsAgentAssistant

```python
# 在ai_client.py文件末尾添加

class OpsAgentAssistant:
    """
    Ops-Agent AI助手适配器

    复用OpenClawAssistant的HTTP调用逻辑
    但针对OA的特定行为做微调
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        provider_api_key: str | None = None,
        default_model: str = "ops-agent",
        assistant_type: str = "ops-agent",
    ):
        self.base_url = base_url.rstrip("/")
        self.gateway_token = api_key
        self.provider_api_key = provider_api_key or os.environ.get("OPS_AGENT_API_KEY")
        self.default_model = default_model
        self.assistant_type = assistant_type
        _read_timeout = float(os.environ.get("AI_CLIENT_READ_TIMEOUT_SEC", "180.0"))  # OA执行可能较慢
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=_read_timeout, write=10.0, pool=10.0))

    async def chat_completion_stream(
        self, messages: list[dict[str, str]], user_id: str, pod_endpoint: str | None = None, model: str = ""
    ) -> AsyncGenerator[str, None]:
        """
        调用OA的OpenAI-compatible端点

        Args:
            messages: 消息列表
            user_id: 用户ID（用于trace关联）
            pod_endpoint: 不使用（OA不支持热备池）
            model: 模型名（固定为ops-agent）
        """
        headers = {
            "Content-Type": "application/json",
        }

        payload = {
            "model": model or self.default_model,
            "messages": messages,
            "stream": True,
            "user": user_id
        }

        # 只使用base_url（OA不支持热备池）
        endpoint = self.base_url
        url = f"{endpoint}/v1/chat/completions"

        token = self.provider_api_key or self.gateway_token
        if token:
            headers["Authorization"] = f"Bearer {token}"

        logger.info(
            event="ai_request",
            message="Sending request to Ops-Agent",
            url=url,
            user_id=user_id,
            assistant_type=self.assistant_type
        )

        try:
            async with self.client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    error_text = error_body.decode("utf-8", errors="replace")
                    logger.error(
                        event="ai_error",
                        message=f"Ops-Agent returned status {response.status_code}",
                        status=response.status_code,
                        body=error_text
                    )
                    error_detail = self._parse_ai_error(response.status_code, error_text)
                    raise AIStreamError(
                        code=error_detail["code"],
                        message=error_detail["message"],
                        detail=error_detail["detail"],
                    )

                got_first_token = False
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue

                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        if not got_first_token:
                            logger.warning(
                                event="ai_empty_response",
                                message="Ops-Agent returned empty response",
                                url=url
                            )
                        return

                    try:
                        data = json.loads(data_str)
                        delta = data.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            got_first_token = True
                            yield content
                    except json.JSONDecodeError:
                        continue

            if not got_first_token:
                logger.warning(
                    event="ai_stream_no_content",
                    message="Ops-Agent stream ended without content",
                    url=url
                )
                raise AIStreamError(
                    code=ErrorCode.AI_UPSTREAM_ERROR,
                    message="Ops-Agent returned no content",
                    detail="stream ended without content"
                )

        except AIStreamError:
            raise
        except Exception as e:
            last_error = e
            logger.error(
                event="ai_exception",
                message="Error calling Ops-Agent API",
                error_type=type(e).__name__,
                error_message=str(e)
            )
            error_detail = self._parse_generic_error(e)
            raise AIStreamError(
                code=error_detail["code"],
                message=error_detail["message"],
                detail=error_detail["detail"],
            ) from e

    async def check_health(self) -> bool:
        """检查OA服务健康状态"""
        url = f"{self.base_url}/health"
        try:
            response = await self.client.get(url)
            return response.status_code == 200
        except Exception:
            return False

    async def close(self):
        """关闭客户端"""
        await self.client.aclose()


def create_ops_agent_client(
    base_url: str,
    api_key: str | None = None,
    provider_api_key: str | None = None,
    default_model: str = "ops-agent",
    assistant_type: str = "ops-agent",
) -> OpsAgentAssistant:
    """工厂函数: 创建OA助手客户端"""
    return OpsAgentAssistant(
        base_url=base_url,
        api_key=api_key,
        provider_api_key=provider_api_key,
        default_model=default_model,
        assistant_type=assistant_type
    )
```

#### 4.2.2 backend/conversation-service/app/config.py - 添加OA配置项

```python
class Settings(BaseSettings):
    # ... 现有配置保持不变 ...

    # Ops-Agent集成配置（新增）
    OPS_AGENT_ENABLED: bool = False
    OPS_AGENT_BASE_URL: str = "http://ops-agent-service:8006"
    OPS_AGENT_API_KEY: str = ""

    @property
    def assistant_registry(self) -> dict[str, dict[str, Any]]:
        """解析助手注册表并与默认openclaw配置合并"""
        default_registry: dict[str, dict[str, Any]] = {
            "openclaw": {
                "base_url": self.OPENCLAW_BASE_URL,
                "gateway_token": self.OPENCLAW_GATEWAY_TOKEN,
                "model": self.OPENCLAW_DEFAULT_MODEL,
                "enabled": True,
            }
        }

        # 添加Ops-Agent配置（新增）
        if self.OPS_AGENT_ENABLED:
            default_registry["ops-agent"] = {
                "base_url": self.OPS_AGENT_BASE_URL,
                "gateway_token": self.OPS_AGENT_API_KEY,
                "model": "ops-agent",
                "enabled": True,
            }

        try:
            custom = json.loads(self.ASSISTANT_REGISTRY_JSON or "{}")
            if isinstance(custom, dict):
                for assistant_type, cfg in custom.items():
                    if isinstance(cfg, dict):
                        merged = {**default_registry.get(assistant_type, {}), **cfg}
                        default_registry[assistant_type] = merged
        except json.JSONDecodeError:
            pass
        return default_registry
```

#### 4.2.3 backend/conversation-service/app/main.py - 注册OA助手

```python
# 在main.py中修改助手注册表初始化部分

from app.services.ai_client import (
    AIAssistantRegistry,
    create_openclaw_client,
    create_ops_agent_client  # 新增导入
)

# 在lifespan函数中

    # 按配置注册多助手客户端
    ai_registry = AIAssistantRegistry()
    for assistant_type, cfg in settings.assistant_registry.items():
        if not cfg.get("enabled", True):
            continue

        base_url = cfg.get("base_url", settings.OPENCLAW_BASE_URL)
        gateway_token = cfg.get("gateway_token", settings.OPENCLAW_GATEWAY_TOKEN)
        provider_key = cfg.get("provider_api_key") or None
        model = cfg.get("model", assistant_type)

        # 根据助手类型选择创建函数
        if assistant_type == "ops-agent":
            client = create_ops_agent_client(
                base_url=base_url,
                api_key=gateway_token,
                provider_api_key=provider_key,
                default_model=model,
                assistant_type=assistant_type,
            )
        else:
            client = create_openclaw_client(
                base_url=base_url,
                api_key=gateway_token,
                provider_api_key=provider_key,
                default_model=model,
                assistant_type=assistant_type,
            )

        ai_registry.register(assistant_type, client)
```

### 4.3 前端变更（可选）

前端无需大改动，现有逻辑已支持动态助手类型。用户在创建工单时可以选择：

- `openclaw`（默认，现有）
- `ops-agent`（新增）

---

## 五、部署方案

### 5.1 Docker Compose开发环境

```yaml
# deploy/docker/docker-compose.opsagent.yaml

version: '3.8'

services:
  ops-agent-service:
    build:
      context: ../../ops-agent
      dockerfile: Dockerfile.ops-server
    ports:
      - "8006:8006"
    environment:
      - OPS_CONFIG_PATH=/app/ops_config.yaml
      - OPENROUTER_API_KEY=${OPENROUTER_API_KEY}
      - OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
      - HCI_TP_ENABLED=true
    volumes:
      - ../../ops-agent/ops_config.yaml:/app/ops_config.yaml:ro
      - ../../ops-agent/data/:/app/data/:ro
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8006/health"]
      interval: 30s
      timeout: 3s
      retries: 3
      start_period: 10s
    depends_on:
      otel-collector:
        condition: service_started
```

在conversation-service环境变量中添加：

```yaml
services:
  conversation-service:
    environment:
      - OPS_AGENT_ENABLED=true
      - OPS_AGENT_BASE_URL=http://ops-agent-service:8006
```

### 5.2 Kubernetes生产环境

#### 5.2.1 deploy/helm/hci-platform/templates/ops-agent-service/service.yaml

```yaml
apiVersion: v1
kind: Service
metadata:
  name: ops-agent-service
  labels:
    app.kubernetes.io/name: ops-agent-service
spec:
  type: ClusterIP
  ports:
    - port: 8006
      targetPort: http
      protocol: TCP
      name: http
  selector:
    app.kubernetes.io/name: ops-agent-service
```

#### 5.2.2 deploy/helm/hci-platform/templates/ops-agent-service/deployment.yaml

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ops-agent-service
  labels:
    app.kubernetes.io/name: ops-agent-service
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: ops-agent-service
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ops-agent-service
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "8006"
        prometheus.io/path: "/metrics"
    spec:
      containers:
        - name: ops-agent
          image: ghcr.io/tomturing/ops-agent:latest
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: 8006
              protocol: TCP
          env:
            - name: OPS_CONFIG_PATH
              value: /app/ops_config.yaml
            - name: HCI_TP_ENABLED
              value: "true"
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: http://hci-platform-otel-collector:4318
            - name: OPENROUTER_API_KEY
              valueFrom:
                secretKeyRef:
                  name: ops-agent-secret
                  key: openrouter-api-key
                  optional: true
          volumeMounts:
            - name: ops-config
              mountPath: /app/ops_config.yaml
              subPath: ops_config.yaml
          resources:
            requests:
              cpu: "100m"
              memory: "128Mi"
            limits:
              cpu: "500m"
              memory: "512Mi"
          livenessProbe:
            httpGet:
              path: /health/live
              port: http
            initialDelaySeconds: 10
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /health/ready
              port: http
            initialDelaySeconds: 5
            periodSeconds: 10
      volumes:
        - name: ops-config
          configMap:
            name: ops-agent-config
```

#### 5.2.3 deploy/helm/hci-platform/templates/ops-agent-service/configmap.yaml

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: ops-agent-config
data:
  ops_config.yaml: |
    agents:
      enable_lakeview: false
      ops_agent:
        model: glm47-openrouter
        max_steps: 500
        sop_catalog_path: data/case_sop_data/af/sop/node_sops.jsonl
        query_sub_agent:
          model: glm47-openrouter
          max_steps: 80
          tool_call_budget: 40
          budget_warning_threshold: 10

    model_providers:
      openrouter-example:
        api_key: ""
        base_url: https://openrouter.ai/api/v1
        provider: openrouter

    models:
      glm47-openrouter:
        model_provider: openrouter-example
        model: z-ai/glm-4.7
        max_tokens: 8192
        temperature: 0.8
        top_p: 0.95
        top_k: 0
        max_retries: 3
        parallel_tool_calls: false
        enable_thinking: false
```

#### 5.2.4 conversation-service部署配置变更

```yaml
# 在conversation-service部署中添加环境变量
env:
  - name: OPS_AGENT_ENABLED
    value: "true"
  - name: OPS_AGENT_BASE_URL
    value: "http://ops-agent-service:8006"
```

---

## 六、测试与验证

### 6.1 测试清单

| 测试项 | 测试方法 | 预期结果 |
|------|---------|---------|
| OA服务健康检查 | curl http://localhost:8006/health | 返回200 |
| OA API端点测试 | 发送test请求到/v1/chat/completions | 返回SSE流 |
| HCI-TP助手注册 | 调用GET /assistants | 列表中包含ops-agent |
| 端到端对话测试 | 创建工单选择ops-agent，发送消息 | 对话正常工作 |
| 可观测性检查 | 查看Grafana Trace | Trace包含OA服务Span |
| 回滚测试 | 执行回滚流程 | 5分钟内回滚完成 |

### 6.2 测试环境启动命令

```bash
# 启动完整测试环境
cd hci-troubleshoot-platform
docker-compose -f deploy/docker/docker-compose.yaml -f deploy/docker/docker-compose.opsagent.yaml up -d

# 查看OA服务日志
docker-compose -f deploy/docker/docker-compose.opsagent.yaml logs -f ops-agent-service
```

---

## 七、回滚方案

### 7.1 回滚触发条件

| 触发条件 | 说明 |
|------|------|
| OA服务异常 | OA服务连续健康检查失败 |
| 对话功能异常 | 选择ops-agent助手无法正常对话 |
| 性能影响 | 集成后系统性能大幅下降 |

### 7.2 回滚步骤

#### 步骤1：禁用OA助手（最快回滚，<1分钟）

```bash
# 方式1：通过K8s环境变量禁用
kubectl set env deployment/conversation-service OPS_AGENT_ENABLED=false

# 方式2：通过Helm values
helm upgrade hci-platform -f custom-values.yaml --set conversationService.opsAgent.enabled=false

# 说明：禁用后，HCI-TP用户无法选择ops-agent助手
# 现有对话若已选择ops-agent可能失败，但不会影响系统整体
```

#### 步骤2：完整回滚（如需，<5分钟）

```bash
# 1. 禁用OA助手（同步骤1）

# 2. 卸载OA服务
kubectl delete -f deploy/helm/hci-platform/templates/ops-agent-service/

# 3. 回滚conversation-service代码（如需要）
kubectl rollout undo deployment/conversation-service
```

### 7.3 回滚验证

```bash
# 检查OA服务已停止
kubectl get deployment ops-agent-service  # 应不存在

# 检查conversation-service健康
kubectl get pod -l app.kubernetes.io/name=conversation-service

# 检查助手列表中无ops-agent
# 通过前端或API验证
```

---

## 八、实施时间线

| 阶段 | 任务 | 预计时间 |
|------|------|---------|
| Phase 1 | OA端：FastAPI服务实现 | 2天 |
| Phase 2 | HCI-TP端：OpsAgentAssistant实现 | 1天 |
| Phase 3 | 部署配置（Docker/Helm） | 1天 |
| Phase 4 | 测试与验证 | 1天 |
| **总计** | | **5天** |

---

## 九、风险与缓解措施

| 风险 | 可能性 | 影响 | 缓解措施 |
|------|---------|------|---------|
| OA执行超时 | 🟡 中 | 🟡 中 | 增加超时时间，流式输出，用户可取消 |
| SOP格式不兼容 | 🟢 低 | 🟡 中 | 先使用本地SOP，后续可对接KB |
| 回滚流程不畅 | 🟢 低 | 🟡 中 | 预演回滚流程，文档清晰 |
| 性能影响 | 🟢 低 | 🟡 中 | 资源限制，独立部署，监控告警 |

---

## 十、后续优化方向

### 10.1 短期优化（可选）

- [ ] SOP知识库双向同步（OA ↔ KB Service）
- [ ] OA工具调用权限控制
- [ ] OA轨迹导入HCI-TP审计系统

### 10.2 长期演进（按需）

- [ ] 将OA工具系统拆解为独立服务
- [ ] 深度集成到HCI-TP诊断状态机
- [ ] 支持更多OA特性作为可选助手类型

---

## 附录

### A. 完整文件清单

**OA端新增文件：**
- ops_agent/server/main.py
- ops_agent/server/openai_compat.py
- ops_agent/server/otel_integration.py
- Dockerfile.ops-server

**HCI-TP端修改文件：**
- backend/conversation-service/app/services/ai_client.py
- backend/conversation-service/app/config.py
- backend/conversation-service/app/main.py

**HCI-TP端新增文件：**
- deploy/helm/hci-platform/templates/ops-agent-service/service.yaml
- deploy/helm/hci-platform/templates/ops-agent-service/deployment.yaml
- deploy/helm/hci-platform/templates/ops-agent-service/configmap.yaml
- deploy/docker/docker-compose.opsagent.yaml

### B. 相关文档

- [Ops-Agent架构设计文档](../../../../ops-agent/docs/架构设计文档.md)
- [Ops-Agent使用手册](../../../../ops-agent/docs/项目使用手册.md)
- [HCI-TP AI助手设计](./AI助手设计.md)
