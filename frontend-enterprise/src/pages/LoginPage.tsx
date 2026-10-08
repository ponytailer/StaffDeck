import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react';

import { animate, createTimeline, stagger, text, utils } from 'animejs';

import { api, TENANT_ID, ApiError } from '../api/client';
import { setEnterpriseAuthSession, type EnterpriseAuthSession } from '../auth';
import AppHeader from '../components/AppHeader';
import BrandLogo from '../components/BrandLogo';
import EmployeeAvatar from '../components/EmployeeAvatar';
import LoginProductPreview from '../components/LoginProductPreview';
import IconFieldClear from '../assets/icons/field-clear.svg?react';
import IconFieldEye from '../assets/icons/field-eye.svg?react';
import IconFieldEyeOn from '../assets/icons/field-eye-on.svg?react';
import { EMPLOYEE_AVATAR_PRESETS } from '../employee';

export type LoginPageProps = {
  onLogin: (session: EnterpriseAuthSession) => void;
};

/**
 * 登录页的「在岗播报」内容：岗位、头像与职责全部取自产品里真实的数字员工，
 * 不是营销文案——第一眼就能看出这个平台到底在做什么。
 */
const STAFF_ON_DUTY = [
  { role: '市场', avatarPreset: 'marketing-spark', duty: '梳理市场目标、受众与内容策划清单' },
  { role: '数据分析', avatarPreset: 'data-insight', duty: '定义指标口径、分析周期与对比维度' },
  { role: '采购', avatarPreset: 'procurement-check', duty: '准备采购需求、供应商比较与审批材料' },
  { role: '项目管理', avatarPreset: 'project-board', duty: '拆解目标、里程碑、责任人与风险' },
  { role: '行政', avatarPreset: 'after-sales-seal', duty: '统筹会议室预订、办公用品与用章申请' },
  { role: '销售', avatarPreset: 'sales-handshake', duty: '澄清客户需求、准备沟通材料并推进商机' },
] as const;

/** 与产品能力一一对应，直接来自核心功能清单。 */
const HERO_CAPABILITIES = ['流程 SOP', '知识检索', '自主执行', '长期记忆'] as const;

/** 入场前先藏起来，避免首帧闪一下再跳到起点（useEffect 在首帧绘制后才跑）。 */
function hiddenFrom(offsetY: number): CSSProperties {
  return { opacity: 0, transform: `translateY(${offsetY}px)` };
}

function avatarProfile(preset: string) {
  const found = EMPLOYEE_AVATAR_PRESETS.find((item) => item.key === preset);
  return {
    avatarKind: 'preset' as const,
    avatarImage: '',
    avatarPreset: preset,
    avatarText: found?.text ?? '员',
    avatarTone: found?.tone ?? 'teal',
  };
}

/**
 * 未登录的落地 / 登录页。
 *
 * 版式：品牌眼眉 + 产品主标题（按字入场）→ 副标题 → 登录（点开后在原地滑出表单）
 * → 数字员工「在岗播报」→ 能力标签 → 产品预览演示区。
 * 背景只放几团缓慢漂移的光斑和一层淡网格：留白不空，但不抢内容。
 */
