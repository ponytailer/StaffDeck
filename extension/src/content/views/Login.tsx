import { useState } from 'react';
import { ApiError, login } from '../../lib/api';
import { storage, DEFAULT_API_BASE, DEFAULT_TENANT_ID } from '../../lib/storage';
import type { UserRead } from '../../types';

export function LoginView(props: { onLoggedIn: (u: UserRead) => void; notice: string }) {
  const [apiBase, setApiBase] = useState('');
  const [tenantId, setTenantId] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [remember, setRemember] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

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
        <div className="sd-eyebrow">StaffDeck Assistant</div>
        <h2>把数字员工<br />装进每一个网页</h2>
        <p>登录 StaffDeck 企业账号，与你配置的数字员工和小队随时对话。</p>
      </div>

      {props.notice && <div className="sd-error">{props.notice}</div>}
      {error && <div className="sd-error">{error}</div>}

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
