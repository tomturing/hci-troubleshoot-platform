---
status: active
category: task
audience: developer
last_updated: 2026-03-28
owner: team
related: 03
---

# Task 03：清理错误 SOP，修复 vm_power_failure 格式（P0）

```
你是一名负责 hci-troubleshoot-platform 数据管道的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
data-pipeline/sop_skills/ 目录下存在以下严重问题，需要立即修复：

▌问题1：5 个 SOP 文件使用了完全错误的命令（虚假的 hcicli 工具）
  - vm_no_power.md → 使用 hcicli（不存在），应使用 acli
  - network_unreachable.md → 同上
  - node_degraded.md → 同上，另使用了 journalctl/libvirtd（非 HCI 工具）
  - storage_offline.md → 同上
  - snapshot_failed.md → 同上
  以上文件是 AI 生成的幻觉内容，直接误导 AI 排障 Agent，必须移除出 KB 索引

▌问题2：vm_power_failure.md 和 vm_power_failure/chapters/*.md 内容来自
  真实 docx 文档, 但导入时产生 artifact（命令文本重复、换行异常），需要清理格式

真实权威来源是：虚拟机开关机失败排障手册.docx（位于 data-pipeline/sop_skills/）

【任务目标】
1. 从 index.json 中移除 5 个错误 SOP 的索引条目
2. 重命名这 5 个错误文件（加 .DEPRECATED 后缀），保留文件内容供审计
3. 清理 vm_power_failure.md 和 chapters/*.md 的格式问题（去重复、修换行）
4. 确认修复后的文件格式通过 ingestor.py 的加载检查

【涉及服务 / 文件范围】
允许修改：
  - data-pipeline/sop_skills/index.json
  - data-pipeline/sop_skills/vm_power_failure.md（格式修复）
  - data-pipeline/sop_skills/vm_power_failure/chapters/*.md（格式修复）
禁止改动（只读）：
  - data-pipeline/sop_skills/vm_no_power.md → 改名为 vm_no_power.md.DEPRECATED
  - 同理处理其他 4 个错误文件
禁止删除原始文件（仅改名）

【详细实现步骤】

Step 1：备份并隔离错误 SOP

```bash
cd data-pipeline/sop_skills

# 重命名（保留内容供后续审计，不删除）
mv vm_no_power.md vm_no_power.md.DEPRECATED
mv network_unreachable.md network_unreachable.md.DEPRECATED
mv node_degraded.md node_degraded.md.DEPRECATED
mv storage_offline.md storage_offline.md.DEPRECATED
mv snapshot_failed.md snapshot_failed.md.DEPRECATED

# 同理处理 network_failure/、node_failure/、storage_failure/、vm_boot_failure/ 目录下的 chapters
# 检查这些目录是否也有对应的错误文件
ls network_failure/chapters/ node_failure/chapters/ storage_failure/chapters/ vm_boot_failure/chapters/
```

Step 2：更新 index.json

从 index.json 中移除以下 5 个 skill 条目（根据 file 字段匹配）：
  - vm_no_power.md
  - storage_offline.md
  - network_unreachable.md
  - node_degraded.md
  - snapshot_failed.md

保留 vm_power_failure 相关条目。

Step 3：修复 vm_power_failure.md 格式问题

格式问题特征（运行以下命令确认）：
```bash
grep -n "acli.*acli" data-pipeline/sop_skills/vm_power_failure.md | head -20
# 会看到命令被重复写了两遍，如：
# "acli task get -v ${vmid}acli task get -v ${vmid}"
```

编写修复脚本（Python 最稳定）：
```python
#!/usr/bin/env python3
# scripts/dev/fix_sop_format.py
"""修复 SOP 文件中的 docx 导入 artifact：命令文本重复、多余换行"""
import re
import sys
from pathlib import Path

