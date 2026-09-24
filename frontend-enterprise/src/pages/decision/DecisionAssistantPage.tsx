import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  Code2,
  Gauge,
  HelpCircle,
  ListTree,
  LoaderCircle,
  Minus,
  Plus,
  RefreshCw,
  Sparkles,
  Trash2,
  Upload,
  Wand2,
} from 'lucide-react';

import AppHeader from '@/components/AppHeader';
import { Input, Popover, PopoverContent, PopoverTrigger, Textarea } from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { checkLayaHealth, predictLayaQuestions, type LayaHealth } from '../../api/laya';
import type { EnterpriseAuthUser } from '../../auth';
import { ApiError } from '../../api/client';
import { cn } from '@/lib/utils';
import {
  QUESTION_TYPE_META,
  QUESTION_TYPE_ORDER,
  buildQuestions,
  createOption,
  createQuestion,
  interpretResult,
  optionsOf,
  parseDecisionJson,
  sampleQuestions,
  validateDraft,
  withOptions,
  withType,
  type DecisionConclusion,
  type DecisionOption,
  type DecisionQuestionDraft,
  type LayaPredictResult,
  type LayaQuestionType,
} from './decisionForm';
import DecisionApiDialog from './DecisionApiDialog';

const KIND_TONE: Record<string, string> = {
  是非: 'bg-[#eef2ff] text-[#4f46e5]',
  分类: 'bg-[#e8f0ff] text-[#1a71ff]',
  评分: 'bg-[#fff7e8] text-[#8a4b00]',
};

const CONFIDENCE_TONE: Record<string, string> = {
  高: 'bg-[#e9f7ef] text-[#1a7f4b]',
  中: 'bg-[#e8f0ff] text-[#1a71ff]',
  低: 'bg-[#fff7e8] text-[#8a4b00]',
  未知: 'bg-[#f3f4f6] text-[#757f9c]',
};

function SectionCard({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <section
      className={cn(
        'flex flex-col gap-[16px] rounded-[20px] border-[0.5px] border-[#e3e7f1] bg-white px-[20px] py-[18px]',
        className,
      )}
    >
      {children}
    </section>
  );
}

function GroupTitle({ icon, title, hint }: { icon: React.ReactNode; title: string; hint?: string }) {
  return (
    <div className="flex items-center gap-[6px] text-[#757f9c]">
      {icon}
      <span className="text-[14px] font-normal leading-none text-[#464c5e]">{title}</span>
      {hint && <span className="text-[12px] text-[#a3aaba]">{hint}</span>}
    </div>
  );
}

function TypePicker({
  value,
  disabled,
  onChange,
}: {
  value: LayaQuestionType;
  disabled?: boolean;
  onChange: (next: LayaQuestionType) => void;
}) {
  return (
    <div className="inline-flex shrink-0 gap-[2px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white p-[2px]">
      {QUESTION_TYPE_ORDER.map((type) => (
        <button
          key={type}
          type="button"
          disabled={disabled}
          onClick={() => onChange(type)}
          className={cn(
            'rounded-[8px] px-[10px] py-[3px] text-[12px] transition-colors disabled:opacity-60',
            value === type
              ? 'bg-[#18181a] text-white'
              : 'text-[#5b6273] hover:bg-[#f6f6f6] hover:text-[#18181a]',
          )}
        >
          {QUESTION_TYPE_META[type].label}
        </button>
      ))}
    </div>
  );
}

