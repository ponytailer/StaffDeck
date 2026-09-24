import { Check, Code2, Copy, KeyRound } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';

import { fetchLayaApiAccess, type LayaApiAccess } from '@/api/laya';
import { Button, Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, notify } from '@/components/ui';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { copyTextToClipboard } from '@/lib/clipboard';

import { buildApiSamples } from './decisionApiSamples';
import { buildQuestions, sampleQuestions, validateDraft, type DecisionQuestionDraft } from './decisionForm';

// 接口未就绪时的兜底值；后端 /api/enterprise/laya/api-access 给的是权威值
const OPEN_API_SCOPE = 'decisions:run';
const OPEN_API_PATH = '/api/v1/decisions/predict';

type SampleSource = 'form' | 'sample';
type SampleLanguage = 'curl' | 'python' | 'node';

const LANGUAGES: { id: SampleLanguage; label: string }[] = [
  { id: 'curl', label: 'cURL' },
  { id: 'python', label: 'Python' },
  { id: 'node', label: 'Node.js' },
];

/**
 * 「API 接入」弹窗：可随时关闭、不占页面空间。
 * 说明与示例都在这里，示例代码由 `decisionApiSamples.ts` 生成。
 */
export default function DecisionApiDialog({
  open,
  onClose,
  background,
  questions,
}: {
  open: boolean;
  onClose: () => void;
  background: string;
  questions: DecisionQuestionDraft[];
}) {
  const formReady = useMemo(() => validateDraft(background, questions) === null, [background, questions]);

  const [access, setAccess] = useState<LayaApiAccess | null>(null);
  const [accessFailed, setAccessFailed] = useState(false);
  const [source, setSource] = useState<SampleSource>('sample');
  const [language, setLanguage] = useState<SampleLanguage>('curl');
  const [copied, setCopied] = useState<string | null>(null);

  // 打开时取一次服务端配置（对外基址来自 .env 的 PUBLIC_BASE_URL）
  useEffect(() => {
    if (!open || access || accessFailed) return;
    let active = true;
    fetchLayaApiAccess()
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

  const baseUrl = (access?.public_base_url?.trim() || (typeof window === 'undefined' ? '' : window.location.origin))
    .replace(/\/+$/, '');
  const endpointUrl = `${baseUrl}${access?.endpoint_path || OPEN_API_PATH}`;
  const requiredScope = access?.required_scope || OPEN_API_SCOPE;

  // 每次打开都按「表单是否填完整」重置示例来源，避免上次的选择留下歧义
  useEffect(() => {
    if (!open) return;
    setSource(formReady ? 'form' : 'sample');
    setCopied(null);
  }, [open, formReady]);

  const payload = useMemo(() => {
    if (source === 'form' && formReady) {
      return { state: { background: background.trim() }, questions: buildQuestions(questions) };
    }
    const sample = sampleQuestions();
    return { state: { background: sample.background }, questions: buildQuestions(sample.questions) };
  }, [source, formReady, background, questions]);

  const samples = useMemo(() => buildApiSamples(endpointUrl, payload), [endpointUrl, payload]);
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

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent
        aria-describedby="decision-api-description"
        className="flex max-h-[calc(100dvh-3rem)] w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-[18px] border-0 bg-[#f7f8fa] p-0 shadow-[0_28px_80px_rgba(24,31,46,0.20)] sm:max-w-[820px]"
      >
        <DialogHeader className="border-b border-[#e9ecf2] bg-white px-[26px] py-[22px]">
          <div className="flex items-center gap-[12px]">
            <span className="grid size-[38px] place-items-center rounded-[12px] bg-[#18181a] text-white">
              <Code2 className="size-[18px]" />
            </span>
            <div>
              <DialogTitle className="text-[16px] font-semibold text-[#18181a]">API 接入 · 决策助手</DialogTitle>
              <DialogDescription id="decision-api-description" className="mt-[5px] text-[12px] text-[#757f9c]">
                用账号 API 密钥直接调用同一个 Laya 决策头，无需打开这个页面。
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
                3. 账号级密钥默认已包含 <code className="rounded-[4px] bg-white/70 px-[4px] font-mono text-[11px]">{requiredScope}</code> 权限，无需额外授权。
              </li>
            </ol>
            <p className="mt-[10px] text-[11px] leading-[17px] text-[#7f897f]">
              密钥等同于你的账号身份，不要写进前端代码或提交到仓库；泄露后到同一个入口吊销即可。
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
              <dt className="text-[#757f9c]">请求头</dt>
              <dd>
                <code className="font-mono text-[11.5px] text-[#464c5e]">Authorization: Bearer sd_live_…</code>
              </dd>
              <dt className="text-[#757f9c]">所需权限</dt>
              <dd>
                <code className="font-mono text-[11.5px] text-[#464c5e]">{requiredScope}</code>
              </dd>
              <dt className="text-[#757f9c]">请求体</dt>
              <dd className="text-[#464c5e]">与本页表单一致：state.background + questions（key 自取，回包按同一 key 返回）。</dd>
            </dl>
          </section>

          <section className="rounded-[16px] border-[0.5px] border-[#e3e7f1] bg-white p-[18px]">
            <div className="flex flex-wrap items-center justify-between gap-[10px]">
              <span className="text-[13px] font-semibold text-[#18181a]">第三步 · 复制示例代码</span>
              <div className="flex items-center gap-[6px]">
                <span className="text-[11px] text-[#a3aaba]">请求体来源</span>
                <div className="flex items-center gap-[3px] rounded-[9px] bg-[#f4f5f8] p-[3px]">
                  <button
                    type="button"
                    disabled={!formReady}
                    title={formReady ? undefined : '当前表单未填写完整，先用官方示例演示'}
                    onClick={() => setSource('form')}
                    className={`rounded-[7px] px-[10px] py-[4px] text-[11.5px] transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${
                      source === 'form' ? 'bg-white text-[#18181a] shadow-[0_1px_2px_rgba(24,31,46,0.10)]' : 'text-[#757f9c] hover:text-[#18181a]'
                    }`}
                  >
                    当前表单
                  </button>
                  <button
                    type="button"
                    onClick={() => setSource('sample')}
                    className={`rounded-[7px] px-[10px] py-[4px] text-[11.5px] transition-colors ${
                      source === 'sample' ? 'bg-white text-[#18181a] shadow-[0_1px_2px_rgba(24,31,46,0.10)]' : 'text-[#757f9c] hover:text-[#18181a]'
                    }`}
                  >
                    官方示例
                  </button>
                </div>
              </div>
            </div>

            {!formReady && (
              <p className="mt-[8px] text-[11px] text-[#a3aaba]">当前表单未填写完整，示例先用官方工单场景；填好后可切回「当前表单」。</p>
            )}

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
                  <div className="relative">
                    <pre
                      data-testid={`decision-api-sample-${item.id}`}
                      className="max-h-[340px] overflow-auto rounded-[14px] bg-[#1d2027] p-[14px] pr-[92px] font-mono text-[11.5px] leading-[18px] text-[#e7ebf3]"
                    >
                      {samples[item.id]}
                    </pre>
                    <Button
                      type="button"
                      onClick={() => void copySample()}
                      className="absolute top-[9px] right-[9px] h-[27px] gap-[5px] rounded-[8px] bg-white/10 px-[9px] text-[11px] text-white hover:bg-white/20"
                    >
                      {copied === item.id ? <Check className="size-[12px]" /> : <Copy className="size-[12px]" />}
                      {copied === item.id ? '已复制' : '复制'}
                    </Button>
                  </div>
                </TabsContent>
              ))}
            </Tabs>
          </section>

          <section className="rounded-[16px] border-[0.5px] border-[#e3e7f1] bg-white p-[18px]">
            <div className="text-[13px] font-semibold text-[#18181a]">请求体与回包</div>
            <div className="mt-[10px] grid gap-[8px] text-[12px] leading-[18px] text-[#464c5e]">
              <p>
                <code className="font-mono text-[11.5px] text-[#18181a]">state.background</code>
                ：决策背景，模型据此推理（必填）。
              </p>
              <p>
                <code className="font-mono text-[11.5px] text-[#18181a]">questions.&lt;key&gt;</code>
                ：key 由你自取（如 <code className="font-mono text-[11.5px]">refund_requested</code>），回包按同一 key 返回。
              </p>
              <p>
                <code className="font-mono text-[11.5px] text-[#18181a]">noul</code>
                ：是非题，只需要 instructions。
              </p>
              <p>
                <code className="font-mono text-[11.5px] text-[#18181a]">choice</code>
                ：分类题，criteria 是「选项名 → 说明」对象，回包给命中选项与各选项概率。
              </p>
              <p>
                <code className="font-mono text-[11.5px] text-[#18181a]">score</code>
                ：评分题，criteria 是档位文案数组，回包给期望档位、图例与各档位概率。
              </p>
              <p className="text-[11.5px] text-[#757f9c]">
                confidence 是判定把握：noul 取 max(p, 1-p)；choice / score 取赢家概率，偏低时建议人工复核。
              </p>
            </div>
          </section>
        </div>
      </DialogContent>
    </Dialog>
  );
}
