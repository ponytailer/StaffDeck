import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import IconAdd from '@/assets/icons/add.svg?react';
import IconRefresh from '@/assets/icons/refresh.svg?react';
import { api, TENANT_ID } from '@/api/client';
import { notify } from '@/components/ui';
import { cn } from '@/lib/utils';
import { AGENT_CATALOG, findAgentCatalogEntry } from '@/lib/agentCatalog';
import type { AgentProfileRead } from '@/types';

/**
 * 开放广场首页（默认页）：
 *
 * 左侧「我的工作台」——快捷方式画布。两种模式：
 * - 浏览（默认）：卡片只读，点击即打开（数字员工 → 对话；Agent 应用 → 工作台；链接 → 新标签页）
 * - 编辑：可拖拽摆放（16px 网格吸附）、悬停删除、添加候选、重置；「保存」写 localStorage
 *   后回到只读。
 *
 * 右侧「AI 圈 / 旅文圈」新闻 TOP——后端按天缓存 RSS 抓取结果，一日一更新；
 * 前端只渲染两组各 3 条（共 6 条，数量按整页排版定）。
 */

// ---------- 类型 ----------

type PlazaChip =
  | { kind: 'employee'; agentId: string; name: string; color: string }
  | { kind: 'agent-app'; entryId: string; name: string; color: string }
  | { kind: 'link'; url: string; name: string; color: string };

type ChipLayout = { chip: PlazaChip; x: number; y: number };

type NewsItem = {
  category: 'ai' | 'travel';
  title: string;
  summary: string;
  source: string;
  url: string;
  published_at: string;
};

type NewsPayload = { date: string; items: { ai?: NewsItem[]; travel?: NewsItem[] }; provenance: string };

// ---------- 常量 ----------

const LAYOUT_STORAGE_KEY = 'ultrarag_plaza_home_layout_v1';
const GRID = 16;
const CANVAS_HEIGHT = 620;
// 画布四周内边距：卡片不贴边，预留一道网格线
const CANVAS_PAD = GRID;

const PALETTE = ['#0f7268', '#c2703e', '#6f7b42', '#5b6fa8', '#8a5aa8'];

// 默认布局：Agent 应用前三个（本地清单即时可得，不依赖接口）。
function defaultLayout(): ChipLayout[] {
  return AGENT_CATALOG.slice(0, 3).map((entry, index) => ({
    chip: { kind: 'agent-app', entryId: entry.entry, name: entry.name, color: PALETTE[index % PALETTE.length] },
    x: CANVAS_PAD + index * (104 + GRID),
    y: CANVAS_PAD,
  }));
}

function loadLayout(): ChipLayout[] | null {
  try {
    const raw = JSON.parse(window.localStorage.getItem(LAYOUT_STORAGE_KEY) || 'null');
    if (!Array.isArray(raw?.chips)) return null;
    const chips = raw.chips.filter(
      (row: ChipLayout): row is ChipLayout =>
        row && row.chip && typeof row.x === 'number' && typeof row.y === 'number',
    );
    return chips.length > 0 ? chips : null;
  } catch {
    return null;
  }
}

function saveLayout(chips: ChipLayout[]) {
  try {
    window.localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify({ v: 1, chips }));
  } catch {
    // 隐私模式等场景写不进就算了，布局丢不了数据
  }
}

const snap = (value: number) => Math.round(value / GRID) * GRID;

function chipKey(chip: PlazaChip): string {
  if (chip.kind === 'employee') return `employee:${chip.agentId}`;
  if (chip.kind === 'agent-app') return `agent-app:${chip.entryId}`;
  return `link:${chip.url}`;
}

// ---------- 子组件 ----------

