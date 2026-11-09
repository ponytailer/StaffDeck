// Background service worker 独立打包：src/background/main.ts → dist/background.js（IIFE）。
// MV3 SW 用经典脚本即可（不需要 module type），与 oa-panel 同款 vite API 二次构建。
import { resolve } from 'node:path';
import { build } from 'vite';

const root = new URL('..', import.meta.url).pathname;

await build({
  configFile: false,
  logLevel: 'warn',
  build: {
    outDir: resolve(root, 'dist'),
    emptyOutDir: false,
    lib: {
      entry: resolve(root, 'src/background/main.ts'),
      formats: ['iife'],
      name: 'SDBackground',
      fileName: () => 'background.js',
    },
  },
});

console.log('[build-background] dist/background.js done');
