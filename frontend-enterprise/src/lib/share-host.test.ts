import { describe, expect, it } from 'vitest';

import { shareHostFromEnv, shareUrlForToken } from './share-host';

describe('shareHostFromEnv', () => {
  it('未配置时回落到当前 origin', () => {
    expect(shareHostFromEnv(undefined, 'https://app.example.com')).toBe('https://app.example.com');
    expect(shareHostFromEnv({}, 'http://localhost:5173')).toBe('http://localhost:5173');
  });

  it('配置了 VITE_SHARE_HOST 时走配置值', () => {
    expect(
      shareHostFromEnv({ VITE_SHARE_HOST: 'https://deck.example.com' }, 'http://10.0.0.1:5173'),
    ).toBe('https://deck.example.com');
  });

  it('空白配置视为未配置', () => {
    expect(shareHostFromEnv({ VITE_SHARE_HOST: '   ' }, 'http://10.0.0.1')).toBe('http://10.0.0.1');
  });

  it('配置末尾的斜杠被归一化', () => {
    expect(shareHostFromEnv({ VITE_SHARE_HOST: 'https://deck.example.com/' }, 'http://x')).toBe(
      'https://deck.example.com',
    );
    expect(shareHostFromEnv({ VITE_SHARE_HOST: 'https://d.com///' }, 'http://x')).toBe('https://d.com');
  });
});

describe('shareUrlForToken', () => {
  it('拼接分享链接', () => {
    expect(shareUrlForToken('tok-1', 'https://deck.example.com')).toBe(
      'https://deck.example.com/share/tok-1',
    );
    expect(shareUrlForToken('tok-1', 'http://localhost:5173')).toBe(
      'http://localhost:5173/share/tok-1',
    );
  });
});
