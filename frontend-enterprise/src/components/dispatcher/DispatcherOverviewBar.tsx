import { cn } from '@/lib/utils';
import type { DispatcherOverviewRead } from '@/lib/dispatcher';

/**
 * 「我的小队」顶部概览：4 张统计卡 + 配置健康度提示条。
 *
 * 为什么加它：这个页面原来只有一行标题 + 一个网格，首屏下方整块留白（用户反馈
 * 「有点太空旷」）。但**不是为了填满而填** —— 4 个数字分别回答「有几个入口 /
 * 覆盖几位同事 / 转过多少次 / 对外发布了几个」，健康度条只列真的会出问题的配置。
 *
 * 口径（数字旁边都带 title，不假装精确）：
 * - 累计转接 = 各会话 dispatch_log 之和，每会话最多 10 条 → 下限
 * - 成员 = 去重后的同事数（一个人被两个入口引用只算一次）
 * - 「未发布到广场」不算问题：大部分入口本来就不需要对外
 */

const CARD_CLASS =
  'flex min-w-[150px] flex-1 flex-col gap-[2px] rounded-[18px] border border-black/[0.05] bg-white px-[18px] py-[13px] text-left shadow-[0_6px_20px_rgba(17,17,17,0.04)]';

type Stat = {
  key: string;
  label: string;
  value: number | null;
  sub: string;
  hint?: string;
};

export default function DispatcherOverviewBar({
  overview,
  loading,
  squadCount,
  squadWithIssues,
  onAttention,
}: {
  overview: DispatcherOverviewRead | null;
  loading: boolean;
  /** 列表已知的数量：概览还没回来时先用它，避免整块闪成 0 */
  squadCount: number;
  /** 有配置问题的入口数（前端从列表算，点击后左列表只留这些） */
  squadWithIssues: number;
  onAttention: () => void;
}) {
  const dash = loading && !overview;
  const stats: Stat[] = [
    {
      key: 'squads',
      label: '小队',
      value: overview ? overview.squads : squadCount,
      sub: '每个入口 = 一位接待同事',
    },
    {
      key: 'members',
      label: '覆盖同事',
      value: overview ? overview.members : null,
      sub: '同一人被多处引用只算一次',
    },
    {
      key: 'transfers',
      label: '累计转接',
      value: overview ? overview.transfers : null,
      sub: '按会话记录累计',
      hint: '按各会话的转接记录累加；每个会话最多记 10 条，所以这是下限而不是精确总次数。',
    },
    {
      key: 'published',
      label: '已发布到广场',
      value: overview ? overview.published : null,
      sub: '同租户同事可直接试用',
    },
  ];

  const healthItems: { key: string; text: string }[] = [];
  if (overview?.missing_rule_members) {
    healthItems.push({
      key: 'missing_rule',
      text: `${overview.missing_rule_members} 位同事没写路由规则`,
    });
  }
  if (overview?.broken_models) {
    healthItems.push({
      key: 'broken_model',
      text: `${overview.broken_models} 个入口的模型已失效`,
    });
  }
  if (overview?.single_member_squads) {
    healthItems.push({
      key: 'single_member',
      text: `${overview.single_member_squads} 个入口只有 1 位同事`,
    });
  }

  return (
    <div className="mb-[16px]">
      <div className="flex flex-wrap items-stretch gap-[12px]" aria-label="小队统计">
        {stats.map((stat) => (
          <div key={stat.key} className={CARD_CLASS} title={stat.hint}>
            <span className="flex items-baseline gap-[6px]">
              <span className="text-[26px] font-semibold leading-none text-[#18181a]">
                {stat.value === null ? (dash ? '·' : '—') : stat.value}
              </span>
              <span className="text-[13px] text-[#464c5e]">{stat.label}</span>
            </span>
            <span className="whitespace-nowrap text-[11.5px] text-[#858b9c]">{stat.sub}</span>
          </div>
        ))}
      </div>

      {(healthItems.length > 0 || squadWithIssues > 0) && (
        <div
          className={cn(
            'mt-[12px] flex flex-wrap items-center gap-[8px] rounded-[14px] border px-[14px] py-[10px]',
            'border-[#f0e0c2] bg-[#fdf8ee]',
          )}
          aria-label="配置健康度"
        >
          <span className="text-[12px] font-medium text-[#8a5a12]">建议处理</span>
          {healthItems.map((item) => (
            <span
              key={item.key}
              className="rounded-[7px] bg-white/80 px-[8px] py-[2px] text-[11.5px] text-[#7a5416]"
            >
              {item.text}
            </span>
          ))}
          {squadWithIssues > 0 && (
            <button
              type="button"
              onClick={onAttention}
              className="ml-auto shrink-0 rounded-[8px] px-[8px] py-[3px] text-[11.5px] font-medium text-[#8a5a12] underline-offset-2 hover:bg-white/70 hover:underline"
            >
              只看需要处理的（{squadWithIssues}）
            </button>
          )}
        </div>
      )}
    </div>
  );
}
