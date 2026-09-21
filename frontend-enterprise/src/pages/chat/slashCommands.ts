import type { ChatSlashCommand } from '@/types';

// @ 与 / 等价：用户习惯用 @ 提及技能/SOP/工具（有补全菜单），底层统一归一成 / 指令；
// 后端只认 / 语法，不做改动。
const PREFIX_PATTERN = /^[@/]([^\s]*)(?:\s+([^\s]*))?$/i;
const KINDS = new Set<ChatSlashCommand['kind']>(['sop', 'skill', 'tool']);

export type SlashCommandQuery = {
  kind?: ChatSlashCommand['kind'];
  search: string;
};

export type SlashCommandMessage = {
  command: ChatSlashCommand;
  requestText: string;
};

const KIND_ALIASES: Record<string, ChatSlashCommand['kind']> = {
  流程: 'sop',
  技能: 'skill',
  工具: 'tool',
};

/** 用户手敲的 @技能/流程/工具 归一成后端认识的 /sop|/skill|/tool。 */
export function normalizeSlashInput(input: string): string {
  const text = input.trim().replace(/^@/u, '/');
  return text.replace(/\/\s*(流程|技能|工具)(?=\s|$)/u, (_all, alias: string) => `/${KIND_ALIASES[alias]}`);
}

export function slashCommandQuery(input: string): SlashCommandQuery | null {
  const match = PREFIX_PATTERN.exec(input);
  if (!match) return null;
  const prefix = (match[1] || '').trim().toLowerCase();
  const kind = KINDS.has(prefix as ChatSlashCommand['kind'])
    ? prefix as ChatSlashCommand['kind']
    : undefined;
  return {
    kind,
    search: (kind ? match[2] || '' : prefix).trim().toLowerCase(),
  };
}

export function matchingSlashCommands(
  input: string,
  commands: ChatSlashCommand[],
  limit = 10,
): ChatSlashCommand[] {
  const query = slashCommandQuery(input);
  if (!query) return [];
  return commands
    // 补全菜单只列 SOP 与技能：工具由 SOP 捆绑调用，不应被用户单独选择
    // （/tool 语法保留可发，只是菜单不再列出）。
    .filter((item) => item.kind !== 'tool')
    .filter((item) => !query.kind || item.kind === query.kind)
    .filter((item) => {
      if (!query.search) return true;
      return [item.kind, item.target, item.label, item.description]
        .some((value) => value.toLowerCase().includes(query.search));
    })
    .slice(0, Math.max(1, limit));
}

export function selectedSlashCommandText(command: ChatSlashCommand): string {
  return `${command.command} `;
}

export function slashCommandComposerText(
  input: string,
  command: ChatSlashCommand | null,
): string {
  if (!command) return input;
  const prefix = selectedSlashCommandText(command);
  return input.startsWith(prefix) ? input.slice(prefix.length) : input;
}

export function slashCommandInput(command: ChatSlashCommand, composerText: string): string {
  return `${selectedSlashCommandText(command)}${composerText}`;
}

export function slashCommandMessage(
  input: string,
  commands: ChatSlashCommand[],
): SlashCommandMessage | null {
  // @ 深层兼容：手敲 @skill 天气 等价 /skill 天气（归一后再解析）；
  // 中文别名（@技能）在下面 KINDS 别名表一并处理。
  const normalized = normalizeSlashInput(input);
  // 归一后剥掉前缀：kind/target/prompt 对应第 1/2/3 组（(?:...) 非捕获不计组）
  const match = /^\/(sop|skill|tool)\s+([^\s]+)(?:\s+([\s\S]*))?$/iu.exec(normalized.trim());
  if (!match) return null;
  const kind = match[1].toLocaleLowerCase() as ChatSlashCommand['kind'];
  const target = match[2];
  const commandText = `/${kind} ${target}`;
  const command = commands.find((item) => (
    item.kind === kind
    && (item.target.toLocaleLowerCase() === target.toLocaleLowerCase()
      || item.command.toLocaleLowerCase() === commandText.toLocaleLowerCase())
  )) || {
    kind,
    target,
    label: target,
    description: '',
    command: commandText,
  };
  return { command, requestText: (match[3] || '').trim() };
}

export function slashMenuScrollTop(
  scrollTop: number,
  viewportHeight: number,
  optionTop: number,
  optionHeight: number,
  padding = 6,
): number {
  const visibleTop = scrollTop + padding;
  const visibleBottom = scrollTop + viewportHeight - padding;
  if (optionTop < visibleTop) return Math.max(0, optionTop - padding);
  const optionBottom = optionTop + optionHeight;
  if (optionBottom > visibleBottom) {
    return Math.max(0, optionBottom - viewportHeight + padding);
  }
  return scrollTop;
}

export function slashCommandKindLabel(kind: ChatSlashCommand['kind']): string {
  if (kind === 'sop') return 'SOP';
  if (kind === 'skill') return '技能';
  return '工具';
}
