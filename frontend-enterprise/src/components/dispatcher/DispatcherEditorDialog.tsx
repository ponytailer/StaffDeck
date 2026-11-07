/**
 * 多智能体（调度入口 Dispatcher）的创建/编辑弹窗。
 *
 * 三段式（用户已验收 docs/dispatcher-config-demo.html 的交互）：
 * ① 基础信息（名称 + **必填模型** + 说明）
 * ② 成员与路由规则（2~3 人，每人写「什么情况转给他」+ 示例问法）
 * ③ 无法匹配时怎么办（clarify / reply / default_member）+ 试跑规则
 *
 * 三条硬约束在这里和 API 层各拦一遍：
 * - 模型必填（免费额度链路的第一道闸）；
 * - 成员数 ∈ [2, 3]（只有 1 个成员时「路由」没有判断成分）；
 * - 每个成员必须写路由规则（空规则会让 router 只能靠岗位描述猜）。
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Button as UIButton,
  Dialog,
  DialogContent,
  DialogTitle,
  Input,
  Textarea,
  notify,
} from '@/components/ui';
import { cn } from '@/lib/utils';
import { api, TENANT_ID } from '../../api/client';
import {
  MAX_DISPATCH_MEMBERS,
  MIN_DISPATCH_MEMBERS,
  createDispatcher,
  memberCountHint,
  previewRoute,
  updateDispatcher,
  type DispatchFallbackMode,
  type DispatcherRead,
} from '../../lib/dispatcher';
import type { AgentProfileRead, ModelConfigRead } from '../../types';
import { employeeDisplayName, employeeProfile } from '../../employee';
import EmployeeAvatar from '../EmployeeAvatar';
import DispatcherTopology, { type TopologyMember } from './DispatcherTopology';

type DraftMember = {
  /** 本地 uuid，仅用于 React key（成员还没保存时没有稳定 id） */
  key: string;
  agentId: string;
  name: string;
  roleName: string;
  description: string;
  rule: string;
  examples: string[];
};

type DraftFallback = {
  mode: DispatchFallbackMode;
  reply: string;
  defaultMemberAgentId: string | null;
  offerCandidates: boolean;
  minConfidence: number;
};

const DEFAULT_FALLBACK_REPLY =
  '这个问题我这边暂时没有对应的同事能处理，可以换个说法，或者直接联系管理员帮你转接。';

const BLANK_FALLBACK: DraftFallback = {
  mode: 'clarify',
  reply: DEFAULT_FALLBACK_REPLY,
  defaultMemberAgentId: null,
  offerCandidates: true,
  minConfidence: 0.5,
};

let keySeed = 0;
function nextKey(): string {
  keySeed += 1;
  return `m${keySeed}-${Date.now().toString(36)}`;
}

function memberFromRead(row: DispatcherRead['members'][number]): DraftMember {
  return {
    key: nextKey(),
    agentId: row.member_agent_id,
    name: row.agent_name,
    roleName: '',
    description: row.agent_description || '',
    rule: row.routing_rule || '',
    examples: [...(row.example_queries || [])],
  };
}

