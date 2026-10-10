"""OA 表单探查工具（内部使用，不暴露给终端用户）。

用途：新增报销场景/字段时，把 OA 表单上的字段与组件关系一次性摸清，
供把 selector 补进 config/expense_workflow.json 的 field_library。

用法（Chrome 需以调试端口 9222 运行，且已在窗口里登录 OA）：
    # 场景 1：探查 config 里某个场景的 URL
    .venv/bin/python scripts/probe_oa_form.py --scenario communication

    # 场景 2：用户已手工打开目标表单页，直接探查当前标签页
    .venv/bin/python scripts/probe_oa_form.py --pick fosunholiday

    # 场景 3：给一个新 URL（如菜单里复制的新流程地址）
    .venv/bin/python scripts/probe_oa_form.py --url "https://oa.fosunholiday.com/spa/..."

可选参数：
    --wait-login 120   若当前是登录页，轮询等待人工登录完成（秒，0=不等待）
    --out /tmp/oa_dump.json    结构化 dump 输出路径
    --shot /tmp/oa_form.png    整页截图路径

输出（JSON）：
    frames[]: 每个 iframe 的
      - fields: 可见 input/textarea/select（id/name 建议定位器 + 所在行文字）
      - fieldmarks: data-fieldmark 自定义组件（e-cology 的下拉/上传/联想框）
      - buttons: 可见按钮（文字 + 建议定位器）
      - selects: ant-select / wea-browser 组件计数（判断联想下拉类型）
明细行字段用「所在行文字 + 列头」对齐认列；label 列做法见 dump 里 row/header 字段。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# playwright 的 CDP 请求指向 localhost，必须绕过系统代理（HTTP_PROXY 劫持回环是老坑）
for _var in ("NO_PROXY", "no_proxy"):
    _hosts = {h.strip() for h in os.environ.get(_var, "").split(",") if h.strip()}
    _hosts.update({"127.0.0.1", "localhost", "::1"})
    os.environ[_var] = ",".join(sorted(_hosts))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

CDP = "http://127.0.0.1:9222"

# 与 config 的 login_check 同口径：命中即认为还没登录
_LOGIN_URL_HINTS = ("logon", "login")
_LOGIN_SELECTORS = ["input[name='loginid']", "input[placeholder*='账号']", "#loginid"]

_DUMP_JS = """
() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  const cssSel = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
    const cls = (el.getAttribute('class') || '').trim().split(/\\s+/).filter(Boolean);
    const base = el.tagName.toLowerCase();
    return cls.length ? base + '.' + CSS.escape(cls[0]) : base;
  };
  const rowText = (el) => {
    const tr = el.closest('tr');
    return tr ? (tr.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 110) : '';
  };
  const headerText = (el) => {
    const td = el.closest('td');
    if (!td) return '';
    let cell = td.previousElementSibling;
    const chain = [];
    for (let i = 0; i < 8 && cell; i++) { chain.unshift((cell.innerText||'').trim()); cell = cell.previousElementSibling; }
    return chain.filter(Boolean).join(' | ').slice(0, 150);
  };

  const fields = [];
  for (const el of document.querySelectorAll('input, textarea, select')) {
    if (!vis(el) && el.type !== 'file') continue;
    fields.push({
      tag: el.tagName.toLowerCase(), type: el.type || '',
      id: el.id || '', name: el.name || '',
      selector: el.id ? '#' + CSS.escape(el.id) : cssSel(el),
      placeholder: el.getAttribute('placeholder') || '',
      value: (el.value || '').slice(0, 60),
      readonly: !!el.readOnly,
      row: rowText(el), header: headerText(el),
    });
  }

  const fieldmarks = [];
  for (const el of document.querySelectorAll('[data-fieldmark]')) {
    if (!vis(el)) continue;
    const marks = new Set();
    let node = el;
    while (node) { const m = node.getAttribute && node.getAttribute('data-fieldmark'); if (m) marks.add(m); node = node.parentElement; }
    fieldmarks.push({
      fieldmark: el.getAttribute('data-fieldmark'),
      fieldname: el.getAttribute('data-fieldname') || '',
      chain: Array.from(marks),
      text: (el.innerText || '').trim().slice(0, 60),
      row: rowText(el), header: headerText(el),
      has_select: !!el.querySelector('.ant-select'),
      has_file_input: !!el.querySelector('input[type=file]'),
    });
  }

  const buttons = [];
  for (const el of document.querySelectorAll('button, a.btn, .wea-btn, [class*=btn]')) {
    if (!vis(el)) continue;
    const text = (el.innerText || '').trim();
    if (!text && !el.title) continue;
    buttons.push({ text: text.slice(0, 30) || el.title, selector: cssSel(el), cls: (el.getAttribute('class') || '').slice(0, 70) });
  }

  const selects = {
    ant_select: document.querySelectorAll('.ant-select').length,
    wea_browser: document.querySelectorAll('.wea-browser, [class*=wea-browser]').length,
    wea_upload: document.querySelectorAll('.wea-upload, [class*=upload]').length,
  };

  return {
    url: location.href.slice(0, 160), title: document.title,
    fields, fieldmarks, buttons: buttons.slice(0, 25), selects,
  };
}
"""


def _is_login_page(page) -> bool:
    url = (page.url or "").lower()
    if any(h in url for h in _LOGIN_URL_HINTS) and "static4form" not in url:
        return True
    for frame in page.frames:
        try:
            for sel in _LOGIN_SELECTORS:
                if frame.locator(sel).first.is_visible(timeout=300):
                    return True
        except Exception:
            continue
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="OA 表单字段/组件探查（内部工具）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scenario", help="探查 config/expense_workflow.json 里的指定场景 URL")
    group.add_argument("--pick", help="在已打开标签页里按 URL 关键字挑一个探查（如 fosunholiday）")
    group.add_argument("--url", help="直接给目标页面 URL（会新开标签页）")
    parser.add_argument("--wait-login", type=int, default=180, help="登录页时轮询等待人工登录的秒数")
    parser.add_argument("--out", default="/tmp/oa_dump.json", help="JSON dump 输出路径")
    parser.add_argument("--shot", default="/tmp/oa_form.png", help="整页截图路径")
    args = parser.parse_args()

    config_url = None
    if args.scenario:
        config = json.loads(
            (Path(__file__).resolve().parent.parent / "config" / "expense_workflow.json").read_text(encoding="utf-8")
        )
        scenario = next((s for s in config["scenarios"] if s["id"] == args.scenario), None)
        if not scenario:
            print(f"场景 {args.scenario} 不存在，可选：{[s['id'] for s in config['scenarios']]}")
            return 1
        config_url = scenario["url"]

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(CDP, timeout=5000)
    except Exception as exc:
        print(f"CDP 连不上（{CDP}）：{exc}")
        print('请先以调试端口启动 Chrome：open -na "Google Chrome" --args --remote-debugging-port=9222 '
              '--user-data-dir="$HOME/.oa-chrome-debug" --no-sandbox --disable-gpu')
        pw.stop()
        return 1

    context = browser.contexts[0]
    if args.url:
        page = context.new_page()
        page.goto(args.url, timeout=60000, wait_until="domcontentloaded")
    elif args.pick:
        pages = [p for p in context.pages if args.pick.lower() in (p.url or "").lower()]
        if not pages:
            print(f"没有 URL 含「{args.pick}」的标签页。当前标签页：")
            for p in context.pages:
                print("  -", (p.url or "")[:100])
            pw.stop()
            return 1
        page = pages[0]
    else:
        pages = [p for p in context.pages if "fosunholiday" in (p.url or "")]
        page = pages[0] if pages else context.new_page()
        if not pages:
            assert config_url
            page.goto(config_url, timeout=60000, wait_until="domcontentloaded")

    if _is_login_page(page):
        if args.wait_login <= 0:
            print("当前是 OA 登录页（--wait-login 0 不等待）。请先登录后重跑。")
            pw.stop()
            return 2
        print(f"当前是登录页，请在 Chrome 窗口登录（最多等 {args.wait_login}s）…")
        deadline = time.time() + args.wait_login
        while time.time() < deadline:
            time.sleep(3)
            if not _is_login_page(page):
                break
        else:
            print("等待登录超时。")
            pw.stop()
            return 2
        print("已登录。")

    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    time.sleep(2)

    dump = {"url": page.url, "frames": []}
    for frame in page.frames:
        if frame is page.main_frame and "static4form" in (page.url or ""):
            continue  # 表单壳页本身没有内容
        try:
            data = frame.evaluate(_DUMP_JS)
        except Exception:
            continue
        if data["fields"] or data["fieldmarks"] or data["buttons"]:
            dump["frames"].append(data)

    Path(args.out).write_text(json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        page.screenshot(path=args.shot)
    except Exception as exc:
        print(f"截图失败（不影响 dump）：{exc}")

    print(f"dump → {args.out}")
    print(f"截图 → {args.shot}")
    for fr in dump["frames"]:
        print(f"\n== frame {fr['url'][:90]}")
        print(f"   fields={len(fr['fields'])} fieldmarks={len(fr['fieldmarks'])} buttons={len(fr['buttons'])} selects={fr['selects']}")

    pw.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
