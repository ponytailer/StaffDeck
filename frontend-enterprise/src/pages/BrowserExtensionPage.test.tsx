// @vitest-environment jsdom

import { createElement } from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it } from 'vitest';

import { I18nProvider } from '@/i18n';

import BrowserExtensionPage from './BrowserExtensionPage';

const demoUser = {
  id: 'user_demo',
  tenant_id: 'tenant_demo',
  username: 'demo',
  display_name: '演示账号',
  role: 'admin',
} as never;

function renderPage() {
  return render(
    createElement(
      MemoryRouter,
      null,
      createElement(I18nProvider, null, createElement(BrowserExtensionPage, { currentUser: demoUser })),
    ),
  );
}

describe('BrowserExtensionPage', () => {
  afterEach(() => cleanup());

  it('同时给出 Chrome / Edge 与 Firefox 两个下载入口', () => {
    renderPage();

    const chromeLink = screen.getByText('下载 Chrome / Edge 版').closest('a');
    expect(chromeLink).not.toBeNull();
    expect(chromeLink?.getAttribute('href')).toContain('aistaff-extension.zip');

    // Firefox 包未上传时不能是死链，要走「上传后开放」的禁用态
    const firefoxEntry = screen.getByText(/下载 Firefox 版/);
    expect(firefoxEntry.closest('a')).toBeNull();
    expect(firefoxEntry.textContent).toContain('上传后开放');
  });

  it('默认展示 Chrome 的安装步骤，并说明只支持 Chromium 系', () => {
    renderPage();

    expect(screen.getByText('开启开发者模式并加载')).toBeTruthy();
    expect(screen.getAllByText(/chrome:\/\/extensions/).length).toBeGreaterThan(0);
    expect(screen.queryByText('临时载入附加组件')).toBeNull();
  });

  it('切到 Firefox 后换成 about:debugging 步骤，并提示临时载入的权限与失效问题', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole('button', { name: 'Firefox' }));

    expect(await screen.findByText('临时载入附加组件')).toBeTruthy();
    expect(screen.getAllByText(/about:debugging/).length).toBeGreaterThan(0);
    expect(screen.queryByText('开启开发者模式并加载')).toBeNull();
    // Firefox 独有的两个坑必须写出来：授权所有网站 + 临时载入重启失效
    expect(screen.getAllByText(/允许访问所有网站的数据/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/关闭浏览器后失效/).length).toBeGreaterThan(0);
  });
});
