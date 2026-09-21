import type { ReactNode } from 'react';

import IconSearch from '../assets/icons/search.svg?react';

/**
 * 「筛选/搜索无结果」的空状态。
 *
 * 收敛自 AgentsPage 的 `AgentsEmptyState` 与 EmployeeGalleryPage 的
 * `EmployeeGalleryEmptyState`：两者标记结构逐字节相同 —— 只有一处把文案写死、
 * 另一处做成了 props，长期看就是「同一个空态在两个页面长得一样但各改各的」。
 * 现在统一到这里，文案作为 props。
 */
export function SearchEmptyState({
  title,
  description,
  height = 'h-[262px]',
  icon,
}: {
  title: ReactNode;
  description: ReactNode;
  /** 外层高度类，默认与数字员工列表一致。 */
  height?: string;
  /** 覆盖默认的放大镜图标。 */
  icon?: ReactNode;
}) {
  return (
    <div
      className={`flex ${height} w-full items-center justify-center rounded-[20px] border border-dashed border-[#e4e9f2] bg-[#fbfcfe] px-[24px] text-center`}
    >
      <div className="flex max-w-[210px] flex-col items-center">
        <span className="grid size-[34px] place-items-center rounded-[12px] bg-white text-[#98a2b3] shadow-[0_1px_8px_rgba(70,76,94,0.06)] ring-1 ring-[#edf1f6]">
          {icon ?? <IconSearch className="size-[16px] shrink-0" />}
        </span>
        <p className="mt-[12px] text-[14px] font-medium leading-[20px] text-[#7f879a]">
          {title}
        </p>
        <p className="mt-[4px] text-[11px] leading-[17px] text-[#a7adbb]">{description}</p>
      </div>
    </div>
  );
}
