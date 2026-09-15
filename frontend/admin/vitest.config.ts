import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'happy-dom',
    globals: true,
    // CI 2 核 runner 上与镜像构建等 job 并行抢占资源时，完整挂载 ElementPlus 的
    // 重组件用例可达 6s+，超过 vitest 默认 5s 阈值产生临界抖动超时（V-016）。
    testTimeout: 20000,
    hookTimeout: 20000,
  },
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
      '@hci/shared': resolve(__dirname, '../shared/src/index.ts'),
    },
  },
})
