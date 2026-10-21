import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    watch: { ignored: ['**/src-tauri/**'] },
    proxy: {
      '/api': { target: 'http://127.0.0.1:8761', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8761', ws: true, changeOrigin: true },
    },
  },
})
