import { useEffect, useMemo, useState } from 'react';
import { Search } from 'lucide-react';

import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  notify,
} from '@/components/ui';
import { cn } from '@/lib/utils';

import { api, TENANT_ID } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import { employeeDisplayName, resourceCount } from '../employee';
import type { AgentProfileRead } from '../types';

type TransferCandidate = {
  id: string;
  username: string;
  display_name?: string | null;
  department?: string | null;
  role?: 'admin' | 'member';
  source?: string;
};

type TransferResult = {
  agent: AgentProfileRead;
  transferred_resources: number;
  transferred_breakdown: Record<string, number>;
  owner_username: string;
};

export type EmployeeTransferDialogProps = {
  agent: AgentProfileRead | null;
  open: boolean;
  currentUser?: EnterpriseAuthUser;
  onClose: () => void;
  /** 转让成功：把服务端返回的员工最新状态交给列表更新。 */
  onTransferred: (agent: AgentProfileRead) => void;
};

function candidateLabel(user: TransferCandidate): string {
  return (user.display_name || '').trim() || user.username;
}

/**
 * 员工转让：管理员或员工当前 owner 把员工转给同租户的另一个成员。
 *
 * 布局要点（踩过的坑）：`DialogContent` 的基础类里带 `sm:max-w-sm`（384px），
 * 它与 `max-w-[440px]` 在 tailwind-merge 里属于不同断点分组、不会互相抵消，桌面端
 * 仍按 384 生效。面板一旦比内容窄，`grid` 的隐式列又按 min-content 撑开，整段内容
 * 会溢出到白色面板之外（和背景页叠在一起，看起来就是「弹窗混乱」）。所以这里把
 * `sm:max-w-[440px]` 显式写一遍，并给内容行补 `min-w-0` + 面板 `overflow-hidden`。
 *
 * 结构用三行 grid：头 / 可滚动的身体 / 底部按钮。只有身体滚动，头和按钮始终可见。
 */
