import { describe, expect, it } from 'vitest';

import { parseMcpArgs, parseMcpTimeoutSeconds } from './ToolsPage';

describe('parseMcpArgs', () => {
  it('preserves spaces inside one argument', () => {
    expect(parseMcpArgs('C:\\Program Files\\mcp server\\index.js')).toEqual([
      'C:\\Program Files\\mcp server\\index.js',
    ]);
  });

  it('uses one non-empty line per argument', () => {
    expect(parseMcpArgs('-m\nmy_mcp.server\n\n--label=customer support')).toEqual([
      '-m',
      'my_mcp.server',
      '--label=customer support',
    ]);
  });
});

describe('parseMcpTimeoutSeconds', () => {
  // MCP 子工具在界面上不可单独编辑，这里是「调大慢工具超时」的唯一入口，
  // 所以留空/非法值的语义必须固定下来：留空=沿用系统默认，越界要当场报错而不是交给后端 400。
  it('treats blank as "inherit the system default"', () => {
    expect(parseMcpTimeoutSeconds('')).toEqual({ value: null, valid: true });
    expect(parseMcpTimeoutSeconds('   ')).toEqual({ value: null, valid: true });
  });

  it('accepts an in-range number of seconds', () => {
    expect(parseMcpTimeoutSeconds('180')).toEqual({ value: 180, valid: true });
    expect(parseMcpTimeoutSeconds('1')).toEqual({ value: 1, valid: true });
    expect(parseMcpTimeoutSeconds('3600')).toEqual({ value: 3600, valid: true });
  });

  it('rejects out-of-range and non-numeric values', () => {
    expect(parseMcpTimeoutSeconds('0').valid).toBe(false);
    expect(parseMcpTimeoutSeconds('3601').valid).toBe(false);
    expect(parseMcpTimeoutSeconds('-5').valid).toBe(false);
    expect(parseMcpTimeoutSeconds('abc').valid).toBe(false);
  });
});
