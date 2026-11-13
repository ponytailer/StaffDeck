# AIStaff 浏览器助手 · Firefox 版

把 AIStaff 数字员工装进每一个网页：注入式悬浮球 + 面板（登录 → 员工列表 → 流式聊天）。

> 本目录是 **Firefox 专用**的一份独立代码，与 `../chrome/` 互不影响。
> 功能、界面、后端接口与 Chrome 版 1:1 一致，差别只在浏览器适配层。

## Firefox 特有的三件事

1. **后台不是 service worker**：Firefox 至今不支持 `background.service_worker`
   （bug 1573659），MV3 的后台是 event page，manifest 里写
   `"background": { "scripts": ["background.js"] }`。逻辑与 Chrome 版完全相同。
2. **API 是 Promise 风格**：Firefox 同时提供 `browser.*`（返回 Promise）和
   `chrome.*`（只能回调）。`src/lib/ext.ts` 里统一判定并抹平，业务代码只写 `await`。
3. **`world: 'MAIN'` 需要 Firefox 128+**：自动登录要在平台标签页里读
   `localStorage`，老版本注入 MAIN world 会报错，适配层会自动退到隔离环境，
   那里读到的仍是页面同源存储，结果一致。

## 开发

```bash
cd extension/firefox
npm install
npm run build     # 产出 dist/（content.js + background.js + oa-panel.js + manifest.json）
npm run dev       # watch 模式
npm run typecheck # tsc --noEmit
npm run package   # 打成 dist/aistaff-firefox-extension.zip
```

## 安装到 Firefox

### 方式一：临时载入（开发/试用，重启浏览器后失效）

1. 地址栏打开 `about:debugging#/runtime/this-firefox`；
2. 点「临时载入附加组件」；
3. 选择 `extension/firefox/dist/manifest.json`（或直接选 `aistaff-firefox-extension.zip`）；
4. 任意网页右下角出现悬浮球 → 点击展开；浏览器里已登录平台时自动接管登录态。

> Firefox 会弹一次「是否允许访问所有网站的数据」——必须允许，否则 content script
> 不会注入，悬浮球也不会出现。

### 方式二：长期安装（需要签名或关掉签名强制）

Firefox 正式版默认只装**已签名**的扩展，所以自建分发有两条路：

- **提交 AMO 签名**（推荐）：把 zip 传到 addons.mozilla.org 做 unlisted 签名，
  拿回 `.xpi` 后即可拖进 Firefox 永久安装；
- **Firefox ESR / Developer Edition / Nightly**：在 `about:config` 把
  `xpinstall.signatures.required` 设为 `false`，然后 `about:addons → 齿轮 → 从文件安装附加组件`
  选 zip 安装（Firefox 正式版即使改了开关也不生效，这是浏览器限制）。

## 与 Chrome 版的差异

| | Chrome 版（`../chrome`） | Firefox 版（本目录） |
|---|---|---|
| manifest `background` | `service_worker` | `scripts`（event page） |
| manifest 额外字段 | — | `browser_specific_settings.gecko.id` + `strict_min_version: 115.0` |
| 构建 target | `chrome120` | `firefox115` |
| 打包名 | `aistaff-extension.zip` | `aistaff-firefox-extension.zip` |

## 架构速记

与 Chrome 版一致，见 `../chrome/README.md` 的「架构速记」。唯一区别是后台形态，
以及 `src/lib/ext.ts` 这层适配。
