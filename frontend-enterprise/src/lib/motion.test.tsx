// @vitest-environment jsdom

import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  MOTION,
  prefersReducedMotion,
  reveal,
  settle,
  useCountUp,
  useRouteReveal,
  useStaggerReveal,
} from './motion';

type MediaListener = (event: MediaQueryListEvent) => void;

function mockMatchMedia(reduce: boolean) {
  window.matchMedia = ((query: string) => ({
    matches: query.includes('reduce') ? reduce : !reduce,
    media: query,
    onchange: null,
    addEventListener: (_type: string, _listener: MediaListener) => {},
    removeEventListener: (_type: string, _listener: MediaListener) => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

function NumberProbe({ value }: { value: number }) {
  const ref = useCountUp(value);
  return <span data-testid="probe" ref={ref}>{value}</span>;
}

function ListProbe({ enabled }: { enabled: boolean }) {
  const ref = useStaggerReveal<HTMLDivElement>(enabled);
  return (
    <div ref={ref}>
      <i>a</i>
      <i>b</i>
      <i>c</i>
    </div>
  );
}

function RouteProbe({ routeKey }: { routeKey: string }) {
  const ref = useRouteReveal<HTMLDivElement>(routeKey);
  return <div ref={ref}>content</div>;
}

afterEach(() => {
  cleanup();
  delete (window as { matchMedia?: unknown }).matchMedia;
});

describe('prefersReducedMotion', () => {
  it('returns false when matchMedia is unavailable (jsdom / SSR)', () => {
    expect(prefersReducedMotion()).toBe(false);
  });

  it('reflects the media query result', () => {
    mockMatchMedia(true);
    expect(prefersReducedMotion()).toBe(true);
    mockMatchMedia(false);
    expect(prefersReducedMotion()).toBe(false);
  });
});

describe('reveal', () => {
  it('lands on the final state and skips animation under reduced motion', () => {
    mockMatchMedia(true);
    const el = document.createElement('div');
    document.body.appendChild(el);

    const animation = reveal(el, { y: 12 });

    expect(animation).toBeNull();
    expect(el.style.opacity).toBe('1');
    el.remove();
  });

  it('hides the target synchronously when motion is allowed', () => {
    mockMatchMedia(false);
    const el = document.createElement('div');
    document.body.appendChild(el);

    const animation = reveal(el, { y: 8 });

    expect(animation).not.toBeNull();
    expect(el.style.opacity).toBe('0');
    animation?.pause();
    animation?.revert();
    el.remove();
  });
});

describe('settle', () => {
  it('forces targets visible', () => {
    const el = document.createElement('div');
    el.style.opacity = '0';
    settle(el);
    expect(el.style.opacity).toBe('1');
  });
});

describe('useCountUp', () => {
  it('writes the final numeric value under reduced motion', () => {
    mockMatchMedia(true);
    const { getByTestId } = render(<NumberProbe value={42} />);
    expect(getByTestId('probe').textContent).toBe('42');
  });

  it('starts from zero when motion is allowed', () => {
    mockMatchMedia(false);
    const raf = vi.spyOn(window, 'requestAnimationFrame');
    const { getByTestId } = render(<NumberProbe value={7} />);
    expect(getByTestId('probe').textContent).toBe('0');
    raf.mockRestore();
  });
});

describe('useStaggerReveal', () => {
  it('does not leave children invisible under reduced motion', () => {
    mockMatchMedia(true);
    const { container } = render(<ListProbe enabled />);
    const items = container.querySelectorAll('i');
    expect(items).toHaveLength(3);
    items.forEach((item) => expect((item as HTMLElement).style.opacity).toBe('1'));
  });

  it('does nothing before it is enabled', () => {
    mockMatchMedia(false);
    const { container } = render(<ListProbe enabled={false} />);
    container.querySelectorAll('i').forEach((item) => {
      expect((item as HTMLElement).style.opacity).toBe('');
    });
  });
});

describe('useRouteReveal', () => {
  it('keeps content visible when rerendered under reduced motion', () => {
    mockMatchMedia(true);
    const { container, rerender } = render(<RouteProbe routeKey="/a" />);
    expect((container.firstChild as HTMLElement).style.opacity).toBe('1');
    rerender(<RouteProbe routeKey="/b" />);
    expect((container.firstChild as HTMLElement).style.opacity).toBe('1');
  });
});

describe('MOTION tokens', () => {
  it('exposes a monotonically increasing duration scale', () => {
    expect(MOTION.fast).toBeLessThan(MOTION.base);
    expect(MOTION.base).toBeLessThan(MOTION.slow);
  });
});