// Background 独立打包：src/background/main.ts → dist/background.js（IIFE）。
//
// Firefox 的 MV3 后台是 event page（manifest 里 background.scripts），同样是经典脚本。
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
    target: 'firefox115',
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
