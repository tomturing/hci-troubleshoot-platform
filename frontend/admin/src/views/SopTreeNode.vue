<script setup lang="ts">
import { computed } from 'vue'
import { ElMessage } from 'element-plus'
import {
  CaretRight,
  Search,
  CircleCheck,
  Warning,
  Tools,
  CopyDocument
} from '@element-plus/icons-vue'

// ──────────────────────────────────────────────────────────────────────────────
// Props & Emits
// ──────────────────────────────────────────────────────────────────────────────
const props = defineProps<{
  node: {
    id: string
    title: string
    level: number
    line_number: number
    children?: any[]
    prerequisite_items?: {
      description: string
      type: 'filter' | 'priority'
      content_type?: 'text' | 'command'
      target_node_hint?: string
    }[]
    variables?: {
      name: string
      type: string
      source: string
      description: string | null
    }[]
    diagnosis?: {
      acli_methods: string[]
      page_methods?: string[] | null
      analysis_steps?: string[]
      possible_causes?: string[]
    } | null
    solution?: {
      quick_recovery?: string[]
      thorough_fix?: string[]
    } | null
  }
  expandedKeys: Set<string>
}>()

defineEmits<{
  (e: 'toggle-expand', nodeId: string): void
}>()

// ──────────────────────────────────────────────────────────────────────────────
// Computed 状态
// ──────────────────────────────────────────────────────────────────────────────
const isLeaf = computed(() => !props.node.children || props.node.children.length === 0)
const isExpanded = computed(() => props.expandedKeys.has(props.node.id))

// ──────────────────────────────────────────────────────────────────────────────
// 交互操作
// ──────────────────────────────────────────────────────────────────────────────
async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success({
      message: '命令已复制到剪贴板',
      duration: 1500,
      customClass: 'premium-message'
    })
  } catch {
    ElMessage.error('复制失败，请手动选取')
  }
}

function isCommandPrerequisite(item: { content_type?: string; description: string }) {
  return item.content_type === 'command'
}
</script>

