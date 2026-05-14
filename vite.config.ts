import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  base: './',
  plugins: [react()],
  server: {
    proxy: { '/api': process.env.CYD_API_URL || 'http://127.0.0.1:8000' },
  },
  preview: {
    proxy: { '/api': process.env.CYD_API_URL || 'http://127.0.0.1:8000' },
  },
})
