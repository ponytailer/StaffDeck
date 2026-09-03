import { useEffect, useState } from 'react';
import { Copy, Eye, EyeOff, KeyRound, LoaderCircle } from 'lucide-react';

import { Textarea } from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { StatusBadge } from '@/pages/scheduled-tasks/StatusBadge';
import { api, ApiError, TENANT_ID } from '../api/client';
import { cn } from '@/lib/utils';
import { type EnterpriseAuthUser } from '../auth';

const MAX_APPLICATIONS = 2;

type ApiKeyApplication = {
  id: string;
  tenant_id: string;
  user_id: string;
  username: string | null;
  purpose: string | null;
  status: 'pending' | 'approved' | 'rejected' | 'revoked';
  api_key_masked: string | null;
  api_key: string | null;
  api_url: string | null;
  reviewer_note: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
};

type MyUsageItem = {
  id: string;
  consumer_id: string | null;
  consumer_name: string | null;
  gateway_name: string | null;
  quota_limit: number | null;
  quota_period: string | null;
  used_amount: number;
  usage_rate: number;
  suggestion: string;
};

const STATUS_META: Record<ApiKeyApplication['status'], { tone: 'orange' | 'green' | 'red' | 'gray'; label: string }> = {
  pending: { tone: 'orange', label: '待审批' },
  approved: { tone: 'green', label: '已批准' },
  rejected: { tone: 'red', label: '已驳回' },
  revoked: { tone: 'gray', label: '已吊销' },
};

const PERIOD_LABEL: Record<string, string> = {
  day: '自然日',
  week: '自然周',
  month: '自然月',
};

const SUGGESTION_META: Record<string, { label: string; tone: 'red' | 'orange' | 'green' }> = {
  expand: { label: '建议扩容', tone: 'red' },
  watch: { label: '关注', tone: 'orange' },
  normal: { label: '正常', tone: 'green' },
};

function formatTime(value: string | null): string {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', { hour12: false });
}

