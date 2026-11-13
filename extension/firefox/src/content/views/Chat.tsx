import { Fragment, useEffect, useRef, useState } from 'react';
import {
  ApiError,
  FREE_MODEL_QUOTA_EXHAUSTED_CODE,
  cancelSession,
  streamChatTurn,
  useAgent,
} from '../../lib/api';
import { storage } from '../../lib/storage';
import type { AgentProfileRead } from '../../types';

type ChatMessage = {
  id: string;
  role: 'me' | 'ai';
  content: string;
  streaming?: boolean;
  /** 小队调度转接通知：独立旁白行，不是答案 */
  kind?: 'text' | 'notice';
  notice?: { memberName: string; intent: string };
};

function newId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `t-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/** 把文本按 http/https 链接切分；URL 只匹配 http(s) 前缀，剥掉尾部标点避免误入链接 */
const URL_RE = /https?:\/\/[^\s<>"']+/g;
const TRAILING_PUNCT_RE = /[)\]},.;:!?、。，；：！？）」』】》”’"']+$/;

/** markdown 图片 `![说明](地址)`；地址部分不允许空白，避免把后面的正文吞进 URL */
const IMAGE_MARKDOWN_RE = /!\[([^\]]*)\]\(\s*([^)\s]+)\s*\)/g;
/** 正常图片标记不会超过这个长度；超了还不见 `)`，就不是图片，按普通文本处理 */
const MAX_PENDING_IMAGE_CHARS = 300;

function splitLinks(text: string): Array<{ type: 'text' | 'link'; value: string }> {
  const parts: Array<{ type: 'text' | 'link'; value: string }> = [];
  let last = 0;
  for (const m of text.matchAll(URL_RE)) {
    const idx = m.index ?? 0;
    if (idx > last) parts.push({ type: 'text', value: text.slice(last, idx) });
    let url = m[0];
    const trailing = url.match(TRAILING_PUNCT_RE);
    if (trailing) url = url.slice(0, url.length - trailing[0].length);
    parts.push({ type: 'link', value: url });
    if (url.length < m[0].length) parts.push({ type: 'text', value: m[0].slice(url.length) });
    last = idx + m[0].length;
  }
  if (last < text.length) parts.push({ type: 'text', value: text.slice(last) });
  return parts;
}

/**
 * 把 markdown 图片地址解成可用的绝对地址。
 *
 * 面板是注入到宿主页面（OA 等）里的，`<img src="/api/...">` 会被解析成宿主域名，
 * 直接 404，所以相对地址必须补 API 基地址。第三方绝对地址一律拒绝：回答正文里的
 * 外链图会被浏览器直接请求，既是 IP/UA 泄露，也能当追踪像素用。
 */
function normalizeImageUrl(raw: string, apiBase: string): string | null {
  const value = raw.trim();
  if (!value || value.startsWith('//')) return null;
  if (value.startsWith('/')) {
    return apiBase ? `${apiBase}${value}` : null;
  }
  if (!/^https?:\/\//i.test(value) || !apiBase) return null;
  try {
    return new URL(value).origin === new URL(apiBase).origin ? value : null;
  } catch {
    return null;
  }
}

/**
 * 流式下发时末尾可能是刚写到一半的图片标记（`![图例](/api/knowledge/assets/ka`），
 * 不压住就会先闪一段 markdown 源码。等右括号到了再由下一片补全。
 */
function trimPendingImage(text: string): string {
  const start = text.lastIndexOf('![');
  if (start < 0) return text;
  const tail = text.slice(start);
  if (tail.includes(')') || tail.length > MAX_PENDING_IMAGE_CHARS) return text;
  return text.slice(0, start);
}

type BubbleSegment =
  | { type: 'text'; value: string }
  | { type: 'image'; src: string; alt: string };

function splitImages(text: string, apiBase: string): BubbleSegment[] {
  const segments: BubbleSegment[] = [];
  let last = 0;
  for (const match of text.matchAll(IMAGE_MARKDOWN_RE)) {
    const index = match.index ?? 0;
    if (index > last) segments.push({ type: 'text', value: text.slice(last, index) });
    const src = normalizeImageUrl(match[2], apiBase);
    // 解析不出来就原样保留 markdown：宁可难看，也别把用户该看到的内容吞掉
    segments.push(src ? { type: 'image', src, alt: match[1] || '' } : { type: 'text', value: match[0] });
    last = index + match[0].length;
  }
  if (last < text.length) segments.push({ type: 'text', value: text.slice(last) });
  return segments;
}

function MsgImage({ src, alt }: { src: string; alt: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <span className="sd-msg-image-failed">{alt ? `[图片加载失败：${alt}]` : '[图片加载失败]'}</span>
    );
  }
  return (
    <a
      className="sd-msg-image-link"
      href={src}
      target="_blank"
      rel="noopener noreferrer"
      title="点击查看大图"
    >
      <img
        className="sd-msg-image"
        src={src}
        alt={alt}
        loading="lazy"
        onError={() => setFailed(true)}
      />
    </a>
  );
}

function renderLinks(text: string) {
  return splitLinks(text).map((p, i) =>
    p.type === 'text' ? (
      <Fragment key={i}>{p.value}</Fragment>
    ) : (
      <a key={i} className="sd-link" href={p.value} target="_blank" rel="noopener noreferrer">
        {p.value}
      </a>
    ),
  );
}

function renderBubbleContent(text: string, streaming: boolean, apiBase: string) {
  const body = streaming ? trimPendingImage(text) : text;
  return (
    <>
      {splitImages(body, apiBase).map((segment, i) =>
        segment.type === 'image' ? (
          <MsgImage key={i} src={segment.src} alt={segment.alt} />
        ) : (
          <Fragment key={i}>{renderLinks(segment.value)}</Fragment>
        ),
      )}
      {streaming && <span className="sd-typing-cursor" />}
    </>
  );
}

export function ChatView(props: {
  agent: AgentProfileRead;
  getSessionId: (agentId: string) => string | null;
  saveSessionId: (agentId: string, sessionId: string) => void;
  onBack: () => void;
  onCollapse: () => void;
  onAuthExpired: () => void;
}) {
  const { agent } = props;
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ text: string; quota: boolean } | null>(null);
  /** 回答里的配图是 API 侧资源，注入到宿主页面后必须用绝对地址（见 normalizeImageUrl） */
  const [apiBase, setApiBase] = useState('');
  const abortRef = useRef<AbortController | null>(null);
  const sessionIdRef = useRef<string | null>(props.getSessionId(agent.id));
  const activeTurnIdRef = useRef<string | null>(null);
  const turnNoticesRef = useRef<Set<string>>(new Set());
  const scrollRef = useRef<HTMLDivElement>(null);

  const { tone, glyph } = (() => {
    let hash = 0;
    for (const ch of agent.name) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
    return { tone: ['t1', 't2', 't3'][hash % 3], glyph: agent.name.trim().slice(0, 1) || 'SD' };
  })();

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages]);

  useEffect(() => {
    let alive = true;
    void storage
      .getApiBase()
      .then((value) => {
        if (alive) setApiBase(value.replace(/\/+$/, ''));
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  const statusText = agent.free_model_enabled
    ? `在线 · 免费${typeof agent.free_model_remaining === 'number' ? ` · 今日剩 ${agent.free_model_remaining} 问` : ''}`
    : agent.kind === 'dispatcher'
      ? '在线 · 小队协同'
      : '在线';

  const stop = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    if (sessionIdRef.current && activeTurnIdRef.current) {
      void cancelSession(sessionIdRef.current, activeTurnIdRef.current);
    }
    activeTurnIdRef.current = null;
    setMessages((prev) =>
      prev.map((m) => (m.streaming ? { ...m, streaming: false, content: m.content || '（已停止）' } : m)),
    );
    setBusy(false);
  };

  const send = async (raw?: string) => {
    const text = (raw ?? input).trim();
    if (!text || busy) return;
    setError(null);
    setInput('');
    setBusy(true);

    const aiId = newId();
    setMessages((prev) => [
      ...prev,
      { id: newId(), role: 'me', content: text },
      { id: aiId, role: 'ai', content: '', streaming: true },
    ]);

    const controller = new AbortController();
    abortRef.current = controller;
    const clientTurnId = newId();
    activeTurnIdRef.current = clientTurnId;
    turnNoticesRef.current = new Set();

    const patchAi = (fn: (content: string) => string) => {
      setMessages((prev) => prev.map((m) => (m.id === aiId ? { ...m, content: fn(m.content) } : m)));
    };

    try {
      await streamChatTurn(
        {
          agentId: agent.id,
          sessionId: sessionIdRef.current,
          message: text,
          clientTurnId,
        },
        {
          onSessionCreated: (sid) => {
            sessionIdRef.current = sid;
            props.saveSessionId(agent.id, sid);
          },
          onDelta: (piece) => patchAi((prev) => prev + piece),
          onReplace: (content) => patchAi(() => content),
          onDispatchNotice: ({ memberName, intent }) => {
            // 转接通知独立成行，插在流式答案气泡之前；同一轮同一成员只插一次
            if (turnNoticesRef.current.has(memberName)) return;
            turnNoticesRef.current.add(memberName);
            setMessages((prev) => {
              const aiIdx = prev.findIndex((m) => m.id === aiId);
              const notice: ChatMessage = {
                id: newId(),
                role: 'ai',
                content: '',
                kind: 'notice',
                notice: { memberName, intent },
              };
              if (aiIdx === -1) return [...prev, notice];
              const copy = [...prev];
              copy.splice(aiIdx, 0, notice);
              return copy;
            });
          },
          onTerminal: ({ ok, error: termError }) => {
            setMessages((prev) => prev.map((m) => (m.id === aiId ? { ...m, streaming: false } : m)));
            if (!ok && termError) {
              const isQuota = termError.code === FREE_MODEL_QUOTA_EXHAUSTED_CODE;
              setError({
                text: isQuota
                  ? `「${agent.name}」今日免费问数已用完，明天再来，或在平台上为它配置模型。`
                  : termError.detail || '回复中断，请重试',
                quota: isQuota,
              });
            }
          },
        },
        controller.signal,
      );
      // 流正常结束（服务端先关）：收尾
      setMessages((prev) => prev.map((m) => (m.id === aiId ? { ...m, streaming: false } : m)));
    } catch (e) {
      if ((e as Error).name === 'AbortError') return; // stop() 已处理 UI
      const isQuota = e instanceof ApiError && e.code === FREE_MODEL_QUOTA_EXHAUSTED_CODE;
      const text2 = isQuota
        ? `「${agent.name}」今日免费问数已用完，明天再来，或在平台上为它配置模型。`
        : e instanceof ApiError
          ? e.status === 401
            ? ''
            : e.detail || '请求失败，请重试'
          : '请求失败，请重试';
      if (e instanceof ApiError && e.status === 401) {
        props.onAuthExpired();
        return;
      }
      if (text2) setError({ text: text2, quota: isQuota });
      setMessages((prev) =>
        prev
          .map((m) => (m.id === aiId ? { ...m, streaming: false, content: m.content } : m))
          .filter((m) => !(m.id === aiId && !m.content)),
      );
    } finally {
      abortRef.current = null;
      activeTurnIdRef.current = null;
      setBusy(false);
    }
  };

  // 打开聊天时计一次用量（广场同口径）
  useEffect(() => {
    void useAgent(agent.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent.id]);

  return (
    <>
      <div className="sd-chat-head">
        <button className="sd-icon-btn" title="返回" onClick={props.onBack}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <path d="M15 18l-6-6 6-6" />
          </svg>
        </button>
        <div className={`sd-emp-avatar ${tone}`}>{glyph}</div>
        <div>
          <div className="sd-emp-name">{agent.name}</div>
          <div className="sd-emp-status">{statusText}</div>
        </div>
        <div className="sd-spacer" style={{ flex: 1 }} />
        <button className="sd-icon-btn" title="收起" onClick={props.onCollapse}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <path d="M6 9l6 6 6-6" />
          </svg>
        </button>
      </div>

      <div className="sd-chat-scroll" ref={scrollRef}>
        {messages.length === 0 && (
          <div className="sd-chat-welcome">
            <div className="sd-wa">{glyph}</div>
            <h3>你好，我是{agent.name}</h3>
            <p>{agent.description || '我可以帮你处理工作问题，直接开始对话吧。'}</p>
          </div>
        )}
        {messages.map((m) =>
          m.kind === 'notice' && m.notice ? (
            <div key={m.id} className="sd-notice">
              <div className="sd-notice-card">
                <span className="sd-notice-badge">路由</span>
                <span>
                  已转交给
                  <span className="sd-notice-target"> {m.notice.memberName}</span>
                </span>
                {m.notice.intent && <span className="sd-notice-intent">{m.notice.intent}</span>}
              </div>
            </div>
          ) : (
            <div key={m.id} className={`sd-msg ${m.role === 'me' ? 'me' : 'ai'}`}>
              {m.role === 'ai' && <div className={`sd-m-avatar ${tone}`}>{glyph}</div>}
              <div className="sd-bubble">
                {m.streaming && !m.content ? (
                  <span className="sd-waiting">
                    <span className="sd-waiting-dots">
                      <span />
                      <span />
                      <span />
                    </span>
                    {agent.kind === 'dispatcher' ? '正在分派成员…' : '正在思考…'}
                    <span className="sd-typing-cursor" />
                  </span>
                ) : (
                  renderBubbleContent(m.content, Boolean(m.streaming), apiBase)
                )}
              </div>
            </div>
          ),
        )}
      </div>

      {error && <div className={`sd-chat-error${error.quota ? ' quota' : ''}`}>{error.text}</div>}

      <div className="sd-input-wrap">
        <div className="sd-input">
          <textarea
            rows={1}
            placeholder="输入消息，Enter 发送…"
            value={input}
            onChange={(e) => {
              setInput(e.target.value);
              e.target.style.height = 'auto';
              e.target.style.height = `${Math.min(e.target.scrollHeight, 80)}px`;
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
          />
          {busy ? (
            <button className="sd-send-btn stop" title="停止" onClick={stop}>
              <svg viewBox="0 0 24 24" fill="currentColor">
                <rect x="6" y="6" width="12" height="12" rx="2" />
              </svg>
            </button>
          ) : (
            <button className="sd-send-btn" title="发送" disabled={!input.trim()} onClick={() => void send()}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z" />
              </svg>
            </button>
          )}
        </div>
        <div className="sd-input-note">
          由 <b>AIStaff</b> 平台模型驱动 · 回复仅供参考
        </div>
      </div>
    </>
  );
}
