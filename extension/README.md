# AIStaff 浏览器助手（Chrome 插件）

把 AIStaff 数字员工装进每一个网页：注入式悬浮球 + 面板（登录 → 员工列表 → 流式聊天）。

正式方案见 `../docs/chrome-extension-plan.md`；视觉基准 Demo 见 `../docs/chrome-extension-demo.html`。

## 开发

```bash
cd extension
npm install
npm run build     # 产出 dist/（content.js + manifest.json）
npm run dev       # watch 模式，改动后重新 build
npm run typecheck # tsc --noEmit
```

## 安装到 Chrome（开发者模式）

1. 打开 `chrome://extensions`，右上角开启「开发者模式」；
2. 点「加载已解压的扩展程序」，选择 `extension/dist` 目录；
3. 默认连接生产后端 `https://ai-staff.folidaymall.com`（`host_permissions` 已声明；本地开发可在登录视图「服务器与租户」中改回 `http://localhost:8000`）；
4. 任意网页右下角出现青绿悬浮球 → 点击展开 → 登录（默认服务地址 `http://localhost:8000`，可在登录视图「服务器与租户」中修改）。

## 架构速记

- 单文件 content script（IIFE，React 18 打包在内），Shadow DOM 隔离样式；
- 无 background service worker：token 存 `chrome.storage.local`，SSE 由 content script 直连后端（MV3 background 30s 空闲会被杀，故不挂网络请求）；
- API 契约与 `frontend-enterprise/src/api/client.ts` 一致：`POST /api/auth/login`、`GET /api/chat/agents`、`POST /api/chat/stream`（SSE 事件：`session_created` / `stream_delta` / `token` / `stream_replace` / 终态事件集）、`POST /api/chat/sessions/{id}/cancel`；
- 免费配额错误码 `free_model_quota_exhausted` 由后端返回，插件展示配额提示；
- 会话与平台 Web 共享（同一 `ChatSession`），`channel: "extension"` 用于区分来源；对外品牌名为 AIStaff。

## 上线前 TODO

- [x] 生产域名 `https://ai-staff.folidaymall.com/*` 已加入 `host_permissions`，并作为默认登录地址；
- [ ] 补插件图标（icons/16/48/128，当前未声明走默认图标）；
- [ ] 「当前网站隐藏悬浮球」兜底入口；
- [ ] 划词发送（二期）。
