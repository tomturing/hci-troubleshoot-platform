import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'happy-dom',
    globals: true,
    // 与 admin 对齐：低配 CI runner 资源抢占下重挂载用例可能超过默认 5s 阈值（V-016）。
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
