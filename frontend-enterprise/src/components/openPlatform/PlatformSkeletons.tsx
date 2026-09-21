import { cn } from '@/lib/utils';

/**
 * 开放广场的骨架屏统一定义。
 *
 * 之前有三份各自内联的骨架（OpenPlatformPage / PlatformKindDetailView 的
 * `DetailSkeleton` 逐字节相同，PlatformColumn 的列内骨架同款卡片），卡片基类
 * `rounded-[20px] border-[#f0f1f5] bg-[#f6f6f6]` 被抄了三遍 —— 改一处忘一处就会出现
 * 「同一个广场的加载态长得不一样」。这里收敛成单一来源。
 */

/** 广场卡片骨架的公共外观。改这里即可全站生效。 */
export const PLATFORM_SKELETON_CARD_CLASS =
  'w-full animate-pulse rounded-[20px] border-[0.5px] border-[#f0f1f5] bg-[#f6f6f6]';

/**
 * 单张广场卡片骨架。
 *
 * `height` 默认 `h-[112px]`；数字员工卡（agents）比资源卡高，走 `h-[140px]`。
 */
export function PlatformCardSkeleton({
  height = 'h-[112px]',
  className,
}: {
  height?: string;
  className?: string;
}) {
  return <div className={cn(PLATFORM_SKELETON_CARD_CLASS, height, className)} />;
}

/** 按模块取卡片骨架高度：数字员工卡更高，其余资源卡一致。 */
export function platformCardSkeletonHeight(kind: string): string {
  return kind === 'agents' ? 'h-[140px]' : 'h-[112px]';
}

/**
 * 详情页网格骨架（开放广场模块全览）。
 *
 * 默认 8 张卡片，与详情页的响应式列数保持一致。
 */
export function PlatformGridSkeleton({
  kind,
  count = 8,
}: {
  kind: string;
  count?: number;
}) {
  const height = platformCardSkeletonHeight(kind);
  return (
    <div className="grid grid-cols-1 gap-[16px] sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5">
      {Array.from({ length: count }, (_, index) => (
        <PlatformCardSkeleton key={index} height={height} />
      ))}
    </div>
  );
}

/** 单列（竖排）骨架，用于开放广场首页的模块列。 */
export function PlatformColumnSkeleton({ count = 3 }: { count?: number }) {
  return (
    <div className="flex w-full flex-col gap-[16px]">
      {Array.from({ length: count }, (_, index) => (
        <PlatformCardSkeleton key={index} className="shrink-0" />
      ))}
    </div>
  );
}
