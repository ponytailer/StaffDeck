import { useCallback, useEffect, useRef, useState } from 'react';
import { storage } from '../lib/storage';
import type { AgentProfileRead, UserRead } from '../types';
import { LoginView } from './views/Login';
import { AgentListView } from './views/AgentList';
import { ChatView } from './views/Chat';
import { OaView } from './views/OaView';

const BALL_SIZE = 56;
const PANEL_WIDTH = 396;

type View = 'login' | 'list' | 'oa' | 'chat';

export function App() {
  const [view, setView] = useState<View>('login');
  const [open, setOpen] = useState(false);
  const [loggedOut, setLoggedOut] = useState(true);
  const [user, setUser] = useState<UserRead | null>(null);
  const [activeAgent, setActiveAgent] = useState<AgentProfileRead | null>(null);
  const [loginNotice, setLoginNotice] = useState('');

  const ballRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const sessionsRef = useRef<Map<string, string>>(new Map());

  /* ---- 初始化：恢复登录态与悬浮球位置 ---- */
  useEffect(() => {
    (async () => {
      const [token, expiresAt, storedUser, pos] = await Promise.all([
        storage.getToken(),
        storage.getExpiresAt(),
        storage.getUser(),
        storage.getBallPos(),
      ]);
      const expired = Boolean(expiresAt && Date.now() > expiresAt * 1000);
      if (token && !expired && storedUser) {
        setUser(storedUser);
        setLoggedOut(false);
        setView('list');
      } else if (expired) {
        await storage.clearSession();
      }
      if (ballRef.current) {
        const x = pos ? Math.min(Math.max(0, pos.x), window.innerWidth - BALL_SIZE) : window.innerWidth - BALL_SIZE - 26;
        const y = pos ? Math.min(Math.max(0, pos.y), window.innerHeight - BALL_SIZE) : window.innerHeight - BALL_SIZE - 26;
        ballRef.current.style.left = `${x}px`;
        ballRef.current.style.top = `${y}px`;
      }
    })();
  }, []);

  /* ---- 悬浮球拖拽 / 点击 ---- */
  const dragState = useRef({ active: false, moved: false, sx: 0, sy: 0, ox: 0, oy: 0 });

  const onBallPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const ball = ballRef.current;
    if (!ball) return;
    dragState.current = {
      active: true, moved: false,
      sx: e.clientX, sy: e.clientY,
      ox: ball.offsetLeft, oy: ball.offsetTop,
    };
    ball.setPointerCapture(e.pointerId);
  }, []);

  const onBallPointerMove = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const st = dragState.current;
    const ball = ballRef.current;
    if (!st.active || !ball) return;
    const dx = e.clientX - st.sx;
    const dy = e.clientY - st.sy;
    if (!st.moved && Math.abs(dx) + Math.abs(dy) > 5) {
      st.moved = true;
      ball.classList.add('dragging');
    }
    if (st.moved) {
      const nx = Math.min(Math.max(0, st.ox + dx), window.innerWidth - BALL_SIZE);
      const ny = Math.min(Math.max(0, st.oy + dy), window.innerHeight - BALL_SIZE);
      ball.style.left = `${nx}px`;
      ball.style.top = `${ny}px`;
    }
  }, []);

  const onBallPointerUp = useCallback(() => {
    const st = dragState.current;
    const ball = ballRef.current;
    if (!st.active) return;
    ball?.classList.remove('dragging');
    if (!st.moved) {
      setOpen((prev) => !prev);
    } else if (ball) {
      void storage.setBallPos({ x: ball.offsetLeft, y: ball.offsetTop });
    }
    dragState.current = { active: false, moved: false, sx: 0, sy: 0, ox: 0, oy: 0 };
  }, []);

  /* ---- 面板拖动：按住窗口空白处（非交互控件、非滚动内容区）移动位置 ---- */
  // 交互控件（按钮/输入框/链接等）仍要能正常点击，聊天与列表的滚动区要能选字滚动，
  // 所以这两类目标不参与拖动；其余空白区域（顶栏、卡片间隙、登录页留白）都可当抓手。
  const panelDrag = useRef({
    active: false, pointerId: -1, sx: 0, sy: 0, ox: 0, oy: 0, moved: false,
  });
  // 用户手动拖过窗口后记住落点：再次打开就还在那儿（本页内有效，换页/刷新回到贴球弹出）
  const panelPosRef = useRef<{ x: number; y: number } | null>(null);

  const onPanelPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const panel = panelRef.current;
    if (!panel || e.button !== 0) return;
    const target = e.target as HTMLElement;
    if (
      target.closest(
        'button, a, input, textarea, select, label, summary, [contenteditable="true"], .sd-chat-scroll, .sd-list-scroll',
      )
    ) {
      return;
    }
    panelDrag.current = {
      active: true, pointerId: e.pointerId,
      sx: e.clientX, sy: e.clientY,
      ox: panel.offsetLeft, oy: panel.offsetTop, moved: false,
    };
    try {
      panel.setPointerCapture(e.pointerId);
    } catch {
      // 少数环境（指针已释放/合成事件）不支持捕获：退化为普通事件流，拖动仍可用
    }
  }, []);

  const onPanelPointerMove = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const st = panelDrag.current;
    const panel = panelRef.current;
    if (!st.active || !panel || e.pointerId !== st.pointerId) return;
    const dx = e.clientX - st.sx;
    const dy = e.clientY - st.sy;
    if (!st.moved && Math.abs(dx) + Math.abs(dy) > 4) {
      st.moved = true;
      panel.classList.add('dragging');
    }
    if (!st.moved) return;
    // 整窗保持可见：不拖出屏幕（右下边距按实际尺寸算，窗口缩放后同样成立）
    const maxX = Math.max(8, window.innerWidth - panel.offsetWidth - 8);
    const maxY = Math.max(8, window.innerHeight - panel.offsetHeight - 8);
    panel.style.left = `${Math.min(Math.max(8, st.ox + dx), maxX)}px`;
    panel.style.top = `${Math.min(Math.max(8, st.oy + dy), maxY)}px`;
  }, []);

  const onPanelPointerUp = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const st = panelDrag.current;
    const panel = panelRef.current;
    if (!st.active) return;
    panel?.classList.remove('dragging');
    if (panel?.hasPointerCapture(e.pointerId)) panel.releasePointerCapture(e.pointerId);
    if (panel && st.moved) panelPosRef.current = { x: panel.offsetLeft, y: panel.offsetTop };
    panelDrag.current = {
      active: false, pointerId: -1, sx: 0, sy: 0, ox: 0, oy: 0, moved: false,
    };
  }, []);

  /* ---- 面板开合：贴球弹出，越界翻转 ---- */
  useEffect(() => {
    const ball = ballRef.current;
    const panel = panelRef.current;
    if (!ball || !panel) return;
    if (open) {
      const height = Math.min(640, window.innerHeight - 48);
      const bx = ball.offsetLeft;
      const by = ball.offsetTop;
      const dragged = panelPosRef.current;
      let px: number;
      let py: number;
      if (dragged) {
        // 用户拖过 → 回到落点（窗口变小后同样夹回可视区）
        px = Math.min(Math.max(8, dragged.x), Math.max(8, window.innerWidth - PANEL_WIDTH - 8));
        py = Math.min(Math.max(8, dragged.y), Math.max(8, window.innerHeight - height - 8));
      } else {
        px = bx + BALL_SIZE + 14;
        py = by + BALL_SIZE - height;
        if (px + PANEL_WIDTH > window.innerWidth - 12) px = bx - PANEL_WIDTH - 14;
        if (px < 12) px = Math.max(12, (window.innerWidth - PANEL_WIDTH) / 2);
        py = Math.min(Math.max(24, py), window.innerHeight - height - 24);
      }
      panel.style.left = `${px}px`;
      panel.style.top = `${py}px`;
      panel.style.transformOrigin = px < bx ? 'bottom right' : 'bottom left';
      ball.classList.add('hidden');
    } else {
      ball.classList.remove('hidden');
    }
  }, [open]);

  /* ---- Esc / 点击外部关闭 ---- */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    const onOutside = (e: PointerEvent) => {
      if (!open) return;
      const path = e.composedPath() as Node[];
      if (ballRef.current?.contains(path[0]) || panelRef.current?.contains(path[0])) return;
      setOpen(false);
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('pointerdown', onOutside, true);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('pointerdown', onOutside, true);
    };
  }, [open]);

  /* ---- 登录/登出/过期 ---- */
  const handleLoggedIn = useCallback((u: UserRead) => {
    setUser(u);
    setLoggedOut(false);
    setLoginNotice('');
    setView('list');
  }, []);

  const handleLogout = useCallback(async () => {
    await storage.clearSession();
    setUser(null);
    setLoggedOut(true);
    setActiveAgent(null);
    sessionsRef.current.clear();
    setView('login');
    setOpen(false);
  }, []);

  const handleAuthExpired = useCallback(() => {
    void storage.clearSession();
    setUser(null);
    setLoggedOut(true);
    setActiveAgent(null);
    setLoginNotice('会话已过期，请重新登录');
    setView('login');
  }, []);

  const openAgent = useCallback((agent: AgentProfileRead) => {
    setActiveAgent(agent);
    setView('chat');
  }, []);

  const getSessionId = useCallback((agentId: string) => sessionsRef.current.get(agentId) ?? null, []);
  const saveSessionId = useCallback((agentId: string, sessionId: string) => {
    sessionsRef.current.set(agentId, sessionId);
  }, []);

  return (
    <>
      <div
        ref={ballRef}
        className={`sd-ball${loggedOut ? ' logged-out' : ''}${open ? ' hidden' : ''}`}
        onPointerDown={onBallPointerDown}
        onPointerMove={onBallPointerMove}
        onPointerUp={onBallPointerUp}
        title="AIStaff 浏览器助手"
      >
        <svg viewBox="0 0 24 24" fill="none">
          <path d="M12 2.5L3.5 7v10L12 21.5 20.5 17V7L12 2.5z" stroke="#fff" strokeWidth="1.6" strokeLinejoin="round" />
          <path d="M12 7.5L7.5 10v4l4.5 2.5 4.5-2.5v-4L12 7.5z" fill="#fff" fillOpacity="0.9" />
        </svg>
        <span className="sd-dot" />
      </div>

      <div
        ref={panelRef}
        className={`sd-panel${open ? ' open' : ''}`}
        onPointerDown={onPanelPointerDown}
        onPointerMove={onPanelPointerMove}
        onPointerUp={onPanelPointerUp}
        onPointerCancel={onPanelPointerUp}
      >
        {/* 登录 */}
        <div className={`sd-view sd-login${view === 'login' ? ' active' : ''}`}>
          <LoginView onLoggedIn={handleLoggedIn} notice={loginNotice} />
        </div>

        {/* 员工列表（tab: 员工） */}
        <div className={`sd-view${view === 'list' ? ' active' : ''}`}>
          {user && (
            <AgentListView
              user={user}
              onOpenAgent={openAgent}
              onTab={(tab) => setView(tab === 'agents' ? 'list' : 'oa')}
              onLogout={handleLogout}
              onCollapse={() => setOpen(false)}
            />
          )}
        </div>

        {/* OA 单据助手（tab: 单据助手） */}
        <div className={`sd-view${view === 'oa' ? ' active' : ''}`}>
          {user && (
            <OaView
              user={user}
              onTab={(tab) => setView(tab === 'agents' ? 'list' : 'oa')}
              onLogout={handleLogout}
              onCollapse={() => setOpen(false)}
            />
          )}
        </div>

        {/* 聊天 */}
        <div className={`sd-view${view === 'chat' ? ' active' : ''}`}>
          {activeAgent && (
            <ChatView
              key={activeAgent.id}
              agent={activeAgent}
              getSessionId={getSessionId}
              saveSessionId={saveSessionId}
              onBack={() => setView('list')}
              onCollapse={() => setOpen(false)}
              onAuthExpired={handleAuthExpired}
            />
          )}
        </div>
      </div>
    </>
  );
}
