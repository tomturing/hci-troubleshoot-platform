# data-pipeline/kbd 配置字段参考

> **本文件替代了旧的"API 配置统一说明"（描述的是已被推翻的 #504 markdownify 方案）。**
> **新架构（P0–P2，2026-07 重构）下，data-pipeline/kbd 只与 kb-service API 交互；**
> **LLM 调用、知识库数据所有权、Vision 任务编排全部由 kb-service 后端负责。**

---

## 配置文件位置

所有配置通过 `data-pipeline/kbd/.env` 注入（推荐用 1Password / Vault 注入敏感值）。也可通过环境变量直接传入。

---

## 配置字段分类

### 1. 数据抓取（Stage 1: FETCH）

| 字段 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `SANGFOR_API_BASE` | 否 | `https://support.sangfor.com.cn` | 深信服技术支持门户 Base URL |
| `SANGFOR_COOKIE` | **是** | `""` | 认证 Cookie，从浏览器 DevTools 复制 |
| `SANGFOR_REQUEST_DELAY` | 否 | `0.8` | 抓取间隔（秒），避免限流 |
| `SANGFOR_TIMEOUT` | 否 | `30.0` | 单条 HTTP 请求超时（秒）|
| `SANGFOR_MAX_RETRIES` | 否 | `4` | 失败重试次数（指数退避）|

### 2. Excel 批量输入

| 字段 | 默认 | 说明 |
|------|------|------|
| `EXCEL_FILE` | `<项目根>/案例生产详细数据24-26.xlsx` | 第一列为案例 ID，跳过标题行 |

### 3. 本地存储

| 字段 | 默认 | 说明 |
|------|------|------|
| `KBD_CACHE_DIR` | `data-pipeline/kbd/cache` | 抓取缓存（`{support_id}/raw.json` + `img_N.*`） |
| `KBD_LOGS_DIR` | `data-pipeline/kbd/logs` | 运行日志（`kbd_{run_id}.log`） + 可观测性 progress 文件 |
| `CATEGORY_BASELINE` | `backend/kb-service/config/category_baseline.yaml` | 分类基线（供 classifier 参考） |

### 4. 数据库（连接池）

| 字段 | 默认 | 说明 |
|------|------|------|
| `DATABASE_URL` | `postgresql://hci_user:hci_pass@localhost:5432/hci_db` | asyncpg 连池串（dev 由 .env 覆盖）|
| `DB_POOL_MIN` / `DB_POOL_MAX` | `2` / `10` | 连池容量 |

### 5. 管道并发行为

| 字段 | 默认 | 说明 |
|------|------|------|
| `VISION_CONCURRENCY` | `3` | VISION 阶段多案例并发提交数（信号量控制，kb-service 后端 _VISION_CONCURRENCY 同步为 3） |

### 6. 行为阈值

| 字段 | 默认 | 说明 |
|------|------|------|
| `MIN_IMAGE_SIZE` | `2048` | 低于此字节视为无效图片（icon / 占位图） |
| `MIN_CLASSIFY_CONFIDENCE` | `0.5` | AI 分类置信度低于此值时，draft 标记"需人工重新分类" |

### 7. kb-service API（pipeline 调 API 必填）

| 字段 | 默认 | 说明 |
|------|------|------|
| `KB_SERVICE_URL` | `http://localhost:8004` | kb-service 内部地址；dev K3s 环境需先 `kubectl port-forward` |
| `INTERNAL_API_TOKEN` | `hci-dev-internal-token` | 内部 Bearer Token（与 kb-service env 一致） |
| `API_TIMEOUT` | `30.0` | API 请求超时（秒）|
| `API_MAX_RETRIES` | `3` | 失败重试（指数退避）|

---

## 完整 `.env` 模板

```bash
# === 抓取（必填）===
SANGFOR_COOKIE=PHPSESSID=...;_pk_id.*=...;...

# === kb-service API ===
KB_SERVICE_URL=http://localhost:8004
INTERNAL_API_TOKEN=hci-dev-internal-token

# === 数据库（dev 环境通常由 .env 自动注入）===
# DATABASE_URL=postgresql+asyncpg://hci_admin:dev_postgres_passwd_2026@postgres:5432/hci_troubleshoot
```

---

## 字段变更履历

- **2026-07 P0–P2 重构**：
  - 删除 `API_KEY` / `BASE_URL` / `VISION_MODEL` / `ANALYSIS_MODEL` / `CLASSIFY_MODEL`
    （data-pipeline 不再直接调 LLM，Vision/分类由 kb-service 后端 `LLM_*`/`VISION_MODEL` env 接管）
  - 删除 `ZAI_*` / `DASHSCOPE_*` 等厂商特定字段（统一由 kb-service 后端处理）
  - 新增 `VISION_CONCURRENCY` 字段（多案例异步并发数，对应后端 Semaphore）
  - `API_TIMEOUT` 默认 30s（异步化后无需高超时，轮询 status 端点 900s 上限由 data-pipeline 侧控制）

---

## 相关文档

- 新架构拓扑：`data-pipeline/kbd/README.md` 架构概览
- 原则：仓库根 `CLAUDE.md` / `AGENTS.md` §9 内容–呈现分离
- 后端配置：`backend/kb-service/.env.example`（kb-service 接管了 LLM / Vision / DB 配置）
