/**
 * 幻灯片「在线修改」：把预览里被鼠标改过的文本回填进 deck。
 *
 * 预览是 `dangerouslySetInnerHTML` 注入的静态 HTML，React 不认识里面的节点，所以编辑能力
 * 靠 HTML 里的 `data-sd-path`（由 `slidesDeck.ts` 渲染时写入）+ 容器上的 blur 委托实现：
 * 用户改完离开某个元素 → 这里按路径改 deck 的**那一个字段** → deck 成为唯一事实来源，
 * 「HTML 源码」与「导出 PPTX」都自然带上改动。
 *
 * 路径语法（点号分隔 token）：
 *
 * - `deck_title` / `page_label`
 * - `p{i}.title` / `p{i}.subtitle` / `p{i}.label`
 * - `p{i}.b{k}`                             第 k 条 bullet
 * - `p{i}.c{j}.badge|title|subtitle|tag|callout_title|callout_body`
 * - `p{i}.c{j}.pt{k}`                       第 k 条 card point
 *
 * 非法路径一律返回原 deck（不抛错）—— 页面上多挂了一个属性不该让整页崩掉。
 */

import type { SlidesCard, SlidesDeck, SlidesPage } from './slidesDeck';

const PAGE_FIELDS = new Set(['title', 'subtitle', 'label']);
const CARD_FIELDS = new Set([
  'badge',
  'title',
  'subtitle',
  'tag',
  'callout_title',
  'callout_body',
]);

/** 单行字段：把换行压平（否则预览里的一个回车会把版式顶乱）。 */
export function collapseText(value: string): string {
  return value.replace(/\s+/g, ' ').trim();
}

function replacePage(deck: SlidesDeck, index: number, page: SlidesPage): SlidesDeck {
  return { ...deck, pages: (deck.pages || []).map((item, i) => (i === index ? page : item)) };
}

function replaceCard(
  deck: SlidesDeck,
  pageIndex: number,
  cardIndex: number,
  card: SlidesCard,
): SlidesDeck {
  const page = deck.pages[pageIndex];
  const cards = (page.cards || []).map((item, i) => (i === cardIndex ? card : item));
  return replacePage(deck, pageIndex, { ...page, cards });
}

export function setDeckField(deck: SlidesDeck, path: string, rawValue: string): SlidesDeck {
  const value = collapseText(rawValue);
  const tokens = String(path || '').split('.');
  const [head, ...rest] = tokens;

  if (rest.length === 0 && (head === 'deck_title' || head === 'page_label')) {
    return { ...deck, [head]: value };
  }

  if (!/^p\d+$/.test(head)) return deck;
  const pageIndex = Number(head.slice(1));
  const page = (deck.pages || [])[pageIndex];
  if (!page) return deck;

  if (rest.length === 1 && PAGE_FIELDS.has(rest[0])) {
    const field = rest[0] as 'title' | 'subtitle' | 'label';
    return replacePage(deck, pageIndex, { ...page, [field]: value });
  }

  if (rest.length === 1 && /^b\d+$/.test(rest[0])) {
    const bulletIndex = Number(rest[0].slice(1));
    const bullets = [...(page.bullets || [])];
    if (bulletIndex >= bullets.length) return deck;
    bullets[bulletIndex] = value;
    return replacePage(deck, pageIndex, { ...page, bullets });
  }

  if (rest.length !== 2 || !/^c\d+$/.test(rest[0])) return deck;
  const cardIndex = Number(rest[0].slice(1));
  const card = (page.cards || [])[cardIndex];
  if (!card) return deck;

  if (CARD_FIELDS.has(rest[1])) {
    const field = rest[1] as keyof SlidesCard;
    return replaceCard(deck, pageIndex, cardIndex, { ...card, [field]: value });
  }

  if (/^pt\d+$/.test(rest[1])) {
    const pointIndex = Number(rest[1].slice(2));
    const points = [...(card.points || [])];
    if (pointIndex >= points.length) return deck;
    points[pointIndex] = value;
    return replaceCard(deck, pageIndex, cardIndex, { ...card, points });
  }

  return deck;
}

/** 从被编辑的元素上取路径；不是可编辑字段就返回空串。 */
export function editPathOf(target: EventTarget | null): string {
  const element = target as HTMLElement | null;
  return element?.dataset?.sdPath || '';
}
