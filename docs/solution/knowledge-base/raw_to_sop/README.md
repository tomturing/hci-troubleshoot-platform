# Raw Graph JSON → SOPNode Markdown 转化工具使用指南

本指南详细说明了如何使用 `raw_to_sop` ETL 工具，将外部 AI 管道输出的 Raw Graph JSON（图/状态机结构）格式的数据转换为符合排障平台要求的标准决策树 Markdown，并将其录入至数据库中。

---

## 一、 前置准备

使用工具前，需进行依赖项安装和环境变量配置：

### 1.1 安装依赖
确保在 Python 3.12 虚拟环境下运行并安装依赖包：
```bash
cd /aihci/hci-troubleshoot-platform/data-pipeline
# 使用 uv 或直接通过虚拟环境 pip 安装
uv pip install python-dotenv httpx
```

### 1.2 环境变量配置
在 `data-pipeline/raw_to_sop/` 目录下创建并编辑 `.env` 文件：
```bash
cd /aihci/hci-troubleshoot-platform/data-pipeline/raw_to_sop
cp .env.example .env
```
修改其中的配置项：
```ini
# API Gateway/kb-service 入口
KB_SERVICE_URL=http://localhost:8004

# 内部鉴权秘钥（必须匹配 kb-service 环境）
INTERNAL_API_TOKEN=your-service-api-token
```
> **提示**：若在 Staging 环境中，可在 K8s pod 中获取 `INTERNAL_API_TOKEN`：
> `kubectl exec -n hci-staging deploy/kb-service -- env | grep INTERNAL_API_TOKEN`

---

## 二、 使用方法

所有的命令均需在 `/aihci/hci-troubleshoot-platform/data-pipeline` 目录下作为 Python 模块运行：
```bash
cd /aihci/hci-troubleshoot-platform/data-pipeline
```

### 2.1 模式 A：导出到 Markdown 文件 (Dry-run)
此模式下，工具会将 Raw Graph JSON 格式的数据进行解析，并在本地指定目录输出结构化的 Markdown，**不与数据库或微服务发生 API 通信**。

#### 2.1.1 导出单个文件
将 `raw/内存ECC故障.json` 转换为 Markdown 并写入 `./out/` 目录：
```bash
/aihci/hci-troubleshoot-platform/.venv/bin/python3 -m raw_to_sop \
  --file raw/内存ECC故障.json \
  --dry-run
```

#### 2.1.2 导出至指定目录
```bash
/aihci/hci-troubleshoot-platform/.venv/bin/python3 -m raw_to_sop \
  --file raw/内存ECC故障.json \
  --dry-run \
  --output-dir /tmp/sop_markdowns
```

#### 2.1.3 在终端中直接预览输出内容
```bash
/aihci/hci-troubleshoot-platform/.venv/bin/python3 -m raw_to_sop \
  --file raw/内存ECC故障.json \
  --dry-run \
  --stdout
```

---

### 2.2 模式 B：直接导入到数据库 (Ingest)
此模式下，工具将解析 JSON 生成 Markdown 内容，并计算其 Hash 校验值作为幂等密钥，直接调用 `kb-service` 的 `/api/sop/ingest` 入库。

* 注意：所有导入的 SOP 默认生成为 **`draft`**（草稿）状态，需登录管理后台（Admin UI）进行校验确认后，点击「发布/Approve」正式激活为 production 态决策树提供给 Agent 推理使用。

#### 2.2.1 导入单个 SOP 记录并绑定分类
```bash
/aihci/hci-troubleshoot-platform/.venv/bin/python3 -m raw_to_sop \
  --file raw/内存ECC故障.json \
  --category-id "硬件-内存"
```

#### 2.2.2 批量导入目录下所有 JSON 故障图
```bash
/aihci/hci-troubleshoot-platform/.venv/bin/python3 -m raw_to_sop \
  --dir raw/ \
  --category-id "硬件"
```
批量运行结束后会在终端显示每个文件的执行状态和 `document_id`。
