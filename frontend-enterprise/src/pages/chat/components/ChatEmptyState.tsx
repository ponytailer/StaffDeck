import EmployeeAvatar from '@/components/EmployeeAvatar';
import { teamLeader } from '@/components/TeamCard';
import { employeeDisplayName } from '@/employee';

import {
  CHAT_EMPTY_CARD_CLASS,
  CHAT_EMPTY_CLASS,
  CHAT_EMPTY_GREETING_CARD_CLASS,
  CHAT_EMPTY_ROLE_CLASS,
  CHAT_EMPTY_STAT_CELL_CLASS,
  CHAT_EMPTY_SUBTITLE_CLASS,
  CHAT_EMPTY_TAGS_CLASS,
  CHAT_EMPTY_TITLE_CLASS,
} from '../chatPageStyles';
import type { UseChatSession } from '../useChatSession';

function greetingFontSize(displayName: string): number {
  const length = Array.from(displayName).length;
  return length > 20 ? 20 : length > 12 ? 24 : length > 6 ? 30 : 36;
}

export default function ChatEmptyState({ chat }: { chat: UseChatSession }) {
  if (chat.currentSession?.team_id) {
    return <TeamEmptyCard chat={chat} />;
  }
  // 多智能体不是普通员工：不套「#角色 / 资料 / 技能 / SOP」那套话术，
  // 改为展示它自己表单里的「入口说明」与「成员有谁」。
  if (chat.displayedAgentIsDispatcher) {
    return <DispatcherEmptyCard chat={chat} />;
  }
  return <EmployeeEmptyCard chat={chat} />;
}

/** 统计格里既可以放数字（同事数），也可以放短文案（兜底方式 / 模型名）。 */
function EmptyStatCell({
  label,
  value,
  weight = 1,
}: {
  label: string;
  value: string | number;
  /** 相对宽度权重。文案格（兜底方式 / 模型名）比数字格更需要横向空间。 */
  weight?: number;
}) {
  const isText = typeof value === 'string';
  return (
    // flex / padding 走行内 style：CHAT_EMPTY_STAT_CELL_CLASS 自带 flex-1 与
    // px-[20px]，再叠 flex-/px- 工具类时谁生效取决于样式表生成顺序，不可控。
    // min-w-0 必须有：flex 项默认 min-width:auto，长模型名会把相邻格压扁
    // （「无法匹配」曾被压成一字一行）。
    <div
      className={`${CHAT_EMPTY_STAT_CELL_CLASS} min-w-0`}
      style={{ flex: `${weight} 1 0%`, paddingInline: isText ? 10 : 14 }}
    >
      <span
        className={
          isText
            // 最多两行 + title 兜底：模型名（如 deepseek-v4.1-flash-1017）
            // 单行放不下，截断会把关键信息藏掉，允许折到第二行。
            ? 'line-clamp-2 block w-full text-[11.5px] font-medium leading-[1.35] text-[#18181a] [overflow-wrap:anywhere]'
            : 'block w-full text-[18px] font-medium leading-none capitalize'
        }
        // text-transform 走行内 style：外层 CHAT_EMPTY_CARD_CLASS 链上有 capitalize，
        // 模型名会被改写成 Deepseek-V4.1-Flash-1017（后端原名是小写），
        // 用 class 覆盖的优先级不可控，直接写 style 最稳。
        style={isText ? { textTransform: 'none' } : undefined}
        title={isText ? String(value) : undefined}
      >
        {value}
      </span>
      <span className="whitespace-nowrap text-[10px] leading-none">{label}</span>
    </div>
  );
}

