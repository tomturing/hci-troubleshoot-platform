# 意图识别评估脚本使用手册

## 概述

`intent_eval.py` 是一个用于批量评估 AI 意图识别准确性的脚本。它能够：

1. 从 Excel 文件读取问题描述
2. 批量创建工单并触发 AI 意图识别
3. 支持断点续传（中途中断后可继续）
4. 将 AI 回复内容导出到 Excel

## 快速开始

### 1. 查看帮助

```bash
# 查看总帮助
python scripts/evaluation/intent_eval.py --help

# 查看子命令帮助
python scripts/evaluation/intent_eval.py run --help
python scripts/evaluation/intent_eval.py export --help
python scripts/evaluation/intent_eval.py status --help
```

### 2. 运行评估

```bash
# 基本用法
python scripts/evaluation/intent_eval.py run --input data.xlsx

# 指定输出目录
python scripts/evaluation/intent_eval.py run --input data.xlsx --output-dir ./output

# 指定 API 地址
python scripts/evaluation/intent_eval.py run --input data.xlsx --api http://172.22.73.249
```

### 3. 查看进度

```bash
# 查看当前评估进度
python scripts/evaluation/intent_eval.py status

# 指定输出目录
python scripts/evaluation/intent_eval.py status --output-dir ./output
```

### 4. 导出结果

```bash
# 将 AI 回复导出到 Excel
python scripts/evaluation/intent_eval.py export --input data.xlsx --output result.xlsx

# 指定结果文件
python scripts/evaluation/intent_eval.py export --input data.xlsx --results ./output/eval_results.json
```

## 命令详解

### `run` - 运行评估

| 参数 | 简写 | 必填 | 默认值 | 说明 |
|-----|------|------|-------|------|
| `--input` | `-i` | 是 | - | 输入 Excel 文件路径 |
| `--api` | `-a` | 否 | `http://172.22.73.249` | API 地址 |
| `--assistant` | - | 否 | `glm-5` | AI 助手类型 |
| `--output-dir` | `-o` | 否 | 脚本所在目录 | 输出目录 |

**示例：**

```bash
# 处理 HCI-虚拟机开关机非P4-20260101-260515.xlsx
python scripts/evaluation/intent_eval.py run \
  --input HCI-虚拟机开关机非P4-20260101-260515.xlsx \
  --output-dir scripts/evaluation/vm_output
```

### `export` - 导出结果

| 参数 | 简写 | 必填 | 默认值 | 说明 |
|-----|------|------|-------|------|
| `--input` | `-i` | 是 | - | 原始 Excel 文件路径 |
| `--output` | `-o` | 否 | 原文件名-结果.xlsx | 输出 Excel 文件路径 |
| `--results` | `-r` | 否 | eval_results.json | 评估结果 JSON 文件 |
| `--api` | `-a` | 否 | `http://172.22.73.249` | API 地址 |

**示例：**

```bash
# 导出 AI 回复到 Excel
python scripts/evaluation/intent_eval.py export \
  --input HCI-虚拟机开关机非P4-20260101-260515.xlsx \
  --output HCI-虚拟机开关机非P4-20260101-260515-结果.xlsx
```

### `status` - 查看进度

| 参数 | 简写 | 必填 | 默认值 | 说明 |
|-----|------|------|-------|------|
| `--output-dir` | `-o` | 否 | 脚本所在目录 | 输出目录 |

**示例：**

```bash
python scripts/evaluation/intent_eval.py status
```

**输出示例：**

```
======================================================================
评估进度状态
======================================================================
最后处理: 第 360 条
总数: 712
更新时间: 2026-05-18T11:27:15.681342
进度: 50.6%
成功记录: 340
失败记录: 20
======================================================================
```

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
python scripts/evaluation/intent_eval.py run --input data.xlsx
# Ctrl+C 中断

# 再次运行，从第 101 条继续
python scripts/evaluation/intent_eval.py run --input data.xlsx
# 输出: 📦 断点续传: 从第 101 条继续 (共 712 条)
```

## 常见问题

### 1. API 不可达

**错误信息**：`❌ API 不可达: ...`

**解决方案**：
- 检查 K3s 服务是否正常运行
- 确认 API 地址是否正确（默认 `http://172.22.73.249`）
- 使用 `--api` 参数指定正确的地址

### 2. Excel 文件不存在

**错误信息**：`❌ Excel 文件不存在: ...`

**解决方案**：
- 检查文件路径是否正确
- 使用绝对路径或相对于当前目录的路径

### 3. 进度文件不存在

**错误信息**：`❌ 未找到进度文件，可能尚未开始评估`

**解决方案**：
- 先运行 `run` 命令开始评估
- 使用 `--output-dir` 指定正确的输出目录

## 完整示例

```bash
# 1. 准备 Excel 文件
ls HCI-虚拟机开关机非P4-20260101-260515.xlsx

# 2. 运行评估
python scripts/evaluation/intent_eval.py run \
  --input HCI-虚拟机开关机非P4-20260101-260515.xlsx \
  --output-dir scripts/evaluation/vm_output

# 3. 查看进度（另开终端）
python scripts/evaluation/intent_eval.py status \
  --output-dir scripts/evaluation/vm_output

# 4. 导出结果
python scripts/evaluation/intent_eval.py export \
  --input HCI-虚拟机开关机非P4-20260101-260515.xlsx \
  --output-dir scripts/evaluation/vm_output

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