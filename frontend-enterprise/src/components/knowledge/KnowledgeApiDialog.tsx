import { Check, Code2, Copy, KeyRound } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';

import { fetchKnowledgeApiAccess, type KnowledgeApiAccess } from '@/api/knowledge';
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  notify,
} from '@/components/ui';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { copyTextToClipboard } from '@/lib/clipboard';

import { buildKnowledgeApiSamples } from './knowledgeApiSamples';

// 接口未就绪时的兜底值；后端 /api/enterprise/knowledge-bases/api-access 给的是权威值
const OPEN_API_SCOPE = 'knowledge:read';
const OPEN_API_PATH = '/api/v1/knowledge-bases/{knowledge_base_id}:search';

type SampleLanguage = 'curl' | 'python' | 'node';

const LANGUAGES: { id: SampleLanguage; label: string }[] = [
  { id: 'curl', label: 'cURL' },
  { id: 'python', label: 'Python' },
  { id: 'node', label: 'Node.js' },
];

/**
 * 个人知识库的「API 接入」弹窗：说明 + 三种语言的检索示例。
 *
 * 与 OCR 助手 / 决策助手的接入弹窗同一套结构，差别在于这里是
 * `POST /v1/knowledge-bases/{knowledge_base_id}:search`：外部系统拿自己的知识库
 * 当问答 / 改文档的依据，回包里的 `citations` 就是依据片段。
 */