function DispatcherEmptyCard({ chat }: { chat: UseChatSession }) {
  const {
    displayedAgent,
    displayedDispatcher,
    displayedDispatcherLoaded,
    emptyDispatcherSummary,
    emptyDispatcherStats,
    agents,
  } = chat;
  const displayName = displayedAgent ? employeeDisplayName(displayedAgent) : '';
  const members = displayedDispatcher?.members || [];
  const agentById = (agentId: string) => agents.find((agent) => agent.id === agentId) || null;
  // 头像叠放沿用团队名片的手法；成员不在当前花名册里时 EmployeeAvatar 自己回落预设头像
  const avatarMembers = members.slice(0, 3);
  // 成员名 chips：routing_rule 挂在 title 上，鼠标停一下能看到「什么情况转给他」。
  // 详情还没回来时不要谎称「没有成员」——配置好的入口一进来就会看到这句话。
  const tags = members.length
    ? members.map((member) => ({ name: member.agent_name, hint: member.routing_rule }))
    : displayedDispatcherLoaded === false
      ? [{ name: '成员加载中', hint: '' }]
      : [{ name: '还没有成员', hint: '' }];

  return (
    <div className={CHAT_EMPTY_CLASS}>
      <div className={CHAT_EMPTY_GREETING_CARD_CLASS}>
        <div className="flex min-h-[102px] w-full gap-[10px]">
          <div className="relative h-[102px] w-[136px] shrink-0 self-end">
            {avatarMembers.length > 0 ? (
              <div className="absolute inset-x-0 bottom-[14px] flex items-center justify-center">
                {avatarMembers.map((member) => (
                  <EmployeeAvatar
                    key={member.member_agent_id}
                    agent={agentById(member.member_agent_id)}
                    size={56}
                    className="-ml-[14px] shrink-0 rounded-full ring-4 ring-[#f6f6f6] first:ml-0"
                  />
                ))}
              </div>
            ) : (
              <div className="absolute inset-x-0 bottom-[14px] flex items-center justify-center">
                <span className="grid size-[56px] place-items-center rounded-full bg-[#e1f1ed] text-[#0f7268] ring-4 ring-[#f6f6f6]">
                  <svg width="24" height="24" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3">
                    <circle cx="8" cy="3.2" r="1.9" />
                    <circle cx="3.4" cy="12.4" r="1.9" />
                    <circle cx="12.6" cy="12.4" r="1.9" />
                    <path d="M7 4.8 4.2 10.9M9 4.8l2.8 6.1" />
                  </svg>
                </span>
              </div>
            )}
          </div>
          <div className="flex min-w-0 flex-1 flex-col justify-center gap-[8px] py-[12px] capitalize">
            <strong
              className={`${CHAT_EMPTY_TITLE_CLASS} max-w-full [overflow-wrap:anywhere]`}
              style={{ fontSize: `${greetingFontSize(displayName)}px` }}
              title={displayName}
            >
              Hello 我是{displayName}！
            </strong>
            <span className={CHAT_EMPTY_SUBTITLE_CLASS}>我们来做什么？</span>
          </div>
        </div>
      </div>

      <div className={CHAT_EMPTY_CARD_CLASS}>
        <div className="flex min-w-0 flex-1 flex-col justify-center gap-[8px] px-[4px]">
          <p className={CHAT_EMPTY_ROLE_CLASS} title={emptyDispatcherSummary}>
            {emptyDispatcherSummary}
          </p>
          <div className={CHAT_EMPTY_TAGS_CLASS}>
            {tags.map((tag, index) => (
              <span key={`${tag.name}-${index}`} title={tag.hint || undefined}>
                {tag.name}
              </span>
            ))}
          </div>
        </div>
        <div className="flex flex-1 items-stretch">
          {emptyDispatcherStats.map((item) => (
            <EmptyStatCell
              key={item.label}
              label={item.label}
              value={item.value}
              weight={typeof item.value === 'number' ? 0.75 : 1.35}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function EmployeeEmptyCard({ chat }: { chat: UseChatSession }) {
  const { displayedAgent, displayedProfile, emptyRoleSummary, emptyProfileTags, emptyStats } = chat;
  const displayName = displayedAgent ? employeeDisplayName(displayedAgent) : '';

  return (
    <div className={CHAT_EMPTY_CLASS}>
      <div className={CHAT_EMPTY_GREETING_CARD_CLASS}>
        <div className="flex min-h-[102px] w-full gap-[10px]">
          <div className="relative h-[102px] w-[136px] shrink-0 self-end">
            <div className="absolute bottom-0 left-0 h-[160px] w-[136px]">
            <EmployeeAvatar
              profile={displayedProfile ?? undefined}
              agent={displayedAgent ?? undefined}
              width={136}
              height={160}
              radius={0}
              fit="cover"
              objectPosition="bottom"
              className="bg-transparent!"
            />
            </div>
          </div>
          <div className="flex min-w-0 flex-1 flex-col justify-center gap-[8px] py-[12px] capitalize">
            <strong
              className={`${CHAT_EMPTY_TITLE_CLASS} max-w-full [overflow-wrap:anywhere]`}
              style={{ fontSize: `${greetingFontSize(displayName)}px` }}
              title={displayName}
            >
              Hello 我是{displayName}！
            </strong>
            <span className={CHAT_EMPTY_SUBTITLE_CLASS}>我们来做什么？</span>
          </div>
        </div>
      </div>

      <div className={CHAT_EMPTY_CARD_CLASS}>
        <div className="flex min-w-0 flex-1 flex-col justify-center gap-[8px] px-[4px]">
          <p className={CHAT_EMPTY_ROLE_CLASS}>{emptyRoleSummary}</p>
          <div className={CHAT_EMPTY_TAGS_CLASS}>
            {emptyProfileTags.map((tag, index) => (
              <span key={`${tag}-${index}`}>{tag}</span>
            ))}
          </div>
        </div>
        <div className="flex flex-1 items-stretch">
          {emptyStats.map((item) => (
            <div key={item.label} className={CHAT_EMPTY_STAT_CELL_CLASS}>
              <span className="text-[18px] font-medium leading-none">{item.value}</span>
              <span className="text-[10px] leading-none">{item.label}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function TeamEmptyCard({ chat }: { chat: UseChatSession }) {
  const { displayedTeam, currentSession, agents, teamEmptyStats } = chat;
  const members = displayedTeam?.members || [];
  const leader = displayedTeam ? teamLeader(displayedTeam) : null;
  const teamName = displayedTeam?.name || currentSession?.team_name || '';
  const agentById = (agentId: string) => agents.find((agent) => agent.id === agentId) || null;
  const summary = displayedTeam?.description?.trim()
    || `团队由 ${members.length} 名成员组成，项目领导是 ${leader?.agent_name || '未设置'}`;
  const memberTags = members.slice(0, 5).map((member) => (
    member.role === 'leader'
      ? `${member.agent_name || '未设置'} · 项目领导`
      : member.agent_name || '未设置'
  ));
  const stats = [
    { label: '成员数', value: members.length },
    { label: '任务数', value: teamEmptyStats.tasks },
    { label: '黑板条目数', value: teamEmptyStats.blackboard },
  ];

  return (
    <div className={CHAT_EMPTY_CLASS}>
      <div className={CHAT_EMPTY_GREETING_CARD_CLASS}>
        <div className="flex min-h-[102px] w-full gap-[10px]">
          <div className="relative h-[102px] w-[136px] shrink-0 self-end">
            <div className="absolute inset-x-0 bottom-[14px] flex items-center justify-center">
              {members.slice(0, 3).map((member) => (
                <EmployeeAvatar
                  key={member.id}
                  agent={agentById(member.agent_id)}
                  size={56}
                  className="-ml-[14px] shrink-0 rounded-full ring-4 ring-[#f6f6f6] first:ml-0"
                />
              ))}
            </div>
          </div>
          <div className="flex min-w-0 flex-1 flex-col justify-center gap-[8px] py-[12px] capitalize">
            <strong
              className={`${CHAT_EMPTY_TITLE_CLASS} max-w-full [overflow-wrap:anywhere]`}
              style={{ fontSize: `${greetingFontSize(teamName)}px` }}
              title={teamName}
            >
              Hello 我们是{teamName}！
            </strong>
            <span className={CHAT_EMPTY_SUBTITLE_CLASS}>我们来做什么？</span>
          </div>
        </div>
      </div>

      <div className={CHAT_EMPTY_CARD_CLASS}>
        <div className="flex min-w-0 flex-1 flex-col justify-center gap-[8px] px-[4px]">
          <p className={CHAT_EMPTY_ROLE_CLASS}>{summary}</p>
          <div className={CHAT_EMPTY_TAGS_CLASS}>
            {memberTags.map((tag, index) => (
              <span key={`${tag}-${index}`}>{tag}</span>
            ))}
          </div>
        </div>
        <div className="flex flex-1 items-stretch">
          {stats.map((item) => (
            <div key={item.label} className={CHAT_EMPTY_STAT_CELL_CLASS}>
              <span className="text-[18px] font-medium leading-none">{item.value}</span>
              <span className="text-[10px] leading-none">{item.label}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
