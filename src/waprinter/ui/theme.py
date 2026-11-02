"""How every window looks: one palette, three typefaces, and the ttk styles
built from them.

Ink on paper with a single mint accent: a quiet page, thin wash-green rules
instead of boxes, small upper-case captions in a monospaced face over each
section, and colour kept for the few things that need a decision. Mint means
"do this" or "all is well", warm amber means "waiting on something", red means
"this failed".

Tk has no stylesheet. The look is ttk styles on the "clam" theme -- the one
built-in theme that lets every colour be set; the native Windows themes draw
their own buttons and ignore most of them -- plus a few helpers below for the
shapes ttk has no widget for: a bordered card, a section caption, a page title.

Nothing here decides what the window says. That is viewmodel.py and
setupmodel.py.
"""

from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

# -- palette -------------------------------------------------------------------

INK = "#17352c"        # text, and the dark mode card in the side bar
PAPER = "#f7f9f7"      # every background
CARD = "#ffffff"       # inputs and tables, a shade lighter than the page
WASH = "#e6efeb"       # rules, borders, the selected row
LINE = "#cbd8d3"       # the border of anything you can type into or press
SUBTLE = "#637770"     # secondary text
MINT = "#27a677"       # the accent: primary buttons, done, sending
MINT_DARK = "#1f8a63"  # a pressed or hovered primary button
MINT_TEXT = "#1b7f5b"  # mint dark enough to read as text on paper
WARM = "#c08942"       # waiting: dots and badges
WARM_TEXT = "#94621f"  # waiting, as text
DANGER = "#c23b31"     # failed


