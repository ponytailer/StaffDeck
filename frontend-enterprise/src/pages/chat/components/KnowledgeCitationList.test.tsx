// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { KnowledgeCitation } from '@/types';

import KnowledgeCitationList from './KnowledgeCitationList';

afterEach(() => cleanup());

const webCitation: KnowledgeCitation = {
  id: 'kref_1',
  label: '[1]',
  kind: 'evidence',
  title: '光明日报要闻',
  source_path: 'https://epaper.gmw.cn/x',
  url: 'https://epaper.gmw.cn/x',
  source_type: 'web',
  site: 'epaper.gmw.cn',
};

const kbCitation: KnowledgeCitation = {
  id: 'kref_2',
  label: '[2]',
  kind: 'evidence',
  title: '乐园手册：乘坐须知',
  source_path: 'kdoc_abc',
};

describe('KnowledgeCitationList 互联网来源', () => {
  it('网页来源点标题直接开原文，不走引用详情弹窗', async () => {
    // 线上故障：网页证据的 url 在引用投影里被丢掉，卡片只能打开「引用详情」，
    // 弹窗里来源是一行不可点的文本，用户点了也到不了原文。
    const onOpen = vi.fn();
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    render(<KnowledgeCitationList citations={[webCitation]} onOpen={onOpen} />);

    await userEvent.click(screen.getByRole('button', { name: /光明日报要闻/ }));

    expect(openSpy).toHaveBeenCalledWith(
      'https://epaper.gmw.cn/x',
      '_blank',
      'noopener,noreferrer',
    );
    expect(onOpen).not.toHaveBeenCalled();
    openSpy.mockRestore();
  });

  it('企业知识来源仍然走引用详情', async () => {
    const onOpen = vi.fn();
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    render(<KnowledgeCitationList citations={[kbCitation]} onOpen={onOpen} />);

    await userEvent.click(screen.getByRole('button', { name: /乐园手册/ }));

    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(openSpy).not.toHaveBeenCalled();
    openSpy.mockRestore();
  });

  it('有网页来源时标题改为「来源」并显示站点', () => {
    render(<KnowledgeCitationList citations={[webCitation, kbCitation]} onOpen={vi.fn()} />);

    expect(screen.getByText('来源')).toBeTruthy();
    expect(screen.getByText('epaper.gmw.cn')).toBeTruthy();
  });

  it('只有企业知识时保持「知识来源」字样', () => {
    render(<KnowledgeCitationList citations={[kbCitation]} onOpen={vi.fn()} />);

    expect(screen.getByText('知识来源')).toBeTruthy();
  });
});