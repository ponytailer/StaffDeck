import {
  FileSearchOutlined,
  ProfileOutlined,
  SolutionOutlined,
  ToolOutlined,
  UsergroupAddOutlined,
} from '../icons';
import { notify } from '@/components/ui';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import type { ComponentType, ReactNode, SVGProps } from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { api, TENANT_ID } from '../api/client';
import { isGalleryEmployee, type EnterpriseAuthUser } from '../auth';
import EmployeeAvatar from '../components/EmployeeAvatar';
import IconAgents from '../assets/icons/nav-agents.svg?react';
import IconFolder from '../assets/icons/cap-folder.svg?react';
import IconMagicWand from '../assets/icons/cap-magicwand.svg?react';
import IconClipboard from '../assets/icons/cap-clipboard.svg?react';
import IconBriefcase from '../assets/icons/cap-briefcase.svg?react';
import IconSearch from '../assets/icons/search.svg?react';
import IconRefresh from '../assets/icons/refresh.svg?react';
import IconAdd from '../assets/icons/add.svg?react';
import plazaKnowledgeIcon from '../assets/icons/plaza-knowledge.svg';
import plazaSkillIcon from '../assets/icons/plaza-skill.svg';
import plazaSopIcon from '../assets/icons/plaza-sop.svg';
import plazaToolIcon from '../assets/icons/plaza-tool.svg';
import {
  agentResourceCount,
  canManageEmployeeAgent,
  employeeDisplayNameWithCreator,
  employeeProfile,
  resourceDisplayNameWithCreator,
} from '../employee';
import type { AgentProfileRead, GeneralSkillRead, KnowledgeBaseRead, SkillRead, ToolRead } from '../types';

import AppHeader from '@/components/AppHeader';
import { Button as UIButton } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import {
  PlatformEmployeeCard,
  PlatformEmployeeDrawer,
  PlatformResourceCard,
  PlatformResourceDrawer,
  type PlatformResourceAccent,
  type PlatformStat,
} from '@/components/openPlatform';
import { isTeamScope, readEmployeeScope } from '@/lib/agent-scope-storage';

const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';

type PlatformKind = 'agents' | 'knowledge' | 'general-skills' | 'skills' | 'tools';

type PlatformConfig = {
  kind: PlatformKind;
  title: string;
  subtitle: string;
  detail: string;
  useLabel: string;
  metricLabel: string;
  signals: string[];
  icon: ReactNode;
};

type PlatformItem = {
  id: string;
  deleteKey?: string;
  title: string;
  description: string;
  meta: string;
  tags: string[];
  agent?: AgentProfileRead;
};

const PLATFORM_CONFIGS: PlatformConfig[] = [
  {
    kind: 'agents',
    title: '数字员工广场',
    subtitle: '已发布到广场，可在对话端直接使用。',
    detail: '选择一个数字员工查看能力、岗位和服务范围。',
    useLabel: '使用此员工',
    metricLabel: '数字员工',
    signals: ['聊天可用', '支持对话', '查看能力'],
    icon: <UsergroupAddOutlined />,
  },
  {
    kind: 'knowledge',
    title: '知识库广场',
    subtitle: '发布到广场的知识库，可复制到你的数字员工。',
    detail: '从广场复制到当前数字员工的知识库。',
    useLabel: '复制到知识库',
    metricLabel: '知识库',
    signals: ['知识图谱', '引用来源', '可复制'],
    icon: <FileSearchOutlined />,
  },
  {
    kind: 'general-skills',
    title: '技能广场',
    subtitle: '浏览器、MCP、查询工具等可复用能力。',
    detail: '从广场复制到当前数字员工的技能。',
    useLabel: '复制到技能',
    metricLabel: '技能',
    signals: ['运行测试', 'MCP/浏览器', '能力复用'],
    icon: <SolutionOutlined />,
  },
  {
    kind: 'skills',
    title: 'SOP 广场',
    subtitle: '可复制和复用的业务流程与执行规范。',
    detail: '从广场复制到当前数字员工的 SOP。',
    useLabel: '复制到 SOP',
    metricLabel: '业务 SOP',
    signals: ['流程推进', '执行规范', '可复制'],
    icon: <ProfileOutlined />,
  },
  {
    kind: 'tools',
    title: '工具广场',
    subtitle: '可开放给员工调用和测试的工具能力。',
    detail: '前往工具页按现有流程配置和测试工具。',
    useLabel: '前往工具页',
    metricLabel: '工具能力',
    signals: ['调用权限', '测试可用', '工具配置'],
    icon: <ToolOutlined />,
  },
];

