"""The settings page has to stay usable on the screens it is installed on.

A build shipped with Apply packed below settings groups that were taller than
the window, on a tab that did not scroll. It was not clipped by a few pixels —
it sat about 300px past the bottom edge, so maximising did not reveal it
either. The operator typed a page of settings, pressed Test send, and was told
the fields were empty.

The assertions are structural rather than pixel measurements on a deliberately
shrunken window. That is not squeamishness about geometry: forcing the old
layout into a window smaller than its content makes Tk fight its own geometry
manager inside update(), and the run hangs instead of failing. A regression
test that wedges CI is worse than no test, so what is checked here is the
shape that made the bug impossible — content that scrolls, and an Apply button
outside the part that scrolls.

Needs a display. Windows CI has one; a headless dev box skips.
"""

from __future__ import annotations

import tkinter as tk

import pytest


@pytest.fixture(scope="module")
def window(tmp_path_factory):
    """One window for the whole module.

    Module scope is not an optimisation: Tk on macOS segfaults when a process
    creates a second Tk() instance, so building one per test — or even probing
    for a display with a throwaway root — crashes the run rather than skipping
    it. These tests only measure the layout; none of them mutate it.
    """
    from waprinter.config import Settings
    from waprinter.pipeline import Pipeline
    from waprinter.send.dryrun import DryRunSender
    from waprinter.send.templates import TemplateStore
    from waprinter.store import Store

    tmp = tmp_path_factory.mktemp("desktop")
    store = Store(tmp / "jobs.db")
    pipeline = Pipeline(
        settings=Settings(),
        store=store,
        sender=DryRunSender(tmp / "dry_run.jsonl"),
        templates=TemplateStore(tmp / "templates.json"),
    )

    from waprinter.ui.desktop import DesktopWindow

    try:
        win = DesktopWindow(pipeline)
    except tk.TclError:
        store.close()
        pytest.skip("no display")

    win.tabs.select(win.settings_tab)
    win.root.update_idletasks()
    yield win
    win.root.destroy()
    store.close()


def _descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


def _apply_button(window):
    found = [
        w
        for w in _descendants(window.settings_tab)
        if w.winfo_class() == "TButton" and str(w.cget("text")) == "Apply"
    ]
    assert len(found) == 1, "expected exactly one Apply button on the settings tab"
    return found[0]


def _canvas(window):
    found = [w for w in _descendants(window.settings_tab) if w.winfo_class() == "Canvas"]
    assert found, "the settings tab does not scroll"
    return found[0]


def test_apply_cannot_scroll_out_of_sight(window):
    """Apply belongs to the tab, not to the content that scrolls under it."""
    canvas_path = str(_canvas(window))
    assert not str(_apply_button(window)).startswith(f"{canvas_path}.")


def test_the_settings_groups_overflow_and_therefore_must_scroll(window):
    """Guards the premise: if the fields ever fit, this file is testing nothing."""
    canvas = _canvas(window)
    content_height = canvas.bbox("all")[3]
    assert content_height > canvas.winfo_height()


def test_the_last_group_can_be_scrolled_to(window):
    canvas = _canvas(window)
    canvas.yview_moveto(1.0)
    window.root.update_idletasks()
    assert canvas.yview()[1] == pytest.approx(1.0)
    canvas.yview_moveto(0.0)


def test_fields_use_the_full_width(window):
    """The scrolling frame tracks the canvas, so entries are not a thin column."""
    canvas = _canvas(window)
    inner = canvas.winfo_children()[0]
    assert inner.winfo_width() == canvas.winfo_width()