def fix_sop_file(path: Path) -> int:
    """返回修复的行数"""
    content = path.read_text(encoding='utf-8')
    original = content

    # 修复1：去除 acli 命令重复（"acli xxx\nacli xxx" → "acli xxx"）
    # 匹配模式：某段 acli 命令后紧跟同一命令
    lines = content.split('\n')
    fixed_lines = []
    prev_line = None
    for line in lines:
        stripped = line.strip()
        # 跳过与前一行完全相同的 acli 命令行（排除代码块注释行）
        if stripped and stripped == (prev_line or '').strip() and stripped.startswith('acli'):
            continue
        fixed_lines.append(line)
        prev_line = line

    content = '\n'.join(fixed_lines)

    # 修复2：去除行内重复（同一行出现两次相同的 acli 命令）
    # 如 "acli task get -v ${vmid}acli task get -v ${vmid}"
    def deduplicate_inline(m):
        half = len(m.group(0)) // 2
        first_half = m.group(0)[:half]
        if m.group(0) == first_half * 2:
            return first_half
        return m.group(0)

    # 匹配 30-200 字符的命令重复
    content = re.sub(r'(acli [^\n]{15,100})\1', r'\1', content)

    if content != original:
        path.write_text(content, encoding='utf-8')
        return content.count('\n') - original.count('\n')  # 减少的行数
    return 0

if __name__ == '__main__':
    sop_dir = Path('data-pipeline/sop_skills')
    files = [sop_dir / 'vm_power_failure.md'] + \
            list((sop_dir / 'vm_power_failure' / 'chapters').glob('*.md'))
    for f in files:
        n = fix_sop_file(f)
        print(f'修复 {f.name}: {n} 处改动')
```

运行：
```bash
uv run python scripts/dev/fix_sop_format.py
```

Step 4：验证修复效果

```bash
# 确认重复命令已清除
grep -c "acli.*acli" data-pipeline/sop_skills/vm_power_failure.md
# 应为 0 或非常少（仅代码块中的说明可能有）

# 确认 ingestor 可加载
uv run python -c "
from data_pipeline.ingestor import parse_sop
chunks = parse_sop('data-pipeline/sop_skills/vm_power_failure.md')
print(f'成功加载 {len(chunks)} 个 chunk')
"

# 验证关键章节结构完整（双轨架构要求 SOP 必须有以下结构才能用于知识原子）
uv run python -c "
from pathlib import Path
chapters = list(Path('data-pipeline/sop_skills/vm_power_failure/chapters').glob('*.md'))
print(f'章节数量: {len(chapters)}')
for ch in chapters[:5]:
    content = ch.read_text()
    has_condition = '判断方法' in content or '现象描述' in content
    has_command = 'acli' in content
    has_solution = '解决方案' in content or '临时' in content
    print(f'{ch.name}: 条件={has_condition}, 命令={has_command}, 方案={has_solution}')
"
# 预期：关键章节都有判断条件、acli 命令、解决方案（这是 Phase 1 知识原子提取的基础）
```

Step 5：整理 index.json，确认剩余有效条目

更新 index.json 后，确认只剩 vm_power_failure 相关条目，并给每个条目添加
`knowledge_type: "sop"` 字段，用于后续 Task 01 中 kb-service 返回时的 chunk_type 兼容。

【约束】
- 不删除原始错误文件（改名保留，便于后续审计和责任追溯）
- 不修改任何服务代码
- 修复脚本本身要有单元测试（针对重复命令格式）

【验收标准】
- [ ] index.json 中已不含 5 个错误 SOP 的条目
- [ ] index.json 中保留的 vm_power_failure 条目含 `knowledge_type: "sop"` 字段
- [ ] 5 个错误文件已改名为 *.DEPRECATED，git status 能看到改动
- [ ] vm_power_failure.md 中 `grep -c "acli.*acli"` 结果 < 5
- [ ] 修复后章节文件结构验证：每个 chapter 都含判断条件 + acli 命令 + 解决方案
- [ ] fix_sop_format.py 脚本有对应单测
- [ ] uv run pytest tests/unit/test_sop_format.py -q 通过
```

---

# 32_任务编排_P1_知识库重建

> **阶段**：Phase 1 — 知识库重建（P0 完成后开始）  
> **目标**：建立以「知识原子」为核心的新知识库架构，将 docx 权威手册转为可检索的结构化知识，为 AI Agent 提供精确诊断依据  
> **并行条件**：T04（DB设计）必须先于 T05/T06，T05/T06 可并行  
> **前置依赖**：Task 02（KB Service 部署正常）  
> **创建日期**：2026-03-22  
> **关联文档**：
> - [docs/architecture/知识库重建设计方案.md](../architecture/知识库重建设计方案.md)
> - [docs/architecture/完整技术方案.md](../architecture/完整技术方案.md) § 四、Phase 1

---