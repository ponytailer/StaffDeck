import { UnderlineTabs, type UnderlineTabItem } from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';

import IconPlus from '../assets/icons/plus.svg?react';
import IconSearch from '../assets/icons/search.svg?react';
import { SearchEmptyState } from '@/components/SearchEmptyState';

import { useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';

import { api, TENANT_ID } from '../api/client';
import { type EnterpriseAuthUser } from '../auth';

import AppHeader from '../components/AppHeader';
import { ConfirmDialog } from '../components/ConfirmDialog';
import EmployeeAvatarEditor from '../components/EmployeeAvatarEditor';
import AgentShareDialog from '../components/AgentShareDialog';
import EmployeeApiKeyDialog from '../components/EmployeeApiKeyDialog';
import EmployeeCard from '../components/EmployeeCard';
import DispatcherDetailPanel from '../components/dispatcher/DispatcherDetailPanel';
import DispatcherEditorDialog from '../components/dispatcher/DispatcherEditorDialog';
import DispatcherListRow from '../components/dispatcher/DispatcherListRow';
import DispatcherOverviewBar from '../components/dispatcher/DispatcherOverviewBar';
import MyCreatedSkillsPanel from '../components/MyCreatedSkillsPanel';
import MyCreatedToolsPanel from '../components/MyCreatedToolsPanel';
import MyStaticAssetsPanel from '../components/MyStaticAssetsPanel';
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
import { EnterpriseRoute } from '../enums/routes';
import {
  deleteDispatcher,
  dispatcherIssueCount,
  fetchDispatcherOverview,
  listDispatchers,
  publishDispatcher,
  unpublishDispatcher,
  type DispatcherActivityRead,
  type DispatcherOverviewRead,
  type DispatcherRead,
} from '../lib/dispatcher';

const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';

export default function AgentsPage({
  currentUser,
  isAdmin = false,
  onCreateAgent,
  onLogout,
  mode = 'all',
}: {
  currentUser?: EnterpriseAuthUser;
  isAdmin?: boolean;
  onCreateAgent?: () => void;
  onLogout?: () => void;
  /**
   * `all` = 数字员工页（员工/技能/知识库/工具/静态资源 五个 tab）。
   * `dispatchers` = 侧栏一级菜单「我的小队」，只渲染小队面板、不渲染 tab 栏。
   * 两者复用同一套状态与弹窗，避免把 300 行小队逻辑复制一份。
   */
  mode?: 'all' | 'dispatchers';
}) {
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  // 调度入口不在 /api/enterprise/agents 里（那接口按 kind 之外的口径返回普通员工），
  // 走自己的列表接口，这里独立维护。
  const [dispatchers, setDispatchers] = useState<DispatcherRead[]>([]);
  const [dispatcherEditorOpen, setDispatcherEditorOpen] = useState(false);
  const [editingDispatcher, setEditingDispatcher] = useState<DispatcherRead | null>(null);
  /** 打开编辑弹窗时滚到哪一段（档案页「去配置/调整」深链带过来） */
  const [dispatcherFocus, setDispatcherFocus] = useState<'members' | 'fallback' | null>(null);
  const [dispatcherDeleteTarget, setDispatcherDeleteTarget] = useState<DispatcherRead | null>(null);
  const [deletingDispatcher, setDeletingDispatcher] = useState(false);
  // 正在发布/下架的多智能体 id（同一时刻只处理一个，按钮显示「处理中…」）
  const [publishingDispatcherId, setPublishingDispatcherId] = useState<string | null>(null);
  // 「我的小队」页的 master-detail：左列表选中谁，右详情就显示谁。
  // 单独存 id 而不是对象：列表刷新（增删改）后不会拿到过期快照。
  const [selectedDispatcherId, setSelectedDispatcherId] = useState<string | null>(null);
  // 概览（统计卡 + 转接活动）走独立聚合接口，慢一点也不能挡住列表渲染
  const [dispatcherOverview, setDispatcherOverview] = useState<DispatcherOverviewRead | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(false);
  // 「只看需要处理的」：健康度条点进来时打开，左列表只留配置有问题的入口
  const [attentionOnly, setAttentionOnly] = useState(false);
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
  // 页面级 tab：数字员工 / 技能管理 / 知识库 / 工具管理 / 静态资源。
  // 「员工小队」已升为侧栏一级菜单「我的小队」（/enterprise/squads），不再占 tab。
  const [pageTab, setPageTab] = useState<
    'employees' | 'skills' | 'knowledge' | 'tools' | 'assets'
  >(() => {
    if (window.location.hash.includes('knowledge')) return 'knowledge';
    if (window.location.hash.includes('skills')) return 'skills';
    if (window.location.hash.includes('tools')) return 'tools';
    if (window.location.hash.includes('assets')) return 'assets';
    return 'employees';
  });
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(
    () => readEmployeeScope() || null,
  );
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  // 从员工小队档案页「去配置 / 调整」带过来的深链参数：
  // ?edit=<dispatcherId>&focus=members|fallback#dispatchers
  const pendingEditId = searchParams.get('edit');
  const pendingFocus = searchParams.get('focus');
  // 已经消费过就别再弹：列表刷新 / 保存后重拉都会让下面的 effect 再跑一次，
  // 没有这个闸门会把用户刚关掉的弹窗又弹出来。
  const consumedEditIdRef = useRef<string | null>(null);

  // 旧链接 `/enterprise/agents#dispatchers` 仍要能用：小队升为一级菜单后
  // 这里直接重定向过去（保留 hash 语义）。其余 hash 仍是本页的 tab 深链。
  useEffect(() => {
    const hash = location.hash.replace(/^#/, '');
    if (!hash) return;
    if (hash.startsWith('dispatchers')) {
      navigate(`${EnterpriseRoute.Squads}${location.search}`, { replace: true });
      return;
    }
    if (hash.startsWith('skills')) setPageTab('skills');
    else if (hash.startsWith('knowledge')) setPageTab('knowledge');
    else if (hash.startsWith('assets')) setPageTab('assets');
    else if (hash.startsWith('tools')) setPageTab('tools');
  }, [location.hash, location.search, navigate]);

  async function loadOverview() {
    setOverviewLoading(true);
    try {
      const payload = await fetchDispatcherOverview();
      // 保守校验：接口异常/网关拦截时可能回来一个形状不对的东西（数组、null、HTML），
      // 直接塞进 state 会让 `overview.activity.find` 抛错并整棵树卸载 —— 宁可当没拿到。
      const valid =
        payload &&
        typeof payload === 'object' &&
        typeof payload.squads === 'number' &&
        Array.isArray(payload.activity);
      setDispatcherOverview(valid ? payload : null);
    } catch {
      // 概览是「锦上添花」：拿不到就退回列表已知的数字，不弹错误打断主流程
      setDispatcherOverview(null);
    } finally {
      setOverviewLoading(false);
    }
  }

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
      if (mode === 'dispatchers') void loadOverview();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载员工失败');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  // master-detail 的选中项：列表到货后自动选中第一个；选中的被删掉/被筛掉时回落。
  // 不存整行对象 —— 列表刷新后会拿到过期快照（改了名字详情不更新）。
  useEffect(() => {
    if (mode !== 'dispatchers') return;
    if (selectedDispatcherId && dispatchers.some((row) => row.id === selectedDispatcherId)) return;
    setSelectedDispatcherId(dispatchers[0]?.id ?? null);
  }, [mode, dispatchers, selectedDispatcherId]);

  const dispatchersWithIssues = useMemo(
    () => dispatchers.filter((row) => dispatcherIssueCount(row) > 0),
    [dispatchers],
  );
  const visibleDispatchers = attentionOnly ? dispatchersWithIssues : dispatchers;
  const selectedDispatcher = dispatchers.find((row) => row.id === selectedDispatcherId) || null;
  const selectedActivity: DispatcherActivityRead | undefined =
    (dispatcherOverview?.activity || []).find((item) => item.id === selectedDispatcherId);
  const activityById = useMemo(() => {
    const map = new Map<string, DispatcherActivityRead>();
    for (const item of dispatcherOverview?.activity || []) map.set(item.id, item);
    return map;
  }, [dispatcherOverview]);

  /** 健康度条 → 只看需要处理的（同时选中第一个有问题的入口，右栏立刻有内容）。 */
  function focusAttention() {
    setAttentionOnly(true);
    setSelectedDispatcherId(dispatchersWithIssues[0]?.id ?? selectedDispatcherId);
  }

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
    // 参数已被消费（URL 里没有 edit 了）→ 放开闸门，让下一次从档案页
    // 点同一个小队的「去配置」还能正常打开。否则第二次会被 ref 挡住。
    if (!pendingEditId) {
      consumedEditIdRef.current = null;
      return;
    }
    if (consumedEditIdRef.current === pendingEditId) return;
    // 冷启动竞态：dispatchers 要等远程 PG 往返（实测 3~8s）。列表没到时
    // **不能**清参数，否则等它到了就没有机会打开弹窗了 —— 静默返回即可。
    const row = dispatchers.find((item) => item.id === pendingEditId);
    if (!row) return;

    consumedEditIdRef.current = pendingEditId;
    setEditingDispatcher(row);
    setDispatcherEditorOpen(true);
    setDispatcherFocus(pendingFocus === 'fallback' ? 'fallback' : 'members');
    // 消费掉 URL 参数：用 navigate 的对象形式**显式保留 #dispatchers**
    // （setSearchParams 只改 search，但它走 navigate('?...') 字符串形式，
    //   hash 会不会被带上取决于版本 —— 不如写死，免得页面悄悄跳回数字员工 tab）。
    // replace 让浏览器后退不会又停在带参数的地址上。
    const next = new URLSearchParams(searchParams);
    next.delete('edit');
    next.delete('focus');
    const search = next.toString();
    navigate(
      {
        pathname: location.pathname,
        search: search ? `?${search}` : '',
        hash: location.hash,
      },
      { replace: true },
    );
  }, [pendingEditId, pendingFocus, dispatchers, searchParams, navigate, location.pathname, location.hash]);

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
      notify.error(error instanceof Error ? error.message : '打开员工小队失败');
    } finally {
      setSelectingAgentId(null);
    }
  }

  /**
   * 进小队档案页：只切作用域，**不进对话**。
   *
   * 与 `selectDispatcher` 的唯一差别是落点（`/enterprise/dashboard` 而非对话草稿页），
   * 其余三件事（本地选中态 / 持久化作用域 / 广播）必须一致 —— 少任何一步，
   * 档案页会因为作用域没落库而被兜底改写成某位员工（历史上踩过）。
   */
  async function openDispatcherProfile(row: DispatcherRead) {
    if (selectingAgentId) return;
    setSelectingAgentId(row.id);
    try {
      setSelectedAgentId(row.id);
      persistSharedAgentScope(row.id, currentUser?.id);
      emitAgentScopeChange(row.id);
      navigate('/enterprise/dashboard');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '打开员工小队档案失败');
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
    setDispatcherFocus(null);
    setDispatcherEditorOpen(true);
  }

  async function confirmDispatcherDelete() {
    const row = dispatcherDeleteTarget;
    if (!row) return;
    setDeletingDispatcher(true);
    try {
      await deleteDispatcher(row.id);
      notify.success('员工小队已删除');
      setDispatcherDeleteTarget(null);
      setDispatchers((current) => current.filter((item) => item.id !== row.id));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除员工小队失败');
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


      {mode === 'all' && (
        <UnderlineTabs
          className="mb-[16px]"
          aria-label="我的数字员工页面切换"
          value={pageTab}
          onChange={setPageTab}
          items={[
            { value: 'employees', label: '数字员工' },
            { value: 'skills', label: '技能管理' },
            { value: 'knowledge', label: '知识库' },
            { value: 'tools', label: '工具管理' },
            { value: 'assets', label: '静态资源' },
          ]}
        />
      )}

      {mode === 'dispatchers' ? (
        // 小队是「只路由不干活」的另一类对象：混进员工网格会让员工总数虚高、
        // 也容易被误以为能配知识库。独立成一级菜单后仍复用这里的全部状态与弹窗。
        // 注意必须在三元链最前面判断：此时 pageTab 仍是默认的 'employees'。
        <div className="mt-[28px]" aria-label="员工小队">
          <div className="mb-[14px] flex flex-wrap items-baseline gap-[10px]">
            <h1 className="m-0 text-[22px] font-semibold leading-[30px] text-[#18181a]">我的小队</h1>
            <span className="text-[12px] text-[#858b9c]">{dispatchers.length} 个</span>
          </div>
          <p className="mb-[16px] text-[12px] leading-[18px] text-[#757f9c]">
            一个入口把问题路由给合适的同事，本身不回答业务问题。
          </p>

          <DispatcherOverviewBar
            overview={dispatcherOverview}
            loading={overviewLoading || loading}
            squadCount={dispatchers.length}
            squadWithIssues={dispatchersWithIssues.length}
            onAttention={focusAttention}
          />

          {dispatchers.length === 0 ? (
            <SearchEmptyState
              height="h-[280px]"
              title="还没有员工小队"
              description="选 2~3 位同事并写清各自负责什么，就能把问题路由给他们"
              icon={
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" className="shrink-0">
                  <circle cx="8" cy="3.2" r="1.9" />
                  <circle cx="3.4" cy="12.4" r="1.9" />
                  <circle cx="12.6" cy="12.4" r="1.9" />
                  <path d="M7 4.8 4.2 10.9M9 4.8l2.8 6.1" />
                </svg>
              }
              action={
                // 空态时左列表不渲染，「新建小队」必须在这里 —— 否则整个页面没有创建入口
                <button
                  type="button"
                  onClick={() => openDispatcherEditor(null)}
                  className="inline-flex h-[34px] items-center gap-[5px] rounded-[9px] border border-[#cfe7e2] bg-[#eef7f5] px-[14px] text-[12.5px] font-medium text-[#0f5f57] transition-colors hover:bg-[#e4f3ef]"
                >
                  <IconPlus className="size-[14px]" />
                  新建小队
                </button>
              }
            />
          ) : (
            // master-detail：左列表永远是窄栏（1 个入口时也就一行 + 新建按钮），
            // 右详情撑满剩余宽度 —— 这样「只有 1 个入口 → 卡片下面一大片空白」不再出现。
            <div className="flex flex-col gap-[14px] lg:flex-row lg:items-start">
              <aside className="w-full shrink-0 rounded-[18px] border border-black/[0.05] bg-white p-[8px] shadow-[0_6px_20px_rgba(17,17,17,0.04)] lg:sticky lg:top-[16px] lg:w-[276px]">
                {/* 「新建小队」放在列表**上方**且不在滚动容器里：小队一多，列表会滚，
                    按钮放底部就会被推到折叠线以下（用户反馈）。 */}
                <button
                  type="button"
                  onClick={() => openDispatcherEditor(null)}
                  className="mb-[6px] flex w-full items-center gap-[7px] rounded-[12px] px-[8px] py-[7px] text-[12.5px] font-medium text-[#0f5f57] transition-colors hover:bg-[#f4faf8]"
                >
                  <span className="grid size-[22px] shrink-0 place-items-center rounded-[7px] border border-[#cfe7e2] bg-[#eef7f5]">
                    <IconPlus className="size-[12px]" />
                  </span>
                  新建小队
                </button>
                {attentionOnly && (
                  <div className="mb-[6px] flex items-center justify-between gap-[6px] rounded-[10px] bg-[#fdf8ee] px-[9px] py-[6px]">
                    <span className="text-[11px] text-[#8a5a12]">
                      只看需要处理的（{dispatchersWithIssues.length}）
                    </span>
                    <button
                      type="button"
                      onClick={() => setAttentionOnly(false)}
                      className="shrink-0 text-[11px] text-[#8a5a12] underline-offset-2 hover:underline"
                    >
                      清除
                    </button>
                  </div>
                )}
                <div
                  className="flex flex-col gap-[2px] lg:max-h-[calc(100vh-360px)] lg:overflow-y-auto"
                  role="listbox"
                  aria-label="小队列表"
                >
                  {visibleDispatchers.map((row) => (
                    <DispatcherListRow
                      key={row.id}
                      dispatcher={row}
                      selected={row.id === selectedDispatcherId}
                      busy={selectingAgentId === row.id}
                      issueCount={dispatcherIssueCount(row)}
                      transferCount={activityById.get(row.id)?.transfers}
                      onSelect={() => setSelectedDispatcherId(row.id)}
                    />
                  ))}
                  {visibleDispatchers.length === 0 && (
                    <p className="m-0 px-[10px] py-[8px] text-[11.5px] leading-[17px] text-[#858b9c]">
                      没有需要处理的入口 —— 都配好了。
                    </p>
                  )}
                </div>
              </aside>

              <div className="min-w-0 flex-1">
                <DispatcherDetailPanel
                  dispatcher={selectedDispatcher}
                  activity={selectedActivity}
                  busy={Boolean(selectedDispatcher && selectingAgentId === selectedDispatcher.id)}
                  publishing={Boolean(selectedDispatcher && publishingDispatcherId === selectedDispatcher.id)}
                  onCreate={() => openDispatcherEditor(null)}
                  onChat={() => {
                    if (selectedDispatcher) void selectDispatcher(selectedDispatcher);
                  }}
                  onEdit={() => {
                    if (selectedDispatcher) openDispatcherEditor(selectedDispatcher);
                  }}
                  onProfile={() => {
                    if (selectedDispatcher) void openDispatcherProfile(selectedDispatcher);
                  }}
                  onDelete={() => {
                    if (selectedDispatcher) setDispatcherDeleteTarget(selectedDispatcher);
                  }}
                  onGallery={(published) => {
                    if (selectedDispatcher) void updateDispatcherGalleryState(selectedDispatcher, published);
                  }}
                />
              </div>
            </div>
          )}
        </div>
      ) : pageTab === 'employees' ? (
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
      ) : pageTab === 'skills' ? (
        <div className="mt-[24px]">
          <div className="flex items-center justify-end mb-[16px]">
            <UIButton
              data-guide-target="skills-create"
              type="button"
              onClick={() => navigate('/enterprise/general-skills/new?scope=gallery&return=employee_tab')}
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
      ) : pageTab === 'tools' ? (
        <div className="mt-[24px]">
          <div className="flex flex-wrap items-center justify-end gap-[10px] mb-[16px]">
            <UIButton
              type="button"
              variant="outline"
              onClick={() => navigate('/enterprise/tools/import/swagger?return=employee_tab')}
              className="h-[38px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[20px] text-[13px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]"
            >
              从 Swagger 导入
            </UIButton>
            <UIButton
              type="button"
              onClick={() => navigate('/enterprise/tools/new?return=employee_tab')}
              className="h-[38px] rounded-[10px] bg-[#18181a] px-[20px] text-[13px] font-normal text-white hover:bg-[#303030]"
            >
              <IconPlus className="size-[16px]" />
              新建工具
            </UIButton>
          </div>
          {/* 我创建的工具：跨作用域汇总（广场工具 + 挂在员工下的工具）。 */}
          <MyCreatedToolsPanel agents={agents} />
        </div>
      ) : (
        <div className="mt-[24px]">
          {/* 静态资源：上传即得一个固定下载地址，可粘进知识库让员工把链接回给用户。 */}
          <MyStaticAssetsPanel embedded />
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
        focusSection={dispatcherFocus ?? undefined}
        onClose={() => {
          setDispatcherEditorOpen(false);
          setEditingDispatcher(null);
          setDispatcherFocus(null);
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
        title={`删除员工小队「${dispatcherDeleteTarget?.name || ''}」？`}
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
