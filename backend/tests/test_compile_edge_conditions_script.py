"""``scripts/compile_skill_edge_conditions.py`` 的参数语义与模式互斥。

这个脚本会在**生产库**上写数据，所以「哪些组合是只读的」必须在入口被钉死：
历史上 ``--queue --dry-run`` 会被静默当成一次真实落库（queue 分支完全没看
``--dry-run``），而 ``--no-llm`` 又被一行注释掉的赋值彻底忽略。两个都属于
「参数看上去生效、其实没生效」，比直接报错危险得多。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = BACKEND_DIR / "scripts" / "compile_skill_edge_conditions.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("compile_edge_conditions_script", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["compile_edge_conditions_script"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def script():
    return _load_script()


@pytest.fixture()
def calls(monkeypatch, script):
    """记录各模式入口收到的参数，同时让目标枚举返回一个假目标。

    四个入口都替换成「记录 + 返回空 Totals」，因此不触碰数据库。
    """

    recorded: dict[str, dict] = {}

    class _Target:
        tenant_id = "tenant_a"
        skill_id = "sop_1"
        agent_id = None
        name = "示例"
        version = "v1"

        @property
        def label(self) -> str:
            return "sop_1"

    def _recorder(name: str):
        def _run(*_targets, **kwargs):
            recorded[name] = kwargs
            return script.Totals()

        return _run

    monkeypatch.setattr(script, "_collect_targets", lambda *a, **k: [_Target()])
    for func_name, key in (
        ("run_dry", "dry"),
        ("run_apply", "apply"),
        ("run_queue", "queue"),
        ("run_verify", "verify"),
    ):
        monkeypatch.setattr(script, func_name, _recorder(key))
    return recorded


def test_queue_without_apply_is_rejected(script, calls, capsys) -> None:
    """queue 模式由 worker 真实写库，必须显式 --apply。"""

    assert script.main(["--queue", "--tenant", "tenant_a"]) == 2
    assert "queue" not in calls
    assert "不能省略 --apply" in capsys.readouterr().err


def test_queue_with_dry_run_is_rejected(script, calls, capsys) -> None:
    """--dry-run 拦不住 queue，组合出现必须报错而不是静默写库。"""

    assert script.main(["--apply", "--queue", "--dry-run"]) == 2
    assert "queue" not in calls
    assert "--dry-run" in capsys.readouterr().err


def test_apply_with_queue_is_allowed(script, calls) -> None:
    assert script.main(["--apply", "--queue"]) == 0
    assert "queue" in calls
    assert "dry" not in calls and "apply" not in calls


def test_no_llm_with_apply_is_rejected(script, calls, capsys) -> None:
    """禁用 LLM 落库会把非预设边固化成 llm_judge，必须挡住。"""

    assert script.main(["--apply", "--no-llm"]) == 2
    assert "apply" not in calls
    assert "--no-llm 只用于 dry-run 预览" in capsys.readouterr().err


def test_no_llm_is_honoured_in_dry_run(script, calls) -> None:
    """--no-llm 必须真的传到编译层（历史 bug：被硬编码的 allow_llm=True 吃掉）。"""

    assert script.main(["--no-llm"]) == 0
    assert calls["dry"]["allow_llm"] is False


def test_dry_run_defaults_to_llm(script, calls) -> None:
    assert script.main([]) == 0
    assert calls["dry"]["allow_llm"] is True


def test_apply_does_not_implicitly_force(script, calls) -> None:
    """--apply 不带 --force 时不能强编，否则会覆盖已有的 LLM 编译产物。"""

    assert script.main(["--apply"]) == 0
    assert calls["apply"]["force"] is False

    calls.clear()
    assert script.main(["--apply", "--force"]) == 0
    assert calls["apply"]["force"] is True


def test_quiet_flag_disables_per_skill_output(script, calls) -> None:
    assert script.main(["--quiet"]) == 0
    assert calls["dry"]["verbose"] is False


def test_no_targets_is_a_noop(script, monkeypatch) -> None:
    monkeypatch.setattr(script, "_collect_targets", lambda *a, **k: [])
    assert script.main([]) == 0