export function ApiKeyApplicationsPanel({ currentUser }: { currentUser?: EnterpriseAuthUser }) {
  const [items, setItems] = useState<ApiKeyApplication[]>([]);
  const [usageItems, setUsageItems] = useState<MyUsageItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [purpose, setPurpose] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [revealedIds, setRevealedIds] = useState<Set<string>>(new Set());

  const toggleRevealed = (id: string) => {
    setRevealedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  const activeCount = items.filter(
    (item) => item.status === 'pending' || item.status === 'approved',
  ).length;
  const canApply = activeCount < MAX_APPLICATIONS;

  const load = () => {
    setLoading(true);
    return api
      .get<ApiKeyApplication[]>(`/api/enterprise/api-key-applications/mine?tenant_id=${TENANT_ID}`)
      .then((rows) => setItems(rows))
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载申请失败'))
      .finally(() => setLoading(false));
  };

  const loadUsage = () => {
    return api
      .get<MyUsageItem[]>(`/api/enterprise/api-key-applications/mine/usage?tenant_id=${TENANT_ID}`)
      .then((rows) => setUsageItems(rows))
      .catch(() => setUsageItems([]));
  };

  useEffect(() => {
    void load();
    void loadUsage();
  }, []);

  async function submit() {
    if (!canApply) return;
    const text = purpose.trim();
    if (!text) {
      notify.error('请填写申请用途');
      return;
    }
    setSubmitting(true);
    try {
      await api.post<ApiKeyApplication>('/api/enterprise/api-key-applications', {
        tenant_id: TENANT_ID,
        purpose: text,
      });
      notify.success('申请已提交，等待管理员审批');
      setPurpose('');
      await load();
    } catch (error) {
      const message = error instanceof ApiError ? error.message : '提交申请失败';
      notify.error(message);
    } finally {
      setSubmitting(false);
    }
  }

  function copy(text: string, label: string) {
    void navigator.clipboard
      .writeText(text)
      .then(() => notify.success(`${label}已复制`))
      .catch(() => notify.error('复制失败，请手动选择复制'));
  }

  const mineTab = (
    <section className="flex flex-col gap-[24px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px_18px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
      <div className="flex flex-col gap-[14px]">
        <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <KeyRound className="size-[14px] shrink-0" />
          <span className="text-[14px] font-normal leading-none">我的 API Key</span>
        </div>

        <p className="px-[12px] text-[12px] leading-[18px] text-[#757f9c]">
          每位用户最多申请 2 个 API Key。提交后由管理员审批，通过后自动分配 API Key 与网关地址。
        </p>

        <div className="px-[12px]">
          {loading ? (
            <div className="flex items-center justify-center gap-[8px] py-[40px] text-[13px] text-[#858b9c]">
              <LoaderCircle className="size-[16px] animate-spin" />
              加载中
            </div>
          ) : items.length === 0 ? (
            <div className="py-[40px] text-center text-[13px] text-[#858b9c]">暂无申请记录</div>
          ) : (
            <div className="flex flex-col gap-[10px]">
              {items.map((item) => {
                const meta = STATUS_META[item.status];
                const usage = usageItems.find((u) => u.id === item.id);
                const periodLabel =
                  usage?.quota_period === 'month'
                    ? `${new Date().getFullYear()}年${new Date().getMonth() + 1}月`
                    : usage?.quota_period === 'week'
                      ? '本周'
                      : usage?.quota_period === 'day'
                        ? '今日'
                        : '';
                return (
                  <div key={item.id} className="rounded-[12px] border border-[#eceef1] bg-white p-[14px]">
                    <div className="flex items-start justify-between gap-[10px]">
                      <div className="min-w-0 flex-1">
                        <span className="block truncate text-[13px] font-medium text-[#18181a]">
                          {item.purpose || '（未填写用途）'}
                        </span>
                        <span className="mt-[4px] block text-[12px] text-[#858b9c]">
                          申请于 {formatTime(item.created_at)}
                        </span>
                      </div>
                      <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>
                    </div>

                    {item.status === 'approved' && (
                      <div className="mt-[12px] flex flex-col gap-[8px]">
                        <label className="flex flex-col gap-[4px]">
                          <span className="text-[11px] font-medium text-[#464c5e]">API Key</span>
                          <div className="flex items-center gap-[6px]">
                            <code className="min-w-0 flex-1 truncate rounded-[8px] bg-[#f6f6f6] px-[10px] py-[7px] font-mono text-[12px] text-[#18181a]">
                              {revealedIds.has(item.id)
                                ? item.api_key || item.api_key_masked
                                : item.api_key_masked || '••••••••••••••••'}
                            </code>
                            <button
                              type="button"
                              aria-label={revealedIds.has(item.id) ? '隐藏 API Key' : '显示 API Key'}
                              onClick={() => toggleRevealed(item.id)}
                              className="grid size-[30px] shrink-0 place-items-center rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white text-[#757f9c] transition-colors hover:bg-black/5 hover:text-[#18181a]"
                            >
                              {revealedIds.has(item.id) ? <EyeOff className="size-[14px]" /> : <Eye className="size-[14px]" />}
                            </button>
                            <button
                              type="button"
                              aria-label="复制 API Key"
                              onClick={() => item.api_key && copy(item.api_key, 'API Key')}
                              className="grid size-[30px] shrink-0 place-items-center rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white text-[#757f9c] transition-colors hover:bg-black/5 hover:text-[#18181a]"
                            >
                              <Copy className="size-[14px]" />
                            </button>
                          </div>
                        </label>
                        {item.api_url && (
                          <label className="flex flex-col gap-[4px]">
                            <span className="text-[11px] font-medium text-[#464c5e]">网关地址</span>
                            <div className="flex items-center gap-[6px]">
                              <code className="min-w-0 flex-1 truncate rounded-[8px] bg-[#f6f6f6] px-[10px] py-[7px] font-mono text-[12px] text-[#18181a]">
                                {item.api_url}
                              </code>
                              <button
                                type="button"
                                aria-label="复制网关地址"
                                onClick={() => item.api_url && copy(item.api_url, '网关地址')}
                                className="grid size-[30px] shrink-0 place-items-center rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white text-[#757f9c] transition-colors hover:bg-black/5 hover:text-[#18181a]"
                              >
                                <Copy className="size-[14px]" />
                              </button>
                            </div>
                          </label>
                        )}

                        {(() => {
                          const usage = usageItems.find((u) => u.id === item.id);
                          if (!usage) return null;
                          const pct = Math.round(usage.usage_rate * 100);
                          const barColor =
                            pct >= 90 ? 'bg-[#ef4444]' : pct >= 70 ? 'bg-[#f59e0b]' : 'bg-[#22c55e]';
                          const meta = SUGGESTION_META[usage.suggestion] ?? { label: '正常', tone: 'green' as const };
                          return (
                            <div className="flex flex-col gap-[6px] rounded-[10px] bg-[#f8f9fb] px-[12px] py-[10px]">
                              <div className="flex items-center justify-between">
                                <span className="text-[11px] font-medium text-[#464c5e]">
                                  本期配额使用{periodLabel ? `（${periodLabel}）` : ''}
                                </span>
                                <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>
                              </div>
                              <div className="flex items-center gap-[8px]">
                                <div className="h-[6px] min-w-[100px] flex-1 overflow-hidden rounded-full bg-[#e8eaf0]">
                                  <div
                                    className={cn('h-full rounded-full transition-all', barColor)}
                                    style={{ width: `${Math.min(pct, 100)}%` }}
                                  />
                                </div>
                                <span className="text-[12px] tabular-nums text-[#464c5e]">{pct}%</span>
                              </div>
                              <div className="flex items-center justify-between text-[11px] text-[#858b9c]">
                                <span>
                                  已用{' '}
                                  <span className="font-medium tabular-nums text-[#18181a]">
                                    {usage.used_amount.toLocaleString('zh-CN')}
                                  </span>
                                </span>
                                <span>
                                  上限{' '}
                                  <span className="font-medium tabular-nums text-[#18181a]">
                                    {usage.quota_limit ? usage.quota_limit.toLocaleString('zh-CN') : '—'}
                                  </span>
                                  {usage.quota_period ? ` / ${PERIOD_LABEL[usage.quota_period] ?? usage.quota_period}` : ''}
                                </span>                              </div>
                            </div>
                          );
                        })()}
                      </div>
                    )}

                    {item.status === 'rejected' && item.reviewer_note && (
                      <p className="mt-[10px] rounded-[8px] bg-[#fdeeee] px-[10px] py-[8px] text-[12px] leading-[18px] text-[#c0392b]">
                        驳回原因：{item.reviewer_note}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="flex flex-col gap-[10px] border-t border-[#eef0f4] px-[12px] pt-[14px]">
          <div className="flex items-center justify-between">
            <span className="text-[12px] text-[#757f9c]">
              已申请 <span className="font-medium text-[#18181a]">{activeCount}</span> / {MAX_APPLICATIONS}
            </span>
            {!canApply && <span className="text-[12px] text-[#c0392b]">已达申请上限</span>}
          </div>

          {canApply ? (
            <>
              <Textarea
                rows={3}
                value={purpose}
                disabled={submitting}
                placeholder="请说明 API Key 的用途，例如：对接自有业务系统调用模型网关"
                onChange={(event) => setPurpose(event.target.value)}
                className="min-h-[72px] resize-y text-[12px]"
              />
              <div className="flex justify-end">
                <UIButton
                  disabled={submitting}
                  onClick={() => void submit()}
                  className={cn(
                    'h-[32px] rounded-[10px] bg-[#18181a] px-[16px] text-[13px] font-normal text-white',
                    'hover:bg-[#303030] disabled:opacity-60',
                  )}
                >
                  {submitting && <LoaderCircle className="size-[14px] animate-spin" />}
                  提交申请
                </UIButton>
              </div>
            </>
          ) : (
            <p className="text-[12px] leading-[18px] text-[#858b9c]">
              如需更多 API Key，请等待现有申请审批或联系管理员吊销已批准的 Key。
            </p>
          )}
        </div>
      </div>
    </section>
  );

  return mineTab;
}