export default function KnowledgeApiDialog({
  open,
  onClose,
  knowledgeBaseId,
  knowledgeBaseName,
}: {
  open: boolean;
  onClose: () => void;
  /** 当前选中的个人知识库；为空时示例里保留 `{knowledge_base_id}` 占位符。 */
  knowledgeBaseId?: string;
  knowledgeBaseName?: string;
}) {
  const [access, setAccess] = useState<KnowledgeApiAccess | null>(null);
  const [accessFailed, setAccessFailed] = useState(false);
  const [language, setLanguage] = useState<SampleLanguage>('curl');
  const [copied, setCopied] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState(false);

  useEffect(() => {
    if (!open || access || accessFailed) return;
    let active = true;
    fetchKnowledgeApiAccess()
      .then((value) => {
        if (active) setAccess(value);
      })
      .catch(() => {
        if (active) setAccessFailed(true);
      });
    return () => {
      active = false;
    };
  }, [open, access, accessFailed]);

  useEffect(() => {
    if (open) {
      setCopied(null);
      setCopiedId(false);
    }
  }, [open]);

  const baseUrl = (
    access?.public_base_url?.trim()
    || (typeof window === 'undefined' ? '' : window.location.origin)
  ).replace(/\/+$/, '');
  const requiredScope = access?.required_scope || OPEN_API_SCOPE;
  const endpointPath = (access?.endpoint_path || OPEN_API_PATH).replace(
    '{knowledge_base_id}',
    knowledgeBaseId || '{knowledge_base_id}',
  );
  const endpointUrl = `${baseUrl}${endpointPath}`;

  const samples = useMemo(() => buildKnowledgeApiSamples(endpointUrl), [endpointUrl]);
  const code = samples[language];

  async function copySample() {
    try {
      await copyTextToClipboard(code);
      setCopied(language);
      notify.success('已复制调用示例');
    } catch {
      setCopied(null);
      notify.error('复制失败，请手动选中代码复制');
    }
  }

  /** 单独复制 knowledge_base_id：外部系统只需要存这个，不必去读示例代码。 */
  async function copyKnowledgeBaseId() {
    if (!knowledgeBaseId) return;
    try {
      await copyTextToClipboard(knowledgeBaseId);
      setCopiedId(true);
      notify.success('已复制知识库 ID');
    } catch {
      setCopiedId(false);
      notify.error('复制失败，请手动选中复制');
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent
        aria-describedby="knowledge-api-description"
        className="flex max-h-[calc(100dvh-3rem)] w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-[18px] border-0 bg-[#f7f8fa] p-0 shadow-[0_28px_80px_rgba(24,31,46,0.20)] sm:max-w-[820px]"
      >
        <DialogHeader className="border-b border-[#e9ecf2] bg-white px-[26px] py-[22px]">
          <div className="flex items-center gap-[12px]">
            <span className="grid size-[38px] place-items-center rounded-[12px] bg-[#18181a] text-white">
              <Code2 className="size-[18px]" />
            </span>
            <div>
              <DialogTitle className="text-[16px] font-semibold text-[#18181a]">API 接入 · 我的知识库</DialogTitle>
              <DialogDescription id="knowledge-api-description" className="mt-[5px] text-[12px] text-[#757f9c]">
                用账号 API 密钥直接检索这个知识库，把回包里的引用片段当作问答 / 改文档的依据。
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="min-h-0 flex-1 space-y-[18px] overflow-y-auto px-[26px] py-[22px]">
          <section className="rounded-[16px] border border-[#cfe7dc] bg-[#eef8f3] p-[18px]">
            <div className="flex items-center gap-[8px] text-[13px] font-semibold text-[#18181a]">
              <KeyRound className="size-[15px] text-[#207451]" />
              第一步 · 拿到账号 API 全量密钥
            </div>
            <ol className="mt-[10px] grid gap-[6px] text-[12px] leading-[19px] text-[#464c5e]">
              <li>1. 点击右上角头像，选择「API 全量密钥」。</li>
              <li>2. 创建密钥并复制明文（仅显示一次，页面关掉就看不到）。</li>
              <li>
                3. 账号级密钥默认已包含{' '}
                <code className="rounded-[4px] bg-white/70 px-[4px] font-mono text-[11px]">{requiredScope}</code>{' '}
                权限，无需额外授权。
              </li>
            </ol>
            <p className="mt-[10px] text-[11px] leading-[17px] text-[#7f897f]">
              个人知识库只对「账号级」密钥开放：员工级密钥绑定单个数字员工，会返回 403 ACCOUNT_KEY_REQUIRED。
              密钥等同于你的账号身份，不要写进前端代码或提交到仓库。
            </p>
          </section>

          <section className="rounded-[16px] border-[0.5px] border-[#e3e7f1] bg-white p-[18px]">
            <div className="text-[13px] font-semibold text-[#18181a]">第二步 · 调用端点</div>
            <div className="mt-[10px] flex flex-wrap items-center gap-[8px]">
              <span className="rounded-[6px] bg-[#18181a] px-[8px] py-[3px] font-mono text-[10px] font-semibold text-white">
                POST
              </span>
              <code className="min-w-0 flex-1 truncate rounded-[8px] bg-[#f4f5f8] px-[10px] py-[6px] font-mono text-[11.5px] text-[#18181a]">
                {endpointUrl}
              </code>
            </div>
            <dl className="mt-[12px] grid gap-[8px] text-[12px] sm:grid-cols-[84px_minmax(0,1fr)]">
              <dt className="text-[#757f9c]">知识库</dt>
              <dd className="text-[#464c5e]">
                {knowledgeBaseId ? (
                  <div className="flex flex-wrap items-center gap-[6px]">
                    <span>{knowledgeBaseName || '未命名'}</span>
                    <code
                      data-testid="knowledge-api-kb-id"
                      className="rounded-[4px] bg-[#f4f5f8] px-[6px] py-[2px] font-mono text-[11px] text-[#464c5e]"
                    >
                      {knowledgeBaseId}
                    </code>
                    <Button
                      type="button"
                      onClick={() => void copyKnowledgeBaseId()}
                      className="h-[22px] gap-[4px] rounded-[6px] bg-[#f4f5f8] px-[8px] text-[11px] text-[#464c5e] hover:bg-[#eceef3]"
                    >
                      {copiedId ? <Check className="size-[11px]" /> : <Copy className="size-[11px]" />}
                      {copiedId ? '已复制' : '复制 ID'}
                    </Button>
                  </div>
                ) : (
                  <span className="text-[#b4690e]">还没有个人知识库，先新建一个再复制示例</span>
                )}
                <p className="mt-[6px] text-[11px] leading-[17px] text-[#858b9c]">
                  这个 ID 就是外部系统要长期保存的引用；它在「我的知识库」列表行上也能直接复制。
                  知识库只能下线、不能删除，所以 ID 不会失效。
                </p>
              </dd>
              <dt className="text-[#757f9c]">请求头</dt>
              <dd>
                <code className="font-mono text-[11.5px] text-[#464c5e]">Authorization: Bearer sd_live_…</code>
              </dd>
              <dt className="text-[#757f9c]">内容类型</dt>
              <dd>
                <code className="font-mono text-[11.5px] text-[#464c5e]">application/json</code>
              </dd>
              <dt className="text-[#757f9c]">所需权限</dt>
              <dd>
                <code className="font-mono text-[11.5px] text-[#464c5e]">{requiredScope}</code>
              </dd>
              <dt className="text-[#757f9c]">请求体</dt>
              <dd className="text-[#464c5e]">
                <code className="font-mono text-[11.5px]">query</code> 必填，其余字段见下方「接口说明」。
              </dd>
            </dl>
          </section>

          <section className="rounded-[16px] border-[0.5px] border-[#e3e7f1] bg-white p-[18px]">
            <div className="flex flex-wrap items-center justify-between gap-[10px]">
              <span className="text-[13px] font-semibold text-[#18181a]">第三步 · 复制示例代码</span>
              <div className="flex items-center gap-[6px]">
                <Button
                  type="button"
                  onClick={() => void copySample()}
                  className="h-[26px] gap-[5px] rounded-[8px] bg-[#1d2027] px-[10px] text-[11px] text-white hover:bg-[#2b2f38]"
                >
                  {copied === language ? <Check className="size-[12px]" /> : <Copy className="size-[12px]" />}
                  {copied === language ? '已复制' : '复制'}
                </Button>
              </div>
            </div>

            <Tabs value={language} onValueChange={(value) => setLanguage(value as SampleLanguage)} className="mt-[12px] gap-0">
              <TabsList variant="line" className="h-auto w-full justify-start gap-[4px] rounded-none p-0">
                {LANGUAGES.map((item) => (
                  <TabsTrigger
                    key={item.id}
                    value={item.id}
                    className="h-[28px] flex-none rounded-[8px] px-[12px] text-[12px] data-active:bg-[#f4f5f8] data-active:text-[#18181a]"
                  >
                    {item.label}
                  </TabsTrigger>
                ))}
              </TabsList>

              {LANGUAGES.map((item) => (
                <TabsContent key={item.id} value={item.id} className="mt-[10px]">
                  <pre
                    data-testid={`knowledge-api-sample-${item.id}`}
                    className="max-h-[340px] overflow-auto rounded-[14px] bg-[#1d2027] p-[14px] font-mono text-[11.5px] leading-[18px] text-[#e7ebf3]"
                  >
                    {samples[item.id]}
                  </pre>
                </TabsContent>
              ))}
            </Tabs>
          </section>

          <section className="rounded-[16px] border-[0.5px] border-[#e3e7f1] bg-white p-[18px]">
            <div className="text-[13px] font-semibold text-[#18181a]">接口说明</div>
            <div className="mt-[10px] grid gap-[8px] text-[12px] leading-[18px] text-[#464c5e]">
              <p>
                <code className="font-mono text-[11.5px] text-[#18181a]">query</code>
                ：要检索的问题（必填）。
                <code className="font-mono text-[11.5px]">mode</code>
                ：<code className="font-mono text-[11.5px]">chat</code>（默认）/
                <code className="font-mono text-[11.5px]">skill_discovery</code> /
                <code className="font-mono text-[11.5px]">debug</code>（调试，带完整 trace）。
              </p>
              <p>
                <code className="font-mono text-[11.5px] text-[#18181a]">max_depth</code> /
                <code className="font-mono text-[11.5px]">max_buckets</code> /
                <code className="font-mono text-[11.5px]">max_chunks</code> /
                <code className="font-mono text-[11.5px]">budget_tokens</code>
                ：检索深度与预算，一般不用传；
                <code className="font-mono text-[11.5px]">need_evidence_pack</code> 默认
                <code className="font-mono text-[11.5px]">true</code>，回包带原文片段。
              </p>
              <p>
                回包：<code className="font-mono text-[11.5px]">citations</code>（依据片段，按相关度排序）、
                <code className="font-mono text-[11.5px]">selected_buckets</code>、
                <code className="font-mono text-[11.5px]">knowledge_base_id</code> /
                <code className="font-mono text-[11.5px]">knowledge_base_version</code>
                （版本变化说明知识库有更新，可按它做缓存失效）。
              </p>
              <p className="text-[11.5px] text-[#757f9c]">
                个人知识库只属于你：非属主访问返回 404。知识库只能下线不能删除，下线后检索返回
                409 KNOWLEDGE_BASE_ARCHIVED（<code className="font-mono text-[11px]">knowledge_base_id</code>
                仍然有效，可据此提示用户）。
              </p>
              <p className="text-[11.5px] text-[#757f9c]">
                这个接口只做检索（只读）。要补充内容，直接在「我的数字员工 → 知识库」上传文档，或把整库导入到某个数字员工。
              </p>
            </div>
          </section>
        </div>
      </DialogContent>
    </Dialog>
  );
}