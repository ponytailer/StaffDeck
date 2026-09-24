import { useCallback, useEffect, useState } from 'react';
import {
  Braces,
  Check,
  Eye,
  EyeOff,
  KeyRound,
  LoaderCircle,
  Plus,
  RotateCcw,
  Settings2,
  Star,
  Trash2,
} from 'lucide-react';

import {
  createAiReviewPreset,
  deleteAiReviewPreset,
  deleteAiReviewRuleFile,
  fetchAiReviewCredentials,
  fetchAiReviewPresets,
  fetchAiReviewRuleFile,
  saveAiReviewCredential,
  saveAiReviewRuleFile,
  updateAiReviewPreset,
  type AiReviewCredentialSummary,
  type AiReviewPlatform,
  type AiReviewPreset,
  type AiReviewRuleFileState,
} from '../../api/aiReview';
import { ApiError } from '../../api/client';
import { Button, Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, Input, notify, Textarea } from '@/components/ui';
import { cn } from '@/lib/utils';
import { ruleFileSummary, sampleRuleFileText, validateRuleFileText } from './aiReviewModel';

const PLATFORMS: { id: AiReviewPlatform; label: string }[] = [
  { id: 'github', label: 'GitHub' },
  { id: 'gitlab', label: 'GitLab' },
];

