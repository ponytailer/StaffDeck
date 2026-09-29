import type { ClipboardEvent, KeyboardEvent, ReactNode } from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import AppHeader from '@/components/AppHeader';
import {
  Checkbox,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  notify,
} from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { api, TENANT_ID } from '@/api/client';
import IconChevronDown from '@/assets/icons/chevron-down.svg?react';
import IconRefresh from '@/assets/icons/refresh.svg?react';
import type { EnterpriseAuthUser } from '@/auth';
import type { AgentCatalogEntry } from '@/lib/agentCatalog';
import { cn } from '@/lib/utils';
import AgentAppBackButton from './AgentAppBackButton';
import type { ModelConfigRead } from '@/types';

import { editPathOf, setDeckField } from './deckEdit';
import {
  DECK_STYLE,
  deckSummary,
  renderDeckBody,
  renderDeckDocument,
  type SlidesDeck,
} from './slidesDeck';

/** 骨架页开关的 key 与服务端约定一致（`cover` / `toc` / `end`）。 */
type SkeletonKey = 'cover' | 'toc' | 'end';

const SKELETON_OPTIONS: { key: SkeletonKey; label: string }[] = [
  { key: 'cover', label: '首页' },
  { key: 'toc', label: '目录页' },
  { key: 'end', label: '结尾页' },
];

/** 自定义「页数」的下限：至少要容得下 1 张内容页（骨架页会占掉额度）。 */
const MIN_PAGE_COUNT = 1;
const MAX_PAGE_COUNT = 30;

const NARRATIVE_PLACEHOLDER =
  '把汇报正文整段贴进来即可 —— 背景、进展、成果、数据、下一步……';

const NARRATIVE_EXAMPLE =
  'AI 先锋项目本月完成全球招募，共有 397 位大使加入，覆盖 20+ 国家和地区。'
  + '已产生 12 场 AI 应用案例，形成四步路径：招募、培训、实践、认证。';

const FIELD_LABEL_CLASS = 'flex items-center gap-[6px] text-[12px] font-medium leading-none text-[#18181a]';
const FIELD_INPUT_CLASS =
  'h-[34px] w-full rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[10px] text-[12px] text-[#18181a] outline-none transition-colors placeholder:text-[#a3aab9] focus:border-[#18181a]';
const HINT_CLASS = 'text-[11px] leading-[1.5] text-[#a3aab9]';

function OptionalBadge({ text }: { text: string }) {
  return (
    <span className="rounded-[4px] border-[0.5px] border-[#e3e7f1] px-[5px] py-[1px] text-[10px] font-normal text-[#858b9c]">
      {text}
    </span>
  );
}

function Field({
  label,
  badge,
  children,
  hint,
}: {
  label: string;
  badge?: string;
  children: ReactNode;
  hint?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-[6px]">
      <div className={FIELD_LABEL_CLASS}>
        <span>{label}</span>
        {badge ? <OptionalBadge text={badge} /> : null}
      </div>
      {children}
      {hint ? <div className={HINT_CLASS}>{hint}</div> : null}
    </div>
  );
}

export type SlidesMakerWorkspaceProps = {
  entry: AgentCatalogEntry;
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
};

/**
 * AI 幻灯片生成工作台。
 *
 * 左栏是生成参数（模板标题 / 页面文字 / 正文口述 / 页数 / 汇报样式），底部固定一栏收「骨架页 +
 * 模型 +『导出 PPTX | 生成幻灯片』」——参数区自身可滚动，保证这两个动作按钮永远在可视区。
 * 右栏是「幻灯片预览 | HTML 源码」两个视图，都由 `slidesDeck.ts` 里同一个渲染函数产出，
 * 不会出现预览与源码不一致。
 *
 * 「页数」是**总页数**：内容页 = 页数 - 勾选的骨架页（至少 1 张），换算在服务端完成，
 * 前端只负责保证输入下限（页数 ≥ 骨架页数 + 1）。
 *
 * 在线修改：预览里的文字可以直接用鼠标点开改（`contenteditable` + `data-sd-path`）。
 * 改完由容器上的 blur 委托回填 deck —— deck 是唯一事实来源，所以「HTML 源码」与
 * 「导出 PPTX」都会带上改动。预览的 HTML 存放在 state 里、只在「生成完 / 切回预览」时重算，
 * 否则编辑过程中的一次 setState 就会重建 innerHTML、把正在编辑的元素顶掉焦点。
 *
 * 模型：只用当前用户在「模型配置」里配置并启用的模型；一个都没有时按钮不可点，
 * 并给出直达模型配置的入口（不能用别人的模型代跑）。
 *
 * 汇报样式：清单 JSON 的 `prompt` 是该 Agent 自带的固定口径，勾上「旅文汇报样式」才随请求
 * 下发；不勾则完全走通用 prompt，两者互不干扰。
 */
