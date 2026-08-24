import { useEffect, useMemo, useState } from 'react';
import { ChevronLeft, ChevronRight, LoaderCircle, Search, SlidersHorizontal } from 'lucide-react';

import { api, ApiError, TENANT_ID } from '../api/client';
import { Button as UIButton } from '@/components/ui/button';
import { Dialog, DialogContent, DialogTitle, Input, Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';

const PERIOD_LABELS: Record<string, string> = {
  day: '日',
  week: '周',
  month: '月',
};

const SUGGESTION_META: Record<string, { label: string; tone: 'red' | 'orange' | 'green' | 'gray' }> = {
  expand: { label: '建议扩容', tone: 'red' },
  watch: { label: '关注', tone: 'orange' },
  normal: { label: '正常', tone: 'green' },
  unknown: { label: '未知', tone: 'gray' },
};

type UsageItem = {
  id: string;
  user_id: string;
  username: string | null;
  user_no: string | null;
  department: string | null;
  consumer_id: string | null;
  consumer_name: string | null;
  gateway_name: string | null;
  gateway_id: string | null;
  quota_limit: number | null;
  quota_period: string | null;
  quota_rule_id: string | null;
  used_amount: number;
  usage_rate: number;
  suggestion: string;
};

type UsageSummary = {
  allocated_users: number;
  total_quota: number;
  total_used: number;
  avg_usage_rate: number;
  high_watermark_users: number;
  low_watermark_users: number;
};

type UsageData = {
  month: string;
  summary: UsageSummary;
  items: UsageItem[];
};

function formatNumber(n: number): string {
  return n.toLocaleString('zh-CN');
}

function formatPercent(n: number): string {
  return `${Math.round(n * 100)}%`;
}

function getMonthOptions(): string[] {
  const options: string[] = [];
  const now = new Date();
  for (let i = 0; i < 12; i++) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    const value = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
    options.push(value);
  }
  return options;
}

function ProgressBar({ rate }: { rate: number }) {
  const percent = Math.max(0, Math.min(1, rate)) * 100;
  let colorClass = 'bg-[#3b82f6]';
  if (rate >= 0.9) colorClass = 'bg-[#ef4444]';
  else if (rate >= 0.7) colorClass = 'bg-[#f97316]';
  return (
    <div className="flex items-center gap-[10px]">
      <div className="h-[8px] w-[96px] overflow-hidden rounded-full bg-[#ebedf2]">
        <div
          className={cn('h-full rounded-full transition-all', colorClass)}
          style={{ width: `${percent}%` }}
        />
      </div>
      <span className="w-[44px] text-[12px] font-medium text-[#18181a]">{formatPercent(rate)}</span>
    </div>
  );
}

function SummaryCard({
  label,
  value,
  valueClassName,
}: {
  label: string;
  value: React.ReactNode;
  valueClassName?: string;
}) {
  return (
    <div className="flex flex-1 flex-col gap-[6px] rounded-[12px] border border-[#eceef1] bg-white p-[16px]">
      <span className={cn('text-[24px] font-semibold leading-[28px]', valueClassName || 'text-[#18181a]')}>
        {value}
      </span>
      <span className="text-[12px] text-[#858b9c]">{label}</span>
    </div>
  );
}

