import { useState } from 'react';
import { ApiError, login } from '../../lib/api';
import { syncAuthFromPlatform, type SyncResult } from '../../lib/platform-auth';
import { storage, DEFAULT_API_BASE, DEFAULT_TENANT_ID } from '../../lib/storage';
import type { UserRead } from '../../types';

/** 一键登录失败时的解释：每一种都告诉用户下一步该做什么，而不是干说"失败" */
const SYNC_FAIL_HINT: Record<Extract<SyncResult, { ok: false }>['reason'], string> = {
  'no-platform-session': '没找到平台登录态。请先在浏览器里打开并登录 AIStaff 平台（标签页保持开着），再点这里。',
  expired: '平台登录态已过期，请回平台重新登录后再试。',
  'logged-out': '你之前在这个插件里退出过登录，请用账号密码登录。',
  invalid: '平台登录态已失效，请回平台重新登录后再试。',
  unreachable: '读不到平台标签页（可能刚被关闭），可以直接用账号密码登录。',
};

export function LoginView(props: { onLoggedIn: (u: UserRead) => void; notice: string }) {
  const [apiBase, setApiBase] = useState('');
  const [tenantId, setTenantId] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [remember, setRemember] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [syncHint, setSyncHint] = useState('');

  /** 主动再试一次"接管平台登录态"（自动同步没成功时的兜底入口） */
  const syncNow = async () => {
    setError('');
    setSyncHint('');
    setSyncing(true);
    try {
      const res = await syncAuthFromPlatform();
      if (res.ok) {
        props.onLoggedIn(res.user);
        return;
      }
      setSyncHint(SYNC_FAIL_HINT[res.reason]);
    } finally {
      setSyncing(false);
    }
  };

  const submit = async () => {
    setError('');
    const base = (apiBase.trim() || (await storage.getApiBase())).replace(/\/+$/, '');
    const tenant = tenantId.trim() || (await storage.getTenantId());
    if (!username.trim() || !password) {
      setError('请输入账号和密码');
      return;
    }
    setBusy(true);
    try {
      const result = await login({
        tenantId: tenant,
        username: username.trim(),
        password,
        remember,
      });
      // 登录成功才持久化服务地址与租户，避免失败值被缓存
      await storage.setApiBase(base);
      await storage.setTenantId(tenant);
      await storage.setToken(result.token);
      await storage.setUser(result.user);
      await storage.setExpiresAt(result.expires_at);
      props.onLoggedIn(result.user);
    } catch (e) {
      if (e instanceof ApiError) {
        setError(e.status === 401 || e.status === 403 ? '账号或密码错误' : e.detail || '登录失败，请稍后重试');
      } else {
        setError('无法连接服务器，请检查服务地址');
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="sd-login-head">
        <div className="sd-eyebrow">AIStaff Assistant</div>
        <h2>把数字员工<br />装进每一个网页</h2>
        <p>登录 AIStaff 企业账号，与你配置的数字员工和小队随时对话。</p>
      </div>

      {props.notice && <div className="sd-error">{props.notice}</div>}
      {error && <div className="sd-error">{error}</div>}

      <div className="sd-sync">
        <button className="sd-btn sd-btn-ghost" disabled={syncing} onClick={() => void syncNow()}>
          {syncing ? '正在读取平台登录态…' : '用平台登录态一键登录'}
        </button>
        <div className="sd-sync-hint">
          {syncHint || '浏览器里已登录 AIStaff 平台，插件会自动接管登录态，无需再输密码。'}
        </div>
      </div>

      <div className="sd-divider"><span>或使用账号密码</span></div>

      <div className="sd-field">
        <label>账号</label>
        <input
          type="text"
          placeholder="用户名"
          value={username}
          autoComplete="off"
          onChange={(e) => setUsername(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && void submit()}
        />
      </div>
      <div className="sd-field">
        <label>密码</label>
        <input
          type="password"
          placeholder="••••••••"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && void submit()}
        />
      </div>

      <label className="sd-field" style={{ display: 'flex', alignItems: 'center', gap: 7, cursor: 'pointer' }}>
        <input
          type="checkbox"
          checked={remember}
          onChange={(e) => setRemember(e.target.checked)}
          style={{ width: 14, height: 14, accentColor: '#0f766e' }}
        />
        <span style={{ fontSize: 12, fontWeight: 400, color: '#6d726e' }}>记住账号（30 天内免登录）</span>
      </label>

      <details className="sd-login-advanced">
        <summary>服务器与租户</summary>
        <div className="sd-field">
          <label>服务地址</label>
          <input
            type="text"
            placeholder={DEFAULT_API_BASE}
            value={apiBase}
            onChange={(e) => setApiBase(e.target.value)}
          />
        </div>
        <div className="sd-field">
          <label>租户 ID</label>
          <input
            type="text"
            placeholder={DEFAULT_TENANT_ID}
            value={tenantId}
            onChange={(e) => setTenantId(e.target.value)}
          />
        </div>
      </details>

      <button className="sd-btn-primary" disabled={busy} onClick={() => void submit()}>
        {busy ? '登录中…' : '登 录'}
      </button>
      <div className="sd-login-foot">登录凭证以 JWT 形式保存在浏览器本地，不会发送给任何第三方</div>
    </>
  );
}
