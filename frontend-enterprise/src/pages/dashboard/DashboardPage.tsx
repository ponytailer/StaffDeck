import { useEffect, useState } from 'react';
import type { ComponentType, ReactNode, SVGProps } from 'react';
import { useNavigate } from 'react-router-dom';
import { Badge, Button as UiButton, Tabs, TabsList, TabsTrigger, notify } from '@/components/ui';
import { EnterpriseRoute } from '../../enums/routes';
import IconChat from '../../assets/icons/chat.svg?react';
import IconEdit from '../../assets/icons/edit.svg?react';
import IconAccount from '../../assets/icons/sys-accounts.svg?react';
import IconProfileFile from '../../assets/icons/profile-file.svg?react';
import IconProfileAlarm from '../../assets/icons/profile-alarm.svg?react';
import IconProfileHistory from '../../assets/icons/profile-history.svg?react';
import IconProfileCalendar from '../../assets/icons/profile-calendar.svg?react';
import IconFolder from '../../assets/icons/cap-folder.svg?react';
import IconCapClipboard from '../../assets/icons/cap-clipboard.svg?react';
import IconCapMagicWand from '../../assets/icons/cap-magicwand.svg?react';
import IconCapBriefcase from '../../assets/icons/cap-briefcase.svg?react';
import { api, TENANT_ID } from '../../api/client';
import type { EnterpriseAuthUser } from '../../auth';
import AppHeader from '../../components/AppHeader';
import EmployeeAvatar from '../../components/EmployeeAvatar';
import EmployeeAvatarEditor from '../../components/EmployeeAvatarEditor';
import EmployeeProfileEditor from '../../components/EmployeeProfileEditor';
import StaffdeckIcon from '../../components/StaffdeckIcon';
import ScheduledTasksTab from './ScheduledTasksTab';
import MemoriesTab from './MemoriesTab';
import ConversationLogsTab from './ConversationLogsTab';
import WorkRecordTab, { ActivityTimeline } from './WorkRecordTab';
import KnowledgeManagePage from '../KnowledgePage';
import SkillsPage from '../SkillsPage';
import GeneralSkillsPage from '../GeneralSkillsPage';
import ToolsPage from '../ToolsPage';
import EvolutionPanel from './EvolutionPanel';
import { employeeDashboardMetrics } from './employeeDashboardMetrics';
import {
  agentResourceCount,
  canManageEmployeeAgent,
  canSelectCurrentEmployeeAgent,
  employeeCreatorName,
  employeeDisplayName,
  employeeProfile,
  preferredEmployeeAgent,
  staffdeckDisplayText,
} from '../../employee';
import type {
  AgentProfileRead,
  AgentWorkRecordEventRead,
  AgentWorkRecordRead,
  EnterpriseChatSessionRead,
  FeedbackSummaryRead,
  GeneralSkillRead,
  KnowledgeBasePage,
  KnowledgeBaseRead,
  ModelConfigRead,
  ScheduledTaskRead,
  SkillRead,
  ToolRead,
} from '../../types';
import { isTeamScope, readEmployeeScope } from '../../lib/agent-scope-storage';
import { cn } from '@/lib/utils';
import { parseBackendDateTime } from '../../lib/timezone';
import {
  MAX_DISPATCH_MEMBERS,
  dispatchFallbackLabel,
  listDispatchers,
  type DispatcherRead,
} from '../../lib/dispatcher';
import DispatcherGlyph from '../../components/dispatcher/DispatcherGlyph';
import { isDispatcherScopeId } from '../../lib/dispatcher-scope';

const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';

/**
 * 档案页 hero 的两颗按钮样式。提到模块级是因为员工档案与员工小队档案
 * **必须长得一模一样** —— 用户反馈「按钮布局不对」时，除了位置，视觉权重也
 * 在对比范围内；两处各写一份 class 迟早会漂。
 */
const HERO_PRIMARY_BUTTON_CLASS =
  'inline-flex items-center justify-center gap-[7px] h-[40px] px-[20px] rounded-[12px] border-0 bg-[#0f7268] text-[13.5px] font-semibold text-white shadow-[0_10px_22px_-10px_rgba(15,114,104,0.85)] transition-all hover:bg-[#0c6158] hover:-translate-y-[1px] hover:shadow-[0_14px_26px_-10px_rgba(15,114,104,0.9)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0f7268]/35 focus-visible:ring-offset-2';
const HERO_SECONDARY_BUTTON_CLASS =
  'inline-flex items-center justify-center gap-[4px] py-[8px] px-[12px] rounded-[14px] border-[0.5px] border-[#e3e7f1] bg-white text-[12px] font-normal text-[#858b9c] shadow-[0px_6px_6px_rgba(0,0,0,0.05)] hover:bg-[#f6f6f6] hover:text-[#858b9c]';

