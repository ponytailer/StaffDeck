import type { CSSProperties, ReactNode } from 'react';

import { cn } from '@/lib/utils';

/**
 * 员工小队头像。
 *
 * 小队不是「人」，所以不走员工那套人物插画；但只给一个固定拓扑图标又太单调
 * （用户反馈「小队头像搞几个可选的，不要只有一个」）。这里用「几何图形 × 色调」
 * 组出 6 个预设 —— 纯 SVG，不新增图片资源，任意尺寸都清晰。
 *
 * 选中的 key 存在 `AgentProfile.metadata_json["avatar_preset"]`，与员工的
 * `avatar_preset` 同名；旧数据没有这个字段时回落 `network`（即原来的拓扑图标），
 * 不需要数据迁移。
 */

type AvatarTone = {
  bg: string;
  fg: string;
  ring: string;
};

const TONES: Record<string, AvatarTone> = {
  teal: { bg: '#e1f1ed', fg: '#0f7268', ring: '#cfe7e2' },
  blue: { bg: '#e8f0ff', fg: '#1a71ff', ring: '#d3e2ff' },
  olive: { bg: '#eef4e6', fg: '#5c7a2e', ring: '#dfead2' },
  copper: { bg: '#fbeee4', fg: '#b5652a', ring: '#f3ddcd' },
  ink: { bg: '#eceef2', fg: '#464c5e', ring: '#dfe3ea' },
  gold: { bg: '#fdf6e8', fg: '#a9762a', ring: '#f6e8cd' },
};

const GLYPHS: Record<string, ReactNode> = {
  /** 一个入口 + 两个下属（原拓扑图标，作为默认）。 */
  network: (
    <>
      <circle cx="8" cy="3.2" r="1.9" />
      <circle cx="3.4" cy="12.4" r="1.9" />
      <circle cx="12.6" cy="12.4" r="1.9" />
      <path d="M7 4.8 4.2 10.9M9 4.8l2.8 6.1" />
    </>
  ),
  /** 中心 + 环绕轨道，强调「一圈同事围着同一个入口」。 */
  orbit: (
    <>
      <circle cx="8" cy="8" r="2" />
      <ellipse cx="8" cy="8" rx="5.6" ry="3.4" transform="rotate(-28 8 8)" />
      <circle cx="12.9" cy="6.1" r="1.05" fill="currentColor" stroke="none" />
      <circle cx="3.4" cy="10.2" r="1.05" fill="currentColor" stroke="none" />
    </>
  ),
  /** 十字中枢：四个人在一个节点上协同。 */
  hub: (
    <>
      <circle cx="8" cy="8" r="2.1" />
      <circle cx="8" cy="2.6" r="1.35" />
      <circle cx="8" cy="13.4" r="1.35" />
      <circle cx="2.6" cy="8" r="1.35" />
      <circle cx="13.4" cy="8" r="1.35" />
      <path d="M8 4v1.9M8 10.1v1.9M4 8h1.9M10.1 8H12" />
    </>
  ),
  /** 三层错位：任务逐层下发。 */
  layers: (
    <>
      <path d="M8 2.6 13.4 5.4 8 8.2 2.6 5.4Z" />
      <path d="M2.6 8.4 8 11.2 13.4 8.4" />
      <path d="M2.6 11.2 8 14 13.4 11.2" />
    </>
  ),
  /** 盾牌：守门人，先把不该转的挡住。 */
  shield: (
    <>
      <path d="M8 2.2 13 4.2v4.1c0 2.7-2 4.9-5 5.9-3-1-5-3.2-5-5.9V4.2Z" />
      <path d="M6.1 8.1 7.5 9.5 10.2 6.6" />
    </>
  ),
  /** 星芒：多人协作产出。 */
  spark: (
    <>
      <path d="M8 1.9 9.5 6.1 13.8 7.6 9.5 9.1 8 13.3 6.5 9.1 2.2 7.6 6.5 6.1Z" />
      <circle cx="8" cy="7.6" r="1.15" fill="currentColor" stroke="none" />
    </>
  ),
};

export type DispatcherAvatarPreset = {
  key: string;
  label: string;
  tone: keyof typeof TONES;
};

export const DISPATCHER_AVATAR_PRESETS: DispatcherAvatarPreset[] = [
  { key: 'network', label: '拓扑', tone: 'teal' },
  { key: 'orbit', label: '环轨', tone: 'blue' },
  { key: 'hub', label: '中枢', tone: 'olive' },
  { key: 'layers', label: '分层', tone: 'copper' },
  { key: 'shield', label: '守门', tone: 'ink' },
  { key: 'spark', label: '协作', tone: 'gold' },
];

export const DEFAULT_DISPATCHER_AVATAR = 'network';

/** 把（可能为空 / 已废弃的）key 归一成合法预设，未知值回落默认。 */
export function dispatcherAvatarPreset(key?: string | null): DispatcherAvatarPreset {
  return (
    DISPATCHER_AVATAR_PRESETS.find((item) => item.key === key)
    || DISPATCHER_AVATAR_PRESETS[0]
  );
}

export default function DispatcherAvatar({
  preset,
  size = 44,
  radius,
  className,
  style,
  /** 装饰用途（卡片里与名字重复）可关掉无障碍标签。 */
  decorative = true,
}: {
  preset?: string | null;
  size?: number;
  radius?: number | string;
  className?: string;
  style?: CSSProperties;
  decorative?: boolean;
}) {
  const item = dispatcherAvatarPreset(preset);
  const tone = TONES[item.tone] || TONES.teal;
  return (
    <span
      data-dispatcher-avatar={item.key}
      className={cn('inline-grid shrink-0 place-items-center', className)}
      style={{
        width: size,
        height: size,
        borderRadius: radius ?? Math.round(size * 0.3),
        background: tone.bg,
        color: tone.fg,
        ...style,
      }}
      aria-hidden={decorative || undefined}
      role={decorative ? undefined : 'img'}
      aria-label={decorative ? undefined : `小队头像：${item.label}`}
    >
      <svg
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
        strokeLinejoin="round"
        style={{ width: '56%', height: '56%' }}
      >
        {GLYPHS[item.key] || GLYPHS[DEFAULT_DISPATCHER_AVATAR]}
      </svg>
    </span>
  );
}
