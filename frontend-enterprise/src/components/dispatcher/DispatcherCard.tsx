/**
 * 多智能体在「数字员工」列表里的卡片。
 *
 * 与 EmployeeCard 的差异（都是刻意的）：
 * - 头像位置换成「入口 + 下辖成员」的小拓扑缩略，一眼看出它能转给谁；
 * - 主操作是「去对话」，没有知识库/技能/工具入口（它本来就不配这些）；
 * - 不能标记免费、不能上下线（后端对 dispatcher 的资源与免费端点都返回 403）；
 * - 但**可以发布到开放广场的「员工小队」**（与普通员工同一个 published_to_gallery 口径）。
 */

import { cn } from '@/lib/utils';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import StaffdeckIcon from '@/components/StaffdeckIcon';
import type { DispatcherRead } from '../../lib/dispatcher';
import { MAX_DISPATCH_MEMBERS, dispatchFallbackLabel } from '../../lib/dispatcher';
import DispatcherAvatar from './DispatcherAvatar';

export default function DispatcherCard({
  dispatcher,
  busy,
  publishing,
  onChat,
  onEdit,
  onDelete,
  onGallery,
  onOpenProfile,
}: {
  dispatcher: DispatcherRead;
  busy?: boolean;
  /** 发布/下架请求进行中 */
  publishing?: boolean;
  onChat: () => void;
  onEdit: () => void;
  onDelete: () => void;
  /** 发布到广场 / 从广场下架。传 true 表示发布，false 表示下架 */
  onGallery: (published: boolean) => void;
  /** 切到该小队作用域并跳档案页（对应切换器里的「当前员工小队」） */
  onOpenProfile: () => void;
}) {
  const members = dispatcher.members;
  const missingRule = members.filter((item) => !item.routing_rule.trim()).length;
  const ownModelCount = members.filter((item) => item.has_own_model).length;
  const published = dispatcher.published_to_gallery === true;
  // 没成员的入口发到广场上也没法路由，后端也会拒（400）；这里先禁用，省一次往返
  const canPublish = members.length > 0;

  return (
    <div className="group relative flex h-full flex-col rounded-[20px] border border-black/[0.05] bg-white p-[18px] shadow-[0_6px_20px_rgba(17,17,17,0.04)] transition-shadow hover:shadow-[0_12px_28px_rgba(17,17,17,0.08)]">
      <div className="flex items-start gap-[10px]">
        <DispatcherAvatar preset={dispatcher.avatar_preset} size={38} radius={12} />
        <div className="flex min-w-0 flex-1 flex-col">
          {/* 名称与徽标同行：名称可截断（min-w-0 + truncate），徽标 flex-none 不许被压。
              反过来写（徽标也参与收缩）时，长名字会把「路由/广场」挤成两行。 */}
          <div className="flex min-w-0 items-center gap-[6px]">
            <span className="min-w-0 truncate text-[14px] font-semibold text-[#18181a]">
              {dispatcher.name}
            </span>
            <span className="flex-none rounded-[5px] bg-[#f0f1f4] px-[5px] py-[1px] text-[10px] text-[#757f9c]">
              路由
            </span>
            {published && (
              <span className="flex-none rounded-[5px] bg-[#e1f1ed] px-[5px] py-[1px] text-[10px] text-[#0f7268]">
                广场
              </span>
            )}
          </div>
          <div className="mt-[1px] truncate text-[11.5px] text-[#9aa1b1]">
            员工小队 · {members.length}/{MAX_DISPATCH_MEMBERS} 位同事
          </div>
        </div>
        {/* 低频管理动作收进右上角「···」：档案 / 发布下架 / 删除。底部只留「去对话 + 配置」，
            按钮不再挤成两行，窄屏也不会把文字压变形。
            档案放第一位：它是最常回看的入口，且与员工卡片「进档案」的心智一致。 */}
        <DropdownMenu>
          <DropdownMenuTrigger
            aria-label={`更多操作 ${dispatcher.name}`}
            className="-mr-[4px] -mt-[2px] grid size-[26px] shrink-0 place-items-center rounded-[8px] text-[#b4bac8] transition-colors hover:bg-[#f2f3f6] hover:text-[#5b6274] data-[state=open]:bg-[#f2f3f6] data-[state=open]:text-[#5b6274]"
          >
            <StaffdeckIcon name="more" size={15} />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-[148px] rounded-[10px] p-[4px]">
            <DropdownMenuItem
              onSelect={onOpenProfile}
              className="rounded-[7px] px-[8px] py-[6px] text-[12px] text-[#5b6274] data-[highlighted]:bg-[#f6f6f6]"
            >
              员工小队档案
            </DropdownMenuItem>
            <DropdownMenuSeparator className="my-[3px]" />
            <DropdownMenuItem
              disabled={publishing || (!published && !canPublish)}
              onSelect={() => onGallery(!published)}
              title={!published && !canPublish ? '先添加同事才能发布到广场' : undefined}
              className="rounded-[7px] px-[8px] py-[6px] text-[12px] text-[#5b6274] data-[highlighted]:bg-[#f6f6f6]"
            >
              {publishing ? '处理中…' : published ? '从广场下架' : '发布到广场'}
            </DropdownMenuItem>
            <DropdownMenuSeparator className="my-[3px]" />
            <DropdownMenuItem
              onSelect={onDelete}
              className="rounded-[7px] px-[8px] py-[6px] text-[12px] text-[#dc2626] data-[highlighted]:bg-[#fdf2f2] data-[highlighted]:text-[#dc2626]"
            >
              删除
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {dispatcher.description && (
        <p className="mt-[10px] line-clamp-2 text-[12px] leading-[1.6] text-[#5b6274]">
          {dispatcher.description}
        </p>
      )}

      {/* 成员缩略：名称 + 是否配了规则 */}
      <div className="mt-[10px] flex flex-col gap-[5px]">
        {members.length === 0 ? (
          <div className="rounded-[8px] bg-[#fdf6ec] px-[9px] py-[7px] text-[11.5px] text-[#8a5a12]">
            还没有成员，问题会全部走兜底
          </div>
        ) : (
          members.map((member, index) => {
            const noRule = !member.routing_rule.trim();
            return (
              <div
                key={member.member_agent_id}
                className="flex items-center gap-[6px] text-[11.5px]"
              >
                <span className="grid size-[15px] shrink-0 place-items-center rounded-[4px] bg-[#f0f1f4] text-[9.5px] font-medium text-[#9aa1b1]">
                  {index + 1}
                </span>
                <span className="min-w-0 flex-1 truncate text-[#5b6274]">
                  {member.agent_name}
                  {member.has_own_model && (
                    <span className="ml-[4px] text-[10px] text-[#b4bac8]">自有模型</span>
                  )}
                </span>
                <span
                  className={cn(
                    'shrink-0 whitespace-nowrap text-[10px]',
                    noRule ? 'text-[#c08a3e]' : 'text-[#c3c9d6]',
                  )}
                >
                  {noRule ? '缺规则' : `${member.example_queries.length} 例`}
                </span>
              </div>
            );
          })
        )}
      </div>

      <div className="mt-[10px] flex flex-wrap items-center gap-[5px] text-[10.5px] text-[#9aa1b1]">
        <span className="rounded-[5px] bg-[#f7f8fa] px-[6px] py-[2px]">
          无法匹配：{dispatchFallbackLabel(dispatcher.config.fallback_mode)}
        </span>
        <span className="rounded-[5px] bg-[#f7f8fa] px-[6px] py-[2px]">
          {dispatcher.model_name || '未配置模型'}
        </span>
        {ownModelCount > 0 && (
          <span className="rounded-[5px] bg-[#f7f8fa] px-[6px] py-[2px]">
            {ownModelCount} 位用自有模型
          </span>
        )}
        {missingRule > 0 && (
          <span className="rounded-[5px] bg-[#fdf6ec] px-[6px] py-[2px] text-[#8a5a12]">
            {missingRule} 位缺规则
          </span>
        )}
      </div>

      {/* 底部只留一行：「去对话」占主宽度 + 「配置」固定宽。
          发布/下架与删除已收进右上角「···」下拉 —— 原先四个按钮两行排布，
          窄卡片下仍是「从广场下架」一行字占满、主次不清（真实反馈）。 */}
      <div className="mt-auto flex items-center gap-[8px] pt-[12px]">
        <button
          type="button"
          disabled={busy}
          onClick={onChat}
          className="h-[32px] min-w-0 flex-1 whitespace-nowrap rounded-[8px] bg-[#18181a] text-[12px] text-white transition-colors hover:bg-[#303030] disabled:opacity-50"
        >
          {busy ? '打开中…' : '去对话'}
        </button>
        <button
          type="button"
          onClick={onEdit}
          className="h-[32px] shrink-0 whitespace-nowrap rounded-[8px] border border-[#e3e7f1] px-[14px] text-[12px] text-[#757f9c] transition-colors hover:border-[#cbd3e6] hover:text-[#18181a]"
        >
          配置
        </button>
      </div>
    </div>
  );
}