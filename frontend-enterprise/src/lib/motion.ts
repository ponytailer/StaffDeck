import { useEffect, useLayoutEffect, useRef, type RefObject } from 'react';
import { animate, stagger, utils } from 'animejs';
import type { AnimationParams, JSAnimation, TargetsParam } from 'animejs';

/**
 * Shared motion foundation for the enterprise console.
 *
 * 登录页（`LoginPage` / `LoginProductPreview`）已经有一套精修过的动效，但它是
 * 一次性写死的；后台页面要加动效时没有可复用的入口，容易各自为战，也容易漏掉
 * `prefers-reduced-motion` 分支。这里把最常用的三种动作收敛成一个基座：
 *
 *   reveal()           —— 一次性「淡入 + 上浮」，用于路由/列表/卡片入场
 *   useStaggerReveal() —— 容器首屏子元素 stagger 入场
 *   useCountUp()       —— 指标数字滚动
 *   useRouteReveal()   —— 路由切换时的内容淡入
 *
 * 所有导出在缺少 `matchMedia` 的环境（jsdom）与 reduced-motion 下都必须安全：
 * reduced-motion 直接落到终态、不产生位移，jsdom 里动效照常可跑但不会抛错。
 */

/** 全站统一的时长/缓动 token，数值对齐登录页既有节奏。 */
export const MOTION = {
  fast: 160,
  base: 240,
  slow: 420,
  /** 主入场缓动，与登录页保持一致。 */
  ease: 'outQuint',
  /** 柔和收尾缓动，用于数字/大块面。 */
  easeSoft: 'outExpo',
} as const;

/**
 * 是否处于「减少动效」偏好。
 *
 * jsdom 默认不实现 `matchMedia`，SSR 下也没有 `window`；两种环境都返回 `false`
 * （即按正常动效处理），保证测试与静态渲染不会因为缺少 API 而崩溃。
 */
export function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return false;
  }
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch {
    return false;
  }
}

/**
 * `useLayoutEffect` 在 SSR 下会告警。`StatCard` 之类组件会被
 * `renderToStaticMarkup` 渲染，所以服务端退回 `useEffect`（不执行）即可。
 */
const useIsomorphicLayoutEffect =
  typeof window === 'undefined' ? useEffect : useLayoutEffect;

export type RevealOptions = {
  /** 起始纵向位移（px）。`0` 表示只做透明度。 */
  y?: number;
  /** 起始缩放。留空表示不做缩放。 */
  scale?: number;
  /** 起始透明度。路由这种整页淡入宜用 0.3~0.4，避免「闪白」。 */
  opacity?: number;
  /** 单次动画时长（ms）。 */
  duration?: number;
  /** 缓动名。 */
  ease?: string;
  /** 每个目标之间的延迟（ms），用于 stagger。 */
  stagger?: number;
};

/** 起点是否包含纵向位移/缩放。为 0/未传时不写 transform，避免留下包含块。 */
function usesY(options: RevealOptions): boolean {
  return options.y != null && options.y !== 0;
}

function usesScale(options: RevealOptions): boolean {
  return options.scale != null;
}

/** 动画起点：先同步写进内联样式，避免首帧闪一下终态。 */
function fromState(options: RevealOptions): AnimationParams {
  const state: AnimationParams = { opacity: options.opacity ?? 0 };
  if (usesY(options)) state.y = options.y as number;
  if (usesScale(options)) state.scale = options.scale as number;
  return state;
}

/** 动画终点（也是 reduced-motion 下的落点）。 */
function toState(options: RevealOptions): AnimationParams {
  const state: AnimationParams = { opacity: 1 };
  if (usesY(options)) state.y = 0;
  if (usesScale(options)) state.scale = 1;
  return state;
}

/**
 * 把目标直接静态落到终态。用于 reduced-motion 以及清理阶段，
 * 保证内容在任何时候都是可见、可点击的。
 *
 * 只重置该次动画真正用到的属性：`y` 为 0 时绝不写 `transform`，
 * 否则会给长驻容器（如 `.content`）留下一个 transform 包含块。
 */
export function settle(targets: TargetsParam, options: RevealOptions = {}): void {
  const state: AnimationParams = { opacity: 1 };
  if (usesY(options)) state.y = 0;
  if (usesScale(options)) state.scale = 1;
  utils.set(targets, state);
}

/**
 * 动画结束后清掉内联 transform，避免它盖住后续的 CSS hover 位移
 * （内联样式优先级高于样式表里的 `:hover` 规则）。
 */
function clearInlineTransform(targets: TargetsParam): void {
  const list = Array.isArray(targets) ? targets : [targets];
  list.forEach((target) => {
    if (target instanceof HTMLElement) {
      target.style.transform = '';
      target.style.translate = '';
    }
  });
}