export default function EmployeeTransferDialog({
  agent,
  open,
  currentUser,
  onClose,
  onTransferred,
}: EmployeeTransferDialogProps) {
  const [candidates, setCandidates] = useState<TransferCandidate[]>([]);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [selectedId, setSelectedId] = useState('');
  const [keyword, setKeyword] = useState('');
  const [error, setError] = useState('');

  const currentOwnerId = String(agent?.metadata?.owner_user_id || '');

  useEffect(() => {
    if (!open) return;
    setSelectedId('');
    setKeyword('');
    setError('');
    let alive = true;
    setLoading(true);
    api
      .get<TransferCandidate[]>(`/api/auth/users?tenant_id=${TENANT_ID}`)
      .then((rows) => {
        if (!alive) return;
        // 分享访客不是租户成员，后端也会拒；这里先筛掉，别让人选完才报错。
        setCandidates(rows.filter((row) => row.source !== 'share'));
      })
      .catch((err) => {
        if (!alive) return;
        setError(err instanceof Error ? err.message : '加载成员列表失败');
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [open]);

  const selectable = useMemo(() => {
    const words = keyword.trim().toLowerCase();
    return candidates.filter((row) => {
      if (row.id === currentOwnerId) return false;
      if (!words) return true;
      return [row.display_name || '', row.username, row.department || '']
        .some((value) => value.toLowerCase().includes(words));
    });
  }, [candidates, currentOwnerId, keyword]);

  const selected = candidates.find((row) => row.id === selectedId) || null;
  const name = agent ? employeeDisplayName(agent) : '';
  const counts = agent
    ? [
        { label: '资料', value: resourceCount(agent.resources, 'knowledge_base') },
        { label: '技能', value: resourceCount(agent.resources, 'general_skill') },
        { label: 'SOP', value: resourceCount(agent.resources, 'skill') },
        { label: '工具', value: resourceCount(agent.resources, 'tool') },
      ].filter((item) => item.value > 0)
    : [];
  const isAdmin = currentUser?.role === 'admin';

  function handleOpenChange(next: boolean) {
    if (!next && !submitting) onClose();
  }

  async function submit() {
    if (!agent || !selected || submitting) return;
    setSubmitting(true);
    setError('');
    try {
      const result = await api.post<TransferResult>(
        `/api/enterprise/agents/${agent.id}/transfer`,
        { tenant_id: TENANT_ID, owner_user_id: selected.id },
      );
      const extra = result.transferred_resources
        ? `，连同 ${result.transferred_resources} 项资源`
        : '';
      notify.success(`「${name}」已转让给 ${candidateLabel(selected)}${extra}`);
      onTransferred(result.agent);
      onClose();
    } catch (err) {
      const message = err instanceof Error ? err.message : '转让失败';
      setError(message);
      notify.error(message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        aria-describedby="employee-transfer-description"
        className="grid max-h-[86vh] w-[440px] max-w-[calc(100vw_-_32px)] grid-rows-[auto_minmax(0,1fr)_auto] gap-0 overflow-hidden rounded-[18px] border-0 bg-white p-0 shadow-[0_28px_80px_rgba(24,31,46,0.20)] ring-0 sm:max-w-[440px]"
      >
        <DialogHeader className="min-w-0 border-b border-[#eceff4] px-[20px] py-[16px]">
          <DialogTitle className="truncate pr-[24px] text-[15px] font-semibold text-[#18181a]">
            转让员工「{name}」
          </DialogTitle>
          <DialogDescription
            id="employee-transfer-description"
            className="mt-[4px] text-[12px] leading-[18px] text-[#757f9c]"
          >
            转给同租户的其他成员，员工名下的资料、技能、SOP、工具一并转名。
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-0 min-w-0 overflow-y-auto px-[20px] py-[14px]">
          {counts.length > 0 && (
            <p className="m-0 mb-[10px] text-[12px] leading-[18px] text-[#464c5e]">
              一并转名：
              {counts.map((item) => `${item.label} ${item.value}`).join(' · ')}
            </p>
          )}

          <div className="flex h-[38px] w-full items-center gap-[8px] rounded-[10px] border border-[#e4e8ef] bg-white px-[10px] text-[#8b93a5] focus-within:border-[#c7cfdd]">
            <Search className="size-[15px] shrink-0" aria-hidden="true" />
            <input
              autoComplete="off"
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              placeholder="搜索成员姓名 / 账号 / 部门"
              aria-label="搜索接收成员"
              className="min-w-0 flex-1 border-0 bg-transparent text-[13px] text-[#18181a] outline-none placeholder:text-[#a8b0c0]"
            />
          </div>

          <div role="radiogroup" aria-label="选择接收成员" className="mt-[10px]">
            {loading && (
              <p className="m-0 py-[22px] text-center text-[12px] text-[#8b93a5]">正在加载成员…</p>
            )}
            {!loading && !selectable.length && (
              <p className="m-0 py-[22px] text-center text-[12px] leading-[18px] text-[#8b93a5]">
                {keyword.trim() ? '没有匹配的成员' : '除当前 owner 外，还没有其他可选成员'}
              </p>
            )}
            {selectable.map((row) => {
              const active = row.id === selectedId;
              return (
                <button
                  key={row.id}
                  type="button"
                  role="radio"
                  aria-checked={active}
                  disabled={submitting}
                  onClick={() => setSelectedId(row.id)}
                  className={cn(
                    'flex w-full min-w-0 items-center gap-[10px] rounded-[10px] border-0 px-[8px] py-[7px] text-left transition-colors',
                    active ? 'bg-[#f2f4f7]' : 'bg-transparent hover:bg-[#f8f9fb]',
                  )}
                >
                  <span className="grid size-[28px] shrink-0 place-items-center rounded-full bg-[#eef1f6] text-[12px] font-medium text-[#5e687c]">
                    {candidateLabel(row).slice(0, 1)}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex min-w-0 items-center gap-[6px]">
                      <span className="truncate text-[13px] font-medium text-[#18181a]">
                        {candidateLabel(row)}
                      </span>
                      {row.role === 'admin' && (
                        <span className="shrink-0 rounded-[5px] bg-[#eef7f5] px-[5px] py-px text-[10px] leading-[14px] text-[#0f7268]">
                          管理员
                        </span>
                      )}
                    </span>
                    <span className="mt-[1px] block truncate text-[11px] leading-[15px] text-[#8b93a5]">
                      {row.username}
                      {row.department ? ` · ${row.department}` : ''}
                    </span>
                  </span>
                  <span
                    className={cn(
                      'grid size-[16px] shrink-0 place-items-center rounded-full border',
                      active ? 'border-[#18181a] bg-[#18181a]' : 'border-[#d3d9e3]',
                    )}
                    aria-hidden="true"
                  >
                    {active && <span className="size-[6px] rounded-full bg-white" />}
                  </span>
                </button>
              );
            })}
          </div>

          {error && <p className="m-0 mt-[10px] text-[12px] leading-[18px] text-[#d20b0b]">{error}</p>}

          {!isAdmin && (
            <p className="m-0 mt-[10px] text-[11px] leading-[17px] text-[#8b93a5]">
              转让后你将失去这位员工的管理权限；如需再调整，需要新 owner 或管理员操作。
            </p>
          )}
        </div>

        <div className="flex min-w-0 items-center gap-[10px] border-t border-[#eceff4] px-[20px] py-[12px]">
          <p className="m-0 min-w-0 flex-1 truncate text-[12px] text-[#8b93a5]">
            {selected ? `转给 ${candidateLabel(selected)}` : '请选择接收成员'}
          </p>
          <Button
            type="button"
            variant="outline"
            disabled={submitting}
            onClick={() => handleOpenChange(false)}
            className="h-[34px] shrink-0 rounded-[9px] border-[#e2e6ed] px-[14px] text-[13px] text-[#5e687c] hover:bg-[#f4f6f9]"
          >
            取消
          </Button>
          <Button
            type="button"
            disabled={!selected || submitting}
            onClick={() => void submit()}
            className="h-[34px] shrink-0 rounded-[9px] bg-[#18181a] px-[14px] text-[13px] text-white hover:bg-[#2d2d33] disabled:opacity-40"
          >
            {submitting ? (
              <span className="mr-1 inline-block size-[13px] animate-spin rounded-full border-2 border-white/40 border-t-white" />
            ) : null}
            确认转让
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
