import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { api, TENANT_ID } from '@/api/client';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { Paginator } from '@/components/Paginator';
import { StatCard } from '@/components/StatCard';
import { notify } from '@/components/ui/app-toast';
import { Button as UIButton } from '@/components/ui/button';
import { UnderlineTabs } from '@/components/ui/underline-tabs';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui';
import { useClientPagination } from '@/hooks/useClientPagination';
import { cn } from '@/lib/utils';

import type { ScheduledTaskRead } from '@/types';

import IconAlarm from '../../assets/icons/profile-alarm.svg?react';
import IconAlignJustify from '../../assets/icons/align-justify.svg?react';
import IconListBulleted from '../../assets/icons/list-bulleted.svg?react';
import IconMore from '../../assets/icons/more.svg?react';
import IconPause from '../../assets/icons/pause.svg?react';
import IconPlay from '../../assets/icons/play.svg?react';
import IconRefresh from '../../assets/icons/refresh.svg?react';
import IconSearch from '../../assets/icons/search.svg?react';
import IconTrash from '../../assets/icons/trash.svg?react';
import { StatusBadge, TaskRunResultBadge, TaskStatusBadge } from '../scheduled-tasks/StatusBadge';
import {
  RUN_FILTER_TABS,
  TASK_PAGE_SIZE,
  formatSchedule,
  formatTime,
  type RunListFilter,
} from '../scheduled-tasks/shared';

// ---------------------------------------------------------------------------
// Types（后端 /api/enterprise/scheduled-task-monitor）
// ---------------------------------------------------------------------------

type MonitorWorker = {
  name: string;
  hostname: string;
  pid: number | null;
  queues: string[];
  state: string;
  last_heartbeat: string | null;
  current_job: { id: string; task_id: string | null; started_at: string | null } | null;
};

type MonitorRuntime = {
  backend: string;
  enabled: boolean;
  queue_name: string;
  redis_connected: boolean;
  counts: Record<string, number>;
  workers: MonitorWorker[];
  error: string | null;
};

type MonitorOverview = {
  generated_at: string;
  tasks: { total: number; active: number; paused: number; completed: number; archived: number };
  runs: {
    total: number;
    pending: number;
    running: number;
    queued: number;
    succeeded: number;
    failed: number;
    skipped: number;
    needs_input: number;
    last_24h_total: number;
    last_24h_failed: number;
  };
  runtime: MonitorRuntime;
};

type MonitorTaskItem = {
  id: string;
  title: string;
  prompt: string;
  status: string;
  schedule_type: string;
  schedule: Record<string, unknown>;
  timezone: string;
  agent_id: string;
  agent_name: string | null;
  created_by_user_id: string;
  created_by_name: string | null;
  next_run_at: string | null;
  last_run_at: string | null;
  last_status: string | null;
  run_count: number;
  failure_count: number;
  in_flight: number;
  concurrency_policy: string;
  misfire_policy: string;
  updated_at: string | null;
};

type MonitorRunItem = {
  id: string;
  scheduled_task_id: string;
  task_title: string | null;
  task_status: string | null;
  agent_id: string;
  agent_name: string | null;
  user_id: string;
  user_name: string | null;
  session_id: string | null;
  scheduled_for: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  result_summary: string | null;
  error: string | null;
  created_at: string;
};

// ---------------------------------------------------------------------------
// 常量
// ---------------------------------------------------------------------------

type TaskFilter = 'all' | 'active' | 'paused' | 'completed' | 'archived';

const TASK_FILTER_TABS: { value: TaskFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'active', label: '启用中' },
  { value: 'paused', label: '已暂停' },
  { value: 'completed', label: '已完成' },
  { value: 'archived', label: '已删除' },
];

const POLL_INTERVAL_MS = 15_000;

const MENU_ITEM_CLASS =
  'w-[110px] cursor-pointer gap-[4px] rounded-[10px] px-[12px] py-[6px] text-[12px] text-[#858b9c] focus:text-[#18181a] [&_svg]:size-[14px]';
