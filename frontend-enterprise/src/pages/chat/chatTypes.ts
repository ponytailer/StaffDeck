import type { ChatAttachmentRead, ChatMessage } from '@/types';

export type SessionSlot = {
  serverMessages: ChatMessage[];
  realtimeMessages: ChatMessage[];
};

export type StreamSlot = {
  loading: boolean;
  phase: string;
  timer: number | null;
  accumulated: string;
  turnId: string | null;
  cancelledTurnId: string | null;
  abortController: AbortController | null;
  relayRecoveryStartedAt: number | null;
  relayRecoveryTurnId: string | null;
};

export type TraceSkill = {
  skillId: string;
  name?: string;
  stepId?: string;
  state?: string;
};

export type TraceTool = {
  toolId: string;
  toolCallId?: string;
  toolName: string;
  rawToolName?: string;
  success?: boolean;
  isError?: boolean;
  content?: unknown;
};

export type CotTraceIconName = 'advance' | 'execute' | 'generated' | 'judge' | 'loading' | 'select' | 'tool';

export type MCPAppViewDescriptor = {
  server_id: string;
  resource_uri: string;
  tool_name: string;
  visibility: string[];
  mime_type: string;
  tenant_id?: string | null;
  agent_id?: string | null;
  session_id?: string | null;
  active_skill_id?: string | null;
  initial_result?: unknown;
  initial_meta?: Record<string, unknown>;
};

export type TraceLine = {
  id: string;
  kind: 'thinking' | 'decision' | 'skill' | 'tool' | 'code' | 'knowledge';
  text: string;
  detail?: string;
  code?: string;
  language?: string;
  output?: string;
  outputLanguage?: string;
  outputTitle?: string;
  state: 'running' | 'completed' | 'failed';
  collapsible?: boolean;
  icon?: CotTraceIconName;
  placeholder?: boolean;
  provisional?: boolean;
  depth?: number;
  mcpApp?: MCPAppViewDescriptor;
  /** 步骤耗时（毫秒）。历史轮次来自后端事件投影，实时轮次由前端计时。 */
  durationMs?: number;
  /** 前端实时计时起点（毫秒时间戳）。 */
  startedAt?: number;
  /** 前端实时计时终点（毫秒时间戳）。 */
  completedAt?: number;
};

export type TurnTrace = {
  lines: TraceLine[];
  startedAt: number;
  completedAt?: number;
  /** 整轮耗时（毫秒）。历史轮次来自后端事件投影。 */
  durationMs?: number;
};

export type ComposerAttachment = ChatAttachmentRead & {
  uploadStatus: 'uploading' | 'ready' | 'error';
  uploadKey: string;
};

/**
 * A2UI：后端在「需要用户补充信息」时随助手消息下发的**表单描述**。
 * 用户提交的是结构化取值，后端直接写槽，不再让模型去解析一段自然语言。
 */
export type A2UIFieldType =
  | 'text'
  | 'number'
  | 'date'
  | 'time'
  | 'datetime'
  | 'boolean'
  | 'select'
  | 'confirm';

export type A2UIFieldOption = {
  value: string | number | boolean;
  label: string;
};

export type A2UIField = {
  name: string;
  label: string;
  type: A2UIFieldType;
  required?: boolean;
  placeholder?: string;
  options?: A2UIFieldOption[];
};

export type A2UIForm = {
  kind: 'slot_form';
  skill_id?: string;
  step_id?: string;
  step_name?: string;
  title?: string;
  submit_label?: string;
  fields: A2UIField[];
};

export const A2UI_FIELD_TYPES: ReadonlySet<string> = new Set([
  'text',
  'number',
  'date',
  'time',
  'datetime',
  'boolean',
  'select',
  'confirm',
]);


export type ComposerInteractionMode = 'normal' | 'scheduled_task';
export type DraftScheduleType = 'once' | 'daily' | 'weekly' | 'monthly';

export function createEmptySlot(): SessionSlot {
  return { serverMessages: [], realtimeMessages: [] };
}

export function createStreamSlot(): StreamSlot {
  return {
    loading: false,
    phase: '',
    timer: null,
    accumulated: '',
    turnId: null,
    cancelledTurnId: null,
    abortController: null,
    relayRecoveryStartedAt: null,
    relayRecoveryTurnId: null,
  };
}

export function createTurnTrace(): TurnTrace {
  return { lines: [], startedAt: Date.now() };
}
