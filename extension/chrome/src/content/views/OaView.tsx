import { useEffect, useState } from 'react';
import { isOaPage, oaEntryUrl, oaScenarios, openInNewTab, toggleOaPanel, type OaScenarioInfo } from '../../lib/oa';
import type { UserRead } from '../../types';
import { PanelHeader } from './PanelHeader';

/**
 * 「单据助手」tab：与员工 tab 平级的 OA 助手独立视图（对齐 docs/extension-oa-tab-demo.html）。
 * - 状态卡：在 OA 页面 → 打开代填面板；不在 → 「跳转 OA 报销单 ↗」新 tab
 * - 场景清单：后端配置实时下发，「去填报」新 tab 打开对应报销单
 * - 使用步骤说明卡
 */
export function OaView(props: {
  user: UserRead;
  onTab: (tab: 'agents' | 'oa') => void;
  onLogout: () => void;
  onCollapse: () => void;
}) {
  const [oaHere, setOaHere] = useState<boolean | null>(null);
  const [entryUrl, setEntryUrl] = useState('');
  const [scenarios, setScenarios] = useState<OaScenarioInfo[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    void isOaPage().then(setOaHere);
    void oaEntryUrl().then(setEntryUrl);
    void oaScenarios().then(setScenarios);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const openPanel = async () => {
    setBusy(true);
    setError('');
    try {
      await toggleOaPanel();
      props.onCollapse();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'OA 面板加载失败，请重试');
    } finally {
      setBusy(false);
    }
  };

  const jump = (url: string) => {
    if (!url) {
      setError('OA 场景配置加载失败，请稍后重试');
      return;
    }
    openInNewTab(url);
    props.onCollapse();
  };

  const onOaPage = oaHere === true;

  return (
    <>
      <PanelHeader tab="oa" onTab={props.onTab} onLogout={props.onLogout} onCollapse={props.onCollapse} />

      <div className="sd-list-scroll">
        {/* 连接状态卡 */}
        <div className={`sd-oa-status${onOaPage ? ' on' : ''}`}>
          <div className="sd-oa-status-row">
            <span className="sd-oa-dot" />
            <div>
              <div className="sd-oa-status-title">
                {oaHere === null ? '正在检测当前页面…' : onOaPage ? '已检测到 OA 页面' : '未检测到 OA 页面'}
              </div>
              <div className="sd-oa-status-desc">
                {onOaPage ? '当前页面是报销单 · 面板就绪，可直接代填' : '在 OA 报销单页面打开即可传发票自动代填'}
              </div>
            </div>
          </div>
          {oaHere !== null && (
            <button
              className={`sd-btn btn-block ${onOaPage ? 'sd-btn-primary' : 'sd-btn-ghost'}`}
              onClick={() => void (onOaPage ? openPanel() : jump(entryUrl))}
              disabled={busy}
            >
              {busy ? '正在加载面板…' : onOaPage ? '打开代填面板' : '跳转 OA 报销单 ↗'}
            </button>
          )}
          {error && <div className="sd-oa-error">{error}</div>}
        </div>

        {/* 场景清单 */}
        <div className="sd-section-label">支持的单据场景</div>
        {scenarios === null && <div className="sd-list-status">场景加载中…</div>}
        {scenarios !== null && scenarios.length === 0 && (
          <div className="sd-list-status">暂无可用场景（配置为空）</div>
        )}
        {scenarios?.map((sc) => (
          <button key={sc.id} className="sd-oa-scene" onClick={() => jump(sc.url)}>
            <div className="sd-scene-glyph">
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
                <path d="M14 2v6h6M9 13h6M9 17h4" />
              </svg>
            </div>
            <div className="sd-emp-info">
              <div className="sd-scene-name">{sc.name}</div>
              {sc.summary && <div className="sd-scene-meta">{sc.summary}</div>}
            </div>
            <span className="sd-scene-go">去填报</span>
          </button>
        ))}

        {/* 使用步骤 */}
        <div className="sd-section-label" style={{ marginTop: 14 }}>
          怎么用
        </div>
        <div className="sd-oa-steps">
          <li>
            <span className="sd-step-n">1</span>点「去填报」新标签页打开对应报销单（未在 OA 页时自动跳转）
          </li>
          <li>
            <span className="sd-step-n">2</span>把发票图片拖进面板，或粘贴截图，自动识别金额 / 税号 / 日期
          </li>
          <li>
            <span className="sd-step-n">3</span>核对预览后一键代填到表单，全过程留痕可回溯
          </li>
        </div>
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
