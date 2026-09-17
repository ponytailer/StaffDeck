import path from 'path';
import tailwindcss from '@tailwindcss/vite';
import { loadEnv, searchForWorkspaceRoot } from 'vite';
import react from '@vitejs/plugin-react';
import svgr from 'vite-plugin-svgr';
import { defineConfig } from 'vitest/config';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  return {
    plugins: [react(), tailwindcss(), svgr()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    base: '/',
    test: {
      setupFiles: ['./src/test/setup.ts'],
      environmentOptions: {
        jsdom: {
          url: 'http://localhost/',
        },
      },
    },
    server: {
      port: 5173,
      // 「平台更新内容」页以 `?raw` 引入仓库根目录的 changelog.md，
      // 它在 frontend-enterprise 之外，dev server 需要显式放行上一级目录。
      fs: {
        allow: [searchForWorkspaceRoot(process.cwd()), '..'],
      },
      proxy: {
        '/api': {
          target: env.VITE_PROXY_TARGET || 'http://localhost:8000',
          changeOrigin: true,
        },
      },
    },
  };
});