function OptionRows({
  question,
  disabled,
  onChange,
}: {
  question: DecisionQuestionDraft;
  disabled: boolean;
  onChange: (options: DecisionOption[]) => void;
}) {
  const meta = QUESTION_TYPE_META[question.type];
  const options = optionsOf(question);

  function update(index: number, label: string) {
    onChange(options.map((option, position) => (position === index ? { ...option, label } : option)));
  }

  function add() {
    onChange([...options, createOption()]);
  }

  function remove(index: number) {
    if (options.length <= 2) return;
    onChange(options.filter((_, position) => position !== index));
  }

  return (
    <div className="flex flex-col gap-[6px]">
      {options.map((option, index) => (
        <div key={option.id} className="flex items-center gap-[8px]">
          <span className="grid size-[20px] shrink-0 place-items-center rounded-[6px] bg-[#f3f4f6] text-[11px] tabular-nums text-[#757f9c]">
            {index + 1}
          </span>
          <Input
            value={option.label}
            disabled={disabled}
            placeholder={meta.optionPlaceholder}
            onChange={(event) => update(index, event.target.value)}
            className="h-[30px] min-w-0 flex-1 text-[12px]"
          />
          <button
            type="button"
            aria-label="删除该选项"
            disabled={disabled || options.length <= 2}
            onClick={() => remove(index)}
            title={options.length <= 2 ? '至少保留 2 个选项' : '删除该选项'}
            className="grid size-[26px] shrink-0 place-items-center rounded-[8px] text-[#858b9c] transition-colors hover:bg-[#fce7e7] hover:text-[#c0392b] disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-[#858b9c]"
          >
            <Minus className="size-[14px]" />
          </button>
        </div>
      ))}
      <button
        type="button"
        disabled={disabled}
        onClick={add}
        className="mt-[2px] inline-flex w-fit items-center gap-[4px] rounded-[8px] px-[8px] py-[3px] text-[12px] text-[#1a71ff] transition-colors hover:bg-[#f0f5ff] disabled:opacity-50"
      >
        <Plus className="size-[13px]" />
        添加选项
      </button>
    </div>
  );
}

function QuestionCard({
  question,
  index,
  disabled,
  onChange,
  onRemove,
}: {
  question: DecisionQuestionDraft;
  index: number;
  disabled: boolean;
  onChange: (next: DecisionQuestionDraft) => void;
  onRemove: () => void;
}) {
  const meta = QUESTION_TYPE_META[question.type];
  return (
    <div className="flex flex-col gap-[10px] rounded-[14px] border-[0.5px] border-[#e3e7f1] bg-[#fafbfd] px-[14px] py-[12px]">
      <div className="flex flex-wrap items-center gap-[8px]">
        <span className="grid size-[22px] shrink-0 place-items-center rounded-[7px] bg-[#eef2ff] text-[11px] font-medium tabular-nums text-[#4f46e5]">
          {index + 1}
        </span>
        <TypePicker
          value={question.type}
          disabled={disabled}
          onChange={(type) => onChange(withType(question, type))}
        />
        <button
          type="button"
          aria-label="删除该决策内容"
          disabled={disabled}
          onClick={onRemove}
          className="ml-auto grid size-[26px] shrink-0 place-items-center rounded-[8px] text-[#858b9c] transition-colors hover:bg-[#fce7e7] hover:text-[#c0392b] disabled:opacity-40"
        >
          <Trash2 className="size-[14px]" />
        </button>
      </div>

      <div className="flex flex-col gap-[6px]">
        <span className="text-[12px] text-[#757f9c]">问题描述</span>
        <Input
          value={question.instructions}
          disabled={disabled}
          placeholder={meta.questionPlaceholder}
          onChange={(event) => onChange({ ...question, instructions: event.target.value })}
          className="h-[34px] text-[13px]"
        />
        <span className="text-[11px] leading-[16px] text-[#a3aaba]">{meta.hint}</span>
      </div>

      {meta.needsOptions && (
        <OptionRows
          question={question}
          disabled={disabled}
          onChange={(options) => onChange(withOptions(question, options))}
        />
      )}
    </div>
  );
}

