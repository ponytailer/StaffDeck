import { useEffect, useRef, useState } from 'react';
import {
  ApiError,
  FREE_MODEL_QUOTA_EXHAUSTED_CODE,
  cancelSession,
  streamChatTurn,
  useAgent,
} from '../../lib/api';
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

function renderBubbleContent(text: string, streaming: boolean) {
  return (
    <>
      {splitLinks(text).map((p, i) =>
        p.type === 'text' ? (
          p.value
        ) : (
          <a key={i} className="sd-link" href={p.value} target="_blank" rel="noopener noreferrer">
            {p.value}
          </a>
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
                  renderBubbleContent(m.content, Boolean(m.streaming))
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
