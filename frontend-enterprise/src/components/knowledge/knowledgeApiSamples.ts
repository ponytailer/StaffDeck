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

export type KnowledgeApiSampleOptions = {
  /** collection=true：集合检索端点（不传 ids = 全部个人库）。 */
  collection?: boolean;
  /** 集合检索示意「限定范围」时传入；单库端点不需要。 */
  knowledgeBaseIds?: string[];
};

/** 生成 curl / Python / Node 三种 JSON 检索示例。 */
export function buildKnowledgeApiSamples(
  endpoint: string,
  options: KnowledgeApiSampleOptions = {},
): KnowledgeApiSamples {
  const idList = options.collection
    ? (options.knowledgeBaseIds || []).filter(Boolean)
    : [];
  const body = idList.length
    ? `{"query": "travel hotel budget", "knowledge_base_ids": ${JSON.stringify(idList)}, "mode": "debug", "max_depth": 2}`
    : '{"query": "travel hotel budget", "mode": "debug", "max_depth": 2}';

  // 单库接口回 knowledge_base_id / knowledge_base_version；
  // 集合接口回 knowledge_bases[]（每个库当前版本，用于缓存失效）。
  const scopeEcho = options.collection
    ? ["for item in payload[\"knowledge_bases\"]:", "    print(item[\"id\"], item[\"name\"], item[\"version\"])"]
    : ["print(payload[\"knowledge_base_id\"], payload[\"knowledge_base_version\"])"];
  const scopeEchoNode = options.collection
    ? [
        'for (const item of payload.knowledge_bases) {',
        '  console.log(item.id, item.name, item.version);',
        '}',
      ]
    : ['console.log(payload.knowledge_base_id, payload.knowledge_base_version);'];

  const curl = [
    `curl -X POST '${endpoint}' \\`,
    `  -H 'Authorization: Bearer ${PLACEHOLDER_KEY}' \\`,
    "  -H 'Content-Type: application/json' \\",
    `  -d '${body}'`,
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
    `    json=${body},`,
    '    timeout=120,',
    ')',
    'response.raise_for_status()',
    '',
    'payload = response.json()',
    ...scopeEcho,
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
    `  body: JSON.stringify(${body}),`,
    '});',
    '',
    'if (!response.ok) throw new Error(await response.text());',
    '',
    'const payload = await response.json();',
    ...scopeEchoNode,
    'for (const citation of payload.citations.slice(0, 5)) {',
    '  console.log(JSON.stringify(citation).slice(0, 300));',
    '}',
  ].join('\n');

  return { curl, python, node };
}