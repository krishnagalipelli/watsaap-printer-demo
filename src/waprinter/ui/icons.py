"""Line icons, drawn from SVG at the size and colour the screen needs.

Tk cannot draw SVG, and a folder of PNGs would need one file per colour per
scaling factor. PyMuPDF is already here to read the prints and it renders SVG,
so the icons are a few paths in this file, rendered on first use and cached.

The shapes follow the Lucide icon set (ISC licence): 24-unit grid, 2-unit
round-capped strokes. An icon that cannot be drawn comes back as "" and the
widget shows its text alone -- an icon is never the only way to tell what a
control does.
"""

from __future__ import annotations

import base64
import logging
import tkinter as tk

from .theme import px

log = logging.getLogger(__name__)

_SHAPES = {
    "printer": '<path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/>'
               '<path d="M6 9V3a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v6"/>'
               '<rect x="6" y="14" width="12" height="8" rx="1"/>',
    "activity": '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    "alert": '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>'
             '<line x1="12" y1="16" x2="12.01" y2="16"/>',
    "file": '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/>'
            '<path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M16 13H8"/><path d="M16 17H8"/>'
            '<path d="M10 9H8"/>',
    "settings": '<path d="M20 7h-9"/><path d="M14 17H5"/>'
                '<circle cx="17" cy="17" r="3"/><circle cx="7" cy="7" r="3"/>',
    "clipboard": '<rect x="8" y="2" width="8" height="4" rx="1"/>'
                 '<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6'
                 'a2 2 0 0 1 2-2h2"/><path d="M12 11h4"/><path d="M12 16h4"/>'
                 '<path d="M8 11h.01"/><path d="M8 16h.01"/>',
    "refresh": '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/>'
               '<path d="M21 3v5h-5"/>'
               '<path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/>'
               '<path d="M8 16H3v5"/>',
    "arrow": '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    # Not Lucide: the small filled dot beside something still to do.
    "dot": '<circle cx="12" cy="12" r="6" fill="{colour}" stroke="none"/>',
}

# (interpreter, name, colour, sizes) -> image. Tk drops an image the moment
# Python stops referring to it, so the cache is also what keeps them alive.
_cache: dict[tuple, tk.PhotoImage | None] = {}


def icon(master: tk.Misc, name: str, colour: str, size: int = 16,
         before: int = 0, after: int = 0) -> tk.PhotoImage | str:
    """The icon, or "" -- which is what a widget's image option takes for none.

    `before` and `after` are clear space beside it. ttk puts an image and its
    text edge to edge with no option for a gap, so the gap is in the image.
    All three are pixels at 100% scaling.
    """
    key = (id(master.tk), name, colour, px(master, size), px(master, before),
           px(master, after))
    if key not in _cache:
        _cache[key] = _draw(master, name, colour, *key[3:])
    return _cache[key] or ""


def _draw(master: tk.Misc, name: str, colour: str, pixels: int, before: int,
          after: int) -> tk.PhotoImage | None:
    try:
        import fitz

        unit = 24 / pixels
        left, width = -before * unit, 24 + (before + after) * unit
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.2f}" height="24" '
            f'viewBox="{left:.2f} 0 {width:.2f} 24" fill="none" stroke="{colour}" '
            'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            + _SHAPES[name].replace("{colour}", colour)
            + "</svg>"
        )
        with fitz.open(stream=svg.encode("utf-8"), filetype="svg") as doc:
            scale = pixels / 24
            pixmap = doc[0].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=True)
            data = base64.b64encode(pixmap.tobytes("png"))
        return tk.PhotoImage(master=master, data=data)
    except Exception:
        log.debug("could not draw the %s icon", name, exc_info=True)
        return None