<template>
  <div class="tree-node-wrapper">
    <!-- 节点核心卡片 -->
    <div 
      class="node-card" 
      :class="{ 
        'is-leaf-node': isLeaf, 
        'is-expanded-node': isExpanded && !isLeaf,
        'is-root-node': node.level === 1
      }"
    >
      <!-- 卡片头部标题行 -->
      <div class="card-header" @click="$emit('toggle-expand', node.id)">
        <div class="header-left">
          <!-- 展开/折叠箭头（仅非叶子节点） -->
          <span v-if="!isLeaf" class="expand-arrow-icon" :class="{ 'is-rotated': isExpanded }">
            <el-icon><CaretRight /></el-icon>
          </span>
          <span class="level-pill" :class="`level-h${node.level}`">H{{ node.level }}</span>
          <span class="node-title-text">{{ node.title }}</span>
          <span class="line-badge">行 {{ node.line_number }}</span>
        </div>
        <div class="header-right">
          <el-tag v-if="isLeaf" type="success" effect="light" size="small" class="node-type-tag">案例叶子节点</el-tag>
          <el-tag v-else-if="node.level === 1" type="primary" effect="dark" size="small" class="node-type-tag">场景根节点</el-tag>
          <el-tag v-else type="warning" effect="light" size="small" class="node-type-tag">路由决策节点</el-tag>
        </div>
      </div>

      <!-- 卡片主内容区 -->
      <div class="card-content-area">
        <!-- 1. 前置检查条件 -->
        <div v-if="node.prerequisite_items && node.prerequisite_items.length" class="node-section">
          <div class="section-label-row">
            <span class="section-indicator"></span>
            <span class="section-label">前置检查条件（Prerequisites）</span>
          </div>
          <div class="prereq-list">
            <div v-for="(item, idx) in node.prerequisite_items" :key="idx" class="prereq-item-row">
              <span 
                class="prereq-badge" 
                :class="isCommandPrerequisite(item) ? 'is-command' : item.type === 'filter' ? 'is-filter' : 'is-priority'"
              >
                {{ isCommandPrerequisite(item) ? '命令' : item.type === 'filter' ? '过滤' : '优先' }}
              </span>
              <span v-if="isCommandPrerequisite(item)" class="prereq-command">
                <code>{{ item.description }}</code>
                <el-button
                  size="small"
                  class="prereq-command-copy"
                  :icon="CopyDocument"
                  circle
                  @click.stop="copyText(item.description)"
                  title="复制命令"
                />
              </span>
              <span v-else class="prereq-description">{{ item.description }}</span>
              <span v-if="item.target_node_hint" class="prereq-node-hint">
                <span class="arrow">→</span> {{ item.target_node_hint }}
              </span>
            </div>
          </div>
        </div>

        <!-- 2. 变量声明表 -->
        <div v-if="node.variables && node.variables.length" class="node-section">
          <div class="section-label-row">
            <span class="section-indicator"></span>
            <span class="section-label">变量声明（Variables）</span>
          </div>
          <el-table :data="node.variables" border size="small" style="width: 100%" class="variables-sleek-table">
            <el-table-column prop="name" label="变量名" width="140">
              <template #default="{ row }">
                <code class="monospace-inline-var">{{ row.name }}</code>
              </template>
            </el-table-column>
            <el-table-column prop="type" label="类型" width="90" align="center">
              <template #default="{ row }">
                <el-tag type="info" size="small" effect="plain" class="type-pill">{{ row.type }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="source" label="数据来源" width="180">
              <template #default="{ row }">
                <code class="monospace-inline-src">{{ row.source }}</code>
              </template>
            </el-table-column>
            <el-table-column prop="description" label="变量描述">
              <template #default="{ row }">
                <span class="var-desc-text">{{ row.description || '—' }}</span>
              </template>
            </el-table-column>
          </el-table>
        </div>

        <!-- 3. 叶子场景专用：诊断详情与解决方案 -->
        <div v-if="isLeaf" class="leaf-row-details">
          <el-row :gutter="16">
            <!-- 诊断逻辑 -->
            <el-col :span="12">
              <div class="sub-detail-panel is-diagnosis">
                <div class="panel-header">
                  <el-icon class="panel-icon header-blue"><Search /></el-icon>
                  <span class="panel-title">诊断逻辑（Diagnosis）</span>
                </div>
                <div class="panel-body">
                  <!-- acli 命令列表 -->
                  <div v-if="node.diagnosis?.acli_methods && node.diagnosis.acli_methods.length" class="leaf-subsection">
                    <span class="subsection-title">推荐 acli 检查命令</span>
                    <div v-for="(cmd, cIdx) in node.diagnosis.acli_methods" :key="cIdx" class="terminal-cmd-box">
                      <div class="terminal-header">
                        <span class="terminal-dot red"></span>
                        <span class="terminal-dot yellow"></span>
                        <span class="terminal-dot green"></span>
                        <span class="terminal-title">SSH Terminal</span>
                        <el-button 
                          size="small" 
                          class="terminal-copy-action"
                          :icon="CopyDocument"
                          circle
                          @click="copyText(cmd)"
                          title="复制命令"
                        />
                      </div>
                      <pre class="terminal-pre"><code>{{ cmd }}</code></pre>
                    </div>
                  </div>

                  <!-- 页面检查方法 -->
                  <div v-if="node.diagnosis?.page_methods && node.diagnosis.page_methods.length" class="leaf-subsection">
                    <span class="subsection-title">页面检查步骤</span>
                    <ol class="sleek-numeric-list">
                      <li v-for="(step, sIdx) in node.diagnosis.page_methods" :key="sIdx">
                        <span class="step-num">{{ sIdx + 1 }}</span>
                        <span class="step-text">{{ step }}</span>
                      </li>
                    </ol>
                  </div>

                  <!-- 分析步骤 -->
                  <div v-if="node.diagnosis?.analysis_steps && node.diagnosis.analysis_steps.length" class="leaf-subsection">
                    <span class="subsection-title">分析步骤</span>
                    <ul class="sleek-bullet-list">
                      <li v-for="(step, aIdx) in node.diagnosis.analysis_steps" :key="aIdx">
                        <span class="bullet-dot"></span>
                        <span class="step-text">{{ step }}</span>
                      </li>
                    </ul>
                  </div>

                  <!-- 可能原因 -->
                  <div v-if="node.diagnosis?.possible_causes && node.diagnosis.possible_causes.length" class="leaf-subsection">
                    <span class="subsection-title is-warning">可能原因分析</span>
                    <ul class="sleek-bullet-list is-warning">
                      <li v-for="(cause, pIdx) in node.diagnosis.possible_causes" :key="pIdx">
                        <span class="bullet-dot warning"></span>
                        <span class="step-text">{{ cause }}</span>
                      </li>
                    </ul>
                  </div>
                </div>
              </div>
            </el-col>

            <!-- 解决方案 -->
            <el-col :span="12">
              <div class="sub-detail-panel is-solution">
                <div class="panel-header">
                  <el-icon class="panel-icon header-emerald"><CircleCheck /></el-icon>
                  <span class="panel-title">解决方案（Solutions）</span>
                </div>
                <div class="panel-body">
                  <!-- 快速恢复步骤 -->
                  <div v-if="node.solution?.quick_recovery && node.solution.quick_recovery.length" class="leaf-subsection">
                    <span class="subsection-title is-amber">
                      <el-icon class="inline-sec-icon text-amber"><Warning /></el-icon>
                      快速恢复步骤（临时规避方案）
                    </span>
                    <div class="solution-step-wrapper is-amber">
                      <ol class="solution-step-ol">
                        <li v-for="(step, qrIdx) in node.solution.quick_recovery" :key="qrIdx">
                          <span class="step-number-pill is-amber">{{ qrIdx + 1 }}</span>
                          <span class="step-content">{{ step }}</span>
                        </li>
                      </ol>
                    </div>
                  </div>

                  <!-- 彻底修复步骤 -->
                  <div v-if="node.solution?.thorough_fix && node.solution.thorough_fix.length" class="leaf-subsection">
                    <span class="subsection-title is-emerald">
                      <el-icon class="inline-sec-icon text-emerald"><Tools /></el-icon>
                      彻底修复步骤（根治技术方案）
                    </span>
                    <div class="solution-step-wrapper is-emerald">
                      <ol class="solution-step-ol">
                        <li v-for="(step, tfIdx) in node.solution.thorough_fix" :key="tfIdx">
                          <span class="step-number-pill is-emerald">{{ tfIdx + 1 }}</span>
                          <span class="step-content">{{ step }}</span>
                        </li>
                      </ol>
                    </div>
                  </div>
                </div>
              </div>
            </el-col>
          </el-row>
        </div>
      </div>
    </div>

    <!-- 递归子节点区域（如果展开） -->
    <div v-if="node.children && node.children.length && isExpanded" class="node-children">
      <SopTreeNode
        v-for="child in node.children"
        :key="child.id"
        :node="child"
        :expanded-keys="expandedKeys"
        @toggle-expand="$emit('toggle-expand', $event)"
      />
    </div>
  </div>
</template>

<style scoped>
.tree-node-wrapper {
  width: 100%;
  display: flex;
  flex-direction: column;
  position: relative;
}

/* 递归子节点容器：兄弟层级缩进，以虚线相连，解决容器宽度无限压缩的问题 */
.node-children {
  padding-left: 24px;
  border-left: 1.5px dashed #cbd5e1; /* 莫兰迪灰色虚线连接线 */
  margin-left: 18px;
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  position: relative;
}

/* 节点卡片外框：高雅微渐变、悬浮微动效 */
.node-card {
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.02);
  transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
  overflow: hidden;
  position: relative;
}

