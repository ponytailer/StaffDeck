import { useState } from 'react';
import { CircleCheck, Download, Puzzle } from 'lucide-react';

import type { EnterpriseAuthUser } from '../auth';
import AppHeader from '@/components/AppHeader';
import { cn } from '@/lib/utils';

/**
 * 插件包下载地址（平台静态资源的公开下载链接）。
 * 更新插件时：重新打包 extension/dist → 上传静态资源 → 替换这里的 URL 和版本号。
 */
const EXTENSION_DOWNLOAD_URL =
  'https://ai-staff.folidaymall.com/f/FK9aCdQDtiPN/aistaff-chrome-plugin.zip';
const EXTENSION_VERSION = 'v0.2.3';
const EXTENSION_UPDATED_AT = '2026-11-12';

type InstallStep = {
  title: string;
  body: string;
  tip?: string;
};

const INSTALL_STEPS: InstallStep[] = [
  {
    title: '下载插件包并解压',
    body: '点击上方按钮下载 aistaff-chrome-plugin.zip，解压到一个固定目录（解压后不要删除，插件直接从这个目录加载）。',
  },
  {
    title: '打开 Chrome 扩展管理页',
    body: '在 Chrome 地址栏输入 chrome://extensions 并回车（或依次点开「窗口 → 扩展程序」）。',
  },
  {
    title: '开启开发者模式并加载',
    body: '打开页面右上角的「开发者模式」开关，点「加载已解压的扩展程序」，选中刚解压出来的文件夹。',
    tip: '建议固定到工具栏：扩展管理页点「详情 → 固定到工具栏」，随时能找到入口。',
  },
  {
    title: '登录 AIStaff 账号',
    body: '打开任意网页，右下角会出现 AI 悬浮球，点击展开面板，用平台同款企业账号密码登录即可。',
  },
];

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

export default function BrowserExtensionPage({
  currentUser,
  onLogout,
}: {
  currentUser: EnterpriseAuthUser;
  onLogout?: () => void;
}) {
  const [copied, setCopied] = useState(false);

  const copyChromeUrl = async () => {
    try {
      await navigator.clipboard.writeText('chrome://extensions');
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      // 剪贴板不可用时忽略，用户可手动输入
    }
  };

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        className="items-center"
        onLogout={onLogout}
        userName={currentUser?.username}
        title="Chrome 浏览器插件"
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
            </p>
            <div className="mt-[16px] flex items-center gap-[12px]">
              {EXTENSION_DOWNLOAD_URL ? (
                <a
                  href={EXTENSION_DOWNLOAD_URL}
                  className="inline-flex h-[36px] items-center gap-[6px] rounded-[10px] bg-[#18181a] px-[20px] text-[12px] font-normal text-white transition-colors hover:bg-[#303030]"
                >
                  <Download className="size-[14px]" />
                  下载插件包（.zip）
                </a>
              ) : (
                <span className="inline-flex h-[36px] cursor-not-allowed items-center gap-[6px] rounded-[10px] bg-[#eef0f4] px-[20px] text-[12px] font-normal text-[#a0a8bd]">
                  <Download className="size-[14px]" />
                  插件包上传后开放下载
                </span>
              )}
              <span className="text-[11px] text-[#a0a8bd]">
                更新于 {EXTENSION_UPDATED_AT} · 仅支持 Chrome / Edge 等 Chromium 浏览器
              </span>
            </div>
          </div>
          <ExtensionMock />
        </section>

        {/* 安装步骤 */}
        <section>
          <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
            <span className="text-[14px] font-normal leading-none">安装步骤</span>
          </div>
          <div className="mt-[14px] grid grid-cols-2 gap-[14px] max-[900px]:grid-cols-1">
            {INSTALL_STEPS.map((step, index) => (
              <div
                key={step.title}
                className="rounded-[14px] border-[0.5px] border-[#e9ecf2] bg-white p-[18px]"
              >
                <div className="flex items-center gap-[10px]">
                  <span
                    className={cn(
                      'flex size-[24px] items-center justify-center rounded-[8px] text-[12px] font-semibold',
                      index === INSTALL_STEPS.length - 1
                        ? 'bg-[#0f766e] text-white'
                        : 'bg-[#f2f4f8] text-[#464C5E]',
                    )}
                  >
                    {String(index + 1).padStart(2, '0')}
                  </span>
                  <span className="text-[14px] font-medium text-[#18181a]">{step.title}</span>
                </div>
                <p className="mt-[10px] text-[12.5px] leading-[21px] text-[#757f9c]">{step.body}</p>
                {index === 1 && (
                  <button
                    type="button"
                    onClick={() => void copyChromeUrl()}
                    className="mt-[10px] inline-flex h-[26px] cursor-pointer items-center gap-[5px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[10px] text-[11px] text-[#464C5E] transition-colors hover:border-[#cbd3e6]"
                  >
                    {copied ? (
                      <CircleCheck className="size-[12px] text-[#0f766e]" />
                    ) : (
                      <span className="font-mono">chrome://extensions</span>
                    )}
                    {copied ? '已复制' : '复制'}
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
            <li>· 插件使用平台同款账号登录，无需单独注册；账号、数据与平台完全同源。</li>
            <li>· 更新插件版本：下载新 zip 解压覆盖原目录后，在扩展管理页点刷新图标即可。</li>
            <li>· 如悬浮球未出现，刷新网页一次；仍不出现请确认扩展已启用且页面非 Chrome 商店等系统页。</li>
          </ul>
        </section>
      </div>
    </div>
  );
}
