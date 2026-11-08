/**
 * 多智能体（调度入口 Dispatcher）的类型与 API 封装。
 *
 * 后端口径（docs/agent-dispatcher-plan.md）：
 * - 成员数 2~3，每人必须写「什么情况转给他」的路由规则；
 * - **模型必填**（创建时就要求），成员自己配了模型走自己的，没配才用入口的；
 * - 不配知识库/技能/工具，纯路由。
 */

import { api, TENANT_ID } from '../api/client';

/** 成员数量上下限。改这里必须同步后端 app/dispatch/schema.py 与配置页 demo。 */
export const MAX_DISPATCH_MEMBERS = 3;
export const MIN_DISPATCH_MEMBERS = 2;

export type DispatchFallbackMode = 'clarify' | 'reply' | 'default_member';

export type DispatcherMemberInput = {
  member_agent_id: string;
  /** 「什么情况转给他 / 不要转给他」，路由准确率的主要来源 */
  routing_rule: string;
  example_queries: string[];
  sort_order?: number;
};

export type DispatcherMemberRead = DispatcherMemberInput & {
  sort_order: number;
  status: string;
  agent_name: string;
  agent_description?: string | null;
  agent_status: string;
  free_model_enabled: boolean;
  /** 该成员是否配了自己的模型（决定这一轮用谁的模型/谁付费） */
  has_own_model: boolean;
};

export type DispatchConfig = {
  fallback_mode: DispatchFallbackMode;
  fallback_reply: string;
  default_member_agent_id?: string | null;
  offer_candidates: boolean;
  min_confidence: number;
};

