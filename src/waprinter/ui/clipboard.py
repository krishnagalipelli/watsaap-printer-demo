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
    if getattr(root, "_clipboard_installed", False):
        return
    root._clipboard_installed = True
    if sys.platform == "darwin":
        # Aqua also maps Ctrl+A/V to Emacs line/page movement. Those virtual
        # events can win over a class key binding, so remove the conflicting
        # aliases while retaining Home/PageDown and the native Command keys.
        root.event_delete("<<LineStart>>", "<Control-Key-a>", "<Control-Lock-Key-A>")
        root.event_delete("<<NextPage>>", "<Control-Key-v>", "<Control-Lock-Key-V>")
    # macOS reports the right button as button 2. Everywhere else button 2 is
    # the middle button, which Tk already uses.
    buttons = ("<Button-2>", "<Control-Button-1>") if sys.platform == "darwin" else ("<Button-3>",)
    for button in buttons:
        for widget_class in (*_BOXES, "TLabel", "Label", "Treeview"):
            root.bind_class(widget_class, button, _show_menu, add="+")
    for widget_class in ("TEntry", "Entry", "TCombobox"):
        root.bind_class(widget_class, "<<Paste>>", _paste)
    for widget_class in (*_BOXES, "Treeview"):
        root.bind_class(widget_class, "<Control-Key>", _by_key_position, add="+")
        root.bind_class(widget_class, "<Shift-F10>", _keyboard_menu)
        root.bind_class(widget_class, "<Key-Menu>", _keyboard_menu)
        for modifier in (("Control", "Command") if sys.platform == "darwin" else ("Control",)):
            for key, name in (("a", "<<SelectAll>>"), ("c", "<<Copy>>"),
                              ("v", "<<Paste>>"), ("x", "<<Cut>>")):
                root.bind_class(widget_class, f"<{modifier}-Key-{key}>",
                                lambda e, n=name: _dispatch(e.widget, n))
                root.bind_class(widget_class, f"<{modifier}-Lock-Key-{key.upper()}>",
                                lambda e, n=name: _dispatch(e.widget, n))
        root.bind_class(widget_class, "<<SelectAll>>", lambda e: _select_all(e.widget))
    root.bind_class("Treeview", "<<Copy>>", _copy_rows)
    root.bind_class("TCombobox", "<<Copy>>", _copy_combo)
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


def _select_all(widget: tk.Misc) -> str:
    widget.focus_set()
    if widget.winfo_class() == "Text":
        widget.tag_add("sel", "1.0", "end-1c")
    elif widget.winfo_class() == "Treeview":
        if str(widget.cget("selectmode")) == "extended":
            widget.selection_set(widget.get_children())
    else:
        widget.selection_range(0, "end")
        widget.icursor("end")
    return "break"


def _dispatch(widget, name):
    widget.event_generate(name)
    return "break"


def _copy_combo(event):
    widget = event.widget
    if not _hidden(widget):
        if widget.selection_present():
            _put(widget, widget.get()[widget.index("sel.first"):widget.index("sel.last")])
        elif widget.instate(["readonly"]):
            _put(widget, widget.get())
    return "break"


def _put(widget: tk.Misc, text: str) -> None:
    widget.clipboard_clear()
    widget.clipboard_append(text)


def _row_text(tree, row) -> str:
    """Visible columns, including empty cells, in their displayed order."""
    cells = [str(tree.item(row, "text"))] if "tree" in map(str, tree.tk.splitlist(tree.cget("show"))) else []
    columns = tuple(map(str, tree.tk.splitlist(tree.cget("displaycolumns"))))
    if columns == ("#all",):
        columns = tree.tk.splitlist(tree.cget("columns"))
    cells.extend(str(tree.set(row, column)) for column in columns)
    return "\t".join(cells)


def _copy_rows(event):
    tree = event.widget
    selected = set(tree.selection())
    rows = [row for row in tree.get_children() if row in selected]
    if rows:
        _put(tree, "\n".join(_row_text(tree, row) for row in rows))
    return "break"


def _keyboard_menu(event):
    widget = event.widget
    if widget.winfo_class() == "Treeview":
        row = widget.focus()
        if not row:
            return "break"
        widget.see(row)
        box = widget.bbox(row)
        if not box:
            return "break"
        event.x, event.y = box[0] + 8, box[1] + box[3] // 2
    else:
        event.x, event.y = 8, widget.winfo_height() // 2
    event.x_root = widget.winfo_rootx() + event.x
    event.y_root = widget.winfo_rooty() + event.y
    return _show_menu(event)


def show_text(widget, text, title="Select and copy text"):
    """Read long labels or clipped table cells and select any part of them."""
    dialog = tk.Toplevel(widget)
    dialog.title(title)
    dialog.transient(widget.winfo_toplevel())
    theme.fit_window(dialog, 640, 360, 320, 180)
    from tkinter import ttk
    ttk.Button(dialog, text="Close", command=dialog.destroy).pack(side="bottom", pady=8)
    body = ttk.Frame(dialog, padding=12)
    body.pack(fill="both", expand=True)
    view = theme.style_text(tk.Text(body, wrap="word", width=1, height=1, takefocus=True))
    bar = ttk.Scrollbar(body, command=view.yview)
    view.configure(yscrollcommand=bar.set)
    bar.pack(side="right", fill="y")
    view.pack(fill="both", expand=True)
    view.insert("1.0", text)
    view.configure(state="disabled")
    view.focus_set()
    dialog.bind("<Escape>", lambda _e: dialog.destroy())
    return dialog


def _menu(widget: tk.Misc) -> tk.Menu:
    return tk.Menu(
        widget, tearoff=0, background=theme.CARD, foreground=theme.INK,
        activebackground=theme.WASH, activeforeground=theme.INK, font=theme.BODY,
    )


def _show_menu(event):
    widget = event.widget
    kind = widget.winfo_class()
    if kind in ("TLabel", "Label"):
        text = str(widget.cget("text"))
        if not text:
            return None
        menu = _menu(widget)
        menu.add_command(label="Copy", command=lambda: _put(widget, text))
        menu.add_command(label="Select text…", command=lambda: show_text(widget, text))
    elif kind == "Treeview":
        row = widget.identify_row(event.y)
        if not row:
            return None
        widget.focus_set()
        widget.focus(row)
        if row not in widget.selection():
            widget.selection_set(row)
        text = _row_text(widget, row)
        menu = _menu(widget)
        column = widget.identify_column(event.x)
        if column:
            cell = widget.item(row, "text") if column == "#0" else widget.set(row, column)
            menu.add_command(label="Copy cell", command=lambda: _put(widget, str(cell)))
        menu.add_command(label="Copy selected rows", command=lambda: _copy_rows(event))
        menu.add_command(label="Select row text…", command=lambda: show_text(widget, text))
    else:
        menu = _menu(widget)
        _box_items(menu, widget)
    # tk_popup returns immediately on Windows/X11. Keep the menu alive until
    # it is dismissed, then release its registered Python callbacks.
    menu.bind("<Unmap>", lambda _e: menu.after_idle(menu.destroy))
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
