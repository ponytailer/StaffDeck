import { useEffect, useRef, type CSSProperties } from 'react';

import { animate, createTimeline, stagger, text, utils } from 'animejs';

import LoginForm from '../components/LoginForm';
import AppHeader from '@/components/AppHeader';
import BrandLogo from '@/components/BrandLogo';
import EmployeeAvatar from '@/components/EmployeeAvatar';
import LoginCardStack from '../components/LoginCardStack';
import LoginSopFlow from '../components/LoginSopFlow';
import LoginWingCards from '../components/LoginWingCards';
import type { EnterpriseAuthSession } from '../auth';
import { EMPLOYEE_AVATAR_PRESETS } from '../employee';

export type LoginPageProps = {
  onLogin: (session: EnterpriseAuthSession) => void;
};

/**
 * 登录页的「在岗播报」内容：岗位、头像与职责全部取自产品里真实的数字员工，
 * 不是营销文案——第一眼就能看出这个平台到底在做什么。
 */
const STAFF_ON_DUTY = [
  { role: '市场', avatarPreset: 'marketing-spark', duty: '梳理市场目标、受众与内容策划清单' },
  { role: '数据分析', avatarPreset: 'data-insight', duty: '定义指标口径、分析周期与对比维度' },
  { role: '采购', avatarPreset: 'procurement-check', duty: '准备采购需求、供应商比较与审批材料' },
  { role: '项目管理', avatarPreset: 'project-board', duty: '拆解目标、里程碑、责任人与风险' },
  { role: '行政', avatarPreset: 'after-sales-seal', duty: '统筹会议室预订、办公用品与用章申请' },
  { role: '销售', avatarPreset: 'sales-handshake', duty: '澄清客户需求、准备沟通材料并推进商机' },
] as const;

/** 入场前先藏起来，避免首帧闪一下再跳到起点（useEffect 在首帧绘制后才跑）。 */
function hiddenFrom(offsetY: number): CSSProperties {
  return { opacity: 0, transform: `translateY(${offsetY}px)` };
}

function avatarProfile(preset: string) {
  const found = EMPLOYEE_AVATAR_PRESETS.find((item) => item.key === preset);
  return {
    avatarKind: 'preset' as const,
    avatarImage: '',
    avatarPreset: preset,
    avatarText: found?.text ?? '员',
    avatarTone: found?.tone ?? 'teal',
  };
}

/**
 * 未登录的落地 / 登录页。
 *
 * 版式：品牌眼眉 + 产品主标题（按字入场）→ 副标题 → 在岗播报 →
 * 「视差浮层卡片堆」登录框（前排表单卡 + 后排界面卡随光标 3D 转动）
 * → 底部「SOP 光路流水线」：一条光路穿过四个能力节点，光脉冲循环跑完全程。
 * 两翼（宽屏）放「工作快照」卡：左执行中任务、右已交付成果——
 * 中央是「你」，两侧是数字员工正在干的活。
 * 背景只放几团缓慢漂移的光斑和一层淡网格：留白不空，但不抢内容。
 *
 * 性能约束（改这里之前先读一遍，实测 12fps 的教训）：
 *   1. 背景光斑不用 `filter: blur()`：模糊半径 70px 的层每帧都要重新栅格化，
 *      改用自身带柔边的 radial-gradient，transform 动画就只剩合成，不再重绘。
 *   2. 任何常驻动画都只允许动 transform / opacity，不动 letterSpacing、width、height。
 *   3. 表单状态在 LoginForm 内部：输入时这一层（连同卡片堆、两翼、光路）一次都不重渲染，
 *      否则每敲一个字符就会打断一圈合成层。
 */
