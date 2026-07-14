"""
关键信号 LLM 提取 Prompt 模板

定义了从 KBD/SOP 自然语言文本提取信号的统一 Prompt
"""

KEY_SIGNAL_EXTRACTION_PROMPT = """## 任务
你是一个 HCI 超融合平台排障专家，需要从以下自然语言描述中提取关键信号。

## 输入
<troubleshooting_text>
{text}
</troubleshooting_text>

## 关键信号分类
根据文本描述的语义，判别信号类别：

### 前端信号（frontend）
如果文本描述涉及以下内容，提取为 FrontendSignal：
- 告警信息检查（如"检查某告警是否存在"）
- 任务状态查询（如"查看失败任务"、"任务执行情况"）
- 交互弹框日志（如"弹窗确认日志"、"对话记录"）

前端信号示例：
```json
{
  "signal_category": "frontend",
  "query": "alert",
  "keyword": "配置存储服务备节点异常",
  "description": "检查备节点异常告警"
}
```

### 后端信号（backend）
如果文本描述涉及以下内容，提取为 BackendSignal：
- 日志内容检索（如"在 mysql.log 中搜索错误"）
- 服务状态检查（如"检查 redis 服务"、"确认某服务运行"）
- 系统指标检查（如"检查 CPU/内存/磁盘"）
- 网络/存储状态诊断

后端信号示例：
```json
{
  "signal_category": "backend",
  "signal_type": "log_keyword",
  "target": {
    "scope": "{{HOST}}",
    "resource": "mysql-managed.log",
    "path": "/sf/data/platform_database/"
  },
  "keywords": ["file system read-only"],
  "expected": true,
  "description": "在备节点检查数据库日志是否有只读错误"
}
```

## 提取规范

1. **信号类别判别**：
   - 必须首先判断是 frontend 还是 backend
   - 如果提到告警/任务/弹框 → frontend
   - 如果提到日志/服务/系统指标 → backend

2. **关键字提取**：
   - keyword 必须是核心检索词，不要包含具体命令
   - 示例：文本"检查配置存储服务备节点异常告警" → keyword="备节点异常"

3. **变量占位符**（backend 专用，ADR-2 强制 `{{全大写}}`）：
   - 如果文本提到"在备节点"/"故障节点"，使用 `{{HOST}}` 占位符
   - 如果文本提到时间范围，使用 `{{END}}` 占位符
   - 占位符必须全大写双花括号（如 `{{HOST}}`），小写/单花括号（`${host}`/`{host}`）一律非法，抽取期会被 `validate_placeholder_case` 拒绝
   - 这样可以在运行时自动注入前端信号提取的变量值

4. **expected 字段**（backend 专用）：
   - 如果文本说"检查是否有报错" → expected=true（期望出现）
   - 如果文本说"确认服务正常"/"检查无报错" → expected=false（期望不出现）

## 输出 JSON 格式
必须输出严格的 JSON，不要有多余说明：

```json
{
  "signal_category": "frontend 或 backend",
  "keyword": "核心检索词",
  "description": "步骤说明",
  // frontend 时补充以下字段
  "query": "alert/task/dialog",
  "is_failed": false,
  // backend 时补充以下字段
  "signal_type": "log_keyword/service_status/vm_state/network_check/storage_state/hardware_state/platform_state/system_metric",
  "target": {
    "scope": "{{HOST}}",
    "resource": "具体资源名",
    "path": "日志路径"
  },
  "keywords": ["判定关键字"],
  "match_mode": "any",
  "expected": true
}
```

请直接输出 JSON，不要有任何其他文字。
"""

KEY_SIGNAL_BATCH_EXTRACTION_PROMPT = """## 任务
你是一个 HCI 超融合平台排障专家，需要从以下自然语言描述中批量提取多个关键信号。

## 输入
<troubleshooting_text>
{text}
</troubleshooting_text>

## 关键信号分类
详细分类规则请参考单个信号提取 Prompt。简述如下：

### 前端信号（frontend）
- 涉及告警/任务/弹框查询 → FrontendSignal

### 后端信号（backend）
- 涉及日志/服务/系统诊断 → BackendSignal

## 提取规范
1. 识别文本中的所有排查步骤
2. 每个步骤提取为一个信号对象
3. 判别每个信号的类别（frontend/backend）
4. 按 KBD/SOP 文本顺序输出

## 输出 JSON 格式
输出 JSON 数组，每个元素是一个信号：

```json
[
  {
    "signal_category": "frontend",
    "query": "alert",
    "keyword": "备节点异常",
    "description": "步骤1：检查告警"
  },
  {
    "signal_category": "backend",
    "signal_type": "log_keyword",
    "target": {
      "scope": "{{HOST}}",
      "resource": "mysql-managed.log",
      "path": "/sf/data/platform_database/"
    },
    "keywords": ["file system read-only"],
    "expected": true,
    "description": "步骤2：在备节点检查数据库日志"
  }
]
```

请直接输出 JSON 数组，不要有任何其他文字。
"""

__all__ = [
    "KEY_SIGNAL_EXTRACTION_PROMPT",
    "KEY_SIGNAL_BATCH_EXTRACTION_PROMPT",
]
