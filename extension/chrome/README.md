# AIStaff 浏览器助手 · Chrome / Edge 版

把 AIStaff 数字员工装进每一个网页：注入式悬浮球 + 面板（登录 → 员工列表 → 流式聊天）。

> 本目录是 **Chromium 专用**的一份独立代码，与 `../firefox/` 互不影响。
> 改这里的任何文件都不会影响 Firefox 版，反之亦然。

## 开发

```bash
cd extension/chrome
npm install
npm run build     # 产出 dist/（content.js + background.js + oa-panel.js + manifest.json）
npm run dev       # watch 模式，改动后重新 build
npm run typecheck # tsc --noEmit
npm run package   # 把 dist/ 里 4 个文件打成 aistaff-extension.zip
```

## 安装到 Chrome（开发者模式）

1. 打开 `chrome://extensions`，右上角开启「开发者模式」；
2. 点「加载已解压的扩展程序」，选择 `extension/chrome/dist` 目录（或解压发布出去的 zip）；
3. 默认连接生产后端 `https://ai-staff.folidaymall.com`（`host_permissions` 已声明；本地开发可在登录视图「服务器与租户」中改回 `http://localhost:8000`）；
4. 任意网页右下角出现青绿悬浮球 → 点击展开；浏览器里已登录平台时自动接管登录态，否则用平台同款账号密码登录。

## 与 Firefox 版的差异

| | Chrome 版（本目录） | Firefox 版（`../firefox`） |
|---|---|---|
| manifest `background` | `service_worker` | `scripts`（event page） |
| 构建 target | `chrome120` | `firefox115` |
| 打包名 | `aistaff-extension.zip` | `aistaff-firefox-extension.zip` |

业务代码（`src/`）两边各存一份，内容相同；跨浏览器的 API 差异统一收在 `src/lib/ext.ts`。

## 架构速记

- 单文件 content script（IIFE，React 18 打包在内），Shadow DOM 隔离样式；
- background service worker 是**唯一网络层**：content script 与宿主页面同源，跨源 fetch 会被页面 CORS 卡死（生产网关对 OPTIONS 返回 400）；后台凭 host_permissions 豁免 CORS。content script 通过 `runtime.connect({ name: 'sd-proxy' })` 端口代理请求（JSON 一次性回传，SSE 逐块转发，断开端口即 abort）；token 存 `storage.local`；
- 自动登录：读平台前端写在 `localStorage['ultrarag_auth']` 的会话（`src/lib/platform-auth.ts`），本页是平台页直接读，不是则让后台注入平台标签页读，读到后用 `/api/auth/me` 验一次，无效则整体回滚；
- API 契约与 `frontend-enterprise/src/api/client.ts` 一致：`POST /api/auth/login`、`GET /api/chat/agents`、`POST /api/chat/stream`（SSE 事件：`session_created` / `stream_delta` / `token` / `stream_replace` / 终态事件集）、`POST /api/chat/sessions/{id}/cancel`；
- 会话与平台 Web 共享（同一 `ChatSession`），`channel: "extension"` 用于区分来源；对外品牌名为 AIStaff。

## 上线前 TODO

- [x] 生产域名 `https://ai-staff.folidaymall.com/*` 已加入 `host_permissions`，并作为默认登录地址；
- [ ] 补插件图标（icons/16/48/128，当前未声明走默认图标）；
- [ ] 「当前网站隐藏悬浮球」兜底入口；
- [ ] 划词发送（二期）。
