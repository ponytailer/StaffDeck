import React from 'react';
import { createRoot } from 'react-dom/client';
import panelCss from './panel.css?inline';
import { App } from './App';

/** content script 入口：注入 Shadow DOM，样式与宿主页面完全隔离 */
function bootstrap() {
  if (document.getElementById('staffdeck-assistant-root')) return;
  const host = document.createElement('div');
  host.id = 'staffdeck-assistant-root';
  host.style.all = 'initial';
  const shadow = host.attachShadow({ mode: 'open' });

  const style = document.createElement('style');
  style.textContent = panelCss;
  shadow.appendChild(style);

  const rootEl = document.createElement('div');
  rootEl.className = 'sd-root';
  shadow.appendChild(rootEl);

  document.documentElement.appendChild(host);
  createRoot(rootEl).render(React.createElement(App));
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootstrap, { once: true });
} else {
  bootstrap();
}
