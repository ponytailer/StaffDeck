import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  FileUp,
  ListChecks,
  LoaderCircle,
  Play,
  ReceiptText,
  RefreshCw,
  Wallet,
} from 'lucide-react';

import AppHeader from '@/components/AppHeader';
import { Checkbox, Input, Label, RadioGroup, RadioGroupItem, Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { API_BASE_URL, authHeader } from '@/api/client';
import type { EnterpriseAuthUser } from '@/auth';
import { cn } from '@/lib/utils';
import AgentAppBackButton from '../agentApps/AgentAppBackButton';

// 类型仅覆盖前端渲染需要的部分；OA 定位器等细节留在后端配置文件里
type ScenarioField = { key: string; label: string; mode: string };
type Scenario = {
  id: string;
  name: string;
  enabled: boolean;
  reason_prefill: string;
  fields: ScenarioField[];
};
type ExpenseConfig = { scenarios: Scenario[]; browser: { open: boolean; url: string | null; mode?: string | null } };

type RunStep = { field: string; status: 'filled' | 'todo' | 'skipped'; detail: string };
type RunResult = { status: 'need_login' | 'partial' | 'done' | 'failed'; message: string; steps: RunStep[]; elapsed_ms?: number };

/** 事由预填里的月份 token：当前月 -1，1 月回绕到 12 月。 */
function lastMonth(): number {
  const month = new Date().getMonth() + 1;
  return month === 1 ? 12 : month - 1;
}

/** 金额输入失焦时收敛为两位小数；非法输入原样保留交给执行前校验。 */
function normalizeAmount(raw: string): string {
  const value = Number(raw);
  if (!raw.trim() || Number.isNaN(value)) return raw;
  return value.toFixed(2);
}

/** 发票类型选项：value 是发给 OA 联想下拉的匹配文本，label 是界面展示文案。 */
const INVOICE_TYPES = [{ value: '普通发票', label: '普通发票 税率0%' }];

/** 公司名称选项：value 是发给 OA 联想下拉的匹配文本。 */
const COMPANY_NAMES = ['上海复星旅游管理有限公司'];

export default function ExpenseWorkflowPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
}) {
  const [config, setConfig] = useState<ExpenseConfig | null>(null);
  const [configError, setConfigError] = useState('');
  const [scenarioId, setScenarioId] = useState('');
  const [amount, setAmount] = useState('');
  const [reason, setReason] = useState('');
  const [reasonTouched, setReasonTouched] = useState(false);
  const [invoiceType, setInvoiceType] = useState(INVOICE_TYPES[0]?.value ?? '');
  const [companyName, setCompanyName] = useState(COMPANY_NAMES[0] ?? '');
  const [allETicket, setAllETicket] = useState(true);
  const [attachments, setAttachments] = useState<{ path: string; filename: string }[]>([]);
  const [autoCalcAmount, setAutoCalcAmount] = useState(true);
  const [calculating, setCalculating] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<RunResult | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const scenario = useMemo(() => config?.scenarios.find((s) => s.id === scenarioId), [config, scenarioId]);

  // 进页面拉一次场景配置
  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE_URL}/api/enterprise/expense-workflow/config`, { headers: authHeader() })
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res.status}`))))
      .then((data: ExpenseConfig) => {
        if (cancelled) return;
        setConfig(data);
        const first = data.scenarios[0];
        if (first) setScenarioId(first.id);
      })
      .catch((err) => {
        if (!cancelled) setConfigError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // 场景变化时渲染事由预填模板（用户手改过就不覆盖）
  useEffect(() => {
    if (reasonTouched || !scenario?.reason_prefill) return;
    setReason(scenario.reason_prefill.replace(/\{last_month\}/g, String(lastMonth())));
  }, [scenario, reasonTouched]);

  const uploadAttachment = useCallback(async (file: File): Promise<{ path: string; filename: string } | null> => {
    setUploading(true);
    try {
      const body = new FormData();
      body.append('file', file);
      const res = await fetch(`${API_BASE_URL}/api/enterprise/expense-workflow/attachments`, {
        method: 'POST',
        headers: authHeader(),
        body,
      });
      if (!res.ok) throw new Error(`附件登记失败（HTTP ${res.status}）`);
      const data = (await res.json()) as { path: string; filename: string };
      setAttachments((prev) => [...prev, { path: data.path, filename: data.filename }]);
      notify.success(`${data.filename} 已登记`);
      return { path: data.path, filename: data.filename };
    } catch (err) {
      notify.error(err instanceof Error ? err.message : '附件登记失败');
      return null;
    } finally {
      setUploading(false);
    }
  }, []);

  /** 勾选「自动计算」时：读已登记的电子发票 PDF 价税合计并求和，填充报销金额。 */
  const recalcFromInvoices = useCallback(
    async (paths: string[]) => {
      if (!paths.length) return;
      setCalculating(true);
      try {
        const res = await fetch(`${API_BASE_URL}/api/enterprise/expense-workflow/sum-amounts`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...authHeader() },
          body: JSON.stringify({ paths }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = (await res.json()) as {
          items: { filename: string; amount: string | null; error: string | null }[];
          total: string;
        };
        setAmount(data.total);
        const failed = data.items.filter((i) => i.error);
        if (failed.length) notify.warning(`有 ${failed.length} 张发票未能解析金额：${failed.map((f) => f.filename).join('、')}`);
        else notify.success(`已按发票合计填充金额：¥${data.total}`);
      } catch (err) {
        notify.error(err instanceof Error ? err.message : '发票金额计算失败');
      } finally {
        setCalculating(false);
      }
    },
    [],
  );

  const runTask = useCallback(async () => {
    if (!scenarioId) return;
    if (!amount.trim() || Number.isNaN(Number(amount))) {
      notify.error('请先填写报销金额');
      return;
    }
    if (!reason.trim()) {
      notify.error('请先填写报销事由');
      return;
    }
    if (allETicket && attachments.length === 0) {
      notify.error('全为电子票时需要先登记附件');
      return;
    }
    setRunning(true);
    setResult(null);
    try {
      const res = await fetch(`${API_BASE_URL}/api/enterprise/expense-workflow/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeader() },
        body: JSON.stringify({
          scenario_id: scenarioId,
          amount: normalizeAmount(amount),
          reason,
          company_name: companyName,
          invoice_type: invoiceType,
          all_e_ticket: allETicket,
          attachment_paths: allETicket ? attachments.map((a) => a.path) : [],
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      setResult(data as RunResult);
      if (data.status === 'done') notify.success('OA 表单已填写并保存');
      else if (data.status === 'need_login') notify.warning('OA 未登录，请在浏览器中登录后重试');
      else if (data.status === 'failed') notify.error(data.message || '执行失败');
      else notify.warning('流程执行完毕，部分字段待配置');
    } catch (err) {
      const message = err instanceof Error ? err.message : '执行失败';
      setResult({ status: 'failed', message, steps: [] });
      notify.error(message);
    } finally {
      setRunning(false);
    }
  }, [scenarioId, amount, reason, companyName, invoiceType, allETicket, attachments]);

  const statusTone: Record<RunResult['status'], string> = {
    need_login: 'border-[#f3d28b] bg-[#fff8e8] text-[#6f4500]',
    partial: 'border-[#f3d28b] bg-[#fff8e8] text-[#6f4500]',
    done: 'border-[#bfded2] bg-[#eef7f3] text-[#0f6b4f]',
    failed: 'border-[#f0b9b9] bg-[#fdf1f1] text-[#a4342c]',
  };
  const statusIcon: Record<RunResult['status'], React.ReactNode> = {
    need_login: <AlertTriangle className="mt-[1px] size-[14px] shrink-0" />,
    partial: <AlertTriangle className="mt-[1px] size-[14px] shrink-0" />,
    done: <CheckCircle2 className="mt-[1px] size-[14px] shrink-0" />,
    failed: <AlertTriangle className="mt-[1px] size-[14px] shrink-0" />,
  };

  return (
    <div className="box-border min-h-full px-[48px] pb-[43px] pt-[20px] max-[900px]:px-[16px]">
      <AppHeader
        className="mb-[16px]"
        onLogout={onLogout}
        userName={currentUser?.username}
        title="Agent 广场 · 报销流程助手"
      />

      {/* 与 PPT Studio / 决策助手统一的工作台外壳 */}
      <div className="flex h-[calc(100vh-190px)] min-h-[560px] flex-col gap-[14px] rounded-[20px] bg-white p-[16px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex flex-wrap items-center justify-end gap-[10px]">
          <AgentAppBackButton />
        </div>

        {configError && (
          <div className="flex shrink-0 items-start gap-[10px] rounded-[12px] border border-[#f0b9b9] bg-[#fdf1f1] px-[16px] py-[10px] text-[12px] leading-[18px] text-[#a4342c]">
            <AlertTriangle className="mt-[2px] size-[14px] shrink-0" />
            <span>场景配置加载失败：{configError}。请确认后端服务与 config/expense_workflow.json 正常。</span>
          </div>
        )}

        <div className="flex min-h-0 flex-1 flex-col gap-[14px] xl:flex-row">
          {/* 左栏：报销表单 */}
          <div className="flex min-h-0 flex-1 flex-col gap-[12px] xl:border-r-[0.5px] xl:border-[#eef0f5] xl:pr-[16px]">
            <div className="flex min-h-0 flex-1 flex-col gap-[16px] overflow-y-auto pr-[2px]">
              <div className="flex flex-col gap-[10px]">
                <div className="flex items-center gap-[6px] text-[#757f9c]">
                  <ReceiptText className="size-[14px]" />
                  <span className="text-[14px] font-normal leading-none text-[#464c5e]">报销信息</span>
                </div>

                <div className="flex flex-col gap-[6px]">
                  <Label className="text-[12.5px] text-[#5b6274]">报销场景</Label>
                  <Select
                    value={scenarioId}
                    onValueChange={(next) => {
                      setScenarioId(next);
                      setReasonTouched(false);
                    }}
                  >
                    <SelectTrigger className="h-[38px] w-full rounded-[10px] border-[#e3e7f1] text-[13px]">
                      <SelectValue placeholder={config ? '选择报销场景' : '加载中…'} />
                    </SelectTrigger>
                    <SelectContent>
                      {config?.scenarios.map((s) => (
                        <SelectItem key={s.id} value={s.id}>
                          {s.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div className="flex flex-col gap-[6px]">
                  <Label className="text-[12.5px] text-[#5b6274]">
                    报销事由
                  </Label>
                  <Input
                    value={reason}
                    onChange={(e) => {
                      setReason(e.target.value);
                      setReasonTouched(true);
                    }}
                    placeholder="如：7月通讯费报销"
                    className="h-[38px] rounded-[10px] border-[#e3e7f1] text-[13px]"
                  />
                </div>

                <div className="flex flex-col gap-[6px]">
                  <Label className="text-[12.5px] text-[#5b6274]">公司名称</Label>
                  <Select value={companyName} onValueChange={setCompanyName}>
                    <SelectTrigger className="h-[38px] w-full rounded-[10px] border-[#e3e7f1] text-[13px]">
                      <SelectValue placeholder="选择公司名称" />
                    </SelectTrigger>
                    <SelectContent>
                      {COMPANY_NAMES.map((name) => (
                        <SelectItem key={name} value={name}>
                          {name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div className="flex flex-col gap-[6px]">
                  <Label className="text-[12.5px] text-[#5b6274]">发票类型</Label>
                  <Select value={invoiceType} onValueChange={setInvoiceType}>
                    <SelectTrigger className="h-[38px] w-full rounded-[10px] border-[#e3e7f1] text-[13px]">
                      <SelectValue placeholder="选择发票类型" />
                    </SelectTrigger>
                    <SelectContent>
                      {INVOICE_TYPES.map((t) => (
                        <SelectItem key={t.value} value={t.value}>
                          {t.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div className="flex flex-col gap-[6px]">
                  <Label className="text-[12.5px] text-[#5b6274]">是否全为电子票</Label>
                  <RadioGroup
                    value={allETicket ? 'yes' : 'no'}
                    onValueChange={(next) => setAllETicket(next === 'yes')}
                    className="flex items-center gap-[18px]"
                  >
                    <label className="flex cursor-pointer items-center gap-[7px] text-[13px] text-[#464c5e]">
                      <RadioGroupItem value="yes" />
                      是
                    </label>
                    <label className="flex cursor-pointer items-center gap-[7px] text-[13px] text-[#464c5e]">
                      <RadioGroupItem value="no" />
                      否
                    </label>
                  </RadioGroup>
                </div>

                {allETicket && (
                  <div className="flex flex-col gap-[6px] rounded-[12px] border border-dashed border-[#dfe3ec] bg-[#fafbfd] p-[12px]">
                    <div className="flex items-center gap-[6px] text-[#757f9c]">
                      <FileUp className="size-[13px]" />
                      <span className="text-[12.5px] text-[#464c5e]">电子票附件（仅支持 PDF · 可多选 · 任务执行时自动挂到 OA 相关票据区）</span>
                    </div>
                    <input
                      ref={fileInputRef}
                      type="file"
                      accept=".pdf,application/pdf"
                      multiple
                      className="hidden"
                      onChange={async (e) => {
                        const files = Array.from(e.target.files ?? []);
                        e.target.value = '';
                        if (!files.length) return;
                        const invalid = files.filter((f) => !f.name.toLowerCase().endsWith('.pdf') && f.type !== 'application/pdf');
                        if (invalid.length) {
                          notify.error(`以下文件不是 PDF，已跳过：${invalid.map((f) => f.name).join('、')}`);
                        }
                        const registered: { path: string }[] = [];
                        for (const file of files.filter((f) => !invalid.includes(f))) {
                          const r = await uploadAttachment(file);
                          if (r) registered.push(r);
                        }
                        if (autoCalcAmount && allETicket && registered.length) {
                          void recalcFromInvoices([...attachments.map((a) => a.path), ...registered.map((r) => r.path)]);
                        }
                      }}
                    />
                    <div className="flex items-center gap-[10px]">
                      <UIButton
                        type="button"
                        variant="outline"
                        disabled={uploading}
                        onClick={() => fileInputRef.current?.click()}
                        className="h-[32px] rounded-[9px] border-[#e3e7f1] px-[12px] text-[12.5px] text-[#464c5e]"
                      >
                        {uploading ? <LoaderCircle className="size-[13px] animate-spin" /> : <FileUp className="size-[13px]" />}
                        选择附件
                      </UIButton>
                      {attachments.length > 0 && (
                        <button
                          type="button"
                          onClick={() => setAttachments([])}
                          className="text-[12px] text-[#a3aaba] hover:text-[#a4342c]"
                        >
                          清空
                        </button>
                      )}
                    </div>
                    {attachments.length > 0 && (
                      <div className="flex flex-col gap-[4px]">
                        {attachments.map((a, index) => (
                          <div key={`${a.path}-${index}`} className="flex min-w-0 items-center gap-[6px] text-[12px] text-[#5b6274]">
                            <span className="shrink-0 text-[#1a7f4b]">✓</span>
                            <span className="truncate" title={a.path}>
                              {a.filename}
                            </span>
                            <span className="shrink-0 text-[10.5px] text-[#a3aaba]">已登记</span>
                          </div>
                        ))}
                      </div>
                    )}
                    <label className="flex cursor-pointer items-center gap-[7px] text-[12.5px] text-[#464c5e]">
                      <Checkbox
                        checked={autoCalcAmount}
                        disabled={calculating}
                        onCheckedChange={(v) => {
                          const next = v === true;
                          setAutoCalcAmount(next);
                          if (next && allETicket && attachments.length) {
                            void recalcFromInvoices(attachments.map((a) => a.path));
                          }
                        }}
                      />
                      自动计算发票总金额（读取 PDF 价税合计并求和）
                      {calculating && <LoaderCircle className="size-[12px] animate-spin text-[#757f9c]" />}
                    </label>
                  </div>
                )}

                <div className="flex flex-col gap-[6px]">
                  <Label className="text-[12.5px] text-[#5b6274]">报销金额</Label>
                  <div className="relative">
                    <span className="pointer-events-none absolute left-[12px] top-1/2 -translate-y-1/2 text-[13px] text-[#757f9c]">¥</span>
                    <Input
                      inputMode="decimal"
                      value={amount}
                      onChange={(e) => setAmount(e.target.value)}
                      onBlur={() => setAmount((v) => normalizeAmount(v))}
                      placeholder="0.00"
                      className="h-[38px] rounded-[10px] border-[#e3e7f1] pl-[26px] text-[13px]"
                    />
                  </div>
                  {autoCalcAmount && allETicket && attachments.length > 0 && (
                    <p className="text-[11px] leading-[16px] text-[#757f9c]">由电子发票自动计算填充，可手动修改。</p>
                  )}
                </div>
              </div>
            </div>

            {/* 栏底操作条：执行任务（need_login 时它就是「重试」） */}
            <div className="flex shrink-0 items-center justify-end gap-[10px] border-t pt-[12px]">
              <UIButton
                type="button"
                onClick={() => void runTask()}
                disabled={running || !config || !scenarioId}
                className="h-[38px] rounded-[10px] bg-[#18181a] px-[20px] text-[13px] text-white hover:bg-[#303030] disabled:opacity-50"
              >
                {running ? <LoaderCircle className="size-[14px] animate-spin" /> : allETicket && result?.status === 'need_login' ? <RefreshCw className="size-[14px]" /> : <Play className="size-[14px]" />}
                {running ? '执行中…' : result?.status === 'need_login' ? '登录后重试' : '执行任务'}
              </UIButton>
            </div>
          </div>

          {/* 右栏：执行状态与步骤日志 */}
          <div className="flex min-h-0 w-full flex-col gap-[12px] xl:w-[380px] xl:shrink-0">
            <div className="flex items-center gap-[6px] text-[#757f9c]">
              <ListChecks className="size-[14px]" />
              <span className="text-[14px] font-normal leading-none text-[#464c5e]">执行状态</span>
              {result?.elapsed_ms != null && (
                <span className="text-[11.5px] text-[#a3aaba]">{(result.elapsed_ms / 1000).toFixed(1)}s</span>
              )}
              {config?.browser.mode && (
                <span
                  className="ml-auto rounded-full px-[8px] py-[2px] text-[10.5px] font-medium"
                  title={
                    config.browser.mode === 'cdp'
                      ? '已通过 CDP 连接当前浏览器，复用其登录态与标签页'
                      : '使用独立浏览器窗口（cookie 持久保存在 .oa-browser-profile）'
                  }
                >
                  {config.browser.mode === 'cdp' ? '当前浏览器' : '独立窗口'}
                </span>
              )}
            </div>

            {!result && (
              <div className="flex flex-1 items-center justify-center rounded-[12px] border border-dashed border-[#e3e7f1] text-[12.5px] text-[#a3aaba]">
                填好左侧表单后点击「执行任务」
              </div>
            )}

            {result && (
              <div className="flex min-h-0 flex-1 flex-col gap-[10px] overflow-y-auto">
                <div className={cn('flex items-start gap-[8px] rounded-[12px] border px-[14px] py-[10px] text-[12.5px] leading-[18px]', statusTone[result.status])}>
                  {statusIcon[result.status]}
                  <div className="min-w-0">
                    <div>{result.message}</div>
                    {result.status === 'need_login' && (
                      <div className="mt-[4px] text-[11.5px] opacity-80">
                        已打开的浏览器窗口不要关闭；登录完成后回到这里点「登录后重试」，表单内容会原样带上。
                      </div>
                    )}
                  </div>
                </div>

                {result.steps.length > 0 && (
                  <div className="rounded-[12px] border border-[#eef0f5]">
                    {result.steps.map((step, index) => (
                      <div
                        key={`${step.field}-${index}`}
                        className={cn(
                          'flex items-center justify-between gap-[10px] px-[12px] py-[8px] text-[12.5px]',
                          index > 0 && 'border-t border-[#f2f4f8]',
                        )}
                      >
                        <span className="shrink-0 text-[#464c5e]">{step.field}</span>
                        <span
                          className={cn(
                            'truncate text-right text-[12px]',
                            step.status === 'filled' && 'text-[#1a7f4b]',
                            step.status === 'todo' && 'text-[#8a4b00]',
                            step.status === 'skipped' && 'text-[#a3aaba]',
                          )}
                          title={step.detail}
                        >
                          {step.status === 'filled' ? '✓ ' : step.status === 'todo' ? '待配置 · ' : '跳过 · '}
                          {step.detail}
                        </span>
                      </div>
                    ))}
                  </div>
                )}

                {result.status === 'partial' && (
                  <div className="rounded-[12px] border border-[#eef0f5] bg-[#fafbfd] px-[12px] py-[10px] text-[11.5px] leading-[17px] text-[#757f9c]">
                    「待配置」表示 OA 页面上对应字段的定位器还没写进配置文件
                    （backend/config/expense_workflow.json）。点左侧「检查 OA 页面」拿到建议定位器，
                    点击条目即可复制，补进配置后重新执行即可完整填充。
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
