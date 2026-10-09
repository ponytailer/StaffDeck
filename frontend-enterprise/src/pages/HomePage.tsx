import { useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, TENANT_ID } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import AppHeader from '@/components/AppHeader';
import PlazaHomePage from '@/components/openPlatform/PlazaHomePage';

const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';

/** 站点默认首页（侧边栏「首页」）：AI 新闻 + 快捷方式画布。 */
export default function HomePage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
}) {
  const navigate = useNavigate();

  // 复用广场「使用此员工」的完整链路：标记 used → 写入员工作用域 → 进员工档案。
  // 接口失败（已用过/网络抖动）不拦跳转，保证快捷方式始终可达。
  const handleUseEmployee = useCallback(
    (agentId: string) => {
      void (async () => {
        try {
          await api.post(`/api/chat/agents/${agentId}/use?tenant_id=${TENANT_ID}`, {});
        } catch {
          // 忽略：是否已用过不影响后续跳转
        }
        window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, agentId);
        window.dispatchEvent(new Event('ultrarag-enterprise-agent-scope-refresh'));
        window.dispatchEvent(
          new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId } }),
        );
        navigate('/enterprise/dashboard');
      })();
    },
    [navigate],
  );

  const handleOpenAgentApp = useCallback(
    (entryId: string) => {
      navigate(`/enterprise/agent-apps/${encodeURIComponent(entryId)}`);
    },
    [navigate],
  );

  return (
    <div className="relative min-h-full box-border overflow-x-clip bg-[#fbfbfa] px-[48px] pt-[20px] pb-[43px] max-[900px]:px-[16px]">
      {/* 登录页同款背景层：缓慢极光光斑 + 淡点阵网格，纯装饰 */}
      <div aria-hidden className="pointer-events-none absolute inset-0 overflow-hidden">
        <div
          data-aurora
          className="absolute -left-[10%] top-[4%] h-[380px] w-[500px] rounded-full bg-[radial-gradient(circle,rgba(15,118,110,0.16),transparent_68%)] blur-[70px]"
        />
        <div
          data-aurora
          className="absolute -right-[8%] top-[30%] h-[400px] w-[460px] rounded-full bg-[radial-gradient(circle,rgba(168,93,50,0.12),transparent_68%)] blur-[80px]"
        />
        <div
          data-aurora
          className="absolute left-[28%] top-[64%] h-[340px] w-[460px] rounded-full bg-[radial-gradient(circle,rgba(111,123,66,0.12),transparent_70%)] blur-[80px]"
        />
        <div className="absolute inset-0 bg-[radial-gradient(rgba(24,24,26,0.05)_1px,transparent_1px)] [background-size:26px_26px] [mask-image:radial-gradient(ellipse_at_50%_28%,black,transparent_72%)]" />
      </div>

      <AppHeader
        className="relative z-10 mb-[16px]"
        onLogout={onLogout}
        userName={currentUser?.username}
        title="首页"
      />

      <div className="relative z-10">
        <PlazaHomePage onUseEmployee={handleUseEmployee} onOpenAgentApp={handleOpenAgentApp} />
      </div>
    </div>
  );
}
