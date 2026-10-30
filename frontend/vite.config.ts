import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// dev: proxy /api -> backend uvicorn em :8000
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