/**
 * 「淡入 + 上浮」入场。
 *
 * reduced-motion：静态落到终态并返回 `null`，调用方无需额外分支。
 * 正常情况下会先同步把目标设为起点，再交给 anime 补间——`useLayoutEffect`
 * 在绘制前执行，但 anime 的首帧是在 rAF 之后，若不先落起点会闪一帧。
 */
export function reveal(
  targets: TargetsParam,
  options: RevealOptions = {},
): JSAnimation | null {
  const { duration = MOTION.base, ease = MOTION.ease, stagger: step } = options;

  if (Array.isArray(targets) && targets.length === 0) return null;

  if (prefersReducedMotion()) {
    settle(targets, options);
    return null;
  }

  utils.set(targets, fromState(options));

  const params: AnimationParams = {
    ...toState(options),
    duration,
    ease,
  };
  if (step != null) {
    params.delay = stagger(step) as AnimationParams['delay'];
  }
  const animation = animate(targets, params);
  // 动画收尾后清掉内联 transform，否则会盖住卡片 hover 的 CSS 位移。
  if (usesY(options) || usesScale(options)) {
    animation.then(() => clearInlineTransform(targets));
  }
  return animation;
}

/**
 * 容器首屏 stagger 入场。
 *
 * 只在 `enabled` 第一次为真、且容器里已经有子元素时播放一次；筛选、翻页导致的
 * 子元素增减不会再触发（`played` ref 锁住），避免列表反复「抖动」。
 *
 * 需要「每次切换都重播」（例如开放平台的 tab 切换）时传入 `replayKey`：
 * key 变化会重放入场，未传则保持只播一次。
 */
export function useStaggerReveal<T extends HTMLElement>(
  enabled: boolean,
  options: RevealOptions & { selector?: string; replayKey?: string | number } = {},
): RefObject<T> {
  const ref = useRef<T>(null);
  const played = useRef(false);
  const {
    selector = ':scope > *',
    y = 12,
    stagger: step = 36,
    duration,
    scale,
    replayKey,
  } = options;

  useIsomorphicLayoutEffect(() => {
    if (!enabled) return;
    if (replayKey === undefined && played.current) return;
    const container = ref.current;
    if (!container) return;
    const items = Array.from(container.querySelectorAll<HTMLElement>(selector));
    if (items.length === 0) return;
    played.current = true;
    const animation = reveal(items, { y, stagger: step, duration, scale });
    return () => {
      // 组件销毁或依赖变化时把内容落回可见，绝不留下 opacity:0。
      animation?.pause();
      settle(items, { y, scale });
    };
  }, [enabled, replayKey, selector, y, step, duration, scale]);

  return ref;
}

/**
 * 指标数字滚动。
 *
 * `ref` 绑定的元素在渲染时仍然保留 `{value}` 作为子节点：这样 SSR/静态渲染
 * 能输出真实数字，且 React 在 `value` 不变时不会重写 `textContent`，与命令式的
 * 补间互不干扰。`value` 变化时从旧值滚到新值。
 */
export function useCountUp(value: number, duration = 900): RefObject<HTMLSpanElement> {
  const ref = useRef<HTMLSpanElement>(null);
  const previous = useRef(0);

  useIsomorphicLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;

    const from = previous.current;
    previous.current = value;

    if (prefersReducedMotion() || from === value) {
      el.textContent = String(value);
      return;
    }

    // 同步写起点：anime 的首帧在 rAF 之后，不先落起点会先闪一帧目标值。
    el.textContent = String(from);

    const state = { n: from };
    const animation = animate(state, {
      n: value,
      duration,
      ease: MOTION.easeSoft,
      onUpdate: () => {
        el.textContent = String(Math.round(state.n));
      },
      onComplete: () => {
        el.textContent = String(value);
      },
    });

    return () => {
      animation.revert();
      el.textContent = String(value);
    };
  }, [value, duration]);

  return ref;
}

/**
 * 路由切换时的内容淡入。
 *
 * 只用 `opacity`，不加 transform：`.content` 是长驻容器，加 transform 会创建
 * 包含块，可能让页面里的 `fixed` / `sticky` 元素瞬间错位。`key` 变化即重播。
 */
export function useRouteReveal<T extends HTMLElement>(
  key: string,
  options: RevealOptions = {},
): RefObject<T> {
  const ref = useRef<T>(null);

  useIsomorphicLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const animation = reveal(el, { y: 0, opacity: 0.4, duration: MOTION.fast + 40, ...options });
    return () => {
      animation?.pause();
      settle(el);
    };
    // 仅随路由 key 重播；options 由调用方以常量形式传入。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return ref;
}