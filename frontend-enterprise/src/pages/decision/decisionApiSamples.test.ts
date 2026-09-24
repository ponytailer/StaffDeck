import { describe, expect, it } from 'vitest';

import { buildApiSamples } from './decisionApiSamples';

const ENDPOINT = 'https://staffdeck.example.com/api/v1/decisions/predict';
const PAYLOAD = {
  state: { background: 'customer asked for a refund' },
  questions: { q1: { type: 'noul', instructions: 'refund requested?' } },
};

describe('buildApiSamples', () => {
  it('produces curl / python / node samples that hit the given endpoint', () => {
    const samples = buildApiSamples(ENDPOINT, PAYLOAD);

    expect(samples.curl).toContain(`curl -X POST '${ENDPOINT}'`);
    expect(samples.curl).toContain("-H 'Authorization: Bearer sd_live_REPLACE_WITH_YOUR_KEY'");

    expect(samples.python).toContain('import requests');
    expect(samples.python).toContain(`"${ENDPOINT}"`);

    expect(samples.node).toContain(`await fetch('${ENDPOINT}'`);
    expect(samples.node).toContain("'Bearer ' + API_KEY");
  });

  it('inlines the request body as pretty-printed JSON', () => {
    const samples = buildApiSamples(ENDPOINT, PAYLOAD);

    for (const code of [samples.curl, samples.python, samples.node]) {
      expect(code).toContain('"questions"');
      expect(code).toContain('"noul"');
      expect(code).toContain('\n  ');
    }
    expect(samples.curl).toContain('-d \'{\n');
  });

  it('keeps the sample skeleton free of CJK so the i18n catalog stays clean', () => {
    const samples = buildApiSamples(ENDPOINT, PAYLOAD);

    for (const code of [samples.curl, samples.python, samples.node]) {
      expect(/[\u3400-\u9fff]/.test(code)).toBe(false);
    }
  });
});