export default function LoginPage({ onLogin }: LoginPageProps) {
  const pageRef = useRef<HTMLDivElement>(null);
  const brandRef = useRef<HTMLSpanElement>(null);
  const titleRef = useRef<HTMLSpanElement>(null);
  const subtitleRef = useRef<HTMLParagraphElement>(null);
  const tickerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const page = pageRef.current;
    const brand = brandRef.current;
    const title = titleRef.current;
    const subtitle = subtitleRef.current;
    const ticker = tickerRef.current;
    if (!page || !brand || !title || !subtitle || !ticker) return;

    const tickerRows = Array.from(ticker.querySelectorAll<HTMLElement>('[data-ticker-row]'));
    const auroraBlobs = Array.from(page.querySelectorAll<HTMLElement>('[data-aurora]'));
    const liveDots = Array.from(page.querySelectorAll<HTMLElement>('[data-live-dot]'));
    const heroBlocks = [brand, subtitle, ticker];

    // 主标题按字入场。拆字依赖 ResizeObserver，jsdom 等环境没有它，
    // 那就退化成整行出现，不为了动画把页面渲染搞挂。
    const splitter = typeof ResizeObserver === 'undefined'
      ? null
      : text.splitText(title, { chars: true, words: false, lines: false });
    const titleParts: HTMLElement[] = splitter?.chars?.length
      ? (splitter.chars as HTMLElement[])
      : [title];

    const disposers: Array<() => void> = [];
    if (splitter) disposers.push(() => splitter.revert());
    const disposeAll = () => {
      for (let i = disposers.length - 1; i >= 0; i -= 1) disposers[i]();
    };

    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      utils.set([...heroBlocks, title], { opacity: 1, y: 0, scale: 1 });
      utils.set(tickerRows[0], { y: '0%', opacity: 1 });
      return disposeAll;
    }

    // 背景光斑：十几秒一轮的缓慢漂移，让大面积留白「活」着。
    // 纯 transform（位移 + 缩放），元素本身没有 filter，所以帧内不重绘。
    auroraBlobs.forEach((blob, index) => {
      const drift = animate(blob, {
        x: [0, index % 2 ? -70 : 80],
        y: [0, index % 2 ? 50 : -60],
        scale: [1, index % 2 ? 1.18 : 0.9],
        duration: 11000 + index * 2600,
        ease: 'inOutSine',
        loop: true,
        alternate: true,
      });
      disposers.push(() => drift.revert());
    });

    // 在岗小绿点：呼吸
    liveDots.forEach((dot, index) => {
      const pulse = animate(dot, {
        scale: [1, 1.6],
        opacity: [1, 0.25],
        duration: 1000,
        ease: 'inOutSine',
        loop: true,
        alternate: true,
        delay: index * 130,
      });
      disposers.push(() => pulse.revert());
    });

    // 主入场：眼眉 → 标题逐字 → 副标题 → 在岗播报（登录卡堆入场由 LoginCardStack 自理）
    // 原来还带一段 letterSpacing 0.62em → 0.2em 的收字距动画：那是逐帧布局，已删除，
    // 字距直接用静态值，视觉差异肉眼几乎看不出，帧时间省一半。
    const intro = createTimeline({ defaults: { duration: 720, ease: 'outQuint' } });
    intro
      .set(title, { opacity: 1 }, 0)
      .add(brand, { opacity: [0, 1], y: [14, 0] }, 0)
      .add(
        titleParts,
        { opacity: [0, 1], y: ['0.7em', '0em'], duration: 900, delay: stagger(30), ease: 'outExpo' },
        140,
      )
      .add(subtitle, { opacity: [0, 1], y: [16, 0] }, '-=540')
      .add(ticker, { opacity: [0, 1], y: [14, 0] }, '-=400');
    disposers.push(() => intro.revert());

    // 在岗播报：6 条职责循环滚过（只动 translateY / opacity）
    if (tickerRows.length > 1) {
      const loop = createTimeline({ loop: true });
      tickerRows.forEach((row, index) => {
        loop
          .add(
            row,
            { y: ['110%', '0%'], opacity: [0, 1], duration: 560, ease: 'outQuint' },
            index === 0 ? 0 : '+=140',
          )
          .add(row, { y: ['0%', '-110%'], opacity: [1, 0], duration: 470, ease: 'inQuad' }, '+=1500');
      });
      disposers.push(() => loop.revert());
    }

    return disposeAll;
  }, []);

  return (
    <div ref={pageRef} className="relative flex min-h-screen flex-col overflow-x-clip bg-[#fbfbfa]">
      {/* 背景层：光斑 + 淡网格，纯装饰。
          光斑不加 filter: blur —— 柔边由 gradient 自己的多段色标提供，
          否则 70px 的模糊会跟着 transform 每帧重算，是原来卡顿的头号开销。 */}
      <div aria-hidden className="pointer-events-none absolute inset-0 overflow-hidden">
        <div
          data-aurora
          className="absolute -left-[18%] top-[-2%] h-[520px] w-[660px] rounded-full bg-[radial-gradient(circle,rgba(15,118,110,0.17),rgba(15,118,110,0.05)_52%,transparent_76%)] [will-change:transform]"
        />
        <div
          data-aurora
          className="absolute -right-[16%] top-[16%] h-[560px] w-[640px] rounded-full bg-[radial-gradient(circle,rgba(168,93,50,0.14),rgba(168,93,50,0.04)_52%,transparent_76%)] [will-change:transform]"
        />
        <div
          data-aurora
          className="absolute left-[26%] top-[52%] h-[460px] w-[640px] rounded-full bg-[radial-gradient(circle,rgba(111,123,66,0.13),rgba(111,123,66,0.04)_54%,transparent_78%)] [will-change:transform]"
        />
        <div className="absolute inset-0 bg-[radial-gradient(rgba(24,24,26,0.05)_1px,transparent_1px)] [background-size:26px_26px] [mask-image:radial-gradient(ellipse_at_50%_36%,black,transparent_72%)]" />
      </div>

      <AppHeader
        className="relative z-10 h-[60px] shrink-0 px-[32px] pt-[10px]"
        left={<BrandLogo markSize={28} />}
        right={null}
      />

      <main className="relative z-10 flex flex-1 flex-col items-center px-[32px] pb-[10px]">
        {/* 两翼工作快照：左侧执行中任务卡 + 右侧已交付成果卡（窄屏自动隐藏） */}
        <LoginWingCards />

        <div className="flex w-full max-w-[1120px] flex-col items-center pt-[46px] text-center">
          {/* 两行做层级化排版：公司名作眼眉（小号、宽字距、弱色）承接品牌，
              产品名作主标题。拆成两个 span 是为了让读屏把公司名与产品名分开读，
              中间的空白文本节点不能省。 */}
          <h1 className="flex flex-col items-center text-center">
            <span
              ref={brandRef}
              style={hiddenFrom(14)}
              className="mr-[-4px] text-[20px] font-medium leading-[30px] tracking-[4px] text-[#757f9c]"
            >
              地中海度假集团
            </span>
            {' '}
            <span
              ref={titleRef}
              style={{ opacity: 0 }}
              className="mt-[10px] text-[40px] font-semibold leading-[52px] tracking-[0.8px] text-[#18181a]"
            >
              AI 数字员工平台
            </span>
          </h1>

          <p
            ref={subtitleRef}
            style={hiddenFrom(16)}
            className="mt-[18px] max-w-[560px] text-[16px] leading-[26px] text-[#5b6274]"
          >
            把经验、流程与判断标准，沉淀成持续在岗的数字员工
          </p>

          {/* 在岗播报：一次只露一条，逐条滚过（置于登录框上方）。
              原来每行还带 backdrop-blur：6 个半透明行叠在一起滚动，
              等于每帧重采样 6 次背景，已改为纯白底。 */}
          <div
            ref={tickerRef}
            style={hiddenFrom(14)}
            className="relative mt-[26px] h-[52px] w-full max-w-[520px] overflow-hidden"
          >
            {STAFF_ON_DUTY.map((item) => (
              <div
                key={item.role}
                data-ticker-row
                style={{ opacity: 0, transform: 'translateY(110%)' }}
                className="absolute inset-0 flex items-center gap-[10px] rounded-[12px] border border-black/[0.05] bg-white px-[14px] text-left shadow-[0_6px_20px_rgba(17,17,17,0.05)]"
              >
                <EmployeeAvatar profile={avatarProfile(item.avatarPreset)} size={34} radius={10} />
                <span className="flex min-w-0 flex-1 items-baseline gap-[8px]">
                  <span className="shrink-0 text-[13px] font-medium text-[#18181a]">
                    {item.role}
                  </span>
                  <span className="truncate text-[12.5px] text-[#757f9c]">{item.duty}</span>
                </span>
                <span className="flex shrink-0 items-center gap-[6px] text-[12px] text-[#138a55]">
                  <i data-live-dot className="size-[6px] rounded-full bg-[#138a55]" />
                  在岗
                </span>
              </div>
            ))}
          </div>

          {/* 登录框：视差浮层卡片堆，前排为真实表单。
              表单自己管状态，输入时这一层不重渲染。 */}
          <LoginCardStack>
            <LoginForm onLogin={onLogin} />
          </LoginCardStack>
        </div>

        {/* 底部：SOP 光路流水线（替代原产品截图预览） */}
        <LoginSopFlow />
      </main>
    </div>
  );
}
