import { cn } from '@/lib/utils';
import logoFull from '../assets/fosun-holiday-logo.png';

export type BrandLogoProps = {
  /** Collapsed sidebar mode: render a smaller version of the brand image. */
  markOnly?: boolean;
  /** 可见 logo 的高度（px）。展开态默认 24；折叠态忽略此值，按轨道宽度自动收敛。 */
  markSize?: number;
  className?: string;
  /** Kept for backward compatibility; no longer used (the brand image already contains the wordmark). */
  wordmarkClassName?: string;
};

/**
 * 地中海度假集团（Club Med Lifestyle Group）品牌 lockup —— 单张横向 PNG，透明背景。
 *
 * ⚠️ 改图 / 换图前必读（这里踩过两次坑）：
 * 1. 资源已裁掉四周透明留白，画布 **630x126（= 5:1）**，墨迹铺满整图。
 *    历史事故：代码曾写死 `width = height * 3.1`，而换上的新图是 730x411（1.78:1）
 *    且带 73% 透明留白 → logo 被横向拉伸 1.79 倍，看起来「很扁」。
 *    **宽度一律由高度 × LOGO_ASPECT 推导，不要再写死别的比例。**
 * 2. 若换上的新图带透明留白，请先裁掉再替换：否则同一 height 下可见 logo 会又小又扁
 *    （旧图 186x60 墨迹占 169x40，新图 730x411 墨迹只占 630x126）。
 */
const LOGO_ASPECT = 630 / 126; // = 5

/**
 * 折叠轨道可用宽度极窄：`--sidebar-width-icon` 72px，两侧各 padding 16/20px，
 * 外层按钮再吃 p-[10px]。所以折叠态改为「按宽度反推高度」，避免横向溢出。
 */
const COLLAPSED_MAX_WIDTH = 40;

export default function BrandLogo({
  markOnly = false,
  markSize,
  className,
}: BrandLogoProps) {
  const height =
    markSize ?? (markOnly ? Math.round(COLLAPSED_MAX_WIDTH / LOGO_ASPECT) : 24);
  return (
    <span
      className={cn('flex items-center overflow-hidden p-[2px]', className)}
      style={{ height: height + 4 }}
    >
      <img
        src={logoFull}
        alt="地中海度假集团 Club Med Lifestyle Group"
        className="shrink-0"
        style={{ height, width: Math.round(height * LOGO_ASPECT) }}
      />
    </span>
  );
}
