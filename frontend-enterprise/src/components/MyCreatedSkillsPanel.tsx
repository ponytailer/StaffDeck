import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Ban, CircleCheck, Download, Share2 } from 'lucide-react';

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { SkillShareDialog } from './SkillShareDialog';
import { downloadGeneralSkillPackage } from '../lib/skill-package';
import {
  announceEnterpriseCapabilityCatalogChange,
  subscribeEnterpriseCapabilityCatalogRefresh,
} from '@/lib/capability-catalog-events';
import {
  MENU_CONTENT_CLASS,
  MENU_ITEM_CLASS,
  MENU_ITEM_DANGER_CLASS,
  MOBILE_CARD_CLASS,
  OUTLINE_ACTION_BUTTON_SM_CLASS,
  formatDateTime,
} from '@/lib/enterprise-ui';

import IconEdit from '../assets/icons/edit.svg?react';
import IconMore from '../assets/icons/more.svg?react';
import IconRefresh from '../assets/icons/refresh.svg?react';
import IconSkill from '../assets/icons/plaza-skill.svg?react';
import IconTrash from '../assets/icons/trash.svg?react';
import { api, TENANT_ID } from '../api/client';
import { employeeDisplayName } from '../employee';
import { StatusBadge } from '../pages/scheduled-tasks/StatusBadge';
import type { BadgeTone } from '../pages/scheduled-tasks/shared';
import type { AgentProfileRead, GeneralSkillRead } from '../types';
import { ConfirmDialog } from './ConfirmDialog';
import { DataTable, type DataTableColumn } from './DataTable';

const STATUS_BADGE: Record<GeneralSkillRead['status'], { tone: BadgeTone; text: string }> = {
  draft: { tone: 'blue', text: '草稿' },
  published: { tone: 'green', text: '已启用' },
  archived: { tone: 'gray', text: '已停用' },
};

/** 宿主数字员工 id；广场技能没有宿主，返回空串。 */
function skillOwnerAgentId(row: GeneralSkillRead): string {
  const value = row.metadata?.owner_agent_id;
  return typeof value === 'string' ? value.trim() : '';
}

/** 技能归属：广场技能落在「技能广场」，员工技能落在宿主员工的显示名上。 */
function skillScopeLabel(row: GeneralSkillRead, agents: AgentProfileRead[]): string {
  const agentId = skillOwnerAgentId(row);
  if (!agentId) return '技能广场';
  // employeeDisplayName(undefined) 自带「数字员工」兜底
  return employeeDisplayName(agents.find((item) => item.id === agentId));
}

/**
 * 「我创建的技能」维护区。
 *
 * 技能广场与数字员工解耦之后，成员在广场里创建的技能不再挂在任何员工下，
 * 于是创建者失去了维护入口（以前是在员工里维护自己那些技能）。这里给创建者
 * 一个跨作用域的汇总视图：广场技能 + 挂在员工下的技能都能编辑 / 启停 / 删除。
 *
 * 数据来自 `GET /api/enterprise/general-skills?mine=1`——后端按 metadata 里的
 * 创建者字段过滤，并把员工技能的启停状态折算成它宿主的绑定状态。
 */