/* Hover 触发立体投影和微升动画，突显品质 */
.node-card:hover {
  transform: translateY(-2px);
  border-color: #cbd5e1;
  box-shadow: 0 4px 12px rgba(148, 163, 184, 0.12);
}

/* 卡片不同状态的侧边装饰条，纯 HSL 配色 */
.node-card::before {
  content: "";
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 4px;
  background: #64748b; /* 默认深灰蓝 */
  transition: background 0.3s;
}

.node-card.is-root-node::before {
  background: hsl(220, 90%, 56%); /* 根节点：皇家蓝 */
}

.node-card.is-expanded-node::before {
  background: hsl(200, 95%, 48%); /* 展开路由：亮蓝 */
}

.node-card.is-leaf-node::before {
  background: hsl(142, 70%, 45%); /* 叶子案例：翡翠绿 */
}

/* 卡片头部标题栏 */
.card-header {
  padding: 12px 16px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: linear-gradient(180deg, #f8fafc 0%, #f1f5f9 100%);
  border-bottom: 1px solid #e2e8f0;
  cursor: pointer;
  user-select: none;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 1;
  min-width: 0;
}

.expand-arrow-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: #64748b;
  font-size: 15px;
  transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1);
  width: 20px;
  height: 20px;
}

.expand-arrow-icon.is-rotated {
  transform: rotate(90deg);
}

.level-pill {
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: 11px;
  font-weight: 700;
  padding: 2px 6px;
  border-radius: 4px;
  line-height: 1;
}