function CredentialForm({ platform }: { platform: AiReviewPlatform }) {
  const [credential, setCredential] = useState<AiReviewCredentialSummary | null>(null);
  const [token, setToken] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [showToken, setShowToken] = useState(false);
  const [saving, setSaving] = useState(false);

  const reload = useCallback(async () => {
    try {
      const rows = await fetchAiReviewCredentials();
      const row = rows.find((item) => item.platform === platform) ?? null;
      setCredential(row);
      setBaseUrl(row?.base_url ?? '');
      setToken('');
      setShowToken(false);
    } catch (cause) {
      notify.error(cause instanceof ApiError || cause instanceof Error ? cause.message : '加载凭证失败');
    }
  }, [platform]);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function save() {
    setSaving(true);
    try {
      const updated = await saveAiReviewCredential(platform, token.trim(), baseUrl.trim());
      setCredential(updated);
      setToken('');
      notify.success(updated.token_set ? `已保存 ${platform} access token` : `已清除 ${platform} access token`);
    } catch (cause) {
      notify.error(cause instanceof ApiError || cause instanceof Error ? cause.message : '保存失败');
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-[10px] rounded-[14px] border-[0.5px] border-[#e3e7f1] bg-white px-[14px] py-[12px]">
      <div className="flex items-center justify-between gap-[10px]">
        <span className="inline-flex items-center gap-[6px] text-[13px] font-semibold text-[#18181a]">
          <KeyRound className="size-[13px] text-[#5b6273]" />
          {platform === 'github' ? 'GitHub' : 'GitLab'} Access Token
        </span>
        {credential?.token_set && (
          <span className="rounded-[6px] bg-[#e9f7ef] px-[7px] py-[2px] text-[11px] text-[#1a7f4b]">
            已配置 ·•••{credential.token_last4}
          </span>
        )}
      </div>
      <p className="text-[11.5px] leading-[17px] text-[#757f9c]">
        {platform === 'github'
          ? 'GitHub PAT（repo / pulls 读权限即可），用于拉取 open PR 列表与克隆仓库。'
          : 'GitLab PAT（api 读权限）；自托管实例请在下一栏填实例根地址。'}
      </p>
      <div className="flex items-center gap-[8px]">
        <Input
          type={showToken ? 'text' : 'password'}
          value={token}
          placeholder={credential?.token_set ? '粘贴新 token 覆盖；留空并保存则清除' : '粘贴 access token（保存后仅显示掩码）'}
          onChange={(event) => setToken(event.target.value)}
          className="h-[34px] flex-1 font-mono text-[12px]"
        />
        <button
          type="button"
          aria-label={showToken ? '隐藏 token' : '显示 token'}
          onClick={() => setShowToken((value) => !value)}
          className="grid size-[34px] shrink-0 place-items-center rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white text-[#757f9c] transition-colors hover:bg-[#f6f6f6] hover:text-[#18181a]"
        >
          {showToken ? <EyeOff className="size-[14px]" /> : <Eye className="size-[14px]" />}
        </button>
      </div>
      {platform === 'gitlab' && (
        <div className="flex flex-col gap-[6px]">
          <label className="text-[12px] font-medium text-[#464c5e]">GitLab 实例根地址（gitlab.com 可留空）</label>
          <Input
            value={baseUrl}
            placeholder="https://gitlab.example.com"
            onChange={(event) => setBaseUrl(event.target.value)}
            className="h-[34px] font-mono text-[12px]"
          />
        </div>
      )}
      <div className="flex justify-end">
        <Button
          type="button"
          disabled={saving || (!token.trim() && !credential?.token_set && platform === 'github')}
          onClick={() => void save()}
          className="h-[30px] gap-[6px] rounded-[8px] bg-[#18181a] px-[14px] text-[12px] text-white hover:bg-[#2b2b2e] disabled:opacity-50"
        >
          {saving && <LoaderCircle className="size-[12px] animate-spin" />}
          保存
        </Button>
      </div>
    </div>
  );
}

function PresetSection() {
  const [presets, setPresets] = useState<AiReviewPreset[]>([]);
  const [name, setName] = useState('');
  const [content, setContent] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setPresets(await fetchAiReviewPresets());
    } catch (cause) {
      notify.error(cause instanceof ApiError || cause instanceof Error ? cause.message : '加载预设失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function add() {
    if (!name.trim() || !content.trim()) {
      notify.error('请填写预设名称与内容');
      return;
    }
    setSubmitting(true);
    try {
      await createAiReviewPreset({ name: name.trim(), content: content.trim(), is_default: false });
      setName('');
      setContent('');
      notify.success('预设已创建');
      await reload();
    } catch (cause) {
      notify.error(cause instanceof ApiError || cause instanceof Error ? cause.message : '创建失败');
    } finally {
      setSubmitting(false);
    }
  }

  async function toggleDefault(preset: AiReviewPreset) {
    try {
      await updateAiReviewPreset(preset.id, { is_default: !preset.is_default });
      await reload();
    } catch (cause) {
      notify.error(cause instanceof ApiError || cause instanceof Error ? cause.message : '更新失败');
    }
  }

  async function remove(preset: AiReviewPreset) {
    try {
      await deleteAiReviewPreset(preset.id);
      notify.success('预设已删除');
      await reload();
    } catch (cause) {
      notify.error(cause instanceof ApiError || cause instanceof Error ? cause.message : '删除失败');
    }
  }

  return (
    <div className="flex flex-col gap-[10px] rounded-[14px] border-[0.5px] border-[#e3e7f1] bg-white px-[14px] py-[12px]">
      <div className="flex items-center justify-between gap-[10px]">
        <span className="inline-flex items-center gap-[6px] text-[13px] font-semibold text-[#18181a]">
          <Star className="size-[13px] text-[#8a4b00]" />
          全局评审要求预设
        </span>
        <button
          type="button"
          aria-label="刷新预设列表"
          onClick={() => void reload()}
          className="grid size-[26px] place-items-center rounded-[7px] text-[#a3aaba] transition-colors hover:bg-[#f6f6f6] hover:text-[#18181a]"
        >
          <RotateCcw className="size-[12px]" />
        </button>
      </div>
      <p className="text-[11.5px] leading-[17px] text-[#757f9c]">
        全租户公共的评审口径。提交评审任务时可勾选叠加；标星的那条会在发起评审时默认勾上。
      </p>

      {loading ? (
        <div className="flex items-center gap-[8px] py-[10px] text-[12px] text-[#a3aaba]">
          <LoaderCircle className="size-[13px] animate-spin" />
          正在加载…
        </div>
      ) : presets.length > 0 ? (
        <div className="flex flex-col gap-[6px]">
          {presets.map((preset) => (
            <div key={preset.id} className="flex items-start gap-[8px] rounded-[10px] bg-[#fafbfd] px-[10px] py-[8px]">
              <button
                type="button"
                aria-label={preset.is_default ? '取消默认' : '设为默认'}
                title={preset.is_default ? '取消默认' : '设为默认（发起评审时默认勾选）'}
                onClick={() => void toggleDefault(preset)}
                className={cn(
                  'mt-[1px] grid size-[18px] shrink-0 place-items-center rounded-[5px] transition-colors',
                  preset.is_default ? 'bg-[#8a4b00] text-white' : 'border-[0.5px] border-[#d8dbe3] bg-white text-transparent hover:text-[#8a4b00]',
                )}
              >
                <Star className={cn('size-[10px]', preset.is_default ? 'fill-current' : '')} />
              </button>
              <div className="min-w-0 flex-1">
                <div className="truncate text-[12.5px] font-medium text-[#18181a]">{preset.name}</div>
                <p className="mt-[2px] line-clamp-2 text-[11px] leading-[16px] text-[#757f9c]">{preset.content}</p>
              </div>
              <button
                type="button"
                aria-label="删除该预设"
                onClick={() => void remove(preset)}
                className="mt-[1px] grid size-[24px] shrink-0 place-items-center rounded-[7px] text-[#a3aaba] transition-colors hover:bg-[#fce7e7] hover:text-[#c0392b]"
              >
                <Trash2 className="size-[12px]" />
              </button>
            </div>
          ))}
        </div>
      ) : (
        <div className="rounded-[10px] border-[0.5px] border-dashed border-[#e3e7f1] bg-[#fafbfd] px-[10px] py-[12px] text-[11.5px] leading-[17px] text-[#757f9c]">
          还没有预设。评审要求只对单次任务有效，公共口径建议沉淀成预设。
        </div>
      )}

      <div className="mt-[2px] flex flex-col gap-[8px] border-t-[0.5px] border-[#eef0f4] pt-[10px]">
        <Input
          value={name}
          placeholder="预设名称，例如：安全口径"
          onChange={(event) => setName(event.target.value)}
          className="h-[32px] text-[12.5px]"
        />
        <Textarea
          rows={3}
          value={content}
          placeholder="评审要求内容，例如：重点关注 SQL 注入、越权访问与异常处理缺失"
          onChange={(event) => setContent(event.target.value)}
          className="min-h-[64px] resize-y text-[12px]"
        />
        <div className="flex justify-end">
          <Button
            type="button"
            disabled={submitting}
            onClick={() => void add()}
            className="h-[30px] gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] text-[#464c5e] hover:bg-[#f6f6f6] disabled:opacity-50"
          >
            {submitting ? <LoaderCircle className="size-[12px] animate-spin" /> : <Plus className="size-[12px]" />}
            添加预设
          </Button>
        </div>
      </div>
    </div>
  );
}

function RuleFileSection() {
  const [state, setState] = useState<AiReviewRuleFileState | null>(null);
  const [name, setName] = useState('');
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);

  // 当前文本的校验结果：用于实时提示与保存门槛
  const validation = text.trim() ? validateRuleFileText(text) : null;
  const validationOk = validation === null || validation.ok;

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const next = await fetchAiReviewRuleFile();
      setState(next);
      setName(next.exists ? next.name : '');
      setText(next.exists && next.content ? JSON.stringify(next.content, null, 2) : '');
    } catch (cause) {
      notify.error(cause instanceof ApiError || cause instanceof Error ? cause.message : '加载自定义规则失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function save() {
    if (!validation || !validation.ok) {
      notify.error(validation && !validation.ok ? validation.message : '请先填写规则内容');
      return;
    }
    setBusy(true);
    try {
      const next = await saveAiReviewRuleFile(name.trim() || '自定义评审规则', text);
      setState(next);
      notify.success('自定义评审规则已保存，下一次评审生效');
      await reload();
    } catch (cause) {
      notify.error(cause instanceof ApiError || cause instanceof Error ? cause.message : '保存失败');
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    try {
      await deleteAiReviewRuleFile();
      notify.success('已清除自定义规则，回落到内置规则');
      await reload();
    } catch (cause) {
      notify.error(cause instanceof ApiError || cause instanceof Error ? cause.message : '删除失败');
    } finally {
      setBusy(false);
    }
  }

  const configured = Boolean(state?.exists);

  return (
    <div className="flex flex-col gap-[10px] rounded-[14px] border-[0.5px] border-[#e3e7f1] bg-white px-[14px] py-[12px]">
      <div className="flex items-center justify-between gap-[10px]">
        <span className="inline-flex items-center gap-[6px] text-[13px] font-semibold text-[#18181a]">
          <Braces className="size-[13px] text-[#1a71ff]" />
          自定义评审规则
        </span>
        {configured ? (
          <span className="rounded-[6px] bg-[#e8f0ff] px-[7px] py-[2px] text-[11px] text-[#1a71ff]">
            已配置 · {state?.content ? ruleFileSummary(state.content) : ''}
          </span>
        ) : (
          <span className="rounded-[6px] bg-[#f3f4f6] px-[7px] py-[2px] text-[11px] text-[#757f9c]">未配置</span>
        )}
      </div>
      <p className="text-[11.5px] leading-[17px] text-[#757f9c]">
        以 ocr 的 <code className="rounded-[4px] bg-[#f3f4f6] px-[4px] font-mono text-[10.5px]">rule.json</code> 结构生效：
        include / exclude 是 glob 过滤，rules 按 path 匹配文件并注入专属规则（匹配到即止，merge_system_rule 决定是否叠加内置规则）。
        保存后以最高优先级注入每次评审，覆盖项目内配置与系统默认。
      </p>

      {loading ? (
        <div className="flex items-center gap-[8px] py-[10px] text-[12px] text-[#a3aaba]">
          <LoaderCircle className="size-[13px] animate-spin" />
          正在加载…
        </div>
      ) : (
        <>
          <div className="flex items-center gap-[8px]">
            <Input
              value={name}
              placeholder="规则名称，留空则记为「自定义评审规则」"
              onChange={(event) => setName(event.target.value)}
              className="h-[32px] text-[12.5px]"
            />
          </div>
          <Textarea
            rows={10}
            value={text}
            placeholder={'{\n  "exclude": ["**/generated/**"],\n  "rules": [{ "path": "**/*.go", "rule": "…" }]\n}'}
            onChange={(event) => setText(event.target.value)}
            className={cn(
              'min-h-[160px] resize-y font-mono text-[11.5px] leading-[17px]',
              text.trim() && !validationOk && 'border-[#c0392b] focus-visible:ring-[#c0392b]',
            )}
          />
          <div className="min-h-[16px]">
            {text.trim() && validation && !validation.ok ? (
              <p className="text-[11px] leading-[16px] text-[#c0392b]">{validation.message}</p>
            ) : validation && validation.ok ? (
              <p className="text-[11px] leading-[16px] text-[#1a7f4b]">格式校验通过 · {ruleFileSummary(validation.config)}</p>
            ) : (
              <p className="text-[11px] leading-[16px] text-[#a3aaba]">填写 JSON 后保存；清空并删除可完全回落到内置规则。</p>
            )}
          </div>
          <div className="flex items-center justify-between gap-[10px]">
            <button
              type="button"
              onClick={() => setText(sampleRuleFileText())}
              className="rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[10px] py-[5px] text-[11.5px] text-[#464c5e] transition-colors hover:bg-[#f6f6f6]"
            >
              填入示例
            </button>
            <div className="flex items-center gap-[8px]">
              <Button
                type="button"
                disabled={busy || !configured}
                onClick={() => void remove()}
                className="h-[30px] gap-[4px] rounded-[8px] border-[0.5px] border-[#f0d4d4] bg-white px-[12px] text-[12px] text-[#c0392b] hover:bg-[#fce7e7] disabled:opacity-40"
              >
                <Trash2 className="size-[12px]" />
                清除规则
              </Button>
              <Button
                type="button"
                disabled={busy || !validation || !validation.ok}
                onClick={() => void save()}
                className="h-[30px] gap-[6px] rounded-[8px] bg-[#18181a] px-[14px] text-[12px] text-white hover:bg-[#2b2b2e] disabled:opacity-50"
              >
                {busy ? <LoaderCircle className="size-[12px] animate-spin" /> : <Check className="size-[12px]" />}
                保存规则
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

export default function AiReviewSettingsDialog({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent
        aria-describedby="ai-review-settings-description"
        className="flex max-h-[calc(100dvh-3rem)] w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-[18px] border-0 bg-[#f7f8fa] p-0 shadow-[0_28px_80px_rgba(24,31,46,0.20)] sm:max-w-[620px]"
      >
        <DialogHeader className="border-b border-[#e9ecf2] bg-white px-[24px] py-[20px]">
          <div className="flex items-center gap-[12px]">
            <span className="grid size-[38px] place-items-center rounded-[12px] bg-[#18181a] text-white">
              <Settings2 className="size-[17px]" />
            </span>
            <div>
              <DialogTitle className="text-[16px] font-semibold text-[#18181a]">平台设置 · AI Reviewer</DialogTitle>
              <DialogDescription id="ai-review-settings-description" className="mt-[5px] text-[12px] text-[#757f9c]">
                平台 token 与全局评审要求都是租户级全局配置，对所有成员生效
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="min-h-0 flex-1 space-y-[14px] overflow-y-auto px-[24px] py-[20px]">
          {PLATFORMS.map((item) => (
            <CredentialForm key={item.id} platform={item.id} />
          ))}
          <PresetSection />
          <RuleFileSection />
        </div>
      </DialogContent>
    </Dialog>
  );
}
