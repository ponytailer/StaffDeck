import { UnderlineTabs, type UnderlineTabItem } from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';

import IconPlus from '../assets/icons/plus.svg?react';
import IconSearch from '../assets/icons/search.svg?react';
import { SearchEmptyState } from '@/components/SearchEmptyState';

import { useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { api, TENANT_ID } from '../api/client';
import { type EnterpriseAuthUser } from '../auth';

import AppHeader from '../components/AppHeader';
import { ConfirmDialog } from '../components/ConfirmDialog';
import EmployeeAvatarEditor from '../components/EmployeeAvatarEditor';
import AgentShareDialog from '../components/AgentShareDialog';
import EmployeeApiKeyDialog from '../components/EmployeeApiKeyDialog';
import EmployeeCard from '../components/EmployeeCard';
import DispatcherCard from '../components/dispatcher/DispatcherCard';
import DispatcherEditorDialog from '../components/dispatcher/DispatcherEditorDialog';
import MyCreatedSkillsPanel from '../components/MyCreatedSkillsPanel';
import MyCreatedToolsPanel from '../components/MyCreatedToolsPanel';
import KnowledgeManagePage from './KnowledgePage';
import EmployeeProfileEditor from '../components/EmployeeProfileEditor';
import { RevealGroup } from '../components/RevealGroup';
import {
  canManageEmployeeAgent,
  canSelectCurrentEmployeeAgent,
  employeeDisplayName,
  employeeDisplayNameWithCreator,
  employeeProfile,
} from '../employee';
import {
  emitAgentRosterRefresh,
  emitAgentScopeChange,
  isTeamScope,
  onAgentRosterRefresh,
  persistSharedAgentScope,
  readEmployeeScope,
} from '../lib/agent-scope-storage';
import type { AgentProfileRead, ModelConfigRead } from '../types';
import {
  deleteDispatcher,
  listDispatchers,
  publishDispatcher,
  unpublishDispatcher,
  type DispatcherRead,
} from '../lib/dispatcher';

const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';

export default function AgentsPage({
  currentUser,
  isAdmin = false,
  onCreateAgent,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  isAdmin?: boolean;
  onCreateAgent?: () => void;
  onLogout?: () => void;
}) {
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  // 调度入口不在 /api/enterprise/agents 里（那接口按 kind 之外的口径返回普通员工），
  // 走自己的列表接口，这里独立维护。
  const [dispatchers, setDispatchers] = useState<DispatcherRead[]>([]);
  const [dispatcherEditorOpen, setDispatcherEditorOpen] = useState(false);
  const [editingDispatcher, setEditingDispatcher] = useState<DispatcherRead | null>(null);
  const [dispatcherDeleteTarget, setDispatcherDeleteTarget] = useState<DispatcherRead | null>(null);
  const [deletingDispatcher, setDeletingDispatcher] = useState(false);
  // 正在发布/下架的多智能体 id（同一时刻只处理一个，按钮显示「处理中…」）
  const [publishingDispatcherId, setPublishingDispatcherId] = useState<string | null>(null);
  const [models, setModels] = useState<ModelConfigRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [avatarAgent, setAvatarAgent] = useState<AgentProfileRead | null>(null);
  const [profileAgent, setProfileAgent] = useState<AgentProfileRead | null>(null);
  const [apiKeyAgent, setApiKeyAgent] = useState<AgentProfileRead | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<AgentProfileRead | null>(null);
  const [shareAgent, setShareAgent] = useState<AgentProfileRead | null>(null);
  // 非空表示「这次删除被多智能体挡住了」：弹窗切换成告知态，
  // 文案用后端返回的原话（里面已带上入口名字与「先解绑」的指引）。
  const [deleteBlockedBy, setDeleteBlockedBy] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [selectingAgentId, setSelectingAgentId] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [employeeFilter, setEmployeeFilter] = useState<'all' | 'online' | 'offline'>('all');
  // 页面级 tab：数字员工 / 多智能体 / 技能管理 / 知识库 / 工具管理。
  // 多智能体是与员工并列的一等对象（不是员工的一种），所以给它独立 tab，
  // 顺序紧跟数字员工、排在技能管理之前。
  const [pageTab, setPageTab] = useState<
    'employees' | 'dispatchers' | 'skills' | 'knowledge' | 'tools'
  >(() => {
    if (window.location.hash.includes('dispatchers')) return 'dispatchers';
    if (window.location.hash.includes('knowledge')) return 'knowledge';
    if (window.location.hash.includes('skills')) return 'skills';
    if (window.location.hash.includes('tools')) return 'tools';
    return 'employees';
  });
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(
    () => readEmployeeScope() || null,
  );
  const navigate = useNavigate();
  const location = useLocation();

  // 侧栏「成员与路由规则」用 `/enterprise/agents#dispatchers` 深链跳进来。
  // 从员工页点它属于「同路由换 hash」，组件不会重挂载 → 只能监听 hash 变化。
  useEffect(() => {
    const hash = location.hash.replace(/^#/, '');
    if (!hash) return;
    if (hash.startsWith('dispatchers')) setPageTab('dispatchers');
    else if (hash.startsWith('skills')) setPageTab('skills');
    else if (hash.startsWith('knowledge')) setPageTab('knowledge');
    else if (hash.startsWith('tools')) setPageTab('tools');
  }, [location.hash]);

  async function load() {
    setLoading(true);
    try {
      // 两个列表并行拉：调度入口是独立类型，混在 /agents 里会污染员工计数
      const [rows, dispatchRows, modelRows] = await Promise.all([
        api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`),
        listDispatchers().catch(() => [] as DispatcherRead[]),
        api
          .get<ModelConfigRead[]>(`/api/enterprise/model-configs?tenant_id=${TENANT_ID}`)
          .catch(() => [] as ModelConfigRead[]),
      ]);
      setAgents(rows);
      setDispatchers(dispatchRows);
      setModels(modelRows);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载员工失败');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  // 员工列表是每个页面各自拉取的，创建 / 编辑 / 上下线 / 删除发生在别的页面
  // 或 App 侧边栏弹窗时，本页收不到任何信号。订阅花名册广播，避免新建员工后
  // 必须手动刷新页面才能看到。
  useEffect(
    () =>
      onAgentRosterRefresh(() => {
        void load();
      }),
    [],
  );

  useEffect(() => {
    const handler = (event: Event) => {
      const next = (event as CustomEvent<{ agentId?: string }>).detail?.agentId || '';
      setSelectedAgentId(next && !isTeamScope(next) ? next : readEmployeeScope() || null);
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', handler);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', handler);
  }, []);

  const employees = useMemo(
    () => agents.filter((item) => !item.is_overall && canManageEmployeeAgent(item, currentUser)),
    [agents, currentUser],
  );
  // 员工 → 引用它的调度入口。一次 O(n·m) 的本地计算，比每张卡各拉一次
  // `/dispatchers/{id}/referenced-by` 便宜得多（远程 PG 单次往返 10ms 起）。
  // 数据源就是上面 load() 已经拿到的 dispatchers[].members，没有额外请求。
  const referencedByMemo = useMemo(() => {
    const map = new Map<string, Array<{ id: string; name: string }>>();
    for (const dispatcher of dispatchers) {
      for (const member of dispatcher.members) {
        const list = map.get(member.member_agent_id);
        if (list) {
          list.push({ id: dispatcher.id, name: dispatcher.name });
        } else {
          map.set(member.member_agent_id, [{ id: dispatcher.id, name: dispatcher.name }]);
        }
      }
    }
    return map;
  }, [dispatchers]);
  const offlineEmployees = employees.filter((item) => item.status !== 'active');
  const onlineEmployees = employees.filter((item) => item.status === 'active');
  const filteredEmployees = employees.filter((item) => {
    const profile = employeeProfile(item);
    const keyword = searchTerm.trim().toLowerCase();
    const matchesFilter = employeeFilter === 'all'
      || (employeeFilter === 'online' && item.status === 'active')
      || (employeeFilter === 'offline' && item.status !== 'active');
    if (!matchesFilter) return false;
    if (!keyword) return true;
    return [
      employeeDisplayName(item),
      employeeDisplayNameWithCreator(item),
      profile.roleName,
      item.description || '',
      profile.workStyles.join(' '),
    ].some((value) => value.toLowerCase().includes(keyword));
  });

  async function selectEmployee(row: AgentProfileRead) {
    if (selectingAgentId) return;
    setSelectingAgentId(row.id);
    try {
      let selectedRow = row;
      if (!canSelectCurrentEmployeeAgent(row, currentUser, { activeOnly: true })) {
        selectedRow = await api.post<AgentProfileRead>(
          `/api/chat/agents/${encodeURIComponent(row.id)}/use?tenant_id=${TENANT_ID}`,
          {},
        );
        updateAgentInList(selectedRow);
      }
      setSelectedAgentId(selectedRow.id);
      persistSharedAgentScope(selectedRow.id, currentUser?.id);
      emitAgentScopeChange(selectedRow.id);
      navigate('/enterprise/dashboard');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载员工失败');
    } finally {
      setSelectingAgentId(null);
    }
  }

  function startEmployeeChat(row: AgentProfileRead) {
    navigate(`/workspace/chat/draft/${row.id}`);
  }

  /** 调度入口直接进对话页：它就是唯一对话入口，不需要「先选用再聊」。 */
  async function selectDispatcher(row: DispatcherRead) {
    if (selectingAgentId) return;
    setSelectingAgentId(row.id);
    try {
      setSelectedAgentId(row.id);
      persistSharedAgentScope(row.id, currentUser?.id);
      emitAgentScopeChange(row.id);
      navigate(`/workspace/chat/draft/${row.id}`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '打开多智能体失败');
    } finally {
      setSelectingAgentId(null);
    }
  }

  async function updateStatus(row: AgentProfileRead, status: 'active' | 'archived') {
    try {
      await api.put<AgentProfileRead>(`/api/enterprise/agents/${row.id}`, {
        tenant_id: TENANT_ID,
        status,
        metadata: row.metadata || {},
      });
      notify.success(status === 'active' ? '员工已上线' : '员工已下线');
      // 本页也订阅了该事件，会自行重新拉取，无需再单独 await load()。
      emitAgentRosterRefresh();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新员工状态失败');
    }
  }

  async function updateGalleryState(row: AgentProfileRead, published: boolean) {
    try {
      const metadata = {
        ...(row.metadata || {}),
        published_to_gallery: published,
        gallery_published_at: published ? new Date().toISOString() : undefined,
        gallery_published_by: published ? currentUser?.username : undefined,
      };
      await api.put<AgentProfileRead>(`/api/enterprise/agents/${row.id}`, {
        tenant_id: TENANT_ID,
        metadata,
      });
      notify.success(published ? '已发布到广场' : '已从广场下架');
      // 本页也订阅了该事件，会自行重新拉取，无需再单独 await load()。
      emitAgentRosterRefresh();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新广场状态失败');
    }
  }

  /**
   * 多智能体的发/下架。走 dispatcher 自己的端点（不走 PUT agents），原因：
   * agents 的更新接口对 dispatcher 只放行极少数字段，metadata 那套是普通员工的口径。
   */
  async function updateDispatcherGalleryState(row: DispatcherRead, published: boolean) {
    setPublishingDispatcherId(row.id);
    try {
      const next = published ? await publishDispatcher(row.id) : await unpublishDispatcher(row.id);
      setDispatchers((current) => current.map((item) => (item.id === next.id ? next : item)));
      notify.success(published ? '已发布到开放广场「员工小队」' : '已从广场下架');
      // 广场页可能正在别处打开着，广播一次让角标与列表刷新。
      emitAgentRosterRefresh();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新广场状态失败');
    } finally {
      setPublishingDispatcherId(null);
    }
  }

  async function confirmDelete() {
    const row = deleteTarget;
    if (!row) return;
    setDeleting(true);
    try {
      await api.delete(`/api/enterprise/agents/${row.id}?tenant_id=${TENANT_ID}`);
      if (readEmployeeScope() === row.id) {
        const nextAgent = employees.find((item) => item.id !== row.id && item.status === 'active')
          || employees.find((item) => item.id !== row.id);
        if (nextAgent) {
          window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, nextAgent.id);
          window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: nextAgent.id } }));
        } else {
          window.localStorage.removeItem(ENTERPRISE_AGENT_STORAGE_KEY);
          window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: '' } }));
        }
      }
      notify.success('员工已删除');
      setDeleteTarget(null);
      // 本页也订阅了该事件，会自行重新拉取，无需再单独 await load()。
      emitAgentRosterRefresh();
    } catch (error) {
      // 409 = 这位员工还在某个多智能体里当成员。后端已经把入口名字拼进 detail，
      // 这里原样透出（不再补一句「请先解绑」，那句话后端已经写了），并给出指路。
      const message = error instanceof Error ? error.message : '删除员工失败';
      notify.error(message);
      // 弹窗别关、别静默失败：用户需要知道「去多智能体里解绑」这条下一步。
      setDeleteBlockedBy(message);
    } finally {
      setDeleting(false);
    }
  }

  function updateAgentInList(row: AgentProfileRead) {
    setAgents((current) => current.map((item) => (item.id === row.id ? row : item)));
  }

  function openDispatcherEditor(row: DispatcherRead | null) {
    setEditingDispatcher(row);
    setDispatcherEditorOpen(true);
  }

  async function confirmDispatcherDelete() {
    const row = dispatcherDeleteTarget;
    if (!row) return;
    setDeletingDispatcher(true);
    try {
      await deleteDispatcher(row.id);
      notify.success('多智能体已删除');
      setDispatcherDeleteTarget(null);
      setDispatchers((current) => current.filter((item) => item.id !== row.id));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除多智能体失败');
    } finally {
      setDeletingDispatcher(false);
    }
  }

  const employeeTabs: UnderlineTabItem<typeof employeeFilter>[] = [
    { value: 'all', label: '全部员工' },
    { value: 'online', label: '在线员工' },
    { value: 'offline', label: '下线员工' },
  ];

  const summaryCardClass =
    'flex h-[100px] flex-1 basis-[220px] items-center gap-[16px] rounded-[20px] bg-[#f6f6f6] px-[32px] py-[20px] text-left transition-shadow';
  const summaryStats: { key: typeof employeeFilter; value: number; label: string; sub: string }[] = [
    { key: 'all', value: employees.length, label: '员工总数', sub: `${onlineEmployees.length}位在线` },
    { key: 'offline', value: offlineEmployees.length, label: '下线员工', sub: '0位在线' },
  ];

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]" aria-busy={loading}>
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
      />


      <UnderlineTabs
        className="mb-[16px]"
        aria-label="我的数字员工页面切换"
        value={pageTab}
        onChange={setPageTab}
        items={[
          { value: 'employees', label: '数字员工' },
          { value: 'dispatchers', label: '多智能体' },
          { value: 'skills', label: '技能管理' },
          { value: 'knowledge', label: '知识库' },
          { value: 'tools', label: '工具管理' },
        ]}
      />

      {pageTab === 'employees' ? (
        <>
          <div className="flex flex-wrap items-stretch gap-[20px] my-[36px]" aria-label="数字员工统计">
        {summaryStats.map((stat) => (
          <button
            key={stat.key}
            type="button"
            aria-pressed={employeeFilter === stat.key}
            onClick={() => setEmployeeFilter(stat.key)}
            className={cn(
              summaryCardClass,
            )}
          >
            <span className="shrink-0 text-[34px] font-semibold leading-none text-[#18181A]">{stat.value}</span>
            <span className="flex min-w-0 flex-col gap-[4px]">
              <span className="whitespace-nowrap text-[14px] text-[#464C5E]">{stat.label}</span>
              <span className="whitespace-nowrap text-[12px] text-[#757F9C]">{stat.sub}</span>
            </span>
          </button>
        ))}
        <button data-guide-target="agents-create" type="button" onClick={onCreateAgent} className={cn(summaryCardClass, 'hover:shadow-[0_16px_30px_0_rgba(0,0,0,0.10)]')}>
          <span className="grid size-[38px] shrink-0 place-items-center text-[#18181A]">
            <IconPlus className="size-[38px]" />
          </span>
          <span className="flex min-w-0 flex-col gap-[4px]">
            <span className="whitespace-nowrap text-[14px] text-[#464C5E]">创建新员工</span>
            <span className="whitespace-nowrap text-[12px] text-[#757F9C]">几步搭好你的数字员工</span>
          </span>
        </button>
        </div>

      <div className="mb-[16px] flex h-[50px] w-full items-center gap-[6px] rounded-[20px] bg-white px-[20px] text-[#757F9C] shadow-[0_0_6px_rgba(0,0,0,0.05)]">
        <IconSearch className="size-[20px] shrink-0" />
        <input
          autoComplete="off"
          data-1p-ignore="true"
          data-lpignore="true"
          data-bwignore="true"
          value={searchTerm}
          onChange={(event) => setSearchTerm(event.target.value)}
          placeholder="搜索"
          aria-label="搜索员工"
          className="min-w-0 flex-1 border-0 bg-transparent text-[14px] text-[#18181A] outline-none placeholder:text-[#757F9C]"
        />
      </div>

      <UnderlineTabs
        className="mb-[16px]"
        aria-label="数字员工分类"
        value={employeeFilter}
        onChange={setEmployeeFilter}
        items={employeeTabs}
      />
      <RevealGroup
        className="grid auto-rows-[minmax(262px,auto)] grid-cols-1 content-start gap-[32px] sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4 max-[900px]:gap-[18px]"
        itemCount={filteredEmployees.length}
      >
        {filteredEmployees.map((employee) => (
          <EmployeeCard
            key={employee.id}
            employee={employee}
            busy={selectingAgentId === employee.id}
            canManage={canManageEmployeeAgent(employee, currentUser)}
            selected={employee.id === selectedAgentId}
            referencedBy={referencedByMemo.get(employee.id)}
            onOpen={() => void selectEmployee(employee)}
            onStatus={(status) => void updateStatus(employee, status)}
            onGallery={(published) => void updateGalleryState(employee, published)}
            onDelete={() => setDeleteTarget(employee)}
            onAvatar={() => setAvatarAgent(employee)}
            onEdit={() => setProfileAgent(employee)}
            onChat={() => startEmployeeChat(employee)}
            onApiKeys={() => setApiKeyAgent(employee)}
            onShare={() => setShareAgent(employee)}
          />
        ))}
        {!filteredEmployees.length && (
          <AgentsEmptyState />
        )}
      </RevealGroup>
        </>
      ) : pageTab === 'dispatchers' ? (
        // 多智能体是一等对象，独立成一个页面级 tab（与技能管理并列、排它之前）。
        // 它是「只路由不干活」的另一种类型：混进员工网格会让员工总数虚高，
        // 也容易被误以为能配知识库，所以从员工页彻底搬出来。
        <div className="mt-[24px]" aria-label="多智能体">
          {/* 不再重复一个「多智能体」大标题：tab 已经标明了当前位置，
              再写一遍就成了同一句话出现两次（技能管理/工具管理两颗 tab 也没有标题）。 */}
          <div className="mb-[16px] flex flex-wrap items-center justify-between gap-[12px]">
            <p className="text-[12px] leading-[18px] text-[#757f9c]">
              一个入口把问题路由给合适的同事，本身不回答业务问题。
            </p>
            <UIButton
              type="button"
              onClick={() => openDispatcherEditor(null)}
              className="h-[38px] shrink-0 rounded-[10px] border border-[#cfe7e2] bg-[#eef7f5] px-[18px] text-[13px] font-medium text-[#0f5f57] hover:bg-[#e4f3ef] hover:text-[#0f5f57]"
            >
              <IconPlus className="size-[16px]" />
              创建多智能体
            </UIButton>
          </div>
          {dispatchers.length > 0 ? (
            <RevealGroup
              className="grid auto-rows-[minmax(240px,auto)] grid-cols-1 content-start gap-[24px] sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4 max-[900px]:gap-[18px]"
              itemCount={dispatchers.length}
            >
              {dispatchers.map((row) => (
                <DispatcherCard
                  key={row.id}
                  dispatcher={row}
                  busy={selectingAgentId === row.id}
                  publishing={publishingDispatcherId === row.id}
                  onChat={() => void selectDispatcher(row)}
                  onEdit={() => openDispatcherEditor(row)}
                  onDelete={() => setDispatcherDeleteTarget(row)}
                  onGallery={(published) => void updateDispatcherGalleryState(row, published)}
                />
              ))}
            </RevealGroup>
          ) : (
            <SearchEmptyState
              height="h-[240px]"
              title="还没有多智能体"
              description="点右上角「创建多智能体」，选 2~3 位同事并写清各自负责什么"
              icon={
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" className="shrink-0">
                  <circle cx="8" cy="3.2" r="1.9" />
                  <circle cx="3.4" cy="12.4" r="1.9" />
                  <circle cx="12.6" cy="12.4" r="1.9" />
                  <path d="M7 4.8 4.2 10.9M9 4.8l2.8 6.1" />
                </svg>
              }
            />
          )}
        </div>
      ) : pageTab === 'skills' ? (
        <div className="mt-[24px]">
          <div className="flex items-center justify-end mb-[16px]">
            <UIButton
              data-guide-target="skills-create"
              type="button"
              onClick={() => navigate('/enterprise/general-skills/new?scope=gallery')}
              className="h-[38px] rounded-[10px] bg-[#18181a] px-[20px] text-[13px] font-normal text-white hover:bg-[#303030]"
            >
              <IconPlus className="size-[16px]" />
              创建技能
            </UIButton>
          </div>
          {/* 我创建的技能：跨作用域汇总（广场技能 + 挂在员工下的技能）。 */}
          <MyCreatedSkillsPanel agents={agents} />
        </div>
      ) : pageTab === 'knowledge' ? (
        <div className="mt-[24px]">
          {/* 知识库不再跟绑数字员工：个人空间在页内直接复用完整知识库页（表、图谱、检索调试）。 */}
          <KnowledgeManagePage embedded mode="personal" currentUser={currentUser} onLogout={onLogout} />
        </div>
      ) : (
        <div className="mt-[24px]">
          <div className="flex flex-wrap items-center justify-end gap-[10px] mb-[16px]">
            <UIButton
              type="button"
              variant="outline"
              onClick={() => navigate('/enterprise/tools/import/swagger')}
              className="h-[38px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[20px] text-[13px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]"
            >
              从 Swagger 导入
            </UIButton>
            <UIButton
              type="button"
              onClick={() => navigate('/enterprise/tools/new')}
              className="h-[38px] rounded-[10px] bg-[#18181a] px-[20px] text-[13px] font-normal text-white hover:bg-[#303030]"
            >
              <IconPlus className="size-[16px]" />
              新建工具
            </UIButton>
          </div>
          {/* 我创建的工具：跨作用域汇总（广场工具 + 挂在员工下的工具）。 */}
          <MyCreatedToolsPanel agents={agents} />
        </div>
      )}
      <EmployeeAvatarEditor
        agent={avatarAgent}
        open={Boolean(avatarAgent)}
        onClose={() => setAvatarAgent(null)}
        onSaved={updateAgentInList}
      />
      <EmployeeProfileEditor
        agent={profileAgent}
        open={Boolean(profileAgent)}
        currentUser={currentUser}
        onClose={() => setProfileAgent(null)}
        onSaved={updateAgentInList}
      />
      <EmployeeApiKeyDialog
        agent={apiKeyAgent}
        open={Boolean(apiKeyAgent)}
        onClose={() => setApiKeyAgent(null)}
      />
      <AgentShareDialog
        agent={shareAgent}
        open={Boolean(shareAgent)}
        onClose={() => setShareAgent(null)}
      />
      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => {
          if (!open) {
            setDeleteTarget(null);
            setDeleteBlockedBy(null);
          }
        }}
        loading={deleting}
        title={
          deleteBlockedBy
            ? '暂时不能删除这位员工'
            : `删除员工「${deleteTarget ? employeeDisplayName(deleteTarget) : ''}」？`
        }
        description={
          deleteBlockedBy
            ? deleteBlockedBy
            : '删除后该员工的所有配置将一并移除，操作不可撤销。'
        }
        // 被拦住时是「告知」而不是「确认」：不给二次删除按钮，避免用户反复撞同一个 409
        confirmText={deleteBlockedBy ? '知道了' : '删除'}
        destructive={!deleteBlockedBy}
        cancelText={deleteBlockedBy ? '关闭' : '取消'}
        onConfirm={() => {
          if (deleteBlockedBy) {
            setDeleteTarget(null);
            setDeleteBlockedBy(null);
            return;
          }
          void confirmDelete();
        }}
      />
      <DispatcherEditorDialog
        open={dispatcherEditorOpen}
        dispatcher={editingDispatcher}
        agents={agents}
        models={models}
        onClose={() => {
          setDispatcherEditorOpen(false);
          setEditingDispatcher(null);
        }}
        onSaved={(row) => {
          setDispatchers((current) => {
            const exists = current.some((item) => item.id === row.id);
            return exists
              ? current.map((item) => (item.id === row.id ? row : item))
              : [row, ...current];
          });
          // 新建的入口要立刻能出现在对话页花名册与侧边栏范围内
          emitAgentRosterRefresh();
        }}
      />
      <ConfirmDialog
        open={Boolean(dispatcherDeleteTarget)}
        onOpenChange={(open) => {
          if (!open) setDispatcherDeleteTarget(null);
        }}
        loading={deletingDispatcher}
        title={`删除多智能体「${dispatcherDeleteTarget?.name || ''}」？`}
        description="删除后该入口的路由配置将一并移除；下辖的数字员工本身不受影响。"
        onConfirm={() => void confirmDispatcherDelete()}
      />
    </div>
  );
}

function AgentsEmptyState() {
  return (
    <SearchEmptyState
      title="没有匹配的数字员工"
      description="调整筛选条件，或换个关键词再试试"
    />
  );
}
