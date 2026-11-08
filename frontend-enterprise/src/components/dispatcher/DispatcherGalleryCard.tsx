import { cn } from '@/lib/utils';
import StaffdeckIcon from '@/components/StaffdeckIcon';
import type { DispatcherRead } from '../../lib/dispatcher';
import { MAX_DISPATCH_MEMBERS, dispatchFallbackShortLabel } from '../../lib/dispatcher';
import DispatcherAvatar from './DispatcherAvatar';

/**
 * 员工广场「我的小队」tab 里的卡片。
 *
 * 与「我的小队」页的 `DispatcherListRow` 不是同一个东西：那边是窄栏列表的一行，
 * 这边是 262px 高的网格卡，要和 `EmployeeCard` 并排不违和 —— 所以沿用员工卡的
 * 结构（头像 + 名称/徽标 + 描述 + 底部统计条 + 主行动），统计项换成小队口径。
 */
export default function DispatcherGalleryCard({
  dispatcher,
  busy,
  onChat,
  onProfile,
}: {
  dispatcher: DispatcherRead;
  busy?: boolean;
  onChat: () => void;
  onProfile: () => void;
}) {
  const members = dispatcher.members;
  const missingRule = members.filter((member) => !member.routing_rule.trim()).length;
  const memberNames = members.map((member) => member.agent_name).filter(Boolean).join('、');

  const stats = [
    { value: String(members.length), label: '成员' },
    { value: dispatcher.usage_count > 9999 ? '9999+' : String(dispatcher.usage_count), label: '被使用' },
    { value: dispatchFallbackShortLabel(dispatcher.config.fallback_mode), label: '兜底', compact: true },
  ];

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => {
        if (!busy) onChat();
      }}
      onKeyDown={(event) => {
        if (!busy && (event.key === 'Enter' || event.key === ' ')) {
          event.preventDefault();
          onChat();
        }
      }}
      className={cn(
        'flex h-full flex-col rounded-[20px] border border-black/[0.05] bg-white p-[18px]',
        'shadow-[0_6px_20px_rgba(17,17,17,0.04)] transition-shadow hover:shadow-[0_12px_28px_rgba(17,17,17,0.08)]',
        busy && 'opacity-60',
      )}
      aria-label={`员工小队 ${dispatcher.name}`}
    >
      <div className="flex items-start gap-[10px]">
        <DispatcherAvatar preset={dispatcher.avatar_preset} size={38} radius={12} />
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="flex min-w-0 items-center gap-[6px]">
            <span className="min-w-0 truncate text-[14px] font-semibold text-[#18181a]">
              {dispatcher.name}
            </span>
            <span className="flex-none rounded-[5px] bg-[#f0f1f4] px-[5px] py-[1px] text-[10px] text-[#757f9c]">
              路由
            </span>
            {dispatcher.published_to_gallery && (
              <span className="flex-none rounded-[5px] bg-[#e1f1ed] px-[5px] py-[1px] text-[10px] text-[#0f7268]">
                广场
              </span>
            )}
          </div>
          <div className="mt-[1px] truncate text-[11.5px] text-[#9aa1b1]">
            员工小队 · {members.length}/{MAX_DISPATCH_MEMBERS} 位同事
          </div>
        </div>
      </div>

      <p className="m-0 mt-[10px] line-clamp-2 text-[12.5px] leading-[19px] text-[#5b6274]">
        {dispatcher.description || '还没写介绍 —— 写一句「它负责接哪些问题」，同事更容易判断什么时候找它'}
      </p>

      <div className="mt-[8px] flex flex-wrap items-center gap-[5px]">
        {memberNames && (
          <span className="max-w-full truncate rounded-[20px] border border-[#E3E7F1] px-[8px] py-px text-[10px] leading-[13px] text-[#757f9c]">
            {memberNames}
          </span>
        )}
        {missingRule > 0 && (
          <span className="rounded-[5px] bg-[#fdf6ec] px-[6px] py-[2px] text-[10px] text-[#8a5a12]">
            {missingRule} 位缺规则
          </span>
        )}
      </div>

      <div className="mt-auto grid grid-cols-3 rounded-[14px] border border-[#E3E7F1] box-sizing: border-box">
        {stats.map((stat, index) => (
          <div
            key={stat.label}
            className={cn(
              'flex flex-col justify-center gap-[4px] px-[10px] py-[6px]',
              index < stats.length - 1 && 'border-r border-[#eef1f5]',
            )}
          >
            <strong
              className={cn(
                'truncate font-bold text-[#18181A]',
                stat.compact ? 'text-[12px] leading-[24px]' : 'text-[18px] leading-[24px]',
              )}
            >
              {stat.value}
            </strong>
            <em className="text-[10px] not-italic text-[#464C5E]">{stat.label}</em>
          </div>
        ))}
      </div>

      <div className="mt-[10px] flex items-center gap-[8px]">
        <button
          type="button"
          disabled={busy}
          onClick={(event) => {
            event.stopPropagation();
            onChat();
          }}
          className="h-[32px] min-w-0 flex-1 whitespace-nowrap rounded-[8px] bg-[#18181a] text-[12px] text-white transition-colors hover:bg-[#303030] disabled:opacity-50"
        >
          {busy ? '打开中…' : '去对话'}
        </button>
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            onProfile();
          }}
          className="inline-flex h-[32px] shrink-0 items-center gap-[4px] whitespace-nowrap rounded-[8px] border border-[#e3e7f1] px-[12px] text-[12px] text-[#757f9c] transition-colors hover:border-[#cbd3e6] hover:text-[#18181a]"
        >
          <StaffdeckIcon name="file" size={13} />
          档案
        </button>
      </div>
    </div>
  );
}
