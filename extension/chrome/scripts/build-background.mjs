// Background 独立打包：src/background/main.ts → dist/background.js（IIFE）。
//
// Chromium 的 MV3 后台是 service worker，经典脚本即可（不需要 module type）。
// 用 vite API 二次构建，避免为每个产物再开一份配置文件。
import { resolve } from 'node:path';
import { build } from 'vite';

const root = new URL('..', import.meta.url).pathname;

await build({
  configFile: false,
  logLevel: 'warn',
  build: {
    outDir: resolve(root, 'dist'),
    emptyOutDir: false,
    target: 'chrome120',
    minify: true,
    lib: {
      entry: resolve(root, 'src/background/main.ts'),
      formats: ['iife'],
      name: 'SDBackground',
      fileName: () => 'background.js',
    },
  },
});

console.log('[build-background] dist/background.js done');
