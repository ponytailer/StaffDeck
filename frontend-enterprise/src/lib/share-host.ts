/**
 * 数字员工分享链接的对外 host。
 *
 * 默认用 window.location.origin（控制台当前访问地址；生产里经反代时可能拿到的是
 * IP 而不是对外域名）。配置 VITE_SHARE_HOST 后（例如 https://deck.example.com），
 * 分享链接统一走配置值；尾斜杠归一化掉，path 留给调用方拼。
 *
 * 纯函数实现，方便打桩测试 VITE_* 环境变量。
 */
export function shareHostFromEnv(
  env: { VITE_SHARE_HOST?: string } | undefined,
  fallbackOrigin: string,
): string {
  const configured = env?.VITE_SHARE_HOST?.trim();
  if (configured) {
    // 去掉末尾 /，避免 `host/` + `/share/token` 出现双斜杠。
    return configured.replace(/\/+$/, '');
  }
  return fallbackOrigin.replace(/\/+$/, '');
}

export function shareUrlForToken(token: string, host: string): string {
  return `${host}/share/${token}`;
}

/** 通用技能分享链接（免登录查看描述并下载技能包）。 */
export function generalSkillShareUrlForToken(token: string, host: string): string {
  return `${host}/share/skill/${token}`;
}
