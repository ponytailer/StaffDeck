import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

const here = fileURLToPath(new URL('.', import.meta.url));

/**
 * Chrome / Edge（Chromium）专用构建，产物输出到 dist/。
 *
 * Content script 必须打成单个 IIFE 文件（MV3 content script 不支持 ES module），
 * 因此用 lib mode 单入口；manifest 等静态资源由 scripts/copy-assets.mjs 复制进 dist。
 * Firefox 版是隔壁 firefox/ 目录里的一份独立代码，改这里不会影响它。
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
        // 单文件内联所有内容，避免动态 import 产生第二个 chunk
        inlineDynamicImports: true,
      },
    },
    target: 'chrome120',
    minify: true,
    cssCodeSplit: false,
  },
  define: {
    'process.env.NODE_ENV': JSON.stringify('production'),
  },
});
