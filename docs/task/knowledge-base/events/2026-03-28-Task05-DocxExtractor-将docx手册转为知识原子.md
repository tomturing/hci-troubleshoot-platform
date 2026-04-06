---
status: active
category: task
audience: developer
last_updated: 2026-03-28
owner: team
related: 05
---

# Task 05：DocxExtractor——将 docx 手册转为知识原子（P1）

```
你是一名负责 hci-troubleshoot-platform 数据管道的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
data-pipeline/sop_skills/ 下有一份权威排障手册：虚拟机开关机失败排障手册.docx
这是目前系统中唯一经专家校验的知识来源，覆盖：
  - 31 个错误类型（CPU不足/内存不足/序列号过期/存储不可访问等）
  - 91 个具体根因
  - 134 个诊断/解决步骤
  - 全部使用真实 acli 命令（非虚构工具）

文档结构：
  Heading 1: 虚拟机开关机失败排查流程
  Heading 2: 有启动虚拟机失败任务 / 无启动虚拟机失败任务 / 任务完成
  Heading 3: CPU不足 / 内存不足 / 内部异常 / 序列号过期 ... (31个)
  Heading 4: 根因类型（内存泄露/内存耗尽等）
  Heading 5: 判断方法 / 解决方案

需要实现 DocxExtractor，将此文档自动转为 knowledge_atoms 表的数据。

前置条件：Task 04（知识原子数据库设计）已完成。

【任务目标】
1. 实现 data-pipeline/atoms/docx_extractor.py
2. 从 docx 提取 knowledge_atoms，正确填充所有字段
3. 提取错误码并写入 error_code_index（如 0x010032F5 等）
4. 将提取结果写入 knowledge_atoms 表（需 KB Service 运行）
5. 验证至少 25 个知识原子成功写入并可检索

【涉及服务 / 文件范围】
允许修改/新建：
  - data-pipeline/atoms/（新建目录）
  - data-pipeline/atoms/__init__.py
  - data-pipeline/atoms/docx_extractor.py（核心实现）
  - data-pipeline/atoms/atom_writer.py（写入 KB Service）
  - scripts/dev/run_docx_extraction.py（执行脚本）
只读参考：
  - data-pipeline/sop_skills/虚拟机开关机失败排障手册.docx（原始来源）
  - docs/architecture/知识库重建设计方案.md § 三、核心设计（知识原子模型）
  - data-pipeline/config/category_baseline.yaml（分类基准）
  - database 中 knowledge_atoms 表结构
禁止：
  - 修改 backend/ 下任何服务代码

【详细实现步骤】

Step 1：分析 docx 结构（运行以下命令确认）

```python
import docx
doc = docx.Document('data-pipeline/sop_skills/虚拟机开关机失败排障手册.docx')
from collections import Counter
styles = Counter(p.style.name for p in doc.paragraphs if p.text.strip())
print(dict(styles))
# 应看到：Heading 1~5, Normal, _Style 15
```

Step 2：实现 DocxExtractor

```python
# data-pipeline/atoms/docx_extractor.py
"""将 docx 排障手册解析为知识原子列表"""
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import docx

# 错误码正则（如 0x010032F5、0x00000001）
ERROR_CODE_PATTERN = re.compile(r'0[xX][0-9A-Fa-f]{6,8}')

# 分类映射：H2 场景 → 触发条件类型
SCENARIO_MAP = {
    '有启动虚拟机失败任务': {
        'scenario': 'has_failed_task',
        'trigger_tool': 'acli_task_get',
    },
    '无启动虚拟机失败任务': {
        'scenario': 'no_failed_task',
    },
    '任务完成': {
        'scenario': 'task_completed',
    },
}

@dataclass
class KnowledgeAtomDraft:
    """知识原子草稿，提取后写入 DB"""
    id: str = field(default_factory=lambda: f"ka-{uuid.uuid4().hex[:12]}")
    type: str = ""              # diagnostic_step|fix_action|decision_gate|background
    category_id: str = "虚拟机-003"   # 本手册固定为虚拟机开机失败
    category_path: list = field(default_factory=list)
    knowledge_domain: str = "sop"
    trigger: dict = field(default_factory=dict)
    content: dict = field(default_factory=dict)
    applicable_version_min: Optional[str] = "5.0"
    applicable_version_max: Optional[str] = None
    source_type: str = "docx"
    source_ref: str = ""
    confidence: float = 0.95
    verified: bool = True
    error_codes: list = field(default_factory=list)  # 提取到的错误码

