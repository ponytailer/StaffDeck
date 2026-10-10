"""OA 浏览器自动化核心（报销流程助手）。

设计要点：
- **优先 CDP 连接当前浏览器**：配置 `browser.cdp_endpoint`（如 http://127.0.0.1:9222）后，
  通过 Chrome DevTools Protocol attach 到用户已经打开的浏览器 —— 完整复用既有 cookie/登录态，
  且能直接复用已打开的 OA 标签页。前提：Chrome 以 `--remote-debugging-port=9222` 启动
  （macOS：完全退出 Chrome 后执行 `open -na "Google Chrome" --args --remote-debugging-port=9222`）。
  连不上时自动退回独立窗口模式。
- **独立窗口兜底**：`launch_persistent_context` 带 user_data_dir，登录态（cookie）落在磁盘上；
  浏览器进程在多次请求之间保持打开 —— 「检测到登录页 → 用户在浏览器里手动登录 →
  回页面点重试」的交互依赖这一点（重试时直接复用同一个已登录的页面）。
- **单线程执行器**：playwright sync API 要求所有操作在创建它的同一线程上跑；
  用 max_workers=1 的 ThreadPoolExecutor 串行化所有浏览器操作，FastAPI 的
  sync 端点（跑在 anyio 线程池里）全部把任务丢给它。
- **配置驱动**：所有定位器/URL/登录判定都来自 `config/expense_workflow.json`；
  selector 为 `__TODO__` 的字段跳过不填，只在结果里标注「待配置」，不报错。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

TODO_SELECTOR = "__TODO__"

# chrome_path 未配置/不存在时的自动探测候选（跨平台常见安装位置，按序取第一个存在的）。
_CHROME_CANDIDATES: dict[str, list[str]] = {
    "darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ],
    "win32": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"~\AppData\Local\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ],
    "linux": [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge",
    ],
}

# 配置文件路径：backend/config/expense_workflow.json
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "expense_workflow.json"
# 浏览器 profile / 附件落盘目录（相对 backend/，跟随仓库走）
_BACKEND_ROOT = Path(__file__).resolve().parents[2]


class OaBrowserError(RuntimeError):
    """浏览器自动化链路上的可读错误（直接透给前端）。"""


def load_expense_config() -> dict[str, Any]:
    """读配置文件；文件缺失/损坏抛 OaBrowserError（配置是本功能的事实来源）。"""
    if not _CONFIG_PATH.exists():
        raise OaBrowserError(f"缺少配置文件 {_CONFIG_PATH}")
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OaBrowserError(f"配置文件 {_CONFIG_PATH.name} 不是合法 JSON：{exc}") from exc


def resolve_user_path(raw: str) -> Path:
    """展开 ~ 与相对路径（相对 backend/）。"""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (_BACKEND_ROOT / path)


def _find_chrome_binary(explicit: str) -> Path | None:
    """定位可用的浏览器可执行文件：配置显式路径 → PATH 上的命令 → 常见安装位置 →
    playwright 自带 chromium。全找不到返回 None（由调用方给出可操作提示）。"""
    import shutil
    import sys

    if explicit:
        path = resolve_user_path(explicit)
        if path.exists():
            return path
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return Path(found)
    for raw in _CHROME_CANDIDATES.get(sys.platform, []):
        path = Path(raw).expanduser()
        if path.exists():
            return path
    try:
        bundled = Path(_STATE.pw.chromium.executable_path)
        if bundled.exists():
            return bundled
    except Exception:
        pass
    return None


def _no_browser_error() -> OaBrowserError:
    """本机没有任何可用浏览器时的可读错误（前端 notify 直接展示）。"""
    parts = ["未在本机找到可用的 Chrome/Edge 浏览器，无法打开 OA 页面。"]
    if _STATE.attach_error:
        parts.append(f"（也没连上当前浏览器：{_STATE.attach_error[:120]}）")
        _STATE.attach_error = ""
    parts.append(
        "两种解决方式：① 复用当前浏览器（推荐）：完全退出 Chrome，再以调试端口重启——"
        'macOS 终端执行 open -na "Google Chrome" --args --remote-debugging-port=9222，然后回来点「执行任务」重试；'
        "② 在 backend/config/expense_workflow.json 的 browser.chrome_path 里填入本机 Chrome 可执行文件路径。"
    )
    return OaBrowserError(" ".join(parts))


@dataclass
class _BrowserState:
    """常驻浏览器状态（进程内单例，受 _LOCK 保护）。"""

    pw: Any = None
    context: Any = None  # profile 模式=持久化上下文；cdp 模式=浏览器默认 context
    browser: Any = None  # cdp 模式下的 Browser 句柄
    page: Page | None = None
    executor: ThreadPoolExecutor | None = None
    config: dict[str, Any] = field(default_factory=dict)
    mode: str = ""  # "cdp"（连接当前浏览器）/ "profile"（独立窗口）
    attach_error: str = ""  # cdp 连接失败原因（用于结果提示）


_STATE = _BrowserState()
_LOCK = threading.Lock()


def _ensure_browser_locked() -> _BrowserState:
    """在执行器线程里调用：确保 playwright / 浏览器连接 / 页面都已就绪。"""
    cfg_browser = _STATE.config.get("browser", {})
    if _STATE.pw is None:
        # 本地 CDP 端点绝不能走代理：沙箱/企业环境常配 HTTP_PROXY，会把 localhost 请求
        # 也劫给代理（表现为 /json/version 404），这里把回环地址显式加入 NO_PROXY。
        for var in ("NO_PROXY", "no_proxy"):
            hosts = {h.strip() for h in os.environ.get(var, "").split(",") if h.strip()}
            hosts.update({"127.0.0.1", "localhost", "::1"})
            os.environ[var] = ",".join(sorted(hosts))
        _STATE.pw = sync_playwright().start()
    if _STATE.context is None:
        # 1) 优先 CDP attach：复用用户当前打开的浏览器（cookie/登录态/已开标签页）
        cdp_endpoint = str(cfg_browser.get("cdp_endpoint") or "").strip()
        if cdp_endpoint:
            try:
                browser = _STATE.pw.chromium.connect_over_cdp(cdp_endpoint, timeout=3000)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                _STATE.browser = browser
                _STATE.context = context
                _STATE.mode = "cdp"
            except Exception as exc:  # 连不上就退回独立窗口，不中断流程
                _STATE.attach_error = f"{type(exc).__name__}: {exc}"[:220]
        # 2) 兜底：独立持久化窗口。chrome_path 现在可选——没配/不存在时自动探测本机浏览器
        if _STATE.context is None:
            chrome_path = _find_chrome_binary(str(cfg_browser.get("chrome_path") or "").strip())
            if chrome_path is None:
                raise _no_browser_error()
            profile_dir = resolve_user_path(cfg_browser.get("user_data_dir", ".oa-browser-profile"))
            profile_dir.mkdir(parents=True, exist_ok=True)
            _STATE.context = _STATE.pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                executable_path=str(chrome_path),
                headless=bool(cfg_browser.get("headless", False)),
                viewport={"width": 1440, "height": 900},
                args=["--no-first-run", "--no-default-browser-check"],
            )
            _STATE.mode = "profile"
    if _STATE.mode == "profile" and (_STATE.page is None or _STATE.page.is_closed()):
        _STATE.page = _STATE.context.new_page()
    return _STATE


def _page_for_scenario_locked(scenario_url: str) -> Page:
    """挑执行页面：cdp 模式优先复用已打开的 OA 标签页（保留 cookie 与已填内容），
    没有就开新标签；profile 模式沿用常驻页面。"""
    if _STATE.mode == "cdp" and _STATE.context is not None:
        host = urlparse(scenario_url).netloc
        for existing in _STATE.context.pages:
            if host and host in (existing.url or ""):
                _STATE.page = existing
                return existing
        page = _STATE.context.new_page()
        _STATE.page = page
        return page
    assert _STATE.page is not None
    return _STATE.page


def _frame_with(page: Page, selector: str):
    """跨 frame 定位：OA 表单渲染在 iframe（static4form）里，page.locator 穿不进去。
    主 frame 优先，依次找第一个包含该 selector 的 frame；全没有就回主 frame（让后续超时报错）。"""
    frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame]
    for frame in frames:
        try:
            if frame.locator(selector).count() > 0:
                return frame
        except Exception:
            continue
    return page.main_frame


def _is_login_page(page: Page, login_check: dict[str, Any]) -> bool:
    """登录页判定：url 命中任一关键字，或任一登录特征元素可见。"""
    url = page.url or ""
    for keyword in login_check.get("url_contains", []):
        if keyword and keyword.lower() in url.lower():
            return True
    for selector in login_check.get("selectors", []):
        if not selector or selector == TODO_SELECTOR:
            continue
        try:
            if page.locator(selector).first.is_visible(timeout=500):
                return True
        except Exception:
            continue
    return False


def _fill_field(page: Page, name: str, spec: dict[str, Any], values: dict[str, Any], steps: list[dict[str, str]]) -> None:
    """按配置填单个字段。selector 未配置（__TODO__）只记录「待配置」，不视为失败。
    所有定位都在字段所在 frame 上执行（OA 表单在 iframe 里）。"""
    label = spec.get("label", name)
    mode = spec.get("mode", "input")

    # 条件字段：when 不满足直接跳过（如「非全电子票」时没有附件）
    when = spec.get("when")
    if when and when in values and not values[when]:
        steps.append({"field": label, "status": "skipped", "detail": f"条件 {when} 不满足"})
        return

    if mode == "radio":
        yes_selector = spec.get("selector_yes", TODO_SELECTOR)
        no_selector = spec.get("selector_no", TODO_SELECTOR)
        want_yes = bool(values.get(spec.get("from", ""), False))
        selector = yes_selector if want_yes else no_selector
        option = "是" if want_yes else "否"
        if selector in (None, TODO_SELECTOR):
            steps.append({"field": label, "status": "todo", "detail": f"「{option}」定位器待配置"})
            return
        _frame_with(page, selector).locator(selector).first.check(timeout=5000)
        steps.append({"field": label, "status": "filled", "detail": f"勾选 {option}"})
        return

    selector = spec.get("selector", TODO_SELECTOR)
    if selector in (None, TODO_SELECTOR):
        steps.append({"field": label, "status": "todo", "detail": "定位器待配置"})
        return

    if spec.get("from"):
        raw_value = values.get(spec["from"])
        if raw_value in (None, ""):
            steps.append({"field": label, "status": "skipped", "detail": "表单未提供值"})
            return
        value: Any = raw_value
    else:
        value = spec.get("value", "")

    if mode == "select" and isinstance(value, bool):
        # 下拉的是/否字段：布尔值映射成配置的文案（默认 是/否）
        value = spec.get("value_yes", "是") if value else spec.get("value_no", "否")

    if isinstance(value, (list, tuple)) and not value:
        steps.append({"field": label, "status": "skipped", "detail": "表单未提供值"})
        return

    if mode != "file":
        value = str(value)

    frame = _frame_with(page, selector)
    locator = frame.locator(selector).first

    if mode == "file":
        # 相关票据区是多选上传：value 可以是单个路径或路径列表
        file_list = value if isinstance(value, (list, tuple)) else [value]
        paths: list[str] = []
        for item in file_list:
            file_path = resolve_user_path(str(item))
            if not file_path.exists():
                raise OaBrowserError(f"附件路径不存在：{file_path}")
            paths.append(str(file_path))
        # 容器定位（如 div[data-fieldmark=...]）时向下解析真实 file input
        target = locator
        try:
            inner = locator.locator('input[type="file"]').first
            inner.wait_for(state="attached", timeout=1500)
            target = inner
        except Exception:
            pass
        target.set_input_files(paths if len(paths) > 1 else paths[0])
        names = "、".join(Path(p).name for p in paths)
        detail = f"附件 {len(paths)} 个：{names}" if len(paths) > 1 else f"附件 {names}"
        steps.append({"field": label, "status": "filled", "detail": detail})
        return

    if mode == "select":
        # ant-select / wea-select 联想下拉。操作序列照人工录制还原（field7012 实录）：
        # ① 点已渲染文本区打开下拉 → ② 点搜索框 → ③ clear_first 时退格清预填值
        # → ④ fill 搜索词（实测 type 键入不触发该组件的联想过滤，必须 fill）
        # → ⑤ 等下拉出现匹配 li 后真实点击（不用 force，防止点中隐藏陈旧元素）。
        # 搜索框/选项都优先限定在本字段容器内：上一个字段提交后搜索框可能还开着，
        # 全 frame 找 .last 会把搜索词填进别的组件（踩过）。选项只认 ant-select-dropdown
        # 里的 li，其他 wea-browser 组件的 a.child-item 陈旧列表是干扰项。
        # 提交验证只认容器内 hidden input（渲染区文本会镜像搜索词，不能作依据）。
        options_sel = (
            ".ant-select-dropdown:not(.ant-select-dropdown-hidden) "
            "li.ant-select-dropdown-menu-item:visible"
        )
        search_in_field = locator.locator(".ant-select-search__field")
        attempts: list[str] = []

        def _visible_search():
            for i in range(search_in_field.count()):
                el = search_in_field.nth(i)
                try:
                    if el.is_visible():
                        return el
                except Exception:
                    continue
            return None

        # search_value：OA 搜索关键词可与用户表单值不同（如「普通发票 0%」→ OA 搜「普通发票」）
        search_kw = str(spec.get("search_value") or value)
        for _attempt in range(3):
            # 1) 打开下拉，等搜索框或匹配选项任一出现（是/否等不可搜索下拉打开即出全部选项）
            search = _visible_search()
            if search is None:
                target = locator
                try:
                    inner = locator.locator(".ant-select-selection").first
                    inner.wait_for(state="visible", timeout=1500)
                    target = inner
                except Exception:
                    pass
                try:
                    target.click(timeout=5000)
                except Exception:
                    locator.click(timeout=5000)  # selection 隐藏/遮挡时点容器本身
                for _ in range(6):
                    search = _visible_search()
                    if search is not None:
                        break
                    if frame.locator(options_sel).filter(has_text=search_kw).count() > 0:
                        break
                    page.wait_for_timeout(400)
            try:
                if search is not None:
                    page.wait_for_timeout(300)  # 组件事件绑定抖动，稍等再填
                    search.click(timeout=2000)
                    if spec.get("clear_first"):
                        # 预填值可能是已选 tag 也可能是输入文本，退格到渲染区清空为止
                        rendered = locator.locator(".ant-select-selection__rendered").first
                        for _ in range(8):
                            try:
                                cur = (rendered.inner_text(timeout=800) or "").strip()
                            except Exception:
                                cur = ""
                            if not cur:
                                break
                            page.keyboard.press("Backspace")
                            page.wait_for_timeout(200)
                    search.fill(search_kw, timeout=2000)
                    # React 受控输入偶发吞掉 fill 事件：校验搜索值，不一致就重填一次
                    try:
                        if (search.input_value(timeout=1000) or "").strip() != search_kw:
                            search.fill(search_kw, timeout=2000)
                    except Exception:
                        pass
            except Exception as exc:
                attempts.append(f"search_interact_fail:{type(exc).__name__}")
                continue  # 搜索框交互失败，重开下拉
            # 2) 轮询等联想选项渲染：下拉是 portal 渲染（不在容器内），全 frame 找；
            #    只认 ant-select-dropdown 里的 li，排除其他组件的 a.child-item 干扰列表
            option = None
            for _ in range(8):
                opts = frame.locator(options_sel).filter(has_text=search_kw)
                if opts.count() > 0:
                    option = opts.first
                    break
                page.wait_for_timeout(400)
            attempts.append(f"opts={0 if option is None else 'hit'}")
            if option is None:
                continue
            try:
                option.click(timeout=3000)
            except Exception as exc:
                attempts.append(f"click_fail:{type(exc).__name__}")
                continue
            page.wait_for_timeout(600)
            # 3) 提交验证：容器内 hidden input 有值才算真提交
            if _select_committed(locator, value):
                # 收起下拉并失焦：multiple 模式下拉可能不自动关，残留会遮挡下一字段的点击
                try:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(300)
                except Exception:
                    pass
                steps.append({"field": label, "status": "filled", "detail": f"选择 {value}"})
                return
            attempts.append("not_committed")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        raise OaBrowserError(f"下拉选项中未找到「{value}」（{label}；{' | '.join(attempts)}）")

    locator.click(timeout=5000)
    locator.fill("")
    locator.type(value, delay=40)
    if mode == "autocomplete":
        # 等联想下拉出现，优先选完全匹配 value 的项；选不中就按回车接受第一项
        try:
            option = frame.get_by_role("option", name=value, exact=True).first
            option.click(timeout=3000)
        except Exception:
            page.keyboard.press("Enter")
    steps.append({"field": label, "status": "filled", "detail": value})


def _select_committed(container: Any, value: str) -> bool:
    """点选后验证已真实提交（录制证实：点中正确选项后 hidden input 立即变值）。
    只认 hidden input——渲染区文本会镜像搜索词，不能作为提交依据（踩过假成功）。"""
    try:
        hidden = container.locator("input[type='hidden']").first
        if hidden.count() > 0:
            return bool((hidden.input_value(timeout=1000) or "").strip())
    except Exception:
        pass
    # 容器里没有 hidden input 的组件才走兜底：下拉已关闭且渲染区出现选中文本
    try:
        if container.locator(".ant-select-search__field:visible").count() == 0:
            text = container.locator(".ant-select-selection__rendered").first.inner_text(timeout=1000)
            if value and value in (text or ""):
                return True
    except Exception:
        pass
    return False


def _open_scenario_page(scenario: dict[str, Any], steps: list[dict[str, str]]) -> tuple[Page | None, dict[str, Any] | None]:
    """打开/复用场景页面并做登录检测。返回 (page, need_login_result)——后者非 None 表示是登录页。"""
    cfg_browser = _STATE.config.get("browser", {})
    login_check = cfg_browser.get("login_check", {})
    timeout_ms = int(cfg_browser.get("navigate_timeout_ms", 30000))

    _ensure_browser_locked()
    page = _page_for_scenario_locked(scenario["url"])

    # 已停留在别的地址才重新导航；同地址复用页面（保留可能已填到一半的内容）
    if not page.url.startswith(scenario["url"].split("#")[0]) and page.url != scenario["url"]:
        page.goto(scenario["url"], timeout=timeout_ms, wait_until="domcontentloaded")
        steps.append({"field": "导航", "status": "filled", "detail": scenario["url"][:80] + "…"})
    else:
        steps.append({"field": "导航", "status": "filled", "detail": "复用已打开的 OA 页面"})
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass  # OA 页面常有长轮询，networkidle 等不满就算了

    if _is_login_page(page, login_check):
        return None, {
            "status": "need_login",
            "message": "已打开浏览器，但当前是 OA 登录页。请在浏览器窗口里完成登录，然后回到这里点「重试」。表单内容已保留。",
            "steps": steps,
        }
    return page, None


def _attach_hint() -> str:
    """CDP attach 失败退回独立窗口时，把原因记进后端日志（不展示给用户：
    退回独立窗口本身就静默可用，排障细节属于开发者视角）。"""
    if _STATE.mode == "profile" and _STATE.attach_error:
        error = _STATE.attach_error
        _STATE.attach_error = ""
        logging.getLogger(__name__).warning(
            "CDP attach 失败，已退回独立窗口模式（.oa-browser-profile）：%s", error
        )
    return ""


def _scenario_fields(scenario: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """解析场景要填的字段。

    - ``fields`` 是数组（推荐）：元素为 field_library 里的字段 key，场景内 ``field_overrides``
      可覆盖个别字段的值/定位器 → 多场景复用同一套组件定义，不冗余。
    - ``fields`` 是字典：场景内联定义（兼容旧配置与最小化测试配置）。
    """
    raw = scenario.get("fields", {})
    if isinstance(raw, dict):
        return [(name, spec) for name, spec in raw.items()]
    library = (_STATE.config or {}).get("field_library", {})
    overrides = scenario.get("field_overrides", {}) or {}
    resolved: list[tuple[str, dict[str, Any]]] = []
    for name in raw:
        spec = dict(library.get(name) or {})
        if not spec:
            # 库里没有这个 key：按待配置处理，执行时显式留痕而不是静默跳过
            resolved.append((name, {"label": name, "selector": TODO_SELECTOR, "mode": "input"}))
            continue
        spec.update(overrides.get(name) or {})
        resolved.append((name, spec))
    return resolved


def run_expense_scenario_sync(scenario: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """打开/复用 OA 页面 → 登录检测 → 填字段 → 点保存。返回逐步日志。"""
    steps: list[dict[str, str]] = []

    page, need_login = _open_scenario_page(scenario, steps)
    if need_login is not None:
        hint = _attach_hint()
        if hint:
            need_login["message"] += " " + hint
        return need_login
    assert page is not None

    for name, spec in _scenario_fields(scenario):
        if not spec.get("enabled", True):
            continue
        _fill_field(page, name, spec, values, steps)

    save_spec = _STATE.config.get("save_button", {})
    save_selector = save_spec.get("selector", TODO_SELECTOR)
    if save_selector in (None, TODO_SELECTOR):
        steps.append({"field": save_spec.get("label", "保存"), "status": "todo", "detail": "保存按钮定位器待配置"})
    else:
        _frame_with(page, save_selector).locator(save_selector).first.click(timeout=5000)
        steps.append({"field": save_spec.get("label", "保存"), "status": "filled", "detail": "已点击保存"})

    todos = [s for s in steps if s["status"] == "todo"]
    hint = _attach_hint()
    return {
        "status": "done" if not todos else "partial",
        "message": (
            "表单填写完成，已点击保存。"
            if not todos
            else "流程执行完毕，但部分字段的 OA 定位器尚未配置（已在执行详情中列出）。请把待配置字段反馈给管理员，补齐配置后重试即可。"
        )
        + (f" {hint}" if hint else ""),
        "steps": steps,
    }


# 页面字段检查：把 OA 页面上可见的输入控件/按钮连同建议定位器一起吐出来，
# 供用户把 selector 补进 config/expense_workflow.json（解决「待配置」无法上手的问题）。
_INSPECT_JS = """
() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  const cssSel = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
    const cls = (el.getAttribute('class') || '').trim().split(/\\s+/).filter(Boolean);
    const base = el.tagName.toLowerCase();
    return cls.length ? base + '.' + CSS.escape(cls[0]) : base;
  };
  const labelOf = (el) => {
    const lab = el.closest('label');
    if (lab && lab.innerText) return lab.innerText.trim().slice(0, 40);
    if (el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l && l.innerText) return l.innerText.trim().slice(0, 40);
    }
    const cell = el.closest('td, th');
    if (cell && cell.innerText) return cell.innerText.trim().slice(0, 40);
    const box = el.closest('div, li, p');
    if (box && box.parentElement && box.parentElement.innerText) {
      return box.parentElement.innerText.trim().slice(0, 40);
    }
    return '';
  };
  const fields = Array.from(document.querySelectorAll('input, textarea, select'))
    .filter(vis)
    .slice(0, 60)
    .map((el) => ({
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      selector: cssSel(el),
      label: labelOf(el),
      placeholder: el.getAttribute('placeholder') || '',
      value: (el.value || '').slice(0, 40),
      readonly: !!el.readOnly,
    }));
  const buttons = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"], a'))
    .filter(vis)
    .map((el) => ({
      text: (el.innerText || el.value || '').trim().slice(0, 30),
      selector: cssSel(el),
    }))
    .filter((b) => b.text && b.text.length <= 20)
    .slice(0, 40);
  return { fields, buttons, title: document.title, url: location.href };
}
"""


def inspect_scenario_sync(scenario: dict[str, Any]) -> dict[str, Any]:
    """打开/复用场景页面并 dump 可见表单控件与按钮（含建议定位器）。"""
    steps: list[dict[str, str]] = []
    page, need_login = _open_scenario_page(scenario, steps)
    if need_login is not None:
        hint = _attach_hint()
        if hint:
            need_login["message"] += " " + hint
        return need_login
    assert page is not None
    try:
        dumped = page.evaluate(_INSPECT_JS)
    except Exception as exc:
        raise OaBrowserError(f"读取页面字段失败：{exc}") from exc
    return {
        "status": "done",
        "message": "已读取当前 OA 页面字段。对照左栏表单把 selector 补进 backend/config/expense_workflow.json 后重试。",
        "url": dumped.get("url", ""),
        "title": dumped.get("title", ""),
        "fields": dumped.get("fields", []),
        "buttons": dumped.get("buttons", []),
        "steps": steps,
    }


def run_in_browser_thread(scenario: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """对外的同步入口：把整个流程丢进单线程执行器并等待结果。"""
    return _submit_in_browser_thread(run_expense_scenario_sync, scenario, values)


def inspect_in_browser_thread(scenario: dict[str, Any]) -> dict[str, Any]:
    """检查 OA 页面字段（同一条浏览器线程串行执行）。"""
    return _submit_in_browser_thread(inspect_scenario_sync, scenario)


def _submit_in_browser_thread(func, scenario: dict[str, Any], *args: Any) -> dict[str, Any]:
    with _LOCK:
        if _STATE.executor is None:
            _STATE.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="oa-browser")
        if not _STATE.config:
            _STATE.config = load_expense_config()
        started = time.time()
        try:
            result = _STATE.executor.submit(func, scenario, *args).result(timeout=120)
        except OaBrowserError:
            raise
        except Exception as exc:  # playwright 内部错误统一包一层
            raise OaBrowserError(f"浏览器自动化失败：{exc}") from exc
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return result


def browser_status() -> dict[str, Any]:
    """给前端的浏览器状态（模式 / 是否已打开 / 当前页面地址）。"""
    with _LOCK:
        page = _STATE.page
        return {
            "open": bool(page and not page.is_closed()),
            "url": page.url if page else None,
            "mode": _STATE.mode or None,
        }
