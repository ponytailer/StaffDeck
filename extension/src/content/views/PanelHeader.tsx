/** 面板共用头部：品牌行 + 「员工 | 单据助手」平级 tab 栏（对齐 docs/extension-oa-tab-demo.html） */
export function PanelHeader(props: {
  tab: 'agents' | 'oa';
  onTab: (tab: 'agents' | 'oa') => void;
  onLogout: () => void;
  onCollapse: () => void;
}) {
  return (
    <div className="sd-topbar sd-topbar-tabs">
      <div className="sd-topbar-row">
        <div className="sd-brand-mark">AI</div>
        <div>
          <div className="sd-title">AIStaff 助手</div>
          <div className="sd-subtitle">
            <span className="sd-live-dot" /> 已连接
          </div>
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
      <div className="sd-tabs">
        <button
          className={`sd-tab${props.tab === 'agents' ? ' on' : ''}`}
          onClick={() => props.onTab('agents')}
        >
          员工
        </button>
        <button className={`sd-tab${props.tab === 'oa' ? ' on' : ''}`} onClick={() => props.onTab('oa')}>
          单据助手
          <span className="sd-tab-badge">OA</span>
        </button>
      </div>
    </div>
  );
}
