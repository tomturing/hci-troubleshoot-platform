import { describe, expect, it } from 'vitest'

import { parseSkillMarkdown } from '../skillImport'

const FULL_SKILL_MD = `---
name: hci-disk-vendor-lifetime
description: 识别磁盘厂商并判断寿命是否达到返修阈值。当用户报告磁盘 IO 异常、存储池降级时触发。
display_name: 硬盘厂商识别与寿命判定
compatibility: 适用于 HCI v2.x 环境，需要能执行 smartctl 命令
license: Proprietary
allowed-tools: Bash(ssh:*) ReadFile
metadata:
  author: hci-team
  category: storage
  tags: [disk, smart, lifetime]
---

## 技能描述

识别磁盘厂商并判定寿命。

### Step 1：采集 SMART 数据

执行 smartctl 采集。
`

describe('parseSkillMarkdown', () => {
  it('完整 SKILL.md：标准字段与 metadata 全部映射', () => {
    const parsed = parseSkillMarkdown(FULL_SKILL_MD)
    expect(parsed.skill_name).toBe('hci-disk-vendor-lifetime')
    expect(parsed.display_name).toBe('硬盘厂商识别与寿命判定')
    expect(parsed.description).toContain('存储池降级')
    expect(parsed.compatibility).toBe('适用于 HCI v2.x 环境，需要能执行 smartctl 命令')
    expect(parsed.license).toBe('Proprietary')
    expect(parsed.allowed_tools).toBe('Bash(ssh:*) ReadFile')
    expect(parsed.meta_author).toBe('hci-team')
    expect(parsed.meta_category).toBe('storage')
    expect(parsed.meta_tags).toBe('disk, smart, lifetime')
    expect(parsed.instructions_md).toContain('### Step 1：采集 SMART 数据')
    expect(parsed.instructions_md).not.toContain('---')
  })

  it('仅必填字段：可选项回退为空串，正文保留', () => {
    const parsed = parseSkillMarkdown('---\nname: demo-skill\ndescription: 演示技能\n---\n正文内容\n')
    expect(parsed.skill_name).toBe('demo-skill')
    expect(parsed.description).toBe('演示技能')
    expect(parsed.instructions_md).toBe('正文内容\n')
    expect(parsed.compatibility).toBe('')
    expect(parsed.license).toBe('')
    expect(parsed.allowed_tools).toBe('')
    expect(parsed.meta_author).toBe('')
    expect(parsed.meta_tags).toBe('')
  })

  it('引号标量、行尾注释与 CRLF/BOM 归一化', () => {
    const text = '\uFEFF---\r\nname: "quoted-skill" # 注释\r\ndescription: \'含 # 号的描述\'\r\n---\r\n\r\n正文\r\n'
    const parsed = parseSkillMarkdown(text)
    expect(parsed.skill_name).toBe('quoted-skill')
    expect(parsed.description).toBe('含 # 号的描述')
    expect(parsed.instructions_md).toBe('正文\n')
  })

  it('description 块标量（> 折叠 / | 保留换行）', () => {
    const folded = parseSkillMarkdown('---\nname: a-skill\ndescription: >\n  第一行\n  第二行\n---\n正文\n')
    expect(folded.description).toBe('第一行 第二行')
    const literal = parseSkillMarkdown('---\nname: a-skill\ndescription: |\n  第一行\n  第二行\n---\n正文\n')
    expect(literal.description).toBe('第一行\n第二行')
  })

  it('allowed-tools 内联列表归一为空格分隔字符串', () => {
    const parsed = parseSkillMarkdown('---\nname: a-skill\ndescription: 描述\nallowed-tools: [Bash, ReadFile]\n---\n')
    expect(parsed.allowed_tools).toBe('Bash ReadFile')
  })

  it('metadata.tags 逗号字符串原样保留', () => {
    const parsed = parseSkillMarkdown('---\nname: a-skill\ndescription: 描述\nmetadata:\n  tags: disk, smart\n---\n')
    expect(parsed.meta_tags).toBe('disk, smart')
  })

  it('缺少 frontmatter 时抛出可读错误', () => {
    expect(() => parseSkillMarkdown('# 只有正文\n没有元数据')).toThrowError(/frontmatter/)
  })

  it('缺少 name / description 必填字段时分别报错', () => {
    expect(() => parseSkillMarkdown('---\ndescription: 描述\n---\n')).toThrowError(/name/)
    expect(() => parseSkillMarkdown('---\nname: a-skill\n---\n')).toThrowError(/description/)
  })

  it('空输入报错而非静默返回', () => {
    expect(() => parseSkillMarkdown('')).toThrowError()
  })
})