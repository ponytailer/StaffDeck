import { useEffect, useState } from 'react';
import { Check, KeyRound, LoaderCircle, X } from 'lucide-react';

import AppHeader from '@/components/AppHeader';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  Textarea,
} from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { StatusBadge } from './scheduled-tasks/StatusBadge';
import { api, ApiError, TENANT_ID } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import { cn } from '@/lib/utils';

type ApiKeyApproval = {
  id: string;
  tenant_id: string;
  user_id: string;
  username: string | null;
  purpose: string | null;
  status: 'pending' | 'approved' | 'rejected' | 'revoked';
  api_key_masked: string | null;
  api_url: string | null;
  reviewer_note: string | null;
  reviewed_at: string | null;
  created_at: string;
};

const STATUS_META: Record<ApiKeyApproval['status'], { tone: 'orange' | 'green' | 'red' | 'gray'; label: string }> = {
  pending: { tone: 'orange', label: '待审批' },
  approved: { tone: 'green', label: '已批准' },
  rejected: { tone: 'red', label: '已驳回' },
  revoked: { tone: 'gray', label: '已吊销' },
};

function formatTime(value: string | null): string {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', { hour12: false });
}

export default function ApiKeyApprovalsPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
} = {}) {
  const [rows, setRows] = useState<ApiKeyApproval[]>([]);
  const [loading, setLoading] = useState(false);
  const [actingId, setActingId] = useState<string | null>(null);
  const [rejectTarget, setRejectTarget] = useState<ApiKeyApproval | null>(null);
  const [rejectNote, setRejectNote] = useState('');
  const [rejecting, setRejecting] = useState(false);

  function load() {
    setLoading(true);
    return api
      .get<ApiKeyApproval[]>(`/api/enterprise/api-key-applications?tenant_id=${TENANT_ID}`)
      .then((items) => setRows(items))
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载申请失败'))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    void load();
  }, []);

  async function approve(item: ApiKeyApproval) {
    setActingId(item.id);
    try {
      await api.post<ApiKeyApproval>(`/api/enterprise/api-key-applications/${item.id}/approve`, {
        tenant_id: TENANT_ID,
      });
      notify.success('已通过，已自动分配 API Key 与网关地址');
      await load();
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '操作失败');
    } finally {
      setActingId(null);
    }
  }

  async function revoke(item: ApiKeyApproval) {
    setActingId(item.id);
    try {
      await api.post<ApiKeyApproval>(`/api/enterprise/api-key-applications/${item.id}/revoke`, {
        tenant_id: TENANT_ID,
      });
      notify.success('已吊销该 API Key');
      await load();
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '操作失败');
    } finally {
      setActingId(null);
    }
  }

  async function confirmReject() {
    if (!rejectTarget) return;
    setRejecting(true);
    try {
      await api.post<ApiKeyApproval>(`/api/enterprise/api-key-applications/${rejectTarget.id}/reject`, {
        tenant_id: TENANT_ID,
        reviewer_note: rejectNote.trim() || undefined,
      });
      notify.success('已驳回');
      setRejectTarget(null);
      setRejectNote('');
      await load();
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '操作失败');
    } finally {
      setRejecting(false);
    }
  }

  const pendingCount = rows.filter((item) => item.status === 'pending').length;

  const columns: DataTableColumn<ApiKeyApproval>[] = [
    {
      key: 'applicant',
      title: '申请人',
      width: 140,
      render: (row) => (
        <div className="flex min-w-0 flex-col gap-[2px]">
          <span className="truncate text-[13px] font-medium text-[#18181a]">{row.username || '-'}</span>
          <span className="truncate text-[12px] text-[#858b9c]">{row.user_id}</span>
        </div>
      ),
    },
    {
      key: 'purpose',
      title: '用途',
      render: (row) => <span className="block truncate text-[13px] text-[#464c5e]">{row.purpose || '（未填写）'}</span>,
    },
    {
      key: 'api_url',
      title: '网关地址',
      className: 'whitespace-normal',
      render: (row) =>
        row.status === 'approved' && row.api_url ? (
          <code className="block truncate font-mono text-[12px] text-[#464c5e]">{row.api_url}</code>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'status',
      title: '状态',
      width: 90,
      render: (row) => {
        const meta = STATUS_META[row.status];
        return <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>;
      },
    },
    {
      key: 'created_at',
      title: '申请时间',
      width: 160,
      render: (row) => <span className="text-[12px] text-[#858b9c]">{formatTime(row.created_at)}</span>,
    },
    {
      key: 'actions',
      title: '操作',
      width: 150,
      align: 'right',
      render: (row) => {
        const busy = actingId === row.id;
        if (row.status === 'pending') {
          return (
            <div className="flex items-center justify-end gap-[8px]">
              <UIButton
                disabled={busy}
                onClick={() => void approve(row)}
                className="h-[30px] gap-[4px] rounded-[8px] bg-[#18181a] px-[12px] text-[12px] font-normal text-white hover:bg-[#303030] disabled:opacity-60"
              >
                {busy ? <LoaderCircle className="size-[13px] animate-spin" /> : <Check className="size-[13px]" />}
                通过
              </UIButton>
              <UIButton
                disabled={busy}
                onClick={() => {
                  setRejectTarget(row);
                  setRejectNote('');
                }}
                className="h-[30px] gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#c0392b] hover:bg-[#fdeeee] disabled:opacity-60"
              >
                <X className="size-[13px]" />
                驳回
              </UIButton>
            </div>
          );
        }
        if (row.status === 'approved') {
          return (
            <div className="flex items-center justify-end">
              <UIButton
                disabled={busy}
                onClick={() => void revoke(row)}
                className="h-[30px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#757f9c] hover:bg-black/5 hover:text-[#18181a] disabled:opacity-60"
              >
                {busy ? <LoaderCircle className="size-[13px] animate-spin" /> : null}
                吊销
              </UIButton>
            </div>
          );
        }
        return <span className="block text-right text-[12px] text-[#c0c6d4]">—</span>;
      },
    },
  ];

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        className="items-center"
        onLogout={onLogout}
        userName={currentUser?.username}
        title="API Key 审批"
        description={pendingCount > 0 ? `有 ${pendingCount} 个申请待处理` : '用户申请的 API Key 均由管理员审批后分配'}
      />

      <div className="mt-[20px] flex flex-col gap-[24px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px_18px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <KeyRound className="size-[14px] shrink-0" />
          <span className="text-[14px] font-normal leading-none">申请列表</span>
        </div>

        <div className="hidden md:block">
          <DataTable
            aria-label="API Key 申请列表"
            columns={columns}
            data={rows}
            rowKey={(row) => row.id}
            loading={loading}
            emptyText="暂无 API Key 申请"
          />
        </div>

        <div className="grid gap-[10px] md:hidden">
          {loading ? (
            <div className="py-[40px] text-center text-[13px] text-[#858b9c]">加载中</div>
          ) : rows.length === 0 ? (
            <div className="py-[40px] text-center text-[13px] text-[#858b9c]">暂无 API Key 申请</div>
          ) : (
            rows.map((row) => {
              const meta = STATUS_META[row.status];
              const busy = actingId === row.id;
              return (
                <article key={row.id} className="min-w-0 rounded-[12px] border border-[#eceef1] bg-white p-[14px]">
                  <div className="flex min-w-0 items-start justify-between gap-[10px]">
                    <div className="min-w-0">
                      <span className="block truncate text-[13px] font-medium text-[#18181a]">
                        {row.username || '-'}
                      </span>
                      <span className="mt-[2px] block truncate text-[12px] text-[#858b9c]">
                        {row.purpose || '（未填写用途）'}
                      </span>
                    </div>
                    <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>
                  </div>
                  {row.status === 'approved' && row.api_url && (
                    <code className="mt-[8px] block truncate rounded-[8px] bg-[#f6f6f6] px-[10px] py-[7px] font-mono text-[12px] text-[#464c5e]">
                      {row.api_url}
                    </code>
                  )}
                  <span className="mt-[8px] block text-[12px] text-[#858b9c]">
                    申请于 {formatTime(row.created_at)}
                  </span>
                  {row.status === 'pending' && (
                    <div className="mt-[12px] flex items-center justify-end gap-[8px]">
                      <UIButton
                        disabled={busy}
                        onClick={() => void approve(row)}
                        className="h-[30px] gap-[4px] rounded-[8px] bg-[#18181a] px-[12px] text-[12px] font-normal text-white hover:bg-[#303030]"
                      >
                        {busy ? <LoaderCircle className="size-[13px] animate-spin" /> : <Check className="size-[13px]" />}
                        通过
                      </UIButton>
                      <UIButton
                        disabled={busy}
                        onClick={() => {
                          setRejectTarget(row);
                          setRejectNote('');
                        }}
                        className="h-[30px] gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#c0392b] hover:bg-[#fdeeee]"
                      >
                        <X className="size-[13px]" />
                        驳回
                      </UIButton>
                    </div>
                  )}
                </article>
              );
            })
          )}
        </div>
      </div>

      <Dialog open={Boolean(rejectTarget)} onOpenChange={(open) => !open && setRejectTarget(null)}>
        <DialogContent
          aria-describedby={undefined}
          className="flex w-[calc(100%-2rem)] flex-col gap-[14px] rounded-[14px] px-[20px] py-[16px] sm:max-w-[440px]"
        >
          <DialogTitle className="text-[15px] font-medium text-[#18181a]">驳回申请</DialogTitle>
          <p className="text-[12px] leading-[18px] text-[#757f9c]">
            申请：{rejectTarget?.purpose || '（未填写用途）'} · 申请人 {rejectTarget?.username || '-'}
          </p>
          <Textarea
            rows={3}
            value={rejectNote}
            disabled={rejecting}
            placeholder="驳回原因（可选）"
            onChange={(event) => setRejectNote(event.target.value)}
            className="min-h-[72px] resize-y text-[12px]"
          />
          <div className={cn('flex items-center justify-end gap-[8px]')}>
            <UIButton
              variant="outline"
              disabled={rejecting}
              onClick={() => setRejectTarget(null)}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
            >
              取消
            </UIButton>
            <UIButton
              disabled={rejecting}
              onClick={() => void confirmReject()}
              className="h-[32px] w-[80px] rounded-[10px] bg-[#c0392b] px-[12px] text-[14px] font-normal text-white hover:bg-[#a93226]"
            >
              {rejecting && <LoaderCircle className="size-[14px] animate-spin" />}
              确认驳回
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
