import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// A same-origin /api proxy lets localhost and LAN-hosted Vite pages reach the
// loopback-only FastAPI development server without browser CORS or host issues.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    // Keep the documented URL deterministic. Vite otherwise silently chooses
    // 5174 when 5173 is already occupied, which can leave a stale server on 5173.
    strictPort: true,
    proxy: {
      '/api': {
        target: process.env.QA_API_PROXY_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
});