const MENU_ITEM_DANGER_CLASS =
  'w-[110px] cursor-pointer gap-[4px] rounded-[10px] px-[12px] py-[6px] text-[12px] text-[#d20b0b] focus:bg-[#fce7e7] focus:text-[#d20b0b] focus:[&_svg]:text-[#d20b0b]! [&_svg]:size-[14px]';
const PANEL_CLASS = 'rounded-[14px] border border-[#f2f3f7] bg-white p-[16px]';
const SECTION_HEAD_CLASS = 'mb-[16px] flex flex-wrap items-center gap-[6px] px-[12px] text-[#757f9c]';
const MOBILE_META_CLASS =
  'mt-[12px] grid grid-cols-2 gap-[8px] max-[520px]:grid-cols-1 [&>span]:min-w-0 [&>span]:rounded-[10px] [&>span]:border [&>span]:border-[#eef0f4] [&>span]:bg-[#fafbfc] [&>span]:px-[10px] [&>span]:py-[9px] [&>span]:text-[12px] [&>span]:leading-[1.45] [&>span]:text-[#18181a] [&>span]:[overflow-wrap:anywhere] [&_b]:mb-[3px] [&_b]:block [&_b]:text-[11px] [&_b]:font-semibold [&_b]:text-[#858b9c]';

/**
 * 「超级管理员 → 任务监控」子页面。
 *
 * 看两件事：① 业务侧——租户下全部定时任务与执行记录，可直接干预；
 * ② 运行时侧——rq worker / 队列 / Redis 健康度。数据来自
 * `/api/enterprise/scheduled-task-monitor`，写操作复用既有 scheduled-tasks 端点。
 */
