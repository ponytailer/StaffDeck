import { useEffect, useMemo, useState } from 'react';
import { ApiError, fetchChatAgents } from '../../lib/api';
import { isOaPage, oaEntryUrl, toggleOaPanel } from '../../lib/oa';
import type { AgentProfileRead, UserRead } from '../../types';

/** 头像：按名字哈希取色调，取名字首字符 */
function avatarOf(agent: AgentProfileRead): { tone: string; glyph: string } {
  let hash = 0;
  for (const ch of agent.name) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  const tone = ['t1', 't2', 't3'][hash % 3];
  const glyph = agent.name.trim().slice(0, 1) || 'SD';
  return { tone, glyph };
}

export function AgentListView(props: {
  user: UserRead;
  onOpenAgent: (agent: AgentProfileRead) => void;
  onLogout: () => void;
  onCollapse: () => void;
}) {
  const [agents, setAgents] = useState<AgentProfileRead[] | null>(null);
  const [error, setError] = useState('');
  const [oaHere, setOaHere] = useState(false);
  const [oaUrl, setOaUrl] = useState('');
  const [oaBusy, setOaBusy] = useState(false);
  const [oaError, setOaError] = useState('');

  const load = async () => {
    setError('');
    try {
      const list = await fetchChatAgents();
      setAgents(list.filter((a) => a.status === 'active' || !a.status));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        props.onLogout();
        return;
      }
      setError(e instanceof ApiError ? e.detail : '加载失败，请重试');
    }
  };

  useEffect(() => {
    void load();
    void isOaPage().then(setOaHere);
    void oaEntryUrl().then(setOaUrl);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const openOa = async () => {
    if (!oaHere) {
      // 非 OA 页：新 tab 打开 OA 报销表单页，收起面板不挡路
      if (oaUrl) {
        void chrome.tabs.create({ url: oaUrl });
        props.onCollapse();
      } else {
        setOaError('OA 场景配置加载失败，请稍后重试');
      }
      return;
    }
    setOaBusy(true);
    setOaError('');
    try {
      await toggleOaPanel();
      props.onCollapse();
    } catch (e) {
      setOaError(e instanceof Error ? e.message : 'OA 面板加载失败，请重试');
    } finally {
      setOaBusy(false);
    }
  };

  const groups = useMemo(() => {
    const all = agents ?? [];
    return {
      free: all.filter((a) => a.free_model_enabled && a.kind !== 'dispatcher'),
      squad: all.filter((a) => a.kind === 'dispatcher'),
      model: all.filter((a) => !a.free_model_enabled && a.kind !== 'dispatcher'),
    };
  }, [agents]);

  const renderCard = (agent: AgentProfileRead) => {
    const { tone, glyph } = avatarOf(agent);
    const isSquad = agent.kind === 'dispatcher';
    return (
      <button key={agent.id} className="sd-emp-card" onClick={() => props.onOpenAgent(agent)}>
        <div className={`sd-emp-avatar ${tone}`}>{glyph}</div>
        <div className="sd-emp-info">
          <div className="sd-emp-name-row">
            <span className="sd-emp-name">{agent.name}</span>
            {agent.free_model_enabled ? (
              <span className="sd-badge free">
                免费{typeof agent.free_model_remaining === 'number' ? ` · 今日剩 ${agent.free_model_remaining}` : ''}
              </span>
            ) : isSquad ? (
              <span className="sd-badge squad">小队</span>
            ) : (
              <span className="sd-badge model">模型员工</span>
            )}
          </div>
          <div className="sd-emp-desc">{agent.description || (isSquad ? '调度小队 · 分派成员协作' : '数字员工')}</div>
        </div>
        <svg className="sd-emp-arrow" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <path d="M9 18l6-6-6-6" />
        </svg>
      </button>
    );
  };

  const section = (label: string, list: AgentProfileRead[], emptyText: string) => (
    <>
      <div className="sd-section-label">{label}</div>
      {list.length === 0 ? <div className="sd-list-status" style={{ padding: '10px 0' }}>{emptyText}</div> : list.map(renderCard)}
    </>
  );

  return (
    <>
      <div className="sd-topbar">
        <div className="sd-brand-mark">AI</div>
        <div>
          <div className="sd-title">数字员工</div>
          <div className="sd-subtitle">AIStaff · 已连接</div>
        </div>
        <div className="sd-spacer" />
        <button className="sd-icon-btn" title="退出登录" onClick={props.onLogout}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9" />
          </svg>
        </button>
        <button className="sd-icon-btn" title="收起" onClick={props.onCollapse}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <path d="M6 9l6 6 6-6" />
          </svg>
        </button>
      </div>

      <div className="sd-list-scroll">
        {agents === null && !error && <div className="sd-list-status">加载中…</div>}
        {error && (
          <div className="sd-list-status">
            {error}
            <div style={{ marginTop: 10 }}>
              <button className="sd-icon-btn" style={{ width: 'auto', padding: '0 14px' }} onClick={() => void load()}>
                重试
              </button>
            </div>
          </div>
        )}
        {agents !== null && !error && (
          <>
            <div className="sd-section-label">AI 单据助手</div>
            <button className="sd-emp-card" onClick={() => void openOa()} disabled={oaBusy}>
              <div className="sd-emp-avatar t2">OA</div>
              <div className="sd-emp-info">
                <div className="sd-emp-name-row">
                  <span className="sd-emp-name">AI 单据助手</span>
                  <span className="sd-badge model">代填</span>
                </div>
                <div className="sd-emp-desc">
                  {oaError ||
                    (oaBusy
                      ? '正在加载面板…'
                      : oaHere
                        ? '当前是 OA 页面 · 传发票自动填报销单，点开即用'
                        : '一键打开 OA 报销单，传发票自动代填')}
                </div>
              </div>
              {oaHere ? (
                <svg
                  className="sd-emp-arrow"
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                >
                  <path d="M9 18l6-6-6-6" />
                </svg>
              ) : (
                <span className="sd-oa-jump">跳转 ↗</span>
              )}
            </button>
            {section('免费员工', groups.free, '暂无免费员工')}
            {section('模型员工', groups.model, '暂无模型员工')}
            {section('小队', groups.squad, '暂无小队')}
          </>
        )}
      </div>

      <div className="sd-user-strip">
        <div className="sd-ua">{(props.user.display_name || props.user.username).slice(0, 1).toUpperCase()}</div>
        <div>
          <div className="sd-un">{props.user.display_name || props.user.username}</div>
          <div className="sd-up">{props.user.email || props.user.username}</div>
        </div>
        <div className="sd-spacer" />
        <button className="sd-icon-btn" title="收起" onClick={props.onCollapse}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <path d="M6 9l6 6 6-6" />
          </svg>
        </button>
      </div>
    </>
  );
}
