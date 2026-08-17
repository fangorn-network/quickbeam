import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The manual is fully static: the baked Semantic-CDN snapshot is staged into
// public/cdn (scripts/stage-cdn.mjs) and served same-origin at /cdn. A dev /cdn
// proxy to `quickbeam cdn serve` is available if you'd rather not stage.
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    proxy: {
      '/cdn-live': {
        target: 'http://localhost:8090',
        changeOrigin: true,
        rewrite: (p: string) => p.replace(/^\/cdn-live/, ''),
      },
    },
  },
});