function ChipCard({
  layout,
  index,
  editing,
  dragging,
  onPointerDown,
  onPointerMove,
  onPointerUp,
  onRemove,
  onOpen,
}: {
  layout: ChipLayout;
  index: number;
  editing: boolean;
  dragging: boolean;
  onPointerDown: (event: React.PointerEvent<HTMLDivElement>) => void;
  onPointerMove: (event: React.PointerEvent<HTMLDivElement>) => void;
  onPointerUp: (event: React.PointerEvent<HTMLDivElement>) => void;
  onRemove: () => void;
  onOpen: () => void;
}) {
  const { chip } = layout;
  const label = chip.kind === 'employee' ? '数字员工' : chip.kind === 'agent-app' ? 'Agent 应用' : '链接';
  const icon = chip.kind === 'link' ? chip.name.slice(0, 1).toUpperCase() : chip.name.slice(0, 1);
  return (
    <div
      data-plaza-chip
      style={{ left: layout.x, top: layout.y, ['--chip-color' as string]: chip.color }}
      // setPointerCapture 在卡片上，move/up 事件会重定向回卡片元素，
      // 所以拖动三件套必须都挂在卡片本体上（currentTarget 才是卡片）。
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onClick={() => {
        if (!editing) onOpen();
      }}
      className={cn(
        'group absolute z-[2] w-[104px] cursor-pointer touch-none rounded-[14px] border border-black/[0.05] bg-white p-[12px_8px_10px] text-center shadow-[0_1px_2px_rgba(17,17,17,0.04),0_6px_20px_rgba(17,17,17,0.07)] transition-shadow',
        editing && 'cursor-grab',
        dragging && 'z-[20] scale-[1.06] cursor-grabbing border-[#0f7268] shadow-[0_6px_12px_rgba(28,28,30,0.12),0_24px_44px_-16px_rgba(28,28,30,0.35)]',
      )}
      title={editing ? '拖动摆放' : chip.name}
    >
      {editing && (
        <button
          type="button"
          aria-label={`移除 ${chip.name}`}
          onPointerDown={(event) => event.stopPropagation()}
          onClick={(event) => {
            event.stopPropagation();
            onRemove();
          }}
          className="absolute -right-[7px] -top-[7px] hidden h-[20px] w-[20px] items-center justify-center rounded-full border border-black/[0.08] bg-white text-[11px] text-[#757f9c] group-hover:flex hover:border-[#c0392b] hover:text-[#c0392b]"
        >
          ✕
        </button>
      )}
      <div
        className="mx-auto mb-[8px] flex h-[42px] w-[42px] items-center justify-center rounded-[12px] text-[17px] font-semibold text-white"
        style={{ backgroundColor: chip.color }}
      >
        {icon}
      </div>
      <div className="overflow-hidden text-ellipsis whitespace-nowrap text-[12.5px] font-semibold leading-[1.35] text-[#18181a]">
        {chip.name}
      </div>
      <span
        className="mt-[5px] inline-block rounded-full px-[7px] py-[3px] text-[10px] font-medium leading-none"
        style={{ backgroundColor: `color-mix(in srgb, ${chip.color} 10%, #fff)`, color: chip.color }}
      >
        {label}
      </span>
    </div>
  );
}

