import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

const here = fileURLToPath(new URL('.', import.meta.url));

/**
 * Firefox 专用构建，产物输出到 dist/。
 *
 * 与 Chrome 版的差别只有两处，但足以决定能不能装上：
 *  - 后台是 event page（manifest 里 background.scripts），不是 service worker；
 *  - 语法目标是 firefox115（当前 ESR 基线）。
 * 其余业务代码与 chrome/ 目录各自独立，改一边不会串到另一边。
 */
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    lib: {
      entry: `${here}src/content/main.tsx`,
      name: 'StaffDeckContent',
      formats: ['iife'],
      fileName: () => 'content.js',
    },
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
      },
    },
    target: 'firefox115',
    minify: true,
    cssCodeSplit: false,
  },
  define: {
    'process.env.NODE_ENV': JSON.stringify('production'),
  },
});
