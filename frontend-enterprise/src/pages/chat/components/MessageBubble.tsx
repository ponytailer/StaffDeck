import { useEffect, useState } from 'react';

import EmployeeAvatar from '@/components/EmployeeAvatar';
import StaffdeckIcon from '@/components/StaffdeckIcon';
import IconThumbUp from '@/assets/icons/thumb-up.svg?react';
import IconThumbDown from '@/assets/icons/thumb-down.svg?react';
import { employeeDisplayName } from '@/employee';
import { cn } from '@/lib/utils';
import type {
  ChatAttachmentRead,
  ChatMessage,
  HarnessWorkspaceArtifact,
  KnowledgeCitation,
  ScheduledTaskDraftRead,
  ScheduledTaskRead,
} from '@/types';

import {
  CHAT_ATTACHMENT_CARD_CLASS,
  CHAT_ATTACHMENT_COPY_CLASS,
  CHAT_ATTACHMENT_FILE_ICON_CLASS,
  CHAT_ATTACHMENT_IMG_CLASS,
  CHAT_ATTACHMENT_LIST_CLASS,
  CHAT_ATTACHMENT_META_CLASS,
  CHAT_ATTACHMENT_NAME_CLASS,
  CHAT_DISPATCH_NOTICE_CLASS,
  CHAT_DISPATCH_NOTICE_INTENT_CLASS,
  CHAT_DISPATCH_NOTICE_ROUTE_BADGE_CLASS,
  CHAT_DISPATCH_NOTICE_ROW_CLASS,
  CHAT_DISPATCH_NOTICE_TARGET_CLASS,
  CHAT_FEEDBACK_BTN_ACTIVE_CLASS,
  CHAT_FEEDBACK_BTN_CLASS,
  CHAT_FEEDBACK_BTN_DISLIKE_ACTIVE_CLASS,
  CHAT_FEEDBACK_CLASS,
  CHAT_GROUP_MESSAGE_AVATAR_CLASS,
  CHAT_GROUP_MESSAGE_CONTENT_CLASS,
  CHAT_GROUP_MESSAGE_LEADER_BADGE_CLASS,
  CHAT_GROUP_MESSAGE_ROW_CLASS,
  CHAT_GROUP_MESSAGE_SENDER_CLASS,
  CHAT_MESSAGE_ITEM_CLASS,
  CHAT_MESSAGE_MODE_CHIP_CLASS,
  CHAT_PLAIN_ANSWER_CLASS,
  CHAT_SLASH_COMMAND_MESSAGE_CLASS,
  CHAT_SLASH_COMMAND_REQUEST_CLASS,
  CHAT_QUEUED_BUBBLE_CLASS,
  CHAT_QUEUED_DELETE_BTN_CLASS,
  CHAT_QUEUED_MESSAGE_ITEM_CLASS,
  CHAT_TRACE_FLOW_TEXT_CLASS,
  CHAT_TRACE_TOTAL_DURATION_CLASS,
  chatBubbleClass,
  chatRowClass,
} from '../chatPageStyles';
import {
  MarkdownMessage,
  attachmentTypeLabel,
  canRateMessage,
  dispatchNoticeInfo,
  formatTraceDuration,
  isDispatchNotice,
} from '../chatHelpers';
import type { TraceLine } from '../chatTypes';
import type { A2UIForm } from '../chatTypes';
import type { UseChatSession } from '../useChatSession';
import ExecutionRecord from './ExecutionRecord';
import HarnessArtifactDownloads from './HarnessArtifactDownloads';
import KnowledgeCitationList from './KnowledgeCitationList';
import ScheduledDraftCard from './ScheduledDraftCard';
import SlashCommandChip from './SlashCommandChip';
import SlotFormCard from './SlotFormCard';
import { slashCommandMessage } from '../slashCommands';

export type TurnTraceTiming = {
  startedAt: number;
  completedAt?: number;
  durationMs?: number;
};

export type MessageRender = {
  traceTurnId: string;
  summary: { text: string; state: TraceLine['state'] } | null;
  details: TraceLine[];
  expanded: boolean;
  showInlineTrace: boolean;
  visibleContent: string;
  citations: KnowledgeCitation[];
  scheduledDraft: ScheduledTaskDraftRead | null;
  createdTask?: ScheduledTaskRead;
  scheduledTaskPrompt: boolean;
  attachments: ChatAttachmentRead[];
  harnessArtifacts: HarnessWorkspaceArtifact[];
  statusOnly: boolean;
  traceTiming?: TurnTraceTiming;
  /** A2UI：缺槽询问附带的表单（只有最新一条助手消息才挂，避免过期表单被重复提交）。 */
  slotForm?: A2UIForm | null;
  /** 调度入口：这条答案是哪位同事答的（`metadata.dispatch`）。 */
  dispatchInfo?: DispatchAnswerInfo | null;
};

