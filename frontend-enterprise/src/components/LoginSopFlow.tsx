import { useEffect, useRef, type CSSProperties, type ReactNode } from 'react';

import { animate, createTimeline, stagger, svg, utils } from 'animejs';

/** 一条光路串起的四步核心能力（与产品能力清单一一对应）。 */
const SOP_NODES = [
  { x: 200, y: 300, name: '流程 SOP', desc: '把经验写成可执行步骤', color: '#0f7268', icon: 'flow' },
  { x: 480, y: 130, name: '知识检索', desc: '让判断有据可依', color: '#c2703e', icon: 'search' },
  { x: 760, y: 300, name: '自主执行', desc: '跨系统推进到结果', color: '#6f7b42', icon: 'bolt' },
  { x: 1000, y: 130, name: '长期记忆', desc: '越用越懂你的业务', color: '#5b6fa8', icon: 'memory' },
] as const;

/**
 * 光路主干：三次贝塞尔依次穿过四个节点坐标，末端轻轻扬起收尾。
 * 节点坐标改动时这条曲线的锚点要同步改。
 */
const WAVE_PATH =
  'M 40 340 C 100 340, 140 300, 200 300 C 300 300, 380 130, 480 130 ' +
  'C 580 130, 660 300, 760 300 C 860 300, 940 130, 1000 130 C 1040 130, 1070 150, 1090 170';

/** 光脉冲到达各节点的时间占比（按路径长度估算，跑 linear 匀速才对得上）。 */
const PING_AT = [0.152, 0.42, 0.686, 0.914];

/** 节点小图标：与平台能力一一对应的线性图形（不依赖外部图标库）。 */
function NodeIcon({ kind }: { kind: (typeof SOP_NODES)[number]['icon'] }) {
  const common = {
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.9,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
  };
  return (
    <svg viewBox="0 0 24 24" className="size-[26px]" aria-hidden>
      {kind === 'flow' && (
        <g {...common}>
          <path d="M4 6.5h16M4 12h10M4 17.5h16" />
          <circle cx="18.5" cy="12" r="1.6" fill="currentColor" stroke="none" />
        </g>
      )}
      {kind === 'search' && (
        <g {...common}>
          <circle cx="11" cy="11" r="6" />
          <path d="M15.6 15.6 21 21" />
        </g>
      )}
      {kind === 'bolt' && <path {...common} d="M13 2.5 4.5 13.5h6l-1.5 8 8.5-11h-6z" />}
      {kind === 'memory' && (
        <g {...common}>
          <ellipse cx="12" cy="5.5" rx="7" ry="2.8" />
          <path d="M5 5.5v6.5c0 1.6 3.1 2.8 7 2.8s7-1.2 7-2.8V5.5" />
          <path d="M5 12v6.5c0 1.6 3.1 2.8 7 2.8s7-1.2 7-2.8V12" />
        </g>
      )}
    </svg>
  );
}

/** jsdom 没有 getTotalLength，SVG 补间跑不了——用它判定是否为非浏览器环境。 */
function supportsSvgMetrics(): boolean {
  return (
    typeof SVGPathElement !== 'undefined' &&
    typeof SVGPathElement.prototype.getTotalLength === 'function'
  );
}

/**
 * 登录页底部：SOP 光路流水线。
 *
 * 一条发光的流程线横向穿过四个能力节点，光脉冲匀速走完全程，
 * 途经节点逐个「ping」亮——把平台的核心能力讲成一条看得见的流水线。
 *
 * 结构：光脉冲跑 linear 匀速，PING_AT 的时间占比才和节点位置对得上。
 */
