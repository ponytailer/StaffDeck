import type { ChangeEvent, KeyboardEvent } from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { renderAsync } from 'docx-preview';
import {
  Check,
  Download,
  FileText,
  LoaderCircle,
  RotateCcw,
  ScanSearch,
  SendHorizontal,
  Upload,
  X,
} from 'lucide-react';

import AppHeader from '@/components/AppHeader';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  notify,
} from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { Switch } from '@/components/ui/switch';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { api, TENANT_ID } from '@/api/client';
import IconChevronDown from '@/assets/icons/chevron-down.svg?react';
import IconRefresh from '@/assets/icons/refresh.svg?react';
import type { EnterpriseAuthUser } from '@/auth';
import type { ModelConfigRead } from '@/types';
import type { AgentCatalogEntry } from '@/lib/agentCatalog';
import { cn } from '@/lib/utils';
import AgentAppBackButton from './AgentAppBackButton';
import { parseDocx, type DocBlock, type DocIssue } from '@/api/docReview';

export type DocReviewWorkspaceProps = {
  entry: AgentCatalogEntry;
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
};

/** 左栏「对话流」里的一条消息：一键审阅结果或一问一答。 */
type ChatEntry =
  | { kind: 'review'; id: string; issues: DocIssue[]; notes: string[] }
  | { kind: 'chat'; id: string; role: 'user' | 'assistant'; content: string };

/** 修订追踪模式下，AI 的修改先落到「待确认建议」，用户点应用才进正文。 */
type PendingSuggestion = { key: string; blockId: string; newText: string; reason: string };

type IssueStatus = 'open' | 'applied' | 'dismissed';

/**
 * AI 文档审阅工作台（参考 genoffice 的审阅交互）：
 *
 * - 左栏 = 上传/文件栏 + 对话流（问题清单以审阅消息形态插入）+
 *   待确认建议区 + 输入区（修订追踪开关 + 一键审阅 + 模型下拉 + 输入框 + 发送）；
 * - 右栏 = **docx-preview 高保真渲染**：每次 blocks 变化（应用修改/撤销/恢复原文）后，
 *   把「当前版本」交给后端 `doc:export` 管线生成 docx 字节流，再由 docx-preview 重新
 *   渲染 —— 表格、标题样式、页眉页脚全部保留，所见即最终导出的 Word。
 *
 * blocks 的唯一事实来源仍在本组件 state（与 slides 的 deck 模式一致）：AI 只产出
 * 「块 id + 新文本」；右栏是渲染快照，不承载交互 —— 定位用文本匹配做尽力而为的滚动。
 * 修订追踪 ON：AI 修改以 **Word 批注**形式附在原文旁（不改文字，导出后可在 Word 里
 * 看到作者「AI 审阅」的批注）；OFF：直接替换原文并触发右栏重渲染。
 */