/** 答案归属：调度入口透传回来的成员信息，用于在气泡上方标「由 小王 回答」。 */
export type DispatchAnswerInfo = {
  memberName: string;
  memberAgentId?: string;
  intent?: string | null;
  confidence?: number | null;
};

/** 从消息 metadata 里取调度归属。没有就是普通员工自己答的，不显示 chip。 */
export function dispatchAnswerInfo(item: ChatMessage): DispatchAnswerInfo | null {
  const raw = item.metadata?.dispatch;
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const record = raw as Record<string, unknown>;
  const memberName = typeof record.member_name === 'string' ? record.member_name.trim() : '';
  if (!memberName) return null;
  return {
    memberName,
    memberAgentId:
      typeof record.member_agent_id === 'string' ? record.member_agent_id : undefined,
    intent: typeof record.intent === 'string' ? record.intent : null,
    confidence: typeof record.confidence === 'number' ? record.confidence : null,
  };
}

type MessageBubbleProps = {
  chat: UseChatSession;
  item: ChatMessage;
  render: MessageRender;
};

function activeTeamProgress(item: ChatMessage): { phase: string; statusText: string } | null {
  const progress = item.metadata?.team_progress;
  if (!progress || typeof progress !== 'object' || Array.isArray(progress)) return null;
  const progressRecord = progress as Record<string, unknown>;
  const phase = typeof progressRecord.phase === 'string' ? progressRecord.phase : '';
  if (phase !== 'collecting' && phase !== 'synthesizing') return null;
  const statusText = typeof progressRecord.status_text === 'string'
    ? progressRecord.status_text.trim()
    : '';
  return statusText ? { phase, statusText } : null;
}

/** 本轮整体耗时：未完成时实时跳动，完成后定格。 */
function TurnDurationNote({ timing }: { timing: TurnTraceTiming }) {
  const running = timing.completedAt === undefined && timing.durationMs === undefined;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [running]);
  const durationMs = timing.durationMs
    ?? (timing.completedAt !== undefined
      ? Math.max(0, timing.completedAt - timing.startedAt)
      : Math.max(0, now - timing.startedAt));
  const label = formatTraceDuration(durationMs);
  if (!label) return null;
  return (
    <span
      className={cn(CHAT_TRACE_TOTAL_DURATION_CLASS, running && CHAT_TRACE_FLOW_TEXT_CLASS)}
      data-i18n-ignore
      title={running ? '本轮仍在执行，计时进行中' : '本轮执行的总耗时'}
    >
      总耗时 {label}
    </span>
  );
}

