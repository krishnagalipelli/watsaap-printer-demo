"""Make the app's icon and logo files from the logo artwork.

    python tools/make_brand_assets.py [packaging/brand/logo-source.webp]

Run by hand when the artwork changes, and the results committed: Pillow is
needed here and nowhere else, so neither the build nor a counter needs it.

The artwork is one wide picture on white: the mark (a chat bubble with a
document) beside the "rutha. Quick Send" wordmark. Neither is usable as it
is. An icon is square and sits on whatever the taskbar or desktop is, so the
mark is cut out on its own, its white surround taken away while the white
inside the bubble and the document stays, and it is resampled from the
artwork for every size Windows asks for rather than shrunk from one. The
side bar gets the whole logo with its background taken away.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "packaging" / "brand" / "logo-source.webp"
ASSETS = ROOT / "src" / "waprinter" / "ui" / "assets"
INSTALLER = ROOT / "installer"

# Where the artwork's parts are, in its own pixels (1983 x 793).
MARK = (148, 134, 639, 656)       # bubble and document
LOGO = (148, 134, 1843, 656)      # mark and wordmark
WORDMARK_FROM = 649               # the first column of the "r"

ICON_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
# Inno Setup's small wizard image is 55 x 55 at 100% and scales with the
# display; it picks the closest of these.
WIZARD_SMALL = [55, 69, 83, 97, 110, 124, 138]
# The large one, beside the finished page, is 164 x 314 at 100%.
WIZARD_LARGE = [(164, 314), (246, 471), (328, 628)]

NEAR_WHITE = 236


def cut_out(art: Image.Image, box: tuple[int, int, int, int], margin: int = 6,
            enclosed_white_from: int | None = None) -> Image.Image:
    """`box` of the artwork with its white background made transparent.

    White the background reaches from the edge goes; white enclosed by the
    mark (the handset, the inside of the document) stays. Left of
    `enclosed_white_from` that is the rule, and right of it -- the wordmark,
    whose letters' counters are enclosed too -- all white goes.

    The anti-aliased edge is un-mixed from the white it was drawn on, so it
    does not leave a pale fringe on a dark taskbar.
    """
    left, top, right, bottom = box
    rgb = art.crop((left - margin, top - margin, right + margin, bottom + margin))
    w, h = rgb.size
    r, g, b = rgb.split()
    lowest = ImageChops.darker(ImageChops.darker(r, g), b)
    white = lowest.point(lambda v: 255 if v >= NEAR_WHITE else 0)

    flood = white.copy()
    ImageDraw.floodfill(flood, (0, 0), 128)
    background = flood.point(lambda v: 255 if v == 128 else 0)
    if enclosed_white_from is not None:
        split = enclosed_white_from - (left - margin)
        right_part = Image.new("L", (w, h), 0)
        right_part.paste(white.crop((split, 0, w, h)), (split, 0))
        background = ImageChops.lighter(background, right_part)

    # The edge: background grown by the width of the anti-aliasing.
    edge = background.filter(ImageFilter.MaxFilter(7))
    darkest = Image.merge("RGB", [c.filter(ImageFilter.MinFilter(7)) for c in rgb.split()])

    out = Image.new("RGBA", (w, h))
    src, ink, near, bg = rgb.load(), darkest.load(), edge.load(), background.load()
    dst = out.load()
    for y in range(h):
        for x in range(w):
            r, g, b = src[x, y]
            if not near[x, y]:
                dst[x, y] = (r, g, b, 255)
                continue
            fr, fg, fb = ink[x, y]
            # The channel furthest from white says most about coverage.
            spread = max(255 - fr, 255 - fg, 255 - fb)
            if spread < 24:
                dst[x, y] = (r, g, b, 0 if bg[x, y] else 255)
                continue
            channel = [255 - fr, 255 - fg, 255 - fb].index(spread)
            seen = 255 - (r, g, b)[channel]
            alpha = max(0, min(255, round(255 * seen / spread)))
            dst[x, y] = (fr, fg, fb, alpha)
    return out.crop(out.getbbox())


def square(image: Image.Image, size: int, padding: float) -> Image.Image:
    """`image` centred on a transparent square, resampled to `size`."""
    room = round(size * (1 - 2 * padding))
    scale = room / max(image.size)
    fitted = image.resize((max(1, round(image.width * scale)),
                           max(1, round(image.height * scale))), Image.LANCZOS)
    if size <= 32:
        # Resampled this far the document's two lines blur into one grey
        # bar. Sharpening the colour, not the outline, brings them back
        # without a halo round the edge.
        alpha = fitted.getchannel("A")
        fitted = fitted.convert("RGB").filter(
            ImageFilter.UnsharpMask(radius=0.8, percent=160, threshold=0))
        fitted.putalpha(alpha)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(fitted, ((size - fitted.width) // 2,
                                    (size - fitted.height) // 2))
    return canvas


def on_white(image: Image.Image) -> Image.Image:
    """For Inno Setup, whose wizard images are bitmaps without transparency."""
    flat = Image.new("RGB", image.size, (255, 255, 255))
    flat.paste(image, mask=image.getchannel("A"))
    return flat


def main(source: Path = SOURCE) -> None:
    art = Image.open(source).convert("RGB")
    mark = cut_out(art, MARK)
    logo = cut_out(art, LOGO, enclosed_white_from=WORDMARK_FROM)

    ASSETS.mkdir(parents=True, exist_ok=True)
    # A small icon gets less padding: every pixel of a 16 x 16 counts.
    icons = [square(mark, s, 0.02 if s <= 32 else 0.04) for s in ICON_SIZES]
    icons[-1].save(ASSETS / "icon.ico", sizes=[(s, s) for s in ICON_SIZES],
                   append_images=icons[:-1])
    for s in (16, 32, 48, 256):
        icons[ICON_SIZES.index(s)].save(ASSETS / f"icon-{s}.png")
    # The side bar draws it at its own scaling from this one, so it is kept
    # large: 760 wide is the 190 pixels it has, at 400%.
    logo.resize((760, round(760 * logo.height / logo.width)),
                Image.LANCZOS).save(ASSETS / "logo.png", optimize=True)

    INSTALLER.mkdir(exist_ok=True)
    for s in WIZARD_SMALL:
        on_white(square(mark, s, 0.06)).save(INSTALLER / f"wizard-small-{s}.bmp")
    for w, h in WIZARD_LARGE:
        page = Image.new("RGBA", (w, h), (255, 255, 255, 255))
        badge = square(mark, round(w * 0.62), 0)
        page.alpha_composite(badge, ((w - badge.width) // 2, round(h * 0.22)))
        word_w = round(w * 0.84)
        words = logo.crop((WORDMARK_FROM - MARK[0] + 2, 0, logo.width, logo.height))
        words = words.crop(words.getbbox())
        words = words.resize((word_w, round(word_w * words.height / words.width)),
                             Image.LANCZOS)
        page.alpha_composite(words, ((w - word_w) // 2,
                                     round(h * 0.22) + badge.height + round(h * 0.06)))
        on_white(page).save(INSTALLER / f"wizard-large-{w}.bmp")
    print(f"wrote {ASSETS} and the wizard images in {INSTALLER}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else SOURCE)
