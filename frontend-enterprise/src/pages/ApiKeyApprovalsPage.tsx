import { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle,
  Check,
  ChevronLeft,
  ChevronRight,
  KeyRound,
  LoaderCircle,
  Plus,
  Search,
  SlidersHorizontal,
  Trash2,
  Users,
  X,
} from 'lucide-react';

import AppHeader from '@/components/AppHeader';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  Input,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Textarea,
} from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { StatusBadge } from './scheduled-tasks/StatusBadge';
import { UnderlineTabs } from '@/components/ui/underline-tabs';
import { api, ApiError, TENANT_ID } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import { cn } from '@/lib/utils';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type GatewayOption = {
  name: string;
  gateway_id: string;
  gateway_url: string;
};

type ApiKeyApproval = {
  id: string;
  tenant_id: string;
  user_id: string;
  username: string | null;
  purpose: string | null;
  status: 'pending' | 'approved' | 'rejected' | 'revoked';
  api_key_masked: string | null;
  api_url: string | null;
  gateway_name: string | null;
  gateway_id: string | null;
  quota_limit: number | null;
  quota_period: string | null;
  quota_rule_id: string | null;
  quota_rule_name: string | null;
  consumer_id: string | null;
  consumer_name: string | null;
  consumer_group_id: string | null;
  consumer_group_name: string | null;
  used_amount: number | null;
  usage_month: string | null;
  reviewer_note: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
};

type ConsumerGroup = {
  id: string;
  tenant_id: string;
  name: string;
  description: string | null;
  gateway_id: string | null;
  gateway_name: string | null;
  external_consumer_id: string | null;
  consumer_type: string;
  status: string;
  created_by_user_id: string | null;
  created_at: string;
  updated_at: string;
};

type QuotaRule = {
  id: string;
  tenant_id: string;
  name: string;
  gateway_id: string | null;
  gateway_name: string | null;
  quota_dimension: string;
  quota_limit: number;
  period_type: string;
  external_rule_id: string | null;
  status: string;
  created_by_user_id: string | null;
  created_at: string;
  updated_at: string;
};

type ApprovalStats = {
  pending: number;
  allocated: number;
  history: number;
};