export default function MessageBubble({ chat, item, render }: MessageBubbleProps) {
  const {
    toggleTrace,
    rateMessage,
    setActiveCitation,
    confirmScheduledTask,
    dismissScheduledTaskDraft,
    removeQueuedTurn,
  } = chat;
  const {
    traceTurnId,
    summary,
    details,
    expanded,
    showInlineTrace,
    visibleContent,
    citations,
    scheduledDraft,
    createdTask,
    scheduledTaskPrompt,
    attachments,
    harnessArtifacts,
    statusOnly,
    traceTiming,
    slotForm,
    dispatchInfo,
  } = render;
  const queuedMessage = item.role === 'user' && item.metadata?.queued === true;
  // 调度入口的「转接通知」：由路由自己说「已转交给 X」，成员答案在它后面一条。
  // 它不是答案，所以不参与点赞、不显示 trace、也不走普通答案气泡的排版。
  const noticeInfo = item.role === 'assistant' && isDispatchNotice(item)
    ? dispatchNoticeInfo(item)
    : null;
  const isDispatchNoticeMessage = item.role === 'assistant' && isDispatchNotice(item);
  const sentSlashCommand = item.role === 'user'
    ? slashCommandMessage(visibleContent, chat.slashCommands)
    : null;
  const groupAssistantMessage = item.role === 'assistant' && Boolean(chat.displayedTeam);
  const groupSenderName = chat.displayedAgent
    ? employeeDisplayName(chat.displayedAgent)
    : '项目领导';
  const teamProgress = item.role === 'assistant' ? activeTeamProgress(item) : null;

  // 转接通知单独成一块：只显示「路由 → 转给了谁（+ 判定意图）」，
  // 不渲染 trace、点赞、引用卡片——它不是答案，成员答案在紧跟的下一条气泡里。
  if (isDispatchNoticeMessage) {
    return (
      <div className={CHAT_MESSAGE_ITEM_CLASS}>
        <div className={CHAT_DISPATCH_NOTICE_ROW_CLASS}>
          <div className={CHAT_DISPATCH_NOTICE_CLASS} role="status">
            <span className={CHAT_DISPATCH_NOTICE_ROUTE_BADGE_CLASS}>
              <StaffdeckIcon name="branch" size={11} />
              路由
            </span>
            <span data-i18n-ignore>
              已转交给
              <span className={CHAT_DISPATCH_NOTICE_TARGET_CLASS}>
                {noticeInfo?.memberName || '对应同事'}
              </span>
            </span>
            {noticeInfo?.intent && (
              <span className={CHAT_DISPATCH_NOTICE_INTENT_CLASS} data-i18n-ignore>
                {noticeInfo.intent}
              </span>
            )}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={cn(CHAT_MESSAGE_ITEM_CLASS, queuedMessage && CHAT_QUEUED_MESSAGE_ITEM_CLASS)}>
      <div className={cn(chatRowClass(item.role), groupAssistantMessage && CHAT_GROUP_MESSAGE_ROW_CLASS)}>
        {groupAssistantMessage && (
          <EmployeeAvatar
            agent={chat.displayedAgent}
            size={36}
            radius={10}
            className={CHAT_GROUP_MESSAGE_AVATAR_CLASS}
          />
        )}
        <div className={groupAssistantMessage ? CHAT_GROUP_MESSAGE_CONTENT_CLASS : 'contents'}>
          {groupAssistantMessage && (
            <span className={CHAT_GROUP_MESSAGE_SENDER_CLASS} data-i18n-ignore>
              {groupSenderName}
              <span className={CHAT_GROUP_MESSAGE_LEADER_BADGE_CLASS}>项目领导</span>
            </span>
          )}
          <div
            className={cn(
              chatBubbleClass(item.role, item.isError),
              queuedMessage && CHAT_QUEUED_BUBBLE_CLASS,
            )}
          >
          {queuedMessage && (
            <button
              type="button"
              className={CHAT_QUEUED_DELETE_BTN_CLASS}
              aria-label="删除排队消息"
              title="删除排队消息"
              onClick={() => removeQueuedTurn(item.turnId || '')}
            >
              <StaffdeckIcon name="trash" size={14} />
            </button>
          )}
          {/* 调度入口的答案来自某位同事：明确标出来，否则用户会以为入口自己在答。
              放在正文上方而不是下面——读者要先知道「谁在说话」。
              **同轮去重由 MessageList 负责**：一轮里已经渲染过「路由 · 已转交给 X」
              气泡时，MessageList 会把 dispatchInfo 置空、不再挂这个 chip（用户反馈：
              两者并存看起来像「有两段转接记录」）。本组件只负责渲染，不做跨组件判断。 */}
          {item.role === 'assistant' && dispatchInfo && !statusOnly && (
            <div className="mb-[7px] flex flex-wrap items-center gap-[5px] text-[11.5px]">
              <span className="inline-flex items-center gap-[4px] rounded-[6px] bg-[#e1f1ed] px-[6px] py-[2px] text-[#0f7268]">
                <svg width="10" height="10" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                  <path d="M3 8h10M9 4l4 4-4 4" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
                由 {dispatchInfo.memberName} 回答
              </span>
              {dispatchInfo.intent && (
                <span className="truncate text-[#9aa1b1]">{dispatchInfo.intent}</span>
              )}
            </div>
          )}
          {statusOnly ? (
            <div className="text-[13px] text-[#858b9c]">{visibleContent}</div>
          ) : showInlineTrace && summary ? (
            <ExecutionRecord
              traceTurnId={traceTurnId}
              summary={summary}
              details={details}
              expanded={expanded}
              onToggle={toggleTrace}
            />
          ) : null}

          {!statusOnly && visibleContent ? (
            item.role === 'assistant' ? (
              <div aria-live={teamProgress ? 'polite' : undefined} data-i18n-ignore>
                <MarkdownMessage content={visibleContent} />
                {teamProgress && (
                  <div
                    role="status"
                    aria-label={teamProgress.statusText}
                    className="mt-[8px] flex items-center gap-[7px] text-[12px] text-[#6d7791]"
                  >
                    <span className="relative flex size-[7px]">
                      <span className="absolute inline-flex size-full animate-ping rounded-full bg-[#1a71ff] opacity-40" />
                      <span className="relative inline-flex size-[7px] rounded-full bg-[#1a71ff]" />
                    </span>
                    <span>{teamProgress.statusText}</span>
                  </div>
                )}
              </div>
            ) : (
              <div className={cn(
                CHAT_PLAIN_ANSWER_CLASS,
                sentSlashCommand && CHAT_SLASH_COMMAND_MESSAGE_CLASS,
              )}
              >
                {scheduledTaskPrompt && (
                  <span className={CHAT_MESSAGE_MODE_CHIP_CLASS}>
                    <StaffdeckIcon name="clock" size={13} />
                    定时任务
                  </span>
                )}
                {sentSlashCommand ? (
                  <>
                    <SlashCommandChip command={sentSlashCommand.command} />
                    {sentSlashCommand.requestText && (
                      <span className={CHAT_SLASH_COMMAND_REQUEST_CLASS} data-i18n-ignore>
                        {sentSlashCommand.requestText}
                      </span>
                    )}
                  </>
                ) : (
                  <span data-i18n-ignore>{visibleContent}</span>
                )}
              </div>
            )
          ) : null}

          {!statusOnly && attachments.length > 0 && (
            <div className={CHAT_ATTACHMENT_LIST_CLASS}>
              {attachments.map((attachment) => (
                <div className={CHAT_ATTACHMENT_CARD_CLASS} key={attachment.id}>
                  {attachment.kind === 'image' && attachment.data_url ? (
                    <img className={CHAT_ATTACHMENT_IMG_CLASS} src={attachment.data_url} alt={attachment.filename} />
                  ) : (
                    <span className={CHAT_ATTACHMENT_FILE_ICON_CLASS}>
                      <StaffdeckIcon name={attachment.kind === 'pdf' ? 'file' : 'folder'} size={18} />
                    </span>
                  )}
                  <span className={CHAT_ATTACHMENT_COPY_CLASS}>
                    <span className={CHAT_ATTACHMENT_NAME_CLASS} data-i18n-ignore>{attachment.filename}</span>
                    <span className={CHAT_ATTACHMENT_META_CLASS} data-i18n-ignore>
                      {attachmentTypeLabel(attachment)}
                      {attachment.error ? ` · ${attachment.error}` : ''}
                    </span>
                  </span>
                </div>
              ))}
            </div>
          )}

          {item.role === 'assistant' && harnessArtifacts.length > 0 && (
            <HarnessArtifactDownloads
              artifacts={harnessArtifacts}
              tenantId={chat.tenantId}
              sessionId={chat.activeConversationId}
            />
          )}

          {item.role === 'assistant' && (
            <KnowledgeCitationList citations={citations} onOpen={setActiveCitation} />
          )}

          {slotForm && (
            <SlotFormCard
              form={slotForm}
              disabled={Boolean(teamProgress)}
              onSubmit={(values) => chat.submitSlotForm(slotForm, values)}
            />
          )}

          {scheduledDraft && (
            <ScheduledDraftCard
              draft={scheduledDraft}
              createdTask={createdTask}
              onConfirm={(nextDraft) => void confirmScheduledTask(nextDraft, item.id)}
              onDismiss={() => dismissScheduledTaskDraft(item.id)}
            />
          )}

          {canRateMessage(item) && !teamProgress && (
            <div className={CHAT_FEEDBACK_CLASS}>
              <button
                type="button"
                className={cn(CHAT_FEEDBACK_BTN_CLASS, item.feedback_rating === 'up' && CHAT_FEEDBACK_BTN_ACTIVE_CLASS)}
                aria-label="点赞"
                onClick={() => rateMessage(item, 'up')}
              >
                <IconThumbUp width={15} height={15} />
              </button>
              <button
                type="button"
                className={cn(
                  CHAT_FEEDBACK_BTN_CLASS,
                  item.feedback_rating === 'down' && CHAT_FEEDBACK_BTN_DISLIKE_ACTIVE_CLASS,
                )}
                aria-label="点踩"
                onClick={() => rateMessage(item, 'down')}
              >
                <IconThumbDown width={15} height={15} />
              </button>
              {traceTiming && <TurnDurationNote timing={traceTiming} />}
            </div>
          )}
          </div>
        </div>
      </div>
      {/* 排队态**不再显示「排队中」文案**（用户明确要求）：上一条答案还在流式
          输出时，界面本身就在动，用户知道还没轮到这条；挂一行「排队中」只会在
          队列卡住时误导用户以为在正常等待。虚线边框 + 删除按钮已经足够表达
          「这条还没发出去」。 */}
    </div>
  );
}