function NewsPanel() {
  const [payload, setPayload] = useState<NewsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = useCallback((force = false) => {
    setLoading(true);
    setError('');
    api
      .get<NewsPayload>(`/api/enterprise/plaza-news?tenant_id=${TENANT_ID}${force ? '&refresh=1' : ''}`)
      .then(setPayload)
      .catch((err: unknown) => setError(err instanceof Error ? err.message : '新闻加载失败'))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const groups = useMemo(() => {
    const ai = (payload?.items?.ai || []).slice(0, 3);
    const travel = (payload?.items?.travel || []).slice(0, 3);
    return [
      { key: 'ai', label: 'AI 圈', items: ai },
      { key: 'travel', label: '旅文圈', items: travel },
    ];
  }, [payload]);

  let rank = 0;

  return (
    <section className="flex w-[380px] shrink-0 flex-col overflow-hidden rounded-[16px] border border-black/[0.05] bg-white/85 shadow-[0_6px_20px_rgba(17,17,17,0.05)] backdrop-blur max-[1200px]:w-[320px]">
      <div className="flex items-center justify-between border-b border-black/[0.05] px-[18px] py-[14px]">
        <div className="flex items-center gap-[9px] text-[14.5px] font-semibold text-[#18181a]">
          🔥 AI 圈 · 旅文圈
        </div>
        <button
          type="button"
          onClick={() => load(true)}
          className="flex items-center gap-[6px] rounded-[9px] px-[8px] py-[5px] text-[12px] text-[#5b6274] transition-colors hover:bg-[#f4f2ec]"
          title="换一批（后端按天缓存，一日一更新）"
        >
          <IconRefresh className={cn('size-[13px]', loading && 'animate-spin')} />
          刷新
        </button>
      </div>

      <div className="flex-1 overflow-y-auto py-[6px]">
        {loading && !payload && (
          <div className="flex flex-col gap-[12px] px-[18px] py-[14px]">
            {Array.from({ length: 6 }).map((_, index) => (
              <div key={index} className="animate-pulse">
                <div className="mb-[8px] h-[13px] w-[85%] rounded bg-[#efede7]" />
                <div className="mb-[6px] h-[10px] w-full rounded bg-[#f3f1ec]" />
                <div className="h-[10px] w-[55%] rounded bg-[#f3f1ec]" />
              </div>
            ))}
          </div>
        )}
        {error && (
          <div className="px-[18px] py-[30px] text-center text-[12.5px] text-[#757f9c]">
            {error}
            <button type="button" onClick={() => load()} className="ml-[8px] text-[#0f7268] underline">
              重试
            </button>
          </div>
        )}
        {groups.map((group) => (
          <div key={group.key}>
            <div className="flex items-center gap-[8px] px-[18px] pb-[4px] pt-[12px]">
              <span
                className={cn(
                  'rounded-full px-[9px] py-[2px] text-[11px] font-semibold',
                  group.key === 'ai' ? 'bg-[#e3efed] text-[#0f7268]' : 'bg-[#f7ece4] text-[#c2703e]',
                )}
              >
                {group.label}
              </span>
              {group.key === 'travel' && payload?.date && (
                <span className="text-[11px] text-[#757f9c]">{payload.date} 更新</span>
              )}
            </div>
            {group.items.map((item) => {
              rank += 1;
              const color = PALETTE[(rank - 1) % PALETTE.length];
              return (
                <a
                  key={item.url + item.title}
                  href={item.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="group flex gap-[13px] border-b border-[#f2f0ea] px-[18px] py-[13px] last:border-b-0 hover:bg-[#fbfaf7]"
                >
                  <span
                    className="mt-[2px] flex h-[30px] w-[30px] flex-none items-center justify-center rounded-[9px] text-[12.5px] font-bold tabular-nums"
                    style={{ backgroundColor: `color-mix(in srgb, ${color} 11%, #fff)`, color }}
                  >
                    {String(rank).padStart(2, '0')}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block text-[13px] font-semibold leading-[1.45] text-[#18181a] group-hover:text-[#0f7268]">
                      {item.title}
                      <span className="ml-[4px] text-[11px] font-normal text-[#757f9c]">↗</span>
                    </span>
                    {item.summary && (
                      <span className="mt-[3px] block text-[11.5px] leading-[1.6] text-[#5b6274]">{item.summary}</span>
                    )}
                    <span className="mt-[6px] flex items-center gap-[8px] text-[11px] text-[#757f9c]">
                      <span
                        className="rounded-full px-[8px] py-[2px] font-medium"
                        style={{ backgroundColor: `color-mix(in srgb, ${color} 9%, #fff)`, color: `color-mix(in srgb, ${color} 80%, #000)` }}
                      >
                        {item.source || '佚名'}
                      </span>
                      {item.published_at && <span>{item.published_at}</span>}
                    </span>
                  </span>
                </a>
              );
            })}
            {group.items.length === 0 && !loading && (
              <div className="px-[18px] pb-[10px] text-[12px] text-[#757f9c]">暂无内容</div>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

// ---------- 添加候选弹层 ----------

function AddChipDialog({
  open,
  employees,
  existingKeys,
  onClose,
  onPickEmployee,
  onPickAgentApp,
  onPickLink,
}: {
  open: boolean;
  employees: AgentProfileRead[];
  existingKeys: Set<string>;
  onClose: () => void;
  onPickEmployee: (agent: AgentProfileRead) => void;
  onPickAgentApp: (entryId: string) => void;
  onPickLink: (name: string, url: string) => void;
}) {
  const [tab, setTab] = useState<'employees' | 'agents' | 'links'>('employees');
  const [linkName, setLinkName] = useState('');
  const [linkUrl, setLinkUrl] = useState('');
  const colorOf = (index: number) => PALETTE[index % PALETTE.length];

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-[rgba(28,28,30,0.32)] backdrop-blur-[3px]"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="flex max-h-[78vh] w-[460px] flex-col overflow-hidden rounded-[18px] border border-black/[0.05] bg-white shadow-[0_24px_64px_-20px_rgba(28,28,30,0.4)]">
        <div className="flex items-center justify-between px-[20px] pb-[12px] pt-[16px]">
          <h3 className="text-[15px] font-semibold text-[#18181a]">添加快捷方式</h3>
          <button
            type="button"
            onClick={onClose}
            className="h-[28px] w-[28px] rounded-[8px] text-[16px] text-[#757f9c] hover:bg-[#f4f2ec] hover:text-[#18181a]"
            aria-label="关闭"
          >
            ✕
          </button>
        </div>
        <div className="flex gap-[6px] border-b border-black/[0.05] px-[20px] pb-[12px]">
          {(
            [
              ['employees', '数字员工'],
              ['agents', 'Agent 应用'],
              ['links', '外部链接'],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              onClick={() => setTab(value)}
              className={cn(
                'rounded-full border px-[13px] py-[6px] text-[12.5px] transition-colors',
                tab === value
                  ? 'border-[rgba(15,114,104,0.18)] bg-[#e3efed] font-semibold text-[#0f7268]'
                  : 'border-transparent text-[#5b6274] hover:bg-[#f4f2ec]',
              )}
            >
              {label}
            </button>
          ))}
        </div>

        {tab === 'employees' && (
          <div className="flex flex-col gap-[6px] overflow-y-auto p-[14px]">
            {employees.length === 0 && (
              <div className="py-[20px] text-center text-[12.5px] text-[#757f9c]">暂无已发布到广场的数字员工</div>
            )}
            {employees.map((agent, index) => {
              const key = `employee:${agent.id}`;
              const added = existingKeys.has(key);
              return (
                <button
                  key={agent.id}
                  type="button"
                  disabled={added}
                  onClick={() => onPickEmployee(agent)}
                  className={cn(
                    'flex items-center gap-[12px] rounded-[12px] border border-black/[0.05] bg-white px-[12px] py-[10px] text-left transition-colors',
                    added ? 'cursor-not-allowed opacity-50' : 'hover:border-[#0f7268] hover:bg-[#f7fbfa]',
                  )}
                >
                  <span
                    className="flex h-[36px] w-[36px] flex-none items-center justify-center rounded-[10px] text-[15px] font-semibold text-white"
                    style={{ backgroundColor: colorOf(index) }}
                  >
                    {(agent.name || '?').slice(0, 1)}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13px] font-semibold text-[#18181a]">{agent.name}</span>
                    <span className="mt-[2px] block text-[11.5px] text-[#757f9c]">
                      {agent.description || '数字员工'}
                    </span>
                  </span>
                  <span className="text-[11.5px] font-semibold text-[#0f7268]">{added ? '已添加' : '＋ 添加'}</span>
                </button>
              );
            })}
          </div>
        )}

        {tab === 'agents' && (
          <div className="flex flex-col gap-[6px] overflow-y-auto p-[14px]">
            {AGENT_CATALOG.map((entry, index) => {
              const key = `agent-app:${entry.entry}`;
              const added = existingKeys.has(key);
              return (
                <button
                  key={entry.id}
                  type="button"
                  disabled={added}
                  onClick={() => onPickAgentApp(entry.entry)}
                  className={cn(
                    'flex items-center gap-[12px] rounded-[12px] border border-black/[0.05] bg-white px-[12px] py-[10px] text-left transition-colors',
                    added ? 'cursor-not-allowed opacity-50' : 'hover:border-[#0f7268] hover:bg-[#f7fbfa]',
                  )}
                >
                  <span
                    className="flex h-[36px] w-[36px] flex-none items-center justify-center rounded-[10px] text-[15px] font-semibold text-white"
                    style={{ backgroundColor: colorOf(index) }}
                  >
                    {entry.name.slice(0, 1)}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13px] font-semibold text-[#18181a]">{entry.name}</span>
                    <span className="mt-[2px] block text-[11.5px] text-[#757f9c]">{entry.summary}</span>
                  </span>
                  <span className="text-[11.5px] font-semibold text-[#0f7268]">{added ? '已添加' : '＋ 添加'}</span>
                </button>
              );
            })}
          </div>
        )}

        {tab === 'links' && (
          <div className="flex flex-col gap-[10px] p-[20px]">
            <label className="text-[12px] font-medium text-[#5b6274]">名称</label>
            <input
              value={linkName}
              onChange={(event) => setLinkName(event.target.value)}
              placeholder="例如：Hacker News"
              className="rounded-[10px] border border-black/[0.05] px-[12px] py-[9px] text-[13px] outline-none focus:border-[#0f7268] focus:shadow-[0_0_0_3px_rgba(15,114,104,0.1)]"
            />
            <label className="text-[12px] font-medium text-[#5b6274]">链接地址</label>
            <input
              value={linkUrl}
              onChange={(event) => setLinkUrl(event.target.value)}
              placeholder="https://…"
              className="rounded-[10px] border border-black/[0.05] px-[12px] py-[9px] text-[13px] outline-none focus:border-[#0f7268] focus:shadow-[0_0_0_3px_rgba(15,114,104,0.1)]"
            />
            <button
              type="button"
              className="mt-[4px] self-start rounded-[9px] bg-[#0f7268] px-[16px] py-[7px] text-[12.5px] font-medium text-white hover:bg-[#0c6158]"
              onClick={() => {
                if (!linkName.trim() || !linkUrl.trim()) {
                  notify.warning('请填写名称和链接');
                  return;
                }
                onPickLink(linkName.trim(), linkUrl.trim());
                setLinkName('');
                setLinkUrl('');
              }}
            >
              添加到画布
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------- 主组件 ----------

export default function PlazaHomePage({
  onUseEmployee,
  onOpenAgentApp,
}: {
  /** 点击数字员工快捷方式（父组件复用广场「使用此员工」的完整链路）。 */
  onUseEmployee: (agentId: string) => void;
  /** 点击 Agent 应用快捷方式 → 打开对应工作台。 */
  onOpenAgentApp: (entryId: string) => void;
}) {
  const [mode, setMode] = useState<'view' | 'edit'>('view');
  const [chips, setChips] = useState<ChipLayout[]>(() => loadLayout() || defaultLayout());
  const [draggingKey, setDraggingKey] = useState('');
  const [addOpen, setAddOpen] = useState(false);
  const [employees, setEmployees] = useState<AgentProfileRead[]>([]);

  const canvasRef = useRef<HTMLDivElement>(null);
  // 编辑进入时的快照，「取消」恢复
  const snapshotRef = useRef<ChipLayout[]>([]);
  const dragRef = useRef<{
    key: string;
    moved: boolean;
    startX: number;
    startY: number;
    originX: number;
    originY: number;
  } | null>(null);

  // 数字员工候选（进页面拉一次；失败静默——只影响候选列表）
  useEffect(() => {
    api
      .get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`)
      .then((rows) => setEmployees(Array.isArray(rows) ? rows : []))
      .catch(() => setEmployees([]));
  }, []);

  const existingKeys = useMemo(() => new Set(chips.map((row) => chipKey(row.chip))), [chips]);

  const startEdit = useCallback(() => {
    snapshotRef.current = chips;
    setMode('edit');
  }, [chips]);

  const cancelEdit = useCallback(() => {
    setChips(snapshotRef.current);
    setMode('view');
    setDraggingKey('');
  }, []);

  // 保存前自动对齐整理：把所有卡片按当前位置（先上后左）排序后重新排入等距网格——
  // 左右相邻间隔 1 条网格线（16px），上下相邻间隔 2 条网格线（32px），并收敛进画布边界。
  const CHIP_W = 104;
  const CHIP_H = 118;
  const CHIP_PITCH_X = CHIP_W + GRID; // 左右间隔一条线
  const CHIP_PITCH_Y = CHIP_H + GRID * 2; // 上下间隔两条线
  const saveEdit = useCallback(() => {
    const canvas = canvasRef.current;
    const width = canvas ? canvas.clientWidth : 720;
    const height = canvas ? canvas.clientHeight : CANVAS_HEIGHT;
    // 四周留一道边距，卡片不贴画布边缘
    const maxX = Math.max(CANVAS_PAD, snap(width - CHIP_W - CANVAS_PAD));
    const maxY = Math.max(CANVAS_PAD, snap(height - CHIP_H - CANVAS_PAD));
    const cols = Math.max(1, Math.floor((width - 2 * CANVAS_PAD - CHIP_W) / CHIP_PITCH_X) + 1);
    const sorted = [...chips].sort((a, b) => a.y - b.y || a.x - b.x);
    const aligned = sorted.map((row, i) => ({
      ...row,
      x: Math.min(CANVAS_PAD + (i % cols) * CHIP_PITCH_X, maxX),
      y: Math.min(CANVAS_PAD + Math.floor(i / cols) * CHIP_PITCH_Y, maxY),
    }));
    setChips(aligned);
    saveLayout(aligned);
    setMode('view');
    setDraggingKey('');
    notify.success('工作台布局已保存（卡片已自动对齐）');
  }, [chips]);

  const resetLayout = useCallback(() => {
    setChips(defaultLayout());
    notify.success('已重置为默认布局，保存后生效');
  }, []);

  const addChip = useCallback((chip: PlazaChip) => {
    const canvas = canvasRef.current;
    const width = canvas ? canvas.clientWidth : 720;
    const height = canvas ? canvas.clientHeight : CANVAS_HEIGHT;
    const x = snap(CANVAS_PAD + Math.random() * Math.max(60, width - CHIP_W - 2 * CANVAS_PAD));
    const y = snap(CANVAS_PAD + Math.random() * Math.max(60, height - CHIP_H - 2 * CANVAS_PAD));
    setChips((current) => [...current, { chip, x, y }]);
    setAddOpen(false);
  }, []);

  const removeChip = useCallback((key: string) => {
    setChips((current) => current.filter((row) => chipKey(row.chip) !== key));
  }, []);

  const openChip = useCallback(
    (chip: PlazaChip) => {
      if (chip.kind === 'employee') onUseEmployee(chip.agentId);
      else if (chip.kind === 'agent-app') onOpenAgentApp(chip.entryId);
      else window.open(chip.url, '_blank', 'noopener');
    },
    [onOpenAgentApp, onUseEmployee],
  );

  // 拖拽（编辑模式）：pointer capture + 网格吸附
  const handlePointerDown = useCallback(
    (event: React.PointerEvent<HTMLDivElement>, key: string) => {
      if (mode !== 'edit' || event.button !== 0) return;
      const el = event.currentTarget;
      dragRef.current = {
        key,
        moved: false,
        startX: event.clientX,
        startY: event.clientY,
        originX: el.offsetLeft,
        originY: el.offsetTop,
      };
      el.setPointerCapture(event.pointerId);
    },
    [mode],
  );

  const handlePointerMove = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    if (!drag.moved && Math.hypot(dx, dy) < 4) return;
    if (!drag.moved) {
      drag.moved = true;
      setDraggingKey(drag.key);
    }
    const canvas = canvasRef.current;
    const el = event.currentTarget;
    if (!canvas) return;
    const nx = Math.min(Math.max(CANVAS_PAD, drag.originX + dx), canvas.clientWidth - el.offsetWidth - CANVAS_PAD);
    const ny = Math.min(Math.max(CANVAS_PAD, drag.originY + dy), canvas.clientHeight - el.offsetHeight - CANVAS_PAD);
    el.style.left = `${nx}px`;
    el.style.top = `${ny}px`;
  }, []);

  const handlePointerUp = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    dragRef.current = null;
    const el = event.currentTarget;
    if (drag.moved) {
      const x = snap(el.offsetLeft);
      const y = snap(el.offsetTop);
      el.style.left = `${x}px`;
      el.style.top = `${y}px`;
      setDraggingKey('');
      setChips((current) => current.map((row) => (chipKey(row.chip) === drag.key ? { ...row, x, y } : row)));
    }
  }, []);

  return (
    <div className="flex flex-col gap-[16px]">
      {/* 问候行（编辑操作已收进「我的工作台」卡片头部右侧） */}
      <div className="px-[12px]">
        <h2 className="text-[22px] font-bold tracking-[0.5px] text-[#18181a]">
          下午好，<span className="text-[#0f7268]">欢迎回来</span>
        </h2>
        <p className="mt-[6px] text-[13px] text-[#5b6274]">
          从工作台直达你的数字员工与 Agent，或看看 AI 圈与旅文圈今天发生了什么。
        </p>
      </div>

      <div className="flex gap-[18px] max-[1200px]:flex-col">
        {/* 画布 */}
        <section className="flex min-w-0 flex-1 flex-col overflow-hidden rounded-[16px] border border-black/[0.05] bg-white/85 shadow-[0_6px_20px_rgba(17,17,17,0.05)] backdrop-blur">
          <div className="flex items-center justify-between border-b border-black/[0.05] px-[18px] py-[14px]">
            <div className="flex items-center gap-[9px] text-[14.5px] font-semibold text-[#18181a]">
              <span className="flex h-[26px] w-[26px] items-center justify-center rounded-[8px] bg-[#e3efed] text-[13px] text-[#0f7268]">▦</span>
              我的工作台
              {mode === 'edit' && (
                <span className="rounded-full bg-[#f7ece4] px-[9px] py-[2px] text-[11px] font-medium text-[#c2703e]">
                  编辑中
                </span>
              )}
            </div>
            <div className="flex items-center gap-[8px]">
              {mode === 'edit' ? (
                <>
                  <button
                    type="button"
                    onClick={resetLayout}
                    className="rounded-[9px] px-[10px] py-[6px] text-[12.5px] text-[#5b6274] hover:bg-[#f4f2ec]"
                  >
                    ↺ 重置
                  </button>
                  <button
                    type="button"
                    onClick={() => setAddOpen(true)}
                    className="flex items-center gap-[6px] rounded-[9px] bg-[#0f7268] px-[12px] py-[6px] text-[12.5px] font-medium text-white hover:bg-[#0c6158]"
                  >
                    <IconAdd className="size-[13px]" />
                    添加快捷方式
                  </button>
                  <button
                    type="button"
                    onClick={cancelEdit}
                    className="rounded-[9px] border border-[#e3e7f1] bg-white px-[14px] py-[6px] text-[12.5px] text-[#5b6274] hover:border-[#cbd3e6]"
                  >
                    取消
                  </button>
                  <button
                    type="button"
                    onClick={saveEdit}
                    className="rounded-[9px] bg-[#18181a] px-[14px] py-[6px] text-[12.5px] font-medium text-white hover:bg-[#18181a]/90"
                  >
                    保存布局
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  onClick={startEdit}
                  className="rounded-[9px] border border-[#e3e7f1] bg-white px-[14px] py-[6px] text-[12.5px] text-[#5b6274] hover:border-[#cbd3e6] hover:text-[#18181a]"
                >
                  ✎ 编辑布局
                </button>
              )}
            </div>
          </div>

          <div
            ref={canvasRef}
            style={{ height: CANVAS_HEIGHT, backgroundSize: `${GRID}px ${GRID}px` }}
            className="relative overflow-hidden bg-[radial-gradient(circle,rgba(24,24,26,0.07)_1px,transparent_1.2px)] [background-position:0_0] [background-image:radial-gradient(circle,rgba(24,24,26,0.07)_1px,transparent_1.2px)]"
          >
            {chips.length === 0 && (
              <div className="pointer-events-none absolute inset-0 z-[1] flex flex-col items-center justify-center gap-[10px] text-[13px] text-[#757f9c]">
                <span className="flex h-[44px] w-[44px] items-center justify-center rounded-[14px] border-[1.5px] border-dashed border-black/[0.12] text-[20px]">
                  ＋
                </span>
                {mode === 'edit' ? '点击右上角「添加快捷方式」开始布置' : '工作台是空的——点「编辑布局」添加快捷方式'}
              </div>
            )}
            {chips.map((row) => {
              const key = chipKey(row.chip);
              return (
                <ChipCard
                  key={key}
                  layout={row}
                  index={0}
                  editing={mode === 'edit'}
                  dragging={draggingKey === key}
                  onPointerDown={(event) => handlePointerDown(event, key)}
                  onPointerMove={handlePointerMove}
                  onPointerUp={handlePointerUp}
                  onRemove={() => removeChip(key)}
                  onOpen={() => openChip(row.chip)}
                />
              );
            })}
            <div className="pointer-events-none absolute bottom-[12px] right-[14px] z-[3] rounded-full border border-black/[0.05] bg-[rgba(255,255,255,0.82)] px-[10px] py-[4px] text-[11.5px] text-[#757f9c]">
              {mode === 'edit' ? '拖动卡片自由摆放（16px 网格吸附）· 悬停删除' : '点击卡片直接打开 · 点「编辑布局」调整'}
            </div>
          </div>
        </section>

        <NewsPanel />
      </div>

      <AddChipDialog
        open={addOpen}
        employees={employees}
        existingKeys={existingKeys}
        onClose={() => setAddOpen(false)}
        onPickEmployee={(agent) =>
          addChip({ kind: 'employee', agentId: agent.id, name: agent.name || agent.id, color: PALETTE[chips.length % PALETTE.length] })
        }
        onPickAgentApp={(entryId) => {
          const entry = findAgentCatalogEntry(entryId);
          addChip({ kind: 'agent-app', entryId, name: entry?.name || entryId, color: PALETTE[chips.length % PALETTE.length] });
        }}
        onPickLink={(name, url) =>
          addChip({ kind: 'link', url, name, color: PALETTE[chips.length % PALETTE.length] })
        }
      />
    </div>
  );
}
