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

    // Firefox 走 AMO 签名的 .xpi，必须是可点的真实链接
    const firefoxLink = screen.getByText(/下载 Firefox 版/).closest('a');
    expect(firefoxLink).not.toBeNull();
    expect(firefoxLink?.getAttribute('href')).toContain('.xpi');
  });

  it('默认展示 Chrome 的安装步骤，并说明只支持 Chromium 系', () => {
    renderPage();

    expect(screen.getByText('开启开发者模式并加载')).toBeTruthy();
    expect(screen.getAllByText(/chrome:\/\/extensions/).length).toBeGreaterThan(0);
    expect(screen.queryByText('拖进 Firefox 窗口安装')).toBeNull();
  });

  it('切到 Firefox 后换成 .xpi 安装步骤，并提示授权与签名提示', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole('button', { name: 'Firefox' }));

    expect(await screen.findByText('拖进 Firefox 窗口安装')).toBeTruthy();
    // 已给签名 xpi，不应再引导用户去临时载入
    expect(screen.queryByText(/about:debugging/)).toBeNull();
    expect(screen.getAllByText(/about:addons/).length).toBeGreaterThan(0);
    expect(screen.queryByText('开启开发者模式并加载')).toBeNull();
    // 两个坑必须写出来：授权所有网站 + 未签名/未监控提示
    expect(screen.getAllByText(/允许访问所有网站的数据/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Mozilla 主动监控范围/).length).toBeGreaterThan(0);
  });
});
