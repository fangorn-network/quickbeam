import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Dev proxy: app calls /qdrant/* -> http://localhost:6333/* (avoids CORS).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/qdrant': {
        target: 'http://localhost:6333',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/qdrant/, ''),
      },
    },
  },
});
