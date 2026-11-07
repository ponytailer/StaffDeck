/**
 * 多智能体的关系拓扑图：入口 → 各成员 / 兜底出口。
 *
 * 手绘 SVG（不引第三方图形库）：节点数只有 0~3，引库的成本远高于收益。
 * 三处视觉语义必须保留（配置页 demo 已由用户验收）：
 * - 成员没写路由规则 → 节点描边变琥珀色，一眼看出「还差这一步」；
 * - 试跑命中 → 该成员的连线变粗变绿、其余淡出、并画一条动线；
 * - 兜底出口的点亮颜色跟随 fallback_mode（clarify/reply 灰、default_member 绿）。
 */

import { cn } from '@/lib/utils';

const TOPO_W = 800;
const ROW_H = 62;
const PAD_BOTTOM = 8;

export type TopologyMember = {
  agentId: string;
  name: string;
  description: string;
  rule: string;
  exampleCount: number;
};

export type TopologyFallbackMode = 'clarify' | 'reply' | 'default_member';

const AVATAR_GRADIENTS = [
  ['#4b5563', '#1f2937'],
  ['#0f7268', '#0b5a52'],
  ['#a85d32', '#8a4622'],
  ['#6f7b42', '#57622f'],
  ['#6b5b95', '#514178'],
];

/** 中文称呼「小王」「小李」取末字，其余取首字。 */
function initial(name: string): string {
  const text = String(name || '').trim();
  if (!text) return '?';
  if (text.length === 2 && text[0] === '小') return text[1];
  return text[0];
}