export default function MyCreatedSkillsPanel({
  agents,
  searchTerm = '',
}: {
  agents: AgentProfileRead[];
  searchTerm?: string;
}) {
  const navigate = useNavigate();
  const [rows, setRows] = useState<GeneralSkillRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<GeneralSkillRead | null>(null);
  const [deleting, setDeleting] = useState(false);
  // 分享/下载与广场一致：published 才可分享，弹窗负责生成/复制公开链接
  const [shareTarget, setShareTarget] = useState<GeneralSkillRead | null>(null);

  const load = () => {
    setLoading(true);
    return api
      .get<GeneralSkillRead[]>(`/api/enterprise/general-skills?tenant_id=${TENANT_ID}&mine=1&include_files=0`)
      .then(setRows)
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载我的技能失败'))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 在技能广场改完技能再切回本页时，列表要跟着更新：订阅能力目录变更 +
  // focus / pageshow / visibilitychange，避免一直显示进页面时的旧快照。
  useEffect(() => subscribeEnterpriseCapabilityCatalogRefresh(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), []);

  const filteredRows = useMemo(() => {
    const keyword = searchTerm.trim().toLowerCase();
    if (!keyword) return rows;
    return rows.filter((row) => [
      row.name,
      row.slug,
      row.description || '',
      skillScopeLabel(row, agents),
    ].join(' ').toLowerCase().includes(keyword));
  }, [rows, searchTerm, agents]);

  /** 广场技能（无宿主）走 overall 分支；员工技能必须带上宿主 agent_id，状态记在它的绑定行上。 */
  function agentSuffix(row: GeneralSkillRead): string {
    const agentId = skillOwnerAgentId(row);
    return agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
  }

  function editRouteFor(row: GeneralSkillRead): string {
    // 广场技能只能从广场进入编辑器，否则编辑器会按员工作用域解析、返回时掉出广场。
    // return=employee_skills：明确告诉编辑器「我是从数字员工-技能管理进来的」——
    // 返回按钮去技能管理 tab，而不是被 forceGalleryScope 拉到平台广场。
    const scopeQuery = skillOwnerAgentId(row) ? '' : '?scope=gallery&return=employee_skills';
    return `/enterprise/general-skills/${encodeURIComponent(row.slug)}/edit${scopeQuery}`;
  }

  async function setSkillPublished(row: GeneralSkillRead, published: boolean) {
    const agentId = skillOwnerAgentId(row);
    try {
      const next = await api.post<GeneralSkillRead>(
        `/api/enterprise/general-skills/${encodeURIComponent(row.slug)}/${published ? 'publish' : 'archive'}?tenant_id=${TENANT_ID}${agentSuffix(row)}`,
      );
      setRows((current) => current.map((item) => (item.id === next.id ? next : item)));
      notify.success(published ? '已启用技能' : '已停用技能');
      announceEnterpriseCapabilityCatalogChange({ resourceType: 'skill', agentId: agentId || undefined });
    } catch (error) {
      notify.error(error instanceof Error ? error.message : published ? '启用失败' : '停用失败');
    }
  }

  /** 广场/员工技能同一套 zip 下载（share 的 agentSuffix 只影响广场可见性口径）。 */
  async function downloadSkillPackage(row: GeneralSkillRead) {
    try {
      await downloadGeneralSkillPackage({
        slug: row.slug,
        agentId: skillOwnerAgentId(row) || null,
      });
      notify.success(`已下载技能包：${row.slug}`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '技能包下载失败');
    }
  }

  async function confirmDelete() {
    const row = deleteTarget;
    if (!row) return;
    const agentId = skillOwnerAgentId(row);
    setDeleting(true);
    try {
      await api.delete(
        `/api/enterprise/general-skills/${encodeURIComponent(row.slug)}?tenant_id=${TENANT_ID}${agentSuffix(row)}`,
      );
      setRows((current) => current.filter((item) => item.id !== row.id));
      notify.success(agentId ? '已移除技能' : '已删除技能');
      setDeleteTarget(null);
      announceEnterpriseCapabilityCatalogChange({ resourceType: 'skill', agentId: agentId || undefined });
    } catch (error) {
      notify.error(error instanceof Error ? error.message : agentId ? '移除失败' : '删除失败');
    } finally {
      setDeleting(false);
    }
  }

  function renderActions(row: GeneralSkillRead) {
    const published = row.status === 'published';
    const boundToAgent = Boolean(skillOwnerAgentId(row));
    return (
      <DropdownMenu>
        <DropdownMenuTrigger
          aria-label="技能操作"
          className="ml-auto grid size-7 place-items-center rounded-[8px] text-[#1a71ff] transition-colors outline-none hover:bg-black/5 hover:text-[#4a8dff] focus-visible:bg-black/5"
        >
          <IconMore className="size-3.5" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className={MENU_CONTENT_CLASS}>
          <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void downloadSkillPackage(row)}>
            <Download />
            下载
          </DropdownMenuItem>
          {published && (
            <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => setShareTarget(row)}>
              <Share2 />
              分享
            </DropdownMenuItem>
          )}
          <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => navigate(editRouteFor(row))}>
            <IconEdit />
            编辑
          </DropdownMenuItem>
          {published ? (
            <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void setSkillPublished(row, false)}>
              <Ban />
              停用
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void setSkillPublished(row, true)}>
              <CircleCheck />
              启用
            </DropdownMenuItem>
          )}
          <DropdownMenuSeparator className="my-[2px] bg-[#eef0f4]" />
          <DropdownMenuItem
            variant="destructive"
            className={MENU_ITEM_DANGER_CLASS}
            onSelect={() => setDeleteTarget(row)}
          >
            <IconTrash />
            {boundToAgent ? '移除' : '删除'}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    );
  }

  const columns: DataTableColumn<GeneralSkillRead>[] = [
    {
      key: 'name',
      title: '名称',
      width: 200,
      className: 'text-[#18181a]',
      render: (row) => (
        <div className="flex min-w-0 flex-col gap-[2px]">
          <span className="truncate font-medium leading-[18px] text-[#18181a]" title={row.name}>
            {row.name}
          </span>
          <span className="truncate text-[#858b9c]" title={row.slug}>
            {row.slug}
          </span>
        </div>
      ),
    },
    {
      key: 'scope',
      title: '归属',
      width: 130,
      render: (row) => (
        <span className="block truncate text-[#858b9c]" title={skillScopeLabel(row, agents)}>
          {skillScopeLabel(row, agents)}
        </span>
      ),
    },
    {
      key: 'status',
      title: '状态',
      width: 100,
      render: (row) => {
        const preset = STATUS_BADGE[row.status] || { tone: 'gray' as BadgeTone, text: row.status };
        return <StatusBadge tone={preset.tone}>{preset.text}</StatusBadge>;
      },
    },
    {
      key: 'updated',
      title: '更新时间',
      width: 170,
      render: (row) => formatDateTime(row.updated_at),
    },
    {
      key: 'actions',
      title: '操作',
      width: 70,
      align: 'right',
      render: (row) => renderActions(row),
    },
  ];

  function renderMobileCard(row: GeneralSkillRead) {
    const preset = STATUS_BADGE[row.status] || { tone: 'gray' as BadgeTone, text: row.status };
    return (
      <article className={MOBILE_CARD_CLASS} key={row.id}>
        <div className="flex min-w-0 items-start justify-between gap-[10px]">
          <div className="min-w-0">
            <strong className="block truncate text-[14px] font-semibold text-[#18181a]">{row.name}</strong>
            <span className="mt-[2px] block truncate text-[12px] text-[#858b9c]">{row.slug}</span>
            <span className="mt-[2px] block truncate text-[12px] text-[#858b9c]">
              归属：{skillScopeLabel(row, agents)}
            </span>
          </div>
          {renderActions(row)}
        </div>
        {row.description && (
          <p className="mt-[8px] line-clamp-2 text-[12px] leading-[1.55] text-[#858b9c]">{row.description}</p>
        )}
        <div className="mt-[10px] flex items-center justify-between gap-[10px] text-[12px] text-[#858b9c]">
          <StatusBadge tone={preset.tone}>{preset.text}</StatusBadge>
          <span>{formatDateTime(row.updated_at)}</span>
        </div>
      </article>
    );
  }

  const hasSearchTerm = Boolean(searchTerm.trim());

  return (
    <section aria-label="我创建的技能" className="mt-[40px]">
      <div className="mb-[14px] flex flex-wrap items-baseline gap-[8px]">
        <h2 className="text-[16px] font-semibold leading-[22px] text-[#18181a]">我创建的技能</h2>
        <span className="text-[12px] text-[#858b9c]">{rows.length} 个</span>
        <button
          type="button"
          onClick={() => void load()}
          className="ml-auto inline-flex cursor-pointer items-center gap-[4px] border-0 bg-transparent p-0 text-[12px] text-[#757f9c] transition-colors hover:text-[#18181a]"
        >
          <IconRefresh className="size-[14px] shrink-0" />
          刷新
        </button>
      </div>

      {loading && !rows.length ? (
        <p className="rounded-[16px] border border-dashed border-[#e4e9f2] bg-[#fbfcfe] px-[24px] py-[24px] text-center text-[12px] text-[#a7adbb]">
          加载中…
        </p>
      ) : filteredRows.length ? (
        <>
          <div className="hidden md:block">
            <DataTable
              columns={columns}
              data={filteredRows}
              rowKey={(row) => row.id}
              aria-label="我创建的技能列表"
            />
          </div>
          <div className="grid gap-[12px] md:hidden">
            {filteredRows.map((row) => renderMobileCard(row))}
          </div>
        </>
      ) : (
        <div className="flex flex-col items-center rounded-[20px] border border-dashed border-[#e4e9f2] bg-[#fbfcfe] px-[24px] py-[26px] text-center">
          <span className="grid size-[34px] place-items-center rounded-[12px] bg-white text-[#98a2b3] shadow-[0_1px_8px_rgba(70,76,94,0.06)] ring-1 ring-[#edf1f6]">
            <IconSkill className="size-[16px] shrink-0" />
          </span>
          <p className="mt-[12px] text-[14px] font-medium leading-[20px] text-[#7f879a]">
            {hasSearchTerm ? '没有匹配的技能' : '还没有创建过技能'}
          </p>
          {!hasSearchTerm && (
            <>
              <p className="mt-[4px] text-[11px] leading-[17px] text-[#a7adbb]">
                在技能广场创建或导入技能后，可在这里编辑、启停和删除
              </p>
              <UIButton
                variant="outline"
                className={`mt-[14px] ${OUTLINE_ACTION_BUTTON_SM_CLASS}`}
                onClick={() => navigate('/enterprise/general-skills')}
              >
                去技能广场
              </UIButton>
            </>
          )}
        </div>
      )}

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        loading={deleting}
        title={`${deleteTarget && skillOwnerAgentId(deleteTarget) ? '移除' : '删除'}技能「${deleteTarget ? deleteTarget.name : ''}」？`}
        description={
          deleteTarget && skillOwnerAgentId(deleteTarget)
            ? '将从该数字员工移除，操作不可撤销。'
            : '将从技能广场下架并移出广场，操作不可撤销。'
        }
        confirmText={deleteTarget && skillOwnerAgentId(deleteTarget) ? '移除' : '删除'}
        onConfirm={() => void confirmDelete()}
      />

      <SkillShareDialog
        skill={shareTarget}
        open={Boolean(shareTarget)}
        onClose={() => setShareTarget(null)}
      />
    </section>
  );
}
