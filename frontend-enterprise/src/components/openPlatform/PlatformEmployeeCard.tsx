import { Ban } from 'lucide-react';
import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import IconArrowRight from '../../assets/icons/arrow-right.svg?react';

export type PlatformStat = {
  value: ReactNode;
  label: string;
};

export type PlatformEmployeeCardProps = {
  /** Avatar illustration, typically an <EmployeeAvatar />. */
  avatar: ReactNode;
  name: ReactNode;
  role: ReactNode;
  online?: boolean;
  description: ReactNode;
  /** Bottom metric segments (资料 / 技能 / SOP …). */
  stats: PlatformStat[];
  onOpen?: () => void;
  onUnpublish?: () => void;
  unpublishing?: boolean;
  className?: string;
};

/** Compact 数字员工广场 card with an admin-only gallery governance action. */
export default function PlatformEmployeeCard({
  avatar,
  name,
  role,
  online = true,
  description,
  stats,
  onOpen,
  onUnpublish,
  unpublishing = false,
  className,
}: PlatformEmployeeCardProps) {
  return (
    <article
      className={cn(
        'group relative h-[180px] w-full shrink-0 rounded-[20px] border-[0.5px] border-[#dfe4ee] bg-white p-[6px] text-left transition-shadow hover:border-[#cbd3e6] hover:shadow-[0_10px_24px_rgba(0,0,0,0.06)]',
        className,
      )}
    >
      <button
        type="button"
        onClick={onOpen}
        className="flex h-full w-full flex-col justify-end gap-[8px] rounded-[16px] text-left outline-none focus-visible:ring-2 focus-visible:ring-[#9dd7cf]"
      >
        <div className="flex w-full flex-col px-[8px] pb-[2px]">
          <div className="flex h-[72px] w-full items-end justify-between rounded-[14px] bg-[#f6f6f6] px-[12px] pb-[6px] pt-[10px]">
            <div className="flex min-w-0 items-end gap-[12px]">
              <div className="flex h-[78px] w-[66px] shrink-0 items-end justify-center">
                {avatar}
              </div>
              <div className="flex min-w-0 flex-col items-start justify-center gap-[4px]">
                <p className="truncate text-[14px] leading-[1.35] font-medium text-[#18181a]">{name}</p>
                <p className="truncate text-[11px] leading-[1.6] text-[#757f9c]">{role}</p>
                <span className="inline-flex w-[40px] items-center justify-center rounded-[90px] bg-white px-[5px] py-[2px]">
                  <span className="flex items-center gap-[3px]">
                    <i
                      className={cn('size-[5px] shrink-0 rounded-full', online ? 'bg-[#22c55e]' : 'bg-[#9ca3af]')}
                      aria-hidden="true"
                    />
                    <span className="text-[10px] text-[#757f9c]">{online ? '在线' : '下线'}</span>
                  </span>
                </span>
              </div>
            </div>
            <span className="grid size-[28px] shrink-0 self-center place-items-center rounded-[10px] bg-white text-[#757f9c] transition-colors group-hover:text-[#18181a]">
              <IconArrowRight className="size-[16px]" />
            </span>
          </div>
        </div>

        <p className="line-clamp-2 h-[36px] w-full px-[10px] text-[12px] leading-[18px] text-[#757f9c]">
          {description}
        </p>

        <div className="flex w-full items-stretch px-[10px] pb-[4px]">
          {stats.map((stat, index) => (
            <div
              key={stat.label}
              className={cn(
                'flex h-[34px] flex-1 items-center justify-center border-[0.5px] border-[#e3e7f1] px-[10px]',
                index === 0 && 'rounded-l-[10px]',
                index === stats.length - 1 && 'rounded-r-[10px]',
                index > 0 && 'border-l-0',
              )}
            >
              <span className="flex items-baseline gap-[2px] leading-none">
                <span className="text-[13px] font-medium text-[#18181a]">{stat.value}</span>
                <span className="text-[10px] text-[#464c5e]">{stat.label}</span>
              </span>
            </div>
          ))}
        </div>
      </button>

      {onUnpublish && (
        <button
          type="button"
          aria-label="从广场下线"
          title="从广场下线"
          disabled={unpublishing}
          onClick={onUnpublish}
          className={cn(
            'absolute top-[8px] right-[8px] inline-flex h-[24px] items-center gap-[4px] rounded-[9px] border border-[#f3c7c7] bg-white px-[7px] text-[9px] font-medium text-[#b42318] shadow-[0_3px_10px_rgba(20,20,20,0.06)] transition-all',
            'pointer-events-none opacity-0 group-hover:pointer-events-auto group-hover:opacity-100 focus-visible:pointer-events-auto focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#f1aaaa]',
            'hover:border-[#e49b9b] hover:bg-[#fff7f7] disabled:cursor-wait disabled:opacity-50',
          )}
        >
          <Ban className="size-[11px]" strokeWidth={1.8} />
          下线
        </button>
      )}
    </article>
  );
}