export default function DashboardPage({
  currentUser,
  isAdmin = false,
  profileTab = 'work',
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  isAdmin?: boolean;
  profileTab?: ProfileTabKey;
  onLogout?: () => void;
}) {
  const navigate = useNavigate();
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [skills, setSkills] = useState<SkillRead[]>([]);
  const [generalSkills, setGeneralSkills] = useState<GeneralSkillRead[]>([]);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseRead[]>([]);
  const [models, setModels] = useState<ModelConfigRead[]>([]);
  const [tools, setTools] = useState<ToolRead[]>([]);
  const [sessions, setSessions] = useState<EnterpriseChatSessionRead[]>([]);
  const [feedbackSummary, setFeedbackSummary] = useState<FeedbackSummaryRead | null>(null);
  const [scheduledTasks, setScheduledTasks] = useState<ScheduledTaskRead[]>([]);
  const [activityEvents, setActivityEvents] = useState<AgentWorkRecordEventRead[]>([]);
  const [agentId, setAgentId] = useState(readEmployeeScope);
  const [dispatchers, setDispatchers] = useState<DispatcherRead[] | null>(null);
  const [avatarEditorOpen, setAvatarEditorOpen] = useState(false);
  const [profileEditorOpen, setProfileEditorOpen] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [activeTab, setActiveTab] = useState<ProfileTabKey>(profileTab);
  useEffect(() => setActiveTab(profileTab), [profileTab]);

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      const next = (event as CustomEvent<{ agentId?: string }>).detail?.agentId || '';
      setAgentId(next && !isTeamScope(next) ? next : readEmployeeScope());
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

  useEffect(() => {
    let cancelled = false;
    let switchingAgent = false;
    setLoaded(false);
    Promise.all([
      api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`),
      // 多智能体不在 /agents 里（后端按设计排除），单独取一份用于判断「当前作用域是不是它」
      listDispatchers().catch(() => [] as DispatcherRead[]),
      api.get<SkillRead[]>(`/api/enterprise/skills?tenant_id=${TENANT_ID}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ''}`),
      // include_files=0：dashboard 只用技能计数与列表元信息，文件包 MB 级不拉
      api.get<GeneralSkillRead[]>(`/api/enterprise/general-skills?tenant_id=${TENANT_ID}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ''}&include_files=0`),
      api.get<KnowledgeBasePage>(`/api/enterprise/knowledge-bases?tenant_id=${TENANT_ID}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ''}&page=1&page_size=200`),
      api.get<ModelConfigRead[]>(`/api/enterprise/model-configs?tenant_id=${TENANT_ID}`),
      api.get<ToolRead[]>(`/api/enterprise/tools?tenant_id=${TENANT_ID}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ''}`),
      api.get<EnterpriseChatSessionRead[]>(`/api/enterprise/sessions?tenant_id=${TENANT_ID}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ''}`),
      api.get<FeedbackSummaryRead>(`/api/enterprise/feedback/summary?tenant_id=${TENANT_ID}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ''}`),
      api.get<ScheduledTaskRead[]>(`/api/enterprise/scheduled-tasks?tenant_id=${TENANT_ID}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ''}`),
    ])
      .then(([agentRows, dispatcherRows, skillRows, generalSkillRows, kbRows, modelRows, toolRows, sessionRows, feedbackRows, taskRows]) => {
        if (cancelled) return;
        const visibleAgents = agentRows.filter((item) => canSelectCurrentEmployeeAgent(item, currentUser, {
          activeOnly: true,
        }));
        setAgents(visibleAgents);
        setDispatchers(dispatcherRows);
        setModels(modelRows);
        // 多智能体作用域必须原样保留：它不在员工花名册里，若走下面的兜底就会被
        // 悄悄改写成别人的档案（页面上「当前员工信息」变成另一个人）。
        // 同步缓存优先 —— 这份 dispatcher 列表要等远程 PG 往返（冷启动 3~8s），
        // 等它回来之前 `dispatcherRows` 还是空数组，用户会先看到别人的档案再被切走。
        const isDispatcherScope =
          isDispatcherScopeId(agentId) || dispatcherRows.some((item) => item.id === agentId);
        // 团队作用域（`team:xxx`）不在员工花名册里，档案页按既有约定**回落到可用员工**
        // 渲染（团队没有独立档案页）。但绝不能把 `team:xxx` 永久改写成员工 id ——
        // 那会让左下角从「当前团队 OneTeam」跳成「当前员工 亚特」，用户以为
        // 自己切错了作用域。所以这里只跳过「写入」，渲染回落交给下面的 selectedAgent。
        // 注意必须读 localStorage 原始值：`readEmployeeScope()` 已把 team scope
        // 折成空串（员工向页面的语义），拿 agentId 判断永远为 false。
        const isTeamAgentScope = isTeamScope(
          window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY),
        );
        if (
          !isDispatcherScope &&
          !isTeamAgentScope &&
          (!agentId || !visibleAgents.some((item) => item.id === agentId))
        ) {
          const manageableAgents = visibleAgents.filter((item) => canManageEmployeeAgent(item, currentUser));
          const next = isAdmin
            ? preferredEmployeeAgent(visibleAgents)?.id || ''
            : preferredEmployeeAgent(manageableAgents)?.id
              || preferredEmployeeAgent(visibleAgents)?.id
              || '';
          if (next) {
            switchingAgent = true;
            window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, next);
            window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: next } }));
            setAgentId(next);
            return;
          }
        }
        setSkills(skillRows);
        setGeneralSkills(generalSkillRows);
        setKnowledgeBases(kbRows.items);
        setTools(toolRows);
        setSessions(sessionRows);
        setFeedbackSummary(feedbackRows);
        setScheduledTasks(taskRows.filter((item) => item.status !== 'archived'));
        setLoaded(true);
      })
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载数字员工档案失败'))
      .finally(() => {
        if (!cancelled && !switchingAgent) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId, currentUser, isAdmin]);

  /** 当前作用域是多智能体时，它就是页面主体 —— 不能退回「挑一个员工」。 */
  const selectedDispatcher = dispatchers?.find((item) => item.id === agentId) || null;
  /**
   * 同步缓存的兜底：dispatcher 列表还没回来、但作用域确实是小队时，
   * 绝不能走 `agents.find(...) || agents.find(!is_overall)` 挑一个员工上来
   * —— 那会把「X Man 的档案页」渲染成别人的档案（真实事故）。
   * 此时 `selectedAgent` 保持 null，由下面 `selectedDispatcher` 异步到位后再渲染。
   */
  const scopeIsDispatcher = Boolean(selectedDispatcher) || isDispatcherScopeId(agentId);
  const selectedAgent = scopeIsDispatcher
    ? null
    : agents.find((item) => item.id === agentId)
      || agents.find((item) => !item.is_overall)
      || null;
  const employeeSessions = scopeIsDispatcher
    ? sessions
    : selectedAgent?.is_overall
      ? sessions
      : sessions.filter((item) => item.agent_id === selectedAgent?.id);

  useEffect(() => {
    let cancelled = false;
    async function loadWorkRecord() {
      // 员工小队同样走这条接口：它也是 agent_profiles 里的一行，后端
      // `/agents/{id}/work-record` 不按 kind 过滤，实测小队有 78 条回复事件。
      // 档案总览要复用员工那套「活动时间线」，所以这里不能因为 scope 是小队就清空。
      const targetId = selectedAgent?.id || selectedDispatcher?.id || '';
      if (!targetId || selectedAgent?.is_overall) {
        setActivityEvents([]);
        return;
      }
      try {
        const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai';
        const workRecord = await api.get<AgentWorkRecordRead>(
          `/api/enterprise/agents/${encodeURIComponent(targetId)}/work-record?tenant_id=${TENANT_ID}&timezone=${encodeURIComponent(timezone)}`,
        );
        if (cancelled) return;
        // 后端字段理论上一定有，但桩数据/老版本可能缺 `events`：
        // 直接透传会让下游 `events.length` 抛错、整棵档案树被卸载。
        setActivityEvents(workRecord.events || []);
      } catch (error) {
        if (cancelled) return;
        setActivityEvents([]);
        notify.error(error instanceof Error ? error.message : '加载工作记录失败');
      }
    }
    void loadWorkRecord();
    return () => {
      cancelled = true;
    };
  }, [selectedAgent?.id, selectedAgent?.is_overall, selectedDispatcher?.id]);
  const defaultModel = models.find((item) => item.is_default);
  const totalCalls = skills.reduce((sum, item) => sum + (item.total_call_count || item.call_count || 0), 0);
  const positiveFeedback = skills.reduce((sum, item) => sum + (item.total_positive_feedback_count || 0), 0);
  const negativeFeedback = skills.reduce((sum, item) => sum + (item.total_negative_feedback_count || 0), 0);
  const visibleKnowledgeBases = knowledgeBases.filter((item) => !isEmptyDefaultKnowledgeBase(item));

  // Avoid flashing the 开放广场 / empty state before the agents API resolves,
  // which would otherwise briefly render before the employee profile appears.
  if (!loaded && agents.length === 0 && !selectedDispatcher) {
    return <div className="page dashboard-page" />;
  }

  // 作用域是员工小队、但它的详情还没到（列表要等远程 PG 往返）：给一块占位。
  // 绝不能往下走到「挑一个员工」或「开放广场」分支 —— 那会让用户先看到别人的档案。
  if (scopeIsDispatcher && !selectedDispatcher) {
    return <div className="page dashboard-page" />;
  }

  // 技能广场在无数字员工时也应可浏览（公司内部 skill 分享）
  if (profileTab === 'skills' && !selectedAgent && !selectedDispatcher) {
    return (
      <div className="page dashboard-page">
        <GeneralSkillsPage embedded currentUser={currentUser} onLogout={onLogout} />
      </div>
    );
  }

  if (!selectedAgent && !selectedDispatcher && !isAdmin) {
    return (
      <div className="page dashboard-page">
        <div className="empty-workspace-card p-[24px]">
          <h3 className="m-0 text-[20px] font-semibold text-foreground">还没有数字员工</h3>
          <p className="mt-[8px] text-[14px] text-muted-foreground">
            点击左下角「新建数字员工」开始创建，或前往员工广场选择已发布的员工。
          </p>
          <div className="mt-[16px] flex gap-[8px]">
            <UiButton onClick={() => navigate('/enterprise/agents')}>查看我的数字员工</UiButton>
            <UiButton variant="outline" onClick={() => navigate('/enterprise/feedback')}>查看对话日志</UiButton>
          </div>
        </div>
      </div>
    );
  }

  // 多智能体作用域：它没有知识库/技能/SOP/工具，也没有头像/岗位/入职时间，
  // 套员工 hero 会全是「待补充岗位 / 0」。这里给一套只对它成立的字段。
  if (selectedDispatcher) {
    return (
      <DispatcherProfileView
        dispatcher={selectedDispatcher}
        profileTab={profileTab}
        sessions={sessions}
        feedbackSummary={feedbackSummary}
        activityEvents={activityEvents}
        onOpenChat={() => navigate(`/workspace/chat/draft/${selectedDispatcher.id}`)}
        onConfigure={() => navigate(EnterpriseRoute.Agents)}
        onLogout={onLogout}
        userName={currentUser?.username}
      />
    );
  }

  if (!selectedAgent || selectedAgent.is_overall) {
    return (
      <div className="page dashboard-page">
        <div className="page-title">
          <h3>开放广场</h3>
        </div>
        <section className="employee-hero org-hero">
          <div>
            <span className="section-kicker">开放广场</span>
            <h2 className="ui-typography">开放广场</h2>
            <p className="ui-typography">
              汇集所有可共享的 SOP、知识库、技能和工具，新建数字员工时可以从这里复制配置作为起点。
            </p>
          </div>
          <div className="employee-hero-metrics">
            <MetricTile label="员工" value={agents.filter((item) => !item.is_overall).length} />
            <MetricTile label="对话" value={sessions.length} />
            <MetricTile label="反馈" value={feedbackSummary?.total_feedback || 0} />
          </div>
        </section>
        <div className="org-dashboard-grid">
          <DashboardStat title="SOP" value={skills.length} icon={<StaffdeckIcon name="filter" />} />
          <DashboardStat title="技能" value={generalSkills.length} icon={<StaffdeckIcon name="spark" />} />
          <DashboardStat title="知识库" value={visibleKnowledgeBases.length} icon={<StaffdeckIcon name="file" />} />
          <DashboardStat title="可用工具" value={tools.filter(toolActive).length} icon={<StaffdeckIcon name="tool" />} />
          <DashboardStat title="SOP 调用" value={totalCalls} icon={<StaffdeckIcon name="chat" />} />
          <DashboardStat title="好评" value={positiveFeedback || feedbackSummary?.up_count || 0} icon={<StaffdeckIcon name="chat" />} />
          <DashboardStat title="差评" value={negativeFeedback || feedbackSummary?.down_count || 0} icon={<StaffdeckIcon name="chat" />} />
          <div className="org-dashboard-card">
            <div className="ui-card-body p-[24px]">
              <span className="org-dashboard-icon"><StaffdeckIcon name="model" /></span>
              <span className="text-[13px] text-muted-foreground">默认模型</span>
              <span className="text-[15px] text-foreground">{defaultModel ? `${defaultModel.name} / ${defaultModel.model}` : '未配置'}</span>
            </div>
          </div>
        </div>
      </div>
    );
  }

  const employee = employeeProfile(selectedAgent);
  const employeeCreator = employeeCreatorName(selectedAgent);
  const canEditSelectedAgent = canManageEmployeeAgent(selectedAgent, currentUser);
  const activeSkills = skills.filter((item) => item.status === 'published' && item.branch_status !== 'inactive');
  const activeGeneralSkills = generalSkills.filter((item) => item.status === 'published');
  const activeKnowledge = visibleKnowledgeBases.filter((item) => item.status === 'active');
  const activeTools = tools.filter(toolActive);
  const selectedKnowledgeCount = visibleKnowledgeBases.length;
  const selectedGeneralSkillCount = agentResourceCount(selectedAgent, 'general_skill');
  const selectedSkillCount = agentResourceCount(selectedAgent, 'skill');
  // 「工具」只算这个员工真正启用的：绑定状态 active（员工侧停用不算）且工具本身
  // enabled（工具被停用也不算）。整体员工（广场）没有绑定记录，回退到可见且启用。
  const selectedToolCount = selectedAgent.is_overall
    ? activeTools.length
    : tools.filter((item) => item.binding_status === 'active' && item.enabled).length;
  const employeeScheduledTasks = scheduledTasks.filter((item) => item.agent_id === selectedAgent.id && item.status !== 'archived');
  const activeScheduledTasks = employeeScheduledTasks.filter((item) => item.status === 'active');
  const dashboardMetrics = employeeDashboardMetrics(employeeSessions, feedbackSummary);
  const systemPromptSummary = typeof selectedAgent.metadata?.system_prompt_summary === 'string'
    ? selectedAgent.metadata.system_prompt_summary
    : '';
  const systemSummary = compactSummary(
    staffdeckDisplayText(selectedAgent.persona_prompt || systemPromptSummary || selectedAgent.description || `${employee.roleName}，负责接收任务、调用知识库、执行 SOP 并沉淀对话质量反馈。`),
    132,
  );

  const heroActionButtonClass = HERO_SECONDARY_BUTTON_CLASS;
  // 「去对话」是这个页面的主行动：全页唯一的填充色按钮（teal 强调色）+ 更大尺寸 +
  // 柔和投影。首次进入的人视线会先落在这块，不必在几个同权重灰按钮里挑。
  const heroPrimaryButtonClass = HERO_PRIMARY_BUTTON_CLASS;
  const heroAvatar = (
    <EmployeeAvatar
      agent={selectedAgent}
      width={136}
      height={160}
      radius={0}
      fit="contain"
      objectPosition="center bottom"
      style={{ background: 'transparent', border: 'none', boxShadow: 'none', overflow: 'visible' }}
    />
  );

  return (
    <div className="min-h-full w-full min-w-0 max-w-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
        left={(
          <div className="flex flex-wrap items-center gap-x-9 gap-y-6 pt-1 pl-1">
            <div className="flex shrink-0 flex-col items-center">
              {canEditSelectedAgent ? (
                <button
                  type="button"
                  onClick={() => setAvatarEditorOpen(true)}
                  aria-label="更换头像"
                  className="group relative block cursor-pointer border-0 bg-transparent p-0"
                >
                  {heroAvatar}
                  <span className="pointer-events-none absolute inset-x-0 bottom-0 flex items-center justify-center gap-1 bg-black/45 py-1 text-[11px] text-white opacity-0 transition-opacity group-hover:opacity-100">
                    <IconAccount className="size-3" />
                    更换头像
                  </span>
                </button>
              ) : (
                heroAvatar
              )}
              <div className="mt-[4px] flex flex-wrap items-center gap-4">
                <UiButton
                  variant="default"
                  className={heroPrimaryButtonClass}
                  onClick={() => { window.location.href = '/workspace/chat'; }}
                >
                  <IconChat className="size-[16px]" />
                  去对话
                  <span
                    aria-hidden
                    className="ml-[1px] text-[15px] leading-none transition-transform group-hover/button:translate-x-[3px]"
                  >
                    →
                  </span>
                </UiButton>
                {canEditSelectedAgent && (
                  <UiButton
                    variant="outline"
                    className={heroActionButtonClass}
                    onClick={() => setProfileEditorOpen(true)}
                  >
                    <IconEdit className="size-[14px]" />
                    编辑资料
                  </UiButton>
                )}
              </div>
            </div>

            <div className="flex min-w-[280px] flex-1 flex-col gap-2">
              <div className="flex items-end gap-2">
                <h2 className="m-0 text-[22px] leading-none font-semibold text-[#18181a]">
                  {employeeDisplayName(selectedAgent)}
                </h2>
                <span className="text-[13px] leading-none text-[#757f9c]">{employee.roleName || employeeDisplayName(selectedAgent)}</span>
              </div>

              <div className="flex flex-wrap items-center gap-4">
                <span className="inline-flex items-center gap-1.5 rounded-full bg-[#f6f6f6] px-2.5 py-0.5">
                  <span
                    className="size-1.5 rounded-full ring-[1.5px] ring-white"
                    style={{ background: selectedAgent.status === 'active' ? '#22c55e' : '#c4c9d4' }}
                  />
                  <span className="text-[12px] text-[#757f9c]">
                    {selectedAgent.status === 'active' ? '在线' : '下线'}
                  </span>
                </span>
                <span className="text-[12px] text-[#757f9c]">创建者：{employeeCreator}</span>
                <span className="text-[12px] text-[#757f9c]">入职时间：{employee.onboardedAt}</span>
                <div className="flex flex-wrap items-center gap-3">
                  {employee.workStyles.slice(0, 3).map((item) => (
                    <Badge
                      key={item}
                      variant="outline"
                      className="h-auto rounded-[10px] border-[0.5px] border-[#e3e7f1] px-4 py-1 text-[12px] font-normal text-[#757f9c]"
                    >
                      {item}
                    </Badge>
                  ))}
                </div>
              </div>

              <p className="m-0 line-clamp-2 max-w-[720px] text-[14px] leading-[22px] text-[#757f9c]">
                {systemSummary}
              </p>

              <div className="flex w-full max-w-[640px] gap-3">
                <HeroMetric value={selectedKnowledgeCount} label="资料" />
                <HeroMetric value={selectedGeneralSkillCount} label="技能" />
                <HeroMetric value={selectedSkillCount} label="SOP" />
                <HeroMetric value={selectedToolCount} label="工具" />
                <HeroMetric value={activeScheduledTasks.length} label="定时任务" />
              </div>
            </div>
          </div>
        )}
      />
      <EmployeeProfileTabs value={activeTab} onChange={setActiveTab} />
      {activeTab === 'work' && (
        <>
          <WorkRecordTab
            selectedAgent={selectedAgent}
            activeKnowledge={activeKnowledge}
            activeGeneralSkills={activeGeneralSkills}
            activeSkills={activeSkills}
            activeTools={activeTools}
            activeScheduledTasks={activeScheduledTasks}
            employeeSessions={employeeSessions}
            conversationCount={dashboardMetrics.conversationCount}
            activityEvents={activityEvents}
            feedbackCount={dashboardMetrics.feedbackCount}
            positiveRate={dashboardMetrics.positiveRate}
            negativeRate={dashboardMetrics.negativeRate}
          />
          {canEditSelectedAgent && <EvolutionPanel agentId={selectedAgent.id} />}
        </>
      )}
      {activeTab === 'knowledge' && (
        <KnowledgeManagePage embedded currentUser={currentUser} onLogout={onLogout} />
      )}
      {activeTab === 'sop' && (
        <SkillsPage embedded currentUser={currentUser} onLogout={onLogout} />
      )}
      {activeTab === 'skills' && (
        <GeneralSkillsPage embedded currentUser={currentUser} onLogout={onLogout} />
      )}
      {activeTab === 'tools' && (
        <ToolsPage embedded currentUser={currentUser} onLogout={onLogout} />
      )}
      {activeTab === 'scheduled' && <ScheduledTasksTab />}
      {activeTab === 'memories' && <MemoriesTab currentUser={currentUser} agent={selectedAgent} />}
      {activeTab === 'logs' && <ConversationLogsTab />}
      <EmployeeAvatarEditor
        agent={selectedAgent}
        open={avatarEditorOpen}
        onClose={() => setAvatarEditorOpen(false)}
        onSaved={(saved) => setAgents((current) => current.map((item) => (item.id === saved.id ? saved : item)))}
      />
      <EmployeeProfileEditor
        agent={selectedAgent}
        open={profileEditorOpen}
        currentUser={currentUser}
        onClose={() => setProfileEditorOpen(false)}
        onSaved={(saved) => setAgents((current) => current.map((item) => (item.id === saved.id ? saved : item)))}
      />
    </div>
  );
}

/**
 * 多智能体在「员工档案」位的呈现。
 *
 * 与员工 hero 的差异都是刻意的：头像位换成拓扑图标（它不是一个人）、
 * 第二行是「多智能体」而不是岗位名、三格换成 成员 / 无法匹配 / 模型，
 * 主行动是「去对话」，次行动是「配置成员与规则」。
 */
function DispatcherProfileView({
  dispatcher,
  profileTab,
  sessions,
  feedbackSummary,
  activityEvents,
  onOpenChat,
  onConfigure,
  onLogout,
  userName,
}: {
  dispatcher: DispatcherRead;
  profileTab: ProfileTabKey;
  sessions: EnterpriseChatSessionRead[];
  feedbackSummary: FeedbackSummaryRead | null;
  /** 小队自己的活动事件（对话/转接），复用员工那套时间线渲染 */
  activityEvents: AgentWorkRecordEventRead[];
  onOpenChat: () => void;
  onConfigure: () => void;
  onLogout?: () => void;
  userName?: string;
}) {
  const members = dispatcher.members;
  const sessionCount = sessions.filter((item) => item.agent_id === dispatcher.id).length;
  const missingRule = members.filter((item) => !item.routing_rule.trim()).length;

  // 多智能体只有这三块成立：总览 / 定时任务 / 对话日志。
  // 之前这里**没有页内 tab**，只按路由传进来的 profileTab 决定渲染什么 ——
  // 停在 /enterprise/dashboard 时 profileTab 恒为 work，页面上没有任何入口能切到
  // 另外两块（真实反馈「档案页面没法查看对话日志和定时任务」）。
  const [activeTab, setActiveTab] = useState<DispatcherTabKey>(() =>
    isDispatcherTab(profileTab) ? profileTab : 'work',
  );
  useEffect(() => {
    if (isDispatcherTab(profileTab)) setActiveTab(profileTab);
  }, [profileTab]);

  // 档案总览里的「活动时间线」下方「查看全部」要能切到本页的对话日志分区。
  // 绝不能像员工页那样跳 /enterprise/feedback —— 小队的反馈按技能维度统计，
  // 它没有任何技能，那个页面只会是空的（实测 total_feedback 恒为 0）。
  const onOpenLogs = () => setActiveTab('logs');

  return (
    <div className="min-h-full w-full min-w-0 max-w-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        onLogout={onLogout}
        userName={userName}
        left={(
          /* 两列：左=标识块 + 其下方的操作按钮，右=信息区（标题 → 状态行 → 简介 → 指标）。
             这与员工档案页的结构一致 —— 员工页的「去对话 / 编辑资料」就落在左侧头像
             正下方（实测 x=316，与头像左边缘 364.8 基本齐平），小队页原先却把按钮
             塞在右侧信息区最后一行（实测 x=964），于是同一个「档案页」在切作用域时
             按钮会从左跳到右，这是用户说的「按钮布局不对」。
             指标行改为整行铺开（不再与按钮抢一行），也不再被 min-w 挤压。 */
          <div className="flex flex-wrap items-start gap-x-[28px] gap-y-[18px] pt-1 pl-1">
            <div className="flex shrink-0 flex-col items-center">
              <span className="grid size-[112px] shrink-0 place-items-center rounded-[16px] bg-[#e1f1ed] text-[#0f7268]">
                <DispatcherGlyph className="size-[52px]" />
              </span>
              <div className="mt-[4px] flex flex-wrap items-center justify-center gap-[8px]">
                <UiButton
                  variant="default"
                  className={HERO_PRIMARY_BUTTON_CLASS}
                  onClick={onOpenChat}
                >
                  <IconChat className="size-[16px]" />
                  去对话
                  <span aria-hidden className="ml-[1px] text-[15px] leading-none">→</span>
                </UiButton>
                <UiButton
                  variant="outline"
                  className={HERO_SECONDARY_BUTTON_CLASS}
                  onClick={onConfigure}
                >
                  <IconEdit className="size-[14px]" />
                  配置成员
                </UiButton>
              </div>
            </div>

            <div className="flex min-w-[280px] flex-1 flex-col gap-[10px]">
              <div className="flex flex-wrap items-center gap-x-[8px] gap-y-[6px]">
                <h2 className="m-0 text-[22px] leading-none font-semibold text-[#18181a]">
                  {dispatcher.name}
                </h2>
                <span className="whitespace-nowrap rounded-[5px] bg-[#e1f1ed] px-[6px] py-[2px] text-[11px] leading-none text-[#0f7268]">
                  员工小队 · 纯路由
                </span>
                {missingRule > 0 && (
                  <span className="whitespace-nowrap rounded-[5px] bg-[#fdf6ec] px-[6px] py-[2px] text-[11px] leading-none text-[#8a5a12]">
                    {missingRule} 位缺路由规则
                  </span>
                )}
              </div>

              {/* 状态行：在线 / 兜底 / 模型 / 累计转接，统一用「分隔点」串起来，
                  不再是一个个散落的灰色文本。模型名从原来那张独立卡片挪到这里。 */}
              <div className="flex flex-wrap items-center gap-x-[10px] gap-y-[6px] text-[12px] text-[#757f9c]">
                <span className="inline-flex items-center gap-[6px]">
                  <span
                    className="size-[6px] rounded-full"
                    style={{ background: dispatcher.status === 'active' ? '#22c55e' : '#c4c9d4' }}
                  />
                  {dispatcher.status === 'active' ? '在线' : '下线'}
                </span>
                <span aria-hidden className="text-[#d5d9e3]">·</span>
                <span>无法匹配：{dispatchFallbackLabel(dispatcher.config.fallback_mode)}</span>
                <span aria-hidden className="text-[#d5d9e3]">·</span>
                <span className="inline-flex min-w-0 items-center gap-[5px]">
                  <span className="shrink-0">路由模型</span>
                  <span className="min-w-0 truncate rounded-[5px] bg-[#f2f3f6] px-[6px] py-[1px] text-[11.5px] text-[#464c5e]">
                    {dispatcher.model_name || '未配置'}
                  </span>
                </span>
                <span aria-hidden className="text-[#d5d9e3]">·</span>
                <span>累计转接 {dispatcher.usage_count} 次</span>
                {dispatcher.published_to_gallery && (
                  <>
                    <span aria-hidden className="text-[#d5d9e3]">·</span>
                    <span className="whitespace-nowrap rounded-[5px] bg-[#e1f1ed] px-[6px] py-[1px] text-[11px] leading-none text-[#0f7268]">
                      已发布到广场
                    </span>
                  </>
                )}
              </div>

              <p className="m-0 line-clamp-2 max-w-[720px] text-[14px] leading-[22px] text-[#757f9c]">
                {dispatcher.description
                  || '我是员工小队：先说需求，我来判断本轮该交给谁，再把问题转给最合适的同事。'}
              </p>

              {/* 指标行：与员工页同宽铺开（员工页是资料/技能/SOP/工具/定时任务 5 格），
                  小队只有 4 个成立的计数，多出来的宽度交给「累计转接」。 */}
              <div className="flex w-full max-w-[640px] gap-3">
                <HeroMetric value={members.length} label="成员" />
                <HeroMetric value={sessionCount} label="对话" />
                <HeroMetric value={feedbackSummary?.total_feedback || 0} label="反馈" />
                <HeroMetric value={dispatcher.usage_count} label="累计转接" />
              </div>
            </div>
          </div>
        )}
      />

      <div className="mt-[18px]">
        <DispatcherProfileTabs value={activeTab} onChange={setActiveTab} />
      </div>

      {activeTab === 'work' && (
        // 不再用 pl-[226px] 对齐头像列：窄屏时 hero 会换成竖排，那个固定缩进
        // 把卡片整体推向右侧、左边留一大片空白（真实截图确认过）。
        <div className="mt-[18px] flex flex-col gap-[12px]">
          {/* 这一块原先只有「成员与路由规则」一张卡，档案总览看起来空得像个
              未完成页面（真实反馈「员工档案页面现在有点空」）。这里按员工档案页
              的构成补齐：活动时间线（可复用组件）+ 成员规则 + 兜底策略 + 基础信息。 */}
          <DispatcherActivityCard events={activityEvents} onOpenLogs={onOpenLogs} />

          <div className="grid gap-[12px] lg:grid-cols-[1.35fr_1fr]">
            <DispatcherMembersCard
              members={members}
              maxMembers={MAX_DISPATCH_MEMBERS}
              onConfigure={onConfigure}
            />
            <div className="flex flex-col gap-[12px]">
              <DispatcherFallbackCard dispatcher={dispatcher} onConfigure={onConfigure} />
              <DispatcherMetaCard dispatcher={dispatcher} />
            </div>
          </div>
        </div>
      )}
      {activeTab === 'scheduled' && (
        <div className="mt-[20px]">
          <ScheduledTasksTab />
        </div>
      )}
      {activeTab === 'logs' && (
        <div className="mt-[20px]">
          <ConversationLogsTab />
        </div>
      )}
    </div>
  );
}

/** 多智能体能成立的分区（其余部分它本来就不配，不该出现在 tab 里）。 */
type DispatcherTabKey = 'work' | 'scheduled' | 'logs';

const DISPATCHER_TABS: { key: DispatcherTabKey; label: string; Icon: ComponentType<SVGProps<SVGSVGElement>> }[] = [
  { key: 'work', label: '档案总览', Icon: IconProfileFile },
  { key: 'scheduled', label: '定时任务', Icon: IconProfileAlarm },
  { key: 'logs', label: '对话日志', Icon: IconProfileCalendar },
];

function isDispatcherTab(key: ProfileTabKey): key is DispatcherTabKey {
  return key === 'work' || key === 'scheduled' || key === 'logs';
}

const PROFILE_TAB_TRIGGER_CLASS =
  'h-[35px] flex-none gap-[6px] rounded-t-lg rounded-b-none border-0 px-[18px] text-[12px] font-bold whitespace-nowrap text-[#8b94aa] hover:text-[#202226] data-[state=active]:bg-white data-[state=active]:text-[#202226] data-[state=active]:shadow-[0_-12px_28px_rgba(21,26,38,0.04)] in-data-[theme=dark]:text-[#8f98aa] in-data-[theme=dark]:hover:text-[#f0f2f6] in-data-[theme=dark]:data-[state=active]:bg-[#202126] in-data-[theme=dark]:data-[state=active]:text-[#c5ccd8] in-data-[theme=dark]:data-[state=active]:shadow-none';

function DispatcherProfileTabs({
  value,
  onChange,
}: {
  value: DispatcherTabKey;
  onChange: (value: DispatcherTabKey) => void;
}) {
  return (
    <Tabs
      value={value}
      onValueChange={(next) => onChange(next as DispatcherTabKey)}
      className="flex w-full flex-col items-start"
    >
      <TabsList
        aria-label="员工小队档案分区"
        className="h-[35px]! max-w-full gap-1 overflow-x-auto rounded-none bg-transparent p-0 scrollbar-hide"
      >
        {DISPATCHER_TABS.map(({ key, label, Icon }) => (
          <TabsTrigger key={key} value={key} className={PROFILE_TAB_TRIGGER_CLASS}>
            <Icon />
            {label}
          </TabsTrigger>
        ))}
      </TabsList>
    </Tabs>
  );
}

/**
 * 小队档案页的卡片外壳。四张卡共用同一套边距/圆角/阴影，避免各写一份后慢慢漂移
 * （员工页那几张卡就是各自写的，间距已经不一致了）。
 */
function DispatcherCardShell({
  title,
  count,
  action,
  children,
}: {
  title: string;
  count?: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="flex min-w-0 flex-col rounded-[20px] border border-black/[0.05] bg-white/85 shadow-[0_6px_20px_rgba(17,17,17,0.04)] backdrop-blur">
      <div className="flex items-center justify-between gap-[12px] px-[20px] pt-[16px]">
        <span className="flex min-w-0 items-baseline gap-[6px]">
          <span className="truncate text-[13px] font-medium text-[#18181a]">{title}</span>
          {count && (
            <span className="shrink-0 text-[11.5px] leading-none text-[#9aa1b1]">{count}</span>
          )}
        </span>
        {action}
      </div>
      <div className="flex min-w-0 flex-col gap-[10px] px-[20px] pb-[18px] pt-[12px]">
        {children}
      </div>
    </section>
  );
}

/**
 * 活动时间线卡：直接复用员工档案页那套（Day/Week/Month + 悬浮明细）。
 *
 * 小队的事件几乎全是 `chat`（转接问答也记在它自己名下），所以图上只有「对话」
 * 一条泳道 —— 这是事实，不是占位。
 */
function DispatcherActivityCard({
  events,
  onOpenLogs,
}: {
  events: AgentWorkRecordEventRead[];
  onOpenLogs: () => void;
}) {
  return (
    <section className="flex min-w-0 flex-col gap-[16px] rounded-[20px] border border-black/[0.05] bg-white/85 p-[20px] shadow-[0_6px_20px_rgba(17,17,17,0.04)] backdrop-blur">
      <div className="flex items-center justify-between gap-[12px]">
        <span className="flex min-w-0 items-baseline gap-[6px]">
          <span className="text-[13px] font-medium text-[#18181a]">活动记录</span>
          <span className="shrink-0 text-[11.5px] leading-none text-[#9aa1b1]">
            近 {events.length} 条事件
          </span>
        </span>
        <button
          type="button"
          onClick={onOpenLogs}
          className="shrink-0 whitespace-nowrap text-[12px] text-[#0f7268] hover:underline"
        >
          查看全部对话
        </button>
      </div>
      <ActivityTimeline events={events} />
    </section>
  );
}

/**
 * 成员与路由规则。这是小队唯一「有内容」的配置，所以给它更宽的一列。
 *
 * 每位成员补了三样原先看不到的信息：
 * - 他的岗位描述（`agent_description`）—— 判断「规则写得对不对」的依据；
 * - 示例问题数量（`example_queries`）—— 路由准确率的另一个来源；
 * - 状态（`agent_status`）—— 成员被下线后这条规则就失效了，得能看见。
 */
function DispatcherMembersCard({
  members,
  maxMembers,
  onConfigure,
}: {
  members: DispatcherRead['members'];
  maxMembers: number;
  onConfigure: () => void;
}) {
  return (
    <DispatcherCardShell
      title="成员与路由规则"
      count={`${members.length}/${maxMembers}`}
      action={(
        <button
          type="button"
          onClick={onConfigure}
          className="shrink-0 whitespace-nowrap text-[12px] text-[#0f7268] hover:underline"
        >
          去配置
        </button>
      )}
    >
      {members.length === 0 && (
        <span className="text-[12.5px] leading-[1.6] text-[#8a5a12]">
          还没有成员，所有问题都会走兜底。至少添加 {maxMembers > 1 ? 2 : 1} 位同事才有路由意义。
        </span>
      )}
      {members.map((member, index) => {
        const offline = member.agent_status !== 'active';
        const noRule = !member.routing_rule.trim();
        return (
          <div key={member.member_agent_id} className="flex items-start gap-[10px]">
            <span
              className={cn(
                'mt-[1px] grid size-[20px] shrink-0 place-items-center rounded-[6px] text-[10.5px] font-medium',
                offline ? 'bg-[#f2f3f6] text-[#b4bac8]' : 'bg-[#e1f1ed] text-[#0f7268]',
              )}
            >
              {index + 1}
            </span>
            <div className="flex min-w-0 flex-1 flex-col gap-[3px]">
              <span className="flex min-w-0 flex-wrap items-center gap-[6px] text-[13px] text-[#18181a]">
                <span className={cn('min-w-0 truncate', offline && 'text-[#9aa1b1]')}>
                  {member.agent_name}
                </span>
                {member.has_own_model && (
                  <span className="shrink-0 whitespace-nowrap rounded-[4px] bg-[#f2f9f7] px-[4px] py-[1px] text-[10px] text-[#0f7268]">
                    自有模型
                  </span>
                )}
                {offline && (
                  <span className="shrink-0 whitespace-nowrap rounded-[4px] bg-[#fdf6ec] px-[4px] py-[1px] text-[10px] text-[#8a5a12]">
                    已下线 · 这条规则暂不生效
                  </span>
                )}
              </span>
              {member.agent_description && (
                <span className="line-clamp-1 min-w-0 text-[11.5px] leading-[1.5] text-[#9aa1b1]">
                  {member.agent_description}
                </span>
              )}
              <span
                className={cn(
                  'min-w-0 break-words text-[12px] leading-[1.5]',
                  noRule ? 'text-[#8a5a12]' : 'text-[#5b6274]',
                )}
              >
                {noRule ? '还没写「什么情况交给他」，这一位基本选不中。' : member.routing_rule}
              </span>
              {member.example_queries.length > 0 && (
                <span className="text-[11px] leading-none text-[#b4bac8]">
                  {member.example_queries.length} 条示例问题
                </span>
              )}
            </div>
          </div>
        );
      })}
    </DispatcherCardShell>
  );
}

/** 「无法匹配时怎么办」的完整兜底策略：模式 + 固定话术 + 置信度门槛。 */
function DispatcherFallbackCard({
  dispatcher,
  onConfigure,
}: {
  dispatcher: DispatcherRead;
  onConfigure: () => void;
}) {
  const config = dispatcher.config;
  const defaultMember = dispatcher.members.find(
    (item) => item.member_agent_id === config.default_member_agent_id,
  );
  return (
    <DispatcherCardShell
      title="兜底策略"
      action={(
        <button
          type="button"
          onClick={onConfigure}
          className="shrink-0 whitespace-nowrap text-[12px] text-[#0f7268] hover:underline"
        >
          调整
        </button>
      )}
    >
      <div className="flex flex-col gap-[2px]">
        <span className="text-[11.5px] leading-none text-[#9aa1b1]">无法匹配时</span>
        <span className="text-[13px] leading-[1.5] text-[#18181a]">
          {dispatchFallbackLabel(config.fallback_mode)}
          {config.fallback_mode === 'default_member' && (
            <span className="text-[#5b6274]">
              {defaultMember ? ` · ${defaultMember.agent_name}` : ' · 未指定默认同事'}
            </span>
          )}
        </span>
      </div>
      {config.fallback_mode === 'reply' && config.fallback_reply.trim() && (
        <p className="m-0 rounded-[10px] bg-[#f7f8fa] px-[10px] py-[8px] text-[12px] leading-[1.6] text-[#5b6274]">
          {config.fallback_reply}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-[6px] text-[11px] leading-none text-[#9aa1b1]">
        <span className="rounded-[5px] bg-[#f7f8fa] px-[6px] py-[3px]">
          置信度门槛 {config.min_confidence}
        </span>
        <span className="rounded-[5px] bg-[#f7f8fa] px-[6px] py-[3px]">
          {config.offer_candidates ? '会列出候选同事' : '不列候选'}
        </span>
      </div>
    </DispatcherCardShell>
  );
}

/** 基础信息：创建时间 / 更新时间 / 广场状态。原先这些字段根本无处可看。
 *  （路由模型不重复列在这里 —— 它已经在 hero 状态行里，重复出现会让同一事实
 *   在页面上出现两次，也容易让「取值口径」日后漂移。） */
function DispatcherMetaCard({ dispatcher }: { dispatcher: DispatcherRead }) {
  const rows: Array<[string, string]> = [
    ['创建时间', formatDispatcherTime(dispatcher.created_at)],
    ['最近修改', formatDispatcherTime(dispatcher.updated_at)],
    ['广场状态', dispatcher.published_to_gallery ? '已发布到「员工小队」' : '未发布'],
  ];
  return (
    <DispatcherCardShell title="基础信息">
      <dl className="m-0 flex flex-col gap-[7px]">
        {rows.map(([label, value]) => (
          <div key={label} className="flex items-baseline justify-between gap-[12px]">
            <dt className="shrink-0 text-[11.5px] leading-none text-[#9aa1b1]">{label}</dt>
            <dd className="m-0 min-w-0 truncate text-right text-[12px] leading-[1.5] text-[#5b6274]">
              {value}
            </dd>
          </div>
        ))}
      </dl>
    </DispatcherCardShell>
  );
}

/** `2026-11-07T04:29:34.488845` → `2026-11-07 04:29`；解析失败原样返回。 */
function formatDispatcherTime(value?: string | null): string {
  if (!value) return '—';
  const parsed = parseBackendDateTime(value);
  if (Number.isNaN(parsed.getTime())) return value;
  const pad = (n: number) => `${n}`.padStart(2, '0');
  return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`;
}

