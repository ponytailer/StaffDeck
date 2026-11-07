/**
 * 「当前作用域是员工小队吗」的同步判定。
 *
 * 为什么需要它：员工小队（dispatcher）不在 `GET /api/enterprise/agents` 里
 * （后端按设计排除），前端只能从 `/api/enterprise/dispatchers` 单独拿。
 * 而这条请求是**异步**的，并且要等远程 PG 往返（冷启动实测 3~8s）。
 *
 * 于是所有「见 id 就回退到某个员工」的兜底（知识库作用域解析、档案页兜底、
 * 路由守卫）在冷启动窗口里都会判错，造成真实事故：
 * - 员工小队作用域下打开 `/enterprise/knowledge/new`，作用域被**改写成某个员工 id**；
 * - 员工小队作用域下打开档案页，先闪出**别人的档案**再切成小队；
 * - 侧栏先显示员工导航（员工档案/记忆/员工能力）再切换。
 *
 * 修法：把「已知的 dispatcher id 集合」在拉到列表时**同步缓存**到 localStorage，
 * 任何页面（包括首次渲染）都能立刻判断，不必等接口。
 *
 * 过期风险可接受：缓存只用于「别把它当员工」，每次 App 加载都会用最新列表覆盖；
 * 小队被删后这个 id 已不存在，最坏只是多拦一次重定向，下一次加载即自动纠正。
 */

const STORAGE_KEY = 'ultrarag_dispatcher_scope_ids';

let cachedIds: Set<string> | null = null;

function readStoredIds(): Set<string> {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed)
      ? new Set(parsed.filter((item): item is string => typeof item === 'string'))
      : new Set();
  } catch {
    return new Set();
  }
}

/** 拉到 dispatcher 列表后调用（App 每次加载都会走一次）。 */
export function rememberDispatcherScopeIds(ids: string[]): void {
  cachedIds = new Set(ids);
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(ids));
  } catch {
    // localStorage 写满/被禁：内存缓存仍然有效，本次会话不受影响
  }
}

/** 同步判断某个 agent id 是不是员工小队。dispatchers 未加载时回退到上次已知集合。 */
export function isDispatcherScopeId(agentId?: string | null): boolean {
  if (!agentId) return false;
  if (!cachedIds) cachedIds = readStoredIds();
  return cachedIds.has(agentId);
}

/** 仅测试用：清掉内存缓存，模拟冷启动。 */
export function resetDispatcherScopeCacheForTests(): void {
  cachedIds = null;
}