export default function SlidesMakerWorkspace({ entry, currentUser, onLogout }: SlidesMakerWorkspaceProps) {
  const navigate = useNavigate();
  const [templateTitle, setTemplateTitle] = useState('');
  const [pageLabel, setPageLabel] = useState('');
  const [narrative, setNarrative] = useState('');
  const [pageCountMode, setPageCountMode] = useState<'custom' | 'auto'>('custom');
  const [pageCount, setPageCount] = useState(5);
  const [skeleton, setSkeleton] = useState<Record<SkeletonKey, boolean>>({
    cover: false,
    toc: false,
    end: false,
  });
  const [models, setModels] = useState<ModelConfigRead[]>([]);
  const [modelsLoading, setModelsLoading] = useState(true);
  const [modelId, setModelId] = useState('');
  const [generating, setGenerating] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [useReportStyle, setUseReportStyle] = useState(false);
  const [deck, setDeck] = useState<SlidesDeck | null>(null);
  /** 预览的 HTML 快照：只在生成完 / 切回预览时更新，编辑过程中不重算（见组件注释）。 */
  const [previewHtml, setPreviewHtml] = useState('');
  const [edited, setEdited] = useState(false);
  const [activeTab, setActiveTab] = useState<'preview' | 'source'>('preview');

  useEffect(() => {
    let alive = true;
    setModelsLoading(true);
    api
      .get<ModelConfigRead[]>(`/api/enterprise/model-configs?tenant_id=${TENANT_ID}`)
      .then((rows) => {
        if (!alive) return;
        // 只列当前用户自己配置且已启用的模型（接口本身已按 user_id 过滤）
        const usable = (rows || []).filter((row) => row.enabled);
        setModels(usable);
        setModelId((current) => {
          if (current && usable.some((row) => row.id === current)) return current;
          return (usable.find((row) => row.is_default) || usable[0])?.id || '';
        });
      })
      .catch((error: unknown) => {
        if (alive) notify.error(error instanceof Error ? error.message : '加载模型配置失败');
      })
      .finally(() => {
        if (alive) setModelsLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  const selectedModel = useMemo(
    () => models.find((row) => row.id === modelId) || null,
    [modelId, models],
  );
  /** 清单里给这个 Agent 配的固定口径（如「旅文汇报样式」）；为空则不展示勾选项。 */
  const stylePrompt = (entry.prompt || '').trim();
  const hasStylePrompt = stylePrompt.length > 0;
  const skeletonCount = SKELETON_OPTIONS.filter((option) => skeleton[option.key]).length;
  /** 总页数扣掉勾选的骨架页，至少留 1 张内容页。 */
  const contentPages = Math.max(1, pageCount - skeletonCount);
  const hasNarrative = narrative.trim().length > 0;
  const canGenerate = hasNarrative && Boolean(modelId) && !generating;
  const canExport = Boolean(deck) && !exporting;
  const deckHtmlDocument = useMemo(() => (deck ? renderDeckDocument(deck) : ''), [deck]);
  const pageCountHint = pageCountMode === 'auto'
    ? '由 AI 按内容自动决定'
    : `共 ${pageCount} 页 ＝ 内容 ${contentPages} 页 ＋ 骨架 ${skeletonCount} 页`;

  // 勾选的骨架页会占掉总页数额度：把输入下限抬到「骨架页数 + 1」，只往上抬、不跟用户抢
  useEffect(() => {
    setPageCount((value) => Math.min(MAX_PAGE_COUNT, Math.max(value, skeletonCount + MIN_PAGE_COUNT)));
  }, [skeletonCount]);

  const toggleSkeleton = useCallback((key: SkeletonKey) => {
    setSkeleton((current) => ({ ...current, [key]: !current[key] }));
  }, []);

  const selectTab = useCallback(
    (tab: 'preview' | 'source') => {
      setActiveTab(tab);
      // 预览容器在「源码」tab 下是卸载状态，切回来必须用最新 deck 重建
      if (tab === 'preview' && deck) setPreviewHtml(renderDeckBody(deck, { editable: true }));
    },
    [deck],
  );

  const generate = useCallback(async () => {
    if (!modelId) {
      notify.warning('请先在「模型配置」里配置一个属于你的模型');
      return;
    }
    if (!hasNarrative) {
      notify.warning('请先填写正文口述');
      return;
    }
    setGenerating(true);
    try {
      const result = await api.post<SlidesDeck>('/api/enterprise/agent-apps/slides:generate', {
        tenant_id: TENANT_ID,
        model_config_id: modelId,
        narrative: narrative.trim(),
        template_title: templateTitle.trim(),
        page_label: pageLabel.trim(),
        page_count_mode: pageCountMode,
        // 「页数」是总页数（含骨架页），内容张数由服务端换算
        page_count: pageCountMode === 'auto' ? 0 : pageCount,
        skeleton_pages: SKELETON_OPTIONS.filter((option) => skeleton[option.key]).map((option) => option.key),
        // 勾了「旅文汇报样式」才把清单里的固定口径带上；不勾则完全走通用 prompt
        style_prompt: useReportStyle ? stylePrompt : '',
      });
      setDeck(result);
      setPreviewHtml(renderDeckBody(result, { editable: true }));
      setEdited(false);
      setActiveTab('preview');
      notify.success(`已生成 ${result.pages?.length || 0} 页幻灯片`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '幻灯片生成失败');
    } finally {
      setGenerating(false);
    }
  }, [
    hasNarrative,
    modelId,
    narrative,
    pageCount,
    pageCountMode,
    pageLabel,
    skeleton,
    stylePrompt,
    templateTitle,
    useReportStyle,
  ]);

  /** 导出的是当前展示的这一份 deck（含鼠标改动；后端只做渲染，不再调模型）。 */
  const exportPptx = useCallback(async () => {
    if (!deck) {
      notify.warning('请先生成幻灯片后再导出');
      return;
    }
    setExporting(true);
    try {
      const blob = await api.postBlob('/api/enterprise/agent-apps/slides:export', {
        deck,
        file_name: templateTitle.trim() || deck.deck_title || '',
      });
      const fileName = `${templateTitle.trim() || deck.deck_title || '演示文稿'}.pptx`;
      const objectUrl = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = fileName;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(objectUrl);
      notify.success(`已开始下载 ${fileName}`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '幻灯片导出失败');
    } finally {
      setExporting(false);
    }
  }, [deck, templateTitle]);

  /** 鼠标改完离开某个字段：按 `data-sd-path` 把这一处改动写回 deck。 */
  const commitEdit = useCallback(
    (target: EventTarget | null) => {
      if (!deck) return;
      const element = target as HTMLElement | null;
      const path = editPathOf(element);
      if (!path) return;
      const next = setDeckField(deck, path, element?.textContent || '');
      if (next === deck) return;
      setDeck(next);
      setEdited(true);
    },
    [deck],
  );

  const handlePreviewKeyDown = useCallback((event: KeyboardEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement;
    if (!target.dataset?.sdPath) return;
    // 幻灯片里的每个字段都是单行：回车即提交（不插 <br>），Esc 直接失焦
    if (event.key === 'Enter') {
      event.preventDefault();
      target.blur();
    }
  }, []);

  const handlePreviewPaste = useCallback((event: ClipboardEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement;
    if (!target.dataset?.sdPath) return;
    event.preventDefault();
    // 只收纯文本：外部复制来的富文本会把幻灯片的版式结构带进来
    const plain = event.clipboardData.getData('text/plain').replace(/\s+/g, ' ').trim();
    if (plain) document.execCommand('insertText', false, plain);
  }, []);

  return (
    <div className="min-h-full box-border px-[48px] pt-[20px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        className="mb-[16px]"
        onLogout={onLogout}
        userName={currentUser?.username}
        title={`Agent 广场 · ${entry.name}`}
      />

      <div className="flex h-[calc(100vh-190px)] min-h-[560px] flex-col gap-[14px] rounded-[20px] bg-white p-[16px_16px_16px_16px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        {/* 名称/作者/更新时间不再在卡片内重复展示（页面标题里已有），只留右上角的返回入口 */}
        <div className="flex flex-wrap items-center justify-end gap-[10px]">
          <AgentAppBackButton />
        </div>

        <div className="flex min-h-0 flex-1 gap-[18px] max-[900px]:flex-col">
          <div className="flex w-[300px] shrink-0 flex-col border-r-[0.5px] border-[#eef0f5] pr-[16px] max-[900px]:w-full max-[900px]:border-r-0 max-[900px]:border-b-[0.5px] max-[900px]:pb-[16px]">
            {/* 上半区可滚动：内容比一屏高时只滚参数区，不把生成按钮顶出可视区 */}
            <div className="flex min-h-0 flex-1 flex-col gap-[12px] overflow-y-auto">
              <Field label="模板标题" badge="选填">
                <input
                  value={templateTitle}
                  onChange={(event) => setTemplateTitle(event.target.value)}
                  placeholder="如：AI Lab 2026年9月工作汇报"
                  className={FIELD_INPUT_CLASS}
                />
              </Field>

              <Field label="页面文字" badge="选填">
                <input
                  value={pageLabel}
                  onChange={(event) => setPageLabel(event.target.value)}
                  placeholder="如：AI Lab 月度"
                  className={FIELD_INPUT_CLASS}
                />
              </Field>

              <Field label="正文口述" badge="必填">
                <div className="relative">
                  <textarea
                    value={narrative}
                    onChange={(event) => setNarrative(event.target.value)}
                    placeholder={NARRATIVE_PLACEHOLDER}
                    rows={6}
                    className="w-full resize-none rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[10px] py-[8px] pb-[24px] text-[12px] leading-[1.6] text-[#18181a] outline-none transition-colors placeholder:text-[#a3aab9] focus:border-[#18181a]"
                  />
                  <span className="pointer-events-none absolute bottom-[8px] right-[10px] text-[11px] text-[#a3aab9]">
                    {narrative.length} 字
                  </span>
                </div>
                <div className="rounded-[8px] bg-[#f6f7f9] p-[8px]">
                  {/* 两行封顶：示例只是提示，别把参数区挤到需要滚动 */}
                  <p className="line-clamp-2 text-[11px] leading-[1.6] text-[#858b9c]">
                    <span className="mr-[4px] text-[#757f9c]">例</span>
                    {NARRATIVE_EXAMPLE}
                  </p>
                </div>
              </Field>

              <Field label="页数" hint={pageCountHint}>
                <div className="flex items-center gap-[10px]">
                  <div className="flex rounded-[8px] border-[0.5px] border-[#e3e7f1] p-[2px]">
                    {(['custom', 'auto'] as const).map((mode) => (
                      <button
                        key={mode}
                        type="button"
                        aria-pressed={pageCountMode === mode}
                        onClick={() => setPageCountMode(mode)}
                        className={cn(
                          'rounded-[6px] px-[10px] py-[4px] text-[11px] transition-colors',
                          pageCountMode === mode
                            ? 'bg-[#18181a] font-medium text-white'
                            : 'text-[#757f9c] hover:text-[#18181a]',
                        )}
                      >
                        {mode === 'custom' ? '自定义' : 'AI 自动'}
                      </button>
                    ))}
                  </div>
                  {pageCountMode === 'custom' && (
                    <div className="flex items-center gap-[4px]">
                      <input
                        type="number"
                        min={skeletonCount + MIN_PAGE_COUNT}
                        max={MAX_PAGE_COUNT}
                        value={pageCount}
                        onChange={(event) => {
                          const next = Number(event.target.value);
                          const floor = skeletonCount + MIN_PAGE_COUNT;
                          setPageCount(
                            Number.isFinite(next)
                              ? Math.min(MAX_PAGE_COUNT, Math.max(floor, Math.round(next)))
                              : floor,
                          );
                        }}
                        className="h-[30px] w-[56px] rounded-[8px] border-[0.5px] border-[#e3e7f1] px-[8px] text-center text-[12px] text-[#18181a] outline-none focus:border-[#18181a]"
                      />
                      <span className="text-[12px] text-[#757f9c]">页</span>
                    </div>
                  )}
                </div>
              </Field>

              {/* 清单里配了固定口径才出现；勾上才随请求下发，不勾走通用 prompt */}
              {hasStylePrompt && (
                <Field label="汇报样式" hint="勾选后按该 Agent 自带的固定口径生成内容">
                  <label
                    htmlFor="slides-report-style"
                    className="flex cursor-pointer items-center justify-between gap-[8px]"
                  >
                    <span className="flex items-center gap-[8px]">
                      <Checkbox
                        id="slides-report-style"
                        aria-label="旅文汇报样式"
                        checked={useReportStyle}
                        onCheckedChange={() => setUseReportStyle((current) => !current)}
                      />
                      <span className="text-[12px] text-[#4f5669]">旅文汇报样式</span>
                    </span>
                  </label>
                </Field>
              )}
            </div>

            {/* 底部一栏（固定在栏底）：骨架页 + 模型独占一行 + 「导出 PPTX | 生成幻灯片」一行 */}
            <div className="flex shrink-0 flex-col gap-[12px] border-t-[0.5px] border-[#eef0f5] pt-[12px]">
              {!modelsLoading && models.length === 0 && (
                <button
                  type="button"
                  onClick={() => navigate('/enterprise/models')}
                  className="rounded-[8px] bg-[#fff7ed] px-[10px] py-[8px] text-left text-[11px] leading-[1.6] text-[#c2410c] transition-colors hover:bg-[#ffedd5]"
                >
                  你还没有配置模型，生成不可用。点此前往「模型配置」添加自己的模型 →
                </button>
              )}

              <Field label="骨架页" hint="计入上面的「页数」">
                <div className="flex items-center gap-[14px]">
                  {SKELETON_OPTIONS.map((option) => (
                    <label
                      key={option.key}
                      htmlFor={`skeleton-${option.key}`}
                      className="flex cursor-pointer items-center gap-[6px]"
                    >
                      <Checkbox
                        id={`skeleton-${option.key}`}
                        aria-label={option.label}
                        checked={skeleton[option.key]}
                        onCheckedChange={() => toggleSkeleton(option.key)}
                      />
                      <span className="whitespace-nowrap text-[12px] text-[#4f5669]">{option.label}</span>
                    </label>
                  ))}
                </div>
              </Field>

              {/* 模型不带 label，独占一行（不跟按钮抢宽度，长模型名也不用截断得太狠） */}
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    type="button"
                    aria-label="选择模型"
                    disabled={models.length === 0}
                    className="flex h-[38px] w-full min-w-0 items-center justify-between gap-[6px] rounded-[10px] border-[0.5px] border-[#e3e7f1] px-[12px] text-[12px] text-[#4f5669] transition-colors hover:border-[#cbd3e6] disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <span className="truncate">{selectedModel?.name || (modelsLoading ? '加载中…' : '选择模型')}</span>
                    <IconChevronDown className="size-[12px] shrink-0 text-[#858b9c]" />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" side="top" className="max-h-[260px] min-w-[200px] overflow-y-auto">
                  {models.length === 0 ? (
                    <DropdownMenuItem disabled>暂无可用模型</DropdownMenuItem>
                  ) : (
                    models.map((row) => (
                      <DropdownMenuItem key={row.id} onSelect={() => setModelId(row.id)}>
                        <span className="truncate text-[12px]">{row.name}</span>
                      </DropdownMenuItem>
                    ))
                  )}
                </DropdownMenuContent>
              </DropdownMenu>

              {/* 左「导出 PPTX」/ 右「生成幻灯片」：grid 保证严格 50/50，不随文案长度浮动 */}
              <div className="grid grid-cols-2 gap-[8px]">
                <UIButton
                  type="button"
                  onClick={() => void exportPptx()}
                  disabled={!canExport}
                  title={deck ? '导出为 PPTX' : '请先生成幻灯片'}
                  className="h-[38px] w-full min-w-0 justify-center gap-[6px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[13px] font-normal text-[#4f5669] shadow-none transition-colors hover:border-[#cbd3e6] hover:bg-[#f6f7f9] disabled:cursor-not-allowed disabled:border-[#eef0f5] disabled:bg-[#f6f7f9] disabled:text-[#c2c8d4]"
                >
                  {exporting && <IconRefresh className="size-[14px] animate-spin" />}
                  {exporting ? '导出中…' : '导出 PPTX'}
                </UIButton>
                <UIButton
                  onClick={() => void generate()}
                  disabled={!canGenerate}
                  className="h-[38px] w-full min-w-0 justify-center gap-[6px] rounded-[10px] bg-[#18181a] px-[12px] text-[13px] font-normal text-white hover:bg-[#303030] disabled:cursor-not-allowed disabled:bg-[#c9cfda]"
                >
                  {generating && <IconRefresh className="size-[14px] animate-spin" />}
                  {generating ? '生成中…' : '生成幻灯片'}
                </UIButton>
              </div>
            </div>
          </div>

          <div className="flex min-h-0 flex-1 flex-col gap-[12px]">
            <div className="flex items-center justify-between gap-[12px]">
              <div role="tablist" aria-label="幻灯片视图" className="flex items-center gap-[18px]">
                {([
                  ['preview', '幻灯片预览'],
                  ['source', 'HTML 源码'],
                ] as const).map(([key, label]) => (
                  <button
                    key={key}
                    type="button"
                    role="tab"
                    aria-selected={activeTab === key}
                    onClick={() => selectTab(key)}
                    className={cn(
                      'border-b-[2px] pb-[6px] text-[13px] transition-colors',
                      activeTab === key
                        ? 'border-[#18181a] font-medium text-[#18181a]'
                        : 'border-transparent text-[#858b9c] hover:text-[#4f5669]',
                    )}
                  >
                    {label}
                  </button>
                ))}
              </div>
              {/* 导出按钮已移到左栏底部一栏（「导出 PPTX | 生成幻灯片」），这里只留状态与页数摘要 */}
              <div className="flex items-center gap-[10px]">
                {deck && edited && (
                  <span className="rounded-[4px] bg-[#eef3ff] px-[6px] py-[2px] text-[10px] text-[#2f6bff]">
                    已编辑
                  </span>
                )}
                <span className="font-mono text-[11px] text-[#a3aab9]">{deck ? deckSummary(deck) : ''}</span>
              </div>
            </div>

            <div className="min-h-0 flex-1 overflow-auto rounded-[14px] bg-[#f1f2f5] p-[24px]">
              {activeTab === 'source' ? (
                deck ? (
                  <pre className="whitespace-pre-wrap break-words rounded-[10px] bg-[#1b1f2a] p-[16px] font-mono text-[11px] leading-[1.7] text-[#d6dbe6]">
                    {deckHtmlDocument}
                  </pre>
                ) : (
                  <EmptyDeck />
                )
              ) : deck ? (
                <>
                  <style>{DECK_STYLE}</style>
                  <div className="mb-[10px] text-[11px] text-[#8b93a5]">
                    提示：预览里的文字可以直接用鼠标点开修改，改动会同步到「HTML 源码」与导出的 PPTX。
                  </div>
                  <div
                    className="sd-deck"
                    onBlur={(event) => commitEdit(event.target)}
                    onKeyDown={handlePreviewKeyDown}
                    onPaste={handlePreviewPaste}
                    dangerouslySetInnerHTML={{ __html: previewHtml }}
                  />
                </>
              ) : (
                <EmptyDeck />
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function EmptyDeck() {
  return (
    <div className="grid h-full min-h-[280px] place-items-center content-center gap-[8px] text-center">
      <span className="text-[13px] font-medium text-[#757f9c]">还没有生成内容</span>
      <span className="text-[12px] leading-[1.6] text-[#a3aab9]">
        左侧填写正文口述、选好模型后点击「生成幻灯片」，
        <br />
        这里会给出整套幻灯片的预览与 HTML 源码。
      </span>
      <span className="text-[11px] leading-[1.6] text-[#c2c8d4]">生成完成后可导出 PPTX。</span>
    </div>
  );
}
