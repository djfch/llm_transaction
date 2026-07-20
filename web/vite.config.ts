import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// 前端 dev server 端口 17576；开发代理：/api 与 /ws 转发到本地 FastAPI 服务（127.0.0.1:17577）
export default defineConfig({
  plugins: [react()],
  server: {
    port: 17576,
    proxy: {
      '/api': { target: 'http://127.0.0.1:17577', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:17577', ws: true },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    css: false,
  },
})
