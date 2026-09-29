import { describe, expect, it } from 'vitest';

import {
  DECK_STYLE,
  deckSummary,
  escapeHtml,
  renderDeckBody,
  renderDeckDocument,
  renderDeckPage,
  type SlidesDeck,
  type SlidesPage,
} from './slidesDeck';

function page(overrides: Partial<SlidesPage> = {}): SlidesPage {
  return {
    layout: 'bullets',
    title: '标题',
    subtitle: '',
    label: '',
    bullets: [],
    cards: [],
    ...overrides,
  };
}

function deck(pages: SlidesPage[]): SlidesDeck {
  return { deck_title: '库存收尾', page_label: '库存收尾', pages };
}

describe('escapeHtml', () => {
  it('转义所有 HTML 元字符', () => {
    expect(escapeHtml('<img src=x onerror="alert(1)"> &\'\'')).toBe(
      '&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp;&#39;&#39;',
    );
  });
});

describe('renderDeckPage', () => {
  it('内容页带页眉与全局页码（0-based 入参 → 1-based 显示）', () => {
    const html = renderDeckPage(
      deck([page(), page()]),
      page({ title: '正文', subtitle: '副标题', label: '库存收尾' }),
      1,
    );
    expect(html).toContain('data-page="2"');
    expect(html).toContain('PAGE <b>02</b>');
    expect(html).toContain('库存收尾');
    expect(html).toContain('副标题');
  });

  it('卡片数 >= 3 时切到网格版式，<= 2 时保持纵向排列', () => {
    const three = renderDeckPage(
      deck([page()]),
      page({
        cards: [1, 2, 3].map((index) => ({
          badge: `0${index}`,
          title: `卡${index}`,
          subtitle: '',
          tag: '',
          points: [],
          callout_title: '',
          callout_body: '',
        })),
      }),
      0,
    );
    expect(three).toContain('sd-cards--grid');

    const one = renderDeckPage(
      deck([page()]),
      page({
        cards: [{ badge: '01', title: '卡1', subtitle: '', tag: '', points: [], callout_title: '', callout_body: '' }],
      }),
      0,
    );
    expect(one).not.toContain('sd-cards--grid');
  });

  it('封面页不渲染页眉，改用封面版式与页码脚注', () => {
    const html = renderDeckPage(deck([page({ layout: 'cover', title: '年度汇报' })]), page({ layout: 'cover', title: '年度汇报' }), 0);
    expect(html).toContain('sd-slide--cover');
    expect(html).not.toContain('sd-hd');
    expect(html).toContain('PAGE 01 / 01');
  });

  it('模型输出里的脚本标签被转义，不会进入可执行上下文', () => {
    const html = renderDeckPage(
      deck([page()]),
      page({ title: '<script>alert(1)</script>' }),
      0,
    );
    expect(html).not.toContain('<script>');
    expect(html).toContain('&lt;script&gt;');
  });

  it('未知版式退化成普通内容页而不是崩掉', () => {
    const html = renderDeckPage(deck([page()]), page({ layout: 'hologram' }), 0);
    expect(html).toContain('sd-slide--hologram');
    expect(html).toContain('sd-hd');
  });
});

describe('在线修改（editable）', () => {
  const richPage = page({
    title: '本月进展',
    subtitle: '三条线同步推进',
    label: '库存收尾',
    bullets: ['要点一', '要点二'],
    cards: [
      {
        badge: '01',
        title: '合同转签',
        subtitle: '已完成',
        tag: '进行中',
        points: ['对象：路秀新杰', '状态：已完成'],
        callout_title: '统一签约主体',
        callout_body: '后续对账更清晰',
      },
    ],
  });

  it('默认（源码视图）不带任何可编辑属性', () => {
    const html = renderDeckPage(deck([richPage]), richPage, 0);
    expect(html).not.toContain('contenteditable');
    expect(html).not.toContain('data-sd-path');
  });

  it('预览下每个文本字段都挂上自己的路径', () => {
    const html = renderDeckPage(deck([richPage]), richPage, 0, { editable: true });
    for (const path of [
      'deck_title',
      'p0.label',
      'p0.title',
      'p0.subtitle',
      'p0.b0',
      'p0.b1',
      'p0.c0.badge',
      'p0.c0.title',
      'p0.c0.subtitle',
      'p0.c0.tag',
      'p0.c0.pt0',
      'p0.c0.pt1',
      'p0.c0.callout_title',
      'p0.c0.callout_body',
    ]) {
      expect(html).toContain(`data-sd-path="${path}"`);
    }
  });

  it('封面页也能改标题与页脚标签', () => {
    const cover = page({ layout: 'cover', title: '年度汇报', subtitle: '四步路径' });
    const html = renderDeckPage(deck([cover]), cover, 0, { editable: true });
    expect(html).toContain('data-sd-path="p0.title"');
    expect(html).toContain('data-sd-path="p0.subtitle"');
    expect(html).toContain('data-sd-path="page_label"');
  });

  it('metrics 的数值/指标名分别映射到 badge 与 title', () => {
    const metrics = page({
      layout: 'metrics',
      title: '关键指标',
      cards: [{
        badge: '397',
        title: '大使人数',
        subtitle: '',
        tag: '',
        points: [],
        callout_title: '',
        callout_body: '截至 9 月 28 日',
      }],
    });
    const html = renderDeckPage(deck([metrics]), metrics, 0, { editable: true });
    expect(html).toContain('data-sd-path="p0.c0.badge"');
    expect(html).toContain('data-sd-path="p0.c0.title"');
    expect(html).toContain('data-sd-path="p0.c0.callout_body"');
  });

  it('可编辑模式下内容依然转义（contenteditable 不等于可以塞 HTML）', () => {
    const html = renderDeckPage(
      deck([page()]),
      page({ title: '<img src=x onerror=alert(1)>' }),
      0,
      { editable: true },
    );
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img');
  });
});

describe('deckSummary', () => {
  it('输出「N 页 | 版式短码」', () => {
    expect(deckSummary(deck([
      page({ layout: 'cover' }),
      page({ layout: 'toc' }),
      page({ layout: 'cards' }),
    ]))).toBe('3 页 | cov · toc · art');
  });

  it('空 deck 返回空串', () => {
    expect(deckSummary(deck([]))).toBe('');
  });
});

describe('renderDeckDocument', () => {
  it('产出可直接打开的独立文档，内联同一份样式', () => {
    const doc = renderDeckDocument(deck([page({ title: '库存收尾' })]));
    expect(doc.startsWith('<!doctype html>')).toBe(true);
    expect(doc).toContain('<html lang="zh-CN">');
    expect(doc).toContain(DECK_STYLE.trim());
    expect(doc).toContain('sd-deck');
    expect(doc).toContain('库存收尾');
  });

  it('预览与源码使用同一个 body 渲染结果', () => {
    const input = deck([page({ title: 'A' }), page({ layout: 'closing', title: '谢谢' })]);
    expect(renderDeckDocument(input)).toContain(renderDeckBody(input));
  });

  it('标题也会被转义', () => {
    const doc = renderDeckDocument(deck([page({ title: '</title><script>x</script>' })]));
    expect(doc).not.toContain('<script>');
  });
});
