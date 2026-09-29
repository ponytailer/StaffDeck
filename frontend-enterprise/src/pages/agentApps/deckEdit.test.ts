// @vitest-environment jsdom

import { describe, expect, it } from 'vitest';

import { collapseText, editPathOf, setDeckField } from './deckEdit';
import type { SlidesCard, SlidesDeck, SlidesPage } from './slidesDeck';

function card(overrides: Partial<SlidesCard> = {}): SlidesCard {
  return {
    badge: '01',
    title: '合同转签',
    subtitle: '已完成',
    tag: '进行中',
    points: ['对象：路秀新杰', '状态：已完成'],
    callout_title: '统一签约主体',
    callout_body: '后续对账更清晰',
    ...overrides,
  };
}

function page(overrides: Partial<SlidesPage> = {}): SlidesPage {
  return {
    layout: 'bullets',
    title: '本月进展',
    subtitle: '三条线同步推进',
    label: '库存收尾',
    bullets: ['要点一', '要点二'],
    cards: [card()],
    ...overrides,
  };
}

function deck(overrides: Partial<SlidesDeck> = {}): SlidesDeck {
  return {
    deck_title: '库存收尾',
    page_label: '库存收尾',
    pages: [page(), page({ layout: 'closing', title: '谢谢', bullets: [], cards: [] })],
    ...overrides,
  };
}

describe('collapseText', () => {
  it('压平换行与连续空格', () => {
    expect(collapseText('  多   空格\n换行  ')).toBe('多 空格 换行');
  });
});

describe('setDeckField', () => {
  it('改 deck 标题与页脚标签', () => {
    const next = setDeckField(deck(), 'deck_title', '新的标题');
    expect(next.deck_title).toBe('新的标题');
    expect(setDeckField(deck(), 'page_label', '新页脚').page_label).toBe('新页脚');
    expect(deck().deck_title).toBe('库存收尾'); // 不可变：原对象没被改
  });

  it('改页字段不会动其它页', () => {
    const next = setDeckField(deck(), 'p1.title', '致谢');
    expect(next.pages[1].title).toBe('致谢');
    expect(next.pages[0]).toEqual(deck().pages[0]);
    expect(next.pages[0]).not.toBe(next.pages[1]);
  });

  it('改 bullet 与 card 的各个字段', () => {
    expect(setDeckField(deck(), 'p0.b1', '改过的要点').pages[0].bullets).toEqual(['要点一', '改过的要点']);
    expect(setDeckField(deck(), 'p0.c0.title', '开票回收').pages[0].cards[0].title).toBe('开票回收');
    expect(setDeckField(deck(), 'p0.c0.tag', '已完成').pages[0].cards[0].tag).toBe('已完成');
    expect(setDeckField(deck(), 'p0.c0.pt0', '对象：X').pages[0].cards[0].points).toEqual([
      '对象：X',
      '状态：已完成',
    ]);
    expect(setDeckField(deck(), 'p0.c0.callout_body', '换一句话').pages[0].cards[0].callout_body).toBe(
      '换一句话',
    );
  });

  it('改一个字段时其它 card 与字段保持引用不变', () => {
    const before = deck();
    const next = setDeckField(before, 'p0.c0.title', '开票回收');
    expect(next.pages[0].cards[0].subtitle).toBe(before.pages[0].cards[0].subtitle);
    expect(next.pages[0].bullets).toBe(before.pages[0].bullets);
    expect(next.pages[1]).toBe(before.pages[1]);
  });

  it('值会被压平成单行', () => {
    expect(setDeckField(deck(), 'p0.title', '  两\n行  ').pages[0].title).toBe('两 行');
  });

  it('空值允许写回（清空副标题是合法需求）', () => {
    expect(setDeckField(deck(), 'p0.subtitle', '   ').pages[0].subtitle).toBe('');
  });

  it('非法路径原样返回，不抛错也不改结构', () => {
    const before = deck();
    for (const bad of [
      '',
      'nope',
      'p9.title',
      'p0.nope',
      'p0.b9',
      'p0.c9.title',
      'p0.c0.pt9',
      'p0.c0.nope',
      'p0.title.extra',
      'px.title',
    ]) {
      expect(setDeckField(before, bad, 'x')).toBe(before);
    }
  });
});

describe('editPathOf', () => {
  it('从元素的 data-sd-path 取路径，非可编辑元素返回空串', () => {
    const editable = document.createElement('h1');
    editable.dataset.sdPath = 'p0.title';
    expect(editPathOf(editable)).toBe('p0.title');
    expect(editPathOf(document.createElement('div'))).toBe('');
    expect(editPathOf(null)).toBe('');
  });
});
