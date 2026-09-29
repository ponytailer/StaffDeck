/**
 * AI 幻灯片生成：deck 数据结构 + 渲染（预览与「HTML 源码」共用同一份实现）。
 *
 * 关键约束：预览和 HTML 源码必须是**同一份产物**，否则用户会看到「预览和源码不一致」。
 * 所以这里只有一个纯函数 `renderDeckDocument()`：源码 tab 直接显示它的返回值，
 * 预览 tab 把 `renderDeckBody()`（同一个函数的 body 部分）注入容器。
 *
 * 在线修改：预览用 `editable: true` 渲染，文本元素会带上 `contenteditable` 与
 * `data-sd-path`（见 `deckEdit.ts` 的路径语法）；用户改完由容器上的 blur 委托回填 deck。
 * 「HTML 源码」用默认的 `editable: false`，保持一份干净的、可另存打开的文档。
 *
 * 安全：deck 内容全部来自模型（或用户键盘）输入，所有文本一律走 `escapeHtml()` 再拼接；
 * 因此 `dangerouslySetInnerHTML` 里不会出现可执行的模型文本。
 */

export type SlidesCard = {
  badge: string;
  title: string;
  subtitle: string;
  tag: string;
  points: string[];
  callout_title: string;
  callout_body: string;
};

export type SlidesPage = {
  layout: string;
  title: string;
  subtitle: string;
  label: string;
  bullets: string[];
  cards: SlidesCard[];
};

export type SlidesDeck = {
  deck_title: string;
  page_label: string;
  pages: SlidesPage[];
};

/** 版式 → 摘要条里的短码（对应截图右上角的 `p13 · wp2 · art`）。 */
const LAYOUT_CODES: Record<string, string> = {
  cover: 'cov',
  toc: 'toc',
  section: 'sec',
  bullets: 'txt',
  cards: 'art',
  metrics: 'kpi',
  closing: 'end',
};

const DECK_FONT =
  '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Segoe UI", Roboto, sans-serif';

/**
 * 预览与「HTML 源码」共用的样式表。
 *
 * 注意：这段是 JS 模板字符串，**里面的注释只能是 ASCII** —— i18n 校验脚本会把含中文的
 * 模板字符串当成「待翻译文案」，所以 CSS 注释里不要写中文，否则会报缺失词条。
 */
