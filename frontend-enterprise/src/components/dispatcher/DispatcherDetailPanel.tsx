import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import StaffdeckIcon from '@/components/StaffdeckIcon';
import type {
  DispatcherActivityRead,
  DispatcherMemberRead,
  DispatcherRead,
} from '../../lib/dispatcher';
import { dispatchFallbackLabel } from '../../lib/dispatcher';
import DispatcherAvatar from './DispatcherAvatar';

/**
 * 「我的小队」右详情面板：选中入口的只读速览 + 主行动。
 *
 * **为什么只读**：小队档案页（/enterprise/dashboard）已经承载「成员与路由规则 /
 * 兜底策略 / 活动记录」的权威展示与编辑。这里再放一份可编辑表单，就会出现
 * 「列表一份、详情一份、档案一份」三处漂移。所以这里只做速览，编辑一律跳档案。
 *
 * 唯一由本页产出的数据是「转接概览」（overview 接口的 activity），
 * 且口径写在 title 里：transfers 是各会话记录之和（每会话上限 10 → 下限）。
 */

function Section({
  title,
  action,
  children,
  className,
}: {
  title: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cn('rounded-[16px] border border-black/[0.05] bg-white p-[16px]', className)}>
      <div className="mb-[10px] flex items-center justify-between gap-[10px]">
        <h3 className="m-0 text-[13px] font-semibold text-[#18181a]">{title}</h3>
        {action}
      </div>
      {children}
    </section>
  );
}

function MemberLine({ member, index }: { member: DispatcherMemberRead; index: number }) {
  const missing = !member.routing_rule.trim();
  return (
    <div className="flex items-start gap-[9px] py-[7px]">
      <span className="mt-[1px] grid size-[18px] shrink-0 place-items-center rounded-[6px] bg-[#f2f4f7] text-[10.5px] text-[#5b6274]">
        {index + 1}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-[6px]">
          <span className="min-w-0 truncate text-[12.5px] font-medium text-[#18181a]">
            {member.agent_name || '已删除的同事'}
          </span>
          {member.has_own_model && (
            <span className="flex-none rounded-[4px] bg-[#f2f4f7] px-[4px] py-[1px] text-[9.5px] text-[#757f9c]">
              自有模型
            </span>
          )}
          {missing && (
            <span className="flex-none rounded-[4px] bg-[#fdf6ec] px-[4px] py-[1px] text-[9.5px] text-[#8a5a12]">
              缺规则
            </span>
          )}
        </div>
        <p
          className={cn(
            'm-0 mt-[2px] line-clamp-1 text-[11.5px]',
            missing ? 'text-[#b08246]' : 'text-[#757f9c]',
          )}
          title={member.routing_rule || undefined}
        >
          {missing ? '还没写「什么情况转给他」—— 命中率主要靠这句话' : member.routing_rule}
        </p>
      </div>
    </div>
  );
}

const ghostButtonClass =
  'inline-flex h-[34px] shrink-0 items-center gap-[5px] rounded-[9px] border border-[#e3e7f1] bg-white px-[12px] text-[12px] text-[#5b6274] transition-colors hover:border-[#cbd3e6] hover:text-[#18181a]';