export default function ScheduledTaskMonitorTab() {
  const [overview, setOverview] = useState<MonitorOverview | null>(null);
  const [tasks, setTasks] = useState<MonitorTaskItem[]>([]);
  const [runs, setRuns] = useState<MonitorRunItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [taskFilter, setTaskFilter] = useState<TaskFilter>('all');
  const [runFilter, setRunFilter] = useState<RunListFilter>('all');
  const [query, setQuery] = useState('');
  const [selectedTaskId, setSelectedTaskId] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<MonitorTaskItem | null>(null);
  const [deleting, setDeleting] = useState(false);
  const busyRef = useRef(false);

  const load = useCallback(async (mode: 'initial' | 'manual' | 'poll' = 'manual') => {
    // 轮询撞上手动刷新/初始化时直接跳过，避免并发重复请求
    if (mode === 'poll' && busyRef.current) return;
    busyRef.current = true;
    if (mode === 'initial') setLoading(true);
    if (mode === 'manual') setRefreshing(true);
    try {
      const [nextOverview, nextTasks, nextRuns] = await Promise.all([
        api.get<MonitorOverview>(
          `/api/enterprise/scheduled-task-monitor/overview?tenant_id=${TENANT_ID}`,
        ),
        api.get<MonitorTaskItem[]>(
          `/api/enterprise/scheduled-task-monitor/tasks?tenant_id=${TENANT_ID}&limit=500`,
        ),
        api.get<MonitorRunItem[]>(
          `/api/enterprise/scheduled-task-monitor/runs?tenant_id=${TENANT_ID}&status=all&limit=500`,
        ),
      ]);
      setOverview(nextOverview);
      setTasks(nextTasks);
      setRuns(nextRuns);
    } catch (error) {
      if (mode !== 'poll') {
        notify.error(error instanceof Error ? error.message : '加载任务监控数据失败');
      }
    } finally {
      busyRef.current = false;
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load('initial');
  }, [load]);

  useEffect(() => {
    if (!autoRefresh) return;
    const timer = window.setInterval(() => void load('poll'), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [autoRefresh, load]);

  // ---- 操作 ---------------------------------------------------------------

  async function runNow(taskId: string, title?: string) {
    try {
      await api.post(`/api/enterprise/scheduled-tasks/${taskId}/run-now?tenant_id=${TENANT_ID}`);
      notify.success(`已触发「${title || taskId}」立即执行，稍后可在执行记录中查看`);
      await load('manual');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '立即执行失败');
    }
  }

  async function toggleStatus(task: MonitorTaskItem) {
    const next = task.status === 'active' ? 'paused' : 'active';
    try {
      await api.put(`/api/enterprise/scheduled-tasks/${task.id}`, {
        tenant_id: TENANT_ID,
        status: next,
      });
      notify.success(next === 'active' ? '已启用' : '已暂停');
      await load('manual');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新状态失败');
    }
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await api.delete(`/api/enterprise/scheduled-tasks/${deleteTarget.id}?tenant_id=${TENANT_ID}`);
      notify.success('已删除');
      if (selectedTaskId === deleteTarget.id) setSelectedTaskId('');
      setDeleteTarget(null);
      await load('manual');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除失败');
    } finally {
      setDeleting(false);
    }
  }

  function openTaskRuns(taskId: string) {
    setSelectedTaskId(taskId);
    setRunFilter('all');
  }

  function openChatSession(sessionId?: string | null) {
    if (!sessionId) return;
    window.open(`/workspace/chat/${sessionId}`, '_blank', 'noopener,noreferrer');
  }

  // ---- 派生数据 -----------------------------------------------------------

  const visibleTasks = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return tasks.filter((row) => {
      if (taskFilter !== 'all' && row.status !== taskFilter) return false;
      if (!keyword) return true;
      return (
        row.title.toLowerCase().includes(keyword)
        || row.prompt.toLowerCase().includes(keyword)
        || (row.agent_name || '').toLowerCase().includes(keyword)
      );
    });
  }, [tasks, taskFilter, query]);

  const filteredRuns = useMemo(() => {
    const scoped = selectedTaskId
      ? runs.filter((row) => row.scheduled_task_id === selectedTaskId)
      : runs;
    if (runFilter === 'pending') {
      return scoped.filter((row) => ['queued', 'running', 'retrying', 'needs_input'].includes(row.status));
    }
    if (runFilter === 'failed') {
      return scoped.filter((row) => ['failed', 'skipped'].includes(row.status));
    }
    if (runFilter === 'completed') return scoped.filter((row) => row.status === 'succeeded');
    return scoped;
  }, [runs, runFilter, selectedTaskId]);

  const taskPagination = useClientPagination(visibleTasks, TASK_PAGE_SIZE, `${taskFilter}|${query}`);
  const runPagination = useClientPagination(filteredRuns, TASK_PAGE_SIZE, `${runFilter}|${selectedTaskId}`);

  const selectedTask = tasks.find((row) => row.id === selectedTaskId) || null;
  const runtime = overview?.runtime;

  // ---- 渲染 ---------------------------------------------------------------

  const renderTaskActions = (row: MonitorTaskItem) => {
    const isArchived = row.status === 'archived';
    const isCompleted = row.status === 'completed';
    return (
      <DropdownMenu>
        <DropdownMenuTrigger
          aria-label="操作"
          className="grid size-7 place-items-center rounded-[8px] text-[#1a71ff] transition-colors outline-none hover:bg-black/5 hover:text-[#4a8dff] focus-visible:bg-black/5"
        >
          <IconMore className="size-3.5" />
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="end"
          className="flex w-auto min-w-0 flex-col gap-[4px] rounded-[14px] border-0 bg-white p-[4px] shadow-[0px_0px_8px_rgba(0,0,0,0.1)] ring-0 [--accent:#F6F6F6] [--accent-foreground:#18181A]"
        >
          <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => openTaskRuns(row.id)}>
            <IconListBulleted />
            查看记录
          </DropdownMenuItem>
          {!isArchived && (
            <>
              <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void runNow(row.id, row.title)}>
                <IconPlay />
                立即执行
              </DropdownMenuItem>
              {!isCompleted && (
                <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void toggleStatus(row)}>
                  {row.status === 'active' ? <IconPause /> : <IconPlay />}
                  {row.status === 'active' ? '暂停' : '启用'}
                </DropdownMenuItem>
              )}
              <DropdownMenuSeparator className="my-[2px] bg-[#eef0f4]" />
              <DropdownMenuItem
                variant="destructive"
                className={MENU_ITEM_DANGER_CLASS}
                onSelect={() => setDeleteTarget(row)}
              >
                <IconTrash />
                删除
              </DropdownMenuItem>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    );
  };

  const taskColumns: DataTableColumn<MonitorTaskItem>[] = [
    {
      key: 'title',
      title: '定时任务',
      className: 'whitespace-normal',
      render: (row) => (
        <div className="flex min-w-0 flex-col gap-[4px]">
          <span className="flex min-w-0 items-center gap-[6px]">
            <span className="truncate font-medium leading-[18px] text-[#18181a]">{row.title}</span>
            {row.in_flight > 0 && <StatusBadge tone="blue">执行中</StatusBadge>}
          </span>
          <span className="truncate">{row.prompt}</span>
        </div>
      ),
    },
    {
      key: 'owner',
      title: '所属员工 / 创建人',
      width: 180,
      className: 'whitespace-normal',
      render: (row) => (
        <div className="flex min-w-0 flex-col gap-[3px]">
          <span className="truncate text-[#18181a]">{row.agent_name || row.agent_id}</span>
          <span className="truncate">{row.created_by_name || row.created_by_user_id}</span>
        </div>
      ),
    },
    {
      key: 'schedule',
      title: '计划',
      width: 190,
      className: 'whitespace-normal [overflow-wrap:anywhere]',
      render: (row) => formatSchedule(row as unknown as ScheduledTaskRead),
    },
    { key: 'status', title: '状态', width: 92, render: (row) => <TaskStatusBadge status={row.status} /> },
    { key: 'next', title: '下次执行', width: 160, render: (row) => formatTime(row.next_run_at || undefined) },
    {
      key: 'lastResult',
      title: '最近结果',
      width: 110,
      render: (row) => (row.last_status ? <TaskRunResultBadge status={row.last_status} /> : <span>暂无</span>),
    },
    {
      key: 'failure',
      title: '失败次数',
      width: 92,
      render: (row) =>
        row.failure_count > 0 ? (
          <span className="font-medium text-[#d20b0b]">{row.failure_count}</span>
        ) : (
          <span>0</span>
        ),
    },
    { key: 'actions', title: '操作', width: 80, render: renderTaskActions },
  ];

  const runColumns: DataTableColumn<MonitorRunItem>[] = [
    {
      key: 'task',
      title: '定时任务',
      width: 200,
      className: 'whitespace-normal',
      render: (row) => (
        <div className="flex min-w-0 flex-col gap-[3px]">
          <span className="truncate text-[#18181a]">{row.task_title || row.scheduled_task_id}</span>
          <span className="truncate">{row.agent_name || row.agent_id}</span>
        </div>
      ),
    },
    { key: 'status', title: '状态', width: 100, render: (row) => <TaskRunResultBadge status={row.status} /> },
    { key: 'trigger', title: '触发人', width: 100, render: (row) => row.user_name || row.user_id },
    { key: 'scheduled', title: '计划时间', width: 160, render: (row) => formatTime(row.scheduled_for) },
    {
      key: 'duration',
      title: '耗时',
      width: 90,
      render: (row) => (row.duration_ms != null ? formatDuration(row.duration_ms) : '—'),
    },
    {
      key: 'result',
      title: '结果',
      className: 'whitespace-normal',
      render: (row) => <span className="wrap-break-word">{row.error || row.result_summary || '暂无'}</span>,
    },
    {
      key: 'actions',
      title: '操作',
      width: 140,
      render: (row) => (
        <div className="flex items-center gap-[10px]">
          <UIButton
            variant="link"
            disabled={!row.session_id}
            onClick={() => openChatSession(row.session_id)}
            className="h-auto p-0 text-[12px] font-normal text-[#1a71ff] hover:text-[#4a8dff] hover:no-underline disabled:text-[#c0c6d4]"
          >
            查看会话
          </UIButton>
          <UIButton
            variant="link"
            onClick={() => void runNow(row.scheduled_task_id, row.task_title || undefined)}
            className="h-auto p-0 text-[12px] font-normal text-[#1a71ff] hover:text-[#4a8dff] hover:no-underline"
          >
            重新执行
          </UIButton>
        </div>
      ),
    },
  ];

  const renderTaskMobileCard = (row: MonitorTaskItem) => (
    <article key={row.id} className="rounded-[14px] border border-[#eef0f4] bg-white p-[14px]">
      <div className="flex items-start justify-between gap-[10px]">
        <strong className="min-w-0 text-[14px] font-semibold text-[#18181a]">{row.title}</strong>
        <TaskStatusBadge status={row.status} />
      </div>
      <p className="mt-[8px] line-clamp-2 text-[12px] leading-[1.55] text-[#858b9c]">{row.prompt}</p>
      <div className={MOBILE_META_CLASS}>
        <span>
          <b>所属员工</b>
          {row.agent_name || row.agent_id}
        </span>
        <span>
          <b>下次执行</b>
          {formatTime(row.next_run_at || undefined)}
        </span>
        <span>
          <b>已执行</b>
          {row.run_count} 次
        </span>
        <span>
          <b>失败</b>
          {row.failure_count} 次
        </span>
      </div>
      <div className="mt-[12px] flex justify-end">{renderTaskActions(row)}</div>
    </article>
  );

  const renderRunMobileCard = (row: MonitorRunItem) => (
    <article key={row.id} className="rounded-[14px] border border-[#eef0f4] bg-white p-[14px]">
      <div className="flex items-start justify-between gap-[10px]">
        <strong className="min-w-0 text-[14px] font-semibold text-[#18181a]">
          {row.task_title || row.scheduled_task_id}
        </strong>
        <TaskRunResultBadge status={row.status} />
      </div>
      <div className={MOBILE_META_CLASS}>
        <span>
          <b>计划时间</b>
          {formatTime(row.scheduled_for)}
        </span>
        <span>
          <b>耗时</b>
          {row.duration_ms != null ? formatDuration(row.duration_ms) : '—'}
        </span>
      </div>
      <p className="mt-[8px] line-clamp-2 text-[12px] leading-[1.55] text-[#858b9c]">
        {row.error || row.result_summary || '暂无结果'}
      </p>
      <div className="mt-[12px] flex justify-end">
        <UIButton
          variant="link"
          disabled={!row.session_id}
          onClick={() => openChatSession(row.session_id)}
          className="h-auto p-0 text-[12px] font-normal text-[#1a71ff] hover:text-[#4a8dff] hover:no-underline disabled:text-[#c0c6d4]"
        >
          查看会话
        </UIButton>
      </div>
    </article>
  );

  return (
    <>
      {/* 概览统计：6 张卡在宽屏铺满一行（xl），窄屏按 3 列 / 2 列降级，避免最后一张卡被挤成单独一行 */}
      <div
        className="mt-[16px] grid grid-cols-2 gap-[20px] sm:grid-cols-3 xl:grid-cols-6"
        aria-label="任务监控统计"
      >
        <StatCard label="定时任务" value={overview?.tasks.total ?? '—'} />
        <StatCard label="启用中" value={overview?.tasks.active ?? '—'} tone="green" />
        <StatCard label="已暂停" value={overview?.tasks.paused ?? '—'} />
        <StatCard label="执行中" value={overview?.runs.running ?? '—'} />
        <StatCard
          label="待处理"
          value={overview?.runs.pending ?? '—'}
          valueClassName={(overview?.runs.pending ?? 0) > 0 ? 'text-[#ff7f00]' : undefined}
        />
        <StatCard
          label="24h 失败"
          value={overview?.runs.last_24h_failed ?? '—'}
          tone={(overview?.runs.last_24h_failed ?? 0) > 0 ? 'red' : 'default'}
        />
      </div>

      {/* 调度运行时 */}
      <section aria-label="调度运行时" className="mt-[24px]">
        <div className={SECTION_HEAD_CLASS}>
          <IconRefresh className={cn('size-[14px] shrink-0', refreshing && 'animate-spin')} />
          <span className="text-[14px] font-normal leading-none">调度运行时</span>
          <span className="ml-auto flex items-center gap-[10px] text-[11px]">
            {overview?.generated_at && <span>更新于 {formatTime(overview.generated_at)}</span>}
            <button
              type="button"
              onClick={() => setAutoRefresh((prev) => !prev)}
              className={cn(
                'rounded-full px-[10px] py-[3px] transition-colors',
                autoRefresh ? 'bg-[#e8f0ff] text-[#1a71ff]' : 'bg-[#f2f3f7] text-[#858b9c]',
              )}
            >
              自动刷新 {autoRefresh ? '开' : '关'}
            </button>
            <button
              type="button"
              onClick={() => void load('manual')}
              className="rounded-full bg-[#f2f3f7] px-[10px] py-[3px] text-[#464c5e] transition-colors hover:bg-[#e8eaef]"
            >
              立即刷新
            </button>
          </span>
        </div>

        <div className={cn(PANEL_CLASS, 'flex flex-col gap-[16px]')}>
          <div className="flex flex-wrap items-center gap-[10px]">
            <StatusBadge tone={runtime?.enabled ? 'blue' : 'gray'}>
              {runtime?.enabled
                ? `rq 调度 · ${runtime.queue_name}`
                : `进程内轮询（${runtime?.backend ?? 'poll'}）`}
            </StatusBadge>
            <StatusBadge tone={runtime?.redis_connected ? 'green' : 'red'}>
              {runtime?.redis_connected ? 'Redis 已连接' : 'Redis 未连接'}
            </StatusBadge>
            {runtime?.error && <span className="text-[12px] text-[#d20b0b]">{runtime.error}</span>}
          </div>

          <div className="grid grid-cols-2 gap-[10px] sm:grid-cols-3 lg:grid-cols-5">
            <RuntimeTile label="待触发" value={runtime?.counts.scheduled ?? 0} hint="已注册的下次触发" />
            <RuntimeTile label="排队中" value={runtime?.counts.queued ?? 0} hint="等待 worker 领取" />
            <RuntimeTile label="执行中" value={runtime?.counts.started ?? 0} hint="worker 正在处理" />
            <RuntimeTile label="已完成" value={runtime?.counts.finished ?? 0} hint="rq 保留的成功记录" />
            <RuntimeTile
              label="失败 job"
              value={runtime?.counts.failed ?? 0}
              hint="需人工排查"
              danger={(runtime?.counts.failed ?? 0) > 0}
            />
          </div>

          <div>
            <p className="mb-[8px] text-[12px] font-medium text-[#464c5e]">
              {`Worker（${runtime?.workers.length ?? 0} 个在线）`}
            </p>
            {runtime?.workers.length ? (
              <div className="flex flex-col gap-[8px]">
                {runtime.workers.map((worker) => (
                  <div
                    key={worker.name || `${worker.hostname}-${worker.pid}`}
                    className="flex flex-wrap items-center gap-[10px] rounded-[10px] bg-[#fafbfc] px-[12px] py-[9px] text-[12px] text-[#464c5e]"
                  >
                    <StatusBadge tone={worker.state === 'busy' ? 'blue' : 'green'}>
                      {worker.state === 'busy' ? '执行中' : '空闲'}
                    </StatusBadge>
                    <span className="font-medium text-[#18181a]">{worker.name || worker.hostname}</span>
                    <span>pid {worker.pid ?? '—'}</span>
                    <span>队列 {worker.queues.join('、') || '—'}</span>
                    <span className="ml-auto">
                      心跳 {worker.last_heartbeat ? formatTime(worker.last_heartbeat) : '—'}
                    </span>
                    {worker.current_job && (
                      <span className="w-full truncate text-[#1a71ff]">
                        正在执行：{worker.current_job.task_id || worker.current_job.id}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <div className="rounded-[10px] border border-[#f3d28b] bg-[#fff8e8] px-[12px] py-[10px] text-[12px] leading-[18px] text-[#6f4500]">
                {runtime?.enabled
                  ? '暂无在线 worker —— 定时触发不会被消费。请启动独立进程：uv run rq-worker'
                  : '当前使用进程内轮询（SCHEDULER_BACKEND=poll），无需 rq worker。'}
              </div>
            )}
          </div>
        </div>
      </section>

      {/* 定时任务 */}
      <section aria-label="定时任务" className="mt-[24px]">
        <div className={SECTION_HEAD_CLASS}>
          <IconAlarm className="size-[14px] shrink-0" />
          <span className="text-[14px] font-normal leading-none">定时任务</span>
          <label className="ml-auto flex h-[30px] w-[220px] items-center gap-[6px] rounded-[10px] border border-[#e3e7f1] bg-white px-[10px]">
            <IconSearch className="size-[13px] shrink-0 text-[#858b9c]" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索任务 / 员工"
              className="h-full w-full min-w-0 bg-transparent text-[12px] text-[#18181a] outline-none placeholder:text-[#c0c6d4]"
            />
          </label>
        </div>
        <UnderlineTabs
          aria-label="定时任务筛选"
          variant="line"
          className="mb-[16px]"
          value={taskFilter}
          onChange={setTaskFilter}
          items={TASK_FILTER_TABS}
        />
        <div className="grid gap-[10px] md:hidden">
          {visibleTasks.length ? (
            visibleTasks.map(renderTaskMobileCard)
          ) : (
            <div className="py-[40px] text-center text-[13px] text-[#858b9c]">暂无定时任务</div>
          )}
        </div>
        <div className="hidden md:block">
          <DataTable
            aria-label="定时任务"
            columns={taskColumns}
            data={taskPagination.pagedItems}
            rowKey={(row) => row.id}
            loading={loading}
            emptyText="暂无定时任务"
          />
          {visibleTasks.length > 0 && (
            <Paginator
              aria-label="定时任务分页"
              page={taskPagination.page}
              pageCount={taskPagination.pageCount}
              onChange={taskPagination.setPage}
            />
          )}
        </div>
      </section>

      {/* 执行记录 */}
      <section aria-label="执行记录" className="mt-[24px]">
        <div className={SECTION_HEAD_CLASS}>
          <IconAlignJustify className="size-[14px] shrink-0" />
          <span className="text-[14px] font-normal leading-none">执行记录</span>
          {selectedTask && (
            <span className="ml-auto flex items-center gap-[8px] text-[11px] text-[#464c5e]">
              已按任务筛选：<b className="text-[#18181a]">{selectedTask.title}</b>
              <button
                type="button"
                onClick={() => setSelectedTaskId('')}
                className="text-[#1a71ff] transition-colors hover:text-[#4a8dff]"
              >
                取消
              </button>
            </span>
          )}
        </div>
        <UnderlineTabs
          aria-label="执行记录筛选"
          variant="line"
          className="mb-[16px]"
          value={runFilter}
          onChange={setRunFilter}
          items={RUN_FILTER_TABS}
        />
        <div className="grid gap-[10px] md:hidden">
          {filteredRuns.length ? (
            filteredRuns.map(renderRunMobileCard)
          ) : (
            <div className="py-[40px] text-center text-[13px] text-[#858b9c]">暂无执行记录</div>
          )}
        </div>
        <div className="hidden md:block">
          <DataTable
            aria-label="执行记录"
            columns={runColumns}
            data={runPagination.pagedItems}
            rowKey={(row) => row.id}
            loading={loading}
            emptyText="暂无执行记录"
            size="compact"
            striped
            bordered
          />
          {filteredRuns.length > 0 && (
            <Paginator
              aria-label="执行记录分页"
              page={runPagination.page}
              pageCount={runPagination.pageCount}
              onChange={runPagination.setPage}
            />
          )}
        </div>
      </section>

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        loading={deleting}
        title={`删除定时任务「${deleteTarget?.title ?? ''}」？`}
        description="删除后不再唤醒该员工，历史执行记录会继续保留。"
        onConfirm={() => void confirmDelete()}
      />
    </>
  );
}

function RuntimeTile({
  label,
  value,
  hint,
  danger,
}: {
  label: string;
  value: number;
  hint: string;
  danger?: boolean;
}) {
  return (
    <div
      className={cn(
        'rounded-[11px] border border-[#eef0f4] bg-[#fafbfc] px-[12px] py-[10px]',
        danger && 'border-[#f6c9c9] bg-[#fdf3f3]',
      )}
    >
      <div className={cn('text-[20px] font-semibold leading-none', danger ? 'text-[#d20b0b]' : 'text-[#18181a]')}>
        {value}
      </div>
      <div className="mt-[6px] text-[12px] font-medium text-[#464c5e]">{label}</div>
      <div className="mt-[2px] text-[11px] leading-[16px] text-[#858b9c]">{hint}</div>
    </div>
  );
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return `${minutes}m${rest}s`;
}
