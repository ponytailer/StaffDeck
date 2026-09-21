/// <reference types="vite/client" />
/// <reference types="vite-plugin-svgr/client" />

interface ImportMetaEnv {
  /** 可用：数字员工分享链接的对外域名（如 https://deck.example.com），未配置则用当前 origin。 */
  readonly VITE_SHARE_HOST?: string;
}