const PLATFORM_BY_KIND = new Map(PLATFORM_CONFIGS.map((item) => [item.kind, item]));

// SD1 line glyph shown in each column header, matching the sidebar mapping.
const PLATFORM_ICON: Record<PlatformKind, ComponentType<SVGProps<SVGSVGElement>>> = {
  agents: IconAgents,
  knowledge: IconFolder,
  'general-skills': IconMagicWand,
  skills: IconClipboard,
  tools: IconBriefcase,
};

// Colorful 3D module icon shown on each广场 resource card (agents use avatars instead).
const PLATFORM_RESOURCE_ICON: Partial<Record<PlatformKind, string>> = {
  knowledge: plazaKnowledgeIcon,
  'general-skills': plazaSkillIcon,
  skills: plazaSopIcon,
  tools: plazaToolIcon,
};

// Per-module accent color for the resource card meta line and tag pills (SD1 232:4634).
const PLATFORM_ACCENT: Partial<Record<PlatformKind, PlatformResourceAccent>> = {
  knowledge: 'green',
  'general-skills': 'indigo',
  skills: 'blue',
  tools: 'orange',
};

// Unit rendered after the header count, e.g. "12 员工" / "12 内容".
function platformCountLabel(kind: PlatformKind): string {
  return kind === 'agents' ? '员工' : '内容';
}

// Bottom metric segments for a 数字员工广场 card.
function employeeStats(agent: AgentProfileRead): PlatformStat[] {
  return [
    { value: agentResourceCount(agent, 'knowledge_base'), label: '资料' },
    { value: agentResourceCount(agent, 'general_skill'), label: '技能' },
    { value: agentResourceCount(agent, 'skill'), label: 'SOP' },
  ];
}

function resourceDrawerBadge(kind: PlatformKind, item: PlatformItem): string {
  if (kind === 'skills') {
    const parts = item.meta.split(' / ');
    return parts[parts.length - 1] || item.tags[0] || '';
  }
  return item.tags[0] || '';
}

function DetailSkeleton({ kind }: { kind: PlatformKind }) {
  const cardHeight = kind === 'agents' ? 'h-[140px]' : 'h-[112px]';
  return (
    <div className="grid grid-cols-1 gap-[16px] sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5">
      {Array.from({ length: 8 }, (_, index) => (
        <div
          key={index}
          className={cn(
            'w-full animate-pulse rounded-[20px] border-[0.5px] border-[#f0f1f5] bg-[#f6f6f6]',
            cardHeight,
          )}
        />
      ))}
    </div>
  );
}