type UsageSummary = {
  allocated_users: number;
  total_quota: number;
  total_used: number;
  avg_usage_rate: number;
  high_watermark_users: number;
  low_watermark_users: number;
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

type UsageRead = {
  month: string;
  summary: UsageSummary;
  items: UsageItem[];
};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const STATUS_META: Record<ApiKeyApproval['status'], { tone: 'orange' | 'green' | 'red' | 'gray'; label: string }> = {
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

const SUGGESTION_META: Record<string, { label: string; tone: 'red' | 'orange' | 'green' | 'gray' }> = {
  expand: { label: '建议扩容', tone: 'red' },
  watch: { label: '关注', tone: 'orange' },
  normal: { label: '正常', tone: 'green' },
  unknown: { label: '未知', tone: 'gray' },
};

type TabValue = 'pending' | 'issued' | 'history' | 'quota' | 'groups';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatTime(value: string | null): string {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', { hour12: false });
}

function StatCard({
  label,
  value,
  tone,
  active,
  onClick,
}: {
  label: string;
  value: number;
  tone: 'orange' | 'green' | 'gray';
  active: boolean;
  onClick: () => void;
}) {
  const toneClass = {
    orange: 'text-[#f59e0b]',
    green: 'text-[#22c55e]',
    gray: 'text-[#858b9c]',
  }[tone];
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'flex w-[160px] flex-col items-center justify-center gap-[4px] rounded-[12px] border-[0.5px] px-[16px] py-[12px] text-center transition-colors',
        active
          ? 'border-[#18181a] bg-[#f6f6f6]'
          : 'border-[#e3e7f1] bg-white hover:border-[#cbd3e6] hover:bg-[#f8f9fb]',
      )}
    >
      <span className="text-[12px] text-[#858b9c]">{label}</span>
      <span className={cn('text-[24px] font-semibold leading-[28px]', toneClass)}>{value}</span>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function ApiKeyApprovalsPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
} = {}) {
  const [tab, setTab] = useState<TabValue>('pending');
  const [rows, setRows] = useState<ApiKeyApproval[]>([]);
  const [stats, setStats] = useState<ApprovalStats>({ pending: 0, allocated: 0, history: 0 });
  const [loading, setLoading] = useState(false);
  const [actingId, setActingId] = useState<string | null>(null);

  // Consumer groups
  const [groups, setGroups] = useState<ConsumerGroup[]>([]);
  const [groupsLoading, setGroupsLoading] = useState(false);

  // Quota rules
  const [rules, setRules] = useState<QuotaRule[]>([]);
  const [rulesLoading, setRulesLoading] = useState(false);

  // Gateways
  const [gateways, setGateways] = useState<GatewayOption[]>([]);

  // Usage
  const [usage, setUsage] = useState<UsageRead | null>(null);
  const [usageMonth, setUsageMonth] = useState(() => new Date().toISOString().slice(0, 7));
  const [usageLoading, setUsageLoading] = useState(false);
  const [usageSearch, setUsageSearch] = useState('');

  // Approve dialog
  const [approveTarget, setApproveTarget] = useState<ApiKeyApproval | null>(null);
  const [approveGroupId, setApproveGroupId] = useState('');
  const [approveRuleId, setApproveRuleId] = useState('');
  const [approving, setApproving] = useState(false);

  // Reject dialog
  const [rejectTarget, setRejectTarget] = useState<ApiKeyApproval | null>(null);
  const [rejectNote, setRejectNote] = useState('');
  const [rejecting, setRejecting] = useState(false);

  // Consumer group create dialog
  const [cgOpen, setCgOpen] = useState(false);
  const [cgName, setCgName] = useState('');
  const [cgDesc, setCgDesc] = useState('');
  const [cgGateway, setCgGateway] = useState('');
  const [cgSubmitting, setCgSubmitting] = useState(false);

  // Quota rule create dialog
  const [qrOpen, setQrOpen] = useState(false);
  const [qrName, setQrName] = useState('');
  const [qrGateway, setQrGateway] = useState('');
  const [qrDimension, setQrDimension] = useState<'token' | 'credit'>('token');
  const [qrLimit, setQrLimit] = useState('');
  const [qrPeriod, setQrPeriod] = useState<'day' | 'week' | 'month'>('month');
  const [qrSubmitting, setQrSubmitting] = useState(false);

  // Quota rule edit dialog
  const [editRule, setEditRule] = useState<QuotaRule | null>(null);
  const [editRuleName, setEditRuleName] = useState('');
  const [editRuleLimit, setEditRuleLimit] = useState('');
  const [editRulePeriod, setEditRulePeriod] = useState<'day' | 'week' | 'month'>('month');
  const [editRuleSubmitting, setEditRuleSubmitting] = useState(false);

  // Consumer group quota change dialog
  const [cgQuotaTarget, setCgQuotaTarget] = useState<ConsumerGroup | null>(null);
  const [cgQuotaRuleId, setCgQuotaRuleId] = useState('');
  const [cgQuotaSubmitting, setCgQuotaSubmitting] = useState(false);

  // Adjust quota dialog (for usage tab)
  const [adjustTarget, setAdjustTarget] = useState<UsageItem | null>(null);
  const [adjustLimit, setAdjustLimit] = useState('');
  const [adjustSubmitting, setAdjustSubmitting] = useState(false);

  // ------------------------------------------------------------------------
  // Data loading
  // ------------------------------------------------------------------------

  const loadApplications = useCallback(() => {
    setLoading(true);
    return api
      .get<ApiKeyApproval[]>(`/api/enterprise/api-key-applications?tenant_id=${TENANT_ID}`)
      .then((items) => setRows(items))
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载申请失败'))
      .finally(() => setLoading(false));
  }, []);

  const loadStats = useCallback(() => {
    api
      .get<ApprovalStats>(`/api/enterprise/api-key-applications/stats?tenant_id=${TENANT_ID}`)
      .then((s) => setStats(s))
      .catch(() => {});
  }, []);

  const loadGateways = useCallback(() => {
    api
      .get<GatewayOption[]>('/api/enterprise/api-key-applications/gateways')
      .then((items) => setGateways(items))
      .catch(() => setGateways([]));
  }, []);

  const loadGroups = useCallback(() => {
    setGroupsLoading(true);
    return api
      .get<ConsumerGroup[]>(`/api/enterprise/api-key-applications/consumer-groups?tenant_id=${TENANT_ID}`)
      .then((items) => setGroups(items))
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载消费组失败'))
      .finally(() => setGroupsLoading(false));
  }, []);

  const loadRules = useCallback(() => {
    setRulesLoading(true);
    return api
      .get<QuotaRule[]>(`/api/enterprise/api-key-applications/quota-rules?tenant_id=${TENANT_ID}`)
      .then((items) => setRules(items))
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载配额规则失败'))
      .finally(() => setRulesLoading(false));
  }, []);

  const loadUsage = useCallback((month?: string) => {
    setUsageLoading(true);
    const m = month ?? usageMonth;
    return api
      .get<UsageRead>(`/api/enterprise/api-key-applications/usage?tenant_id=${TENANT_ID}&month=${m}`)
      .then((data) => setUsage(data))
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载用量失败'))
      .finally(() => setUsageLoading(false));
  }, [usageMonth]);

  useEffect(() => {
    void loadApplications();
    void loadStats();
    void loadGateways();
    void loadGroups();
    void loadRules();
  }, [loadApplications, loadStats, loadGateways, loadGroups, loadRules]);

  useEffect(() => {
    if (tab === 'groups') void loadGroups();
  }, [tab, loadGroups]);

  useEffect(() => {
    if (tab === 'quota') {
      void loadRules();
      void loadUsage();
    }
  }, [tab, loadRules, loadUsage]);

  // ------------------------------------------------------------------------
  // Actions
  // ------------------------------------------------------------------------

  function openApprove(item: ApiKeyApproval) {
    setApproveTarget(item);
    setApproveGroupId('');
    setApproveRuleId('');
  }

  async function confirmApprove() {
    if (!approveTarget) return;
    if (!approveGroupId) {
      notify.error('请选择消费组');
      return;
    }
    if (!approveRuleId) {
      notify.error('请选择配额规则');
      return;
    }
    setApproving(true);
    try {
      await api.post<ApiKeyApproval>(`/api/enterprise/api-key-applications/${approveTarget.id}/approve`, {
        tenant_id: TENANT_ID,
        consumer_group_id: approveGroupId,
        quota_rule_id: approveRuleId,
      });
      notify.success('已通过：已在阿里云分配 API Key 并关联消费组与配额规则');
      setApproveTarget(null);
      await Promise.all([loadApplications(), loadStats()]);
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '操作失败');
    } finally {
      setApproving(false);
    }
  }

  async function revoke(item: ApiKeyApproval) {
    setActingId(item.id);
    try {
      await api.post<ApiKeyApproval>(`/api/enterprise/api-key-applications/${item.id}/revoke`, {
        tenant_id: TENANT_ID,
      });
      notify.success('已吊销该 API Key');
      await Promise.all([loadApplications(), loadStats()]);
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
      await Promise.all([loadApplications(), loadStats()]);
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '操作失败');
    } finally {
      setRejecting(false);
    }
  }

  // -- Consumer group actions --

  async function submitConsumerGroup() {
    if (!cgName.trim()) {
      notify.error('请填写消费组名称');
      return;
    }
    if (!cgGateway) {
      notify.error('请选择网关');
      return;
    }
    setCgSubmitting(true);
    try {
      await api.post<ConsumerGroup>('/api/enterprise/api-key-applications/consumer-groups', {
        tenant_id: TENANT_ID,
        name: cgName.trim(),
        description: cgDesc.trim() || undefined,
        gateway_name: cgGateway,
      });
      notify.success('消费组创建成功');
      setCgOpen(false);
      setCgName('');
      setCgDesc('');
      setCgGateway('');
      await loadGroups();
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '创建失败');
    } finally {
      setCgSubmitting(false);
    }
  }

  async function deleteConsumerGroup(group: ConsumerGroup) {
    setActingId(group.id);
    try {
      await api.delete<ConsumerGroup>(
        `/api/enterprise/api-key-applications/consumer-groups/${group.id}?tenant_id=${TENANT_ID}`,
      );
      notify.success('消费组已删除');
      await loadGroups();
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '删除失败');
    } finally {
      setActingId(null);
    }
  }

  async function submitCgQuotaChange() {
    if (!cgQuotaTarget || !cgQuotaRuleId) return;
    setCgQuotaSubmitting(true);
    try {
      await api.post<ConsumerGroup>(
        `/api/enterprise/api-key-applications/consumer-groups/${cgQuotaTarget.id}/quota`,
        {
          tenant_id: TENANT_ID,
          quota_rule_id: cgQuotaRuleId,
        },
      );
      notify.success('配额规则已关联');
      setCgQuotaTarget(null);
      setCgQuotaRuleId('');
      await loadGroups();
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '操作失败');
    } finally {
      setCgQuotaSubmitting(false);
    }
  }

  // -- Quota rule actions --

  async function submitQuotaRule() {
    if (!qrName.trim()) {
      notify.error('请填写规则名称');
      return;
    }
    if (!qrGateway) {
      notify.error('请选择网关');
      return;
    }
    const limit = Number(qrLimit);
    if (!Number.isInteger(limit) || limit <= 0) {
      notify.error('请填写大于 0 的整数配额');
      return;
    }
    setQrSubmitting(true);
    try {
      await api.post<QuotaRule>('/api/enterprise/api-key-applications/quota-rules', {
        tenant_id: TENANT_ID,
        name: qrName.trim(),
        gateway_name: qrGateway,
        quota_dimension: qrDimension,
        quota_limit: limit,
        period_type: qrPeriod,
      });
      notify.success('配额规则创建成功');
      setQrOpen(false);
      setQrName('');
      setQrGateway('');
      setQrDimension('token');
      setQrLimit('');
      setQrPeriod('month');
      await loadRules();
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '创建失败');
    } finally {
      setQrSubmitting(false);
    }
  }

  async function submitEditRule() {
    if (!editRule) return;
    const limit = Number(editRuleLimit);
    if (!Number.isInteger(limit) || limit <= 0) {
      notify.error('请填写大于 0 的整数配额');
      return;
    }
    setEditRuleSubmitting(true);
    try {
      await api.put<QuotaRule>(`/api/enterprise/api-key-applications/quota-rules/${editRule.id}`, {
        tenant_id: TENANT_ID,
        name: editRuleName.trim() || undefined,
        quota_limit: limit,
        period_type: editRulePeriod,
      });
      notify.success('配额规则已更新');
      setEditRule(null);
      await loadRules();
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '更新失败');
    } finally {
      setEditRuleSubmitting(false);
    }
  }

  async function deleteQuotaRule(rule: QuotaRule) {
    setActingId(rule.id);
    try {
      await api.delete<QuotaRule>(
        `/api/enterprise/api-key-applications/quota-rules/${rule.id}?tenant_id=${TENANT_ID}`,
      );
      notify.success('配额规则已删除');
      await loadRules();
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '删除失败');
    } finally {
      setActingId(null);
    }
  }

  // -- Adjust quota (usage tab) --

  async function submitAdjustQuota() {
    if (!adjustTarget) return;
    const limit = Number(adjustLimit);
    if (!Number.isInteger(limit) || limit <= 0) {
      notify.error('请填写大于 0 的整数配额');
      return;
    }
    setAdjustSubmitting(true);
    try {
      await api.post<ApiKeyApproval>(
        `/api/enterprise/api-key-applications/${adjustTarget.id}/quota`,
        {
          tenant_id: TENANT_ID,
          quota_limit: limit,
        },
      );
      notify.success('配额已调整');
      setAdjustTarget(null);
      setAdjustLimit('');
      await Promise.all([loadApplications(), loadUsage()]);
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '调整失败');
    } finally {
      setAdjustSubmitting(false);
    }
  }

  // ------------------------------------------------------------------------
  // Derived data
  // ------------------------------------------------------------------------

  const pendingRows = rows.filter((r) => r.status === 'pending');
  const issuedRows = rows.filter((r) => r.status === 'approved');
  const historyRows = rows.filter((r) => r.status === 'rejected' || r.status === 'revoked');

  // Rules that match the selected consumer group's gateway (for approve dialog)
  const approveCompatibleRules = approveGroupId
    ? rules.filter((r) => {
        const g = groups.find((x) => x.id === approveGroupId);
        return g && r.gateway_id === g.gateway_id;
      })
    : rules;

  // Filtered usage items
  const filteredUsageItems = usage?.items.filter((item) => {
    if (!usageSearch) return true;
    const q = usageSearch.toLowerCase();
    return (
      (item.username?.toLowerCase().includes(q) ?? false) ||
      (item.user_no?.toLowerCase().includes(q) ?? false) ||
      (item.consumer_name?.toLowerCase().includes(q) ?? false) ||
      (item.gateway_name?.toLowerCase().includes(q) ?? false)
    );
  }) ?? [];

  // ------------------------------------------------------------------------
  // Column definitions
  // ------------------------------------------------------------------------

  const pendingColumns: DataTableColumn<ApiKeyApproval>[] = [
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
      render: (row) => (
        <span className="block truncate text-[13px] text-[#464c5e]">{row.purpose || '（未填写）'}</span>
      ),
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
      width: 180,
      align: 'right',
      render: (row) => {
        const busy = actingId === row.id;
        return (
          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              disabled={busy}
              onClick={() => openApprove(row)}
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
      },
    },
  ];

  const issuedColumns: DataTableColumn<ApiKeyApproval>[] = [
    {
      key: 'applicant',
      title: '申请人',
      width: 130,
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
      render: (row) => (
        <span className="block truncate text-[13px] text-[#464c5e]">{row.purpose || '（未填写）'}</span>
      ),
    },
    {
      key: 'gateway',
      title: '网关',
      width: 110,
      render: (row) =>
        row.gateway_name ? (
          <span className="block truncate text-[13px] text-[#464c5e]">{row.gateway_name}</span>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'quota',
      title: '配额',
      width: 120,
      render: (row) =>
        row.quota_limit ? (
          <span className="text-[12px] text-[#464c5e]">
            {row.quota_limit.toLocaleString('zh-CN')} / {PERIOD_LABEL[row.quota_period ?? ''] ?? row.quota_period ?? '-'}
          </span>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'consumer_group',
      title: '消费组',
      width: 120,
      render: (row) =>
        row.consumer_group_name ? (
          <span className="block truncate text-[12px] text-[#464c5e]">{row.consumer_group_name}</span>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'api_url',
      title: '网关地址',
      className: 'whitespace-normal',
      render: (row) =>
        row.api_url ? (
          <code className="block truncate font-mono text-[12px] text-[#464c5e]">{row.api_url}</code>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'api_key',
      title: 'API Key',
      width: 120,
      render: (row) =>
        row.api_key_masked ? (
          <code className="block truncate font-mono text-[12px] text-[#858b9c]">{row.api_key_masked}</code>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'actions',
      title: '操作',
      width: 90,
      align: 'right',
      render: (row) => {
        const busy = actingId === row.id;
        return (
          <UIButton
            disabled={busy}
            onClick={() => void revoke(row)}
            className="h-[30px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#757f9c] hover:bg-black/5 hover:text-[#18181a] disabled:opacity-60"
          >
            {busy ? <LoaderCircle className="size-[13px] animate-spin" /> : null}
            吊销
          </UIButton>
        );
      },
    },
  ];

  const historyColumns: DataTableColumn<ApiKeyApproval>[] = [
    {
      key: 'applicant',
      title: '申请人',
      width: 130,
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
      render: (row) => (
        <span className="block truncate text-[13px] text-[#464c5e]">{row.purpose || '（未填写）'}</span>
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
      key: 'reviewer_note',
      title: '原因',
      render: (row) =>
        row.reviewer_note ? (
          <span className="block truncate text-[12px] text-[#858b9c]">{row.reviewer_note}</span>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'reviewed_at',
      title: '审核时间',
      width: 160,
      render: (row) => <span className="text-[12px] text-[#858b9c]">{formatTime(row.reviewed_at)}</span>,
    },
    {
      key: 'created_at',
      title: '申请时间',
      width: 160,
      render: (row) => <span className="text-[12px] text-[#858b9c]">{formatTime(row.created_at)}</span>,
    },
  ];

  const groupColumns: DataTableColumn<ConsumerGroup>[] = [
    {
      key: 'name',
      title: '消费组名称',
      width: 180,
      render: (row) => (
        <div className="flex min-w-0 flex-col gap-[2px]">
          <span className="truncate text-[13px] font-medium text-[#18181a]">{row.name}</span>
          {row.description && (
            <span className="truncate text-[12px] text-[#858b9c]">{row.description}</span>
          )}
        </div>
      ),
    },
    {
      key: 'gateway',
      title: '网关',
      width: 120,
      render: (row) =>
        row.gateway_name ? (
          <span className="text-[13px] text-[#464c5e]">{row.gateway_name}</span>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'consumer_id',
      title: '消费者 ID',
      render: (row) =>
        row.external_consumer_id ? (
          <code className="block truncate font-mono text-[12px] text-[#858b9c]">{row.external_consumer_id}</code>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'type',
      title: '类型',
      width: 80,
      render: (row) => <span className="text-[12px] text-[#464c5e]">{row.consumer_type}</span>,
    },
    {
      key: 'status',
      title: '状态',
      width: 80,
      render: (row) => <span className="text-[12px] text-[#464c5e]">{row.status}</span>,
    },
    {
      key: 'actions',
      title: '操作',
      width: 180,
      align: 'right',
      render: (row) => {
        const busy = actingId === row.id;
        return (
          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              disabled={busy}
              onClick={() => {
                setCgQuotaTarget(row);
                setCgQuotaRuleId('');
              }}
              className="h-[30px] gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6] disabled:opacity-60"
            >
              <SlidersHorizontal className="size-[13px]" />
              改配额
            </UIButton>
            <UIButton
              disabled={busy}
              onClick={() => void deleteConsumerGroup(row)}
              className="h-[30px] gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#c0392b] hover:bg-[#fdeeee] disabled:opacity-60"
            >
              {busy ? <LoaderCircle className="size-[13px] animate-spin" /> : <Trash2 className="size-[13px]" />}
              删除
            </UIButton>
          </div>
        );
      },
    },
  ];

  const ruleColumns: DataTableColumn<QuotaRule>[] = [
    {
      key: 'name',
      title: '规则名称',
      width: 180,
      render: (row) => (
        <span className="block truncate text-[13px] font-medium text-[#18181a]">{row.name}</span>
      ),
    },
    {
      key: 'gateway',
      title: '网关',
      width: 120,
      render: (row) =>
        row.gateway_name ? (
          <span className="text-[13px] text-[#464c5e]">{row.gateway_name}</span>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'dimension',
      title: '维度',
      width: 80,
      render: (row) => <span className="text-[12px] text-[#464c5e]">{row.quota_dimension}</span>,
    },
    {
      key: 'limit',
      title: '额度',
      width: 100,
      render: (row) => (
        <span className="text-[13px] font-medium text-[#18181a]">
          {row.quota_limit.toLocaleString('zh-CN')}
        </span>
      ),
    },
    {
      key: 'period',
      title: '周期',
      width: 80,
      render: (row) => (
        <span className="text-[12px] text-[#464c5e]">{PERIOD_LABEL[row.period_type] ?? row.period_type}</span>
      ),
    },
    {
      key: 'rule_id',
      title: '规则 ID',
      render: (row) =>
        row.external_rule_id ? (
          <code className="block truncate font-mono text-[12px] text-[#858b9c]">{row.external_rule_id}</code>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'actions',
      title: '操作',
      width: 150,
      align: 'right',
      render: (row) => {
        const busy = actingId === row.id;
        return (
          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              disabled={busy}
              onClick={() => {
                setEditRule(row);
                setEditRuleName(row.name);
                setEditRuleLimit(String(row.quota_limit));
                setEditRulePeriod(row.period_type as 'day' | 'week' | 'month');
              }}
              className="h-[30px] gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6] disabled:opacity-60"
            >
              <SlidersHorizontal className="size-[13px]" />
              编辑
            </UIButton>
            <UIButton
              disabled={busy}
              onClick={() => void deleteQuotaRule(row)}
              className="h-[30px] gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#c0392b] hover:bg-[#fdeeee] disabled:opacity-60"
            >
              {busy ? <LoaderCircle className="size-[13px] animate-spin" /> : <Trash2 className="size-[13px]" />}
              删除
            </UIButton>
          </div>
        );
      },
    },
  ];

  const usageColumns: DataTableColumn<UsageItem>[] = [
    {
      key: 'user',
      title: '用户',
      width: 140,
      render: (row) => (
        <div className="flex min-w-0 flex-col gap-[2px]">
          <span className="truncate text-[13px] font-medium text-[#18181a]">{row.username || '-'}</span>
          <span className="truncate text-[12px] text-[#858b9c]">{row.user_no || row.user_id}</span>
        </div>
      ),
    },
    {
      key: 'gateway',
      title: '网关',
      width: 110,
      render: (row) =>
        row.gateway_name ? (
          <span className="text-[13px] text-[#464c5e]">{row.gateway_name}</span>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'quota',
      title: '配额',
      width: 100,
      render: (row) =>
        row.quota_limit ? (
          <span className="text-[12px] text-[#464c5e]">
            {row.quota_limit.toLocaleString('zh-CN')} / {PERIOD_LABEL[row.quota_period ?? ''] ?? '-'}
          </span>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'used',
      title: '已用',
      width: 90,
      render: (row) => (
        <span className="text-[13px] font-medium text-[#18181a]">
          {row.used_amount.toLocaleString('zh-CN')}
        </span>
      ),
    },
    {
      key: 'rate',
      title: '使用率',
      width: 180,
      render: (row) => {
        const pct = Math.round(row.usage_rate * 100);
        const barColor =
          pct >= 90
            ? 'bg-[#ef4444]'
            : pct >= 70
              ? 'bg-[#f59e0b]'
              : 'bg-[#22c55e]';
        return (
          <div className="flex items-center gap-[8px]">
            <div className="h-[6px] min-w-[80px] flex-1 overflow-hidden rounded-full bg-[#eef0f4]">
              <div className={cn('h-full rounded-full transition-all', barColor)} style={{ width: `${Math.min(pct, 100)}%` }} />
            </div>
            <span className="text-[12px] tabular-nums text-[#464c5e]">{pct}%</span>
          </div>
        );
      },
    },
    {
      key: 'suggestion',
      title: '水位建议',
      width: 100,
      render: (row) => {
        const meta = SUGGESTION_META[row.suggestion] ?? SUGGESTION_META.unknown;
        return <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>;
      },
    },
    {
      key: 'actions',
      title: '操作',
      width: 100,
      align: 'right',
      render: (row) => (
        <UIButton
          onClick={() => {
            setAdjustTarget(row);
            setAdjustLimit(row.quota_limit ? String(row.quota_limit) : '');
          }}
          className="h-[30px] gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]"
        >
          <SlidersHorizontal className="size-[13px]" />
          调配额
        </UIButton>
      ),
    },
  ];

  // ------------------------------------------------------------------------
  // Render
  // ------------------------------------------------------------------------

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        className="items-center"
        onLogout={onLogout}
        userName={currentUser?.username}
        title="API Key 审批"
        description="管理 API Key 申请审批、消费组、配额规则与用量分析"
      />

      {/* Stats cards */}
      <div className="mt-[20px] flex justify-end gap-[12px]">
        <StatCard
          label="待审核"
          value={stats.pending}
          tone="orange"
          active={tab === 'pending'}
          onClick={() => setTab('pending')}
        />
        <StatCard
          label="已分配密钥"
          value={stats.allocated}
          tone="green"
          active={tab === 'issued'}
          onClick={() => setTab('issued')}
        />
        <StatCard
          label="审核历史"
          value={stats.history}
          tone="gray"
          active={tab === 'history'}
          onClick={() => setTab('history')}
        />
      </div>

      {/* Tabs */}
      <UnderlineTabs
        className="mt-[20px]"
        variant="line"
        aria-label="密钥审核管理"
        value={tab}
        onChange={setTab}
        items={[
          { value: 'pending', label: '待审核' },
          { value: 'issued', label: '已分配密钥' },
          { value: 'history', label: '审核历史' },
          { value: 'quota', label: '配额管理' },
          { value: 'groups', label: '消费组管理' },
        ]}
      />

      {/* Tab content */}
      <div className="mt-[16px] flex flex-col gap-[24px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px_18px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        {/* Pending */}
        {tab === 'pending' && (
          <>
            <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
              <KeyRound className="size-[14px] shrink-0" />
              <span className="text-[14px] font-normal leading-none">待审核申请</span>
            </div>
            <div className="hidden md:block">
              <DataTable
                aria-label="待审核申请"
                columns={pendingColumns}
                data={pendingRows}
                rowKey={(row) => row.id}
                loading={loading}
                emptyText="暂无待审核申请"
              />
            </div>
            {/* Mobile fallback */}
            <div className="grid gap-[10px] md:hidden">
              {loading ? (
                <div className="py-[40px] text-center text-[13px] text-[#858b9c]">加载中</div>
              ) : pendingRows.length === 0 ? (
                <div className="py-[40px] text-center text-[13px] text-[#858b9c]">暂无待审核申请</div>
              ) : (
                pendingRows.map((row) => (
                  <article key={row.id} className="min-w-0 rounded-[12px] border border-[#eceef1] bg-white p-[14px]">
                    <div className="flex min-w-0 items-start justify-between gap-[10px]">
                      <div className="min-w-0">
                        <span className="block truncate text-[13px] font-medium text-[#18181a]">{row.username || '-'}</span>
                        <span className="mt-[2px] block truncate text-[12px] text-[#858b9c]">{row.purpose || '（未填写用途）'}</span>
                      </div>
                      <StatusBadge tone="orange">待审批</StatusBadge>
                    </div>
                    <span className="mt-[8px] block text-[12px] text-[#858b9c]">申请于 {formatTime(row.created_at)}</span>
                    <div className="mt-[12px] flex items-center justify-end gap-[8px]">
                      <UIButton
                        onClick={() => openApprove(row)}
                        className="h-[30px] gap-[4px] rounded-[8px] bg-[#18181a] px-[12px] text-[12px] font-normal text-white"
                      >
                        <Check className="size-[13px]" />
                        通过
                      </UIButton>
                      <UIButton
                        onClick={() => {
                          setRejectTarget(row);
                          setRejectNote('');
                        }}
                        className="h-[30px] gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#c0392b]"
                      >
                        <X className="size-[13px]" />
                        驳回
                      </UIButton>
                    </div>
                  </article>
                ))
              )}
            </div>
          </>
        )}

        {/* Issued */}
        {tab === 'issued' && (
          <>
            <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
              <KeyRound className="size-[14px] shrink-0" />
              <span className="text-[14px] font-normal leading-none">已分配密钥</span>
            </div>
            <DataTable
              aria-label="已分配密钥"
              columns={issuedColumns}
              data={issuedRows}
              rowKey={(row) => row.id}
              loading={loading}
              emptyText="暂无已分配密钥"
            />
          </>
        )}

        {/* History */}
        {tab === 'history' && (
          <>
            <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
              <KeyRound className="size-[14px] shrink-0" />
              <span className="text-[14px] font-normal leading-none">审核历史</span>
            </div>
            <DataTable
              aria-label="审核历史"
              columns={historyColumns}
              data={historyRows}
              rowKey={(row) => row.id}
              loading={loading}
              emptyText="暂无审核历史"
            />
          </>
        )}

        {/* Quota management */}
        {tab === 'quota' && (
          <>
            {/* Usage summary cards */}
            {usage && (
              <div className="grid grid-cols-2 gap-[10px] px-[12px] md:grid-cols-3 lg:grid-cols-6">
                {[
                  { label: '已分配用户', value: usage.summary.allocated_users },
                  { label: '配额总额', value: usage.summary.total_quota.toLocaleString('zh-CN') },
                  { label: '已用总额', value: usage.summary.total_used.toLocaleString('zh-CN') },
                  {
                    label: '平均使用率',
                    value: `${Math.round(usage.summary.avg_usage_rate * 100)}%`,
                  },
                  { label: '高水位用户', value: usage.summary.high_watermark_users },
                  { label: '低水位用户', value: usage.summary.low_watermark_users },
                ].map((card) => (
                  <div
                    key={card.label}
                    className="flex flex-col gap-[4px] rounded-[12px] border-[0.5px] border-[#e3e7f1] bg-[#f8f9fb] px-[14px] py-[10px]"
                  >
                    <span className="text-[11px] text-[#858b9c]">{card.label}</span>
                    <span className="text-[20px] font-semibold leading-[24px] text-[#18181a]">{card.value}</span>
                  </div>
                ))}
              </div>
            )}

            {/* Month selector + search for usage table */}
            <div className="flex flex-wrap items-center gap-[8px] px-[12px]">
              <div className="flex items-center gap-[6px]">
                <button
                  type="button"
                  onClick={() => {
                    const d = new Date(usageMonth + '-01');
                    d.setMonth(d.getMonth() - 1);
                    const m = d.toISOString().slice(0, 7);
                    setUsageMonth(m);
                    void loadUsage(m);
                  }}
                  className="grid size-[28px] place-items-center rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white text-[#757f9c] hover:bg-[#f6f6f6]"
                >
                  <ChevronLeft className="size-[14px]" />
                </button>
                <span className="text-[13px] font-medium text-[#18181a] tabular-nums">{usageMonth}</span>
                <button
                  type="button"
                  onClick={() => {
                    const d = new Date(usageMonth + '-01');
                    d.setMonth(d.getMonth() + 1);
                    const m = d.toISOString().slice(0, 7);
                    setUsageMonth(m);
                    void loadUsage(m);
                  }}
                  className="grid size-[28px] place-items-center rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white text-[#757f9c] hover:bg-[#f6f6f6]"
                >
                  <ChevronRight className="size-[14px]" />
                </button>
              </div>
              <div className="relative flex-1 max-w-[240px]">
                <Search className="absolute left-[8px] top-1/2 size-[14px] -translate-y-1/2 text-[#c0c6d4]" />
                <Input
                  value={usageSearch}
                  onChange={(e) => setUsageSearch(e.target.value)}
                  placeholder="搜索用户 / 消费组"
                  className="h-[30px] pl-[28px] text-[12px]"
                />
              </div>
            </div>

            {/* Usage table */}
            <DataTable
              aria-label="用量明细"
              columns={usageColumns}
              data={filteredUsageItems}
              rowKey={(row) => row.id}
              loading={usageLoading}
              emptyText="暂无用量数据"
            />

            {/* Quota rules section */}
            <div className="flex items-center justify-between px-[12px] pt-[8px]">
              <div className="flex items-center gap-[6px] text-[#757f9c]">
                <SlidersHorizontal className="size-[14px] shrink-0" />
                <span className="text-[14px] font-normal leading-none">配额规则</span>
              </div>
              <UIButton
                onClick={() => {
                  setQrOpen(true);
                  setQrName('');
                  setQrGateway(gateways[0]?.name ?? '');
                  setQrDimension('token');
                  setQrLimit('');
                  setQrPeriod('month');
                }}
                className="h-[30px] gap-[4px] rounded-[8px] bg-[#18181a] px-[12px] text-[12px] font-normal text-white hover:bg-[#303030]"
              >
                <Plus className="size-[13px]" />
                新建规则
              </UIButton>
            </div>
            <DataTable
              aria-label="配额规则列表"
              columns={ruleColumns}
              data={rules}
              rowKey={(row) => row.id}
              loading={rulesLoading}
              emptyText="暂无配额规则，点击右上角新建"
            />
          </>
        )}

        {/* Consumer groups */}
        {tab === 'groups' && (
          <>
            <div className="flex items-center justify-between px-[12px]">
              <div className="flex items-center gap-[6px] text-[#757f9c]">
                <Users className="size-[14px] shrink-0" />
                <span className="text-[14px] font-normal leading-none">消费组管理</span>
              </div>
              <UIButton
                onClick={() => {
                  setCgOpen(true);
                  setCgName('');
                  setCgDesc('');
                  setCgGateway(gateways[0]?.name ?? '');
                }}
                className="h-[30px] gap-[4px] rounded-[8px] bg-[#18181a] px-[12px] text-[12px] font-normal text-white hover:bg-[#303030]"
              >
                <Plus className="size-[13px]" />
                新建消费组
              </UIButton>
            </div>
            <DataTable
              aria-label="消费组列表"
              columns={groupColumns}
              data={groups}
              rowKey={(row) => row.id}
              loading={groupsLoading}
              emptyText="暂无消费组，点击右上角新建"
            />
          </>
        )}
      </div>

      {/* ----------------------------------------------------------------- */}
      {/* Approve dialog: select consumer group + quota rule */}
      {/* ----------------------------------------------------------------- */}
      <Dialog open={Boolean(approveTarget)} onOpenChange={(open) => !open && setApproveTarget(null)}>
        <DialogContent
          aria-describedby={undefined}
          className="flex w-[calc(100%-2rem)] flex-col gap-[14px] rounded-[14px] px-[20px] py-[16px] sm:max-w-[480px]"
        >
          <DialogTitle className="text-[15px] font-medium text-[#18181a]">通过申请并分配 API Key</DialogTitle>
          <p className="text-[12px] leading-[18px] text-[#757f9c]">
            申请：{approveTarget?.purpose || '（未填写用途）'} · 申请人 {approveTarget?.username || '-'}
          </p>

          {/* Consumer group select */}
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">消费组</span>
            <Select
              value={approveGroupId}
              onValueChange={(value) => {
                setApproveGroupId(value);
                setApproveRuleId('');
              }}
            >
              <SelectTrigger className="h-[34px]">
                <SelectValue placeholder="选择消费组" />
              </SelectTrigger>
              <SelectContent>
                {groups.length === 0 ? (
                  <SelectItem value="__none__" disabled>
                    无可用消费组（请先在「消费组管理」创建）
                  </SelectItem>
                ) : (
                  groups.map((g) => (
                    <SelectItem key={g.id} value={g.id}>
                      {g.name} ({g.gateway_name || '-'})
                    </SelectItem>
                  ))
                )}
              </SelectContent>
            </Select>
          </label>

          {/* Quota rule select (filtered to same gateway) */}
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">配额规则</span>
            <Select value={approveRuleId} onValueChange={setApproveRuleId}>
              <SelectTrigger className="h-[34px]">
                <SelectValue placeholder={approveGroupId ? '选择配额规则' : '请先选择消费组'} />
              </SelectTrigger>
              <SelectContent>
                {!approveGroupId || approveCompatibleRules.length === 0 ? (
                  <SelectItem value="__none__" disabled>
                    {approveGroupId ? '无同网关配额规则' : '请先选择消费组'}
                  </SelectItem>
                ) : (
                  approveCompatibleRules.map((r) => (
                    <SelectItem key={r.id} value={r.id}>
                      {r.name} ({r.quota_limit.toLocaleString('zh-CN')}/{PERIOD_LABEL[r.period_type] ?? r.period_type})
                    </SelectItem>
                  ))
                )}
              </SelectContent>
            </Select>
          </label>

          <p className="text-[12px] leading-[18px] text-[#858b9c]">
            通过后将在阿里云该网关下为消费组追加自定义 API Key 凭证，并将消费者纳入选定的配额规则。分配的 API Key 与网关地址仅申请人本人可见。
          </p>

          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              variant="outline"
              disabled={approving}
              onClick={() => setApproveTarget(null)}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]"
            >
              取消
            </UIButton>
            <UIButton
              disabled={approving || !approveGroupId || !approveRuleId}
              onClick={() => void confirmApprove()}
              className="h-[32px] w-[96px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030] disabled:opacity-60"
            >
              {approving && <LoaderCircle className="size-[14px] animate-spin" />}
              确认通过
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>

      {/* Reject dialog */}
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
          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              variant="outline"
              disabled={rejecting}
              onClick={() => setRejectTarget(null)}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]"
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

      {/* Consumer group create dialog */}
      <Dialog open={cgOpen} onOpenChange={setCgOpen}>
        <DialogContent
          aria-describedby={undefined}
          className="flex w-[calc(100%-2rem)] flex-col gap-[14px] rounded-[14px] px-[20px] py-[16px] sm:max-w-[440px]"
        >
          <DialogTitle className="text-[15px] font-medium text-[#18181a]">新建消费组</DialogTitle>
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">名称</span>
            <Input
              value={cgName}
              onChange={(e) => setCgName(e.target.value)}
              disabled={cgSubmitting}
              placeholder="例如：研发一组"
              className="h-[34px] text-[12px]"
            />
          </label>
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">描述（可选）</span>
            <Input
              value={cgDesc}
              onChange={(e) => setCgDesc(e.target.value)}
              disabled={cgSubmitting}
              placeholder="例如：主力研发团队"
              className="h-[34px] text-[12px]"
            />
          </label>
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">网关</span>
            <Select value={cgGateway} onValueChange={setCgGateway}>
              <SelectTrigger className="h-[34px]">
                <SelectValue placeholder="选择网关" />
              </SelectTrigger>
              <SelectContent>
                {gateways.length === 0 ? (
                  <SelectItem value="__none__" disabled>
                    未配置网关（检查 ALIYUN_APIG_GATEWAYS）
                  </SelectItem>
                ) : (
                  gateways.map((g) => (
                    <SelectItem key={g.gateway_id} value={g.name}>
                      {g.name}
                    </SelectItem>
                  ))
                )}
              </SelectContent>
            </Select>
          </label>
          <p className="text-[12px] leading-[18px] text-[#858b9c]">
            将在选定网关下创建一个阿里云消费者。
          </p>
          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              variant="outline"
              disabled={cgSubmitting}
              onClick={() => setCgOpen(false)}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]"
            >
              取消
            </UIButton>
            <UIButton
              disabled={cgSubmitting}
              onClick={() => void submitConsumerGroup()}
              className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030] disabled:opacity-60"
            >
              {cgSubmitting && <LoaderCircle className="size-[14px] animate-spin" />}
              创建
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>

      {/* Quota rule create dialog */}
      <Dialog open={qrOpen} onOpenChange={setQrOpen}>
        <DialogContent
          aria-describedby={undefined}
          className="flex w-[calc(100%-2rem)] flex-col gap-[14px] rounded-[14px] px-[20px] py-[16px] sm:max-w-[480px]"
        >
          <DialogTitle className="text-[15px] font-medium text-[#18181a]">新建配额规则</DialogTitle>
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">规则名称</span>
            <Input
              value={qrName}
              onChange={(e) => setQrName(e.target.value)}
              disabled={qrSubmitting}
              placeholder="例如：研发组-月度配额"
              className="h-[34px] text-[12px]"
            />
          </label>
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">网关</span>
            <Select value={qrGateway} onValueChange={setQrGateway}>
              <SelectTrigger className="h-[34px]">
                <SelectValue placeholder="选择网关" />
              </SelectTrigger>
              <SelectContent>
                {gateways.length === 0 ? (
                  <SelectItem value="__none__" disabled>
                    未配置网关
                  </SelectItem>
                ) : (
                  gateways.map((g) => (
                    <SelectItem key={g.gateway_id} value={g.name}>
                      {g.name}
                    </SelectItem>
                  ))
                )}
              </SelectContent>
            </Select>
          </label>
          <div className="flex gap-[12px]">
            <label className="flex flex-1 flex-col gap-[6px]">
              <span className="text-[12px] font-medium text-[#464c5e]">维度</span>
              <Select value={qrDimension} onValueChange={(v) => setQrDimension(v as 'token' | 'credit')}>
                <SelectTrigger className="h-[34px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="token">Token</SelectItem>
                  <SelectItem value="credit">Credit</SelectItem>
                </SelectContent>
              </Select>
            </label>
            <label className="flex flex-1 flex-col gap-[6px]">
              <span className="text-[12px] font-medium text-[#464c5e]">周期</span>
              <Select value={qrPeriod} onValueChange={(v) => setQrPeriod(v as 'day' | 'week' | 'month')}>
                <SelectTrigger className="h-[34px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="day">自然日</SelectItem>
                  <SelectItem value="week">自然周</SelectItem>
                  <SelectItem value="month">自然月</SelectItem>
                </SelectContent>
              </Select>
            </label>
          </div>
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">配额上限</span>
            <Input
              type="number"
              min={1}
              value={qrLimit}
              onChange={(e) => setQrLimit(e.target.value)}
              disabled={qrSubmitting}
              placeholder="例如 60000"
              className="h-[34px] text-[12px]"
            />
          </label>
          <p className="text-[12px] leading-[18px] text-[#858b9c]">
            将在选定网关下创建一个阿里云 QuotaRule。审批时选择此规则即可将消费组纳入限流。
          </p>
          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              variant="outline"
              disabled={qrSubmitting}
              onClick={() => setQrOpen(false)}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]"
            >
              取消
            </UIButton>
            <UIButton
              disabled={qrSubmitting}
              onClick={() => void submitQuotaRule()}
              className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030] disabled:opacity-60"
            >
              {qrSubmitting && <LoaderCircle className="size-[14px] animate-spin" />}
              创建
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>

      {/* Quota rule edit dialog */}
      <Dialog open={Boolean(editRule)} onOpenChange={(open) => !open && setEditRule(null)}>
        <DialogContent
          aria-describedby={undefined}
          className="flex w-[calc(100%-2rem)] flex-col gap-[14px] rounded-[14px] px-[20px] py-[16px] sm:max-w-[440px]"
        >
          <DialogTitle className="text-[15px] font-medium text-[#18181a]">编辑配额规则</DialogTitle>
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">规则名称</span>
            <Input
              value={editRuleName}
              onChange={(e) => setEditRuleName(e.target.value)}
              disabled={editRuleSubmitting}
              className="h-[34px] text-[12px]"
            />
          </label>
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">配额上限</span>
            <Input
              type="number"
              min={1}
              value={editRuleLimit}
              onChange={(e) => setEditRuleLimit(e.target.value)}
              disabled={editRuleSubmitting}
              className="h-[34px] text-[12px]"
            />
          </label>
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">周期</span>
            <Select value={editRulePeriod} onValueChange={(v) => setEditRulePeriod(v as 'day' | 'week' | 'month')}>
              <SelectTrigger className="h-[34px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="day">自然日</SelectItem>
                <SelectItem value="week">自然周</SelectItem>
                <SelectItem value="month">自然月</SelectItem>
              </SelectContent>
            </Select>
          </label>
          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              variant="outline"
              disabled={editRuleSubmitting}
              onClick={() => setEditRule(null)}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]"
            >
              取消
            </UIButton>
            <UIButton
              disabled={editRuleSubmitting}
              onClick={() => void submitEditRule()}
              className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030] disabled:opacity-60"
            >
              {editRuleSubmitting && <LoaderCircle className="size-[14px] animate-spin" />}
              保存
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>

      {/* Consumer group quota change dialog */}
      <Dialog open={Boolean(cgQuotaTarget)} onOpenChange={(open) => !open && setCgQuotaTarget(null)}>
        <DialogContent
          aria-describedby={undefined}
          className="flex w-[calc(100%-2rem)] flex-col gap-[14px] rounded-[14px] px-[20px] py-[16px] sm:max-w-[440px]"
        >
          <DialogTitle className="text-[15px] font-medium text-[#18181a]">关联配额规则</DialogTitle>
          <p className="text-[12px] leading-[18px] text-[#757f9c]">
            消费组：{cgQuotaTarget?.name} · 网关 {cgQuotaTarget?.gateway_name || '-'}
          </p>
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">配额规则</span>
            <Select value={cgQuotaRuleId} onValueChange={setCgQuotaRuleId}>
              <SelectTrigger className="h-[34px]">
                <SelectValue placeholder="选择配额规则" />
              </SelectTrigger>
              <SelectContent>
                {(() => {
                  const compatible = rules.filter(
                    (r) => r.gateway_id === cgQuotaTarget?.gateway_id,
                  );
                  if (compatible.length === 0) {
                    return (
                      <SelectItem value="__none__" disabled>
                        无同网关配额规则
                      </SelectItem>
                    );
                  }
                  return compatible.map((r) => (
                    <SelectItem key={r.id} value={r.id}>
                      {r.name} ({r.quota_limit.toLocaleString('zh-CN')}/{PERIOD_LABEL[r.period_type] ?? r.period_type})
                    </SelectItem>
                  ));
                })()}
              </SelectContent>
            </Select>
          </label>
          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              variant="outline"
              disabled={cgQuotaSubmitting}
              onClick={() => setCgQuotaTarget(null)}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]"
            >
              取消
            </UIButton>
            <UIButton
              disabled={cgQuotaSubmitting || !cgQuotaRuleId}
              onClick={() => void submitCgQuotaChange()}
              className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030] disabled:opacity-60"
            >
              {cgQuotaSubmitting && <LoaderCircle className="size-[14px] animate-spin" />}
              确认
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>

      {/* Adjust quota dialog */}
      <Dialog open={Boolean(adjustTarget)} onOpenChange={(open) => !open && setAdjustTarget(null)}>
        <DialogContent
          aria-describedby={undefined}
          className="flex w-[calc(100%-2rem)] flex-col gap-[14px] rounded-[14px] px-[20px] py-[16px] sm:max-w-[440px]"
        >
          <DialogTitle className="text-[15px] font-medium text-[#18181a]">调整配额</DialogTitle>
          <p className="text-[12px] leading-[18px] text-[#757f9c]">
            用户：{adjustTarget?.username || '-'} · 当前用量 {adjustTarget?.used_amount.toLocaleString('zh-CN') ?? 0}
          </p>
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">新配额上限</span>
            <Input
              type="number"
              min={1}
              value={adjustLimit}
              onChange={(e) => setAdjustLimit(e.target.value)}
              disabled={adjustSubmitting}
              className="h-[34px] text-[12px]"
            />
          </label>
          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              variant="outline"
              disabled={adjustSubmitting}
              onClick={() => setAdjustTarget(null)}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]"
            >
              取消
            </UIButton>
            <UIButton
              disabled={adjustSubmitting || !adjustLimit}
              onClick={() => void submitAdjustQuota()}
              className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030] disabled:opacity-60"
            >
              {adjustSubmitting && <LoaderCircle className="size-[14px] animate-spin" />}
              保存
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