export default function DispatcherDetailPanel({
  dispatcher,
  activity,
  busy,
  publishing,
  onCreate,
  onChat,
  onEdit,
  onProfile,
  onDelete,
  onGallery,
}: {
  dispatcher: DispatcherRead | null;
  activity?: DispatcherActivityRead;
  busy?: boolean;
  publishing?: boolean;
  onCreate: () => void;
  onChat: () => void;
  onEdit: () => void;
  onProfile: () => void;
  onDelete: () => void;
  onGallery: (published: boolean) => void;
}) {
  if (!dispatcher) {
    return (
      <div className="flex min-h-[420px] flex-col items-center justify-center gap-[10px] rounded-[20px] border border-dashed border-[#e3e7f1] bg-white/60 px-[24px] text-center">
        <DispatcherAvatar preset="network" size={46} radius={14} />
        <p className="m-0 text-[14px] font-medium text-[#18181a]">选一个入口，或先建一个</p>
        <p className="m-0 max-w-[360px] text-[12px] leading-[18px] text-[#757f9c]">
          入口不回答业务问题，只负责判断「这句话该由哪位同事接」，然后把问题转过去。
        </p>
        <button
          type="button"
          onClick={onCreate}
          className="mt-[4px] inline-flex h-[34px] items-center gap-[5px] rounded-[9px] border border-[#cfe7e2] bg-[#eef7f5] px-[14px] text-[12.5px] font-medium text-[#0f5f57] hover:bg-[#e4f3ef]"
        >
          新建小队
        </button>
      </div>
    );
  }

  const missingRule = dispatcher.members.filter((member) => !member.routing_rule.trim()).length;
  const modelBroken = Boolean(dispatcher.model_config_id) && !dispatcher.model_name;

  return (
    <div className="flex min-h-[420px] flex-col gap-[12px]">
      {/* 头部：头像 + 名字 + 状态 + 主行动 */}
      <div className="rounded-[20px] border border-black/[0.05] bg-white p-[18px] shadow-[0_6px_20px_rgba(17,17,17,0.04)]">
        <div className="flex flex-wrap items-start gap-[14px]">
          <DispatcherAvatar preset={dispatcher.avatar_preset} size={56} radius={18} decorative={false} />
          <div className="flex min-w-0 flex-1 flex-col gap-[3px]">
            <div className="flex min-w-0 flex-wrap items-center gap-[7px]">
              <h2 className="m-0 min-w-0 truncate text-[18px] font-semibold leading-[26px] text-[#18181a]">
                {dispatcher.name}
              </h2>
              <span className="flex-none rounded-[5px] bg-[#f0f1f4] px-[5px] py-[1px] text-[10px] text-[#757f9c]">
                纯路由
              </span>
              {dispatcher.published_to_gallery && (
                <span className="flex-none rounded-[5px] bg-[#e1f1ed] px-[5px] py-[1px] text-[10px] text-[#0f7268]">
                  已发布到广场
                </span>
              )}
              {!modelBroken && dispatcher.model_name && (
                <span className="text-[11.5px] text-[#858b9c]">路由模型 {dispatcher.model_name}</span>
              )}
            </div>
            <p className="m-0 line-clamp-2 text-[12px] leading-[18px] text-[#5b6274]">
              {dispatcher.description || '还没有写介绍。写一句「它负责接哪些问题」，同事更容易判断什么时候找它。'}
            </p>
          </div>
        </div>

        <div className="mt-[14px] flex flex-wrap items-center gap-[8px]">
          <button
            type="button"
            disabled={busy}
            onClick={onChat}
            className="inline-flex h-[34px] items-center gap-[5px] rounded-[9px] bg-[#18181a] px-[14px] text-[12.5px] font-medium text-white transition-colors hover:bg-[#303030] disabled:opacity-50"
          >
            {busy ? '打开中…' : '去对话'}
          </button>
          <button type="button" onClick={onEdit} className={ghostButtonClass}>
            配置成员
          </button>
          <button type="button" onClick={onProfile} className={ghostButtonClass}>
            员工小队档案
          </button>
          <DropdownMenu>
            <DropdownMenuTrigger
              aria-label={`更多操作 ${dispatcher.name}`}
              className="-ml-[2px] grid size-[34px] place-items-center rounded-[9px] border border-[#e3e7f1] bg-white text-[#b4bac8] transition-colors hover:text-[#5b6274]"
            >
              <StaffdeckIcon name="more" size={15} />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-[172px]">
              <DropdownMenuItem onSelect={() => onGallery(!dispatcher.published_to_gallery)} disabled={publishing}>
                {dispatcher.published_to_gallery ? '从广场下架' : '发布到广场'}
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem onSelect={onDelete} className="text-[#c0392b]">
                删除小队
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>

        {(missingRule > 0 || modelBroken) && (
          <div className="mt-[12px] flex flex-wrap items-center gap-[8px] rounded-[12px] border border-[#f0e0c2] bg-[#fdf8ee] px-[12px] py-[8px]">
            <span className="text-[11.5px] font-medium text-[#8a5a12]">这个入口有问题</span>
            {missingRule > 0 && (
              <span className="rounded-[6px] bg-white/80 px-[7px] py-[1px] text-[11px] text-[#7a5416]">
                {missingRule} 位同事缺路由规则
              </span>
            )}
            {modelBroken && (
              <span className="rounded-[6px] bg-white/80 px-[7px] py-[1px] text-[11px] text-[#7a5416]">
                绑定的模型已被删除或停用
              </span>
            )}
            <button
              type="button"
              onClick={onEdit}
              className="ml-auto shrink-0 text-[11.5px] font-medium text-[#8a5a12] underline-offset-2 hover:underline"
            >
              去修
            </button>
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 gap-[12px] lg:grid-cols-2">
        <Section
          title="成员与路由规则"
          action={
            <button type="button" onClick={onEdit} className="text-[11.5px] text-[#0f7268] hover:underline">
              去配置
            </button>
          }
        >
          <div className="divide-y divide-black/[0.04]">
            {dispatcher.members.map((member, index) => (
              <MemberLine key={member.member_agent_id} member={member} index={index} />
            ))}
            {dispatcher.members.length === 0 && (
              <p className="m-0 py-[8px] text-[11.5px] text-[#b08246]">
                还没有成员 —— 入口没有可转给的人。
              </p>
            )}
          </div>
        </Section>

        <Section
          title="无法匹配时"
          action={
            <button type="button" onClick={onEdit} className="text-[11.5px] text-[#0f7268] hover:underline">
              调整
            </button>
          }
        >
          <p className="m-0 text-[12.5px] text-[#18181a]">
            {dispatchFallbackLabel(dispatcher.config.fallback_mode)}
            <span className="ml-[8px] text-[11px] text-[#858b9c]">
              · 置信度门槛 {Math.round((dispatcher.config.min_confidence || 0) * 100)}%
            </span>
          </p>
          {dispatcher.config.fallback_mode === 'reply' && dispatcher.config.fallback_reply && (
            <p className="m-0 mt-[6px] line-clamp-3 rounded-[10px] bg-[#f7f8fa] px-[10px] py-[7px] text-[11.5px] leading-[17px] text-[#5b6274]">
              「{dispatcher.config.fallback_reply}」
            </p>
          )}
          <p className="m-0 mt-[8px] text-[11px] leading-[16px] text-[#858b9c]">
            {dispatcher.config.offer_candidates
              ? '判断不准时会先反问，并列出可能的同事。'
              : '判断不准时不会列候选，只按上面的方式回复。'}
          </p>
        </Section>
      </div>

      <Section
        title="转接情况"
        action={
          <span className="text-[11px] text-[#858b9c]" title="按会话活动时间近似：转接记录本身没有时间戳">
            近 7 天 {activity?.sessions_7d ?? 0} 个会话
          </span>
        }
      >
        <div className="flex flex-wrap items-end gap-[18px]">
          <span className="flex items-baseline gap-[5px]">
            <span
              className="text-[24px] font-semibold leading-none text-[#18181a]"
              title="各会话转接记录之和；每个会话最多记 10 条，所以是下限"
            >
              {activity?.transfers ?? dispatcher.usage_count}
            </span>
            <span className="text-[11.5px] text-[#5b6274]">次转接</span>
          </span>
          <span className="flex items-baseline gap-[5px]">
            <span className="text-[15px] font-medium leading-none text-[#464c5e]">
              {activity?.sessions ?? dispatcher.usage_count}
            </span>
            <span className="text-[11.5px] text-[#858b9c]">个会话</span>
          </span>
        </div>
        {activity && activity.top_members.length > 0 ? (
          <div className="mt-[10px] flex flex-wrap items-center gap-[6px]">
            <span className="text-[11px] text-[#858b9c]">最常转给</span>
            {activity.top_members.map((item) => (
              <span
                key={item.name}
                className="rounded-[6px] bg-[#f2f4f7] px-[7px] py-[2px] text-[11px] text-[#464c5e]"
              >
                {item.name} × {item.count}
              </span>
            ))}
          </div>
        ) : (
          <p className="m-0 mt-[10px] text-[11.5px] text-[#858b9c]">
            还没有转接记录。点「去对话」问一句业务问题，这里就会出现第一个数字。
          </p>
        )}
      </Section>
    </div>
  );
}
