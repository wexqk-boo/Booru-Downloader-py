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

from bdl.blacklist import BUILTIN_TAGS, load_blacklist, load_user_blacklist, save_user_blacklist
from bdl.config import get_setting
from bdl.i18n import t
from bdl.tui import wizard

PROMPT_LOG: list[str] = []
#: Free-form answers the scripted prompts return, keyed by prompt message.
TEXT_ANSWERS: dict[str, str] = {}
#: Entries to pick from the exclusion menu, keyed by the menu's heading. Each
#: entry is consumed in turn, so a test can drive the menu's loop ("view", then
#: settle on "use") without it spinning forever.
SELECT_ANSWERS: dict[str, list[str]] = {}
#: Values a scripted checkbox comes back with, keyed by prompt message.
CHECKBOX_ANSWERS: dict[str, list[str]] = {}
#: Answers a scripted confirm gives, keyed by prompt message.
CONFIRM_ANSWERS: dict[str, bool] = {}


class _FakeQuestion:
    def __init__(self, kind: str, message: str, **kwargs: Any) -> None:
        self.kind = kind
        self.message = message
        self.kwargs = kwargs
        self.values = [choice.value for choice in kwargs.get("choices", []) if getattr(choice, "checked", False)]

    async def ask_async(self) -> Any:
        PROMPT_LOG.append(self.message)
        if self.kind == "confirm":
            return CONFIRM_ANSWERS.get(self.message, True)
        if self.kind == "checkbox":
            if self.message in CHECKBOX_ANSWERS:
                return list(CHECKBOX_ANSWERS[self.message])
            return list(self.values)
        if self.kind == "select":
            for heading, queue in SELECT_ANSWERS.items():
                if self.message.startswith(heading) and queue:
                    return queue.pop(0)
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
        module.__dict__[kind] = lambda message, _kind=kind, **kwargs: _FakeQuestion(_kind, message, **kwargs)
    return module


@pytest.fixture()
def scripted(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setitem(sys.modules, "questionary", _fake_module())
    # Keep the remembered folder and the blacklist inside the test.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    PROMPT_LOG.clear()
    TEXT_ANSWERS.clear()
    SELECT_ANSWERS.clear()
    CHECKBOX_ANSWERS.clear()
    CONFIRM_ANSWERS.clear()


def test_wizard_collects_every_download_option(scripted: None) -> None:
    answers = asyncio.run(wizard.run_wizard())

    assert answers is not None
    assert answers["sources"]
    assert answers["limit"] == 50
    assert answers["concurrency"] == wizard.DEFAULT_CONCURRENCY == 8
    assert answers["order"] == ""
    assert answers["options"] == wizard.DEFAULT_OPTIONS
    assert answers["options"]["notify"] is True
    assert answers["options"]["sidecar"] is False
    # The default menu entry hands back the built-in tags, so the protection
    # is always on even when the user saves nothing of their own.
    assert answers["blacklist"] == set(BUILTIN_TAGS)
    assert load_blacklist() == answers["blacklist"]


def test_wizard_keeps_saved_tags_when_the_default_entry_is_chosen(scripted: None) -> None:
    """The old step wrote the file on every pass, so a re-run wiped it."""
    save_user_blacklist({"guro"})
    answers = asyncio.run(wizard.run_wizard())

    assert answers is not None
    assert "guro" in answers["blacklist"]
    assert load_user_blacklist() == {"guro"}


def test_wizard_can_add_tags_and_persist_them(scripted: None) -> None:
    SELECT_ANSWERS[t("tui.blacklist")] = ["add", "use"]
    TEXT_ANSWERS[t("tui.blacklist_add_prompt")] = "cat, furry"
    answers = asyncio.run(wizard.run_wizard())

    assert answers is not None
    assert {"cat", "furry"} <= answers["blacklist"]
    assert {"cat", "furry"} <= load_user_blacklist()


def test_wizard_can_add_tags_for_one_run_only(scripted: None) -> None:
    """Declining the save keeps the exclusion for this run but stores nothing."""
    SELECT_ANSWERS[t("tui.blacklist")] = ["add", "use"]
    TEXT_ANSWERS[t("tui.blacklist_add_prompt")] = "cat"
    CONFIRM_ANSWERS[t("tui.blacklist_persist")] = False
    answers = asyncio.run(wizard.run_wizard())

    assert answers is not None
    assert "cat" in answers["blacklist"]
    assert "cat" not in load_user_blacklist()


def test_wizard_can_remove_tags(scripted: None) -> None:
    save_user_blacklist({"guro", "cat"})
    SELECT_ANSWERS[t("tui.blacklist")] = ["remove", "use"]
    CHECKBOX_ANSWERS[t("tui.blacklist_remove_prompt")] = ["guro"]
    answers = asyncio.run(wizard.run_wizard())

    assert answers is not None
    assert "guro" not in load_user_blacklist()
    assert "cat" in load_user_blacklist()


def test_wizard_view_shows_both_lists_and_returns_to_the_step(scripted: None) -> None:
    save_user_blacklist({"guro"})
    SELECT_ANSWERS[t("tui.blacklist")] = ["view", "use"]
    answers = asyncio.run(wizard.run_wizard())

    assert answers is not None
    # The menu came back after the view, then the default entry was taken.
    assert [msg for msg in PROMPT_LOG if msg.startswith(t("tui.blacklist"))]
    assert len([msg for msg in PROMPT_LOG if msg.startswith(t("tui.blacklist"))]) >= 2
    assert load_user_blacklist() == {"guro"}


def test_wizard_asks_every_screen_in_order(scripted: None) -> None:
    asyncio.run(wizard.run_wizard())

    expected = [
        t("tui.sources"),
        t("tui.tags"),
        t("tui.limit"),
        t("tui.order"),
        t("tui.category"),
        t("tui.geometry"),
        t("tui.blacklist"),  # the exclusion menu, whose heading ends in a summary
        t("tui.out"),
        t("tui.confirm"),
    ]
    # The menu heading appends the saved-tag count, so compare by prefix.
    assert len(expected) == len(PROMPT_LOG)
    for want, got in zip(expected, PROMPT_LOG, strict=True):
        assert got.startswith(want), (want, got)


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
