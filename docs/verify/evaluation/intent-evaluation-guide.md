# 意图识别评估脚本使用手册

## 概述

`intent_eval.py` 是一个用于批量评估 AI 意图识别准确性的脚本。它能够：

1. 从 Excel 文件读取问题描述
2. 批量创建工单并触发 AI 意图识别
3. 支持断点续传（中途中断后可继续）
4. 将 AI 回复内容导出到 Excel

## 快速开始

### 1. 修改配置参数

`intent_eval.py` 为独立脚本，配置直接写在 `main()` 函数顶部，运行前需编辑以下变量：

```python
# scripts/evaluation/intent_eval.py — main() 函数顶部
excel_path     = Path("/path/to/HCI-xxx.xlsx")  # 输入 Excel 文件路径
api_base_url   = "http://172.22.73.249"           # HCI API 地址
assistant_type = "glm-5"                           # AI 助手类型
output_dir     = Path(__file__).parent             # 输出目录（默认脚本目录）
```

### 2. 运行评估

```bash
# 修改配置后直接执行
python scripts/evaluation/intent_eval.py
```

支持 `Ctrl+C` 安全中断，下次运行自动从断点继续。

### 3. 查看进度

```bash
# 查看断点续传进度文件
cat scripts/evaluation/progress.json
```

### 4. 导出结果

```bash
# 修改 export_ai_responses.py 中的路径后运行
python scripts/evaluation/export_ai_responses.py
```

## 配置参数

| 参数 | 说明 | 默认值 |
|------|------|-------|
| `excel_path` | 输入 Excel 文件路径 | 脚本目录下 `HCI-内存硬盘非P4-260101-26058.xlsx` |
| `api_base_url` | HCI API 地址 | `http://172.22.73.249` |
| `assistant_type` | AI 助手类型 | `glm-5` |
| `output_dir` | 输出目录 | 脚本所在目录 |

## 输入文件格式

Excel 文件必须包含以下列：

| 列名 | 说明 |
|-----|------|
| 问题编号 | 唯一标识符，如 `Q2026051402151` |
| 问题描述 | 用户描述的问题内容 |

其他列（如产品线、版本、标签等）会被忽略。

## 输出文件

运行评估后，输出目录会生成以下文件：

| 文件 | 说明 |
|-----|------|
| `progress.json` | 进度文件，记录当前处理位置 |
| `eval_results.json` | 成功记录的详细结果 |
| `failed_records.json` | 失败记录及错误原因 |

### progress.json 结构

```json
{
  "last_index": 360,
  "total": 712,
  "updated_at": "2026-05-18T11:27:15.681342"
}
```

### eval_results.json 结构

```json
[
  {
    "index": 1,
    "problem_id": "Q2026051402151",
    "problem_desc": "有3个关机任务残留，现在开机开不了",
    "case_id": "Q2026051820185",
    "conversation_id": "xxx-xxx-xxx",
    "assistant_type": "glm-5",
    "ai_response_preview": "根据您的描述...",
    "status": "success",
    "processed_at": "2026-05-18T11:26:00"
  }
]
```

### failed_records.json 结构

```json
[
  {
    "index": 5,
    "problem_id": "Q2026051400001",
    "problem_desc": "问题描述...",
    "error": "错误原因",
    "failed_at": "2026-05-18T11:27:00"
  }
]
```

## 断点续传

脚本支持断点续传功能：

1. **中断处理**：按 `Ctrl+C` 可安全中断，进度会自动保存
2. **继续执行**：再次运行相同命令，会自动从上次中断的位置继续
3. **进度保存**：每处理 10 条记录自动保存一次进度

**示例：**

```bash
# 第一次运行，处理到第 100 条时中断
python scripts/evaluation/intent_eval.py
# Ctrl+C 中断

# 再次运行，从第 101 条继续
python scripts/evaluation/intent_eval.py
# 输出: 📦 断点续传: 从第 101 条继续 (共 712 条)
```

## 常见问题

### 1. API 不可达

**错误信息**：`❌ API 不可达: ...`

**解决方案**：
- 检查 K3s 服务是否正常运行
- 确认 `intent_eval.py` 中 `api_base_url` 配置是否正确（默认 `http://172.22.73.249`）

### 2. Excel 文件不存在

**错误信息**：`❌ Excel 文件不存在: ...`

**解决方案**：
- 检查 `intent_eval.py` 中 `excel_path` 配置是否指向正确文件
- 确认文件已存在于指定路径

### 3. 进度文件不存在

**错误信息**：`❌ 未找到进度文件，可能尚未开始评估`

**解决方案**：
- 先运行脚本开始评估，`progress.json` 会自动生成
- 确认 `output_dir` 配置与查看的目录一致

## 完整示例

```bash
# 1. 编辑 intent_eval.py，修改配置参数
vim scripts/evaluation/intent_eval.py
# 修改: excel_path / api_base_url / assistant_type / output_dir

# 2. 运行评估
python scripts/evaluation/intent_eval.py

# 3. 查看进度（另开终端）
cat scripts/evaluation/vm_output/progress.json

# 4. 导出结果（修改 export_ai_responses.py 中的路径后运行）
python scripts/evaluation/export_ai_responses.py

# 5. 在 admin-ui 查看工单
# 访问 http://172.22.73.249/admin/
```

## 性能参考

| 数据量 | 预计耗时 | 说明 |
|-------|---------|------|
| 100 条 | 约 2 小时 | 每条约 70 秒 |
| 500 条 | 约 10 小时 | |
| 1000 条 | 约 20 小时 | |
| 2306 条 | 约 45 小时 | 实测数据 |

**注意**：实际耗时取决于 AI API 响应速度和网络状况。