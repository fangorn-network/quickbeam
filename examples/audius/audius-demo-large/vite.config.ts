import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Where `quickbeam cdn serve` is actually listening, from the dev server's point of
// view. Only this process needs to reach it; the browser never does.
const CDN_TARGET = process.env.CDN_TARGET ?? 'http://localhost:8092';
// Where `quickbeam serve` (the private-retrieval API) is listening. Proxied for the
// same reasons as the CDN: naming it absolutely works on this machine and nowhere
// else, and an http:// call from an https:// tunnel is blocked as mixed content.
const API_TARGET = process.env.API_TARGET ?? 'http://localhost:8081';

export default defineConfig({
  plugins: [react()],
  // transformers.js ships WASM/ONNX assets; excluding it from dep pre-bundling keeps
  // the worker's dynamic import() resolving to the real ESM build.
  optimizeDeps: { exclude: ['@huggingface/transformers'] },
  worker: { format: 'es' },
  server: {
    // 5181, not 5180 — so this can run alongside audius-demo for comparison.
    port: 5181,
    allowedHosts: true,
    // Serve the snapshot through the app's OWN origin at /cdn.
    //
    // Without this the client has to name the CDN absolutely (http://localhost:8090),
    // which works on this machine and nowhere else: through a tunnel `localhost` is
    // the VISITOR's machine, and an http:// fetch from an https:// page would be
    // blocked as mixed content regardless. Proxying means one tunnel covers the whole
    // demo, with no CORS and no mixed content.
    proxy: {
      '/cdn': {
        target: CDN_TARGET,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/cdn/, ''),
      },
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
});
