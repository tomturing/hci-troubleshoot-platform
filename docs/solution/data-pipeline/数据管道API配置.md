# data-pipeline API 配置统一说明

## 变更概述

将 Vision API 和分类 API 的配置统一到 `/data-pipeline/kbd/.env` 文件，使用通用字段名，不绑定特定 API 提供商。

## 配置文件

### `/data-pipeline/kbd/.env`

```bash
# LLM API 配置（OpenAI-compatible）
# 安全提示：禁止在文档中硬编码真实密钥。请通过环境变量注入，并在密钥泄露后立即到阿里云百炼控制台轮换。
API_KEY=${DASHSCOPE_API_KEY}
BASE_URL=https://coding.dashscope.aliyuncs.com/v1

# 模型配置
VISION_MODEL=qwen3.7-plus      # 图片处理
CLASSIFY_MODEL=qwen3.7-plus    # 案例分类
ANALYSIS_MODEL=qwen3.7-plus    # 文本分析
```

## 字段说明

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `API_KEY` | LLM API 密钥 | 必填 |
| `BASE_URL` | LLM API 基础 URL | 必填 |
| `VISION_MODEL` | 图片处理模型 | `qwen3.7-plus` |
| `CLASSIFY_MODEL` | 案例分类模型 | `qwen3.7-plus` |
| `ANALYSIS_MODEL` | 文本分析模型 | `qwen3.7-plus` |

## 代码修改

### 1. `config.py`（精简）

```python
# ── LLM API 配置 ─────────────────────────────────────────────────────────────
API_KEY: str = Field(default="", description="LLM API Key")
BASE_URL: str = Field(default="", description="LLM API Base URL")

VISION_MODEL: str = Field(default="qwen3.7-plus", description="Vision 模型")
CLASSIFY_MODEL: str = Field(default="qwen3.7-plus", description="分类模型")
ANALYSIS_MODEL: str = Field(default="qwen3.7-plus", description="分析模型")
```

### 2. `classify.py`（kb-service）

```python
# LLM 配置（从环境变量读取）
LLM_BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
LLM_API_KEY = os.environ.get("API_KEY", "")
LLM_MODEL = os.environ.get("CLASSIFY_MODEL", "qwen3.7-plus")
```

### 3. `image_proc.py`

```python
client = AsyncOpenAI(
    api_key=settings.API_KEY,
    base_url=settings.BASE_URL,
    timeout=settings.LLM_TIMEOUT,
)
```

## 模型支持情况

### ✅ 支持的模型（DashScope API）

| 模型 | 类型 | Vision | 文本 | 说明 |
|------|------|--------|------|------|
| `qwen3.7-plus` | 通用 | ✅ | ✅ | 推荐，性能最强 |
| `qwen3.6-plus` | 通用 | ✅ | ✅ | 性能优秀 |
| `qwen3.5-plus` | 通用 | ✅ | ✅ | 性能良好 |
| `kimi-k2.5` | 通用 | ✅ | ✅ | Moonshot AI |
| `glm-4.7` | 文本 | ❌ | ✅ | 智谱 AI |
| `qwen3-coder-plus` | 编程 | ❌ | ✅ | 代码专用 |
| `MiniMax-M2.5` | 通用 | ❌ | ✅ | MiniMax |

### ❌ 不支持的模型

- `glm-5.2`、`glm-5.1`、`glm-5`
- `qwen-vl-max`、`qwen-vl-plus`

## 使用建议

### 1. Vision 模型（图片处理）

推荐使用 `qwen3.7-plus`：
- 支持 Vision 能力（image_url 输入）
- 文字提取能力强，适合复杂终端/日志截图
- 支持长文本输出（MAX_TOKENS 可设为 8192）

### 2. 分类模型（案例分类）

推荐使用 `qwen3.7-plus`：
- 文本理解能力强
- 语义匹配准确

### 3. 分析模型（文本分析）

推荐使用 `qwen3.7-plus`：
- 推理能力强
- 输出质量高

## 配置变更

