// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { I18nProvider } from '../i18n';

import LoginProductPreview from './LoginProductPreview';

function renderPreview() {
  render(
    <I18nProvider>
      <LoginProductPreview />
    </I18nProvider>,
  );
}

beforeEach(() => {
  // jsdom 不实现 matchMedia；动效要用它判断 prefers-reduced-motion 与精细指针。
  if (!window.matchMedia) {
    window.matchMedia = ((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;
  }
});

afterEach(cleanup);

describe('LoginProductPreview', () => {
  it('renders the product screenshot without decorating it with fake metrics', () => {
    renderPreview();

    expect(screen.getByAltText('StaffDeck 产品预览')).toBeTruthy();
  });

  it('skips the entrance animation when the user prefers reduced motion', () => {
    window.matchMedia = ((query: string) => ({
      matches: query.includes('prefers-reduced-motion'),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;

    renderPreview();

    const reveal = screen.getByTestId('login-preview-reveal');
    expect(reveal.style.opacity).toBe('1');
    // 起点里的位移与转动必须被清掉，否则用户仍会看到偏移动效。
    expect(reveal.style.transform).not.toMatch(/translate[XYZ]?\(-?[1-9]/);
    expect(reveal.style.transform).not.toMatch(/rotate[XYZ]\(-?[1-9]/);
  });
});