def blend(front: str, back: str, amount: float) -> str:
    """`front` laid over `back` at `amount` opacity, as a solid colour.

    Tk has no transparency, so the design's "ink at 25%" has to be mixed.
    """
    f = [int(front[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(back[i : i + 2], 16) for i in (1, 3, 5)]
    mixed = [round(fc * amount + bc * (1 - amount)) for fc, bc in zip(f, b)]
    return "#%02x%02x%02x" % tuple(mixed)


FAINT = blend(INK, PAPER, 0.25)          # a count of nothing
HEAD = blend(WASH, CARD, 0.45)           # table headings
MINT_OFF = blend(MINT, PAPER, 0.45)      # a primary button that cannot be pressed
# Selected text in a box: plainly visible, and INK still reads on it.
SELECTED_TEXT = blend(MINT, CARD, 0.45)
ON_INK = blend(PAPER, INK, 0.65)         # secondary text on the ink card
ON_INK_FAINT = blend(PAPER, INK, 0.5)

# What a tone from the view models looks like as text.
TONES = {"ok": MINT_TEXT, "warn": WARM_TEXT, "bad": DANGER, "muted": SUBTLE}
# ...and as a dot or a badge.
DOTS = {"ok": MINT, "warn": WARM, "bad": DANGER, "muted": SUBTLE}

# -- type ------------------------------------------------------------------------
#
# Named Tk fonts, so any widget can use one by name. Segoe UI is on every
# Windows since Vista, Consolas since 7, so the counters need nothing installed;
# the fallbacks are for a developer's Mac.

BODY = "wpBody"
BOLD = "wpBold"
SMALL = "wpSmall"
MONO = "wpMono"             # section captions and badges, always upper case
MONO_BODY = "wpMonoBody"    # times and phone numbers
CARD_TITLE = "wpCardTitle"
HEADING = "wpHeading"
TITLE = "wpTitle"
STAT = "wpStat"
BRAND = "wpBrand"

_SANS = ("Segoe UI", "Helvetica Neue", "Helvetica", "Arial")
_DISPLAY = ("Segoe UI Semibold", "Avenir Next")
_MONO = ("Consolas", "Menlo", "Courier New")

# Tk deletes a named font when its Python object is collected.
_kept: list[tkfont.Font] = []


def _first(available: set[str], choices: tuple[str, ...], fallback: str) -> str:
    return next((c for c in choices if c in available), fallback)


def _fonts(root: tk.Misc) -> None:
    available = set(tkfont.families(root))
    default = _named(root, "TkDefaultFont").actual("family")
    sans = _first(available, _SANS, default)
    mono = _first(available, _MONO, "TkFixedFont")
    display = _first(available, _DISPLAY, "")
    # A semibold face is its own family on Windows; without one, bold stands in.
    display_weight = "normal" if display == "Segoe UI Semibold" else "bold"
    display = display or sans

    specs = {
        BODY: (sans, 9, "normal"),
        BOLD: (sans, 9, "bold"),
        SMALL: (sans, 8, "normal"),
        MONO: (mono, 8, "normal"),
        MONO_BODY: (mono, 9, "normal"),
        CARD_TITLE: (display, 11, display_weight),
        HEADING: (display, 13, display_weight),
        TITLE: (display, 22, display_weight),
        STAT: (sans, 24, "bold"),
        BRAND: (sans, 12, "bold"),
    }
    names = set(tkfont.names(root))
    for name, (family, size, weight) in specs.items():
        if name in names:
            _named(root, name).configure(family=family, size=size, weight=weight)
        else:
            _kept.append(tkfont.Font(root=root, name=name, family=family,
                                     size=size, weight=weight))
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        _named(root, name).configure(family=sans, size=9)


def _named(root: tk.Misc, name: str) -> tkfont.Font:
    # Not tkfont.nametofont(name, root=...): that argument is 3.10 and later,
    # and the Windows 7 counters run 3.8.
    return tkfont.Font(root=root, name=name, exists=True)


# -- styles ----------------------------------------------------------------------


def apply(root: tk.Tk) -> ttk.Style:
    """Set up the fonts and every ttk style. Call once, on the main window."""
    _fonts(root)
    root.configure(background=PAPER)
    style = ttk.Style(root)
    style.theme_use("clam")

    flat = dict(lightcolor=PAPER, darkcolor=PAPER, bordercolor=PAPER)
    style.configure(
        ".", background=PAPER, foreground=INK, font=BODY, bordercolor=LINE,
        lightcolor=CARD, darkcolor=CARD, troughcolor=PAPER, focuscolor=MINT,
        selectbackground=SELECTED_TEXT, selectforeground=INK, insertcolor=INK,
        fieldbackground=CARD, arrowcolor=SUBTLE,
    )
    style.map(".", foreground=[("disabled", blend(SUBTLE, PAPER, 0.7))])

    # Frames. A card is a thin wash rule round a region of the page.
    style.configure("TFrame", background=PAPER)
    style.configure("Card.TFrame", background=PAPER, bordercolor=WASH,
                    lightcolor=WASH, darkcolor=WASH, relief="solid", borderwidth=1)
    style.configure("Mode.TFrame", background=INK)
    style.configure("TSeparator", background=WASH)

    # Text.
    style.configure("TLabel", background=PAPER, foreground=INK)
    style.configure("Hint.TLabel", foreground=SUBTLE)
    style.configure("Small.TLabel", foreground=SUBTLE, font=SMALL)
    style.configure("Field.TLabel", font=BOLD)
    style.configure("Mono.TLabel", font=MONO_BODY)
    style.configure("MonoHint.TLabel", font=MONO_BODY, foreground=SUBTLE)
    style.configure("Eyebrow.TLabel", font=MONO, foreground=SUBTLE)
    style.configure("CardTitle.TLabel", font=CARD_TITLE)
    style.configure("Head.TLabel", font=HEADING)
    style.configure("Title.TLabel", font=TITLE)
    style.configure("Big.TLabel", font=STAT)
    style.configure("Brand.TLabel", font=BRAND)
    for tone, colour in TONES.items():
        style.configure(f"{tone.title()}.TLabel", foreground=colour)
        style.configure(f"{tone.title()}Tag.TLabel", foreground=colour, font=MONO)
    # The badge in the header: a word in a hairline box.
    style.configure("Chip.TLabel", font=MONO, foreground=SUBTLE, relief="solid",
                    borderwidth=1, bordercolor=WASH, lightcolor=WASH,
                    darkcolor=WASH, padding=(7, 3))
    for tone, colour in TONES.items():
        style.configure(f"{tone.title()}Chip.TLabel", foreground=colour,
                        font=MONO, relief="solid", borderwidth=1,
                        bordercolor=WASH, lightcolor=WASH, darkcolor=WASH,
                        padding=(7, 3))
    # The side bar's mode card is ink, so its text is paper.
    style.configure("ModeEyebrow.TLabel", background=INK, foreground=ON_INK_FAINT, font=MONO)
    style.configure("Mode.TLabel", background=INK, foreground=PAPER, font=BOLD)
    style.configure("ModeHint.TLabel", background=INK, foreground=ON_INK, font=SMALL)

    # Buttons. The default is a white button with a hairline border; Mint is
    # the one thing on a page you are expected to press.
    style.configure("TButton", background=CARD, foreground=INK, bordercolor=LINE,
                    lightcolor=CARD, darkcolor=CARD, padding=(12, 5),
                    focusthickness=1, focuscolor=LINE, anchor="center")
    style.map("TButton",
              background=[("disabled", PAPER), ("pressed", WASH), ("active", WASH)],
              lightcolor=[("disabled", PAPER), ("pressed", WASH), ("active", WASH)],
              darkcolor=[("disabled", PAPER), ("pressed", WASH), ("active", WASH)],
              bordercolor=[("focus", MINT)])
    style.configure("Mint.TButton", background=MINT, foreground=CARD, bordercolor=MINT,
                    lightcolor=MINT, darkcolor=MINT, font=BOLD, focuscolor=INK)
    mint_states = [("disabled", MINT_OFF), ("pressed", MINT_DARK), ("active", MINT_DARK)]
    style.map("Mint.TButton", background=mint_states, lightcolor=mint_states,
              darkcolor=mint_states, bordercolor=mint_states,
              foreground=[("disabled", CARD)])
    # Side bar entries and setup steps: text until you point at them.
    style.configure("Nav.TButton", background=PAPER, foreground=SUBTLE, anchor="w",
                    padding=(10, 8), focuscolor=LINE, **flat)
    style.map("Nav.TButton",
              background=[("pressed", WASH), ("active", WASH)],
              lightcolor=[("pressed", WASH), ("active", WASH)],
              darkcolor=[("pressed", WASH), ("active", WASH)],
              bordercolor=[("pressed", WASH), ("active", WASH)],
              foreground=[("active", INK)])
    on = dict(background=WASH, lightcolor=WASH, darkcolor=WASH, bordercolor=WASH)
    style.configure("NavOn.TButton", foreground=INK, anchor="w", padding=(10, 8),
                    focuscolor=INK, **on)
    style.map("NavOn.TButton", **{k: [("active", v)] for k, v in on.items()})
    # A link: "Open setup →".
    style.configure("Link.TButton", background=PAPER, foreground=INK, font=BOLD,
                    padding=(0, 2), focuscolor=INK, **flat)
    style.map("Link.TButton", foreground=[("active", MINT_TEXT)],
              background=[("active", PAPER), ("pressed", PAPER)],
              lightcolor=[("active", PAPER)], darkcolor=[("active", PAPER)],
              bordercolor=[("active", PAPER)])

    # Anything you type into.
    field = dict(fieldbackground=CARD, background=CARD, foreground=INK,
                 bordercolor=LINE, lightcolor=CARD, darkcolor=CARD)
    # Text selected in a box has to stand out from the box: in WASH, on white,
    # it was next to invisible, and nobody could see what they were copying.
    selected = dict(selectbackground=SELECTED_TEXT, selectforeground=INK)
    style.configure("TEntry", padding=(7, 5), **field, **selected)
    focus = [("focus", MINT)]
    style.map("TEntry", bordercolor=focus, lightcolor=focus,
              fieldbackground=[("disabled", PAPER), ("readonly", PAPER)])
    style.configure("TCombobox", padding=(7, 4), arrowsize=13, **field, **selected)
    style.map("TCombobox", bordercolor=focus, lightcolor=focus,
              fieldbackground=[("readonly", CARD), ("disabled", PAPER)],
              background=[("active", WASH), ("readonly", CARD)],
              selectbackground=[("readonly", SELECTED_TEXT)],
              selectforeground=[("readonly", INK)],
              arrowcolor=[("disabled", LINE)])
    root.option_add("*TCombobox*Listbox.background", CARD)
    root.option_add("*TCombobox*Listbox.foreground", INK)
    root.option_add("*TCombobox*Listbox.selectBackground", WASH)
    root.option_add("*TCombobox*Listbox.selectForeground", INK)
    root.option_add("*TCombobox*Listbox.font", BODY)
    root.option_add("*TCombobox*Listbox.relief", "flat")

    style.configure("TCheckbutton", background=PAPER, foreground=INK,
                    indicatorbackground=CARD, indicatorforeground=CARD,
                    upperbordercolor=LINE, lowerbordercolor=LINE,
                    indicatormargin=(0, 0, 8, 0), focuscolor=INK)
    style.map("TCheckbutton",
              background=[("active", PAPER)],
              indicatorbackground=[("selected", MINT), ("pressed", WASH)],
              upperbordercolor=[("selected", MINT)],
              lowerbordercolor=[("selected", MINT)])

    # Tables: white rows, a pale heading in small capitals, no grid.
    style.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=INK,
                    bordercolor=WASH, lightcolor=WASH, darkcolor=WASH, rowheight=_px(root, 30))
    style.map("Treeview", background=[("selected", SELECTED_TEXT)], foreground=[("selected", INK)])
    style.configure("Treeview.Heading", background=HEAD, foreground=SUBTLE, font=MONO,
                    relief="flat", bordercolor=WASH, lightcolor=HEAD, darkcolor=HEAD,
                    padding=(8, 6))
    style.map("Treeview.Heading", background=[("active", WASH)],
              lightcolor=[("active", WASH)], darkcolor=[("active", WASH)])

    # Slim scroll bars with no arrows.
    for orient in ("Vertical", "Horizontal"):
        sticky = "ns" if orient == "Vertical" else "ew"
        style.layout(f"{orient}.TScrollbar", [
            (f"{orient}.Scrollbar.trough", {"sticky": sticky, "children": [
                (f"{orient}.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"}),
            ]}),
        ])
        style.configure(f"{orient}.TScrollbar", background=WASH, troughcolor=PAPER,
                        bordercolor=PAPER, lightcolor=WASH, darkcolor=WASH,
                        arrowsize=_px(root, 10), gripcount=0)
        style.map(f"{orient}.TScrollbar",
                  background=[("active", LINE)], lightcolor=[("active", LINE)],
                  darkcolor=[("active", LINE)])
    return style


