"""OA 操作录制器（临时工具）：注入监听记录用户真实操作，供分析点击提交机制。

记录内容（/tmp/oa_recorded.json，每 2s 落盘）：
- clicks: 每次 mousedown 的坐标 + 目标元素（tag/class/text/祖先链/所属 fieldmark）
- keys:   关键按键（Backspace/Enter/ArrowDown…）
- state:  每 1s 的 field7012 hidden/span 快照（观察提交时机）
运行 300s 后自动退出；也可提前 pkill。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

for _var in ("NO_PROXY", "no_proxy"):
    _hosts = {h.strip() for h in os.environ.get(_var, "").split(",") if h.strip()}
    _hosts.update({"127.0.0.1", "localhost", "::1"})
    os.environ[_var] = ",".join(sorted(_hosts))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

OUT = Path("/tmp/oa_recorded.json")
DATA: dict[str, list] = {"clicks": [], "keys": [], "state": []}

INJECT_JS = """
() => {
  if (window.__recorder_installed) return 'already';
  window.__recorder_installed = true;
  const describe = (el) => {
    const chain = [];
    let n = el, fm = '';
    for (let i = 0; i < 8 && n && n.tagName !== 'BODY'; i++) {
      const c = (n.className || '').toString().split(' ').slice(0, 2).join('.');
      if (n.getAttribute && n.getAttribute('data-fieldmark')) fm = n.getAttribute('data-fieldmark');
      chain.push(n.tagName + (c ? '.' + c : ''));
      n = n.parentElement;
    }
    return {tag: el.tagName, cls: (el.className || '').toString().slice(0, 70),
            text: (el.innerText || '').trim().slice(0, 50), id: el.id || '', fieldmark: fm, chain};
  };
  document.addEventListener('mousedown', (e) => {
    const el = document.elementFromPoint(e.clientX, e.clientY) || e.target;
    const d = describe(el);
    d.x = e.clientX; d.y = e.clientY; d.t = Date.now();
    window.__rec_clicks = window.__rec_clicks || [];
    window.__rec_clicks.push(d);
  }, true);
  document.addEventListener('keydown', (e) => {
    if (!['Backspace', 'Enter', 'ArrowDown', 'ArrowUp', 'Escape', 'Tab'].includes(e.key)) return;
    window.__rec_keys = window.__rec_keys || [];
    window.__rec_keys.push({key: e.key, target: describe(e.target).cls, t: Date.now()});
  }, true);
  return 'ok';
}
"""

POLL_JS = """
() => {
  const h = document.querySelector('#field7012');
  const s = document.querySelector('#field7012span');
  return {hidden: h ? h.value : null, span: s ? (s.innerText || '').trim().slice(0, 40) : null};
}
"""


def main() -> None:
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=5000)
    page = next(p for p in browser.contexts[0].pages if "workflow/req" in (p.url or ""))
    frames = [f for f in page.frames if f is not None]
    for f in frames:
        try:
            f.evaluate(INJECT_JS)
        except Exception:
            pass
    print(f"已注入 {len(frames)} 个 frame，开始录制 300s（每 2s 落盘）…")
    deadline = time.time() + 300
    last_flush = 0.0
    last_state = time.time()
    while time.time() < deadline:
        time.sleep(0.5)
        for f in frames:
            try:
                clicks = f.evaluate("() => window.__rec_clicks || []")
                keys = f.evaluate("() => window.__rec_keys || []")
                if clicks:
                    f.evaluate("() => { window.__rec_clicks = []; }")
                    DATA["clicks"].extend(clicks)
                if keys:
                    f.evaluate("() => { window.__rec_keys = []; }")
                    DATA["keys"].extend(keys)
            except Exception:
                pass
        if time.time() - last_state >= 1.0:
            last_state = time.time()
            try:
                snap = page.frames[0].evaluate(POLL_JS) if False else None
            except Exception:
                snap = None
            for f in frames:
                try:
                    snap = f.evaluate(POLL_JS)
                    break
                except Exception:
                    continue
            if snap and (snap.get("hidden") or snap.get("span")):
                DATA["state"].append({"t": time.time(), **snap})
        if time.time() - last_flush >= 2.0:
            last_flush = time.time()
            OUT.write_text(json.dumps(DATA, ensure_ascii=False, indent=1), encoding="utf-8")
    OUT.write_text(json.dumps(DATA, ensure_ascii=False, indent=1), encoding="utf-8")
    print("录制结束")
    pw.stop()


if __name__ == "__main__":
    main()
