"""
image_prep — turn a raw ChatGPT PNG into a print-ready file for a given blank.

Does, in order:
  1. Load, orient, force RGBA.
  2. (optional) Remove a near-solid background via edge flood-fill -> transparency.
     Preserves interior regions that share the background color (flood only eats
     what's connected to the border, like a magic-wand-from-the-edges).
  3. Trim transparent margins so the art fills the print area.
  4. Upscale/resize to the blank's target pixels (Lanczos) + a light unsharp pass
     to counter upscaling softness.
  5. Report a quality verdict so the pipeline can warn if the source was too small
     to print crisply (ChatGPT exports ~1024-1536px; a full shirt wants ~4500px).

Pillow-only on purpose: no numpy / no rembg, so GitHub Actions stays free & fast.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageOps

# Sentinel color used only during flood-fill; alpha is derived from it, then it's
# gone. Chosen to almost never appear in real art.
_SENTINEL = (255, 0, 255)


@dataclass
class PrepResult:
    out_path: Path
    source_px: tuple[int, int]
    out_px: tuple[int, int]
    background_removed: bool
    background_confident: bool
    low_res: bool
    art_luminance: float   # mean brightness (0=black .. 255=white) of the actual art
    art_is_light: bool     # True = light-colored ink → belongs on a DARK garment
    notes: list[str]


def _border_is_uniform(rgb: Image.Image, tol: int = 22) -> bool:
    """True if the outer ring of pixels is roughly one color (a removable bg)."""
    w, h = rgb.size
    px = rgb.load()
    samples = []
    step = max(1, w // 40)
    for x in range(0, w, step):
        samples.append(px[x, 0]); samples.append(px[x, h - 1])
    step = max(1, h // 40)
    for y in range(0, h, step):
        samples.append(px[0, y]); samples.append(px[w - 1, y])
    r = sum(s[0] for s in samples) / len(samples)
    g = sum(s[1] for s in samples) / len(samples)
    b = sum(s[2] for s in samples) / len(samples)
    # mean absolute deviation of border from its own average
    mad = sum(abs(s[0]-r)+abs(s[1]-g)+abs(s[2]-b) for s in samples) / (3*len(samples))
    return mad <= tol


def _remove_background(img: Image.Image, tol: int = 40) -> tuple[Image.Image, bool, float]:
    """
    Flood-fill from every border seed and turn the filled region transparent.
    Returns (rgba, confident, removed_fraction).
    """
    from PIL import ImageDraw
    rgb = img.convert("RGB")
    if not _border_is_uniform(rgb):
        return img, False, 0.0  # busy edges — don't risk gouging the art

    work = rgb.copy()
    w, h = work.size
    seeds = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
             (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]
    for s in seeds:
        ImageDraw.floodfill(work, s, _SENTINEL, thresh=tol)

    # alpha: 0 where pixel became the sentinel, else 255
    diff = ImageChops.difference(work, Image.new("RGB", work.size, _SENTINEL)).convert("L")
    alpha = diff.point(lambda v: 0 if v == 0 else 255)

    removed = 1.0 - (sum(alpha.getdata()) / 255) / (w * h)
    # Feather the mask a touch to kill jaggies from the hard threshold.
    alpha = alpha.filter(ImageFilter.GaussianBlur(0.8)).point(lambda v: 255 if v > 128 else 0)

    out = img.convert("RGBA")
    out.putalpha(alpha)
    # Confident unless we removed ~nothing (found no bg) or ~everything (ate the
    # art too). Text designs are mostly whitespace, so high removal % is normal.
    confident = 0.02 <= removed <= 0.985
    return out, confident, removed


def _resize_contain(art: Image.Image, target: tuple[int, int], margin: float = 0.06) -> Image.Image:
    tw, th = target
    canvas = Image.new("RGBA", target, (0, 0, 0, 0))
    avail = (int(tw * (1 - margin)), int(th * (1 - margin)))
    a = art.copy()
    a.thumbnail(avail, Image.LANCZOS)
    canvas.paste(a, ((tw - a.width) // 2, (th - a.height) // 2), a)
    return canvas


def _resize_cover(art: Image.Image, target: tuple[int, int]) -> Image.Image:
    return ImageOps.fit(art.convert("RGBA"), target, Image.LANCZOS)


def prepare(
    src: str | Path,
    out_path: str | Path,
    *,
    target_px: tuple[int, int],
    remove_background: bool,
    margin: float = 0.92,
    fit: str = "contain",   # kept for signature compatibility; unused
) -> PrepResult:
    src = Path(src)
    out_path = Path(out_path)
    notes: list[str] = []

    img = Image.open(src)
    img = ImageOps.exif_transpose(img)
    source_px = img.size
    img = img.convert("RGBA")

    bg_removed = False
    bg_confident = False
    if remove_background:
        # If the file already has real transparency, trust it.
        alpha_extrema = img.getchannel("A").getextrema()
        already_transparent = alpha_extrema[0] < 250
        if already_transparent:
            bg_removed = True
            bg_confident = True
            notes.append("input already had transparency — kept as-is")
        else:
            img, bg_confident, frac = _remove_background(img)
            bg_removed = bg_confident
            if bg_confident:
                notes.append(f"removed solid background (~{frac*100:.0f}% of frame)")
            else:
                notes.append("could NOT auto-remove background (busy/gradient edges) "
                             "— design may print inside a box; check the mockup")

    # Trim transparent margins so the art fills the blank.
    bbox = img.getchannel("A").getbbox()
    if bbox:
        img = img.crop(bbox)

    # Analyze the actual art (opaque pixels only) -> decides garment color.
    art_luminance, art_is_light = _art_stats(img)

    # Upscale the trimmed design to fit within (margin × print area), preserving
    # aspect. No letterbox canvas — Printify positions the transparent art itself
    # (centered) so it sits nicely inside the blank's print area.
    tw, th = int(target_px[0] * margin), int(target_px[1] * margin)
    factor = min(tw / img.width, th / img.height)
    new_size = (max(1, round(img.width * factor)), max(1, round(img.height * factor)))
    out = img.resize(new_size, Image.LANCZOS)

    # Light unsharp to counter upscale softness (gentle; over-sharpening looks crunchy).
    out = out.filter(ImageFilter.UnsharpMask(radius=2, percent=80, threshold=2))

    # Quality verdict: longest source edge vs longest target edge.
    src_long = max(source_px)
    tgt_long = max(target_px)
    low_res = src_long < 0.40 * tgt_long
    if low_res:
        notes.append(f"LOW-RES source ({src_long}px vs ~{tgt_long}px target) — text art "
                     f"still prints ok, but detailed art may look soft. Order a sample.")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(out_path, "PNG", dpi=(150, 150))

    return PrepResult(
        out_path=out_path,
        source_px=source_px,
        out_px=out.size,
        background_removed=bg_removed,
        background_confident=bg_confident,
        low_res=low_res,
        art_luminance=art_luminance,
        art_is_light=art_is_light,
        notes=notes,
    )


def _art_stats(rgba: Image.Image) -> tuple[float, bool]:
    """
    Return (mean_luminance, is_light_art) over opaque pixels.

    is_light_art uses a VOTE, not the mean: count clearly-light ink (lum>170) vs
    clearly-dark ink (lum<85); mid-tones (e.g. a blue accent) abstain. This reads
    'white text + blue accent' as light art (→ dark garment) even though its mean
    luminance sits in the middle. Ties break toward light→dark garment (black tees
    are the safer, more common default for this brand's white-ink designs).
    """
    small = rgba.copy()
    small.thumbnail((140, 140), Image.LANCZOS)
    px = small.load()
    w, h = small.size
    tot = 0.0
    n = 0
    light = dark = 0
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a < 40:
                continue
            lum = 0.299 * r + 0.587 * g + 0.114 * b
            tot += lum
            n += 1
            if lum > 170:
                light += 1
            elif lum < 85:
                dark += 1
    mean = (tot / n) if n else 128.0
    light_frac = light / n if n else 0.0
    dark_frac = dark / n if n else 0.0
    # Asymmetric prior: default to a DARK garment (CrappyRV's white/light-ink
    # designs are the staple, and light-ink art on a white tee is the worst-case
    # invisible failure). Only choose a LIGHT garment when the art is DECISIVELY
    # dark ink (e.g. black-text designs): lots of dark, little light. This is
    # robust to outlined/shadowed art whose light/dark pixels roughly balance.
    clearly_dark = (dark_frac >= 2 * light_frac) and (light_frac < 0.15)
    is_light = not clearly_dark
    return mean, is_light


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python image_prep.py <src.png> <out.png>")
        raise SystemExit(1)
    r = prepare(sys.argv[1], sys.argv[2], target_px=(4500, 5400),
                remove_background=True, fit="contain")
    print(r)
