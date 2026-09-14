export const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';
export const SELECTED_AGENT_STORAGE_KEY = ENTERPRISE_AGENT_STORAGE_KEY;
export const SESSION_FILTER_STORAGE_PREFIX = 'skill_agent_session_filter';

export function sessionFilterStorageKey(userId: string): string {
  return `${SESSION_FILTER_STORAGE_PREFIX}:${userId || 'anonymous'}`;
}

export function persistSharedAgentScope(agentId: string, userId?: string): void {
  void userId;
  if (!agentId) return;
  window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, agentId);
}

export function clearSharedAgentScope(userId?: string): void {
  void userId;
  window.localStorage.removeItem(ENTERPRISE_AGENT_STORAGE_KEY);
}

// Team scopes share the same storage slot as employee agent ids, prefixed so
// readers can tell "current team" apart from "current employee".
export const TEAM_SCOPE_PREFIX = 'team:';

export function toTeamScope(teamId: string): string {
  return teamId ? `${TEAM_SCOPE_PREFIX}${teamId}` : '';
}

export function isTeamScope(value: string | null | undefined): boolean {
  return typeof value === 'string'
    && value.startsWith(TEAM_SCOPE_PREFIX)
    && value.length > TEAM_SCOPE_PREFIX.length;
}

export function teamIdFromScope(value: string | null | undefined): string {
  return isTeamScope(value) ? String(value).slice(TEAM_SCOPE_PREFIX.length) : '';
}

/** 读取共享作用域；团队作用域对员工向页面视为"未选员工"，返回空串。 */
export function readEmployeeScope(): string {
  const raw = window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '';
  return isTeamScope(raw) ? '' : raw;
}

export function emitAgentScopeChange(agentId: string): void {
  window.dispatchEvent(
    new CustomEvent('ultrarag-enterprise-agent-scope-change', {
      detail: { agentId },
    }),
  );
}

/**
 * 员工花名册变更广播。
 *
 * 「谁改了花名册」和「谁要重新拉列表」是两拨组件：创建 / 编辑 / 上下线 / 删除
 * 的入口分散在 App 侧边栏弹窗、员工页、员工广场里，而列表数据是各页各自
 * `GET /api/enterprise/agents` 拉的。任何一处写入成功后都必须广播一次，否则
 * 已经挂载的其它页面会一直拿着 mount 时的旧快照（表现为「新建后看不到，手动
 * 刷新页面才出现」）。
 *
 * 与 `ultrarag-enterprise-agent-scope-change`（切换当前作用域）区分：那个是
 * 「选中的是谁变了」，这个是「名单本身变了」。
 */
export const AGENT_ROSTER_REFRESH_EVENT = 'ultrarag-enterprise-agent-scope-refresh';

/** 广播「员工花名册已变更」；写入型操作成功后调用。 */
export function emitAgentRosterRefresh(): void {
  window.dispatchEvent(new Event(AGENT_ROSTER_REFRESH_EVENT));
}

/** 订阅花名册变更，返回取消订阅函数（直接用作 useEffect 的清理函数）。 */
export function onAgentRosterRefresh(handler: () => void): () => void {
  window.addEventListener(AGENT_ROSTER_REFRESH_EVENT, handler);
  return () => window.removeEventListener(AGENT_ROSTER_REFRESH_EVENT, handler);
}