function DistributionBars({ conclusion }: { conclusion: DecisionConclusion }) {
  if (conclusion.distribution.length === 0) return null;
  return (
    <div className="flex flex-col gap-[6px]">
      {conclusion.distribution.map((item, index) => (
        <div key={`${item.label}-${index}`} className="flex items-center gap-[8px]">
          <span
            title={item.label}
            className={cn(
              'w-[46%] shrink-0 truncate text-[11px]',
              item.winner ? 'font-medium text-[#18181a]' : 'text-[#858b9c]',
            )}
          >
            {item.label}
          </span>
          <span className="h-[6px] min-w-[40px] flex-1 overflow-hidden rounded-full bg-[#eef0f4]">
            <span
              className={cn('block h-full rounded-full', item.winner ? 'bg-[#18181a]' : 'bg-[#c8d3e6]')}
              style={{ width: `${Math.max(1, Math.round(item.probability * 100))}%` }}
            />
          </span>
          <span
            className={cn(
              'w-[48px] shrink-0 text-right text-[11px] tabular-nums',
              item.winner ? 'text-[#18181a]' : 'text-[#858b9c]',
            )}
          >
            {(item.probability * 100).toFixed(2)}%
          </span>
        </div>
      ))}
    </div>
  );
}

/** 尚未拿到答案的问题行：位置与左侧表单一一对应，发送后原地替换为结论。 */
function PendingCard({
  index,
  kindLabel,
  question,
}: {
  index: number;
  kindLabel: string;
  question: string;
}) {
  return (
    <div className="flex items-center gap-[8px] rounded-[14px] border-[0.5px] border-dashed border-[#e3e7f1] bg-[#fafbfd] px-[14px] py-[11px]">
      <span className="grid size-[20px] shrink-0 place-items-center rounded-[6px] bg-[#f3f4f6] text-[11px] tabular-nums text-[#a3aaba]">
        {index}
      </span>
      <span
        className={cn(
          'inline-flex h-[18px] shrink-0 items-center rounded-full px-[7px] text-[10px] font-medium leading-none',
          KIND_TONE[kindLabel] ?? 'bg-[#f3f4f6] text-[#757f9c]',
        )}
      >
        {kindLabel}
      </span>
      <span className="min-w-0 flex-1 truncate text-[12px] text-[#757f9c]" title={question}>
        {question || '（未填写问题描述）'}
      </span>
      <span className="shrink-0 text-[11px] text-[#c0c6d4]">待决策</span>
    </div>
  );
}

function ConclusionCard({ conclusion, index }: { conclusion: DecisionConclusion; index?: number }) {
  return (
    <article className="flex flex-col gap-[10px] rounded-[14px] border-[0.5px] border-[#e3e7f1] bg-white px-[14px] py-[12px]">
      <div className="flex items-start justify-between gap-[10px]">
        <div className="flex min-w-0 flex-col gap-[6px]">
          <span className="flex min-w-0 items-center gap-[6px]">
            {index !== undefined && (
              <span className="grid size-[18px] shrink-0 place-items-center rounded-[6px] bg-[#eef2ff] text-[10px] font-medium tabular-nums text-[#4f46e5]">
                {index}
              </span>
            )}
            <span
              className={cn(
                'inline-flex h-[18px] shrink-0 items-center rounded-full px-[7px] text-[10px] font-medium leading-none',
                KIND_TONE[conclusion.kindLabel] ?? 'bg-[#f3f4f6] text-[#757f9c]',
              )}
            >
              {conclusion.kindLabel}
            </span>
            <span className="truncate text-[12px] text-[#858b9c]" title={conclusion.question}>
              {conclusion.question || '（未命名问题）'}
            </span>
          </span>
          <p className="text-[17px] font-semibold leading-[24px] text-[#18181a] break-words">
            {conclusion.statement}
          </p>
          {conclusion.detail && (
            <span className="text-[11px] leading-[16px] text-[#a3aaba]">{conclusion.detail}</span>
          )}
        </div>
        <div className="flex shrink-0 flex-col items-end gap-[4px]">
          <span
            className={cn(
              'inline-flex h-[20px] items-center rounded-full px-[8px] text-[11px] font-medium leading-none',
              CONFIDENCE_TONE[conclusion.confidenceLabel],
            )}
          >
            置信度 {conclusion.confidenceLabel}
            {conclusion.confidence !== null && ` ${(conclusion.confidence * 100).toFixed(1)}%`}
          </span>
          {conclusion.actProbability !== null && (
            <span className="text-[10px] text-[#a3aaba]">
              行动概率 {(conclusion.actProbability * 100).toFixed(0)}%
            </span>
          )}
        </div>
      </div>

      <DistributionBars conclusion={conclusion} />

      {conclusion.lowConfidence && (
        <div className="flex items-center gap-[6px] rounded-[8px] bg-[#fff7e8] px-[8px] py-[5px] text-[11px] text-[#8a4b00]">
          <AlertTriangle className="size-[12px] shrink-0" />
          置信度偏低，建议人工复核或补充背景信息后重跑
        </div>
      )}
    </article>
  );
}

