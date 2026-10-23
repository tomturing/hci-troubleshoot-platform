<script setup lang="ts">
/**
 * 日志收集引导卡片（建单后一次性出现）
 *
 * 背景：日志收集入口原先只在 header 里有一个 12px 按钮，且依赖活跃工单，
 * 客户建单后很难发现，反馈里表现为"customer-ui 没有收集日志的入口"。
 *
 * 取舍：
 * - 卡片只在当前工单未 dismiss 时出现（状态在 chat store，按 case_id 去重）；
 * - 「暂不需要」只关闭引导，header 入口仍保留，避免用户从此失去入口；
 * - 不在卡片内直接发起采集：acli 采集需要已建立的 SSH 会话，先打开面板再由
 *   面板做 probe，失败时能给出"请先连接 HCI"的明确提示。
 */
import { FolderOpened } from '@element-plus/icons-vue'
import { useChatStore } from '@/stores/chat'

const chatStore = useChatStore()
</script>

<template>
  <div v-if="chatStore.showLogCollectGuide" class="log-collect-guide">
    <!--
      不用 emoji：🗂（U+1F5C2）默认是文本呈现，Windows 会回退到 Segoe UI Symbol 的单色字形，
      16~22px 下糊成一个黑块（Edge 实测）。SVG 图标不依赖系统 emoji 字体，跨平台一致。
    -->
    <el-icon class="guide-icon"><FolderOpened /></el-icon>
    <div class="guide-body">
      <p class="guide-title">需要采集 HCI 侧日志？</p>
      <p class="guide-desc">
        在 SSH 终端连接 HCI 后，可一键收集虚拟机、存储、网络等故障模块的日志包，平台会记录进度并自动归档。
      </p>
    </div>
    <div class="guide-actions">
      <el-button type="primary" size="small" @click="chatStore.openLogCollect()">开始收集</el-button>
      <el-button size="small" text @click="chatStore.dismissLogCollectGuide()">暂不需要</el-button>
    </div>
  </div>
</template>

<style scoped>
.log-collect-guide {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin: 12px 16px 0;
  padding: 12px 16px;
  background: linear-gradient(135deg, #f0f7ff, #e8f3ff);
  border: 1px solid #c6e2ff;
  border-radius: 8px;
}

.guide-icon {
  flex: none;
  font-size: 22px;
  line-height: 1.2;
  color: var(--el-color-primary);
}

.guide-body {
  flex: 1;
  min-width: 0;
}

.guide-title {
  margin: 0 0 4px;
  font-size: 14px;
  font-weight: 600;
  color: #337ecc;
}

.guide-desc {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: #606266;
}

.guide-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}
</style>