function DashboardStat({ title, value, icon }: { title: string; value: number; icon: ReactNode }) {
  return (
    <div className="org-dashboard-card">
      <div className="ui-card-body p-[24px]">
        <span className="org-dashboard-icon">{icon}</span>
        <span className="text-[13px] text-muted-foreground">{title}</span>
        <strong>{value}</strong>
      </div>
    </div>
  );
}

function isEmptyDefaultKnowledgeBase(item: KnowledgeBaseRead): boolean {
  const hasRuntimeKnowledge = item.document_count > 0 || item.bucket_count > 0 || item.chunk_count > 0;
  if (!hasRuntimeKnowledge && item.metadata?.created_from_document_upload && !item.metadata?.source_document_id) {
    return true;
  }
  return (
    item.name === '默认知识库'
    && item.document_count === 0
    && item.bucket_count === 0
    && item.chunk_count === 0
  );
}

function toolActive(item: ToolRead): boolean {
  return item.binding_status ? item.binding_status === 'active' : item.enabled;
}

function MetricTile({ label, value }: { label: string; value: number }) {
  return (
    <div className="employee-metric-tile">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

type ProfileTabKey = 'work' | 'knowledge' | 'sop' | 'skills' | 'tools' | 'scheduled' | 'memories' | 'logs';

const PROFILE_TABS: {
  key: ProfileTabKey;
  label: string;
  Icon: ComponentType<SVGProps<SVGSVGElement>>;
  route: EnterpriseRoute;
}[] = [
  { key: 'work', label: '工作记录', Icon: IconProfileFile, route: EnterpriseRoute.Dashboard },
  { key: 'knowledge', label: '知识库', Icon: IconFolder, route: EnterpriseRoute.Knowledge },
  { key: 'sop', label: 'SOP', Icon: IconCapClipboard, route: EnterpriseRoute.Skills },
  { key: 'skills', label: '技能', Icon: IconCapMagicWand, route: EnterpriseRoute.GeneralSkills },
  { key: 'tools', label: '工具', Icon: IconCapBriefcase, route: EnterpriseRoute.Tools },
  { key: 'scheduled', label: '定时任务', Icon: IconProfileAlarm, route: EnterpriseRoute.ScheduledTasks },
  { key: 'memories', label: '记忆', Icon: IconProfileHistory, route: EnterpriseRoute.Memories },
  { key: 'logs', label: '对话日志', Icon: IconProfileCalendar, route: EnterpriseRoute.Feedback },
];

function EmployeeProfileTabs({ value, onChange }: { value: ProfileTabKey; onChange: (value: ProfileTabKey) => void }) {
  return (
    <Tabs
      value={value}
      onValueChange={(next) => onChange(next as ProfileTabKey)}
      className="flex w-full flex-col items-start pl-[226px] max-[900px]:pl-1"
    >
      <TabsList
        aria-label="个人档案分区"
        className="h-[35px]! max-w-full gap-1 overflow-x-auto rounded-none bg-transparent p-0 scrollbar-hide"
      >
        {PROFILE_TABS.map(({ key, label, Icon }) => (
          <TabsTrigger key={key} value={key} className={PROFILE_TAB_TRIGGER_CLASS}>
            <Icon />
            {label}
          </TabsTrigger>
        ))}
      </TabsList>
    </Tabs>
  );
}

function HeroMetric({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex flex-1 items-end gap-1 rounded-[10px] bg-[#f6f6f6] px-5 py-2">
      <strong className="text-[14px] leading-none font-medium text-[#18181a]">{value}</strong>
      <span className="text-[12px] leading-none text-[#464c5e]">{label}</span>
    </div>
  );
}

function compactSummary(value: string, maxLength: number): string {
  const compact = value.replace(/\s+/g, ' ').trim();
  return compact.length > maxLength ? `${compact.slice(0, maxLength)}...` : compact;
}
