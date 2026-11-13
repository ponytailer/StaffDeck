import { useState } from 'react';
import { CircleCheck, Download, Puzzle } from 'lucide-react';

import type { EnterpriseAuthUser } from '../auth';
import AppHeader from '@/components/AppHeader';
import { cn } from '@/lib/utils';

/**
 * 插件包下载地址（平台静态资源的公开下载链接）。
 * 更新插件时：分别重新打包 extension/chrome 与 extension/firefox → 上传静态资源
 * → 替换下面两个 URL 与版本号。
 */
const EXTENSION_DOWNLOAD_URL =
  'https://ai-staff.folidaymall.com/f/JYQXUuNNzANu/aistaff-extension.zip';
/** Firefox 版打包同上，上传后把链接填进来；空字符串时按钮走「待上传」态 */
const EXTENSION_DOWNLOAD_URL_FIREFOX = '';
const EXTENSION_VERSION = 'v0.2.5';
const EXTENSION_UPDATED_AT = '2026-11-13';

type BrowserId = 'chrome' | 'firefox';

const BROWSERS: Array<{ id: BrowserId; label: string; short: string }> = [
  { id: 'chrome', label: 'Chrome / Edge', short: 'Chromium' },
  { id: 'firefox', label: 'Firefox', short: 'Firefox' },
];

type InstallStep = {
  title: string;
  body: string;
  /** 需要用户复制到地址栏的值（有则渲染复制按钮） */
  copyValue?: string;
  tip?: string;
};

const INSTALL_STEPS: Record<BrowserId, InstallStep[]> = {
  chrome: [
    {
      title: '下载插件包并解压',
      body: '点上方「下载 Chrome / Edge 版」拿到 aistaff-extension.zip，解压到一个固定目录（解压后不要删除，插件直接从这个目录加载）。',
    },
    {
      title: '打开扩展管理页',
      body: '在地址栏输入 chrome://extensions 并回车（或依次点开「窗口 → 扩展程序」）。',
      copyValue: 'chrome://extensions',
    },
    {
      title: '开启开发者模式并加载',
      body: '打开右上角「开发者模式」开关，点「加载已解压的扩展程序」，选中刚解压出来的文件夹。',
      tip: '建议固定到工具栏：扩展管理页点「详情 → 固定到工具栏」，随时能找到入口。',
    },
    {
      title: '免密登录',
      body: '打开任意网页，右下角会出现 AI 悬浮球。浏览器里已登录 AIStaff 平台时，点开面板会自动接管登录态，无需再输密码。',
      tip: '未登录平台也不影响：面板里提供平台同款账号密码登录入口，登录一次即可长期免登。',
    },
  ],
  firefox: [
    {
      title: '下载插件包并解压',
      body: '点上方「下载 Firefox 版」拿到 aistaff-firefox-extension.zip，解压到一个固定目录。',
    },
    {
      title: '打开 Firefox 调试页',
      body: '在地址栏输入 about:debugging#/runtime/this-firefox 并回车。',
      copyValue: 'about:debugging#/runtime/this-firefox',
    },
    {
      title: '临时载入附加组件',
      body: '点「临时载入附加组件」，选中解压目录里的 manifest.json（也可以直接选 zip 包）。',
      tip: 'Firefox 会询问「允许访问所有网站的数据」，必须点允许，否则脚本不会注入、悬浮球不会出现。',
    },
    {
      title: '免密登录',
      body: '打开任意网页，右下角会出现 AI 悬浮球。同一浏览器里已登录 AIStaff 平台时，面板会自动接管登录态。',
      tip: '临时载入的插件在关闭浏览器后失效，需要重新载入一次；长期使用请走签名安装（见下方常见问题）。',
    },
  ],
};

