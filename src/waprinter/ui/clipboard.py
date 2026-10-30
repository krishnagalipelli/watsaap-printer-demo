"""Copy and paste, the way anyone at a Windows counter expects them to work.

Tk gives its input boxes keyboard shortcuts and nothing else. There is no
right-click menu anywhere, a label or a table row cannot be copied at all, and
the shortcuts are bound to the letters C and V, so they stop working the moment
the keyboard is set to Telugu or Hindi. All of it is installed here, once, on
the widget classes, so a box added later has it without anyone remembering to.
"""

from __future__ import annotations

import sys
import tkinter as tk

from . import theme

# Windows reports the physical key as its virtual-key code, whatever the
# keyboard layout makes of it.
_KEY_EVENTS = {65: "<<SelectAll>>", 67: "<<Copy>>", 86: "<<Paste>>", 88: "<<Cut>>"}
_CONTROL = 0x0004
_BOXES = ("TEntry", "Entry", "TCombobox", "Text")


def install(root: tk.Tk) -> None:
    """Give every box, label and table in the application its clipboard."""
    # macOS reports the right button as button 2. Everywhere else button 2 is
    # the middle button, which Tk already uses.
    buttons = ("<Button-2>", "<Control-Button-1>") if sys.platform == "darwin" else ("<Button-3>",)
    for button in buttons:
        for widget_class in (*_BOXES, "TLabel", "Treeview"):
            root.bind_class(widget_class, button, _show_menu, add="+")
    for widget_class in ("TEntry", "Entry", "TCombobox"):
        root.bind_class(widget_class, "<<Paste>>", _paste)
    for widget_class in _BOXES:
        root.bind_class(widget_class, "<Control-Key>", _by_key_position, add="+")
    # A disabled text does not take the focus when clicked on every platform,
    # and without it a selection made there cannot be copied with the keyboard.
    root.bind_class("Text", "<Button-1>", lambda e: e.widget.focus_set(), add="+")
    if sys.platform == "win32":
        root.event_add("<<SelectAll>>", "<Control-Key-a>", "<Control-Lock-Key-A>")


def guard_secret(entry: tk.Misc) -> None:
    """Keep what is hidden in this box from being copied while it is hidden.

    A ttk entry copies the real text behind the dots. Pasting still works.
    """

    def refuse(_event):
        return "break" if _hidden(entry) else None

    entry.bind("<<Copy>>", refuse)
    entry.bind("<<Cut>>", refuse)


def key_event(keycode: int, keysym: str) -> str | None:
    """The clipboard event for a Ctrl+key on a layout without Latin letters.

    None for a Latin layout, where Tk's own bindings already answer.
    """
    if keysym.lower() in ("a", "c", "v", "x"):
        return None
    return _KEY_EVENTS.get(keycode)


def clean_paste(text: str) -> str:
    """Clipboard text as a one-line box should take it.

    A cell copied from Excel or a line from a message arrives with a line break
    on the end, which a one-line box shows as a stray character and which then
    gets saved as part of the value.
    """
    return " ".join(part.strip() for part in text.splitlines() if part.strip())


def _by_key_position(event):
    if sys.platform != "win32" or not event.state & _CONTROL:
        return None
    name = key_event(event.keycode, event.keysym)
    if name is None:
        return None
    event.widget.event_generate(name)
    return "break"


def _paste(event):
    widget = event.widget
    if not _editable(widget):
        return "break"
    try:
        text = clean_paste(widget.clipboard_get())
    except tk.TclError:
        return "break"  # nothing on the clipboard, or nothing that is text
    if not text:
        return "break"
    if widget.selection_present():
        widget.delete("sel.first", "sel.last")
    widget.insert("insert", text)
    widget.xview("insert")
    return "break"


def _editable(widget: tk.Misc) -> bool:
    if hasattr(widget, "instate"):
        return not (widget.instate(["readonly"]) or widget.instate(["disabled"]))
    return str(widget.cget("state")) == "normal"


def _hidden(widget: tk.Misc) -> bool:
    try:
        return bool(str(widget.cget("show")))
    except tk.TclError:
        return False  # a box with no such option hides nothing


def _has_selection(widget: tk.Misc) -> bool:
    if widget.winfo_class() == "Text":
        return bool(widget.tag_ranges("sel"))
    return bool(widget.selection_present())


def _select_all(widget: tk.Misc) -> None:
    widget.focus_set()
    if widget.winfo_class() == "Text":
        widget.tag_add("sel", "1.0", "end-1c")
    else:
        widget.selection_range(0, "end")
        widget.icursor("end")


def _put(widget: tk.Misc, text: str) -> None:
    widget.clipboard_clear()
    widget.clipboard_append(text)


def _row_text(tree, event) -> str:
    """One table row as tab-separated text, which also pastes into Excel."""
    row = tree.identify_row(event.y)
    if not row:
        return ""
    tree.selection_set(row)
    cells = [str(tree.item(row, "text")), *(str(v) for v in tree.item(row, "values"))]
    return "\t".join(cell for cell in cells if cell)


def _menu(widget: tk.Misc) -> tk.Menu:
    return tk.Menu(
        widget, tearoff=0, background=theme.CARD, foreground=theme.INK,
        activebackground=theme.WASH, activeforeground=theme.INK, font=theme.BODY,
    )


def _show_menu(event):
    widget = event.widget
    kind = widget.winfo_class()
    menu = _menu(widget)
    if kind == "TLabel":
        text = str(widget.cget("text"))
        if not text:
            return None
        menu.add_command(label="Copy", command=lambda: _put(widget, text))
    elif kind == "Treeview":
        text = _row_text(widget, event)
        if not text:
            return None
        menu.add_command(label="Copy row", command=lambda: _put(widget, text))
    else:
        _box_items(menu, widget)
    try:
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        menu.grab_release()
    return "break"


def _box_items(menu: tk.Menu, widget: tk.Misc) -> None:
    widget.focus_set()
    editable = _editable(widget)
    selected = _has_selection(widget)
    copyable = selected and not _hidden(widget)
    if not selected and widget.winfo_class() == "TCombobox" and not editable:
        # A read-only drop-down shows no selection to make, only its choice.
        menu.add_command(label="Copy", command=lambda: _put(widget, widget.get()))
        return

    def item(label: str, name: str, enabled: bool) -> None:
        menu.add_command(
            label=label,
            state="normal" if enabled else "disabled",
            command=lambda: widget.event_generate(name),
        )

    item("Cut", "<<Cut>>", editable and copyable)
    item("Copy", "<<Copy>>", copyable)
    item("Paste", "<<Paste>>", editable)
    menu.add_separator()
    menu.add_command(label="Select all", command=lambda: _select_all(widget))
