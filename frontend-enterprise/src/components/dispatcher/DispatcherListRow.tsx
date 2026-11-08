import { cn } from '@/lib/utils';
import type { DispatcherRead } from '@/lib/dispatcher';

import DispatcherAvatar from './DispatcherAvatar';

/**
 * 「我的小队」左列表的一行。
 *
 * 从卡片改成一行：master-detail 布局下左栏只有 280px，卡片里那些
 * 「兜底方式 / 模型名 / 缺规则」挤进去会变成三行小字，扫读反而更慢。
 * 一行只留三件事 —— 头像、名字、规模（几位同事 · 转过多少次）。
 */
export default function DispatcherListRow({
  dispatcher,
  selected,
  busy,
  issueCount,
  transferCount,
  onSelect,
}: {
  dispatcher: DispatcherRead;
  selected: boolean;
  busy?: boolean;
  /** 配置问题数（缺规则 / 模型失效），>0 时行尾挂一个琥珀点 */
  issueCount: number;
  /**
   * 转接次数（来自 overview 的 activity）。
   * **不要用 dispatch.usage_count** —— 它是「建过几次会话」的计数器，与详情里的
   * 会话数、转接数都不是同一个口径，两个数字并排出现会让人以为算错了。
   */
  transferCount?: number;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-current={selected ? 'true' : undefined}
      data-dispatcher-row={dispatcher.id}
      className={cn(
        'group flex w-full items-center gap-[10px] rounded-[14px] px-[10px] py-[9px] text-left transition-colors',
        selected
          ? 'bg-[#eef7f5] ring-1 ring-[#cfe7e2]'
          : 'hover:bg-[#f7f8fa]',
        busy && 'opacity-60',
      )}
    >
      <DispatcherAvatar preset={dispatcher.avatar_preset} size={32} radius={10} />
      <span className="flex min-w-0 flex-1 flex-col gap-[1px]">
        <span className="flex min-w-0 items-center gap-[5px]">
          <span
            className={cn(
              'min-w-0 truncate text-[13px] font-medium',
              selected ? 'text-[#0f5f57]' : 'text-[#18181a]',
            )}
          >
            {dispatcher.name}
          </span>
          {dispatcher.published_to_gallery && (
            <span className="flex-none rounded-[4px] bg-[#e1f1ed] px-[4px] py-[1px] text-[9.5px] text-[#0f7268]">
              广场
            </span>
          )}
        </span>
        <span className="truncate text-[11px] text-[#858b9c]">
          {dispatcher.members.length} 位同事
          {typeof transferCount === 'number' ? ` · ${transferCount} 次转接` : ''}
        </span>
      </span>
      {issueCount > 0 && (
        <span
          className="size-[6px] shrink-0 rounded-full bg-[#d98324]"
          title={`${issueCount} 项配置待处理`}
        />
      )}
    </button>
  );
}