export default function DocReviewWorkspace({ entry, currentUser, onLogout }: DocReviewWorkspaceProps) {
  const navigate = useNavigate();
  const [docId, setDocId] = useState('');
  const [docName, setDocName] = useState('');
  const [blocks, setBlocks] = useState<DocBlock[]>([]);
  /** 解析时的原始文本，撤销/对比的基准。 */
  const [originals, setOriginals] = useState<Record<string, string>>({});

  const [models, setModels] = useState<ModelConfigRead[]>([]);
  const [modelsLoading, setModelsLoading] = useState(true);
  const [modelId, setModelId] = useState('');

  const [chatEntries, setChatEntries] = useState<ChatEntry[]>([]);
  const [issueStatus, setIssueStatus] = useState<Record<string, IssueStatus>>({});
  const [pending, setPending] = useState<PendingSuggestion[]>([]);
  const [trackChanges, setTrackChanges] = useState(true);

  const [input, setInput] = useState('');
  const [parsing, setParsing] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const [chatting, setChatting] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState(false);
  const [changedListOpen, setChangedListOpen] = useState(false);

  const importInputRef = useRef<HTMLInputElement | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const paperRef = useRef<HTMLDivElement | null>(null);
  /** 防渲染竞态：只有最新一次请求的返回可以落进容器。 */
  const renderSeqRef = useRef(0);

  useEffect(() => {
    let alive = true;
    setModelsLoading(true);
    api
      .get<ModelConfigRead[]>(`/api/enterprise/model-configs?tenant_id=${TENANT_ID}`)
      .then((rows) => {
        if (!alive) return;
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

  /** 有改动的块：文本被替换，或挂了修订批注（修订追踪模式）。 */
  const changedIds = useMemo(
    () =>
      new Set(
        blocks.filter((block) => block.text !== originals[block.id] || Boolean(block.comment)).map((block) => block.id),
      ),
    [blocks, originals],
  );
  const changedBlocks = useMemo(() => blocks.filter((block) => changedIds.has(block.id)), [blocks, changedIds]);
  const textChangedCount = useMemo(
    () => blocks.filter((block) => block.text !== originals[block.id]).length,
    [blocks, originals],
  );
  const commentCount = useMemo(() => blocks.filter((block) => Boolean(block.comment)).length, [blocks]);
  const openIssueCount = chatEntries.reduce(
    (count, entryItem) =>
      entryItem.kind === 'review'
        ? count + entryItem.issues.filter((issue) => (issueStatus[issue.id] ?? 'open') === 'open').length
        : count,
    0,
  );

  const requireModel = useCallback(() => {
    if (!modelId) {
      notify.warning('请先在「模型配置」里配置一个属于你的模型');
      return false;
    }
    return true;
  }, [modelId]);

  // ------------------------------------------------------------------ 上传

  const handleFile = useCallback(
    async (file: File | null | undefined) => {
      if (!file) return;
      if (!file.name.toLowerCase().endsWith('.docx')) {
        notify.error('目前只支持 .docx 文件（老 .doc 请先用 Word 另存为 .docx）');
        return;
      }
      setParsing(true);
      try {
        const result = await parseDocx(TENANT_ID, file);
        setDocId(result.doc_id);
        setDocName(result.doc_name);
        setBlocks(result.blocks);
        setOriginals(Object.fromEntries(result.blocks.map((block) => [block.id, block.text])));
        setChatEntries([]);
        setIssueStatus({});
        setPending([]);
        setChangedListOpen(false);
        setPreviewError(false);
        notify.success(`已解析 ${result.blocks.length} 个段落`);
      } catch (error) {
        notify.error(error instanceof Error ? error.message : '文档解析失败');
      } finally {
        setParsing(false);
      }
    },
    [],
  );

  const onFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    // 先清空 value：同一个文件连续导入两次也要能触发 change
    const file = event.target.files?.[0];
    event.target.value = '';
    void handleFile(file);
  };

  // ------------------------------------------------------------------ 右栏渲染（docx-preview）

  useEffect(() => {
    if (!docId || blocks.length === 0) return;
    const seq = ++renderSeqRef.current;
    let cancelled = false;
    setPreviewLoading(true);
    setPreviewError(false);
    // 当前版本 → doc:export 管线（在原文档对象上回写 blocks）→ docx-preview 渲染
    api
      .postBlob('/api/enterprise/agent-apps/doc:export', {
        tenant_id: TENANT_ID,
        doc_id: docId,
        blocks,
        file_name: docName,
      })
      .then(async (blob) => {
        if (cancelled || seq !== renderSeqRef.current || !paperRef.current) return;
        const container = paperRef.current;
        container.innerHTML = '';
        await renderAsync(blob, container, undefined, {
          inWrapper: true,
          breakPages: true,
          renderHeaders: true,
          renderFooters: true,
          renderComments: true,
          useBase64URL: true,
        });
      })
      .catch((error: unknown) => {
        if (!cancelled && seq === renderSeqRef.current) {
          setPreviewError(true);
          if (error instanceof Error && error.message) notify.error(`文档渲染失败：${error.message}`);
        }
      })
      .finally(() => {
        if (!cancelled && seq === renderSeqRef.current) setPreviewLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [docId, blocks, docName]);

  // ------------------------------------------------------------------ 定位（全文拼接匹配，尽力而为）

  /**
   * 在 docx-preview 渲染结果里定位块文本并高亮。
   *
   * docx-preview 会把一段文字切成多个 run 文本节点，单节点前缀匹配会漏 —— 这里把容器内
   * 所有文本节点拼成全文再 indexOf，命中后对相交的每个节点父元素加高亮底色并滚动定位。
   * 依次尝试：当前文本（24/16/8/4 字符）→ 解析时原文（改动后可能已经对不上）。
   * 返回是否定位成功，供「逐个 block_id 尝试」的调用方判断。
   */
  const goToBlock = useCallback(
    (blockId: string): boolean => {
      const container = paperRef.current;
      const block = blocks.find((item) => item.id === blockId);
      if (!container || !block) return false;

      const texts = [block.text, originals[block.id]].filter(
        (text): text is string => Boolean(text && text.trim()),
      );
      const nodes: Array<{ node: Text; start: number; end: number }> = [];
      const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
      let current: Node | null = walker.nextNode();
      let full = '';
      while (current) {
        const textNode = current as Text;
        nodes.push({ node: textNode, start: full.length, end: full.length + textNode.data.length });
        full += textNode.data;
        current = walker.nextNode();
      }

      for (const text of texts) {
        const trimmed = text.trim();
        const snippets = [trimmed.slice(0, 24), trimmed.slice(0, 16), trimmed.slice(0, 8), trimmed.slice(0, 4)];
        for (const snippet of snippets) {
          if (!snippet) continue;
          const index = full.indexOf(snippet);
          if (index < 0) continue;

          const touched: HTMLElement[] = [];
          for (const record of nodes) {
            if (record.end <= index || record.start >= index + snippet.length) continue;
            const element = record.node.parentElement;
            if (!element) continue;
            touched.push(element);
            element.style.transition = 'background-color 0.3s';
            element.style.backgroundColor = '#ffe58f';
          }
          if (touched.length > 0) {
            touched[0].scrollIntoView?.({ behavior: 'smooth', block: 'center' });
            setTimeout(() => {
              touched.forEach((element) => {
                element.style.backgroundColor = '';
              });
            }, 2200);
          }
          return true;
        }
      }
      return false;
    },
    [blocks, originals],
  );

  /** 点击审阅意见跳转：逐个尝试关联的 block_id，全部失败时给出提示而不是静默无反应。 */
  const jumpToIssue = useCallback(
    (issue: DocIssue) => {
      for (const blockId of issue.block_ids) {
        if (goToBlock(blockId)) return;
      }
      notify.warning(
        issue.block_ids.length === 0
          ? '该意见是全局性说明，未关联具体段落'
          : '未能在文档中定位该意见涉及的段落（可能已被修改）',
      );
    },
    [goToBlock],
  );

  // ------------------------------------------------------------------ 审阅

  const runReview = useCallback(async () => {
    if (!requireModel()) return;
    setReviewing(true);
    try {
      const result = await api.post<{ issues: DocIssue[]; reviewed_blocks: number; notes: string[] }>(
        '/api/enterprise/agent-apps/doc:review',
        { tenant_id: TENANT_ID, model_config_id: modelId, doc_id: docId, blocks },
      );
      // 后端 issue id 按批次从 i0 重新编号，多次审阅会撞 id（issueStatus 全局共享 →
      // 应用一条把另一批同号项一起标成已应用）。这里给每条加上本次审阅的条目级前缀。
      const entryId = `review-${Date.now()}`;
      const issues = result.issues.map((issue) => ({ ...issue, id: `${entryId}#${issue.id}` }));
      const statusUpdate: Record<string, IssueStatus> = {};
      issues.forEach((issue) => {
        statusUpdate[issue.id] = 'open';
      });
      setIssueStatus((current) => ({ ...current, ...statusUpdate }));
      setChatEntries((current) => [
        ...current,
        { kind: 'review', id: entryId, issues, notes: result.notes },
      ]);
      if (result.issues.length === 0) {
        notify.success('审阅完成：没有发现明显问题');
      } else {
        notify.success(`审阅完成：发现 ${result.issues.length} 个问题`);
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '文档审阅失败');
    } finally {
      setReviewing(false);
    }
  }, [blocks, docId, modelId, requireModel]);

  /** 修订追踪 ON 时，审阅修改落成段落批注的文案（原文不动）。 */
  const buildIssueComment = (issue: DocIssue, blockId: string): string => {
    const fix = issue.fixes.find((item) => item.block_id === blockId);
    const detail = issue.detail ? `（${issue.detail}）` : '';
    const suggestion = fix ? `建议改为：${fix.new_text}` : '';
    return `【${issue.type}】${issue.title}${detail}${suggestion}`.slice(0, 500);
  };

  const applyFixes = useCallback((issue: DocIssue, asComment: boolean) => {
    if (issue.fixes.length === 0) return;
    setBlocks((current) =>
      current.map((block) => {
        const fix = issue.fixes.find((item) => item.block_id === block.id);
        if (!fix) return block;
        if (asComment) {
          // 修订追踪：不改原文，修改建议以 Word 批注形式附在段落上
          return { ...block, comment: buildIssueComment(issue, block.id) };
        }
        return { ...block, text: fix.new_text, comment: '' };
      }),
    );
  }, []);

  const applyIssue = useCallback(
    (issue: DocIssue) => {
      applyFixes(issue, trackChanges);
      setIssueStatus((current) => ({ ...current, [issue.id]: 'applied' }));
      notify.success(trackChanges ? '已添加批注，右侧正在更新' : '已应用修改，右侧正在更新');
    },
    [applyFixes, trackChanges],
  );

  const dismissIssue = useCallback((issueId: string) => {
    setIssueStatus((current) => ({ ...current, [issueId]: 'dismissed' }));
  }, []);

  const applyAllIssues = useCallback(() => {
    const openIssues = chatEntries.flatMap((entryItem) =>
      entryItem.kind === 'review'
        ? entryItem.issues.filter(
            (issue) => (issueStatus[issue.id] ?? 'open') === 'open' && issue.fixes.length > 0,
          )
        : [],
    );
    if (openIssues.length === 0) return;
    openIssues.forEach((issue) => applyFixes(issue, trackChanges));
    setIssueStatus((current) => {
      const next = { ...current };
      openIssues.forEach((issue) => {
        next[issue.id] = 'applied';
      });
      return next;
    });
    notify.success(
      trackChanges
        ? `已添加 ${openIssues.length} 条批注，右侧正在更新`
        : `已应用 ${openIssues.length} 条修改，右侧正在更新`,
    );
  }, [applyFixes, chatEntries, issueStatus, trackChanges]);

  // ------------------------------------------------------------------ 对话

  const applyActions = useCallback(
    (actions: Array<{ block_id: string; new_text: string; reason: string }>, staged: boolean) => {
      if (actions.length === 0) return;
      if (staged) {
        setPending((current) => [
          ...current,
          ...actions.map((action, index) => ({
            key: `s-${Date.now()}-${index}`,
            blockId: action.block_id,
            newText: action.new_text,
            reason: action.reason || 'AI 建议修改',
          })),
        ]);
      } else {
        setBlocks((current) =>
          current.map((block) => {
            const action = actions.find((item) => item.block_id === block.id);
            return action ? { ...block, text: action.new_text } : block;
          }),
        );
      }
    },
    [],
  );

  const sendMessage = useCallback(async () => {
    const message = input.trim();
    if (!message || chatting) return;
    if (!requireModel()) return;
    setInput('');
    setChatting(true);
    const history = chatEntries.slice(-8).map((entryItem) =>
      entryItem.kind === 'chat'
        ? { role: entryItem.role, content: entryItem.content }
        : { role: 'assistant' as const, content: `完成一键审阅：发现 ${entryItem.issues.length} 个问题` },
    );
    setChatEntries((current) => [...current, { kind: 'chat', id: `u-${Date.now()}`, role: 'user', content: message }]);
    try {
      const result = await api.post<{ reply: string; actions: Array<{ block_id: string; new_text: string; reason: string }> }>(
        '/api/enterprise/agent-apps/doc:chat',
        {
          tenant_id: TENANT_ID,
          model_config_id: modelId,
          doc_id: docId,
          message,
          history,
          blocks,
        },
      );
      applyActions(result.actions, trackChanges);
      setChatEntries((current) => [
        ...current,
        {
          kind: 'chat',
          id: `a-${Date.now()}`,
          role: 'assistant',
          content:
            result.actions.length > 0 && trackChanges
              ? `${result.reply}（已生成 ${result.actions.length} 处修改建议，请在下方确认）`
              : result.reply,
        },
      ]);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : 'AI 修改失败');
    } finally {
      setChatting(false);
    }
  }, [applyActions, blocks, chatEntries, chatting, docId, input, modelId, requireModel, trackChanges]);

  const applySuggestion = useCallback(
    (key: string) => {
      setPending((current) => {
        const item = current.find((suggestion) => suggestion.key === key);
        if (item) {
          setBlocks((blocksCurrent) =>
            blocksCurrent.map((block) => {
              if (block.id !== item.blockId) return block;
              if (trackChanges) {
                // 修订追踪：原文不动，落成 Word 批注
                return {
                  ...block,
                  comment: `【修订建议】${item.reason}：建议改为「${item.newText}」`.slice(0, 500),
                };
              }
              return { ...block, text: item.newText, comment: '' };
            }),
          );
        }
        return current.filter((suggestion) => suggestion.key !== key);
      });
    },
    [trackChanges],
  );

  const dismissSuggestion = useCallback((key: string) => {
    setPending((current) => current.filter((suggestion) => suggestion.key !== key));
  }, []);

  const revertBlock = useCallback(
    (blockId: string) => {
      setBlocks((current) =>
        current.map((block) =>
          block.id === blockId && originals[block.id] !== undefined
            ? { ...block, text: originals[block.id], comment: '' }
            : block,
        ),
      );
      setPending((current) => current.filter((suggestion) => suggestion.blockId !== blockId));
    },
    [originals],
  );

  // ------------------------------------------------------------------ 导出

  const exportDocx = useCallback(async () => {
    if (!docId) return;
    setExporting(true);
    try {
      const blob = await api.postBlob('/api/enterprise/agent-apps/doc:export', {
        tenant_id: TENANT_ID,
        doc_id: docId,
        blocks,
        file_name: docName,
      });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = docName.toLowerCase().endsWith('.docx') ? docName : `${docName || 'document'}.docx`;
      anchor.click();
      URL.revokeObjectURL(url);
      notify.success('已导出 Word 文档');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '导出失败');
    } finally {
      setExporting(false);
    }
  }, [blocks, docId, docName]);

  const onInputKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
      event.preventDefault();
      void sendMessage();
    }
  };

  const restoreAll = useCallback(() => {
    setBlocks((current) =>
      current.map((block) =>
        originals[block.id] !== undefined ? { ...block, text: originals[block.id], comment: '' } : block,
      ),
    );
    setPending([]);
    setChangedListOpen(false);
    // 已应用的审阅项随原文一起回滚：状态回到「待处理」，允许再次应用
    setIssueStatus((current) => {
      const next: Record<string, IssueStatus> = {};
      Object.entries(current).forEach(([id, status]) => {
        next[id] = status === 'applied' ? 'open' : status;
      });
      return next;
    });
    notify.success('已恢复全部原文，已应用的审阅修改已回滚，可再次应用');
  }, [originals]);

  const hasDoc = blocks.length > 0;
  const changedCount = changedIds.size;

  return (
    <div className="min-h-full box-border px-[48px] pt-[20px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader className="mb-[16px]" onLogout={onLogout} userName={currentUser?.username} title={`Agent 广场 · ${entry.name}`} />

      {/* docx-preview 渲染样式微调：容器自身已是灰底，去掉库默认的灰衬底 */}
      <style>{`
        .docx-wrapper { background: transparent !important; padding: 8px !important; }
        .docx-wrapper > section.docx {
          box-shadow: 0 2px 12px 0 rgba(15, 23, 42, 0.08);
          border-radius: 6px;
          margin-bottom: 16px;
        }
      `}</style>

      <div className="flex h-[calc(100vh-190px)] min-h-[560px] flex-col gap-[14px] rounded-[20px] bg-white p-[16px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        {/* 与其它使用页统一：标题在页头，卡片内只留右上角返回入口 */}
        <div className="flex flex-wrap items-center justify-end gap-[10px]">
          <AgentAppBackButton />
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-[14px] xl:flex-row">
          {/* 左栏：文件 + 模型 + 审阅 + 对话 */}
          <div className="flex min-h-0 w-full shrink-0 flex-col gap-[12px] xl:w-[380px] xl:border-r-[0.5px] xl:border-[#eef0f5] xl:pr-[16px]">
            {!hasDoc ? (
              <label
                className={cn(
                  'flex min-h-0 flex-1 cursor-pointer flex-col items-center justify-center gap-[10px] rounded-[14px] border-[1.5px] border-dashed border-[#d8dfeb] bg-[#fafbfd] px-[20px] py-[36px] text-center transition-colors hover:border-[#b9c5dd] hover:bg-[#f4f6fa]',
                  parsing && 'pointer-events-none opacity-70',
                )}
              >
                <input ref={importInputRef} type="file" accept=".docx" data-testid="doc-import-input" className="hidden" onChange={onFileChange} />
                {parsing ? (
                  <LoaderCircle className="size-[26px] animate-spin text-[#858b9c]" />
                ) : (
                  <Upload className="size-[26px] text-[#858b9c]" />
                )}
                <span className="text-[13px] font-medium text-[#18181a]">
                  {parsing ? '正在解析文档…' : '上传 Word 文档'}
                </span>
                <span className="max-w-[240px] text-[11px] leading-[17px] text-[#858b9c]">
                  拖拽或点击选择 .docx 文件；解析后可一键审阅、对话修改，并导出保留原格式的 Word。
                </span>
              </label>
            ) : (
              <>
                {/* 文件栏 */}
                <div className="flex items-center gap-[8px] px-[2px]">
                  <FileText className="size-[15px] shrink-0 text-[#1a71ff]" />
                  <span className="min-w-0 flex-1 truncate text-[12px] font-medium text-[#18181a]" title={docName}>
                    {docName}
                  </span>
                  <button
                    type="button"
                    aria-label="重新上传"
                    title="重新上传"
                    onClick={() => importInputRef.current?.click()}
                    className="grid size-[28px] shrink-0 place-items-center rounded-[8px] text-[#757f9c] transition-colors hover:bg-[#f6f6f6] hover:text-[#18181a]"
                  >
                    <Upload className="size-[14px]" />
                  </button>
                  <UIButton
                    type="button"
                    onClick={() => void exportDocx()}
                    disabled={exporting}
                    className="h-[30px] shrink-0 gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[10px] text-[12px] font-normal text-[#4f5669] hover:bg-[#f6f6f6] disabled:opacity-60"
                  >
                    {exporting ? <LoaderCircle className="size-[13px] animate-spin" /> : <Download className="size-[13px]" />}
                    导出 Word
                  </UIButton>
                  <input ref={importInputRef} type="file" accept=".docx" data-testid="doc-import-input" className="hidden" onChange={onFileChange} />
                </div>

                {/* 对话流（审阅结果 + 一问一答） */}
                <div className="flex min-h-0 flex-1 flex-col gap-[10px] overflow-y-auto pr-[2px]">
                  {chatEntries.map((entryItem) =>
                    entryItem.kind === 'review' ? (
                      <div key={entryItem.id} className="flex flex-col gap-[8px]">
                        <div className="flex items-center justify-between gap-[8px]">
                          <span className="text-[11px] font-medium text-[#757f9c]">
                            审阅结果 · {entryItem.issues.length} 个问题
                          </span>
                          {entryItem.issues.some(
                            (issue) => issue.fixes.length > 0 && (issueStatus[issue.id] ?? 'open') === 'open',
                          ) && (
                            <button
                              type="button"
                              onClick={applyAllIssues}
                              className="rounded-[6px] bg-[#eef3ff] px-[8px] py-[2px] text-[11px] text-[#1a71ff] transition-colors hover:bg-[#dfeaff]"
                            >
                              全部应用
                            </button>
                          )}
                        </div>
                        {entryItem.notes.map((note) => (
                          <p key={note} className="rounded-[8px] bg-[#fff7e8] px-[8px] py-[6px] text-[11px] leading-[16px] text-[#8a4b00]">
                            {note}
                          </p>
                        ))}
                        {entryItem.issues.length === 0 && (
                          <p className="text-[12px] leading-[18px] text-[#858b9c]">没有发现明显问题，文档状态不错。</p>
                        )}
                        {entryItem.issues.map((issue) => {
                          const status = issueStatus[issue.id] ?? 'open';
                          return (
                            <div
                              key={issue.id}
                              className={cn(
                                'flex flex-col gap-[6px] rounded-[12px] border-[0.5px] px-[10px] py-[9px]',
                                status === 'open' && 'border-[#e3e7f1] bg-white',
                                status === 'applied' && 'border-[#bfe6cd] bg-[#f2fbf5]',
                                status === 'dismissed' && 'border-[#eef0f4] bg-[#fafbfd] opacity-70',
                              )}
                            >
                              {issue.block_ids.length > 0 ? (
                                <button
                                  type="button"
                                  onClick={() => jumpToIssue(issue)}
                                  title="点击定位到文档对应位置"
                                  className="flex items-start gap-[6px] text-left"
                                >
                                  <span className="mt-[1px] shrink-0 rounded-[4px] bg-[#eef2ff] px-[5px] py-[1px] text-[10px] text-[#4f46e5]">
                                    {issue.type}
                                  </span>
                                  <span className="text-[12px] font-medium leading-[18px] text-[#18181a]">{issue.title}</span>
                                </button>
                              ) : (
                                <div className="flex items-start gap-[6px]">
                                  <span className="mt-[1px] shrink-0 rounded-[4px] bg-[#fdf2e3] px-[5px] py-[1px] text-[10px] text-[#b25e09]">
                                    {issue.type}
                                  </span>
                                  <span className="text-[12px] font-medium leading-[18px] text-[#18181a]">{issue.title}</span>
                                </div>
                              )}
                              {issue.detail && (
                                <p className="pl-[2px] text-[11px] leading-[16px] text-[#858b9c]">{issue.detail}</p>
                              )}
                              {status === 'open' && issue.fixes.length > 0 && (
                                <div className="flex items-center gap-[8px]">
                                  <button
                                    type="button"
                                    onClick={() => applyIssue(issue)}
                                    className="rounded-[6px] bg-[#18181a] px-[8px] py-[2px] text-[11px] text-white transition-colors hover:bg-[#303030]"
                                  >
                                    应用修改
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => dismissIssue(issue.id)}
                                    className="rounded-[6px] px-[8px] py-[2px] text-[11px] text-[#858b9c] transition-colors hover:bg-[#f6f6f6]"
                                  >
                                    忽略
                                  </button>
                                </div>
                              )}
                              {status === 'applied' && (
                                <span className="flex items-center gap-[4px] text-[11px] text-[#1a7f4b]">
                                  <Check className="size-[12px]" />
                                  {trackChanges ? '已加批注' : '已应用'}
                                </span>
                              )}
                              {status === 'dismissed' && <span className="text-[11px] text-[#a3aaba]">已忽略</span>}
                            </div>
                          );
                        })}
                      </div>
                    ) : (
                      <div
                        key={entryItem.id}
                        className={cn(
                          'flex',
                          entryItem.role === 'user' ? 'justify-end' : 'justify-start',
                        )}
                      >
                        <div
                          className={cn(
                            'max-w-[86%] rounded-[12px] px-[10px] py-[7px] text-[12px] leading-[18px]',
                            entryItem.role === 'user'
                              ? 'bg-[#18181a] text-white'
                              : 'bg-[#f4f6fa] text-[#2c3242]',
                          )}
                        >
                          {entryItem.content}
                        </div>
                      </div>
                    ),
                  )}
                  {chatting && (
                    <div className="flex items-center gap-[6px] text-[11px] text-[#858b9c]">
                      <LoaderCircle className="size-[12px] animate-spin" />
                      AI 正在处理文档…
                    </div>
                  )}
                  {chatEntries.length > 0 && (
                    <div className="flex items-center gap-[10px] py-[2px] text-[10px] text-[#c0c6d4]">
                      <span className="h-[0.5px] flex-1 bg-[#eef0f4]" />
                      以上是历史对话
                      <span className="h-[0.5px] flex-1 bg-[#eef0f4]" />
                    </div>
                  )}
                  <div ref={messagesEndRef} />
                </div>

                {/* 待确认建议（修订追踪模式下 AI 的修改先落在这里） */}
                {pending.length > 0 && (
                  <div className="flex shrink-0 flex-col gap-[6px] rounded-[12px] border-[0.5px] border-[#f0dca8] bg-[#fffaef] px-[10px] py-[8px]">
                    <div className="flex items-center justify-between gap-[8px]">
                      <span className="text-[11px] font-medium text-[#8a4b00]">
                        待确认修改 · {pending.length} 处
                      </span>
                      <button
                        type="button"
                        onClick={() => {
                          const keys = pending.map((item) => item.key);
                          keys.forEach((key) => applySuggestion(key));
                        }}
                        className="rounded-[6px] bg-[#18181a] px-[8px] py-[2px] text-[10px] text-white transition-colors hover:bg-[#303030]"
                      >
                        全部应用
                      </button>
                    </div>
                    {pending.map((item) => (
                      <div key={item.key} className="flex items-center gap-[8px]">
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-[10px] text-[#8a4b00]" title={item.reason}>
                            建议：{item.reason}
                          </p>
                          <p className="truncate text-[11px] leading-[16px] text-[#464c5e]" title={item.newText}>
                            {item.newText}
                          </p>
                        </div>
                        <button
                          type="button"
                          onClick={() => applySuggestion(item.key)}
                          className="shrink-0 rounded-[5px] bg-[#18181a] px-[7px] py-[2px] text-[10px] text-white hover:bg-[#303030]"
                        >
                          应用
                        </button>
                        <button
                          type="button"
                          onClick={() => dismissSuggestion(item.key)}
                          className="shrink-0 rounded-[5px] px-[7px] py-[2px] text-[10px] text-[#858b9c] hover:bg-[#f6f6f6]"
                        >
                          放弃
                        </button>
                      </div>
                    ))}
                  </div>
                )}

                {/* 输入区 */}
                <div className="flex shrink-0 flex-col gap-[8px] border-t-[0.5px] border-[#eef0f4] pt-[10px]">
                  <textarea
                    value={input}
                    rows={2}
                    placeholder="描述修改、写作要求，或直接提问（⌘/Ctrl + Enter 发送）"
                    onChange={(event) => setInput(event.target.value)}
                    onKeyDown={onInputKeyDown}
                    className="w-full resize-none rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[10px] py-[8px] text-[12px] leading-[1.6] text-[#18181a] outline-none transition-colors placeholder:text-[#a3aab9] focus:border-[#18181a]"
                  />
                  <div className="flex items-center justify-between gap-[8px]">
                    <div className="flex items-center gap-[8px]">
                      <span className="text-[11px] text-[#4f5669]">修订追踪</span>
                      <Switch
                        checked={trackChanges}
                        onCheckedChange={(checked) => setTrackChanges(checked)}
                        aria-label="修订追踪开关"
                        className="scale-[0.85]"
                      />
                      <TooltipProvider delayDuration={80}>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <button
                              type="button"
                              aria-label="修订追踪说明"
                              className="grid size-[16px] place-items-center rounded-full border-[0.5px] border-[#c8cede] text-[10px] leading-none text-[#858b9c] transition-colors hover:bg-[#f6f6f6] hover:text-[#18181a]"
                            >
                              ?
                            </button>
                          </TooltipTrigger>
                          <TooltipContent side="top" align="start" className="max-w-[260px] text-[11px] leading-[16px]">
                            开启后，AI 的修改以 Word 批注形式附在原文旁，不改动正文，导出的 Word
                            里可看到作者「AI 审阅」的批注；关闭后，修改直接替换原文。
                          </TooltipContent>
                        </Tooltip>
                      </TooltipProvider>
                    </div>
                    {/* 模型下拉（与修订追踪同行，右对齐） */}
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <button
                          type="button"
                          aria-label="选择模型"
                          disabled={models.length === 0}
                          className="flex h-[26px] w-[150px] min-w-0 shrink-0 items-center justify-between gap-[5px] rounded-[8px] border-[0.5px] border-[#e3e7f1] px-[8px] text-[12px] text-[#4f5669] transition-colors hover:border-[#cbd3e6] disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          <span className="truncate">{selectedModel?.name || (modelsLoading ? '加载中…' : '选择模型')}</span>
                          <IconChevronDown className="size-[11px] shrink-0 text-[#858b9c]" />
                        </button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end" className="max-h-[260px] min-w-[200px] overflow-y-auto">
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
                  </div>
                  <div className="flex items-center justify-between gap-[8px]">
                    {/* 一键审阅（左对齐） */}
                    <UIButton
                      type="button"
                      onClick={() => void runReview()}
                      disabled={reviewing || chatting || models.length === 0}
                      title="一键审阅"
                      className="h-[30px] shrink-0 gap-[5px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[10px] text-[12px] font-normal text-[#4f5669] hover:bg-[#f6f6f6] disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      {reviewing ? <LoaderCircle className="size-[13px] animate-spin" /> : <ScanSearch className="size-[13px]" />}
                      {reviewing ? '审阅中…' : '一键审阅'}
                    </UIButton>
                    <UIButton
                      type="button"
                      onClick={() => void sendMessage()}
                      disabled={!input.trim() || chatting || models.length === 0}
                      className="h-[30px] gap-[6px] rounded-[10px] bg-[#18181a] px-[14px] text-[12px] font-normal text-white hover:bg-[#303030] disabled:cursor-not-allowed disabled:bg-[#c9cfda]"
                    >
                      {chatting ? <LoaderCircle className="size-[13px] animate-spin" /> : <SendHorizontal className="size-[13px]" />}
                      发送
                    </UIButton>
                  </div>
                </div>
              </>
            )}
          </div>

          {/* 右栏：docx-preview 高保真文档视图 */}
          <div className="flex min-h-0 flex-1 flex-col gap-[10px]">
            {hasDoc && (
              <div className="flex flex-wrap items-center justify-between gap-[10px] px-[2px] text-[11px] text-[#858b9c]">
                <div className="flex items-center gap-[10px]">
                  <span>共 {blocks.length} 段</span>
                  {changedCount > 0 && (
                    <button
                      type="button"
                      aria-expanded={changedListOpen}
                      onClick={() => setChangedListOpen((current) => !current)}
                      className={cn(
                        'flex items-center gap-[3px] rounded-[6px] px-[8px] py-[2px] text-[11px] text-[#1a7f4b] transition-colors hover:bg-[#f2fbf5]',
                        changedListOpen && 'bg-[#f2fbf5]',
                      )}
                    >
                      {[
                        textChangedCount > 0 ? `已修改 ${textChangedCount} 段` : '',
                        commentCount > 0 ? `批注 ${commentCount} 条` : '',
                      ]
                        .filter(Boolean)
                        .join(' · ')}
                      <IconChevronDown className={cn('size-[11px] transition-transform', changedListOpen && 'rotate-180')} />
                    </button>
                  )}
                  {pending.length > 0 && <span className="text-[#8a4b00]">待确认 {pending.length} 处</span>}
                </div>
                {changedCount > 0 && (
                  <button
                    type="button"
                    onClick={restoreAll}
                    className="flex items-center gap-[4px] rounded-[6px] px-[8px] py-[2px] text-[11px] text-[#757f9c] transition-colors hover:bg-[#f6f6f6] hover:text-[#18181a]"
                  >
                    <RotateCcw className="size-[12px]" />
                    恢复全部原文
                  </button>
                )}
              </div>
            )}

            {/* 已修改/已加批注段落的撤销列表 */}
            {hasDoc && changedListOpen && changedBlocks.length > 0 && (
              <div className="flex shrink-0 flex-col gap-[4px] rounded-[12px] border-[0.5px] border-[#bfe6cd] bg-[#f7fdf9] px-[10px] py-[8px]">
                {changedBlocks.map((block) => {
                  const textChanged = block.text !== originals[block.id];
                  return (
                    <div key={block.id} className="flex items-center gap-[8px]">
                      <span
                        className="min-w-0 flex-1 truncate text-[11px] leading-[16px] text-[#464c5e]"
                        title={block.comment || block.text}
                      >
                        {block.comment ? `批注：${block.comment}` : block.text}
                      </span>
                      {textChanged && block.comment && (
                        <span className="shrink-0 rounded-[4px] bg-[#eef2ff] px-[4px] py-[1px] text-[10px] text-[#4f46e5]">
                          改文+批注
                        </span>
                      )}
                      <button
                        type="button"
                        aria-label={`撤销第 ${block.id} 段修改`}
                        onClick={() => revertBlock(block.id)}
                        className="grid size-[20px] shrink-0 place-items-center rounded-full text-[#5f8f70] transition-colors hover:bg-[#d9f0e0] hover:text-[#1a7f4b]"
                      >
                        <X className="size-[11px]" />
                      </button>
                    </div>
                  );
                })}
              </div>
            )}

            <div className="relative min-h-0 flex-1 overflow-auto rounded-[14px] bg-[#f1f2f5] p-[16px]">
              {hasDoc ? (
                <>
                  <div ref={paperRef} data-testid="docx-preview-container" className="min-h-full" />
                  {previewLoading && (
                    <div className="absolute inset-0 z-10 flex items-center justify-center gap-[8px] rounded-[14px] bg-white/60 text-[12px] text-[#858b9c]">
                      <LoaderCircle className="size-[14px] animate-spin" />
                      正在渲染文档…
                    </div>
                  )}
                  {previewError && !previewLoading && (
                    <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-[8px] bg-white/80 text-center">
                      <span className="text-[12px] text-[#c0392b]">文档渲染失败</span>
                      <UIButton
                        type="button"
                        onClick={() => {
                          // 触发重渲染：bump 版本号让 effect 重跑
                          renderSeqRef.current += 1;
                          setBlocks((current) => [...current]);
                        }}
                        className="h-[28px] gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[10px] text-[11px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]"
                      >
                        <IconRefresh className="size-[12px]" />
                        重试
                      </UIButton>
                    </div>
                  )}
                </>
              ) : (
                <div className="flex h-full flex-col items-center justify-center gap-[10px] text-center">
                  <FileText className="size-[30px] text-[#c0c6d4]" />
                  <span className="text-[13px] text-[#858b9c]">左侧上传 Word 文档后，这里会按原格式展示全文</span>
                  <span className="max-w-[300px] text-[11px] leading-[17px] text-[#a3aab9]">
                    一键审阅找出错别字、语病与表述问题；对话可直接修改、归纳总结，导出保留原格式。
                  </span>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