const FEATURES: Array<{ title: string; body: string }> = [
  {
    title: '任意网页随时唤起',
    body: '不用切回平台，在任何网页右下角点开悬浮球就能和数字员工对话。',
  },
  {
    title: '免费员工直接用',
    body: '免费员工开箱即聊，每天有赠额；模型员工与小队按平台配置的模型计费。',
  },
  {
    title: '会话与平台互通',
    body: '插件里的聊天记录和平台网页端同源同步，回到平台能接着看。',
  },
];

/** 右上角悬浮球 + 面板的纯 CSS 示意（不用外部图片，跟随品牌色）。 */
function ExtensionMock() {
  return (
    <div className="relative hidden h-[220px] w-[260px] shrink-0 select-none overflow-hidden rounded-[16px] border-[0.5px] border-[#e3e7f1] bg-[#fbfbfa] max-[1100px]:hidden">
      {/* 模拟网页骨架 */}
      <div className="px-[18px] pt-[16px]">
        <div className="h-[8px] w-[70px] rounded-full bg-[#e9ecf2]" />
        <div className="mt-[12px] flex gap-[10px]">
          <div className="h-[52px] flex-1 rounded-[10px] bg-white shadow-[0_1px_4px_0_rgba(0,0,0,0.04)]" />
          <div className="h-[52px] flex-1 rounded-[10px] bg-white shadow-[0_1px_4px_0_rgba(0,0,0,0.04)]" />
        </div>
        <div className="mt-[10px] h-[8px] w-[150px] rounded-full bg-[#eef0f4]" />
        <div className="mt-[8px] h-[8px] w-[110px] rounded-full bg-[#eef0f4]" />
      </div>
      {/* 展开的迷你聊天面板 */}
      <div className="absolute bottom-[-14px] right-[14px] w-[150px] rounded-[14px] border-[0.5px] border-[#e3e7f1] bg-white p-[10px] shadow-[0_8px_24px_0_rgba(24,24,26,0.12)]">
        <div className="flex items-center gap-[6px]">
          <div className="flex size-[16px] items-center justify-center rounded-[6px] bg-[#0f766e] text-[8px] font-semibold text-white">
            AI
          </div>
          <div className="h-[6px] w-[54px] rounded-full bg-[#e9ecf2]" />
        </div>
        <div className="mt-[8px] h-[6px] w-[110px] rounded-full bg-[#eef0f4]" />
        <div className="mt-[5px] h-[6px] w-[92px] rounded-full bg-[#eef0f4]" />
        <div className="mt-[8px] h-[6px] w-[74px] rounded-full bg-[#dcf0ec]" />
      </div>
      {/* 悬浮球 */}
      <div className="absolute bottom-[26px] right-[170px] flex size-[34px] items-center justify-center rounded-full bg-[#18181a] text-[11px] font-semibold text-white shadow-[0_4px_12px_0_rgba(24,24,26,0.28)]">
        AI
      </div>
    </div>
  );
}

/** 下载按钮：有链接是黑色实心，没链接走「待上传」灰态，避免点了没反应 */
function DownloadButton({
  label,
  url,
  variant,
}: {
  label: string;
  url: string;
  variant: 'solid' | 'outline';
}) {
  if (!url) {
    return (
      <span className="inline-flex h-[36px] cursor-not-allowed items-center gap-[6px] rounded-[10px] bg-[#eef0f4] px-[18px] text-[12px] font-normal text-[#a0a8bd]">
        <Download className="size-[14px]" />
        {label} · 上传后开放
      </span>
    );
  }
  return (
    <a
      href={url}
      className={cn(
        'inline-flex h-[36px] items-center gap-[6px] rounded-[10px] px-[18px] text-[12px] font-normal transition-colors',
        variant === 'solid'
          ? 'bg-[#18181a] text-white hover:bg-[#303030]'
          : 'border-[0.5px] border-[#e3e7f1] bg-white text-[#18181a] hover:border-[#cbd3e6] hover:bg-[#fbfbfa]',
      )}
    >
      <Download className="size-[14px]" />
      {label}
    </a>
  );
}

