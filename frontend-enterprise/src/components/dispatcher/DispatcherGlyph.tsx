/**
 * 多智能体的统一图标：一个入口 + 两个下属（拓扑）。
 *
 * 颜色一律 currentColor，由外层决定（teal `#0f7268`）。员工用的头像位在多智能体
 * 这里没有意义 —— 它不是一个「人」，所以侧栏、配置卡、作用域说明页共用这一个图形，
 * 别各自再画一版（之前 DispatcherCard 自己内联了一份，改口径时容易漏）。
 */
export default function DispatcherGlyph({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.3"
      aria-hidden
      className={className}
    >
      <circle cx="8" cy="3.2" r="1.9" />
      <circle cx="3.4" cy="12.4" r="1.9" />
      <circle cx="12.6" cy="12.4" r="1.9" />
      <path d="M7 4.8 4.2 10.9M9 4.8l2.8 6.1" />
    </svg>
  );
}