export const DECK_STYLE = `
.sd-deck{display:flex;flex-direction:column;gap:20px;color:#141b33;font-family:${DECK_FONT};--sd-ink:#141b33;--sd-ink-2:#5a6277;--sd-muted:#8c93a8;--sd-line:#e8ebf2;--sd-accent:#f26a1b;--sd-accent-2:#2f6bff;--sd-soft:#f7f8fb}
.sd-frame{container-type:inline-size;width:100%}
.sd-slide{position:relative;width:100%;aspect-ratio:1280/720;box-sizing:border-box;background:#fff;border-radius:10px;box-shadow:0 1px 3px rgba(15,23,42,.10);display:flex;flex-direction:column;padding:4cqw 4.4cqw;overflow:hidden}
.sd-hd{display:flex;align-items:center;justify-content:space-between;gap:2cqw;padding-bottom:1.1cqw;border-bottom:.11cqw solid var(--sd-line);font-size:1.05cqw;letter-spacing:.06em;color:var(--sd-muted)}
.sd-hd__brand{display:flex;align-items:center;gap:.7cqw;font-weight:600;color:#2a3350;letter-spacing:.02em}
.sd-hd__dot{width:.72cqw;height:.72cqw;border-radius:50%;background:var(--sd-accent);flex:none}
.sd-hd__page{white-space:nowrap}
.sd-hd__page b{color:var(--sd-ink);font-size:1.3cqw}
.sd-title{margin:2.5cqw 0 0;font-size:3.25cqw;line-height:1.16;font-weight:800;letter-spacing:-.015em}
.sd-sub{margin-top:.9cqw;font-size:1.32cqw;line-height:1.5;color:var(--sd-ink-2)}
.sd-rule{width:4.4cqw;height:.42cqw;border-radius:999px;background:var(--sd-accent);margin-top:1.3cqw}
.sd-body{margin-top:2.1cqw;flex:1;min-height:0;display:flex;flex-direction:column;gap:1.5cqw}
.sd-bullets{display:flex;flex-direction:column;gap:1.35cqw}
.sd-bullet{display:flex;gap:1.15cqw;align-items:flex-start;font-size:1.48cqw;line-height:1.55;color:#2c3350}
.sd-bullet__i{margin-top:.62cqw;width:.62cqw;height:.62cqw;border-radius:50%;background:var(--sd-accent-2);flex:none}
.sd-cards{display:flex;flex-direction:column;gap:1.35cqw}
.sd-cards--grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(25cqw,1fr));gap:1.35cqw}
.sd-card{display:flex;align-items:stretch;gap:1.7cqw;background:var(--sd-soft);border:.11cqw solid var(--sd-line);border-radius:1.05cqw;padding:1.55cqw 1.75cqw}
.sd-cards--grid .sd-card{flex-direction:column;gap:1cqw}
.sd-card__badge{flex:none;width:4.1cqw;height:4.1cqw;border-radius:.95cqw;background:var(--sd-ink);color:#fff;font-size:1.45cqw;font-weight:700;display:grid;place-items:center;letter-spacing:.01em}
.sd-card__main{flex:1;min-width:0;display:flex;flex-direction:column;gap:.65cqw}
.sd-card__top{display:flex;align-items:center;gap:.8cqw;flex-wrap:wrap}
.sd-card__title{font-size:1.82cqw;font-weight:700;line-height:1.3}
.sd-card__sub{font-size:1.12cqw;line-height:1.5;color:var(--sd-ink-2)}
.sd-card__points{display:flex;flex-wrap:wrap;gap:.45cqw 1.7cqw;font-size:1.06cqw;line-height:1.5;color:var(--sd-ink-2)}
.sd-chip{display:inline-flex;align-items:center;border-radius:999px;padding:.24cqw .85cqw;font-size:.98cqw;font-weight:600;background:#e8f0ff;color:var(--sd-accent-2)}
.sd-card__note{flex:none;width:23cqw;border-left:.11cqw solid var(--sd-line);padding-left:1.6cqw;display:flex;flex-direction:column;gap:.5cqw}
.sd-cards--grid .sd-card__note{width:auto;border-left:0;border-top:.11cqw solid var(--sd-line);padding-left:0;padding-top:1cqw}
.sd-card__note-t{font-size:1.3cqw;font-weight:700}
.sd-card__note-b{font-size:1.03cqw;line-height:1.5;color:var(--sd-ink-2)}
.sd-toc{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1.4cqw 2.8cqw;align-content:start}
.sd-toc__item{display:flex;gap:1.1cqw;align-items:baseline;padding-bottom:1cqw;border-bottom:.11cqw dashed var(--sd-line);font-size:1.42cqw;line-height:1.4;color:#2c3350}
.sd-toc__n{font-size:1.1cqw;font-weight:700;color:var(--sd-accent);letter-spacing:.04em;flex:none}
.sd-metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(20cqw,1fr));gap:1.5cqw;align-content:start}
.sd-metric{border:.11cqw solid var(--sd-line);border-radius:1.05cqw;padding:1.7cqw;background:var(--sd-soft);display:flex;flex-direction:column;gap:.55cqw}
.sd-metric__v{font-size:2.9cqw;font-weight:800;line-height:1;color:var(--sd-accent-2);letter-spacing:-.02em}
.sd-metric__t{font-size:1.26cqw;font-weight:700}
.sd-metric__d{font-size:1.03cqw;line-height:1.5;color:var(--sd-ink-2)}
.sd-slide--section{justify-content:center}
.sd-slide--section .sd-sec-n{font-size:1.5cqw;font-weight:700;letter-spacing:.18em;color:var(--sd-accent)}
.sd-slide--closing{justify-content:center;align-items:flex-start}
.sd-slide--cover{justify-content:center;align-items:flex-start;background:linear-gradient(128deg,#0f1a33 0%,#1c2a52 52%,#2c4079 100%);box-shadow:0 1px 3px rgba(15,23,42,.18)}
.sd-slide--cover .sd-title{font-size:4.4cqw;color:#fff;max-width:88%;margin-top:0}
.sd-slide--cover .sd-sub{color:#c6d1ea;font-size:1.45cqw;max-width:68%}
.sd-slide--cover .sd-cover__bar{width:6cqw;height:.5cqw;border-radius:999px;background:var(--sd-accent);margin-top:1.7cqw}
.sd-slide--cover .sd-cover__meta{margin-top:auto;display:flex;justify-content:space-between;width:100%;font-size:1.02cqw;letter-spacing:.08em;color:#8ea0c8}
/* editable fields: hover gives a soft halo, focus a ring, so "this can be edited" is obvious.
   CSS comments inside this string must stay ASCII — the i18n checker reads template literals. */
.sd-deck [data-sd-path]{outline:none;border-radius:4px;transition:box-shadow .12s ease,background-color .12s ease}
.sd-deck [data-sd-path]:hover{box-shadow:0 0 0 2px rgba(47,107,255,.16)}
.sd-deck [data-sd-path]:focus{box-shadow:0 0 0 2px rgba(47,107,255,.45);background:rgba(47,107,255,.04)}
.sd-slide--cover [data-sd-path]:hover{box-shadow:0 0 0 2px rgba(255,255,255,.28)}
.sd-slide--cover [data-sd-path]:focus{box-shadow:0 0 0 2px rgba(255,255,255,.6);background:rgba(255,255,255,.06)}
`;