export default function LoginPage({ onLogin }: LoginPageProps) {
  const [showForm, setShowForm] = useState(false);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [usernameError, setUsernameError] = useState('');
  const [passwordError, setPasswordError] = useState('');
  const [loading, setLoading] = useState(false);

  const pageRef = useRef<HTMLDivElement>(null);
  const brandRef = useRef<HTMLSpanElement>(null);
  const titleRef = useRef<HTMLSpanElement>(null);
  const subtitleRef = useRef<HTMLParagraphElement>(null);
  const ctaRef = useRef<HTMLDivElement>(null);
  const tickerRef = useRef<HTMLDivElement>(null);
  const pillsRef = useRef<HTMLUListElement>(null);

  async function login() {
    const trimmedUsername = username.trim();
    const trimmedPassword = password.trim();
    setUsernameError(trimmedUsername ? '' : '请输入账号');
    setPasswordError(trimmedPassword ? '' : '请输入密码');
    if (!trimmedUsername || !trimmedPassword) return;

    setLoading(true);
    try {
      const session = await api.post<EnterpriseAuthSession>('/api/auth/login', {
        tenant_id: TENANT_ID,
        username: trimmedUsername,
        password: trimmedPassword,
      });
      setEnterpriseAuthSession(session);
      onLogin(session);
    } catch (error) {
      const messageText = error instanceof Error ? error.message : '';
      const fallback = '登录失败，请稍后重试';
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
        setUsernameError('账号或密码不正确');
        setPasswordError('请检查后重新输入');
      } else {
        setUsernameError('账号输入错误');
        setPasswordError(messageText || fallback);
      }
    } finally {
      setLoading(false);
    }
  }

  function onFieldKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Enter') void login();
  }

  useEffect(() => {
    const page = pageRef.current;
    const brand = brandRef.current;
    const title = titleRef.current;
    const subtitle = subtitleRef.current;
    const cta = ctaRef.current;
    const ticker = tickerRef.current;
    const pills = pillsRef.current;
    if (!page || !brand || !title || !subtitle || !cta || !ticker || !pills) return;

    const tickerRows = Array.from(ticker.querySelectorAll<HTMLElement>('[data-ticker-row]'));
    const pillItems = Array.from(pills.querySelectorAll<HTMLElement>('li'));
    const auroraBlobs = Array.from(page.querySelectorAll<HTMLElement>('[data-aurora]'));
    const liveDots = Array.from(page.querySelectorAll<HTMLElement>('[data-live-dot]'));
    const heroBlocks = [brand, subtitle, cta, ticker];

    // 主标题按字入场。拆字依赖 ResizeObserver，jsdom 等环境没有它，
    // 那就退化成整行出现，不为了动画把页面渲染搞挂。
    const splitter = typeof ResizeObserver === 'undefined'
      ? null
      : text.splitText(title, { chars: true, words: false, lines: false });
    const titleParts: HTMLElement[] = splitter?.chars?.length
      ? (splitter.chars as HTMLElement[])
      : [title];

    const disposers: Array<() => void> = [];
    if (splitter) disposers.push(() => splitter.revert());
    const disposeAll = () => {
      for (let i = disposers.length - 1; i >= 0; i -= 1) disposers[i]();
    };

    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      utils.set([...heroBlocks, ...pillItems, title], { opacity: 1, y: 0, scale: 1 });
      utils.set(tickerRows[0], { y: '0%', opacity: 1 });
      return disposeAll;
    }

    // 背景光斑：十几秒一轮的缓慢漂移，让大面积留白「活」着
    auroraBlobs.forEach((blob, index) => {
      const drift = animate(blob, {
        x: [0, index % 2 ? -70 : 80],
        y: [0, index % 2 ? 50 : -60],
        scale: [1, index % 2 ? 1.18 : 0.9],
        duration: 11000 + index * 2600,
        ease: 'inOutSine',
        loop: true,
        alternate: true,
      });
      disposers.push(() => drift.revert());
    });

    // 在岗小绿点：呼吸
    liveDots.forEach((dot, index) => {
      const pulse = animate(dot, {
        scale: [1, 1.6],
        opacity: [1, 0.25],
        duration: 1000,
        ease: 'inOutSine',
        loop: true,
        alternate: true,
        delay: index * 130,
      });
      disposers.push(() => pulse.revert());
    });

    // 主入场：眼眉 → 标题逐字 → 副标题 → 登录 → 在岗播报 → 能力标签
    const intro = createTimeline({ defaults: { duration: 720, ease: 'outQuint' } });
    intro
      .set(title, { opacity: 1 }, 0)
      .add(brand, { opacity: [0, 1], y: [14, 0] }, 0)
      .add(brand, { letterSpacing: ['0.62em', '0.2em'], duration: 1400, ease: 'outExpo' }, 0)
      .add(
        titleParts,
        { opacity: [0, 1], y: ['0.7em', '0em'], duration: 900, delay: stagger(30), ease: 'outExpo' },
        140,
      )
      .add(subtitle, { opacity: [0, 1], y: [16, 0] }, '-=540')
      .add(cta, { opacity: [0, 1], y: [18, 0], scale: [0.94, 1], duration: 820, ease: 'outBack' }, '-=400')
      .add(ticker, { opacity: [0, 1], y: [14, 0] }, '-=540')
      .add(pillItems, { opacity: [0, 1], y: [12, 0], delay: stagger(70) }, '-=620');
    disposers.push(() => intro.revert());

    // 在岗播报：6 条职责循环滚过
    if (tickerRows.length > 1) {
      const loop = createTimeline({ loop: true });
      tickerRows.forEach((row, index) => {
        loop
          .add(
            row,
            { y: ['110%', '0%'], opacity: [0, 1], duration: 560, ease: 'outQuint' },
            index === 0 ? 0 : '+=140',
          )
          .add(row, { y: ['0%', '-110%'], opacity: [1, 0], duration: 470, ease: 'inQuad' }, '+=1500');
      });
      disposers.push(() => loop.revert());
    }

    return disposeAll;
  }, []);

  const inputBaseClass =
    'flex h-[44px] w-full items-center gap-[8px] rounded-[10px] border bg-white px-[16px] transition-colors';
  return (
    <div ref={pageRef} className="relative flex min-h-screen flex-col overflow-x-clip bg-[#fbfbfa]">
      {/* 背景层：光斑 + 淡网格，纯装饰 */}
      <div aria-hidden className="pointer-events-none absolute inset-0 overflow-hidden">
        <div
          data-aurora
          className="absolute -left-[14%] top-[4%] h-[420px] w-[540px] rounded-full bg-[radial-gradient(circle,rgba(15,118,110,0.18),transparent_68%)] blur-[70px]"
        />
        <div
          data-aurora
          className="absolute -right-[12%] top-[22%] h-[460px] w-[520px] rounded-full bg-[radial-gradient(circle,rgba(168,93,50,0.14),transparent_68%)] blur-[80px]"
        />
        <div
          data-aurora
          className="absolute left-[30%] top-[58%] h-[380px] w-[520px] rounded-full bg-[radial-gradient(circle,rgba(111,123,66,0.13),transparent_70%)] blur-[80px]"
        />
        <div className="absolute inset-0 bg-[radial-gradient(rgba(24,24,26,0.05)_1px,transparent_1px)] [background-size:26px_26px] [mask-image:radial-gradient(ellipse_at_50%_36%,black,transparent_72%)]" />
      </div>

      <AppHeader
        className="relative z-10 h-[60px] shrink-0 px-[32px]"
        left={<BrandLogo markSize={28} />}
        right={null}
      />

      <main className="relative z-10 flex flex-1 flex-col items-center px-[32px] pb-[64px]">
        <div className="flex w-full max-w-[1120px] flex-col items-center pt-[52px] text-center">
          {/* 两行做层级化排版：公司名作眼眉（小号、宽字距、弱色）承接品牌，
              产品名作主标题。拆成两个 span 是为了让读屏把公司名与产品名分开读，
              中间的空白文本节点不能省。 */}
          <h1 className="flex flex-col items-center text-center">
            <span
              ref={brandRef}
              style={hiddenFrom(14)}
              className="mr-[-4px] text-[20px] font-medium leading-[30px] tracking-[4px] text-[#757f9c]"
            >
              地中海度假集团
            </span>
            {' '}
            <span
              ref={titleRef}
              style={{ opacity: 0 }}
              className="mt-[10px] text-[40px] font-semibold leading-[52px] tracking-[0.8px] text-[#18181a]"
            >
              AI 数字员工平台
            </span>
          </h1>

          <p
            ref={subtitleRef}
            style={hiddenFrom(16)}
            className="mt-[18px] max-w-[560px] text-[16px] leading-[26px] text-[#5b6274]"
          >
            把经验、流程与判断标准，沉淀成持续在岗的数字员工
          </p>

          <div ref={ctaRef} style={hiddenFrom(18)} className="mt-[26px] flex w-full flex-col items-center">
            {!showForm ? (
              <button
                type="button"
                onClick={() => setShowForm(true)}
                className="flex items-center justify-center rounded-[10px] bg-[#18181a] px-[36px] py-[10px] text-[16px] font-normal text-white transition-colors hover:bg-[#18181a]/90"
              >
                登录
              </button>
            ) : (
              <form
                className="flex w-[320px] flex-col duration-300 ease-out animate-in fade-in slide-in-from-top-4"
                onSubmit={(event) => {
                  event.preventDefault();
                  void login();
                }}
              >
                <div
                  className={`${inputBaseClass} ${usernameError ? 'border-[#f54a45]' : username ? 'border-[#18181a]' : 'border-[#e3e7f1]'}`}
                >
                  <input
                    value={username}
                    autoComplete="username"
                    placeholder="请输入账号"
                    aria-label="账号"
                    onChange={(event) => {
                      setUsername(event.target.value);
                      if (usernameError) setUsernameError('');
                    }}
                    onKeyDown={onFieldKeyDown}
                    className="min-w-0 flex-1 border-0 bg-transparent text-[14px] text-[#18181a] outline-none placeholder:text-[#757f9c]"
                  />
                  {username && (
                    <button
                      type="button"
                      aria-label="清空账号"
                      onClick={() => {
                        setUsername('');
                        setUsernameError('');
                      }}
                      className="grid size-[18px] shrink-0 place-items-center text-[#667085] outline-none transition-colors hover:text-[#464c5e]"
                    >
                      <IconFieldClear className="size-[18px]" />
                    </button>
                  )}
                </div>
                {usernameError && (
                  <p className="mt-[6px] text-[12px] leading-none text-[#f54a45]" role="alert">
                    {usernameError}
                  </p>
                )}

                <div
                  className={`mt-[24px] ${inputBaseClass} ${passwordError ? 'border-[#f54a45]' : password ? 'border-[#18181a]' : 'border-[#e3e7f1]'}`}
                >
                  <input
                    value={password}
                    type={showPassword ? 'text' : 'password'}
                    autoComplete="current-password"
                    placeholder="请输入密码"
                    aria-label="密码"
                    onChange={(event) => {
                      setPassword(event.target.value);
                      if (passwordError) setPasswordError('');
                    }}
                    onKeyDown={onFieldKeyDown}
                    className="min-w-0 flex-1 border-0 bg-transparent text-[14px] text-[#18181a] outline-none placeholder:text-[#757f9c]"
                  />
                  <button
                    type="button"
                    aria-label={showPassword ? '隐藏密码' : '显示密码'}
                    onClick={() => setShowPassword((prev) => !prev)}
                    className="grid size-[18px] shrink-0 place-items-center text-[#677185] outline-none transition-colors hover:text-[#464c5e]"
                  >
                    {showPassword ? (
                      <IconFieldEyeOn className="size-[18px]" />
                    ) : (
                      <IconFieldEye className="size-[18px]" />
                    )}
                  </button>
                </div>
                {passwordError && (
                  <p className="mt-[6px] text-[12px] leading-none text-[#f54a45]" role="alert">
                    {passwordError}
                  </p>
                )}

                <button
                  type="submit"
                  disabled={loading}
                  className="mt-[24px] flex h-[40px] w-[120px] items-center justify-center self-center rounded-[10px] bg-[#18181a] text-[16px] font-normal text-white transition-colors hover:bg-[#18181a]/90 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {loading ? '登录中…' : '登录'}
                </button>
              </form>
            )}
          </div>

          {/* 在岗播报：一次只露一条，逐条滚过 */}
          <div
            ref={tickerRef}
            style={hiddenFrom(14)}
            className="relative mt-[30px] h-[52px] w-full max-w-[520px] overflow-hidden"
          >
            {STAFF_ON_DUTY.map((item) => (
              <div
                key={item.role}
                data-ticker-row
                style={{ opacity: 0, transform: 'translateY(110%)' }}
                className="absolute inset-0 flex items-center gap-[10px] rounded-[12px] border border-black/[0.05] bg-white/85 px-[14px] text-left shadow-[0_6px_20px_rgba(17,17,17,0.05)] backdrop-blur"
              >
                <EmployeeAvatar profile={avatarProfile(item.avatarPreset)} size={34} radius={10} />
                <span className="flex min-w-0 flex-1 items-baseline gap-[8px]">
                  <span className="shrink-0 text-[13px] font-medium text-[#18181a]">
                    {item.role}
                  </span>
                  <span className="truncate text-[12.5px] text-[#757f9c]">{item.duty}</span>
                </span>
                <span className="flex shrink-0 items-center gap-[6px] text-[12px] text-[#138a55]">
                  <i data-live-dot className="size-[6px] rounded-full bg-[#138a55]" />
                  在岗
                </span>
              </div>
            ))}
          </div>

          <ul ref={pillsRef} className="mt-[22px] flex flex-wrap items-center justify-center gap-[10px]">
            {HERO_CAPABILITIES.map((label) => (
              <li
                key={label}
                style={hiddenFrom(12)}
                className="rounded-full border border-black/[0.06] bg-white/70 px-[14px] py-[6px] text-[13px] text-[#464c5e] backdrop-blur"
              >
                {label}
              </li>
            ))}
          </ul>
        </div>

        <LoginProductPreview />
      </main>
    </div>
  );
}
