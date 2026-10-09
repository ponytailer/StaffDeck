import { useEffect, useRef } from 'react';

import { animate, utils } from 'animejs';

/**
 * 两翼快照卡的静态姿态：旋转写在内层卡上（不参与动画），
 * 外层壳只承担定位，入场/漂浮动画作用在中间层，避免 transform 互相覆盖。
 */
const WINGS = [
  {
    side: 'left' as const,
    rotate: -2.5,
    tone: 'teal' as const,
    status: '项目任务',
    statusNote: '· 执行中',
    title: '供应商比价与审批材料',
    meta: 'SOP · 采购流程 v2 · 进行到 3 / 4 步',
  },
  {
    side: 'right' as const,
    rotate: 2.5,
    tone: 'orange' as const,
    status: '已交付',
    statusNote: '· 4 分钟前',
    title: '市场目标梳理报告',
    meta: '知识检索 · 结论已同步长期记忆',
  },
] as const;

const TONE_DOT: Record<'teal' | 'orange', string> = {
  teal: 'bg-[#0f7268] shadow-[0_0_0_3px_rgba(15,118,110,0.15)]',
  orange: 'bg-[#c2703e] shadow-[0_0_0_3px_rgba(194,112,62,0.15)]',
};

const STEPS = [
  { label: '澄清采购需求', state: 'done' },
  { label: '汇总供应商清单', state: 'done' },
  { label: '供应商比价分析…', state: 'now' },
  { label: '生成审批材料', state: 'todo' },
] as const;

const STEP_CLASS: Record<string, string> = {
  done: 'text-[#5b6274]',
  now: 'font-medium text-[#0f7268]',
  todo: 'text-[#9aa3b8]',
};

const STATS = [
  { value: '12', label: '建议项' },
  { value: '3', label: '受众分层' },
  { value: '98%', label: '依据引用' },
] as const;

function StepIcon({ state }: { state: 'done' | 'now' | 'todo' }) {
  if (state === 'done') {
    return (
      <i className="grid size-[15px] shrink-0 place-items-center rounded-full border border-[#0f7268] bg-[rgba(15,118,110,0.08)] text-[10px] leading-none text-[#0f7268]">
        ✓
      </i>
    );
  }
  if (state === 'now') {
    return <i className="size-[15px] shrink-0 rounded-full border border-[#0f7268] bg-[#0f7268] shadow-[0_0_0_3px_rgba(15,118,110,0.18)]" />;
  }
  return <i className="size-[15px] shrink-0 rounded-full border-[1.5px] border-[#e3e7f1]" />;
}

/**
 * 登录页两翼的「工作快照」卡：
 *
 *   左侧 —— 一张执行中的任务卡（步骤勾选 + 当前进度高亮 + 进度条生长）
 *   右侧 —— 一张刚交付的成果卡（指标 + 时间戳）
 *
 * 中央是「你」（登录框），两侧是数字员工正在干活的证据。
 * 叙事卡纯装饰（aria-hidden），窄屏（<1240px）整块隐藏不参与布局。
 */
export default function LoginWingCards() {
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;

    const shells = Array.from(root.querySelectorAll<HTMLElement>('[data-wing-float]'));
    const bar = root.querySelector<HTMLElement>('[data-wing-bar]');

    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      utils.set(shells, { opacity: 1 });
      if (bar) utils.set(bar, { width: '62%' });
      return;
    }

    const disposers: Array<() => void> = [];

    // 入场：淡入 + 缓慢漂浮（漂浮与入场错开，入场只动 opacity 不打架）
    shells.forEach((shell, index) => {
      const intro = animate(shell, {
        opacity: [0, 1],
        y: [18, 0],
        duration: 820,
        delay: 640 + index * 160,
        ease: 'outQuint',
      });
      const float = animate(shell, {
        y: [0, index % 2 ? -10 : 10],
        duration: 3000 + index * 500,
        delay: 1700 + index * 200,
        ease: 'inOutSine',
        loop: true,
        alternate: true,
      });
      disposers.push(() => {
        intro.revert();
        float.revert();
      });
    });

    // 左卡进度条：入场后从 0 生长到 62%
    if (bar) {
      const grow = animate(bar, {
        width: ['0%', '62%'],
        duration: 1700,
        delay: 1400,
        ease: 'outExpo',
      });
      disposers.push(() => grow.revert());
    }

    return () => {
      for (let i = disposers.length - 1; i >= 0; i -= 1) disposers[i]();
    };
  }, []);

  return (
    <div ref={rootRef} aria-hidden className="pointer-events-none absolute inset-x-0 top-0 hidden h-full min-[1240px]:block">
      {WINGS.map((wing) => (
        <div
          key={wing.side}
          className="absolute top-[236px] w-[296px]"
          style={wing.side === 'left' ? { left: 'calc(50% - 620px)' } : { right: 'calc(50% - 620px)' }}
        >
          <div data-wing-float style={{ opacity: 0 }}>
            <div
              className="rounded-[16px] border border-black/[0.06] bg-white p-[18px] shadow-[0_20px_50px_rgba(17,17,17,0.10)]"
              style={{ transform: `rotate(${wing.rotate}deg)` }}
            >
              <div className="flex items-center gap-[8px] text-[12px] tracking-[0.04em] text-[#757f9c]">
                <i className={`size-[7px] shrink-0 rounded-full ${TONE_DOT[wing.tone]}`} />
                <b className="font-medium text-[#18181a]">{wing.status}</b>
                <span>{wing.statusNote}</span>
              </div>

              <div className="mt-[10px] text-[15.5px] font-semibold leading-[22px] text-[#18181a]">
                {wing.title}
              </div>

              {wing.side === 'left' ? (
                <>
                  <ul className="mt-[12px] flex flex-col gap-[8px] text-[13px] leading-[18px]">
                    {STEPS.map((step) => (
                      <li key={step.label} className={`flex items-center gap-[9px] ${STEP_CLASS[step.state]}`}>
                        <StepIcon state={step.state} />
                        {step.label}
                      </li>
                    ))}
                  </ul>
                  <div className="mt-[14px] h-[6px] overflow-hidden rounded-[4px] bg-[#eef1f6]">
                    <i data-wing-bar className="block h-full w-0 rounded-[4px] bg-[linear-gradient(90deg,#0f7268,#2a9d8f)]" />
                  </div>
                </>
              ) : (
                <div className="mt-[12px] flex gap-[24px]">
                  {STATS.map((stat) => (
                    <div key={stat.label}>
                      <b className="block text-[21px] font-semibold leading-[26px] tracking-[-0.01em] text-[#c2703e]">
                        {stat.value}
                      </b>
                      <span className="text-[11.5px] text-[#757f9c]">{stat.label}</span>
                    </div>
                  ))}
                </div>
              )}

              <div className="mt-[14px] border-t border-dashed border-[#e3e7f1] pt-[12px] text-[11.5px] text-[#757f9c]">
                {wing.meta}
              </div>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
