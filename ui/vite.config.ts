import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发时把 /api 代理到本机网页服务，可用环境变量覆盖目标地址
export default defineConfig({
  plugins: [vue()],
  build: { outDir: '../agent_dispatch/ui_dist', emptyOutDir: true },
  server: {
    proxy: { '/api': { target: process.env.AGENT_DISPATCH_PROXY || 'http://127.0.0.1:8765', changeOrigin: true } },
  },
})
