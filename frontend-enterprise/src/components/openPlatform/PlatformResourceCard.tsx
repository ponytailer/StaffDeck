import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import IconFolder from '../../assets/icons/cap-folder.svg?react';

/** Per-module accent used for the meta line and tag pills (SD1 232:4634 family). */
export type PlatformResourceAccent = 'green' | 'blue' | 'indigo' | 'orange';

const ACCENT_STYLES: Record<PlatformResourceAccent, { meta: string; tag: string }> = {
  green: { meta: 'text-[#2cb360]', tag: 'bg-[#e9f7ef] text-[#2cb360]' },
  blue: { meta: 'text-[#27c9ff]', tag: 'bg-[#c4f1ff] text-[#25c7ff]' },
  indigo: { meta: 'text-[#1a71ff]', tag: 'bg-[#e8f0ff] text-[#1a71ff]' },
  orange: { meta: 'text-[#ff7f00]', tag: 'bg-[#fff2e5] text-[#ff7f00]' },
};

export const platformResourceAccentStyles = ACCENT_STYLES;

export type PlatformResourceCardProps = {
  title: ReactNode;
  /** Accent metric line under the title, e.g. "12M / 6个片段". */
  meta: ReactNode;
  description: ReactNode;
  tags?: string[];
  /** Full 36px icon visual. When omitted a default folder tile is shown. */
  icon?: ReactNode;
  /** Module accent color for the meta line and tag pills. Defaults to green (知识库). */
  accent?: PlatformResourceAccent;
  onClick?: () => void;
  className?: string;
};

/**
 * 广场 resource card shared by the 知识库 / 技能 / SOP / 工具 modules. It renders a
 * colorful module icon, a title with a green meta line, a two-line description
 * and a row of green pills on a clean white card (SD1 232:4923).
 */
export default function PlatformResourceCard({
  title,
  meta,
  description,
  tags,
  icon,
  accent = 'green',
  onClick,
  className,
}: PlatformResourceCardProps) {
  const accentStyles = ACCENT_STYLES[accent];
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'relative flex h-[150px] w-full shrink-0 flex-col items-center justify-center overflow-hidden rounded-[14px] border-[0.5px] border-[#dfe4ee] bg-white p-[6px] text-left backdrop-blur-[1.835px] transition-shadow hover:border-[#cbd3e6] hover:shadow-[0_8px_20px_rgba(15,23,42,0.06)]',
        '',
        className,
      )}
    >
      <div className="flex w-full flex-col items-start gap-[8px] px-[12px]">
        <div className="flex w-full items-center gap-[8px]">
          {icon ?? (
            <span className="grid size-[40px] shrink-0 place-items-center rounded-[10px] bg-[#f2f4f8] text-[#8a94a6]">
              <IconFolder className="size-[22px]" />
            </span>
          )}
          <div className="flex min-w-0 flex-1 flex-col gap-[4px]">
            <p className="truncate text-[14px] font-medium text-[#464c5e]">{title}</p>
            <p className={cn('truncate text-[12px]', accentStyles.meta)}>{meta}</p>
          </div>
        </div>

        <p className="line-clamp-2 h-[36px] w-full text-[12px] leading-[18px] text-[#757f9c]">
          {description}
        </p>

        {tags && tags.length > 0 && (
          <div className="flex flex-wrap items-center gap-[8px]">
            {tags.map((tag) => (
              <span
                key={tag}
                className={cn(
                  'inline-flex items-center rounded-[90px] px-[10px] py-[3px] text-[10px] leading-[normal]',
                  accentStyles.tag,
                )}
              >
                {tag}
              </span>
            ))}
          </div>
        )}
      </div>
    </button>
  );
}
