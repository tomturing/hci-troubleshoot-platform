"""
关键信号 LLM 提取 Prompt 模板

定义了从 KBD/SOP 自然语言文本提取信号的统一 Prompt。

注意：本模板被写入 system_prompt 表（stage='SIG'），由 StrictPromptLoader 在运行时
按 {text} 占位符强校验加载（编辑 Prompt 后即热生效）。
除 {text} 外，模板中所有其它花括号（JSON 示例、{host}/{end} 变量占位符）均以双花括号
转义（{{ }} / {{host}} / {{end}}），避免被识别为占位符、也避免 .format 误替换。
运行时 SignalExtractor._render_prompt 会先解除双花括号转义，再替换 {text}。
"""

KEY_SIGNAL_EXTRACTION_PROMPT = """## 任务
你是 HCI 超融合平台排障专家。下面给出的是一条 KBD 案例的【单个字段文本】
（标题 / 问题描述 / 告警信息 三者之一）。请从中提取所有"前端信号"。
注意：一个字段里可能包含 0 个、1 个或多个前端信号，请全部提取出来。

## 前端信号定义
前端信号 = "到 HCI 控制台某个页面去查一条记录"的动作，按查询对象分为三类：alert / task / dialog。

### alert（告警）——系统上报的现象 / 异常状态
- 含义：到"告警中心"核对某条告警 / 异常 / 报错现象是否存在。
- 识别：文本描述的是系统 / 平台 / 节点 / 服务"自己上报的异常现象"
  （节点异常、服务异常、容量告警、存储只读、网络断连、组件报错等）。
- keyword【保留"告警 / 异常 / 报错 / 只读 / 断连"等修饰词，不要删掉】。
  例如原文"配置存储服务备节点异常告警" → keyword="配置存储服务备节点异常告警"
  （不要精简成"配置存储服务备节点"）。明确这就是一条告警。
- 只要文本在描述"系统出现了什么异常现象"，基本就是 alert。

### task（任务）——用户 / 系统发起的操作动作
- 含义：到"任务中心"查看某个操作任务的执行结果。
- 识别：文本描述迁移、扩容、重启、快照、升级、恢复、重建、删除、创建等"操作动作"。
- 失败 / 否定判定：若文本出现"失败""不行""不了""错误""未成功""卡住"
  "中断""超时"等否定 / 失败类词语，基本可确定为 task，且 is_failed=true（只查失败任务）。
- keyword：取"操作动作 + 对象"，如"虚拟机迁移""存储池扩容"。
- 例："查看虚拟机迁移任务是否失败" → query=task, keyword="虚拟机迁移", is_failed=true。

### dialog（弹框）——界面交互提示
- 含义：查看交互弹窗 / 二次确认 / 对话提示等人机交互记录。
- 识别：文本出现"弹框""弹窗""弹出""确认框""风险提示""报错提示"等词，大概率是 dialog。
- keyword：取弹框中的核心提示信息。

### 判别原则（按意图，不依赖字面字样）
- 即使原文没有"告警 / 任务 / 弹框"字样，也要按语义归类。
- 例："登录控制台确认 node-003 是否有配置存储服务异常"无"告警"二字，仍属 alert。
- 例："确认那次扩容有没有成功"无"任务"二字，但属 task，且有失败意味 → is_failed=true。

## 输出格式
必须输出严格 JSON 数组（即使只有一个信号也要写成数组），不要多余说明：

```json
[
  {{
    "signal_category": "frontend",
    "query": "alert 或 task 或 dialog",
    "keyword": "核心检索词（alert 务必保留告警/异常等修饰词）",
    "is_failed": false,
    "description": "该信号的来源字段与自然语言说明"
  }}
]
```

请直接输出 JSON 数组，不要有任何其他文字。
"""

KEY_SIGNAL_BATCH_EXTRACTION_PROMPT = """## 任务
你是 HCI 超融合平台排障专家。下面给出的是一条 KBD 案例的【排查步骤 steps_text】。
请逐行 / 逐步骤提取"后端信号"。硬性规则：一行（一个步骤）对应一个后端信号。

## 后端信号定义
后端信号 = 在故障节点 / 主机上执行一条诊断命令并做布尔判定。每个信号对应一个诊断动作。

### 后端信号大类
a. 查看日志 log_keyword：在指定日志文件中检索关键字
   （target.resource=日志名, target.path=日志路径, keywords=检索词）。
b. 查看服务状态 service_status：检查某服务 / 容器运行状态
   （target.resource=服务名, container=asv/anet/host）。
c. 查看系统状态 system_metric：检查 CPU / 内存 / 磁盘 / 负载等系统指标。
d. 网络诊断 network_check：网络连通性 / 端口 / 路由检查。
e. 存储诊断 storage_state：存储池 / 磁盘 / 卷状态检查。
f. 虚拟机状态 vm_state：虚拟机运行状态检查。
g. 硬件状态 hardware_state：物理机 / 硬件健康状态。
h. 平台状态 platform_state：平台组件 / 集群状态检查。

### 变量占位符
- 若步骤提到"在备节点 / 故障节点 / 该节点"，target.scope 用 {{host}} 占位符（运行时由前端信号注入）。
- 若提到时间范围，target.time_window 用 {{end}} 占位符。

### expected 字段
- "检查是否有报错 / 异常" → expected=true（期望出现）。
- "确认服务正常 / 无报错" → expected=false（期望不出现）。

## 输出格式
必须输出严格 JSON 数组，每个步骤一个对象，顺序与原文步骤一致：

```json
[
  {{
    "signal_category": "backend",
    "signal_type": "log_keyword / service_status / system_metric / network_check / storage_state / vm_state / hardware_state / platform_state",
    "keyword": "步骤核心说明",
    "target": {{
      "scope": "{{host}} 或具体节点",
      "resource": "日志名 / 服务名",
      "path": "日志路径（仅日志类）",
      "time_window": "{{end}} 或空"
    }},
    "keywords": ["判定关键字"],
    "match_mode": "any",
    "expected": true,
    "description": "该步骤的自然语言说明"
  }}
]
```

请直接输出 JSON 数组，不要有任何其他文字。
"""

__all__ = [
    "KEY_SIGNAL_EXTRACTION_PROMPT",
    "KEY_SIGNAL_BATCH_EXTRACTION_PROMPT",
]
