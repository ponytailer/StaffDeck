// @vitest-environment jsdom

import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { TraceLine } from '../chatTypes';
import ExecutionRecord from './ExecutionRecord';

function line(patch: Partial<TraceLine> = {}): TraceLine {
  return {
    id: 'line-1',
    kind: 'tool',
    text: '调用工具 knowledge_search',
    state: 'completed',
    ...patch,
  };
}

function renderRecord(lines: TraceLine[]) {
  return render(
    <ExecutionRecord
      traceTurnId="turn-1"
      summary={{ text: '执行记录', state: 'completed' }}
      details={lines}
      expanded
      onToggle={vi.fn()}
    />,
  );
}

describe('ExecutionRecord step duration badge', () => {
  it('renders the durable duration beside completed steps', () => {
    renderRecord([line({ durationMs: 3240 })]);

    expect(screen.getByText('3.2s')).toBeTruthy();
  });

  it('derives duration from front-end timestamps when the projection is absent', () => {
    renderRecord([line({ startedAt: 1_000, completedAt: 2_420 })]);

    expect(screen.getByText('1.4s')).toBeTruthy();
  });

  it('keeps counting while the step is running and shows minute-scale formatting when done', () => {
    renderRecord([
      line({ state: 'running', startedAt: Date.now() - 500 }),
      line({ id: 'line-2', durationMs: 83_000 }),
    ]);

    expect(screen.getByText(/^\d+\.\ds$/)).toBeTruthy();
    expect(screen.getByText('1m 23s')).toBeTruthy();
  });

  it('hides the badge when no timing information exists', () => {
    const { container } = renderRecord([line()]);

    expect(container.textContent).not.toMatch(/\d+(\.\d+)?s/);
  });
});
