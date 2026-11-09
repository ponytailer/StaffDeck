// 将 manifest 等静态资源复制进 dist（lib mode 不会处理 publicDir）
import { copyFileSync, mkdirSync, readdirSync, cpSync } from 'node:fs';
import { join } from 'node:path';

const root = new URL('..', import.meta.url).pathname;
const dist = join(root, 'dist');
mkdirSync(dist, { recursive: true });

// manifest
copyFileSync(join(root, 'manifest.json'), join(dist, 'manifest.json'));

// icons（存在才复制）
try {
  cpSync(join(root, 'icons'), join(dist, 'icons'), { recursive: true });
} catch {
  // icons 目录不存在则跳过（开发阶段允许无图标）
}

console.log('[copy-assets] done:', readdirSync(dist).join(', '));
