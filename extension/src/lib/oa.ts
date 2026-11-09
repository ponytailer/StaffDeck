import { storage } from './storage';
import { proxyFetch } from './proxy';

/**
 * OA 单据助手（书签 panel.js 的插件宿主）。
 *
 * 分工与书签版完全一致：后端 /config 下发配置（场景、定位器、默认值），
 * panel.js 代码随插件包内置（MV3 content script 禁 eval，不能像书签那样
 * 运行时拉 JS 执行）。配置动态、代码静态——改场景/定位器零发版，
 * 只有代填引擎本身改动才需要发插件版本。
 */

export type OaScenarioLite = {
  id: string;
  name: string;
  workflowid: string;
};

type OaConfig = {
  scenarios?: Array<{ id?: string; name?: string; summary?: string; enabled?: boolean; url?: string }>;
};

let configPromise: Promise<OaConfig | null> | null = null;

/** 从场景 url 里提取 workflowid（面板自动匹配场景用的就是它） */
function workflowidOf(url: string): string {
  const m = String(url).match(/[?&#]workflowid=(\d+)/);
  return m ? m[1] : '';
}

async function loadOaConfig(): Promise<OaConfig | null> {
  try {
    const base = (await storage.getApiBase()).replace(/\/+$/, '');
    // 必须走 SW 代理：content script 与页面同源，直连会被页面 CORS 卡死（config 端点
    // 虽有 Access-Control-Allow-Origin: * 但预检 Authorization 头时网关可能拦；代理零风险）
    const res = await proxyFetch(`${base}/api/enterprise/oa-assistant/config`, {
      method: 'GET',
      headers: { 'Content-Type': 'application/json' },
    });
    if (!res.ok) return null;
    const payload = JSON.parse(res.body) as { config?: OaConfig };
    return payload.config ?? null;
  } catch {
    return null;
  }
}

/** 拉配置（每次面板打开都刷新，进程内缓存并发去重） */
async function fetchConfig(): Promise<OaConfig | null> {
  if (!configPromise) {
    configPromise = loadOaConfig().finally(() => {
      // 不常驻缓存：下次打开面板重新拉，后端改配置即时生效
      configPromise = null;
    });
  }
  return configPromise;
}

/** 当前页面是否是 OA 报销表单页（hostname 命中任一场景 url 的 host 即算） */
export async function isOaPage(): Promise<boolean> {
  const config = await fetchConfig();
  if (!config) return false;
  const hosts = new Set<string>();
  for (const sc of config.scenarios ?? []) {
    if (!sc.url) continue;
    try {
      hosts.add(new URL(sc.url).hostname);
    } catch {
      // 非法 url 忽略
    }
  }
  return hosts.has(location.hostname);
}

/**
 * 打开（或收起）OA 代填面板。
 *
 * 首次：写配置/源站 → 动态 import 扩展内的 oa-panel.js（IIFE 立即建面板）；
 * 已加载：直接调面板自己的 toggle（书签版同款入口语义）。
 */
export async function toggleOaPanel(): Promise<void> {
  const w = window as unknown as {
    __SD_OA_CONFIG__?: unknown;
    __SD_OA_BASE__?: string;
    __SD_OA_PANEL__?: { toggle: () => void } | null;
  };

  if (w.__SD_OA_PANEL__) {
    w.__SD_OA_PANEL__.toggle();
    return;
  }

  const config = await fetchConfig();
  if (!config) {
    throw new Error('OA 配置加载失败，请检查网络后重试');
  }
  w.__SD_OA_CONFIG__ = config;
  w.__SD_OA_BASE__ = (await storage.getApiBase()).replace(/\/+$/, '');

  await import(/* @vite-ignore */ chrome.runtime.getURL('oa-panel.js'));
}

/** 面板场景自动匹配用的 workflowid（当前页面能唯一对应场景时，面板自动选中） */
export async function currentWorkflowid(): Promise<string> {
  return workflowidOf(location.href);
}

/** 供入口卡展示的场景数量（清单加载失败时返回 0） */
export async function oaScenarioCount(): Promise<number> {
  const config = await fetchConfig();
  return (config?.scenarios ?? []).filter((sc) => sc.enabled !== false).length;
}

/** 入口卡「跳转」用的场景 url（取第一个启用场景；配置缺失/为空返回 ''） */
export async function oaEntryUrl(): Promise<string> {
  const config = await fetchConfig();
  for (const sc of config?.scenarios ?? []) {
    if (sc.enabled !== false && sc.url) return sc.url;
  }
  return '';
}

export type OaScenarioInfo = {
  id: string;
  name: string;
  summary: string;
  url: string;
};

/** 单据助手 tab 的场景清单（仅启用项；配置加载失败返回 null 以示区分） */
export async function oaScenarios(): Promise<OaScenarioInfo[] | null> {
  const config = await fetchConfig();
  if (!config) return null;
  const items: OaScenarioInfo[] = [];
  for (const sc of config.scenarios ?? []) {
    if (sc.enabled === false || !sc.url) continue;
    items.push({
      id: sc.id || sc.url,
      name: sc.name || '未命名场景',
      summary: sc.summary || '',
      url: sc.url,
    });
  }
  return items;
}
