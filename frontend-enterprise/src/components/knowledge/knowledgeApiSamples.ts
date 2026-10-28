/**
 * 个人知识库开放 API 的示例代码生成。
 *
 * 注意：这里刻意**不出现中文**——示例代码是给用户复制到终端/IDE 直接跑的，
 * 中英文注释会随 i18n 目录一起被判为「待翻译文案」（`npm run i18n:check`）。
 * 面向用户的中文说明放在 `KnowledgeApiDialog.tsx` 的界面文案里。
 */

export type KnowledgeApiSamples = {
  curl: string;
  python: string;
  node: string;
};

const PLACEHOLDER_KEY = 'sd_live_REPLACE_WITH_YOUR_KEY';

/** 生成 curl / Python / Node 三种 JSON 检索示例。 */
export function buildKnowledgeApiSamples(endpoint: string): KnowledgeApiSamples {
  const curl = [
    `curl -X POST '${endpoint}' \\`,
    `  -H 'Authorization: Bearer ${PLACEHOLDER_KEY}' \\`,
    "  -H 'Content-Type: application/json' \\",
    `  -d '{"query": "travel hotel budget", "mode": "debug", "max_depth": 2}'`,
  ].join('\n');

  const python = [
    'import json',
    'import requests',
    '',
    '',
    `API_KEY = "${PLACEHOLDER_KEY}"`,
    `ENDPOINT = "${endpoint}"`,
    '',
    'response = requests.post(',
    '    ENDPOINT,',
    '    headers={"Authorization": f"Bearer {API_KEY}"},',
    '    json={"query": "travel hotel budget", "mode": "debug", "max_depth": 2},',
    '    timeout=120,',
    ')',
    'response.raise_for_status()',
    '',
    'payload = response.json()',
    'print(payload["knowledge_base_id"], payload["knowledge_base_version"])',
    'for citation in payload["citations"][:5]:',
    '    print(json.dumps(citation, ensure_ascii=False)[:300])',
  ].join('\n');

  const node = [
    `const API_KEY = '${PLACEHOLDER_KEY}';`,
    `const ENDPOINT = '${endpoint}';`,
    '',
    'const response = await fetch(ENDPOINT, {',
    "  method: 'POST',",
    "  headers: { Authorization: 'Bearer ' + API_KEY, 'Content-Type': 'application/json' },",
    '  body: JSON.stringify({ query: "travel hotel budget", mode: "debug", max_depth: 2 }),',
    '});',
    '',
    'if (!response.ok) throw new Error(await response.text());',
    '',
    'const payload = await response.json();',
    'console.log(payload.knowledge_base_id, payload.knowledge_base_version);',
    'for (const citation of payload.citations.slice(0, 5)) {',
    '  console.log(JSON.stringify(citation).slice(0, 300));',
    '}',
  ].join('\n');

  return { curl, python, node };
}