export type DispatcherRead = {
  id: string;
  tenant_id: string;
  name: string;
  description?: string | null;
  persona_prompt?: string | null;
  kind: string;
  status: string;
  model_config_id?: string | null;
  model_name?: string | null;
  members: DispatcherMemberRead[];
  config: DispatchConfig;
  usage_count: number;
  /**
   * 是否已发布到开放广场「员工小队」。
   * 与普通员工同口径（AgentProfile.metadata_json.published_to_gallery）：
   * 发布 = 对同租户其他人可见，不动成员、不动模型。
   */
  published_to_gallery: boolean;
  /** 头像预设 key（DISPATCHER_AVATAR_PRESETS）；缺省时前端用默认图形。 */
  avatar_preset?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type DispatcherCreatePayload = {
  tenant_id?: string;
  name: string;
  description?: string | null;
  persona_prompt?: string | null;
  model_config_id: string;
  members: DispatcherMemberInput[];
  config?: Partial<DispatchConfig>;
  avatar_preset?: string | null;
};

export type DispatcherUpdatePayload = Partial<DispatcherCreatePayload>;

/**
 * 「无法匹配时怎么办」的展示文案。
 *
 * `full` 用于配置页/卡片这种有富余宽度的地方，`short` 用于对话端空状态里
 * 只有一格宽的统计位（超过 4 个字就会被挤成两行）。文案统一收在这里，
 * 别在页面里各写一套 —— 之前 DispatcherCard 自己维护过一份，改口径时漏了。
 */
const FALLBACK_LABELS: Record<string, { full: string; short: string }> = {
  clarify: { full: '反问 + 列候选', short: '反问' },
  reply: { full: '固定话术', short: '固定话术' },
  default_member: { full: '转默认同事', short: '默认同事' },
};

export function dispatchFallbackLabel(mode?: string | null): string {
  if (!mode) return '未设置';
  return FALLBACK_LABELS[mode]?.full || mode;
}

export function dispatchFallbackShortLabel(mode?: string | null): string {
  if (!mode) return '未设置';
  return FALLBACK_LABELS[mode]?.short || mode;
}

const BASE = '/api/enterprise/dispatchers';

/** 单个小队的转接活动汇总（后端口径见 summarize_dispatch_activity）。 */
export type DispatcherActivityRead = {
  id: string;
  /** 该小队的父会话数 */
  sessions: number;
  /** 近 7 天有更新的父会话数（转接记录本身没有时间戳，只能按会话近似） */
  sessions_7d: number;
  /**
   * 各父会话 dispatch_log 条数之和。
   * **每个会话最多记 10 条**（后端 DISPATCH_LOG_LIMIT），所以它是下限而不是精确总次数。
   */
  transfers: number;
  top_members: { name: string; count: number }[];
};

/** 「我的小队」页首屏概览：统计卡 + 配置健康度 + 每队活动。 */
export type DispatcherOverviewRead = {
  squads: number;
  /** 去重后的成员数（同一位同事被两个小队引用只算一次） */
  members: number;
  transfers: number;
  published: number;
  missing_rule_members: number;
  /** 绑了模型但解析不出名字 = 那条 ModelConfig 已被删除或停用 */
  broken_models: number;
  single_member_squads: number;
  activity: DispatcherActivityRead[];
};

export async function fetchDispatcherOverview(
  tenantId: string = TENANT_ID,
): Promise<DispatcherOverviewRead> {
  return api.get<DispatcherOverviewRead>(
    `${BASE}/overview?tenant_id=${encodeURIComponent(tenantId)}`,
  );
}

export async function listDispatchers(
  tenantId: string = TENANT_ID,
  options?: { galleryOnly?: boolean },
): Promise<DispatcherRead[]> {
  const params = new URLSearchParams({ tenant_id: tenantId });
  // 广场「员工小队」tab 用这个：只回已发布的，且不按归属过滤（广场本来就能看别人的）
  if (options?.galleryOnly) {
    params.set('gallery_only', 'true');
  }
  return api.get<DispatcherRead[]>(`${BASE}?${params.toString()}`);
}

/** 发布到开放广场「员工小队」。已发布时幂等（不覆盖首次上架时间）。 */
export async function publishDispatcher(agentId: string): Promise<DispatcherRead> {
  return api.post<DispatcherRead>(
    `${BASE}/${agentId}/gallery:publish?tenant_id=${encodeURIComponent(TENANT_ID)}`,
    {},
  );
}

/** 从开放广场下架。只摘发布标记，入口本体/成员/会话全部保留。 */
export async function unpublishDispatcher(agentId: string): Promise<DispatcherRead> {
  return api.post<DispatcherRead>(
    `${BASE}/${agentId}/gallery:unpublish?tenant_id=${encodeURIComponent(TENANT_ID)}`,
    {},
  );
}

export async function getDispatcher(
  agentId: string,
  tenantId: string = TENANT_ID,
): Promise<DispatcherRead> {
  return api.get<DispatcherRead>(
    `${BASE}/${agentId}?tenant_id=${encodeURIComponent(tenantId)}`,
  );
}

export async function createDispatcher(
  payload: DispatcherCreatePayload,
): Promise<DispatcherRead> {
  return api.post<DispatcherRead>(BASE, {
    tenant_id: TENANT_ID,
    ...payload,
  });
}

export async function updateDispatcher(
  agentId: string,
  payload: DispatcherUpdatePayload,
): Promise<DispatcherRead> {
  return api.put<DispatcherRead>(`${BASE}/${agentId}`, {
    tenant_id: TENANT_ID,
    ...payload,
  });
}

export async function deleteDispatcher(
  agentId: string,
  tenantId: string = TENANT_ID,
): Promise<void> {
  await api.delete(`${BASE}/${agentId}?tenant_id=${encodeURIComponent(tenantId)}`);
}

export type DispatcherReference = { id: string; name: string };

/** 「试跑规则」的请求/响应：不落库，用草稿成员跑一次真实路由。 */
export type DispatchPreviewPayload = {
  tenant_id?: string;
  message: string;
  members: DispatcherMemberInput[];
  config?: Partial<DispatchConfig>;
  model_config_id?: string | null;
  dispatcher_id?: string | null;
};

export type DispatchRouteDecision = {
  action: 'route' | 'clarify' | 'reply';
  member_agent_id?: string | null;
  confidence: number;
  intent?: string | null;
  query?: string | null;
  reason?: string | null;
  reply?: string | null;
  fast_path?: boolean;
};

export type DispatchPreviewResponse = {
  decision: DispatchRouteDecision;
  member_name?: string | null;
  member_description?: string | null;
  candidates: { agent_id: string; name: string; description: string }[];
};

export async function previewRoute(
  payload: DispatchPreviewPayload,
): Promise<DispatchPreviewResponse> {
  return api.post<DispatchPreviewResponse>(`${BASE}/preview`, {
    tenant_id: TENANT_ID,
    ...payload,
  });
}

export async function listDispatcherReferences(
  agentId: string,
  tenantId: string = TENANT_ID,
): Promise<{ count: number; dispatchers: DispatcherReference[] }> {
  return api.get<{ count: number; dispatchers: DispatcherReference[] }>(
    `${BASE}/${agentId}/referenced-by?tenant_id=${encodeURIComponent(tenantId)}`,
  );
}

/** 成员数量是否合法（保存/试跑前统一用这个判，避免前后端两套口径）。 */
export function isMemberCountValid(count: number): boolean {
  return count >= MIN_DISPATCH_MEMBERS && count <= MAX_DISPATCH_MEMBERS;
}

/**
 * 这个入口有几项**配置问题**（真的会出问题的那几类，不含「未发布到广场」）。
 *
 * 与后端 `/dispatchers/overview` 的健康度口径一致，但这里按「单个入口」算，
 * 用于左列表打点与「只看需要处理的」筛选 —— 概览接口只回全局数字，
 * 不为了筛选再跑一次接口。
 */
export function dispatcherIssueCount(row: DispatcherRead): number {
  const missingRule = row.members.filter((member) => !member.routing_rule.trim()).length;
  const brokenModel = row.model_config_id && !row.model_name ? 1 : 0;
  const singleMember = row.members.length <= 1 ? 1 : 0;
  return missingRule + brokenModel + singleMember;
}

export function memberCountHint(count: number): string | null {
  if (count <= 0) {
    return `至少要有 ${MIN_DISPATCH_MEMBERS} 个同事才需要员工小队。只有一位同事时，直接找他聊就行。`;
  }
  if (count < MIN_DISPATCH_MEMBERS) {
    return `还差 ${MIN_DISPATCH_MEMBERS - count} 个同事。只有 1 个成员时没有「选谁」的判断，直接建普通员工更合适。`;
  }
  if (count > MAX_DISPATCH_MEMBERS) {
    return `最多 ${MAX_DISPATCH_MEMBERS} 个同事，当前 ${count} 个。`;
  }
  return null;
}