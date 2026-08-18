import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  return {
    // base 必须与 Ingress 路径一致。admin-ui 挂在 /admin 下，assets 引用需带前缀
    // 否则 /assets/... 会被 Traefik 路由到 customer-ui（PIT-023）
    base: '/admin/',
    plugins: [vue()],
    resolve: {
      alias: {
        '@': resolve(__dirname, 'src'),
        '@hci/shared': resolve(__dirname, '../shared/src/index.ts'),
      },
    },
    server: {
      port: 3002,
      proxy: {
        '/api': {
          // 本地并行 worktree 可覆盖代理目标，避免占用既有 Gateway 端口。
          target: env.VITE_DEV_API_TARGET || 'http://localhost:8000',
          changeOrigin: true,
        },
      },
    },
  }
})
