import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The git-native service (commit/push/log/show/clone) runs on :8791. We proxy it
// under /api so the browser makes same-origin requests and there's one URL to set.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5273,
    proxy: {
      // The git-native / publish service.
      '/api': {
        target: process.env.VITE_SERVER_URL ?? 'http://localhost:8791',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
      // Semantic CDN (`quickbeam cdn serve`) — the served shards search reads.
      '/cdn': {
        target: process.env.VITE_CDN_URL ?? 'http://localhost:8090',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/cdn/, ''),
      },
    },
  },
});
