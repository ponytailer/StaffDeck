/** StaffDeck API 客户端：契约与 frontend-enterprise/src/api/client.ts 保持一致。
 *
 * 网络层走 background service worker 代理（MV3 content script 与页面同源，
 * 跨源请求会被页面 CORS 卡死；SW 凭 host_permissions 豁免）。
 */
import { storage } from './storage';
import { proxyFetch } from './proxy';
import type { AgentProfileRead, LoginResponse, StreamEvent } from '../types';

export class ApiError extends Error {
  status: number;
  code: string;
  detail: string;

  constructor(status: number, code: string, detail: string) {
    super(detail || code || `HTTP ${status}`);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

export const FREE_MODEL_QUOTA_EXHAUSTED_CODE = 'free_model_quota_exhausted';

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/** 从 FastAPI 错误响应中解析 {detail: "..."} 或 {detail: {code, message}} */
function parseErrorPayload(status: number, text: string): ApiError {
  let code = '';
  let detail = '';
  try {
    const parsed: unknown = JSON.parse(text);
    if (isRecord(parsed)) {
      const d = parsed.detail;
      if (typeof d === 'string') {
        detail = d;
      } else if (Array.isArray(d)) {
        // FastAPI 422 校验错误：detail 是 [{loc, msg, type}, ...]
        code = 'validation_error';
        detail = d
          .map((item) => (isRecord(item) && typeof item.msg === 'string' ? item.msg : ''))
          .filter(Boolean)
          .join('; ') || '请求参数校验失败';
      } else if (isRecord(d)) {
        code = typeof d.code === 'string' ? d.code : '';
        detail = typeof d.message === 'string' ? d.message : '';
      }
      if (!code && typeof parsed.code === 'string') code = parsed.code;
      if (!detail && typeof parsed.message === 'string') detail = parsed.message;
    }
  } catch {
    detail = text.slice(0, 300);
  }
  return new ApiError(status, code, detail);
}

async function authHeader(): Promise<Record<string, string>> {
  const token = await storage.getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

type FetchOptions = { signal?: AbortSignal };

async function request<T>(path: string, init: RequestInit = {}, opts: FetchOptions = {}): Promise<T> {
  const apiBase = await storage.getApiBase();
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((init.headers as Record<string, string> | undefined) ?? {}),
    ...(await authHeader()),
  };
  let result: Awaited<ReturnType<typeof proxyFetch>>;
  try {
    result = await proxyFetch(`${apiBase}${path}`, {
      method: init.method || 'GET',
      headers,
      body: typeof init.body === 'string' ? init.body : undefined,
      signal: opts.signal,
    });
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e;
    throw new ApiError(0, 'network_error', '无法连接 AIStaff 服务');
  }
  if (!result.ok) {
    const err = parseErrorPayload(result.status, result.body);
    if (result.status === 401) err.code = err.code || 'unauthorized';
    throw err;
  }
  if (result.status === 204) return undefined as T;
  try {
    return JSON.parse(result.body) as T;
  } catch {
    throw new ApiError(result.status, 'bad_response', '服务响应格式异常');
  }
}

/* ---------------- Auth ---------------- */

export async function login(params: {
  tenantId: string;
  username: string;
  password: string;
  remember: boolean;
}): Promise<LoginResponse> {
  const apiBase = await storage.getApiBase();
  let result: Awaited<ReturnType<typeof proxyFetch>>;
  try {
    result = await proxyFetch(`${apiBase}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_id: params.tenantId,
        username: params.username,
        password: params.password,
        remember: params.remember,
      }),
    });
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e;
    throw new ApiError(0, 'network_error', '无法连接 AIStaff 服务');
  }
  if (!result.ok) {
    throw parseErrorPayload(result.status, result.body);
  }
  try {
    return JSON.parse(result.body) as LoginResponse;
  } catch {
    throw new ApiError(result.status, 'bad_response', '服务响应格式异常');
  }
}

export async function fetchMe(): Promise<import('../types').UserRead> {
  return request('/api/auth/me');
}

async function tenantId(): Promise<string> {
  return storage.getTenantId();
}

/* ---------------- Agents ---------------- */

export async function fetchChatAgents(): Promise<AgentProfileRead[]> {
  const list = await request<AgentProfileRead[]>(
    `/api/chat/agents?tenant_id=${encodeURIComponent(await tenantId())}`,
  );
  return Array.isArray(list) ? list : [];
}

export async function useAgent(agentId: string): Promise<void> {
  try {
    await request(
      `/api/chat/agents/${encodeURIComponent(agentId)}/use?tenant_id=${encodeURIComponent(await tenantId())}`,
      { method: 'POST' },
    );
  } catch {
    // 用量计数失败不阻塞对话
  }
}

export async function cancelSession(sessionId: string, turnId: string): Promise<void> {
  try {
    await request(`/api/chat/sessions/${encodeURIComponent(sessionId)}/cancel`, {
      method: 'POST',
      body: JSON.stringify({ tenant_id: await tenantId(), turn_id: turnId }),
    });
  } catch {
    // 会话可能已结束
  }
}

/* ---------------- Chat SSE ---------------- */

const TERMINAL_EVENTS = new Set([
  'complete',
  'done',
  'stream_end',
  'stream_cancelled',
  'stream_interrupted',
  'error',
  'error_occurred',
]);

function parseSseBlock(block: string): StreamEvent | null {
  const lines = block.split('\n').map((line) => line.trimEnd());
  const eventLine = lines.find((line) => line.startsWith('event:'));
  const dataLines = lines.filter((line) => line.startsWith('data:'));
  if (!eventLine || dataLines.length === 0) return null;
  const event = eventLine.replace(/^event:\s*/, '');
  const rawData = dataLines.map((line) => line.replace(/^data:\s*/, '')).join('\n');
  try {
    return { event, data: JSON.parse(rawData) as Record<string, unknown> };
  } catch {
    return { event, data: { raw: rawData } };
  }
}

export type ChatStreamHandlers = {
  onSessionCreated: (sessionId: string) => void;
  onDelta: (piece: string) => void;
  onReplace: (content: string) => void;
  onTerminal: (info: { ok: boolean; error?: ApiError }) => void;
  onDispatchNotice?: (info: { memberName: string; intent: string }) => void;
};

/** POST /api/chat/stream（SSE）。协议与 frontend useChatSession 对齐。 */
export async function streamChatTurn(
  body: {
    agentId: string;
    sessionId: string | null;
    message: string;
    clientTurnId: string;
  },
  handlers: ChatStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const apiBase = await storage.getApiBase();
  const tenantId = await storage.getTenantId();
  let buffer = '';
  let response: Awaited<ReturnType<typeof proxyFetch>>;
  try {
    response = await proxyFetch(`${apiBase}/api/chat/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(await authHeader()) },
      body: JSON.stringify({
        tenant_id: tenantId,
        session_id: body.sessionId,
        agent_id: body.agentId,
        message: body.message,
        client_turn_id: body.clientTurnId,
        channel: 'extension',
        client_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || undefined,
      }),
      signal,
      onChunk: (chunk) => {
        buffer += chunk;
        const blocks = buffer.split('\n\n');
        buffer = blocks.pop() || '';
        blocks.forEach((block) => {
          const parsed = parseSseBlock(block);
          if (parsed) handleEvent(parsed);
        });
      },
    });
  } catch (e) {
    if ((e as Error).name === 'AbortError') return;
    throw new ApiError(0, 'network_error', '无法连接 AIStaff 服务');
  }