def _px(widget: tk.Misc, points_at_96dpi: float) -> int:
    """A size given in pixels at 100% scaling, at this screen's scaling."""
    try:
        scale = float(widget.tk.call("tk", "scaling")) / (96 / 72)
    except (tk.TclError, ValueError):
        scale = 1.0
    return max(1, round(points_at_96dpi * scale))


px = _px


# -- shapes ----------------------------------------------------------------------


def work_area(widget: tk.Misc) -> tuple[int, int, int, int]:
    """(x, y, width, height) of the screen a window can use, without the taskbar.

    Windows says exactly, wherever the taskbar is and however tall. Anywhere
    else, the screen less a taskbar's worth.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            SPI_GETWORKAREA = 48
            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(
                SPI_GETWORKAREA, 0, ctypes.byref(rect), 0
            ):
                return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
        except (AttributeError, OSError, ValueError):
            pass
    return (0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight() - _px(widget, 50))


def fitted_geometry(
    area: tuple[int, int, int, int], width: int, height: int, chrome: int
) -> tuple[int, int, int, int]:
    """(width, height, x, y) for a window centred in `area`, wholly inside it.

    `chrome` is the title bar, which a Tk height does not count and the screen
    still has to find room for.
    """
    left, top, available_width, available_height = area
    width = min(width, available_width)
    height = min(height, available_height - chrome)
    return (
        width,
        height,
        left + (available_width - width) // 2,
        top + max(0, (available_height - chrome - height) // 2),
    )


def fit_window(window: tk.Misc, width: int, height: int,
               min_width: int = 0, min_height: int = 0) -> None:
    """Size and place a window so nothing in it ends up under the taskbar.

    The size asked for if it fits, and otherwise as much as there is: a counter
    screen can be 1024 x 768, and a window opened taller than that keeps its
    bottom edge, and whatever buttons are on it, out of reach.
    """
    w, h, x, y = fitted_geometry(work_area(window), width, height, _px(window, 40))
    window.geometry(f"{w}x{h}+{x}+{y}")
    if min_width or min_height:
        window.minsize(min(min_width, w), min(min_height, h))


def autowrap(label: ttk.Label, margin: int = 6) -> ttk.Label:
    """Wrap the label's text at whatever width it is given, not a fixed one.

    The margin is the label's own padding: wrap at the full width and the
    widest line asks for a few pixels more than it has, and loses them.
    """
    label.bind(
        "<Configure>",
        lambda e: label.configure(wraplength=max(e.width - margin, 60)),
        add="+",
    )
    return label


def eyebrow(parent: tk.Misc, text: str, style: str = "Eyebrow.TLabel") -> ttk.Label:
    """The small upper-case caption over a section."""
    return ttk.Label(parent, text=text.upper(), style=style)


def card(parent: tk.Misc, title: str = "", padding=16) -> ttk.Frame:
    """A bordered region. The caller packs or grids it."""
    frame = ttk.Frame(parent, style="Card.TFrame", padding=padding)
    if title:
        ttk.Label(frame, text=title, style="CardTitle.TLabel").pack(anchor="w", pady=(0, 8))
    return frame


def section_head(parent: tk.Misc, text: str) -> ttk.Frame:
    """A caption with room on the right for a badge or a link. Packed."""
    row = ttk.Frame(parent)
    row.pack(fill="x", pady=(0, 10))
    eyebrow(row, text).pack(side="left")
    return row


def page_title(parent: tk.Misc, caption: str, title: str,
               description: str = "") -> tuple[ttk.Label, ttk.Label]:
    """Caption, title and one line of description at the top of a page."""
    box = ttk.Frame(parent)
    box.pack(fill="x", pady=(0, 22))
    eyebrow(box, caption).pack(anchor="w")
    heading = ttk.Label(box, text=title, style="Title.TLabel")
    heading.pack(anchor="w", pady=(4, 0))
    detail = ttk.Label(box, text=description, style="Hint.TLabel", justify="left")
    detail.pack(anchor="w", fill="x", pady=(2, 0))
    autowrap(detail)
    return heading, detail


def style_text(widget: tk.Text) -> tk.Text:
    """A tk.Text, which ttk does not reach, dressed like an entry."""
    widget.configure(
        background=CARD, foreground=INK, font=BODY, relief="flat", borderwidth=0,
        highlightthickness=1, highlightbackground=WASH, highlightcolor=WASH,
        insertbackground=INK, selectbackground=SELECTED_TEXT, selectforeground=INK,
        inactiveselectbackground=SELECTED_TEXT,
    )
    return widget


class Readout(tk.Text):
    """Selectable, read-only prose that grows to its wrapped content."""

    def __init__(self, parent, text="", style="TLabel"):
        super().__init__(parent, width=1, height=1, wrap="word", padx=0, pady=0,
                         borderwidth=0, highlightthickness=0, relief="flat",
                         cursor="xterm", takefocus=True, exportselection=False,
                         selectbackground=SELECTED_TEXT, selectforeground=INK,
                         inactiveselectbackground=SELECTED_TEXT)
        self._text = ""
        self._style = style
        self._resize_pending = None
        self.configure(style=style, text=text, state="disabled")
        self.bind("<Configure>", self._schedule_resize)
        self.bind("<Destroy>", self._cancel_resize)

    def configure(self, cnf=None, **kwargs):
        if cnf:
            kwargs.update(cnf)
        if "style" in kwargs:
            self._style = kwargs.pop("style")
            style = ttk.Style(self)
            kwargs.setdefault("foreground", style.lookup(self._style, "foreground") or INK)
            kwargs.setdefault("background", style.lookup(self._style, "background") or PAPER)
            kwargs.setdefault("font", style.lookup(self._style, "font") or BODY)
        if "text" in kwargs:
            value = str(kwargs.pop("text"))
            if value != self._text:
                self._text = value
                super().configure(state="normal")
                self.delete("1.0", "end")
                self.insert("1.0", value)
                super().configure(state="disabled")
                self._schedule_resize()
        return super().configure(**kwargs)

    config = configure

    def cget(self, key):
        if key == "text":
            return self._text
        if key == "style":
            return self._style
        return super().cget(key)

    def _schedule_resize(self, _event=None):
        if self._resize_pending is None:
            self._resize_pending = self.after_idle(self._resize)

    def _resize(self):
        self._resize_pending = None
        if self.winfo_width() <= 1:
            return
        lines = max(1, int(self.tk.call(self._w, "count", "-update", "-displaylines", "1.0", "end")))
        if int(self.cget("height")) != lines:
            super().configure(height=lines)

    def _cancel_resize(self, _event=None):
        if self._resize_pending is not None:
            self.after_cancel(self._resize_pending)
            self._resize_pending = None


def pack_table(tree: ttk.Treeview, **options) -> None:
    """Pack a table with both scrollbars, keeping all rows and columns reachable."""
    holder = ttk.Frame(tree.master)
    holder.pack(fill=options.pop("fill", "both"), expand=options.pop("expand", True), **options)
    vertical = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
    horizontal = ttk.Scrollbar(holder, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    holder.rowconfigure(0, weight=1)
    holder.columnconfigure(0, weight=1)
    tree.grid(in_=holder, row=0, column=0, sticky="nsew")
    vertical.grid(row=0, column=1, sticky="ns")
    horizontal.grid(row=1, column=0, sticky="ew")


def headings(tree: ttk.Treeview, columns) -> None:
    """Name and size a table's columns: (column, heading, width) triples.

    Widths are minimums that stretch to fill, so a narrow window loses slack
    rather than the last column.
    """
    for column, heading, width in columns:
        tree.heading(column, text=heading.upper(), anchor="w")
        tree.column(column, width=_px(tree, width), minwidth=_px(tree, 40),
                    anchor="w", stretch=True)


def scrollable(parent: tk.Misc) -> ttk.Frame:
    """A vertically scrolling region filling `parent`, returning its content
    frame. Tk has no scrolling container, so it is a canvas with a frame
    inside it."""
    canvas = tk.Canvas(parent, highlightthickness=0, borderwidth=0, background=PAPER)
    bar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=bar.set)
    canvas.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")

    inner = ttk.Frame(canvas)
    window = canvas.create_window((0, 0), window=inner, anchor="nw")
    # The content decides how tall the scroll region is; the canvas decides
    # how wide the content is, so the groups stretch instead of sitting in
    # a narrow column.
    inner.bind(
        "<Configure>",
        lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
    )
    canvas.bind(
        "<Configure>", lambda e: canvas.itemconfigure(window, width=e.width)
    )

    # Tk delivers the wheel to the widget under the pointer, which is
    # normally an entry inside the frame rather than the canvas. Bind on
    # the window and match by widget path, so the wheel still works over a
    # field but leaves the other pages' lists alone.
    def wheel(event: "tk.Event") -> None:
        path = str(event.widget)
        if path == str(canvas) or path.startswith(f"{canvas}."):
            # Text and tables already scroll themselves through class bindings.
            # Do not move the whole page as well on the same wheel event.
            if event.widget.winfo_class() in ("Text", "Treeview", "Listbox", "TCombobox"):
                if not isinstance(event.widget, Readout):
                    return
            # Only when there is something to scroll: a page shorter than the
            # window would otherwise creep up by a line per notch.
            if canvas.yview() != (0.0, 1.0):
                number = getattr(event, "num", None)
                delta = getattr(event, "delta", 0)
                if number in (4, 5):
                    steps = -1 if number == 4 else 1
                elif delta:
                    steps = -int(delta) if sys.platform == "darwin" else -int(delta / 120) or (-1 if delta > 0 else 1)
                else:
                    return
                canvas.yview_scroll(steps, "units")

    def reveal_focus(event):
        widget = event.widget
        if not str(widget).startswith(f"{inner}."):
            return
        canvas.update_idletasks()
        top = widget.winfo_rooty() - canvas.winfo_rooty()
        bottom = top + widget.winfo_height()
        height = canvas.winfo_height()
        if top >= 0 and bottom <= height:
            return
        bounds = canvas.bbox("all")
        if bounds and bounds[3] > height:
            offset = top if top < 0 or widget.winfo_height() > height else bottom - height
            canvas.yview_moveto(max(0, canvas.canvasy(0) + offset) / bounds[3])

    top = parent.winfo_toplevel()
    bindings = [(sequence, top.bind(sequence, callback, add="+")) for sequence, callback in (
        ("<MouseWheel>", wheel), ("<Button-4>", wheel), ("<Button-5>", wheel),
        ("<FocusIn>", reveal_focus),
    )]

    def cleanup(event):
        if event.widget == canvas:
            for sequence, identifier in bindings:
                # Older Python/Tk builds unbind every handler even with an
                # identifier. Remove only this canvas's callback on Win7 too.
                prefix = f'if {{"[{identifier} '
                script = top.bind(sequence)
                top.bind(sequence, "\n".join(line for line in script.splitlines()
                                            if not line.startswith(prefix)))
                top.deletecommand(identifier)

    canvas.bind("<Destroy>", cleanup)
    return inner