export default function DispatcherEditorDialog({
  open,
  dispatcher,
  agents,
  models,
  onClose,
  onSaved,
}: {
  open: boolean;
  /** 传了就是编辑，null/undefined 是新建 */
  dispatcher?: DispatcherRead | null;
  agents: AgentProfileRead[];
  models: ModelConfigRead[];
  onClose: () => void;
  onSaved?: (row: DispatcherRead) => void;
}) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [modelConfigId, setModelConfigId] = useState('');
  const [members, setMembers] = useState<DraftMember[]>([]);
  const [fallback, setFallback] = useState<DraftFallback>(BLANK_FALLBACK);
  const [saving, setSaving] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [trialQuery, setTrialQuery] = useState('');
  const [trialResult, setTrialResult] = useState<
    | { kind: 'route'; name: string; confidence: number; reason: string; query?: string | null }
    | { kind: 'fallback'; label: string; reason: string; blocked?: boolean }
    | null
  >(null);
  const [trialLoading, setTrialLoading] = useState(false);
  const [highlightMemberId, setHighlightMemberId] = useState<string | null>(null);
  const [highlightFallback, setHighlightFallback] = useState(false);
  const fallbackCardRef = useRef<HTMLDivElement | null>(null);
  const memberCardRefs = useRef<Record<string, HTMLDivElement | null>>({});

  // 打开时初始化：编辑回填 / 新建清空
  useEffect(() => {
    if (!open) return;
    setTrialResult(null);
    setTrialQuery('');
    setHighlightMemberId(null);
    setHighlightFallback(false);
    setPickerOpen(false);
    if (dispatcher) {
      setName(dispatcher.name);
      setDescription(dispatcher.description || '');
      setModelConfigId(dispatcher.model_config_id || '');
      setMembers(dispatcher.members.map(memberFromRead));
      setFallback({
        mode: dispatcher.config.fallback_mode,
        reply: dispatcher.config.fallback_reply || DEFAULT_FALLBACK_REPLY,
        defaultMemberAgentId: dispatcher.config.default_member_agent_id || null,
        offerCandidates: dispatcher.config.offer_candidates,
        minConfidence: dispatcher.config.min_confidence,
      });
    } else {
      setName('');
      setDescription('');
      // 新建时预选一条默认模型：模型必填，让用户少点一次
      const preferred = models.find((item) => item.is_default && item.enabled) || models.find((item) => item.enabled);
      setModelConfigId(preferred?.id || '');
      setMembers([]);
      setFallback(BLANK_FALLBACK);
    }
    // models 变化不该重置表单，故意不放进依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, dispatcher]);

  const selectableAgents = useMemo(
    () =>
      agents.filter(
        (item) => !item.is_overall && item.status === 'active' && item.kind !== 'dispatcher',
      ),
    [agents],
  );

  const count = members.length;
  const countInvalid = count < MIN_DISPATCH_MEMBERS || count > MAX_DISPATCH_MEMBERS;
  const missingRule = members.filter((item) => !item.rule.trim());
  const selectedAgentIds = new Set(members.map((item) => item.agentId));

  const topologyMembers: TopologyMember[] = members.map((item) => ({
    agentId: item.agentId,
    name: item.name,
    description: item.description,
    rule: item.rule,
    exampleCount: item.examples.length,
  }));

  const modelLabel =
    models.find((item) => item.id === modelConfigId)?.name ||
    models.find((item) => item.id === modelConfigId)?.model ||
    '未配置模型';

  function addMember(agent: AgentProfileRead) {
    if (count >= MAX_DISPATCH_MEMBERS) {
      notify.error(`最多只能添加 ${MAX_DISPATCH_MEMBERS} 个同事`);
      return;
    }
    if (selectedAgentIds.has(agent.id)) return;
    const profile = employeeProfile(agent);
    setMembers((prev) => [
      ...prev,
      {
        key: nextKey(),
        agentId: agent.id,
        name: employeeDisplayName(agent),
        roleName: profile.roleName,
        description: agent.description || '',
        rule: '',
        examples: [],
      },
    ]);
    setPickerOpen(false);
    setTrialResult(null);
  }

  function removeMember(agentId: string) {
    setMembers((prev) => prev.filter((item) => item.agentId !== agentId));
    setFallback((prev) =>
      prev.defaultMemberAgentId === agentId ? { ...prev, defaultMemberAgentId: null } : prev,
    );
    setTrialResult(null);
    if (highlightMemberId === agentId) setHighlightMemberId(null);
  }

  function moveMember(agentId: string, direction: -1 | 1) {
    setMembers((prev) => {
      const index = prev.findIndex((item) => item.agentId === agentId);
      const target = index + direction;
      if (index < 0 || target < 0 || target >= prev.length) return prev;
      const next = [...prev];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  }

  function patchMember(agentId: string, patch: Partial<DraftMember>) {
    setMembers((prev) =>
      prev.map((item) => (item.agentId === agentId ? { ...item, ...patch } : item)),
    );
  }

  async function runTrial() {
    const query = trialQuery.trim();
    if (!query) {
      notify.error('先输入一句用户可能会问的话');
      return;
    }
    // 试跑前同样卡成员数：1 个成员时路由比分恒为 100%，会给出假信号
    if (countInvalid) {
      setTrialResult({
        kind: 'fallback',
        label: '无法试跑',
        reason: memberCountHint(count) || '成员数量不合法',
        // 这不是「猜不出来」，是配置没配完 —— 别渲染成兜底出口的样子
        blocked: true,
      });
      setHighlightMemberId(null);
      setHighlightFallback(false);
      return;
    }
    setTrialLoading(true);
    setTrialResult(null);
    try {
      const result = await previewRoute({
        tenant_id: TENANT_ID,
        message: query,
        model_config_id: modelConfigId || undefined,
        dispatcher_id: dispatcher?.id,
        members: members.map((item, index) => ({
          member_agent_id: item.agentId,
          routing_rule: item.rule,
          example_queries: item.examples,
          sort_order: index,
        })),
        config: {
          fallback_mode: fallback.mode,
          fallback_reply: fallback.reply,
          default_member_agent_id: fallback.defaultMemberAgentId,
          offer_candidates: fallback.offerCandidates,
          min_confidence: fallback.minConfidence,
        },
      });

      if (result.decision.action === 'route' && result.decision.member_agent_id) {
        setTrialResult({
          kind: 'route',
          name: result.member_name || '未知成员',
          confidence: result.decision.confidence,
          reason: result.decision.reason || '规则匹配',
          query: result.decision.query,
        });
        setHighlightMemberId(result.decision.member_agent_id);
        setHighlightFallback(false);
      } else {
        setTrialResult({
          kind: 'fallback',
          label:
            result.decision.action === 'reply'
              ? '直接回复固定话术'
              : fallback.mode === 'default_member'
                ? '转给默认同事'
                : '反问用户 + 列出可选同事',
          reason: result.decision.reason || '没有匹配的同事',
        });
        setHighlightMemberId(null);
        setHighlightFallback(true);
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '试跑失败');
    } finally {
      setTrialLoading(false);
    }
  }

  async function save() {
    if (!name.trim()) {
      notify.error('请填写入口名称');
      return;
    }
    if (!modelConfigId) {
      notify.error('员工小队必须配置模型才能保存');
      return;
    }
    if (countInvalid) {
      notify.error(memberCountHint(count) || '成员数量不合法');
      return;
    }
    if (missingRule.length) {
      notify.error(`还有 ${missingRule.length} 位同事没写路由规则`);
      return;
    }
    if (fallback.mode === 'default_member' && !fallback.defaultMemberAgentId) {
      notify.error('请选择兜底转给哪位同事');
      return;
    }

    setSaving(true);
    try {
      const payloadMembers = members.map((item, index) => ({
        member_agent_id: item.agentId,
        routing_rule: item.rule.trim(),
        example_queries: item.examples,
        sort_order: index,
      }));
      const config = {
        fallback_mode: fallback.mode,
        fallback_reply: fallback.reply,
        default_member_agent_id:
          fallback.mode === 'default_member' ? fallback.defaultMemberAgentId : null,
        offer_candidates: fallback.offerCandidates,
        min_confidence: fallback.minConfidence,
      };

      const row = dispatcher
        ? await updateDispatcher(dispatcher.id, {
            name: name.trim(),
            description: description.trim() || null,
            // 编辑时只在真的换了模型才提交，避免无谓地重写绑定
            ...(modelConfigId !== dispatcher.model_config_id
              ? { model_config_id: modelConfigId }
              : {}),
            members: payloadMembers,
            config,
          })
        : await createDispatcher({
            name: name.trim(),
            description: description.trim() || null,
            model_config_id: modelConfigId,
            members: payloadMembers,
            config,
          });

      notify.success(dispatcher ? '员工小队已更新' : '员工小队已创建');
      onSaved?.(row);
      onClose();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存失败');
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      {/* ⚠️ className 里**不能写 `relative`**：tailwind-merge 把 `relative` / `fixed` /
          `absolute` / `sticky` 归在同一个 position 组，后出现的会覆盖前面的。写在这里
          等于把 DialogContent 的 `fixed top-1/2 -translate-y-1/2` 改成 `position: relative`
          → 弹窗不再相对视口居中，而是被 `top: 50%` 推到 body 高度的一半（实测 y=466、
          底部溢出视口 434px，「看不见」）。成员选择浮层本来就在 Content 内部，
          `absolute inset-0` 相对的是 Content，不需要这里再声明相对定位。 */}
      <DialogContent className="flex max-h-[calc(100dvh-32px)] w-[calc(100%-32px)] flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-[880px]">
        <DialogTitle className="shrink-0 px-[24px] pt-[18px] text-[16px] font-semibold text-[#18181a]">
          {dispatcher ? '编辑员工小队' : '新建员工小队'}
          <span className="ml-[8px] rounded-[6px] bg-[#e1f1ed] px-[7px] py-[2px] align-middle text-[11px] font-normal text-[#0f7268]">
            路由型 Agent
          </span>
        </DialogTitle>
        <p className="shrink-0 px-[24px] pt-[6px] text-[12px] leading-[1.6] text-[#757f9c]">
          用户只跟这个入口对话。它不回答业务问题，只负责判断该由哪位同事处理，然后把问题转过去。
        </p>

        <div className="min-h-0 flex-1 overflow-y-auto px-[24px] pb-[8px]">
          <DispatcherTopology
            className="mt-[14px]"
            entryName={name.trim() || '未命名入口'}
            entryModel={modelLabel}
            members={topologyMembers}
            fallbackMode={fallback.mode}
            fallbackMemberName={
              members.find((item) => item.agentId === fallback.defaultMemberAgentId)?.name
            }
            highlightedMemberId={highlightMemberId}
            highlightFallback={highlightFallback}
            onMemberClick={(agentId) => {
              const card = memberCardRefs.current[agentId];
              card?.scrollIntoView({ block: 'center', behavior: 'smooth' });
              setHighlightMemberId(agentId);
            }}
            onFallbackClick={() => {
              fallbackCardRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' });
              setHighlightFallback(true);
            }}
          />

          {/* ① 基础信息 */}
          <Section index={1} title="基础信息">
            <div className="mt-[14px] grid gap-[12px] sm:grid-cols-2">
              <Field label="入口名称" required>
                <Input
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="例如：人事服务台"
                />
              </Field>
              <Field label="路由模型" required hint="路由判断与所有子流程都用它">
                <select
                  value={modelConfigId}
                  onChange={(event) => setModelConfigId(event.target.value)}
                  className="h-9 w-full rounded-[10px] border border-[#e3e7f1] bg-white px-[12px] text-[13px] text-[#18181a] outline-none focus:border-[#cbd3e6]"
                >
                  <option value="">请选择模型（必填）</option>
                  {models
                    .filter((item) => item.enabled)
                    .map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name || item.model}
                        {item.is_default ? '（默认）' : ''}
                      </option>
                    ))}
                </select>
                {models.filter((item) => item.enabled).length === 0 && (
                  <span className="text-[11.5px] text-[#dc2626]">
                    当前没有可用模型，请先到「模型管理」添加
                  </span>
                )}
              </Field>
            </div>
            <Field label="入口说明" hint="用户在员工广场看到的一句话">
              <Input
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                placeholder="人事相关问题都可以先问我，我会转给对应的同事。"
              />
            </Field>
            <div className="mt-[12px] flex items-start gap-[8px] rounded-[10px] bg-[#f7f8fa] px-[12px] py-[10px] text-[11.5px] leading-[1.6] text-[#757f9c]">
              <span className="mt-[1px] shrink-0 text-[#9aa1b1]">
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3">
                  <rect x="3.2" y="7" width="9.6" height="6.6" rx="1.6" />
                  <path d="M5.6 7V5.2a2.4 2.4 0 0 1 4.8 0V7" />
                </svg>
              </span>
              <span>
                员工小队<b className="text-[#5b6274]">不配置知识库、技能与工具</b>
                ，也不参与免费额度。路由判断与所有子流程都用上面的模型（成员自己配了模型则用成员自己的）。
              </span>
            </div>
          </Section>

          {/* ② 成员与路由规则 */}
          <Section
            index={2}
            title="成员与路由规则"
            extra={
              <span
                className={cn(
                  'rounded-[6px] px-[7px] py-[2px] text-[11px] font-medium',
                  countInvalid
                    ? 'bg-[#fdeaea] text-[#dc2626]'
                    : count >= MAX_DISPATCH_MEMBERS
                      ? 'bg-[#e1f1ed] text-[#0f7268]'
                      : 'bg-[#f0f1f4] text-[#757f9c]',
                )}
              >
                {count} / {MAX_DISPATCH_MEMBERS}
              </span>
            }
          >
            <p className="text-[11.5px] leading-[1.6] text-[#757f9c]">
              每个成员写清楚「什么情况转给他」。
              <b className="text-[#5b6274]">写负向条件效果最好</b>
              ——例如「涉及薪资的不要转给他，转给薪资专员」，能显著减少转错人。
            </p>

            {countInvalid && (
              <div className="mt-[10px] flex items-start gap-[6px] rounded-[10px] border border-[#f5d5a8] bg-[#fdf6ec] px-[12px] py-[9px] text-[11.5px] leading-[1.6] text-[#8a5a12]">
                <span className="shrink-0">•</span>
                <span>{memberCountHint(count)}</span>
              </div>
            )}

            <div className="mt-[12px] flex flex-col gap-[12px]">
              {members.map((member, index) => {
                const ruleMissing = !member.rule.trim();
                return (
                  <div
                    key={member.key}
                    ref={(node) => {
                      memberCardRefs.current[member.agentId] = node;
                    }}
                    className={cn(
                      'rounded-[12px] border bg-white px-[14px] py-[12px] transition-colors',
                      highlightMemberId === member.agentId
                        ? 'border-[#0f7268] shadow-[0_0_0_3px_rgba(15,114,104,0.10)]'
                        : 'border-black/[0.06]',
                    )}
                  >
                    <div className="flex items-start gap-[10px]">
                      <span className="mt-[6px] grid size-[18px] shrink-0 place-items-center rounded-[5px] bg-[#f0f1f4] text-[11px] font-semibold text-[#757f9c]">
                        {index + 1}
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-x-[6px] text-[13px] font-medium text-[#18181a]">
                          {member.name}
                          {member.roleName && (
                            <span className="text-[11.5px] font-normal text-[#9aa1b1]">
                              · {member.roleName}
                            </span>
                          )}
                        </div>
                        {member.description && (
                          <div className="mt-[1px] truncate text-[11.5px] text-[#9aa1b1]">
                            {member.description}
                          </div>
                        )}
                      </div>
                      <div className="flex shrink-0 items-center gap-[2px]">
                        <IconAction
                          label="上移"
                          disabled={index === 0}
                          onClick={() => moveMember(member.agentId, -1)}
                        >
                          <path d="M8 12.5v-9M4.2 7.3 8 3.5l3.8 3.8" />
                        </IconAction>
                        <IconAction
                          label="下移"
                          disabled={index === members.length - 1}
                          onClick={() => moveMember(member.agentId, 1)}
                        >
                          <path d="M8 3.5v9M4.2 8.7 8 12.5l3.8-3.8" />
                        </IconAction>
                        <IconAction label="移除" onClick={() => removeMember(member.agentId)}>
                          <path d="M4.2 4.2 11.8 11.8M11.8 4.2 4.2 11.8" />
                        </IconAction>
                      </div>
                    </div>

                    <div className="mt-[10px]">
                      <label className="mb-[5px] block text-[11.5px] text-[#5b6274]">
                        什么情况转给他 <span className="text-[#dc2626]">*</span>
                      </label>
                      <Textarea
                        rows={3}
                        value={member.rule}
                        onChange={(event) =>
                          patchMember(member.agentId, { rule: event.target.value })
                        }
                        placeholder={
                          '例：员工咨询入职材料、离职流程、考勤异常时转给他。\n涉及薪资核算的问题不要转给他，转给小李。'
                        }
                        className={cn(
                          'text-[12.5px] leading-[1.7]',
                          ruleMissing && 'border-[#f0d5a8]',
                        )}
                      />
                      <div className="mt-[4px] text-[11px] leading-[1.5] text-[#9aa1b1]">
                        支持多行。写清「什么情况转给他」+「什么情况
                        <b className="text-[#757f9c]">不要</b>
                        ​转给他」，比只写岗位描述准得多。
                      </div>
                    </div>

                    <div className="mt-[9px]">
                      <label className="mb-[5px] block text-[11.5px] text-[#5b6274]">
                        示例问法{' '}
                        <span className="text-[11px] font-normal text-[#9aa1b1]">
                          建议 2~5 条，能明显提高识别率
                        </span>
                      </label>
                      <ExampleChips
                        examples={member.examples}
                        onChange={(examples) => patchMember(member.agentId, { examples })}
                      />
                    </div>
                  </div>
                );
              })}
            </div>

            <button
              type="button"
              disabled={count >= MAX_DISPATCH_MEMBERS}
              onClick={() => setPickerOpen(true)}
              className={cn(
                'mt-[12px] flex h-[38px] w-full items-center justify-center gap-[6px] rounded-[10px] border border-dashed text-[12.5px] transition-colors',
                count >= MAX_DISPATCH_MEMBERS
                  ? 'cursor-not-allowed border-[#e3e7f1] text-[#c3c9d6]'
                  : 'border-[#cbd3e6] text-[#5b6274] hover:border-[#0f7268] hover:text-[#0f7268]',
              )}
            >
              <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6">
                <path d="M8 3.5v9M3.5 8h9" />
              </svg>
              {count >= MAX_DISPATCH_MEMBERS
                ? `已达上限 ${MAX_DISPATCH_MEMBERS} 个同事`
                : '添加同事'}
            </button>
          </Section>

          {/* ③ 无法匹配时 */}
          <Section
            index={3}
            title="无法匹配时怎么办"
            extra={null}
            ref={fallbackCardRef}
          >
            <p className="text-[11.5px] leading-[1.6] text-[#757f9c]">
              当问题不属于任何成员、或路由把握不足时，按这里配置的方式处理。
            </p>

            <div className="mt-[10px] flex flex-col gap-[8px]">
              <FallbackOption
                active={fallback.mode === 'clarify'}
                title="反问用户，并列出可选的同事"
                desc="最稳妥。用户能在对话里直接看到谁可能帮得上，再补充一句或点选。"
                onSelect={() => {
                  setFallback((prev) => ({ ...prev, mode: 'clarify' }));
                  setTrialResult(null);
                  setHighlightFallback(false);
                }}
              >
                <label className="flex items-center gap-[6px] text-[11.5px] text-[#5b6274]">
                  <input
                    type="checkbox"
                    checked={fallback.offerCandidates}
                    onChange={(event) =>
                      setFallback((prev) => ({ ...prev, offerCandidates: event.target.checked }))
                    }
                  />
                  列出可选的同事
                </label>
              </FallbackOption>

              <FallbackOption
                active={fallback.mode === 'reply'}
                title="直接回复固定话术"
                desc="不猜、不罗列，一句话说清并给下一步建议。"
                onSelect={() => {
                  setFallback((prev) => ({ ...prev, mode: 'reply' }));
                  setTrialResult(null);
                  setHighlightFallback(false);
                }}
              >
                <Textarea
                  rows={2}
                  value={fallback.reply}
                  onChange={(event) =>
                    setFallback((prev) => ({ ...prev, reply: event.target.value }))
                  }
                  className="text-[12px]"
                  placeholder={DEFAULT_FALLBACK_REPLY}
                />
              </FallbackOption>

              <FallbackOption
                active={fallback.mode === 'default_member'}
                title="转给默认同事"
                desc="总有一个兜底的人接手，适合成员里天然有个「什么都问」的角色。"
                onSelect={() => {
                  setFallback((prev) => ({
                    ...prev,
                    mode: 'default_member',
                    defaultMemberAgentId:
                      prev.defaultMemberAgentId || members[0]?.agentId || null,
                  }));
                  setTrialResult(null);
                  setHighlightFallback(false);
                }}
              >
                <select
                  value={fallback.defaultMemberAgentId || ''}
                  onChange={(event) =>
                    setFallback((prev) => ({
                      ...prev,
                      defaultMemberAgentId: event.target.value || null,
                    }))
                  }
                  className="h-8 w-full rounded-[8px] border border-[#e3e7f1] bg-white px-[10px] text-[12px] text-[#18181a] outline-none focus:border-[#cbd3e6]"
                >
                  <option value="">请选择默认同事</option>
                  {members.map((item) => (
                    <option key={item.agentId} value={item.agentId}>
                      {item.name}
                      {item.roleName ? `（${item.roleName}）` : ''}
                    </option>
                  ))}
                </select>
              </FallbackOption>
            </div>

            {/* 试跑 */}
            <div className="mt-[14px] rounded-[12px] border border-black/[0.06] bg-[#fbfcfd] px-[14px] py-[12px]">
              <div className="flex items-center gap-[8px] text-[12px] font-medium text-[#5b6274]">
                试跑规则
                <span className="rounded-[5px] bg-white px-[6px] py-[1px] text-[10.5px] font-normal text-[#9aa1b1]">
                  配置完马上验，不用等人来问
                </span>
              </div>
              <div className="mt-[8px] flex gap-[8px]">
                <Input
                  value={trialQuery}
                  onChange={(event) => setTrialQuery(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') {
                      event.preventDefault();
                      void runTrial();
                    }
                  }}
                  placeholder="输入一句用户可能会问的话，例如：我请了三天病假工资怎么算"
                  className="text-[12.5px]"
                />
                <UIButton
                  type="button"
                  disabled={trialLoading}
                  onClick={() => void runTrial()}
                  className="h-9 shrink-0 rounded-[10px] bg-[#18181a] px-[16px] text-[12.5px] font-normal text-white hover:bg-[#303030]"
                >
                  {trialLoading ? '试跑中…' : '试一下'}
                </UIButton>
              </div>

              {trialResult && (
                <div
                  className={cn(
                    'mt-[8px] flex items-start gap-[9px] rounded-[10px] border px-[11px] py-[9px]',
                    trialResult.kind === 'route'
                      ? 'border-[#cfe6e1] bg-[#f2faf8]'
                      : 'border-[#f5d5a8] bg-[#fdf6ec]',
                  )}
                >
                  <span
                    className={cn(
                      'mt-[5px] size-[7px] shrink-0 rounded-full',
                      trialResult.kind === 'route' ? 'bg-[#0f7268]' : 'bg-[#b45309]',
                    )}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="text-[12.5px] font-medium text-[#18181a]">
                      {trialResult.kind === 'route'
                        ? `转给 ${trialResult.name}`
                        : trialResult.blocked
                          ? trialResult.label
                          : `没有匹配的同事 → ${trialResult.label}`}
                    </div>
                    <div className="mt-[2px] text-[11.5px] leading-[1.6] text-[#757f9c]">
                      {trialResult.reason}
                    </div>
                    {trialResult.kind === 'route' && trialResult.query && (
                      <div className="mt-[3px] text-[11px] leading-[1.6] text-[#9aa1b1]">
                        转给同事的问题：{trialResult.query}
                      </div>
                    )}
                  </div>
                  {trialResult.kind === 'route' && (
                    <span className="shrink-0 rounded-[5px] bg-white px-[6px] py-[2px] text-[11px] font-medium text-[#0f7268]">
                      {Math.round(trialResult.confidence * 100)}%
                    </span>
                  )}
                </div>
              )}
            </div>
          </Section>
        </div>

        <div className="flex shrink-0 items-center justify-between gap-[12px] border-t border-black/[0.05] px-[24px] py-[14px]">
          <span className="text-[11.5px] text-[#9aa1b1]">
            创建后可在「员工广场」被成员选用，也可生成分享链接对外。
          </span>
          <div className="flex shrink-0 items-center gap-[8px]">
            <UIButton
              type="button"
              variant="outline"
              onClick={onClose}
              className="h-[34px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[16px] text-[12.5px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]"
            >
              取消
            </UIButton>
            <UIButton
              type="button"
              disabled={saving}
              onClick={() => void save()}
              className="h-[34px] rounded-[10px] bg-[#18181a] px-[18px] text-[12.5px] font-normal text-white hover:bg-[#303030]"
            >
              {saving ? '保存中…' : dispatcher ? '保存' : '新建小队'}
            </UIButton>
          </div>
        </div>

        {/* 成员选择器。**必须是 DialogContent 的子节点**：Radix 会把 Content 之外的
            任何节点标成 pointer-events:none（模态语义），那样鼠标和键盘都点不到。
            用 absolute 而不是 fixed —— 祖先若有 backdrop-blur，fixed 会相对它定位。 */}
        {pickerOpen && (
          <div
            className="absolute inset-0 z-[5] flex items-center justify-center bg-black/25 p-[24px]"
            onClick={() => setPickerOpen(false)}
          >
            <div
              className="flex max-h-[70vh] w-[440px] max-w-full flex-col overflow-hidden rounded-[14px] bg-white shadow-[0_20px_60px_rgba(17,17,17,0.18)]"
              onClick={(event) => event.stopPropagation()}
            >
              <div className="shrink-0 px-[18px] pt-[16px] pb-[10px]">
                <h3 className="text-[14px] font-semibold text-[#18181a]">
                  选择要挂到这个入口的同事
                </h3>
                <p className="mt-[3px] text-[11.5px] text-[#9aa1b1]">
                  只有已上线、且你可见的数字员工才在列表里
                </p>
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto px-[10px] pb-[12px]">
                {selectableAgents.length === 0 ? (
                  <div className="px-[10px] py-[24px] text-center text-[12px] text-[#9aa1b1]">
                    没有可添加的同事，请先创建并上线数字员工
                  </div>
                ) : (
                  selectableAgents.map((agent) => {
                    const used = selectedAgentIds.has(agent.id);
                    const profile = employeeProfile(agent);
                    return (
                      <button
                        key={agent.id}
                        type="button"
                        disabled={used}
                        onClick={() => addMember(agent)}
                        className={cn(
                          'flex w-full items-center gap-[10px] rounded-[10px] px-[10px] py-[9px] text-left transition-colors',
                          used ? 'cursor-not-allowed opacity-50' : 'hover:bg-[#f6f7f9]',
                        )}
                      >
                        <EmployeeAvatar agent={agent} size={30} />
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-[12.5px] font-medium text-[#18181a]">
                            {employeeDisplayName(agent)}
                            {profile.roleName && (
                              <span className="font-normal text-[#9aa1b1]">
                                {' '}
                                · {profile.roleName}
                              </span>
                            )}
                          </span>
                          {agent.description && (
                            <span className="mt-[1px] block truncate text-[11px] text-[#9aa1b1]">
                              {agent.description}
                            </span>
                          )}
                        </span>
                        {used && <span className="shrink-0 text-[11px] text-[#9aa1b1]">已添加</span>}
                      </button>
                    );
                  })
                )}
              </div>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

/* ────────────────────────── 子组件 ────────────────────────── */

const Section = ({
  index,
  title,
  extra,
  children,
  ref,
}: {
  index: number;
  title: string;
  extra?: React.ReactNode;
  children: React.ReactNode;
  ref?: React.Ref<HTMLDivElement>;
}) => (
  <div ref={ref} className="mt-[18px] border-t border-black/[0.05] pt-[16px]">
    <div className="mb-[8px] flex items-center gap-[8px]">
      <span className="grid size-[20px] shrink-0 place-items-center rounded-[6px] bg-[#18181a] text-[11px] font-semibold text-white">
        {index}
      </span>
      <span className="text-[13px] font-semibold text-[#18181a]">{title}</span>
      {extra}
    </div>
    {children}
  </div>
);

const Field = ({
  label,
  required,
  hint,
  children,
}: {
  label: string;
  required?: boolean;
  hint?: string;
  children: React.ReactNode;
}) => (
  <div className="flex flex-col gap-[5px]">
    <label className="flex items-baseline gap-[4px] text-[11.5px] text-[#5b6274]">
      {label}
      {required && <span className="text-[#dc2626]">*</span>}
      {hint && <span className="text-[11px] font-normal text-[#9aa1b1]">{hint}</span>}
    </label>
    {children}
  </div>
);

const IconAction = ({
  label,
  disabled,
  onClick,
  children,
}: {
  label: string;
  disabled?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) => (
  <button
    type="button"
    title={label}
    aria-label={label}
    disabled={disabled}
    onClick={onClick}
    className={cn(
      'grid size-[24px] place-items-center rounded-[6px] transition-colors',
      disabled
        ? 'cursor-not-allowed text-[#d6dae4]'
        : 'text-[#9aa1b1] hover:bg-[#f0f1f4] hover:text-[#18181a]',
    )}
  >
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      {children}
    </svg>
  </button>
);

const FallbackOption = ({
  active,
  title,
  desc,
  onSelect,
  children,
}: {
  active: boolean;
  title: string;
  desc: string;
  onSelect: () => void;
  children?: React.ReactNode;
}) => (
  <div
    role="radio"
    aria-checked={active}
    tabIndex={0}
    onClick={onSelect}
    onKeyDown={(event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        onSelect();
      }
    }}
    className={cn(
      'cursor-pointer rounded-[11px] border px-[12px] py-[10px] transition-colors',
      active
        ? 'border-[#0f7268] bg-[#f7fbfa]'
        : 'border-black/[0.07] bg-white hover:border-[#cbd3e6]',
    )}
  >
    <div className="flex items-start gap-[9px]">
      <span
        className={cn(
          'mt-[3px] grid size-[14px] shrink-0 place-items-center rounded-full border',
          active ? 'border-[#0f7268]' : 'border-[#c3c9d6]',
        )}
      >
        {active && <span className="size-[7px] rounded-full bg-[#0f7268]" />}
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[12.5px] font-medium text-[#18181a]">{title}</div>
        <div className="mt-[2px] text-[11.5px] leading-[1.6] text-[#9aa1b1]">{desc}</div>
        {active && children && (
          <div className="mt-[8px]" onClick={(event) => event.stopPropagation()}>
            {children}
          </div>
        )}
      </div>
    </div>
  </div>
);

const ExampleChips = ({
  examples,
  onChange,
}: {
  examples: string[];
  onChange: (next: string[]) => void;
}) => {
  const [draft, setDraft] = useState('');

  function commit() {
    const value = draft.trim();
    if (!value) return;
    if (examples.length >= 6) {
      notify.error('最多 6 条示例问法');
      return;
    }
    onChange([...examples, value]);
    setDraft('');
  }

  return (
    <div className="flex flex-wrap items-center gap-[5px] rounded-[9px] border border-[#e3e7f1] bg-white px-[8px] py-[6px]">
      {examples.map((item, index) => (
        <span
          key={`${item}-${index}`}
          className="inline-flex items-center gap-[4px] rounded-[6px] bg-[#f0f1f4] px-[7px] py-[3px] text-[11.5px] text-[#5b6274]"
        >
          {item}
          <button
            type="button"
            aria-label={`删除示例 ${item}`}
            onClick={() => onChange(examples.filter((_value, idx) => idx !== index))}
            className="text-[#9aa1b1] hover:text-[#dc2626]"
          >
            ✕
          </button>
        </span>
      ))}
      <input
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            event.preventDefault();
            commit();
          }
        }}
        onBlur={commit}
        placeholder="+ 输入后回车"
        className="min-w-[110px] flex-1 border-0 bg-transparent text-[11.5px] text-[#18181a] outline-none placeholder:text-[#c3c9d6]"
      />
    </div>
  );
};