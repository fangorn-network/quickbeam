import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Dev proxy so a single host/tunnel serves both the app and its backends
// same-origin (no CORS, no second tunnel, no mixed-content):
//   /qdrant/* -> http://localhost:6333/*      (VITE_DATA_SOURCE=qdrant)
//   /cdn/*    -> http://localhost:8090/*       (VITE_DATA_SOURCE=shards, VITE_CDN_URL=/cdn)
const ALLOWED_HOSTS = ['untrainable-milton-gawky.ngrok-free.dev'];

const proxy = {
  '/qdrant': {
    target: 'http://localhost:6333',
    changeOrigin: true,
    rewrite: (path: string) => path.replace(/^\/qdrant/, ''),
  },
  '/cdn': {
    target: 'http://localhost:8090',
    changeOrigin: true,
    rewrite: (path: string) => path.replace(/^\/cdn/, ''),
  },
};

export default defineConfig({
  plugins: [react()],
  // The ML worker (src/lib/ml.worker.ts) dynamically imports transformers.js, so
  // its bundle must be ES (the default 'iife' can't code-split).
  worker: { format: 'es' },
  server: { allowedHosts: ALLOWED_HOSTS, proxy },
  preview: { allowedHosts: ALLOWED_HOSTS, proxy },
});
