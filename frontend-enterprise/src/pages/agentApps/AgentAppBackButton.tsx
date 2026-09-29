import { useNavigate } from 'react-router-dom';

/**
 * Agent 广场工作台右上角的返回入口（PPT Studio / 决策助手等使用页共用）。
 * 目标地址与广场清单入口保持一致：/enterprise/platform/agent-apps。
 */
export default function AgentAppBackButton() {
  const navigate = useNavigate();
  return (
    <button
      type="button"
      onClick={() => navigate('/enterprise/platform/agent-apps')}
      className="text-[12px] text-[#757f9c] transition-colors hover:text-[#18181a]"
    >
      ← 返回 Agent 广场
    </button>
  );
}