- ✅ 统一使用 `API_KEY` 和 `BASE_URL` 通用字段
- ✅ 移除所有提供商特定字段（如 `ZAI_*`、`DASHSCOPE_*`）
- ✅ 移除向后兼容逻辑
- ✅ 精简代码和注释
- ✅ 支持轻松切换 API 提供商（只需修改 `BASE_URL`）

## 验证配置

```bash
cd /mnt/d/aihci/hci-troubleshoot-platform/data-pipeline/kbd
uv run python3 -c "
from config import settings
print('API_KEY:', settings.API_KEY[:20] + '...')
print('BASE_URL:', settings.BASE_URL)
print('VISION_MODEL:', settings.VISION_MODEL)
print('CLASSIFY_MODEL:', settings.CLASSIFY_MODEL)
"
```

输出：
```
API_KEY: ${DASHSCOPE_API_KEY}
BASE_URL: https://coding.dashscope.aliyuncs.com/v1
VISION_MODEL: qwen3.7-plus
CLASSIFY_MODEL: qwen3.7-plus
```


## Prompt 统一管理（2026-07-09 变更）

### 变更概述

Vision Prompt 和分类 Prompt 已从文件/硬编码迁移到数据库 `system_prompt` 表，与 TriageAgent 保持统一一致。

### 新架构

**改造前**：
- 分类 Prompt：硬编码在 `backend/kb-service/app/routes/classify.py`
- Vision Prompt：存放在 `data-pipeline/kbd/prompt/image_proc_vision_v4.txt`

**改造后**：
- 分类 Prompt：`system_prompt` 表记录 `kbd_classify_v1`
- Vision Prompt：`system_prompt` 表记录 `kbd_vision_v1`
- 管理入口：admin-ui Prompt 管理页面（在线编辑，热生效）

### data-pipeline 改造

**`image_proc.py`** 已重构为薄封装：

```python
# 原架构（已移除）
async def _process_image(client, img_path, context):
    prompt = _PROMPT.format(context=context)  # Prompt 从文件读取
    response = await client.chat.completions.create(...)  # 本地调用 LLM
    
# 新架构
async def process_images_batch(kbd_ids: list[str]):
    # 调用 kb-service API，Prompt 从数据库热加载
    url = f"{settings.KB_SERVICE_URL}/api/admin/kbd/{kbd_entry_id}/reanalyze-images"
    response = await client.post(url, headers=headers)
```

**变更内容**：
- 移除 `_PROMPT_PATH`、`_PROMPT` 常量和 `_vision_analyze` 函数
- 移除本地文件加载逻辑
- `process_images_batch()` 改为调用 kb-service API
- 与 `classifier.py` 的 API 调用模式保持一致

### 核心逻辑迁移

Vision 处理核心逻辑已迁移到：

**`backend/kb-service/app/services/vision_processor.py`**

主要函数：
- `reanalyze_kbd_images(kbd_entry_id, db_session)` - 入口函数
- `_vision_analyze()` - Vision LLM 单次调用
- `_extract_context()` - 图片上下文提取
- `_compress_image_if_needed()` - 图片压缩

Prompt 加载方式：

```python
from shared.utils.prompt_loader import StrictPromptLoader

prompt_template = await StrictPromptLoader.load_and_validate(
    db_session,
    "kbd_vision_v1",  # 从 system_prompt 表加载
    ["context"],
    consumer="kb-service.vision_processor",
)
```

### 在线重算

**admin-ui KBD 审核页面**新增两个按钮：

| 按钮 | API | 说明 |
|------|------|------|
| 重新分类 | `POST /api/v1/kbd/{id}/reclassify` | 用最新 Prompt 重算分类 |
| 重新识图 | `POST /api/v1/kbd/{id}/reanalyze-images` | 用最新 Prompt 重算识图 |

**流程**：
1. 在 admin-ui 修改 Prompt
2. 进入 KBD 审核页面，点击按钮触发重算
3. 无需重启服务，无需运行后台脚本

### 删除的文件

- `data-pipeline/kbd/prompt/image_proc_vision_v4.txt`（内容已迁入数据库）

### 新增依赖

**`backend/kb-service/pyproject.toml`**：

```toml
pillow>=10.0.0  # 图片压缩
```

运行 `uv sync` 安装新依赖。
