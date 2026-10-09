import { useEffect, useRef, type CSSProperties, type ReactNode } from 'react';

import { animate, createAnimatable, createTimeline, stagger, utils } from 'animejs';

export type LoginCardStackProps = {
  /** 前排卡片的内容：真实的登录表单。 */
  children: ReactNode;
};

/**
 * 后排界面卡的静态错位参数。位移/旋转/缩放写在外层定位壳上（不参与动画），
 * 漂浮与入场动画只作用于内层，避免两套 transform 互相覆盖。
 */
const BACK_CARDS = [
  { x: -178, y: -86, rotate: -5, scale: 0.92, bars: ['w-[60%]', 'w-[80%]', 'w-[40%]'], tone: 'teal' },
  { x: 180, y: -72, rotate: 4, scale: 0.95, bars: ['w-[80%]', 'w-[60%]', 'w-[80%]'], tone: 'orange' },
  { x: 8, y: 96, rotate: -1.5, scale: 1, bars: ['w-[40%]', 'w-[80%]', 'w-[60%]'], tone: 'plain' },
] as const;

const TONE_CLASS: Record<string, string> = {
  teal: 'bg-[rgba(15,118,110,0.10)] text-[#0f7268]',
  orange: 'bg-[rgba(194,112,62,0.12)] text-[#c2703e]',
  plain: 'bg-[#eef1f6] text-[#757f9c]',
};

/** 卡片骨架条的公共样式。 */
const BAR_CLASS = 'h-[9px] rounded-[5px] bg-[#eef1f6]';

function hiddenState(scale: number): CSSProperties {
  return { opacity: 0, transform: `scale(${scale})` };
}

/**
 * 登录框的「视差浮层卡片堆」：
 *
 *   scene —— 透视容器，承接指针事件
 *   tilt  —— 指针视差：整个卡堆随光标轻微 3D 转动（createAnimatable 平滑跟随）
 *   shell —— 每张卡的静态错位（位移 + 旋转 + 缩放，内联 transform，不参与动画）
 *   float —— 漂浮：每张卡以不同周期缓慢上下呼吸
 *
 * 后排三张是产品界面示意卡（骨架条 + 能力片），前排是真实登录表单。
 */
export default function LoginCardStack({ children }: LoginCardStackProps) {
  const sceneRef = useRef<HTMLDivElement>(null);
  const tiltRef = useRef<HTMLDivElement>(null);
  const backRefs = useRef<Array<HTMLDivElement | null>>([]);
  const frontRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const scene = sceneRef.current;
    const tilt = tiltRef.current;
    const front = frontRef.current;
    const backs = backRefs.current.filter(Boolean) as HTMLDivElement[];
    if (!scene || !tilt || !front) return;

    const allCards = [...backs, front];

    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      utils.set(allCards, { opacity: 1, scale: 1 });
      return;
    }

    // 入场：后排依次浮出，前排表单最后落位
    const intro = createTimeline({ defaults: { duration: 720, ease: 'outQuint' } });
    intro
      .add(backs, { opacity: [0, 1], scale: [0.7, 1], duration: 640, delay: stagger(110), ease: 'outBack' }, 0)
      .add(front, { opacity: [0, 1], scale: [0.82, 1], duration: 780, ease: 'outBack' }, 320);

    // 漂浮：每张卡不同周期、不同相位，画面不「死」也不「晃」
    const floats = backs.map((card, index) =>
      animate(card, {
        y: [0, index % 2 ? -11 : 11],
        duration: 2600 + index * 520,
        delay: 1500,
        ease: 'inOutSine',
        loop: true,
        alternate: true,
      }),
    );
    const frontFloat = animate(front, {
      y: [0, -6],
      duration: 3400,
      delay: 1600,
      ease: 'inOutSine',
      loop: true,
      alternate: true,
    });

    // 指针视差：精细指针设备才启用，触屏不做
    let tiltAnimation: ReturnType<typeof createAnimatable> | null = null;
    const onPointerMove = (event: PointerEvent) => {
      if (!tiltAnimation) return;
      const bounds = scene.getBoundingClientRect();
      if (!bounds.width || !bounds.height) return;
      const px = (event.clientX - bounds.left) / bounds.width - 0.5;
      const py = (event.clientY - bounds.top) / bounds.height - 0.5;
      tiltAnimation.rotateY(px * 9);
      tiltAnimation.rotateX(-py * 6);
    };
    const onPointerLeave = () => {
      tiltAnimation?.rotateX(0);
      tiltAnimation?.rotateY(0);
    };

    if (window.matchMedia('(pointer: fine)').matches) {
      tiltAnimation = createAnimatable(tilt, { rotateX: 500, rotateY: 500, ease: 'out(2)' });
      scene.addEventListener('pointermove', onPointerMove);
      scene.addEventListener('pointerleave', onPointerLeave);
    }

    return () => {
      intro.revert();
      floats.forEach((item) => item.revert());
      frontFloat.revert();
      scene.removeEventListener('pointermove', onPointerMove);
      scene.removeEventListener('pointerleave', onPointerLeave);
      tiltAnimation?.revert();
    };
  }, []);

  return (
    <div ref={sceneRef} className="relative flex w-full justify-center py-[10px] [perspective:1200px]">
      <div ref={tiltRef} className="relative h-[352px] w-[min(560px,94vw)] [transform-style:preserve-3d]">
        {BACK_CARDS.map((card, index) => (
          <div
            key={card.tone}
            aria-hidden
            className="absolute left-1/2 top-1/2"
            style={{
              transform: `translate(-50%, -50%) translate(${card.x}px, ${card.y}px) rotate(${card.rotate}deg) scale(${card.scale})`,
            }}
          >
            <div
              ref={(el) => {
                backRefs.current[index] = el;
              }}
              style={hiddenState(1)}
              className="w-[290px] rounded-[16px] border border-black/[0.06] bg-white p-[18px] shadow-[0_20px_50px_rgba(17,17,17,0.10)]"
            >
              {card.bars.map((bar, barIndex) => (
                <div key={barIndex} className={`${BAR_CLASS} ${bar} ${barIndex > 0 ? 'mt-[10px]' : ''}`} />
              ))}
              <div className="mt-[16px] flex gap-[8px]">
                <span className={`rounded-[7px] px-[10px] py-[3px] text-[11px] ${TONE_CLASS[card.tone]}`}>
                  数字员工
                </span>
                <span className="rounded-[7px] bg-[#eef1f6] px-[10px] py-[3px] text-[11px] text-[#757f9c]">
                  执行记录
                </span>
              </div>
            </div>
          </div>
        ))}

        <div className="absolute left-1/2 top-1/2 z-10" style={{ transform: 'translate(-50%, -50%)' }}>
          <div
            ref={frontRef}
            style={hiddenState(1)}
            className="w-[326px] rounded-[18px] border border-black/[0.07] bg-white p-[22px] shadow-[0_28px_70px_rgba(17,17,17,0.14)]"
          >
            {children}
          </div>
        </div>
      </div>
    </div>
  );
}