export function ApiKeyQuotaPanel() {
  const [data, setData] = useState<UsageData | null>(null);
  const [loading, setLoading] = useState(false);
  const [month, setMonth] = useState(() => {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
  });
  const [search, setSearch] = useState('');
  const [adjustTarget, setAdjustTarget] = useState<UsageItem | null>(null);
  const [adjustValue, setAdjustValue] = useState('');
  const [adjusting, setAdjusting] = useState(false);

  const monthOptions = useMemo(() => getMonthOptions(), []);

  const load = () => {
    setLoading(true);
    return api
      .get<UsageData>(`/api/enterprise/api-key-applications/usage?tenant_id=${TENANT_ID}&month=${encodeURIComponent(month)}`)
      .then((result) => setData(result))
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载用量失败'))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    void load();
  }, [month]);

  const filteredItems = useMemo(() => {
    const keyword = search.trim().toLowerCase();
    if (!keyword || !data) return data?.items || [];
    return data.items.filter((item) =>
      [item.username, item.user_no, item.department, item.consumer_name, item.gateway_name]
        .some((v) => (v || '').toLowerCase().includes(keyword)),
    );
  }, [data, search]);

  function openAdjust(item: UsageItem) {
    setAdjustTarget(item);
    setAdjustValue(String(item.quota_limit || 0));
  }

  async function confirmAdjust() {
    if (!adjustTarget) return;
    const limit = Number(adjustValue);
    if (!Number.isFinite(limit) || limit <= 0 || !Number.isInteger(limit)) {
      notify.error('配额必须是正整数');
      return;
    }
    setAdjusting(true);
    try {
      await api.post<UsageItem>(`/api/enterprise/api-key-applications/${adjustTarget.id}/quota`, {
        tenant_id: TENANT_ID,
        quota_limit: limit,
      });
      notify.success('配额已调整');
      setAdjustTarget(null);
      await load();
    } catch (error) {
      const message = error instanceof ApiError ? error.message : '调整配额失败';
      notify.error(message);
    } finally {
      setAdjusting(false);
    }
  }

  const columns: DataTableColumn<UsageItem>[] = [
    {
      key: 'user',
      title: '用户',
      width: 160,
      render: (row) => (
        <div className="flex items-center gap-[10px]">
          <div className="grid size-[28px] place-items-center rounded-full bg-[#18181a] text-[12px] font-medium text-white">
            {(row.username || 'U').slice(0, 1)}
          </div>
          <div className="flex min-w-0 flex-col">
            <span className="truncate text-[13px] font-medium text-[#18181a]">{row.username || row.user_id}</span>
            <span className="text-[11px] text-[#858b9c]">{row.user_no || '-'}</span>
          </div>
        </div>
      ),
    },
    {
      key: 'department',
      title: '部门',
      width: 120,
      render: (row) => <span className="text-[#18181a]">{row.department || '-'}</span>,
    },
    {
      key: 'consumer',
      title: '绑定消费组',
      width: 180,
      render: (row) => (
        <div className="flex flex-wrap gap-[6px]">
          <span className="rounded-[6px] bg-[#f2f3f7] px-[8px] py-[3px] text-[11px] text-[#464c5e]">
            {row.consumer_name || row.consumer_id || '-'}
          </span>
          {row.quota_period && (
            <span className="rounded-[6px] bg-[#eef3ff] px-[8px] py-[3px] text-[11px] text-[#1a71ff]">
              {PERIOD_LABELS[row.quota_period] || row.quota_period}
            </span>
          )}
        </div>
      ),
    },
    {
      key: 'quota',
      title: '分配额度',
      width: 110,
      align: 'right',
      render: (row) => <span className="text-[#18181a]">{formatNumber(row.quota_limit || 0)}</span>,
    },
    {
      key: 'used',
      title: '已使用',
      width: 110,
      align: 'right',
      render: (row) => <span className="text-[#18181a]">{formatNumber(row.used_amount)}</span>,
    },
    {
      key: 'usage_rate',
      title: '使用率',
      width: 170,
      render: (row) => <ProgressBar rate={row.usage_rate} />,
    },
    {
      key: 'suggestion',
      title: '建议',
      width: 110,
      render: (row) => {
        const meta = SUGGESTION_META[row.suggestion] || SUGGESTION_META.unknown;
        const toneClasses = {
          red: 'text-[#ef4444]',
          orange: 'text-[#f97316]',
          green: 'text-[#22c55e]',
          gray: 'text-[#858b9c]',
        };
        return <span className={cn('text-[12px] font-medium', toneClasses[meta.tone])}>{meta.label}</span>;
      },
    },
    {
      key: 'actions',
      title: '操作',
      width: 100,
      align: 'right',
      render: (row) => (
        <UIButton
          onClick={() => openAdjust(row)}
          className="h-[28px] rounded-[8px] bg-[#f97316] px-[12px] text-[12px] font-normal text-white hover:bg-[#ea580c]"
        >
          调整配额
        </UIButton>
      ),
    },
  ];

  const summary = data?.summary;

  return (
    <section className="flex flex-col gap-[20px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px_18px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
      {/* Header */}
      <div className="flex flex-col gap-[12px] px-[12px] pt-[4px]">
        <div className="flex items-center gap-[8px]">
          <span className="rounded-[6px] bg-[#eef3ff] px-[8px] py-[4px] text-[11px] font-medium text-[#1a71ff]">配额管理</span>
          <div className="ml-auto flex items-center gap-[8px]">
            <button
              type="button"
              aria-label="上个月"
              onClick={() => {
                const idx = monthOptions.indexOf(month);
                if (idx < monthOptions.length - 1) setMonth(monthOptions[idx + 1]);
              }}
              disabled={monthOptions.indexOf(month) >= monthOptions.length - 1}
              className="grid size-[28px] place-items-center rounded-[8px] border border-[#e3e7f1] text-[#757f9c] transition-colors hover:bg-black/5 disabled:opacity-40"
            >
              <ChevronLeft className="size-[14px]" />
            </button>
            <Select value={month} onValueChange={setMonth}>
              <SelectTrigger className="h-[28px] w-[120px] rounded-[8px] border-[#e3e7f1] bg-white px-[10px] text-[12px] text-[#18181a]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {monthOptions.map((m) => (
                  <SelectItem key={m} value={m} className="text-[12px]">
                    {m}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <button
              type="button"
              aria-label="下个月"
              onClick={() => {
                const idx = monthOptions.indexOf(month);
                if (idx > 0) setMonth(monthOptions[idx - 1]);
              }}
              disabled={monthOptions.indexOf(month) <= 0}
              className="grid size-[28px] place-items-center rounded-[8px] border border-[#e3e7f1] text-[#757f9c] transition-colors hover:bg-black/5 disabled:opacity-40"
            >
              <ChevronRight className="size-[14px]" />
            </button>
            <span className="ml-[6px] rounded-[6px] bg-[#f2f3f7] px-[8px] py-[4px] text-[11px] text-[#858b9c]">演示数据</span>
          </div>
        </div>
        <div>
          <h2 className="text-[22px] font-semibold leading-[28px] text-[#18181a]">API 用量分析</h2>
          <p className="mt-[4px] text-[12px] text-[#858b9c]">
            查看每个月已分配 API Key 的用户的使用情况，对比「已使用 / 分配」，按使用率调整后续配额。
          </p>
        </div>
      </div>

      {/* Summary cards */}
      <div className="flex flex-wrap gap-[12px] px-[12px]">
        <SummaryCard label="已分配用户" value={summary?.allocated_users ?? 0} />
        <SummaryCard label="分配总额" value={formatNumber(summary?.total_quota ?? 0)} />
        <SummaryCard label="已用总额" value={formatNumber(summary?.total_used ?? 0)} />
        <SummaryCard
          label="平均使用率"
          value={formatPercent(summary?.avg_usage_rate ?? 0)}
          valueClassName="text-[#f97316]"
        />
        <SummaryCard label="高水位用户" value={summary?.high_watermark_users ?? 0} valueClassName="text-[#ef4444]" />
        <SummaryCard label="低水位用户" value={summary?.low_watermark_users ?? 0} valueClassName="text-[#3b82f6]" />
      </div>

      {/* Detail table */}
      <div className="flex flex-col gap-[14px] px-[12px]">
        <div className="flex items-center justify-between">
          <h3 className="text-[14px] font-medium text-[#18181a]">{data?.month || month} 用量明细</h3>
          <label className="flex h-[34px] w-[260px] items-center gap-[8px] overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] transition-colors focus-within:border-[#18181a]">
            <Search className="size-[14px] shrink-0 text-[#858b9c]" />
            <input
              autoComplete="off"
              value={search}
              placeholder="搜索姓名 / 工号 / 消费组"
              onChange={(e) => setSearch(e.target.value)}
              className="h-full min-w-0 flex-1 bg-transparent text-[12px] text-[#17191f] outline-none placeholder:text-[#c0c6d4]"
            />
          </label>
        </div>

        {loading ? (
          <div className="flex items-center justify-center gap-[8px] py-[60px] text-[13px] text-[#858b9c]">
            <LoaderCircle className="size-[16px] animate-spin" />
            加载中
          </div>
        ) : (
          <DataTable
            aria-label="API Key 用量明细"
            columns={columns}
            data={filteredItems}
            rowKey={(row) => row.id}
            emptyText="暂无已签发的 API Key"
            size="compact"
          />
        )}
      </div>

      {/* Adjust quota dialog */}
      <Dialog open={Boolean(adjustTarget)} onOpenChange={(open) => !open && setAdjustTarget(null)}>
        <DialogContent
          aria-describedby={undefined}
          className="flex w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[420px]"
        >
          <div className="flex items-center gap-[8px]">
            <SlidersHorizontal className="size-[16px] text-[#757f9c]" />
            <DialogTitle className="text-[14px] font-medium text-[#18181a]">调整配额</DialogTitle>
          </div>
          {adjustTarget && (
            <div className="flex flex-col gap-[14px]">
              <div className="rounded-[10px] bg-[#f6f6f6] p-[12px] text-[12px] leading-[20px] text-[#464c5e]">
                <p>用户：{adjustTarget.username || adjustTarget.user_id}</p>
                <p>消费组：{adjustTarget.consumer_name || adjustTarget.consumer_id || '-'}</p>
                <p>当前配额：{formatNumber(adjustTarget.quota_limit || 0)} / {PERIOD_LABELS[adjustTarget.quota_period || ''] || adjustTarget.quota_period || '周期'}</p>
              </div>
              <label className="flex flex-col gap-[6px]">
                <span className="text-[12px] font-medium text-[#464c5e]">新配额（Token）</span>
                <Input
                  type="number"
                  min={1}
                  step={1}
                  value={adjustValue}
                  onChange={(e) => setAdjustValue(e.target.value)}
                  placeholder="例如 3000"
                  className="h-[36px] text-[13px]"
                />
              </label>
              <div className="flex justify-end gap-[8px]">
                <UIButton
                  variant="outline"
                  onClick={() => setAdjustTarget(null)}
                  className="h-[32px] rounded-[10px] border-[#e3e7f1] bg-white px-[16px] text-[12px] text-[#464c5e] hover:bg-[#f6f6f6]"
                >
                  取消
                </UIButton>
                <UIButton
                  disabled={adjusting}
                  onClick={() => void confirmAdjust()}
                  className="h-[32px] rounded-[10px] bg-[#18181a] px-[16px] text-[12px] text-white hover:bg-[#303030]"
                >
                  {adjusting && <LoaderCircle className="mr-[6px] size-[14px] animate-spin" />}
                  确认调整
                </UIButton>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </section>
  );
}