export default function DecisionAssistantPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
} = {}) {
  const [background, setBackground] = useState('');
  const [questions, setQuestions] = useState<DecisionQuestionDraft[]>(() => [createQuestion('noul')]);
  const [result, setResult] = useState<LayaPredictResult | null>(null);
  const [conclusions, setConclusions] = useState<DecisionConclusion[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [health, setHealth] = useState<LayaHealth | null>(null);
  const [apiDialogOpen, setApiDialogOpen] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const importInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    let active = true;
    checkLayaHealth()
      .then((state) => {
        if (active) setHealth(state);
      })
      .catch(() => {
        if (active) setHealth(null);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => () => abortRef.current?.abort(), []);

  /** 「可导入的 JSON」提示内容：用官方示例拼出等价请求体，不给同一份样例维护两处。 */
  const importSample = useMemo(() => {
    const sample = sampleQuestions();
    return {
      state: { background: sample.background },
      questions: buildQuestions(sample.questions),
    };
  }, []);

  function importJsonFile(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0] ?? null;
    // 先清空 value：同一个文件连续导入两次也要能触发 change
    event.target.value = '';
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const parsed = parseDecisionJson(String(reader.result ?? ''));
      if (!parsed.ok) {
        setError(parsed.message);
        notify.error(parsed.message);
        return;
      }
      setBackground(parsed.background);
      setQuestions(parsed.questions);
      setConclusions([]);
      setResult(null);
      setError(null);
      if (parsed.warning) notify.warning(parsed.warning);
      notify.success(`已导入 ${parsed.questions.length} 条决策内容`);
    };
    reader.onerror = () => notify.error('文件读取失败，请重试。');
    reader.readAsText(file);
  }

  const run = useCallback(async () => {
    const invalid = validateDraft(background, questions);
    if (invalid) {
      setError(invalid);
      notify.error(invalid);
      return;
    }
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setRunning(true);
    setError(null);
    try {
      const response = await predictLayaQuestions(
        { state: { background: background.trim() }, questions: buildQuestions(questions) },
        controller.signal,
      );
      setResult(response);
      setConclusions(interpretResult(questions, response));
      if (response.routing?.model && health && !health.reachable) setHealth({ ...health, reachable: true });
    } catch (cause) {
      if (controller.signal.aborted) return;
      const message = cause instanceof ApiError || cause instanceof Error ? cause.message : '决策失败';
      setError(message);
      notify.error(message);
    } finally {
      setRunning(false);
    }
  }, [background, questions, health]);

  const filledQuestionCount = useMemo(
    () => questions.filter((question) => question.instructions.trim()).length,
    [questions],
  );

  /**
   * 题型变了，之前那次推理的结论就不再对应这条问题（比如分类的答案挂在是非题上），
   * 直接丢掉该问题的结论，右栏回到「待决策」。
   */
  const invalidateConclusion = useCallback((key: string) => {
    setConclusions((prev) => prev.filter((conclusion) => conclusion.key !== key));
  }, []);

  /**
   * 右栏按左侧表单的顺序一一对应：发送前是「待决策」占位行，拿到答案后原地替换成结论。
   * 上游额外返回的 key 追加在末尾，避免静默丢失。
   */
  const answerRows = useMemo(() => {
    const byKey = new Map(conclusions.map((conclusion) => [conclusion.key, conclusion]));
    return questions.map((question, index) => ({
      key: question.key,
      index: index + 1,
      kindLabel: QUESTION_TYPE_META[question.type].shortLabel,
      question: question.instructions.trim(),
      conclusion: byKey.get(question.key) ?? null,
    }));
  }, [questions, conclusions]);

  /**
   * 「查看原始返回」展示的内容：剔掉 `routing`。
   * 那段是服务端内部的路由/语言检测细节（repo、reason、script_profile…），对使用者是噪音。
   */
  const rawResult = useMemo(() => {
    if (!result) return null;
    const copy: Record<string, unknown> = { ...result };
    delete copy.routing;
    return copy;
  }, [result]);

  const extraRows = useMemo(() => {
    const knownKeys = new Set(questions.map((question) => question.key));
    return conclusions
      .filter((conclusion) => !knownKeys.has(conclusion.key))
      .map((conclusion, index) => ({
        key: conclusion.key,
        index: questions.length + index + 1,
        kindLabel: conclusion.kindLabel,
        question: conclusion.question,
        conclusion,
      }));
  }, [questions, conclusions]);

  return (
    <div
      className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]"
      onKeyDown={(event) => {
        if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
          event.preventDefault();
          void run();
        }
      }}
    >
      <AppHeader
        className="items-center"
        onLogout={onLogout}
        userName={currentUser?.username}
        title="决策助手"
        description="把决策背景与待判问题交给 Laya，一次得到分类、评分与是非结论"
      />

      {health && !health.reachable && (
        <div className="mt-[16px] flex items-start gap-[10px] rounded-[12px] border border-[#f3d28b] bg-[#fff8e8] px-[16px] py-[10px] text-[12px] leading-[18px] text-[#6f4500]">
          <AlertTriangle className="mt-[2px] size-[14px] shrink-0" />
          <span>
            决策服务当前不可达（{health.upstream_url}）。{health.message ? `原因：${health.message}` : ''}
            请确认 Laya 服务已启动后重试。
          </span>
        </div>
      )}

      <div className="mt-[20px] grid items-start gap-[16px] xl:grid-cols-[minmax(0,1fr)_440px]">
        <SectionCard>
          <div className="flex flex-wrap items-center justify-between gap-[10px]">
            <GroupTitle
              icon={<Sparkles className="size-[14px] shrink-0" />}
              title="决策背景"
              hint="必填 · 描述这次要决策的事情"
            />
            <UIButton
              type="button"
              onClick={() => setApiDialogOpen(true)}
              className="h-[30px] shrink-0 gap-[4px] rounded-[8px] border-[0.5px] border-[#cfe0ff] bg-[#f4f8ff] px-[12px] text-[12px] font-normal text-[#1a71ff] hover:bg-[#e9f1ff]"
            >
              <Code2 className="size-[13px]" />
              API 接入
            </UIButton>
          </div>
          <Textarea
            rows={4}
            value={background}
            disabled={running}
            placeholder="例如：客户来电反馈账单被重复扣款，情绪激动，明确要求退款并威胁投诉"
            onChange={(event) => setBackground(event.target.value)}
            className="min-h-[96px] resize-y text-[13px]"
          />

          <div className="flex items-center justify-between gap-[10px]">
            <GroupTitle
              icon={<ListTree className="size-[14px] shrink-0" />}
              title="决策内容"
              hint={`共 ${questions.length} 条 · 已填写 ${filledQuestionCount} 条`}
            />
            <UIButton
              type="button"
              disabled={running}
              onClick={() => setQuestions((prev) => [...prev, createQuestion('noul')])}
              className="h-[30px] shrink-0 gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6] disabled:opacity-60"
            >
              <Plus className="size-[13px]" />
              添加决策内容
            </UIButton>
          </div>

          <div className="flex flex-col gap-[10px]">
            {questions.map((question, index) => (
              <QuestionCard
                key={question.id}
                question={question}
                index={index}
                disabled={running}
                onChange={(next) => {
                  setQuestions((prev) => prev.map((item) => (item.id === next.id ? next : item)));
                  if (next.type !== question.type) invalidateConclusion(next.key);
                }}
                onRemove={() =>
                  setQuestions((prev) =>
                    prev.length <= 1 ? prev : prev.filter((item) => item.id !== question.id),
                  )
                }
              />
            ))}
            <span className="text-[11px] leading-[16px] text-[#a3aaba]">
              切换题型不会清空已填内容：分类与评分各自保存自己的选项，来回切换互不覆盖。
            </span>
            {questions.length <= 1 && (
              <span className="text-[11px] text-[#a3aaba]">至少保留一条决策内容</span>
            )}
          </div>

          <div className="flex flex-wrap items-center justify-between gap-[10px] border-t-[0.5px] border-[#eef0f4] pt-[14px]">
            <div className="flex flex-wrap items-center gap-[8px]">
              <UIButton
                type="button"
                disabled={running}
                onClick={() => importInputRef.current?.click()}
                className="h-[30px] gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6] disabled:opacity-60"
              >
                <Upload className="size-[13px]" />
                导入 JSON
              </UIButton>
              <input
                ref={importInputRef}
                type="file"
                accept=".json,application/json"
                data-testid="decision-import-input"
                className="hidden"
                onChange={importJsonFile}
              />
              <Popover>
                <PopoverTrigger asChild>
                  <button
                    type="button"
                    aria-label="查看可导入的 JSON 格式"
                    className="grid size-[30px] shrink-0 place-items-center rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white text-[#757f9c] transition-colors hover:bg-[#f6f6f6] hover:text-[#18181a]"
                  >
                    <HelpCircle className="size-[14px]" />
                  </button>
                </PopoverTrigger>
                <PopoverContent
                  side="top"
                  align="start"
                  className="w-[540px] max-w-[calc(100vw-48px)] p-[14px]"
                >
                  <div className="text-[12px] font-medium text-[#18181a]">可导入的 JSON 格式</div>
                  <p className="mt-[6px] text-[11px] leading-[17px] text-[#757f9c]">
                    与发给 Laya 的请求体一致：state.background 是决策背景；questions 里每个 key 是一道题。
                    是非题只填 type 与 instructions；分类题的 criteria 是「选项名 → 说明」；评分题的
                    criteria 是档位数组（由轻到重）。
                  </p>
                  <pre className="mt-[8px] max-h-[260px] overflow-auto rounded-[10px] bg-[#1d2027] p-[10px] font-mono text-[11px] leading-[16px] text-[#e7ebf3]">
                    {JSON.stringify(importSample, null, 2)}
                  </pre>
                  <p className="mt-[8px] text-[11px] leading-[17px] text-[#a3aaba]">
                    导入后可继续在左侧表单里修改；问题 key 沿用文件里的写法，回包按同一 key 返回。
                  </p>
                </PopoverContent>
              </Popover>
              <UIButton
                type="button"
                disabled={running}
                onClick={() => {
                  const sample = sampleQuestions();
                  setBackground(sample.background);
                  setQuestions(sample.questions);
                  setConclusions([]);
                  setResult(null);
                  setError(null);
                }}
                className="h-[30px] gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6] disabled:opacity-60"
              >
                <Wand2 className="size-[13px]" />
                填入示例
              </UIButton>
              <UIButton
                type="button"
                disabled={running}
                onClick={() => {
                  setBackground('');
                  setQuestions([createQuestion('noul')]);
                  setConclusions([]);
                  setResult(null);
                  setError(null);
                }}
                className="h-[30px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#757f9c] hover:bg-black/5 hover:text-[#18181a] disabled:opacity-60"
              >
                清空
              </UIButton>
            </div>
            <div className="flex items-center gap-[10px]">
              <span className="hidden text-[11px] text-[#a3aaba] sm:inline">⌘/Ctrl + Enter 直接提交</span>
              <UIButton
                type="button"
                disabled={running}
                onClick={() => void run()}
                className="h-[34px] gap-[6px] rounded-[10px] bg-[#18181a] px-[18px] text-[13px] font-normal text-white hover:bg-[#303030] disabled:opacity-60"
              >
                {running ? <LoaderCircle className="size-[14px] animate-spin" /> : <Gauge className="size-[14px]" />}
                {running ? '决策中…' : '开始决策'}
              </UIButton>
            </div>
          </div>

          {error && (
            <div className="flex items-start gap-[8px] rounded-[10px] border-[0.5px] border-[#f3d5d2] bg-[#fdf3f2] px-[12px] py-[9px] text-[12px] leading-[18px] text-[#c0392b]">
              <AlertTriangle className="mt-[2px] size-[13px] shrink-0" />
              <span className="break-all">{error}</span>
            </div>
          )}
        </SectionCard>

        <div className="flex flex-col gap-[16px] xl:sticky xl:top-[24px]">
          <SectionCard className="gap-[12px]">
            <div className="flex items-center justify-between gap-[10px]">
              <GroupTitle
                icon={<Gauge className="size-[14px] shrink-0" />}
                title="问题答案"
                hint="按左侧表单顺序一一对应"
              />
              {result && (
                <UIButton
                  type="button"
                  disabled={running}
                  onClick={() => void run()}
                  className="h-[28px] gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[10px] text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6] disabled:opacity-60"
                >
                  <RefreshCw className={cn('size-[12px]', running && 'animate-spin')} />
                  重跑
                </UIButton>
              )}
            </div>

            {!result && !running && (
              <div className="flex flex-col items-center gap-[8px] rounded-[14px] border-[0.5px] border-dashed border-[#e3e7f1] px-[16px] py-[20px] text-center">
                <HelpCircle className="size-[22px] text-[#c0c6d4]" />
                <span className="text-[13px] text-[#858b9c]">还没有结论</span>
                <span className="max-w-[300px] text-[12px] leading-[18px] text-[#a3aaba]">
                  下方按左侧表单顺序列出待决策的问题；点击「开始决策」后，每个问题右侧会渲染出 Laya 的结论、置信度与概率分布。
                </span>
              </div>
            )}

            {running && !result && (
              <div className="flex items-center justify-center gap-[8px] rounded-[14px] border-[0.5px] border-[#e3e7f1] px-[16px] py-[20px] text-[13px] text-[#858b9c]">
                <LoaderCircle className="size-[14px] animate-spin" />
                Laya 正在推理…
              </div>
            )}

            {result && (
              <div className="flex flex-wrap items-center gap-[8px] text-[11px] text-[#a3aaba]">
                {typeof result.elapsed_ms === 'number' && (
                  <span className="rounded-full bg-[#f3f4f6] px-[8px] py-[2px]">
                    耗时 {Math.round(result.elapsed_ms)} ms
                  </span>
                )}
                {result.routing?.model && (
                  <span className="rounded-full bg-[#f3f4f6] px-[8px] py-[2px]">模型 {result.routing.model}</span>
                )}
                <span className="rounded-full bg-[#f3f4f6] px-[8px] py-[2px]">共 {conclusions.length} 项结论</span>
              </div>
            )}

            <div className="flex flex-col gap-[10px]">
              {[...answerRows, ...extraRows].map((row) =>
                row.conclusion ? (
                  <ConclusionCard key={row.key} conclusion={row.conclusion} index={row.index} />
                ) : (
                  <PendingCard
                    key={row.key}
                    index={row.index}
                    kindLabel={row.kindLabel}
                    question={row.question}
                  />
                ),
              )}
            </div>

            {rawResult && (
              <details className="rounded-[10px] border-[0.5px] border-[#eef0f4] bg-[#fafbfd] px-[12px] py-[8px]">
                <summary className="cursor-pointer text-[12px] text-[#757f9c]">查看原始返回</summary>
                <pre className="mt-[8px] max-h-[280px] overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] leading-[16px] text-[#464c5e]">
                  {JSON.stringify(rawResult, null, 2)}
                </pre>
              </details>
            )}
          </SectionCard>
        </div>
      </div>

      <DecisionApiDialog
        open={apiDialogOpen}
        onClose={() => setApiDialogOpen(false)}
        background={background}
        questions={questions}
      />
    </div>
  );
}
