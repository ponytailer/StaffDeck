import { UnderlineTabs, type UnderlineTabItem } from '@/components/ui';
import { notify } from '@/components/ui/app-toast';

import IconSearch from '../assets/icons/search.svg?react';
import { SearchEmptyState } from '@/components/SearchEmptyState';

import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api, TENANT_ID } from '../api/client';
import { isGalleryEmployee, type EnterpriseAuthUser } from '../auth';

import AppHeader from '../components/AppHeader';
import { ConfirmDialog } from '../components/ConfirmDialog';
import EmployeeAvatarEditor from '../components/EmployeeAvatarEditor';
import EmployeeCard from '../components/EmployeeCard';
import EmployeeProfileEditor from '../components/EmployeeProfileEditor';
import DispatcherGalleryCard from '../components/dispatcher/DispatcherGalleryCard';
import { RevealGroup } from '../components/RevealGroup';
import MyCreatedSkillsPanel from '../components/MyCreatedSkillsPanel';
import {
  canManageEmployeeAgent,
  employeeDisplayName,
  employeeDisplayNameWithCreator,
  employeeProfile,
  isMyEmployeeAgent,
  visibleEmployeeAgents,
} from '../employee';
import {
  activateAgentScope,
  emitAgentRosterRefresh,
  emitAgentScopeChange,
  ENTERPRISE_AGENT_STORAGE_KEY,
  onAgentRosterRefresh,
} from '../lib/agent-scope-storage';
import { listDispatchers, type DispatcherRead } from '../lib/dispatcher';
import { EnterpriseRoute } from '../enums/routes';
import type { AgentProfileRead } from '../types';

type GalleryScope = 'all' | 'mine' | 'squads' | 'gallery';

