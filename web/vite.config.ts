import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// 开发代理：/api 与 /ws 转发到本地 FastAPI 服务（默认 127.0.0.1:8080）
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8080', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8080', ws: true },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    css: false,
  },
})
