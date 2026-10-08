import { useEffect, useRef } from 'react';

import { animate, createAnimatable, createTimeline, onScroll, utils } from 'animejs';

import loginPreview from '../assets/staffdeck/login-preview.png';

/**
 * 入场前的初始状态。写成内联样式而不是 CSS 类：`useEffect` 在首帧绘制之后才跑，
 * 只靠类名的话会先以最终状态闪一帧，再跳到起点。
 */
const REVEAL_FROM = {
  opacity: 0,
  transform: 'translate3d(0, 96px, 0) rotateX(16deg) scale(0.95)',
} as const;

/**
 * 登录页底部的产品预览（演示）区域。
 *
 * 拆成多层是为了让每个动画独占自己的 transform 属性，互不覆盖：
 *   stage  —— 滚动视差：整体随滚动轻微平移，和正文拉开层次
 *   glow   —— 背后的柔光，随入场一起亮起
 *   reveal —— 入场：从下方升起并转正，像一块屏幕被推到眼前
 *   float  —— 漂浮：入场结束后的低速呼吸，让画面不「死」
 *   tilt   —— 指针视差：光标移动时的轻微 3D 转动
 */
export default function LoginProductPreview() {
  const stageRef = useRef<HTMLDivElement>(null);
  const glowRef = useRef<HTMLDivElement>(null);
  const revealRef = useRef<HTMLDivElement>(null);
  const floatRef = useRef<HTMLDivElement>(null);
  const tiltRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const stage = stageRef.current;
    const glow = glowRef.current;
    const reveal = revealRef.current;
    const float = floatRef.current;
    const tilt = tiltRef.current;
    if (!stage || !glow || !reveal || !float || !tilt) return;

    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      utils.set(reveal, { opacity: 1, y: 0, scale: 1, rotateX: 0 });
      utils.set(glow, { opacity: 1, scale: 1 });
      return;
    }

    // 1. 入场：柔光亮起 → 预览升起转正。延迟一点，让标题先落地。
    const intro = createTimeline({ defaults: { ease: 'outQuint' }, delay: 260 });
    intro
      .add(glow, { opacity: [0, 1], scale: [0.82, 1], duration: 1200, ease: 'outExpo' }, 0)
      .add(
        reveal,
        { opacity: [0, 1], y: [96, 0], scale: [0.95, 1], rotateX: [16, 0], duration: 1200, ease: 'outExpo' },
        60,
      );

    // 2. 漂浮：等入场收尾再启动，幅度压在 6px 内，只做「呼吸」不做「晃动」
    const drift = animate(float, {
      y: [0, -6],
      duration: 3600,
      delay: 1500,
      ease: 'inOutSine',
      loop: true,
      alternate: true,
    });

    // 3. 滚动视差：预览比正文滚得慢一点。jsdom 等无 ResizeObserver 的环境直接跳过
    //    （滚动容器观测依赖它，缺少它就没法算阈值）。
    //    幅度压到 ±16px：再大就会顶出内容盒被裁掉一截，而滚到底也补不回来。
    const observer = typeof ResizeObserver === 'undefined'
      ? null
      : onScroll({ sync: 220, enter: 'bottom bottom', leave: 'top top' });
    const parallax = observer
      ? animate(stage, { y: [16, -16], ease: 'linear', autoplay: observer })
      : null;

    // 4. 指针视差：精细指针设备才启用，触屏不做
    let tiltAnimation: ReturnType<typeof createAnimatable> | null = null;
    const onPointerMove = (event: PointerEvent) => {
      if (!tiltAnimation) return;
      const bounds = stage.getBoundingClientRect();
      if (!bounds.width || !bounds.height) return;
      const px = (event.clientX - bounds.left) / bounds.width - 0.5;
      const py = (event.clientY - bounds.top) / bounds.height - 0.5;
      tiltAnimation.rotateY(px * 7);
      tiltAnimation.rotateX(-py * 5);
    };
    const onPointerLeave = () => {
      tiltAnimation?.rotateX(0);
      tiltAnimation?.rotateY(0);
    };

    if (window.matchMedia('(pointer: fine)').matches) {
      tiltAnimation = createAnimatable(tilt, {
        rotateX: 600,
        rotateY: 600,
        ease: 'out(2)',
      });
      stage.addEventListener('pointermove', onPointerMove);
      stage.addEventListener('pointerleave', onPointerLeave);
    }

    // 预览宽度跟着视口变，尺寸变了要重算滚动阈值
    const onResize = () => observer?.refresh();
    window.addEventListener('resize', onResize);

    return () => {
      intro.revert();
      drift.revert();
      parallax?.revert();
      observer?.revert();
      window.removeEventListener('resize', onResize);
      stage.removeEventListener('pointermove', onPointerMove);
      stage.removeEventListener('pointerleave', onPointerLeave);
      tiltAnimation?.revert();
    };
  }, []);

  return (
    <div className="mt-[36px] flex w-full justify-center">
      <div ref={stageRef} className="relative w-full max-w-[1120px] [perspective:1600px]">
        <div
          ref={glowRef}
          aria-hidden
          style={{ opacity: 0 }}
          className="pointer-events-none absolute inset-x-[6%] bottom-[8%] top-[8%] z-0 rounded-[80px] bg-[radial-gradient(ellipse_at_center,rgba(15,118,110,0.17),rgba(15,118,110,0)_72%)] blur-[70px]"
        />

        <div
          ref={revealRef}
          data-testid="login-preview-reveal"
          className="relative z-10 [transform-style:preserve-3d]"
          style={REVEAL_FROM}
        >
          <div ref={floatRef}>
            <div ref={tiltRef} className="[transform-style:preserve-3d]">
              <div className="overflow-hidden rounded-[16px] border border-black/[0.07] bg-white shadow-[0_28px_80px_rgba(17,17,17,0.14)]">
                <img
                  src={loginPreview}
                  alt="StaffDeck 产品预览"
                  className="block h-auto w-full select-none object-contain"
                  draggable={false}
                />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}