export default function EmployeeGalleryPage({
  currentUser,
  isAdmin = false,
  onStartChat,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  isAdmin?: boolean;
  onStartChat?: (agent: AgentProfileRead) => void | Promise<void>;
  onLogout?: () => void;
}) {
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [avatarAgent, setAvatarAgent] = useState<AgentProfileRead | null>(null);
  const [profileAgent, setProfileAgent] = useState<AgentProfileRead | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<AgentProfileRead | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [startingAgentId, setStartingAgentId] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [scope, setScope] = useState<GalleryScope>('all');
  // 小队是另一类对象（kind=dispatcher），不在 /api/enterprise/agents 里，
  // 只有切到「我的小队」tab 时才拉 —— 别让进页面就多等一次远程 PG（3~8s）。
  const [squads, setSquads] = useState<DispatcherRead[]>([]);
  const [squadsLoaded, setSquadsLoaded] = useState(false);
  const [squadsLoading, setSquadsLoading] = useState(false);
  const [startingSquadId, setStartingSquadId] = useState<string | null>(null);
  const navigate = useNavigate();

  async function loadSquads() {
    setSquadsLoading(true);
    try {
      setSquads(await listDispatchers());
      setSquadsLoaded(true);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载员工小队失败');
    } finally {
      setSquadsLoading(false);
    }
  }

  async function load() {
    setLoading(true);
    try {
      const rows = await api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`);
      setAgents(rows);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载员工失败');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  // 员工列表按页面各自拉取：创建 / 编辑 / 上下线 / 删除发生在别的页面或 App
  // 侧边栏弹窗时，本页收不到任何信号，会一直显示 mount 时的旧快照。订阅花名册
  // 广播，避免新建员工后必须手动刷新页面才能看到。
  useEffect(
    () =>
      onAgentRosterRefresh(() => {
        void load();
      }),
    [],
  );

  // Keep these tabs aligned with the rest of the app:
  // - 所有员工: employees the current user can access and chat with
  // - 我的数字员工: employees the current user can manage/edit
  // - 数字员工广场: public employees not already listed as mine
  const availableAgents = useMemo(
    () => visibleEmployeeAgents(agents, currentUser, { activeOnly: true }),
    [agents, currentUser],
  );
  const myEmployees = useMemo(
    () => availableAgents.filter((item) => isMyEmployeeAgent(item, currentUser)),
    [availableAgents, currentUser],
  );
  const galleryEmployees = useMemo(() => {
    const myIds = new Set(myEmployees.map((item) => item.id));
    return availableAgents.filter((item) => isGalleryEmployee(item) && !myIds.has(item.id));
  }, [availableAgents, myEmployees]);

  const scopedEmployees = scope === 'mine'
    ? myEmployees
    : scope === 'gallery'
      ? galleryEmployees
      : availableAgents;

  // 小队也用同一个搜索框：名字 / 介绍 / 成员名任一命中即可
  const filteredSquads = useMemo(() => {
    const keyword = searchTerm.trim().toLowerCase();
    if (!keyword) return squads;
    return squads.filter((row) => [
      row.name,
      row.description || '',
      ...row.members.map((member) => member.agent_name || ''),
    ].some((value) => value.toLowerCase().includes(keyword)));
  }, [squads, searchTerm]);

  const filteredEmployees = scopedEmployees.filter((item) => {
    const profile = employeeProfile(item);
    const keyword = searchTerm.trim().toLowerCase();
    if (!keyword) return true;
    return [
      employeeDisplayName(item),
      employeeDisplayNameWithCreator(item),
      profile.roleName,
      item.description || '',
      profile.workStyles.join(' '),
      profile.expertiseTags.join(' '),
    ].some((value) => value.toLowerCase().includes(keyword));
  });

  async function startEmployeeChat(row: AgentProfileRead) {
    if (startingAgentId) return;
    setStartingAgentId(row.id);
    try {
      if (onStartChat) {
        await onStartChat(row);
        return;
      }
      navigate(`/workspace/chat/draft/${row.id}`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '发起对话失败');
    } finally {
      setStartingAgentId(null);
    }
  }

  /**
   * 进小队对话：**必须先切作用域**再 navigate。
   * 只 navigate 时，对话页会把这个 id 当成「不在花名册里的员工」而兜底改写回某一位
   * 员工（历史踩过，表现是「点了小队却进了别人的对话」）。切作用域的三步收敛在
   * `activateAgentScope` 里，与「我的小队」页共用。
   */
  function startDispatcherChat(row: DispatcherRead) {
    if (startingSquadId) return;
    setStartingSquadId(row.id);
    try {
      activateAgentScope(row.id, currentUser?.id);
      navigate(`/workspace/chat/draft/${row.id}`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '发起对话失败');
    } finally {
      setStartingSquadId(null);
    }
  }

  /** 进小队档案页（只切作用域，不进对话）。 */
  function openDispatcherProfile(row: DispatcherRead) {
    activateAgentScope(row.id, currentUser?.id);
    navigate(EnterpriseRoute.Dashboard);
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
      const metadata: Record<string, unknown> = {
        ...(row.metadata || {}),
        published_to_gallery: published,
        gallery_published_at: published ? new Date().toISOString() : undefined,
        gallery_published_by: published ? currentUser?.username : undefined,
      };
      if (published) {
        delete metadata.gallery_unpublished_at;
        delete metadata.gallery_unpublished_by;
      }
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

  async function confirmDelete() {
    const row = deleteTarget;
    if (!row) return;
    setDeleting(true);
    try {
      await api.delete(`/api/enterprise/agents/${row.id}?tenant_id=${TENANT_ID}`);
      if (window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) === row.id) {
        const nextAgent = availableAgents.find((item) => item.id !== row.id && item.status === 'active')
          || availableAgents.find((item) => item.id !== row.id);
        if (nextAgent) {
          window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, nextAgent.id);
          emitAgentScopeChange(nextAgent.id);
        } else {
          window.localStorage.removeItem(ENTERPRISE_AGENT_STORAGE_KEY);
          emitAgentScopeChange('');
        }
      }
      notify.success('员工已删除');
      setDeleteTarget(null);
      // 本页也订阅了该事件，会自行重新拉取，无需再单独 await load()。
      emitAgentRosterRefresh();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除员工失败');
    } finally {
      setDeleting(false);
    }
  }

  function updateAgentInList(row: AgentProfileRead) {
    setAgents((current) => current.map((item) => (item.id === row.id ? row : item)));
  }

  const galleryTabs: UnderlineTabItem<GalleryScope>[] = [
    { value: 'all', label: '所有员工' },
    { value: 'mine', label: '我的数字员工' },
    // 小队也是「可以聊的对象」，只是它不干活、只负责把人分对 —— 放在「我的」这组里，
    // 不混进「所有员工」（混进去会让员工总数虚高，也容易被以为能给它配知识库）。
    { value: 'squads', label: '我的小队' },
    { value: 'gallery', label: '数字员工广场' },
  ];

  function changeScope(nextScope: GalleryScope) {
    setScope(nextScope);
    // 懒加载：只在第一次切到这个 tab 时拉，失败时 loaded 保持 false 以便重试
    if (nextScope === 'squads' && !squadsLoaded && !squadsLoading) {
      void loadSquads();
    }
  }

  const hasSearchTerm = Boolean(searchTerm.trim());
  const emptyText = hasSearchTerm ? '没有匹配的数字员工' : '暂无数字员工';
  const emptyDescription = hasSearchTerm
    ? '换个关键词，或切换员工分类再试试'
    : '当前分类还没有可用员工';

  // 「我的数字员工」还需要在下方挂「我创建的技能」维护区，所以把员工网格抽出来复用，
  // 避免在 mine / 其它两个 tab 之间复制同一段 JSX。
  const employeeGrid = (
    <RevealGroup
      className="grid auto-rows-[minmax(262px,auto)] grid-cols-1 content-start gap-[32px] sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4 max-[900px]:gap-[18px]"
      itemCount={filteredEmployees.length}
    >
      {filteredEmployees.map((employee) => (
        <EmployeeCard
          key={employee.id}
          employee={employee}
          busy={startingAgentId === employee.id}
          canManage={canManageEmployeeAgent(employee, currentUser)}
          showMenu={false}
          onOpen={() => void startEmployeeChat(employee)}
          onStatus={(status) => void updateStatus(employee, status)}
          onGallery={(published) => void updateGalleryState(employee, published)}
          onDelete={() => setDeleteTarget(employee)}
          onAvatar={() => setAvatarAgent(employee)}
          onEdit={() => setProfileAgent(employee)}
          onChat={() => void startEmployeeChat(employee)}
        />
      ))}
      {!filteredEmployees.length && (
        <EmployeeGalleryEmptyState title={emptyText} description={emptyDescription} />
      )}
    </RevealGroup>
  );

  /**
   * 「我的小队」tab 的网格。
   *
   * 与员工网格同样是 262px 网格卡，但卡片换成 `DispatcherGalleryCard`（小队没有
   * 知识库/技能/SOP，统计项改成 成员 / 被使用 / 兜底）。空态分三种：加载中、
   * 搜索无结果、一个小队都没有（给「去我的小队」的引导）。
   */
  const squadGrid = (
    <RevealGroup
      className="grid auto-rows-[minmax(262px,auto)] grid-cols-1 content-start gap-[32px] sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4 max-[900px]:gap-[18px]"
      itemCount={filteredSquads.length}
    >
      {filteredSquads.map((row) => (
        <DispatcherGalleryCard
          key={row.id}
          dispatcher={row}
          busy={startingSquadId === row.id}
          onChat={() => startDispatcherChat(row)}
          onProfile={() => openDispatcherProfile(row)}
        />
      ))}
      {squadsLoading && !filteredSquads.length && (
        <p className="col-span-full m-0 py-[44px] text-center text-[12px] text-[#858b9c]">
          正在加载员工小队…
        </p>
      )}
      {!squadsLoading && !filteredSquads.length && (
        hasSearchTerm ? (
          <SearchEmptyState
            title="没有匹配的员工小队"
            description="换个关键词，或清空搜索再试试"
          />
        ) : (
          <SearchEmptyState
            title="还没有员工小队"
            description="在「我的小队」里选 2~3 位同事、写清各自负责什么，就能把问题路由给他们"
            action={
              <button
                type="button"
                onClick={() => navigate(EnterpriseRoute.Squads)}
                className="inline-flex h-[32px] items-center rounded-[9px] border border-[#cfe7e2] bg-[#eef7f5] px-[14px] text-[12.5px] font-medium text-[#0f5f57] transition-colors hover:bg-[#e4f3ef]"
              >
                去我的小队
              </button>
            }
          />
        )
      )}
    </RevealGroup>
  );

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]" aria-busy={loading}>
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
        left={(
          <div className="flex h-[50px] w-full items-center gap-[6px] rounded-[20px] bg-white px-[20px] text-[#757F9C] shadow-[0_0_6px_rgba(0,0,0,0.05)]">
            <IconSearch className="size-[20px] shrink-0" />
            <input
              autoComplete="off"
              data-1p-ignore="true"
              data-lpignore="true"
              data-bwignore="true"
              value={searchTerm}
              onChange={(event) => setSearchTerm(event.target.value)}
              placeholder="搜索"
              aria-label="搜索数字员工"
              className="min-w-0 flex-1 border-0 bg-transparent text-[14px] text-[#18181A] outline-none placeholder:text-[#757F9C]"
            />
          </div>
        )}
      />

      <UnderlineTabs
        className="mt-[36px] mb-[16px] max-[560px]:w-full"
        aria-label="数字员工分类"
        value={scope}
        onChange={changeScope}
        items={galleryTabs}
        tabClassName="max-[560px]:min-h-[54px] max-[560px]:w-auto max-[560px]:flex-1 max-[560px]:px-[6px] max-[560px]:text-[12px] max-[560px]:leading-[16px]"
      />

      {scope === 'squads' ? (
        <section aria-label="我的小队">{squadGrid}</section>
      ) : scope === 'mine' ? (
        <>
          <section aria-label="我的数字员工">{employeeGrid}</section>
          <MyCreatedSkillsPanel agents={agents} searchTerm={searchTerm} />
        </>
      ) : (
        employeeGrid
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
      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        loading={deleting}
        title={`删除员工「${deleteTarget ? employeeDisplayName(deleteTarget) : ''}」？`}
        description="删除后该员工的所有配置将一并移除，操作不可撤销。"
        onConfirm={() => void confirmDelete()}
      />
    </div>
  );
}

function EmployeeGalleryEmptyState({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return <SearchEmptyState title={title} description={description} />;
}