/** 渲染开关：`editable` 时文本元素带上 `contenteditable` + `data-sd-path`。 */
export type DeckRenderOptions = {
  editable?: boolean;
};

export function escapeHtml(value: string): string {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * 可编辑字段的 HTML 属性。
 *
 * `data-sd-path` 是「鼠标改字」回填 deck 的唯一线索（见 `deckEdit.ts`）；
 * 只在预览里挂，源码 tab 拿到的是干净的 HTML。
 */
function editAttrs(path: string, editable: boolean): string {
  if (!editable) return '';
  return ` contenteditable="true" spellcheck="false" data-sd-path="${escapeHtml(path)}"`;
}

function pageCode(layout: string): string {
  return LAYOUT_CODES[layout] || layout.slice(0, 4) || 'txt';
}

/** 摘要条文案：`5 页 | cov · toc · art · txt · end`。 */
export function deckSummary(deck: SlidesDeck): string {
  const pages = deck.pages || [];
  if (!pages.length) return '';
  return `${pages.length} 页 | ${pages.map((page) => pageCode(page.layout)).join(' · ')}`;
}

function slideHeader(deck: SlidesDeck, page: SlidesPage, index: number, editable: boolean): string {
  const brand = deck.deck_title || '演示文稿';
  const label = page.label || deck.page_label;
  const labelHtml = label
    ? `<span${editAttrs(`p${index}.label`, editable)}>${escapeHtml(label)}</span> · `
    : '';
  const meta = `${labelHtml}PAGE <b>${String(index + 1).padStart(2, '0')}</b>`;
  return (
    '<div class="sd-hd">'
    + `<div class="sd-hd__brand"><span class="sd-hd__dot"></span><span${editAttrs('deck_title', editable)}>${escapeHtml(brand)}</span></div>`
    + `<div class="sd-hd__page">${meta}</div>`
    + '</div>'
  );
}

function renderCard(card: SlidesCard, path: string, editable: boolean): string {
  const badge = card.badge
    ? `<div class="sd-card__badge"${editAttrs(`${path}.badge`, editable)}>${escapeHtml(card.badge)}</div>`
    : '';
  const tag = card.tag
    ? `<span class="sd-chip"${editAttrs(`${path}.tag`, editable)}>${escapeHtml(card.tag)}</span>`
    : '';
  const sub = card.subtitle
    ? `<div class="sd-card__sub"${editAttrs(`${path}.subtitle`, editable)}>${escapeHtml(card.subtitle)}</div>`
    : '';
  const points = (card.points || []).length
    ? `<div class="sd-card__points">${card.points
      .map((point, index) => `<span${editAttrs(`${path}.pt${index}`, editable)}>${escapeHtml(point)}</span>`)
      .join('')}</div>`
    : '';
  const note = card.callout_title || card.callout_body
    ? '<div class="sd-card__note">'
      + (card.callout_title
        ? `<div class="sd-card__note-t"${editAttrs(`${path}.callout_title`, editable)}>${escapeHtml(card.callout_title)}</div>`
        : '')
      + (card.callout_body
        ? `<div class="sd-card__note-b"${editAttrs(`${path}.callout_body`, editable)}>${escapeHtml(card.callout_body)}</div>`
        : '')
      + '</div>'
    : '';
  return '<div class="sd-card">'
    + badge
    + '<div class="sd-card__main">'
    + `<div class="sd-card__top"><span class="sd-card__title"${editAttrs(`${path}.title`, editable)}>${escapeHtml(card.title)}</span>${tag}</div>`
    + sub
    + points
    + '</div>'
    + note
    + '</div>';
}

function renderCards(cards: SlidesCard[], pathPrefix: string, editable: boolean): string {
  const list = cards || [];
  if (!list.length) return '';
  const grid = list.length >= 3 ? ' sd-cards--grid' : '';
  return `<div class="sd-cards${grid}">${list
    .map((card, index) => renderCard(card, `${pathPrefix}.c${index}`, editable))
    .join('')}</div>`;
}

function renderBullets(bullets: string[], pathPrefix: string, editable: boolean): string {
  const list = bullets || [];
  if (!list.length) return '';
  return `<div class="sd-bullets">${list
    .map((item, index) => '<div class="sd-bullet"><span class="sd-bullet__i"></span>'
      + `<span${editAttrs(`${pathPrefix}.b${index}`, editable)}>${escapeHtml(item)}</span></div>`)
    .join('')}</div>`;
}

function renderPageBody(page: SlidesPage, pathPrefix: string, editable: boolean): string {
  if (page.layout === 'cover') return '';
  if (page.layout === 'toc') {
    // 目录只用卡片的序号 + 章节名，副标题/备注在目录版式下没有位置
    return renderCards(
      (page.cards || []).map((card) => ({ ...card, callout_title: '', callout_body: '', points: [] })),
      pathPrefix,
      editable,
    );
  }
  return renderBullets(page.bullets, pathPrefix, editable) + renderCards(page.cards, pathPrefix, editable);
}

/** 单页 `<section>`；`index` 是整套 deck 的**全局页码**（0-based）。 */
export function renderDeckPage(
  deck: SlidesDeck,
  page: SlidesPage,
  index: number,
  options: DeckRenderOptions = {},
): string {
  const editable = Boolean(options.editable);
  const path = `p${index}`;
  const layout = page.layout || 'bullets';
  const classes = ['sd-slide', `sd-slide--${layout}`].join(' ');
  if (layout === 'cover') {
    return '<div class="sd-frame">'
      + `<section class="${classes}" data-layout="cover" data-page="${index + 1}">`
      + `<h1 class="sd-title"${editAttrs(`${path}.title`, editable)}>${escapeHtml(page.title)}</h1>`
      + (page.subtitle ? `<p class="sd-sub"${editAttrs(`${path}.subtitle`, editable)}>${escapeHtml(page.subtitle)}</p>` : '')
      + '<div class="sd-cover__bar"></div>'
      + '<div class="sd-cover__meta">'
      + `<span${editAttrs('page_label', editable)}>${escapeHtml(deck.page_label || deck.deck_title)}</span>`
      + `<span>PAGE 01 / ${String(deck.pages.length).padStart(2, '0')}</span>`
      + '</div>'
      + '</section>'
      + '</div>';
  }
  if (layout === 'section') {
    return '<div class="sd-frame">'
      + `<section class="${classes}" data-layout="section" data-page="${index + 1}">`
      + `<div class="sd-sec-n">${String(index + 1).padStart(2, '0')}</div>`
      + `<h1 class="sd-title"${editAttrs(`${path}.title`, editable)}>${escapeHtml(page.title)}</h1>`
      + (page.subtitle ? `<p class="sd-sub"${editAttrs(`${path}.subtitle`, editable)}>${escapeHtml(page.subtitle)}</p>` : '')
      + '<div class="sd-rule"></div>'
      + '</section>'
      + '</div>';
  }
  const metrics = layout === 'metrics' && (page.cards || []).length
    ? `<div class="sd-metrics">${page.cards
      .map((card, cardIndex) => {
        const cardPath = `${path}.c${cardIndex}`;
        // metrics 的「数值/指标名」按有没有 badge 换字段；口径说明写回 callout_body，
        // 因为渲染时 callout_body 的优先级最高，写它才能立即看到改动。
        const valuePath = card.badge ? `${cardPath}.badge` : `${cardPath}.title`;
        const labelPath = card.badge ? `${cardPath}.title` : `${cardPath}.subtitle`;
        const detail = card.callout_body || (card.points || []).join(' · ') || card.subtitle;
        return '<div class="sd-metric">'
          + `<div class="sd-metric__v"${editAttrs(valuePath, editable)}>${escapeHtml(card.badge || card.title)}</div>`
          + `<div class="sd-metric__t"${editAttrs(labelPath, editable)}>${escapeHtml(card.badge ? card.title : card.subtitle)}</div>`
          + `<div class="sd-metric__d"${editAttrs(`${cardPath}.callout_body`, editable)}>${escapeHtml(detail)}</div>`
          + '</div>';
      })
      .join('')}</div>`
    : '';
  return '<div class="sd-frame">'
    + `<section class="${classes}" data-layout="${escapeHtml(layout)}" data-page="${index + 1}">`
    + slideHeader(deck, page, index, editable)
    + `<h1 class="sd-title"${editAttrs(`${path}.title`, editable)}>${escapeHtml(page.title)}</h1>`
    + (page.subtitle ? `<p class="sd-sub"${editAttrs(`${path}.subtitle`, editable)}>${escapeHtml(page.subtitle)}</p>` : '')
    + '<div class="sd-rule"></div>'
    + `<div class="sd-body">${metrics || renderPageBody(page, path, editable)}</div>`
    + '</section>'
    + '</div>';
}

/** 预览用：`.sd-deck` 容器内部的 HTML（外层由 React 提供，样式见 `DECK_STYLE`）。 */
export function renderDeckBody(deck: SlidesDeck, options: DeckRenderOptions = {}): string {
  return (deck.pages || [])
    .map((page, index) => renderDeckPage(deck, page, index, options))
    .join('');
}

/** 「HTML 源码」用：可直接另存打开的独立文档。 */
export function renderDeckDocument(deck: SlidesDeck): string {
  const title = deck.deck_title || 'AI 演示文稿';
  return [
    '<!doctype html>',
    '<html lang="zh-CN">',
    '<head>',
    '<meta charset="utf-8" />',
    '<meta name="viewport" content="width=device-width, initial-scale=1" />',
    `<title>${escapeHtml(title)}</title>`,
    '<style>',
    `body{margin:0;padding:24px;background:#eef0f4}`,
    DECK_STYLE.trim(),
    '</style>',
    '</head>',
    '<body>',
    `<div class="sd-deck">${renderDeckBody(deck)}</div>`,
    '</body>',
    '</html>',
  ].join('\n');
}
