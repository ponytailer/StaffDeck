/**
 * 决策助手开放 API 的示例代码生成。
 *
 * 注意：这里刻意**不出现中文**——示例代码是给用户复制到终端/IDE 直接跑的，
 * 中英文注释会随 i18n 目录一起被判为「待翻译文案」（`npm run i18n:check`）。
 * 面向用户的中文说明放在 `DecisionApiDialog.tsx` 的界面文案里。
 */

export type DecisionApiSamples = {
  curl: string;
  python: string;
  node: string;
};

const PLACEHOLDER_KEY = 'sd_live_REPLACE_WITH_YOUR_KEY';

/** 生成 curl / Python / Node 三种调用示例；`payload` 会被格式化成请求体内联进去。 */
export function buildApiSamples(endpoint: string, payload: unknown): DecisionApiSamples {
  const body = JSON.stringify(payload, null, 2);

  const curl = [
    `curl -X POST '${endpoint}' \\`,
    `  -H 'Authorization: Bearer ${PLACEHOLDER_KEY}' \\`,
    `  -H 'Content-Type: application/json' \\`,
    `  -d '${body}'`,
  ].join('\n');

  const python = [
    'import requests',
    '',
    '',
    `API_KEY = "${PLACEHOLDER_KEY}"`,
    '',
    'payload = ' + body,
    '',
    'response = requests.post(',
    `    "${endpoint}",`,
    '    headers={"Authorization": f"Bearer {API_KEY}"},',
    '    json=payload,',
    '    timeout=60,',
    ')',
    'response.raise_for_status()',
    '',
    'for key, answer in response.json()["answers"].items():',
    '    print(key, answer)',
  ].join('\n');

  const node = [
    `const API_KEY = '${PLACEHOLDER_KEY}';`,
    '',
    'const payload = ' + body + ';',
    '',
    `const response = await fetch('${endpoint}', {`,
    '  method: "POST",',
    '  headers: {',
    "    Authorization: 'Bearer ' + API_KEY,",
    '    "Content-Type": "application/json",',
    '  },',
    '  body: JSON.stringify(payload),',
    '});',
    '',
    'if (!response.ok) throw new Error(await response.text());',
    '',
    'const { answers } = await response.json();',
    'console.log(answers);',
  ].join('\n');

  return { curl, python, node };
}