.level-h1 { color: hsl(220, 90%, 56%); background: hsl(220, 90%, 95%); }
.level-h2 { color: hsl(200, 95%, 40%); background: hsl(200, 95%, 95%); }
.level-h3 { color: hsl(38, 92%, 40%); background: hsl(38, 92%, 95%); }
.level-h4, .level-h5, .level-h6 { color: #64748b; background: #f1f5f9; }

.node-title-text {
  font-size: 14px;
  font-weight: 600;
  color: #1e293b;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.line-badge {
  font-size: 11px;
  color: #94a3b8;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  padding: 1px 5px;
  border-radius: 3px;
  font-family: monospace;
}

.node-type-tag {
  font-weight: 600;
  border-radius: 4px;
}

/* 卡片内容区 */
.card-content-area {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* 分块标签行 */
.node-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.section-label-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.section-indicator {
  width: 3px;
  height: 12px;
  background: hsl(220, 90%, 56%);
  border-radius: 1px;
}

.section-label {
  font-size: 12px;
  font-weight: 700;
  color: #64748b;
  letter-spacing: 0.5px;
}

/* 前置检查条件排版 */
.prereq-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: #f8fafc;
  padding: 8px 12px;
  border-radius: 6px;
  border: 1px solid #f1f5f9;
}

.prereq-item-row {
  display: flex;
  align-items: center;
  font-size: 13px;
  line-height: 1.5;
  color: #334155;
  gap: 8px;
}

.prereq-badge {
  font-size: 10px;
  font-weight: 700;
  padding: 1px 5px;
  border-radius: 3px;
  line-height: 1.3;
}

.prereq-badge.is-filter {
  color: hsl(220, 90%, 45%);
  background: hsl(220, 90%, 95%);
}

.prereq-badge.is-priority {
  color: hsl(38, 92%, 40%);
  background: hsl(38, 92%, 95%);
}

.prereq-badge.is-command {
  color: #047857;
  background: #ecfdf5;
}

.prereq-description {
  font-weight: 500;
}

.prereq-command {
  min-width: 0;
  max-width: 100%;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px;
  background: #0f172a;
  border: 1px solid #1e293b;
  border-radius: 5px;
  box-shadow: inset 0 1px 2px rgba(0,0,0,0.25);
}

.prereq-command code {
  min-width: 0;
  color: #d1fae5;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, Monaco, monospace;
  font-size: 12px;
  line-height: 1.4;
  white-space: pre;
  overflow-x: auto;
}

.prereq-command-copy {
  flex: 0 0 auto;
  background: transparent !important;
  border: none !important;
  color: #94a3b8 !important;
  height: 20px !important;
  width: 20px !important;
}

.prereq-command-copy:hover {
  color: #34d399 !important;
}

.prereq-node-hint {
  font-size: 12px;
  color: #64748b;
  font-family: monospace;
  background: #f1f5f9;
  padding: 0 4px;
  border-radius: 3px;
}

.prereq-node-hint .arrow {
  color: hsl(220, 90%, 56%);
  font-weight: bold;
}

/* 变量声明表格 */
.variables-sleek-table {
  border-radius: 6px;
  overflow: hidden;
  box-shadow: 0 1px 2px rgba(0,0,0,0.01);
}

.monospace-inline-var {
  font-family: 'SFMono-Regular', Consolas, Menlo, monospace;
  font-size: 12px;
  color: hsl(220, 90%, 50%);
  background: hsl(220, 90%, 97%);
  padding: 2px 6px;
  border-radius: 4px;
  border: 1px solid hsl(220, 90%, 93%);
  font-weight: 600;
}

.monospace-inline-src {
  font-family: 'SFMono-Regular', Consolas, Menlo, monospace;
  font-size: 12px;
  color: #475569;
  background: #f1f5f9;
  padding: 2px 6px;
  border-radius: 4px;
  border: 1px solid #e2e8f0;
}

.type-pill {
  font-weight: 600;
}

.var-desc-text {
  font-size: 12px;
  color: #475569;
}

/* 叶子节点详细布局 */
.leaf-row-details {
  margin-top: 4px;
}

.sub-detail-panel {
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  box-shadow: 0 1px 2px rgba(0,0,0,0.02);
  display: flex;
  flex-direction: column;
  height: 100%;
}

.sub-detail-panel.is-diagnosis {
  border-top: 3px solid hsl(200, 95%, 48%);
}

.sub-detail-panel.is-solution {
  border-top: 3px solid hsl(142, 70%, 45%);
}

.panel-header {
  padding: 10px 14px;
  display: flex;
  align-items: center;
  gap: 8px;
  border-bottom: 1px solid #e2e8f0;
  background: #f8fafc;
}

.panel-icon {
  font-size: 16px;
}

.header-blue { color: hsl(200, 95%, 48%); }
.header-emerald { color: hsl(142, 70%, 45%); }

.panel-title {
  font-size: 13px;
  font-weight: 700;
  color: #334155;
  letter-spacing: 0.2px;
}

.panel-body {
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.leaf-subsection {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.subsection-title {
  font-size: 12px;
  font-weight: 700;
  color: #475569;
  border-left: 2.5px solid #94a3b8;
  padding-left: 6px;
  line-height: 1.2;
}

.subsection-title.is-warning {
  color: #b45309;
  border-left-color: hsl(38, 92%, 50%);
}

.subsection-title.is-amber {
  color: #b45309;
  border-left-color: hsl(38, 92%, 50%);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.subsection-title.is-emerald {
  color: #15803d;
  border-left-color: hsl(142, 70%, 45%);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.inline-sec-icon {
  font-size: 14px;
}

.text-amber { color: hsl(38, 92%, 50%); }
.text-emerald { color: hsl(142, 70%, 45%); }

/* 高科技 Terminal 命令行组件 */
.terminal-cmd-box {
  background: #0f172a; /* Slate 900 黑色背景 */
  border-radius: 6px;
  overflow: hidden;
  position: relative;
  border: 1px solid #1e293b;
  box-shadow: inset 0 2px 4px rgba(0,0,0,0.3);
}

.terminal-header {
  height: 28px;
  background: #1e293b;
  display: flex;
  align-items: center;
  padding: 0 10px;
  gap: 6px;
  position: relative;
}

.terminal-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.terminal-dot.red { background: #ef4444; }
.terminal-dot.yellow { background: #f59e0b; }
.terminal-dot.green { background: #10b981; }

.terminal-title {
  color: #94a3b8;
  font-size: 10px;
  font-family: monospace;
  margin-left: 4px;
  user-select: none;
}

.terminal-copy-action {
  position: absolute;
  right: 6px;
  top: 3px;
  background: transparent !important;
  border: none !important;
  color: #94a3b8 !important;
  opacity: 0.7;
  transition: all 0.2s;
  height: 22px !important;
  width: 22px !important;
}

.terminal-copy-action:hover {
  opacity: 1;
  color: #10b981 !important; /* 悬停显绿 */
  transform: scale(1.1);
}

.terminal-pre {
  margin: 0;
  padding: 10px 14px;
  overflow-x: auto;
}

.terminal-pre code {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, Monaco, monospace;
  font-size: 12px;
  color: #10b981; /* 极客亮绿 */
  line-height: 1.5;
  white-space: pre-wrap;
}

/* 列表渲染系统 */
.sleek-numeric-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.sleek-numeric-list li {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.step-num {
  font-family: monospace;
  font-weight: 700;
  font-size: 11px;
  background: #e2e8f0;
  color: #475569;
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  flex-shrink: 0;
  margin-top: 1px;
}

.step-text {
  font-size: 12.5px;
  line-height: 1.5;
  color: #334155;
}

.sleek-bullet-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.sleek-bullet-list li {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.bullet-dot {
  width: 6px;
  height: 6px;
  background: #94a3b8;
  border-radius: 50%;
  flex-shrink: 0;
  margin-top: 6px;
}

.bullet-dot.warning {
  background: hsl(38, 92%, 50%);
}

.sleek-bullet-list.is-warning .step-text {
  color: #78350f;
  font-weight: 500;
}

/* 解决方案步骤渲染 */
.solution-step-wrapper {
  border-radius: 6px;
  padding: 10px 12px;
  border: 1px solid transparent;
}

.solution-step-wrapper.is-amber {
  background: hsl(38, 92%, 97%);
  border-color: hsl(38, 92%, 93%);
}

.solution-step-wrapper.is-emerald {
  background: hsl(142, 70%, 97%);
  border-color: hsl(142, 70%, 93%);
}

.solution-step-ol {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.solution-step-ol li {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.step-number-pill {
  font-family: monospace;
  font-weight: 700;
  font-size: 11px;
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  flex-shrink: 0;
  margin-top: 1.5px;
}

.step-number-pill.is-amber {
  background: hsl(38, 92%, 90%);
  color: #b45309;
}

.step-number-pill.is-emerald {
  background: hsl(142, 70%, 90%);
  color: #15803d;
}

.step-content {
  font-size: 12.5px;
  line-height: 1.5;
  color: #334155;
  font-weight: 500;
}
</style>
