import type { ReactNode } from 'react';

import { useStaggerReveal } from '@/lib/motion';

export type RevealGroupProps = {
  children: ReactNode;
  className?: string;
  /**
   * 子元素数量。首次 `> 0` 时播放一次 stagger；后续筛选/翻页导致的增减
   * 不会再触发，避免列表反复跳动。
   */
  itemCount: number;
  /** 起始纵向位移（px）。 */
  y?: number;
  /** 子元素之间的 stagger 步长（ms）。 */
  stagger?: number;
  /** 变化时重播一次入场（例如开放平台的 tab 切换）；留空则只在首次播放。 */
  replayKey?: string | number;
  'aria-label'?: string;
};

/**
 * 给网格/列表容器加一次首屏 stagger 入场。
 *
 * 渲染结果就是一个普通的 `<div className=...>`，DOM 结构与直接写 `<div>` 完全一致，
 * 因此不会影响布局或既有测试；动效只在首次有子元素时播放。
 */
export function RevealGroup({
  children,
  className,
  itemCount,
  y = 12,
  stagger = 36,
  replayKey,
  'aria-label': ariaLabel,
}: RevealGroupProps) {
  const ref = useStaggerReveal<HTMLDivElement>(itemCount > 0, { y, stagger, replayKey });

  return (
    <div ref={ref} className={className} aria-label={ariaLabel}>
      {children}
    </div>
  );
}