  if (!response.ok) {
    throw parseErrorPayload(response.status, response.body);
  }

  const handleEvent = (item: StreamEvent) => {
    if (item.event === 'session_created') {
      const sid = String(item.data.newSessionId ?? item.data.sessionId ?? '');
      if (sid) handlers.onSessionCreated(sid);
      return;
    }
    if (item.event === 'heartbeat' || item.event === 'user_message_received') return;
    if (item.event === 'dispatch_notice') {
      // 小队调度转接：路由说完「已转交给 X」，成员答案随后才到
      const memberName = typeof item.data.member_name === 'string' ? item.data.member_name : '';
      const intent = typeof item.data.intent === 'string' ? item.data.intent : '';
      if (memberName) handlers.onDispatchNotice?.({ memberName, intent });
      return;
    }
    if (item.event === 'stream_replace') {
      const content = typeof item.data.content === 'string' ? item.data.content : '';
      if (content) handlers.onReplace(content);
      return;
    }
    if (item.event === 'stream_delta' || item.event === 'token') {
      const piece = typeof item.data.content === 'string' ? item.data.content : '';
      if (piece) handlers.onDelta(piece);
      return;
    }
    if (item.event === 'assistant_message_created') {
      const reply = typeof item.data.reply === 'string' ? item.data.reply : '';
      if (reply) handlers.onReplace(reply);
      handlers.onTerminal({ ok: true });
      return;
    }
    if (TERMINAL_EVENTS.has(item.event)) {
      if (item.event === 'error' || item.event === 'error_occurred') {
        const code = typeof item.data.code === 'string' ? item.data.code : '';
        const detail =
          typeof item.data.detail === 'string'
            ? item.data.detail
            : typeof item.data.message === 'string'
              ? item.data.message
              : '回复中断，请重试';
        handlers.onTerminal({ ok: false, error: new ApiError(0, code, detail) });
      } else {
        handlers.onTerminal({ ok: true });
      }
    }
  };

  // 流已读完（onChunk 期间已实时解析），flush 残余的不完整块
  const tail = parseSseBlock(buffer);
  if (tail) handleEvent(tail);
}