class DocxExtractor:
    """从 docx 排障手册提取知识原子"""

    def __init__(self, docx_path: str):
        self.doc = docx.Document(docx_path)
        self.source_name = Path(docx_path).name
        self.atoms: list[KnowledgeAtomDraft] = []
        self.error_codes: dict[str, set] = {}   # {error_code: {atom_id...}}

    def extract(self) -> list[KnowledgeAtomDraft]:
        """主提取逻辑"""
        context = {
            'h1': '', 'h2': '', 'h3': '', 'h4': '',
            'current_section': 'unknown',
            'current_type': 'unknown',   # 判断方法 or 解决方案
            'body_lines': [],            # 当前节的正文
        }

        for para in self.doc.paragraphs:
            if not para.text.strip():
                continue

            style = para.style.name
            text = para.text.strip()

            if style == 'Heading 2':
                context['h2'] = text
                context['h3'] = ''
            elif style == 'Heading 3':
                # 保存前一节
                if context['h3'] and context['body_lines']:
                    self._flush_section(context)
                context['h3'] = text
                context['body_lines'] = []
            elif style == 'Heading 4':
                if context['body_lines']:
                    self._flush_section(context)
                context['h4'] = text
                context['body_lines'] = []
                # 判断节类型
                if '判断方法' in text:
                    context['current_type'] = 'diagnostic_step'
                elif '解决方案' in text or '恢复方案' in text:
                    context['current_type'] = 'fix_action'
                else:
                    context['current_type'] = 'background'
            elif style == 'Heading 5':
                if context['body_lines']:
                    self._flush_section(context)
                context['body_lines'] = []
                if '判断方法' in text:
                    context['current_type'] = 'diagnostic_step'
                elif '解决方案' in text or '恢复方案' in text:
                    context['current_type'] = 'fix_action'
                else:
                    context['current_type'] = 'background'
            else:  # Normal, _Style 15
                context['body_lines'].append(text)
                # 提取错误码
                for code in ERROR_CODE_PATTERN.findall(text):
                    if code not in self.error_codes:
                        self.error_codes[code] = set()

        # 最后一节
        if context['body_lines']:
            self._flush_section(context)

        return self.atoms

    def _flush_section(self, ctx: dict):
        """从当前 context 创建知识原子"""
        if not ctx['body_lines']:
            return

        content_text = '\n'.join(ctx['body_lines'])
        error_keyword = ctx['h3']  # 如"CPU不足"、"内存不足"

        # 构建触发条件
        trigger = {
            'stage': 'S2',
            'task_error_keywords': [error_keyword] if error_keyword else [],
        }

        # 构建知识内容
        content = {
            'description': f"[{ctx['h3']} > {ctx['h4']}] {content_text[:100]}",
            'full_text': content_text,
            'commands': self._extract_commands(content_text),
        }

        # 来源引用（面包屑路径）
        source_ref = ' > '.join(filter(None, [
            ctx['h1'] or self.source_name,
            ctx['h2'], ctx['h3'], ctx['h4']
        ]))

        atom = KnowledgeAtomDraft(
            type=ctx['current_type'],
            category_path=['虚拟机', '虚拟机开机失败', ctx['h3']],
            trigger=trigger,
            content=content,
            source_ref=source_ref,
            error_codes=ERROR_CODE_PATTERN.findall(content_text),
        )
        self.atoms.append(atom)

        # 记录错误码关联
        for code in atom.error_codes:
            if code not in self.error_codes:
                self.error_codes[code] = set()
            self.error_codes[code].add(atom.id)

    def _extract_commands(self, text: str) -> list[str]:
        """从文本中提取 acli 命令"""
        commands = []
        for line in text.split('\n'):
            stripped = line.strip()
            if stripped.startswith('acli ') or stripped.startswith('acli\t'):
                commands.append(stripped)
        return list(dict.fromkeys(commands))   # 去重保序
```

Step 3：实现写入模块

```python
# data-pipeline/atoms/atom_writer.py
"""将知识原子写入 KB Service API"""
import httpx
from .docx_extractor import KnowledgeAtomDraft

async def write_atoms_to_kb(
    atoms: list[KnowledgeAtomDraft],
    kb_url: str = "http://localhost:8004"
):
    async with httpx.AsyncClient() as client:
        success = 0
        for atom in atoms:
            resp = await client.post(
                f"{kb_url}/api/v1/atoms",
                json={
                    "id": atom.id,
                    "type": atom.type,
                    "category_id": atom.category_id,
                    "category_path": atom.category_path,
                    "knowledge_domain": atom.knowledge_domain,
                    "trigger": atom.trigger,
                    "content": atom.content,
                    "source_type": atom.source_type,
                    "source_ref": atom.source_ref,
                    "confidence": atom.confidence,
                    "verified": atom.verified,
                },
                timeout=30.0,
            )
            if resp.status_code == 201:
                success += 1
            else:
                print(f"写入失败 {atom.id}: {resp.text}")
        print(f"成功写入 {success}/{len(atoms)} 个知识原子")
```

Step 4：执行提取和写入

```bash
# 执行提取
uv run python scripts/dev/run_docx_extraction.py

# 验证写入结果
curl "http://localhost:8004/api/v1/atoms/search?query=CPU不足&category_id=虚拟机-003"
# 预期：返回包含 acli system top 命令的知识原子
```

Step 5：单元测试

在 tests/unit/test_docx_extractor.py 中：
  - 测试 _extract_commands 正确提取 acli 命令
  - 测试 _flush_section 正确区分 diagnostic_step/fix_action
  - 测试错误码提取（0x010032F5 格式）
  - 使用 fixture 测试文件（无需依赖真实 docx，可用小片段构造）

【约束】
- 提取器必须是幂等的（重复运行结果相同，通过 UPSERT 实现）
- acli 命令提取不能误将普通文本识别为命令
- 知识原子 ID 格式：ka-{hash12}（不用 UUID）

【验收标准】
- [ ] 处理 虚拟机开关机失败排障手册.docx 后，成功提取知识原子数量 >= 25
- [ ] 至少提取到 3 个错误码（写入 error_code_index）
- [ ] 每个 CPU不足/内存不足/序列号过期 对应知识原子都包含 acli 命令
- [ ] GET /api/v1/atoms/search?query=CPU不足 能返回含 acli system top 命令的原子
- [ ] uv run pytest tests/unit/test_docx_extractor.py -v 通过
- [ ] make lint 无新增错误
```

---