export default function OpenPlatformPage({
  currentUser,
  isAdmin = false,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  isAdmin?: boolean;
  onLogout?: () => void;
}) {
  const navigate = useNavigate();
  const { kind } = useParams<{ kind?: PlatformKind }>();
  const initialKind = kind && PLATFORM_BY_KIND.has(kind) ? kind : 'agents';
  const [activeKind, setActiveKind] = useState<PlatformKind>(initialKind);
  const [searchText, setSearchText] = useState('');
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseRead[]>([]);
  const [generalSkills, setGeneralSkills] = useState<GeneralSkillRead[]>([]);
  const [skills, setSkills] = useState<SkillRead[]>([]);
  const [tools, setTools] = useState<ToolRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [deletingItemKey, setDeletingItemKey] = useState('');
  const [agentId, setAgentId] = useState(readEmployeeScope);
  const [detailItem, setDetailItem] = useState<{ kind: PlatformKind; item: PlatformItem } | null>(null);
  const [confirmTarget, setConfirmTarget] = useState<{ kind: PlatformKind; item: PlatformItem } | null>(null);

  useEffect(() => {
    if (kind && PLATFORM_BY_KIND.has(kind)) {
      setActiveKind(kind);
    }
  }, [kind]);

  useEffect(() => {
    setSearchText('');
  }, [activeKind]);

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      const next = (event as CustomEvent<{ agentId?: string }>).detail?.agentId || '';
      setAgentId(next && !isTeamScope(next) ? next : readEmployeeScope());
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

  const loadPlatformData = useCallback(async () => {
    setLoading(true);
    try {
      const agentRows = await api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`);
      const overall = agentRows.find((item) => item.is_overall);
      const overallSuffix = overall ? `&agent_id=${encodeURIComponent(overall.id)}` : '';
      const [kbRows, generalRows, skillRows, toolRows] = await Promise.all([
        api.get<KnowledgeBaseRead[]>(`/api/enterprise/knowledge-bases?tenant_id=${TENANT_ID}${overallSuffix}`),
        api.get<GeneralSkillRead[]>(`/api/enterprise/general-skills?tenant_id=${TENANT_ID}${overallSuffix}`),
        overall
          ? api.get<SkillRead[]>(`/api/enterprise/agents/${overall.id}/skills?tenant_id=${TENANT_ID}`)
          : Promise.resolve([]),
        api.get<ToolRead[]>(`/api/enterprise/tools?tenant_id=${TENANT_ID}${overallSuffix}`),
      ]);
      setAgents(agentRows);
      setKnowledgeBases(kbRows);
      setGeneralSkills(generalRows);
      setSkills(skillRows);
      setTools(toolRows);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载开放广场失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadPlatformData();
  }, [loadPlatformData]);

  const visibleAgents = useMemo(
    () => agents.filter((item) => !item.is_overall && item.status === 'active' && isGalleryEmployee(item)),
    [agents],
  );
  const overallAgent = agents.find((item) => item.is_overall) || null;
  const canManagePlatform = isAdmin;
  const currentAgent = agents.find((item) => item.id === agentId);
  const targetEmployee = currentAgent && canManageEmployeeAgent(currentAgent, currentUser)
    ? currentAgent
    : agents.find((item) => canManageEmployeeAgent(item, currentUser) && !item.is_overall);

  const platformItems = useMemo<Record<PlatformKind, PlatformItem[]>>(() => ({
    agents: visibleAgents.map((item) => {
      const profile = employeeProfile(item);
      return {
        id: item.id,
        deleteKey: item.id,
        title: employeeDisplayNameWithCreator(item),
        description: item.description || '广场开放的数字员工。',
        meta: profile.roleName,
        tags: [
          item.status === 'active' ? '在线' : '下线',
          `SOP ${agentResourceCount(item, 'skill')}`,
          `技能 ${agentResourceCount(item, 'general_skill')}`,
        ],
        agent: item,
      };
    }),
    knowledge: knowledgeBases
      .filter((item) => item.status === 'active' && !isEmptyDefaultKnowledgeBase(item))
      .map((item) => ({
        id: item.id,
        deleteKey: item.id,
        title: resourceDisplayNameWithCreator(item.name, item),
        description: item.description || '广场沉淀的知识库。',
        meta: `${item.document_count} 文档 / ${item.bucket_count} 目录 / ${item.chunk_count} 引用`,
        tags: [item.version || 'v1.0.0', item.branch_sync_state || '广场版'],
      })),
    'general-skills': generalSkills
      .filter((item) => item.status === 'published')
      .map((item) => ({
        id: item.id,
        deleteKey: item.slug,
        title: resourceDisplayNameWithCreator(item.name, item),
        description: item.description || '可复制到当前数字员工的技能。',
        meta: item.slug,
        tags: [item.homepage ? '外部能力' : '内置能力', '已启用'],
      })),
    skills: skills
      .filter((item) => item.status === 'published')
      .map((item) => ({
        id: item.id,
        deleteKey: item.skill_id,
        title: resourceDisplayNameWithCreator(item.name, item),
        description: item.description || '可复制和复用的业务 SOP。',
        meta: `${item.skill_id} / ${item.version}`,
        tags: [item.business_domain || '业务流程', `${item.total_call_count || item.call_count || 0} 次调用`],
      })),
    tools: tools
      .filter((item) => item.enabled)
      .map((item) => ({
        id: item.id,
        deleteKey: item.id,
        title: resourceDisplayNameWithCreator(item.display_name || item.name, item),
        description: item.description || '可配置到员工工具的工具。',
        meta: `${item.bucket || '工具'} / ${item.tool_type.toUpperCase()}`,
        tags: [item.method, item.enabled ? '已启用' : '已停用'],
      })),
  }), [generalSkills, knowledgeBases, skills, tools, visibleAgents]);

  const filteredItems = useMemo(() => {
    const items = platformItems[activeKind];
    const keyword = searchText.trim().toLowerCase();
    if (!keyword) return items;
    return items.filter((item) => [
      item.title,
      item.description,
      item.meta,
      item.tags.join(' '),
    ].some((value) => value.toLowerCase().includes(keyword)));
  }, [activeKind, platformItems, searchText]);

  const tabItems = useMemo(
    () =>
      PLATFORM_CONFIGS.map((config) => ({
        value: config.kind,
        label: config.title,
        count: platformItems[config.kind].length,
      })),
    [platformItems],
  );

  function ensureTargetEmployee(): boolean {
    if (!targetEmployee) {
      notify.warning('请先选择一个员工，再从广场复制资源。');
      return false;
    }
    if (targetEmployee.id !== agentId) {
      window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, targetEmployee.id);
      window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: targetEmployee.id } }));
      setAgentId(targetEmployee.id);
    }
    return true;
  }

  async function markPlatformAgentUsed(agent: AgentProfileRead) {
    const metadata = agent.metadata || {};
    if (metadata.used_by_current_user !== true && metadata.chat_used_by_current_user !== true) {
      await api.post<AgentProfileRead>(`/api/chat/agents/${agent.id}/use?tenant_id=${TENANT_ID}`, {});
    }
    setAgents((current) => current.map((item) => (
      item.id === agent.id
        ? {
          ...item,
          metadata: {
            ...(item.metadata || {}),
            used_by_current_user: true,
            chat_used_by_current_user: true,
          },
        }
        : item
    )));
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, agent.id);
    window.dispatchEvent(new Event('ultrarag-enterprise-agent-scope-refresh'));
    window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: agent.id } }));
    setAgentId(agent.id);
  }

  async function usePlatformItem(platformKind: PlatformKind, itemId?: string) {
    if (platformKind === 'agents') {
      const agent = visibleAgents.find((item) => item.id === itemId) || visibleAgents[0];
      if (!agent) {
        notify.warning('广场暂无可用数字员工');
        return;
      }
      try {
        await markPlatformAgentUsed(agent);
        navigate('/enterprise/dashboard');
      } catch (error) {
        notify.error(error instanceof Error ? error.message : '使用数字员工失败');
      }
      return;
    }
    if (!ensureTargetEmployee()) return;
    const resourceParam = itemId ? `&resourceId=${encodeURIComponent(itemId)}` : '';
    if (platformKind === 'knowledge') navigate(`/enterprise/knowledge?add=plaza${resourceParam}`);
    if (platformKind === 'general-skills') navigate(`/enterprise/general-skills?add=plaza${resourceParam}`);
    if (platformKind === 'skills') navigate(`/enterprise/skills?add=plaza${resourceParam}`);
    if (platformKind === 'tools') navigate('/enterprise/tools?add=plaza');
  }

  function platformItemDeleteKey(platformKind: PlatformKind, item: PlatformItem): string {
    return `${platformKind}:${item.deleteKey || item.id}`;
  }

  function platformDeleteUrl(platformKind: PlatformKind, item: PlatformItem): string {
    const resourceKey = encodeURIComponent(item.deleteKey || item.id);
    const overallSuffix = overallAgent ? `&agent_id=${encodeURIComponent(overallAgent.id)}` : '';
    if (platformKind === 'agents') return `/api/enterprise/agents/${resourceKey}?tenant_id=${TENANT_ID}`;
    if (platformKind === 'knowledge') return `/api/enterprise/knowledge-bases/${resourceKey}?tenant_id=${TENANT_ID}${overallSuffix}`;
    if (platformKind === 'general-skills') return `/api/enterprise/general-skills/${resourceKey}?tenant_id=${TENANT_ID}${overallSuffix}`;
    if (platformKind === 'skills') return `/api/enterprise/skills/${resourceKey}?tenant_id=${TENANT_ID}${overallSuffix}`;
    return `/api/enterprise/tools/${resourceKey}?tenant_id=${TENANT_ID}${overallSuffix}`;
  }

  async function runDelete() {
    if (!confirmTarget) return;
    const { kind: platformKind, item } = confirmTarget;
    const key = platformItemDeleteKey(platformKind, item);
    setDeletingItemKey(key);
    try {
      if (platformKind === 'agents' && item.agent) {
        await api.post<AgentProfileRead>(
          `/api/enterprise/agents/${encodeURIComponent(item.agent.id)}/gallery:unpublish?tenant_id=${encodeURIComponent(TENANT_ID)}`,
          {},
        );
        window.dispatchEvent(new Event('ultrarag-enterprise-agent-scope-refresh'));
      } else {
        await api.delete(platformDeleteUrl(platformKind, item));
      }
      notify.success(platformKind === 'agents' ? '员工已从广场下线' : '已从广场移除');
      setDetailItem((current) => (
        current && current.kind === platformKind && current.item.id === item.id ? null : current
      ));
      setConfirmTarget(null);
      await loadPlatformData();
    } catch (error) {
      notify.error(error instanceof Error
        ? error.message
        : platformKind === 'agents'
          ? '员工广场下线失败'
          : '删除失败');
    } finally {
      setDeletingItemKey('');
    }
  }

  function navigateDetailItem(offset: -1 | 1) {
    if (!detailItem) return;
    const items = platformItems[detailItem.kind];
    const currentIndex = items.findIndex((entry) => entry.id === detailItem.item.id);
    const nextItem = items[currentIndex + offset];
    if (!nextItem) return;
    setDetailItem({ kind: detailItem.kind, item: nextItem });
  }

  function handleCreateGeneralSkill() {
    const overall = agents.find((item) => item.is_overall);
    if (!overall) {
      notify.error('暂时无法找到开放广场');
      return;
    }
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, overall.id);
    window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', {
      detail: { agentId: overall.id },
    }));
    navigate('/enterprise/general-skills/new?scope=gallery');
  }

  function handleKindChange(next: PlatformKind) {
    setActiveKind(next);
    navigate(`/enterprise/platform/${next}`);
  }

  function renderItemDrawer() {
    if (!detailItem) return null;
    const config = PLATFORM_BY_KIND.get(detailItem.kind) || PLATFORM_CONFIGS[0];
    const { item } = detailItem;
    const deleteKey = platformItemDeleteKey(detailItem.kind, item);
    const drawerItems = platformItems[detailItem.kind];
    const drawerIndex = drawerItems.findIndex((entry) => entry.id === item.id);

    if (detailItem.kind === 'agents' && item.agent) {
      const profile = employeeProfile(item.agent);
      const detailText = item.agent.persona_prompt
        || item.agent.description
        || config.detail;
      return (
        <PlatformEmployeeDrawer
          open
          agent={item.agent}
          platformTitle={config.title}
          name={item.title}
          role={item.meta}
          description={item.description}
          detailText={detailText}
          workStyles={profile.workStyles}
          stats={employeeStats(item.agent)}
          online={item.agent.status === 'active'}
          canManage={canManagePlatform}
          unpublishing={deletingItemKey === deleteKey}
          hasPrev={drawerIndex > 0}
          hasNext={drawerIndex >= 0 && drawerIndex < drawerItems.length - 1}
          onClose={() => setDetailItem(null)}
          onPrev={() => navigateDetailItem(-1)}
          onNext={() => navigateDetailItem(1)}
          onUnpublish={() => setConfirmTarget({ kind: detailItem.kind, item })}
          onUse={() => {
            setDetailItem(null);
            void usePlatformItem(detailItem.kind, item.id);
          }}
        />
      );
    }

    return (
      <PlatformResourceDrawer
        open
        platformTitle={config.title}
        icon={PLATFORM_RESOURCE_ICON[detailItem.kind]
          ? <img src={PLATFORM_RESOURCE_ICON[detailItem.kind]} alt="" className="size-[36px] object-contain" />
          : <span className="grid size-[36px] place-items-center text-[#757f9c]">{config.icon}</span>}
        accent={PLATFORM_ACCENT[detailItem.kind]}
        title={item.title}
        description={item.description}
        badge={resourceDrawerBadge(detailItem.kind, item)}
        categoryMeta={item.meta}
        detailText={config.detail}
        useLabel={config.useLabel}
        canManage={canManagePlatform}
        deleting={deletingItemKey === deleteKey}
        hasPrev={drawerIndex > 0}
        hasNext={drawerIndex >= 0 && drawerIndex < drawerItems.length - 1}
        onClose={() => setDetailItem(null)}
        onPrev={() => navigateDetailItem(-1)}
        onNext={() => navigateDetailItem(1)}
        onDelete={() => setConfirmTarget({ kind: detailItem.kind, item })}
        onUse={() => {
          setDetailItem(null);
          void usePlatformItem(detailItem.kind, item.id);
        }}
      />
    );
  }

  function renderConfirm() {
    const config = confirmTarget ? PLATFORM_BY_KIND.get(confirmTarget.kind) || PLATFORM_CONFIGS[0] : null;
    return (
      <ConfirmDialog
        open={Boolean(confirmTarget)}
        onOpenChange={(next) => { if (!next) setConfirmTarget(null); }}
        title={confirmTarget && config
          ? confirmTarget.kind === 'agents'
            ? `从广场下线员工「${confirmTarget.item.title}」？`
            : `删除${config.metricLabel}「${confirmTarget.item.title}」？`
          : ''}
        description={confirmTarget?.kind === 'agents'
          ? '下线后该员工不再出现在开放广场，也不能被新用户添加；员工本体及其资源、已有使用记录都会保留。'
          : '删除后该广场内容会从开放平台移除，已复制到员工侧的引用可能不再可同步。'}
        confirmText={confirmTarget?.kind === 'agents' ? '确认下线' : '删除'}
        loading={Boolean(confirmTarget) && deletingItemKey === (confirmTarget ? platformItemDeleteKey(confirmTarget.kind, confirmTarget.item) : '')}
        onConfirm={() => void runDelete()}
      />
    );
  }

  const config = PLATFORM_BY_KIND.get(activeKind) || PLATFORM_CONFIGS[0];
  const PlatformIcon = PLATFORM_ICON[activeKind];

  return (
    <div className="min-h-full box-border px-[48px] pt-[20px] pb-[43px] max-[900px]:px-[16px]" aria-busy={loading}>
      <AppHeader
        className="mb-[16px]"
        onLogout={onLogout}
        userName={currentUser?.username}
        title="开放广场平台"
      />

      <div className="flex flex-col gap-[20px] rounded-[20px] bg-white p-[18px_18px_24px_18px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex flex-wrap items-center justify-between gap-[12px]">
          <div
            role="tablist"
            aria-label="广场类型"
            className="flex flex-wrap gap-[12px] max-[560px]:gap-[8px]"
          >
            {tabItems.map((item) => {
              const active = item.value === activeKind;
              return (
                <button
                  key={item.value}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  onClick={() => handleKindChange(item.value)}
                  className={cn(
                    'flex min-w-[120px] items-center justify-center gap-[8px] whitespace-nowrap rounded-[10px] border-[0.5px] px-[14px] py-[8px] text-[14px] leading-[normal] transition-colors outline-none focus-visible:ring-2 focus-visible:ring-[#1a71ff] focus-visible:ring-offset-2',
                    active
                      ? 'border-[#1a71ff] bg-[#eef0ff] font-medium text-[#1a71ff]'
                      : 'border-[#e3e7f1] bg-white font-normal text-[#4f5669] hover:border-[#cbd3e6] hover:bg-[#f6f7fa] hover:text-[#18181a]',
                    'max-[560px]:min-w-[88px] max-[560px]:px-[10px] max-[560px]:py-[6px] max-[560px]:text-[12px]',
                  )}
                >
                  <span>{item.label}</span>
                  <span
                    className={cn(
                      'rounded-[90px] px-[7px] py-[1px] text-[11px] leading-none',
                      active ? 'bg-white text-[#1a71ff]' : 'bg-[#f2f4f8] text-[#757f9c]',
                    )}
                  >
                    {item.count}
                  </span>
                </button>
              );
            })}
          </div>

          <div className="flex flex-wrap items-center gap-[8px]">
            {activeKind === 'general-skills' && (
              <UIButton onClick={handleCreateGeneralSkill} className="h-8 gap-1 rounded-[10px] bg-[#18181a] px-5 text-[12px] font-normal text-white hover:bg-[#303030]">
                <IconAdd className="size-3.5" />
                创建开放 Skill
              </UIButton>
            )}
            <UIButton
              variant="outline"
              onClick={() => void loadPlatformData()}
              disabled={loading}
              className="h-8 gap-1 rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-5 text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]"
            >
              <IconRefresh className={cn('size-[14px]', loading && 'animate-spin')} />
              刷新
            </UIButton>
          </div>
        </div>

        <div className="flex flex-col gap-[10px] px-[12px]">
          <div className="flex items-center gap-[8px] text-[#18181a]">
            <PlatformIcon className="size-[16px] shrink-0" />
            <span className="text-[18px] font-medium leading-none">{config.title}</span>
          </div>

          {config.signals.length > 0 && (
            <div className="flex flex-wrap items-center gap-[8px]">
              {config.signals.map((signal) => (
                <span
                  key={signal}
                  className="rounded-[20px] border-[0.5px] border-[#e3e7f1] px-[10px] py-[3px] text-[11px] leading-[normal] text-[#757f9c]"
                >
                  {signal}
                </span>
              ))}
            </div>
          )}
        </div>

        <div className="flex flex-col gap-[16px]">
          <label className="flex h-[34px] w-full max-w-[360px] items-center gap-[8px] overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] transition-colors focus-within:border-[#18181a]">
            <IconSearch className="size-[14px] shrink-0 text-[#858b9c]" />
            <input
              autoComplete="off"
              data-1p-ignore="true"
              data-lpignore="true"
              data-bwignore="true"
              value={searchText}
              placeholder={`搜索${platformCountLabel(activeKind)}`}
              onChange={(event) => setSearchText(event.target.value)}
              className="min-w-0 flex-1 border-0 bg-transparent text-[12px] text-[#18181a] outline-none placeholder:text-[#858b9c]"
            />
          </label>

          {loading ? (
            <DetailSkeleton kind={activeKind} />
          ) : filteredItems.length === 0 ? (
            <div className="grid min-h-[180px] w-full place-items-center content-center gap-[10px] rounded-[18px] border border-dashed border-[#dfe4ec] bg-[#fbfcfd] px-[20px] py-[40px] text-center font-bold text-[#8b94aa]">
              <IconSearch className="size-[20px] shrink-0" />
              <span>{platformItems[activeKind].length === 0 ? '暂无开放内容' : '没有匹配的广场内容'}</span>
            </div>
          ) : activeKind === 'agents' ? (
            <div className="grid grid-cols-3 gap-[16px] max-[900px]:grid-cols-2 max-[560px]:grid-cols-1">
              {filteredItems.map((item) => item.agent && (
                <PlatformEmployeeCard
                  key={item.id}
                  avatar={(
                    <EmployeeAvatar
                      agent={item.agent}
                      width={66}
                      height={78}
                      fit="contain"
                      objectPosition="center bottom"
                      className="overflow-visible! rounded-none! border-0! bg-transparent! bg-none! shadow-none! after:hidden!"
                    />
                  )}
                  name={item.title}
                  role={item.meta}
                  online={item.agent.status === 'active'}
                  description={item.description}
                  stats={employeeStats(item.agent)}
                  onOpen={() => setDetailItem({ kind: activeKind, item })}
                  onUnpublish={canManagePlatform
                    ? () => setConfirmTarget({ kind: activeKind, item })
                    : undefined}
                  unpublishing={deletingItemKey === platformItemDeleteKey(activeKind, item)}
                />
              ))}
            </div>
          ) : (
            <div className="grid grid-cols-3 gap-[16px] max-[900px]:grid-cols-2 max-[560px]:grid-cols-1">
              {filteredItems.map((item) => (
                <PlatformResourceCard
                  key={item.id}
                  icon={PLATFORM_RESOURCE_ICON[activeKind]
                    ? <img src={PLATFORM_RESOURCE_ICON[activeKind]} alt="" className="size-[32px] shrink-0 object-contain" />
                    : undefined}
                  accent={PLATFORM_ACCENT[activeKind]}
                  title={item.title}
                  meta={item.meta}
                  description={item.description}
                  tags={item.tags.slice(0, 2)}
                  onClick={() => setDetailItem({ kind: activeKind, item })}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      {renderItemDrawer()}
      {renderConfirm()}
    </div>
  );
}

function isEmptyDefaultKnowledgeBase(item: KnowledgeBaseRead): boolean {
  return item.name === '默认知识库' && item.document_count === 0 && item.bucket_count === 0 && item.chunk_count === 0;
}
