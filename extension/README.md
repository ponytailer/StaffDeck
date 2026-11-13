# AIStaff 浏览器助手

同一套能力的两个浏览器版本，**代码各自独立**，改一边不影响另一边。

```
extension/
├── chrome/     Chromium（Chrome / Edge / 360 / QQ 浏览器）版
└── firefox/    Firefox 版
```

| | chrome/ | firefox/ |
|---|---|---|
| manifest `background` | `service_worker` | `scripts`（event page） |
| 构建 target | `chrome120` | `firefox115` |
| 产物 | `dist/aistaff-extension.zip` | `dist/aistaff-firefox-extension.zip` |
| 载入方式 | `chrome://extensions` → 开发者模式 → 加载已解压的扩展程序 | `about:debugging#/runtime/this-firefox` → 临时载入 |

两边功能 1:1 一致：悬浮球 + 面板、平台登录态自动接管、员工列表、流式聊天、OA 代填。
所有跨浏览器的 API 差异（Promise / 回调、service worker / event page、MAIN world 版本）
都收在各自 `src/lib/ext.ts` 里，业务代码不用判断浏览器。

各自用法见 `chrome/README.md` 与 `firefox/README.md`。

平台侧介绍页：`frontend-enterprise/src/pages/BrowserExtensionPage.tsx`。
更新发布时两个 zip 都要重新打包上传，再改该文件顶部的下载地址常量。

## Firefox 上架 AMO 的硬性要求

AMO 的自动校验比 Chrome 严，以下几条踩过就装不上：

1. **`browser_specific_settings.gecko.data_collection_permissions` 必填**
   （Firefox 140+ 新规，缺了直接报 error 拒绝提交）。
   当前声明 `required: ["authenticationInfo", "websiteContent"]`：
   读平台登录 token 属于前者，读网页/OA 表单内容属于后者。若将来新增采集项要同步补。
2. **版本号必须递增**，同一个版本号不能重复上传，改 manifest 与 `package.json` 两处。
3. 代码是压缩产物，审核通常会要求提供**源码包**：
   `cd firefox && zip -r -X dist/aistaff-firefox-source.zip src scripts manifest.json package.json package-lock.json tsconfig.json vite.config.ts -x "*/node_modules/*"`
4. 用 **unlisted（自签名）** 通道发布，拿到 `.xpi` 后可正式版 Firefox 直接安装，
   不必每次 `about:debugging` 临时载入。

剩余 warning（`innerHTML` ×2、`import()` ×1）来自 react-dom 内部与 oa-panel 按需加载，
不影响签名，提交时可以带着走。
