"""Wizard smoke tests: the question flow runs headless against a stub.

``questionary`` is replaced by a scripted double, so the tests cover the
assembly of the answer dictionary (including the steps that only exist in the
wizard) without touching a terminal or the network. The config directory is
redirected to a throwaway path, so the blacklist and the remembered folder
stay inside the test.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from bdl.blacklist import load_blacklist
from bdl.config import get_setting
from bdl.i18n import t
from bdl.tui import wizard

PROMPT_LOG: list[str] = []
#: Free-form answers the scripted prompts return, keyed by prompt message.
TEXT_ANSWERS: dict[str, str] = {}


class _FakeQuestion:
    def __init__(self, kind: str, message: str, **kwargs: Any) -> None:
        self.kind = kind
        self.message = message
        self.kwargs = kwargs
        self.values = [choice.value for choice in kwargs.get("choices", []) if getattr(choice, "checked", False)]

    async def ask_async(self) -> Any:
        PROMPT_LOG.append(self.message)
        if self.kind == "confirm":
            return True
        if self.kind == "checkbox":
            return list(self.values)
        if self.kind == "select":
            return self.kwargs["choices"][0].value
        if self.kind == "text":
            if self.message in TEXT_ANSWERS:
                return TEXT_ANSWERS[self.message]
            default = self.kwargs.get("default", "")
            # The tags prompt is the only empty one that expects content.
            return "1girl" if self.message == t("tui.tags") else default
        raise AssertionError(self.kind)


class _FakeChoice:
    def __init__(self, title: str, value: Any = None, checked: bool = False) -> None:
        self.title = title
        self.value = value
        self.checked = checked


class _FakeStyle:
    def __init__(self, data: Any) -> None:
        self.data = data


def _fake_module() -> types.ModuleType:
    module = types.ModuleType("questionary")
    module.Choice = _FakeChoice
    module.Style = _FakeStyle
    module.print = print
    for kind in ("checkbox", "text", "select", "confirm"):
        module.__dict__[kind] = (
            lambda message, _kind=kind, **kwargs: _FakeQuestion(_kind, message, **kwargs)
        )
    return module


@pytest.fixture()
def scripted(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setitem(sys.modules, "questionary", _fake_module())
    # Keep the remembered folder and the blacklist inside the test.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    PROMPT_LOG.clear()
    TEXT_ANSWERS.clear()


def test_wizard_collects_every_download_option(scripted: None) -> None:
    TEXT_ANSWERS[t("tui.blacklist")] = "ai"
    answers = asyncio.run(wizard.run_wizard())

    assert answers is not None
    assert answers["sources"]
    assert answers["limit"] == 50
    assert answers["concurrency"] == wizard.DEFAULT_CONCURRENCY == 8
    assert answers["order"] == ""
    assert answers["options"] == wizard.DEFAULT_OPTIONS
    assert answers["options"]["notify"] is True
    assert answers["options"]["sidecar"] is False
    # A root expands into its whole family instead of a single tag.
    assert "ai" in answers["blacklist"]
    assert "ai_generated" in answers["blacklist"]
    assert load_blacklist() == answers["blacklist"]


def test_wizard_leaves_no_blacklist_when_the_field_is_empty(scripted: None) -> None:
    answers = asyncio.run(wizard.run_wizard())

    assert answers is not None
    assert answers["blacklist"] == set()
    assert load_blacklist() == set()


def test_wizard_asks_every_screen_in_order(scripted: None) -> None:
    asyncio.run(wizard.run_wizard())

    expected = [
        t("tui.sources"),
        t("tui.tags"),
        t("tui.limit"),
        t("tui.order"),
        t("tui.category"),
        t("tui.geometry"),
        t("tui.blacklist"),
        t("tui.out"),
        t("tui.confirm"),
    ]
    assert expected == PROMPT_LOG


def test_wizard_has_no_concurrency_or_options_screen(scripted: None) -> None:
    asyncio.run(wizard.run_wizard())

    assert t("tui.concurrency") not in PROMPT_LOG
    assert t("tui.options") not in PROMPT_LOG
    assert t("tui.blacklist_extra") not in PROMPT_LOG


def test_wizard_remembers_the_output_folder(scripted: None) -> None:
    asyncio.run(wizard.run_wizard())
    assert t("tui.out") in PROMPT_LOG
    saved = get_setting("out")
    assert saved

    # Second run: the folder is already known, so it must not be asked again.
    PROMPT_LOG.clear()
    asyncio.run(wizard.run_wizard())
    assert t("tui.out") not in PROMPT_LOG
    assert get_setting("out") == saved


def test_wizard_env_out_wins_and_is_not_saved(scripted: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("BDL_OUT", str(tmp_path / "env-lib"))
    answers = asyncio.run(wizard.run_wizard())

    assert answers is not None
    assert str(answers["out"]) == str(tmp_path / "env-lib")
    assert t("tui.out") not in PROMPT_LOG
    # The environment is the caller's business; the wizard must not persist it.
    assert get_setting("out") == ""


def test_geometry_descriptions() -> None:
    from bdl.filters import AVATAR_ASPECT, WALLPAPER_ASPECT

    assert wizard._describe_geometry(None) == t("tui.geometry_none")
    assert wizard._describe_geometry(WALLPAPER_ASPECT) == t("tui.geometry_wallpapers")
    assert wizard._describe_geometry(AVATAR_ASPECT) == t("tui.geometry_avatars")
    assert wizard._describe_geometry((0.7, 0.9)) == "0.7–0.9"
