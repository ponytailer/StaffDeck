// OA 面板独立打包：src/content/oa-panel-entry.ts → dist/oa-panel.js（IIFE）。
//
// 主 content script 通过 import(runtime.getURL('oa-panel.js')) 懒加载它
// （需 manifest web_accessible_resources）。不能并进主包：panel.js 是立即执行的
// IIFE，静态引入会在所有页面自动弹面板。
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
      entry: resolve(root, 'src/content/oa-panel-entry.ts'),
      formats: ['iife'],
      name: 'SDOAPanelEntry',
      fileName: () => 'oa-panel.js',
    },
  },
});

console.log('[build-oa-panel] dist/oa-panel.js done');