export default function LoginSopFlow() {
  const stageRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;

    const nodes = Array.from(stage.querySelectorAll<HTMLElement>('[data-sop-node]'));
    const basePath = stage.querySelector<SVGPathElement>('[data-sop-base]');
    const glowPath = stage.querySelector<SVGPathElement>('[data-sop-glow]');
    const travelPath = stage.querySelector<SVGPathElement>('[data-sop-travel]');
    const pulse = stage.querySelector<SVGGElement>('[data-sop-pulse]');
    if (!nodes.length || !basePath || !glowPath || !travelPath || !pulse) return;

    const settleVisible = () => {
      utils.set(nodes, { opacity: 1, scale: 1 });
      utils.set(pulse, { opacity: 0 });
    };

    if (
      window.matchMedia('(prefers-reduced-motion: reduce)').matches ||
      !supportsSvgMetrics()
    ) {
      settleVisible();
      return;
    }

    const disposers: Array<() => void> = [];
    let loopTimer: ReturnType<typeof setTimeout> | null = null;

    try {
      // 入场：光路从左到右描出来，节点依次点亮
      const drawables = svg.createDrawable([basePath, glowPath]);
      const intro = createTimeline({ defaults: { ease: 'outQuint' } });
      intro
        .add(drawables, { draw: '0 1', duration: 2200, ease: 'inOutQuad' }, 0)
        .add(
          nodes,
          { opacity: [0, 1], scale: [0.6, 1], duration: 620, delay: stagger(140), ease: 'outBack' },
          500,
        );
      disposers.push(() => intro.revert());

      // 主循环：光脉冲走完全程，途经节点逐个 ping 亮
      const flight = 7000;
      loopTimer = setTimeout(() => {
        const loop = createTimeline({ loop: true, loopDelay: 900 });
        loop
          .add(pulse, { opacity: [0, 1], duration: 160 }, 0)
          .add(pulse, { ...svg.createMotionPath(travelPath), duration: flight, ease: 'linear' }, 0)
          .add(pulse, { opacity: [1, 0], duration: 220 }, flight - 200);
        nodes.forEach((node, index) => {
          const at = flight * PING_AT[index];
          const ping = node.querySelector<HTMLElement>('[data-sop-ping]');
          const icon = node.querySelector<HTMLElement>('[data-sop-icon]');
          if (ping) loop.add(ping, { scale: [1, 1.8], opacity: [0.9, 0], duration: 620, ease: 'outQuad' }, at);
          if (icon) loop.add(icon, { scale: [1, 1.12, 1], duration: 520, ease: 'outQuad' }, at);
        });
        disposers.push(() => loop.revert());
      }, 2500);
      disposers.push(() => {
        if (loopTimer) clearTimeout(loopTimer);
      });
    } catch {
      // SVG 指标 API 异常等兜底：直接呈现完整网络，不为动效牺牲内容
      settleVisible();
      disposers.forEach((dispose) => dispose());
      return;
    }

    return () => {
      disposers.forEach((dispose) => dispose());
    };
  }, []);

  return (
    <div className="mt-[10px] flex w-full justify-center pb-[26px]">
      <div className="w-full max-w-[1120px]">
        <p className="text-center text-[13px] text-[#757f9c]">
          把一件事交给数字员工 —— 一条光路，四步完成
        </p>
        <div ref={stageRef} className="relative mt-[4px] aspect-[1120/420] w-full">
          <svg
            viewBox="0 0 1120 420"
            fill="none"
            aria-hidden
            className="absolute inset-0 h-full w-full"
          >
            <path data-sop-base d={WAVE_PATH} stroke="rgba(24,24,26,0.12)" strokeWidth="2" strokeLinecap="round" />
            <path
              data-sop-glow
              d={WAVE_PATH}
              stroke="rgba(15,118,110,0.32)"
              strokeWidth="5"
              strokeLinecap="round"
              style={{ filter: 'blur(4px)' }}
            />
            <path data-sop-travel d={WAVE_PATH} stroke="none" />
            <g data-sop-pulse opacity="0">
              <circle r="13" fill="rgba(15,118,110,0.20)" />
              <circle r="5.5" fill="#0f7268" />
            </g>
          </svg>

          {SOP_NODES.map((node) => {
            const style: CSSProperties = {
              left: `${(node.x / 1120) * 100}%`,
              top: `${(node.y / 420) * 100}%`,
              opacity: 0,
            };
            return (
              <div
                key={node.name}
                data-sop-node
                style={style}
                className="absolute flex -translate-x-1/2 -translate-y-1/2 flex-col items-center gap-[6px]"
              >
                <span
                  className="relative grid size-[62px] place-items-center rounded-full border-[1.5px] bg-white shadow-[0_8px_20px_rgba(17,17,17,0.07)]"
                  style={{ color: node.color, borderColor: `${node.color}66` }}
                >
                  <span data-sop-icon className="grid place-items-center">
                    <NodeIcon kind={node.icon} />
                  </span>
                  <span
                    data-sop-ping
                    className="absolute inset-[-4px] rounded-full border-2 opacity-0"
                    style={{ borderColor: node.color }}
                  />
                </span>
                <span className="text-[13px] font-semibold text-[#18181a]">{node.name}</span>
                <span className="-mt-[2px] whitespace-nowrap text-[11px] text-[#757f9c]">{node.desc}</span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