export default function BrowserExtensionPage({
  currentUser,
  onLogout,
}: {
  currentUser: EnterpriseAuthUser;
  onLogout?: () => void;
}) {
  const [copied, setCopied] = useState('');
  const [browser, setBrowser] = useState<BrowserId>('chrome');

  const copyValue = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(value);
      window.setTimeout(() => setCopied(''), 1600);
    } catch {
      // 剪贴板不可用时忽略，用户可手动输入
    }
  };

  const steps = INSTALL_STEPS[browser];

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        className="items-center"
        onLogout={onLogout}
        userName={currentUser?.username}
        title="浏览器插件"
      />

      <div className="mt-[16px] flex flex-col gap-[24px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px_18px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        {/* 头部：介绍 + 下载 */}
        <section className="flex items-start gap-[24px] rounded-[16px] border-[0.5px] border-[#e9ecf2] bg-[#fbfbfa] p-[24px]">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-[8px]">
              <div className="flex size-[30px] items-center justify-center rounded-[9px] bg-[#18181a]">
                <Puzzle className="size-[16px] text-white" />
              </div>
              <span className="text-[17px] font-semibold leading-none text-[#18181a]">
                AIStaff 浏览器助手
              </span>
              <span className="rounded-full bg-[#dcf0ec] px-[8px] py-[2px] text-[11px] leading-[16px] font-medium text-[#0f766e]">
                {EXTENSION_VERSION}
              </span>
            </div>
            <p className="mt-[10px] max-w-[520px] text-[13px] leading-[22px] text-[#757f9c]">
              把 AIStaff 数字员工装进每一个网页。安装后在任意网站右下角都会出现悬浮球，
              点开即可与免费员工、模型员工和小队对话，聊天记录与平台互通。
              提供 Chrome / Edge 与 Firefox 两个版本，功能一致、账号数据同源。
            </p>
            <div className="mt-[16px] flex flex-wrap items-center gap-[10px]">
              <DownloadButton label="下载 Chrome / Edge 版" url={EXTENSION_DOWNLOAD_URL} variant="solid" />
              <DownloadButton label="下载 Firefox 版" url={EXTENSION_DOWNLOAD_URL_FIREFOX} variant="outline" />
            </div>
            <div className="mt-[10px] text-[11px] text-[#a0a8bd]">
              更新于 {EXTENSION_UPDATED_AT} · 两个版本安装包独立，按你常用的浏览器下载对应版本即可
            </div>
          </div>
          <ExtensionMock />
        </section>

        {/* 安装步骤：按浏览器切换 */}
        <section>
          <div className="flex items-center justify-between px-[12px]">
            <span className="text-[14px] font-normal leading-none text-[#757f9c]">安装步骤</span>
            <div className="flex items-center gap-[2px] rounded-[10px] bg-[#f4f6f9] p-[3px]">
              {BROWSERS.map((item) => {
                const active = item.id === browser;
                return (
                  <button
                    key={item.id}
                    type="button"
                    aria-pressed={active}
                    onClick={() => setBrowser(item.id)}
                    className={cn(
                      'h-[26px] cursor-pointer rounded-[8px] px-[12px] text-[12px] transition-colors',
                      active
                        ? 'bg-white text-[#18181a] shadow-[0_1px_2px_0_rgba(24,24,26,0.08)]'
                        : 'text-[#757f9c] hover:text-[#464C5E]',
                    )}
                  >
                    {item.label}
                  </button>
                );
              })}
            </div>
          </div>
          <div className="mt-[14px] grid grid-cols-2 gap-[14px] max-[900px]:grid-cols-1">
            {steps.map((step, index) => (
              <div
                key={step.title}
                className="rounded-[14px] border-[0.5px] border-[#e9ecf2] bg-white p-[18px]"
              >
                <div className="flex items-center gap-[10px]">
                  <span
                    className={cn(
                      'flex size-[24px] items-center justify-center rounded-[8px] text-[12px] font-semibold',
                      index === steps.length - 1
                        ? 'bg-[#0f766e] text-white'
                        : 'bg-[#f2f4f8] text-[#464C5E]',
                    )}
                  >
                    {String(index + 1).padStart(2, '0')}
                  </span>
                  <span className="text-[14px] font-medium text-[#18181a]">{step.title}</span>
                </div>
                <p className="mt-[10px] text-[12.5px] leading-[21px] text-[#757f9c]">{step.body}</p>
                {step.copyValue && (
                  <button
                    type="button"
                    onClick={() => void copyValue(step.copyValue!)}
                    className="mt-[10px] inline-flex h-[26px] max-w-full cursor-pointer items-center gap-[5px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[10px] text-[11px] text-[#464C5E] transition-colors hover:border-[#cbd3e6]"
                  >
                    {copied === step.copyValue ? (
                      <CircleCheck className="size-[12px] shrink-0 text-[#0f766e]" />
                    ) : (
                      <span className="truncate font-mono">{step.copyValue}</span>
                    )}
                    <span className="shrink-0">{copied === step.copyValue ? '已复制' : '复制'}</span>
                  </button>
                )}
                {step.tip && (
                  <p className="mt-[8px] rounded-[8px] bg-[#fbfbfa] px-[10px] py-[7px] text-[11.5px] leading-[18px] text-[#a0a8bd]">
                    {step.tip}
                  </p>
                )}
              </div>
            ))}
          </div>
        </section>

        {/* 能力亮点 */}
        <section>
          <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
            <span className="text-[14px] font-normal leading-none">安装后能做什么</span>
          </div>
          <div className="mt-[14px] grid grid-cols-3 gap-[14px] max-[900px]:grid-cols-1">
            {FEATURES.map((feature) => (
              <div
                key={feature.title}
                className="rounded-[14px] border-[0.5px] border-[#e9ecf2] bg-white p-[16px]"
              >
                <div className="flex items-center gap-[6px]">
                  <CircleCheck className="size-[14px] text-[#0f766e]" />
                  <span className="text-[13px] font-medium text-[#18181a]">{feature.title}</span>
                </div>
                <p className="mt-[8px] text-[12px] leading-[20px] text-[#757f9c]">{feature.body}</p>
              </div>
            ))}
          </div>
        </section>

        {/* 注意事项 */}
        <section className="rounded-[14px] border-[0.5px] border-[#e9ecf2] bg-[#fbfbfa] p-[16px]">
          <div className="px-[2px] text-[13px] font-medium text-[#18181a]">常见问题</div>
          <ul className="mt-[8px] flex flex-col gap-[6px] px-[2px] text-[12px] leading-[20px] text-[#757f9c]">
            <li>· 插件使用平台同款账号登录，无需单独注册；两个浏览器版本账号与数据完全同源。</li>
            <li>
              · 免密登录依赖浏览器里 AIStaff 平台的登录态：同一浏览器已登录平台即自动接管；平台 token
              过期或未登录时退回密码登录。
            </li>
            <li>
              · 在插件内主动登出后不会自动接管；如需恢复免密，在平台网页端重新登录一次即可。
            </li>
            <li>
              · Firefox 版的「临时载入」在关闭浏览器后失效，重开需重新载入一次；要长期安装需走签名：
              把 zip 提交 addons.mozilla.org 做 unlisted 签名，或用 Firefox ESR 在 about:config
              关闭 xpinstall.signatures.required 后从文件安装。
            </li>
            <li>· 更新插件版本：Chrome 解压覆盖原目录后在扩展管理页点刷新图标；Firefox 重新临时载入一次即可。</li>
            <li>
              · 如悬浮球未出现，刷新网页一次；Chrome 请确认扩展已启用且页面非扩展商店等系统页，
              Firefox 请确认已授予「访问所有网站的数据」权限。
            </li>
          </ul>
        </section>
      </div>
    </div>
  );
}