function trunc(value: string, limit: number): string {
  const text = String(value || '').trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

/** 按成员数算节点宽度，保证 n 个节点 + 间距恰好落在画布内。 */
function nodeWidth(count: number): number {
  if (count <= 0) return 300;
  const gapX = count <= 2 ? 40 : count === 3 ? 28 : 16;
  const usable = TOPO_W - 48 - Math.max(0, count - 1) * gapX;
  return Math.floor(Math.min(300, usable / count));
}

function nodeGapX(count: number): number {
  return count <= 2 ? 40 : count === 3 ? 28 : 16;
}

export default function DispatcherTopology({
  entryName,
  entryModel,
  members,
  fallbackMode,
  fallbackMemberName,
  highlightedMemberId,
  highlightFallback = false,
  onMemberClick,
  onFallbackClick,
  className,
}: {
  entryName: string;
  entryModel: string;
  members: TopologyMember[];
  fallbackMode: TopologyFallbackMode;
  fallbackMemberName?: string;
  highlightedMemberId?: string | null;
  highlightFallback?: boolean;
  onMemberClick?: (agentId: string) => void;
  onFallbackClick?: () => void;
  className?: string;
}) {
  const count = members.length;
  const width = nodeWidth(count);
  const gapX = nodeGapX(count);
  const totalW = count * width + Math.max(0, count - 1) * gapX;
  const startX = (TOPO_W - totalW) / 2;

  const rootY = 6;
  const rootH = 44;
  const rowY = rootY + rootH + 34;
  const fallbackY = rowY + ROW_H + 26;
  const height = fallbackY + 40 + PAD_BOTTOM;

  const rootW = 232;
  const rootX = (TOPO_W - rootW) / 2;
  const fbW = 232;
  const fbX = (TOPO_W - fbW) / 2;

  const fbMeta = {
    clarify: { label: '反问用户 + 列出可选同事', color: '#757f9c', dash: '4 3' },
    reply: { label: '直接回复固定话术', color: '#757f9c', dash: '4 3' },
    default_member: { label: '转给默认同事', color: '#0f7268', dash: '' },
  }[fallbackMode];

  return (
    <div
      className={cn(
        'rounded-[14px] border border-black/[0.05] bg-white/70 px-[14px] pt-[12px] pb-[6px]',
        className,
      )}
    >
      <div className="mb-[6px] flex items-center justify-between">
        <span className="flex items-center gap-[5px] text-[11.5px] font-medium text-[#5b6274]">
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4">
            <circle cx="8" cy="3.2" r="1.9" />
            <circle cx="3.4" cy="12.4" r="1.9" />
            <circle cx="12.6" cy="12.4" r="1.9" />
            <path d="M7 4.8 4.2 10.9M9 4.8l2.8 6.1" />
          </svg>
          关系拓扑
        </span>
        <span className="text-[10.5px] text-[#9aa1b1]">配置变化会实时反映在这里</span>
      </div>
      <svg
        viewBox={`0 0 ${TOPO_W} ${height}`}
        role="img"
        aria-label="多智能体与成员的关系拓扑"
        className="block h-auto w-full"
      >
        <defs>
          <marker id="disp-arw" viewBox="0 0 8 8" refX="6.4" refY="4" markerWidth="6" markerHeight="6" orient="auto">
            <path d="M1 1.2 6 4 1 6.8" fill="none" stroke="#c3cadb" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
          </marker>
          <marker id="disp-arw-hot" viewBox="0 0 8 8" refX="6.4" refY="4" markerWidth="6" markerHeight="6" orient="auto">
            <path d="M1 1.2 6 4 1 6.8" fill="none" stroke="#0f7268" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
          </marker>
          <marker id="disp-arw-fb" viewBox="0 0 8 8" refX="6.4" refY="4" markerWidth="6" markerHeight="6" orient="auto">
            <path d="M1 1.2 6 4 1 6.8" fill="none" stroke="#b45309" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
          </marker>
          {AVATAR_GRADIENTS.map(([from, to], index) => (
            <linearGradient key={index} id={`disp-g${index}`} x1="0" y1="0" x2="1" y2="1">
              <stop offset="0" stopColor={from} />
              <stop offset="1" stopColor={to} />
            </linearGradient>
          ))}
        </defs>

        {/* ① 入口节点 */}
        <g>
          <rect
            x={rootX}
            y={rootY}
            width={rootW}
            height={rootH}
            rx="12"
            fill="#fff"
            stroke="#0f7268"
            strokeWidth="1.4"
          />
          <rect x={rootX + 11} y={rootY + 11} width="20" height="20" rx="6" fill="#e1f1ed" />
          <path
            d={`M${rootX + 17.5} ${rootY + 21.5}h7M${rootX + 21} ${rootY + 18}v7`}
            stroke="#0f7268"
            strokeWidth="1.3"
            strokeLinecap="round"
          />
          <text x={rootX + 38} y={rootY + 19} fontSize="11.5" fontWeight="600" fill="#18181a">
            {trunc(entryName, 14)}
          </text>
          <text x={rootX + 38} y={rootY + 32} fontSize="9.5" fill="#757f9c">
            多智能体 · {trunc(entryModel, 16)}
          </text>
          <text
            x={rootX + rootW - 10}
            y={rootY + 19}
            fontSize="9.5"
            fill="#9aa1b1"
            textAnchor="end"
          >
            唯一对话入口
          </text>
        </g>

        {/* ② 成员节点 */}
        {count === 0 ? (
          <>
            <path
              d={`M${TOPO_W / 2} ${rootY + rootH} V${rowY + 14}`}
              stroke="#d6dae4"
              strokeWidth="1.3"
              strokeDasharray="3 3"
              markerEnd="url(#disp-arw)"
            />
            <rect
              x={(TOPO_W - 220) / 2}
              y={rowY}
              width="220"
              height="34"
              rx="10"
              fill="#fbfcfe"
              stroke="#e2e7f1"
              strokeWidth="1.2"
              strokeDasharray="4 3"
            />
            <text
              x={TOPO_W / 2}
              y={rowY + 21.5}
              fontSize="10.5"
              fill="#9aa1b1"
              textAnchor="middle"
            >
              还没有成员，问题会全部走兜底
            </text>
          </>
        ) : (
          members.map((member, index) => {
            const x = startX + index * (width + gapX);
            const cx = x + width / 2;
            const hasRule = member.rule.trim().length > 0;
            const fromY = rootY + rootH;
            const midY = fromY + (rowY - fromY) * 0.55;
            const dimmed =
              Boolean(highlightedMemberId) && highlightedMemberId !== member.agentId;
            const hot = highlightedMemberId === member.agentId;

            const fitChars = Math.max(6, Math.floor((width - 28) / 9.6));
            const nameChars = Math.max(4, Math.floor((width - 90) / 11));
            const summary =
              trunc(member.rule.split('\n')[0].trim(), fitChars) || '未填写路由规则';

            return (
              <g
                key={member.agentId}
                onClick={() => onMemberClick?.(member.agentId)}
                style={{ cursor: onMemberClick ? 'pointer' : 'default', opacity: dimmed ? 0.35 : 1 }}
              >
                <path
                  d={`M${TOPO_W / 2} ${fromY} V${midY} H${cx} V${rowY - 5}`}
                  fill="none"
                  stroke={hot ? '#0f7268' : '#d3d9e6'}
                  strokeWidth={hot ? 2 : 1.3}
                  markerEnd="url(#disp-arw)"
                />
                <rect
                  x={x}
                  y={rowY}
                  width={width}
                  height={ROW_H}
                  rx="11"
                  fill="#fff"
                  stroke={hot ? '#0f7268' : hasRule ? '#e3e7f1' : '#f0d5a8'}
                  strokeWidth={hot ? 1.8 : 1.2}
                />
                <circle cx={x + 22} cy={rowY + 22} r="10" fill={`url(#disp-g${index % 5})`} />
                <text
                  x={x + 22}
                  y={rowY + 26}
                  fontSize="9.5"
                  fontWeight="600"
                  fill="#fff"
                  textAnchor="middle"
                >
                  {initial(member.name)}
                </text>
                <text x={x + 38} y={rowY + 20} fontSize="11" fontWeight="600" fill="#18181a">
                  {trunc(member.name, nameChars)}
                </text>
                <text x={x + 38} y={rowY + 32} fontSize="9.5" fill="#9aa1b1">
                  {trunc(member.description, nameChars + 2)}
                </text>
                <text
                  x={x + 13}
                  y={rowY + 48}
                  fontSize="9.5"
                  fill={hasRule ? '#757f9c' : '#c08a3e'}
                >
                  {summary}
                </text>
                {member.exampleCount > 0 && (
                  <text
                    x={x + width - 12}
                    y={rowY + 20}
                    fontSize="9"
                    fill="#b4bac8"
                    textAnchor="end"
                  >
                    {member.exampleCount} 例
                  </text>
                )}
              </g>
            );
          })
        )}

        {/* ③ 成员 → 兜底出口的汇聚线 */}
        {count > 0 &&
          members.map((member, index) => {
            const cx = startX + index * (width + gapX) + width / 2;
            const linkY = rowY + ROW_H;
            const midY = linkY + (fallbackY - linkY) * 0.5;
            const dimmed =
              Boolean(highlightedMemberId) && highlightedMemberId !== member.agentId;
            return (
              <path
                key={member.agentId}
                d={`M${cx} ${linkY} V${midY} H${TOPO_W / 2} V${fallbackY - 5}`}
                fill="none"
                stroke={highlightFallback ? '#d8b078' : '#e0e5ef'}
                strokeWidth="1.1"
                strokeDasharray="3 3"
                markerEnd="url(#disp-arw)"
                opacity={dimmed ? 0.3 : 1}
              />
            );
          })}

        {/* ④ 兜底出口 */}
        <g
          onClick={onFallbackClick}
          style={{ cursor: onFallbackClick ? 'pointer' : 'default' }}
        >
          <rect
            x={fbX}
            y={fallbackY}
            width={fbW}
            height="34"
            rx="10"
            fill={highlightFallback ? '#fdf6ec' : '#fbfcfe'}
            stroke={highlightFallback ? '#b45309' : fbMeta.color}
            strokeWidth="1.2"
            strokeDasharray={fbMeta.dash || undefined}
          />
          <circle
            cx={fbX + 17}
            cy={fallbackY + 17}
            r="3"
            fill={highlightFallback ? '#b45309' : fbMeta.color}
            opacity=".8"
          />
          <text x={fbX + 27} y={fallbackY + 21} fontSize="10.5" fill="#5b6274">
            无法匹配 → {fbMeta.label}
            {fallbackMode === 'default_member' && fallbackMemberName
              ? `（${trunc(fallbackMemberName, 8)}）`
              : ''}
          </text>
        </g>
      </svg>
    </div>
  );
}