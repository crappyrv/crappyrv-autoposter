"""
thumbnail.py — generate a branded 1280x720 YouTube thumbnail from a video.

Why: left to itself YouTube grabs a random (often blurry, mid-motion) frame,
which reads as nothing in the feed and kills click-through. This module picks
the SHARPEST candidate frame from the clip and composes a brand thumbnail:
punched-up photo on the right, a bold hook (Anton) on a brand panel on the left,
plus the lemon + CRAPPYRV wordmark.

Cross-platform: frame extraction uses the static ffmpeg binary bundled by
`imageio-ffmpeg`, so the SAME code runs locally (macOS) and on GitHub Actions
(Linux). No system ffmpeg needed.

Best-effort by design: every entry point returns None (and logs) on any failure
rather than raising — a thumbnail problem must NEVER fail the actual publish.
The video still posts; it just keeps YouTube's default frame.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Resolve bundled assets relative to this file (so it works from any CWD / on CI).
_HERE = Path(__file__).resolve().parent
FONT_ANTON = _HERE / "assets" / "fonts" / "Anton-Regular.ttf"
FONT_MARKER = _HERE / "assets" / "fonts" / "PermanentMarker-Regular.ttf"

W, H = 1280, 720  # YouTube recommended thumbnail size

# Brand palette (mirrors styles.css / the merch + cover scripts).
CREAM = (250, 246, 237)
INK = (21, 17, 10)
GOLD = (200, 150, 50)
LEMON = (245, 205, 60)
RED = (200, 50, 40)

STYLES = {
    # style -> (panel bg, text color, accent)
    "ink": (INK, CREAM, GOLD),
    "lemon": (LEMON, INK, RED),
    "red": (RED, CREAM, LEMON),
}


# --- ffmpeg helpers ----------------------------------------------------------
def _ffmpeg_exe() -> Optional[str]:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ffmpeg unavailable (%s); skipping thumbnail.", exc)
        return None


def _probe_duration(exe: str, video: Path) -> Optional[float]:
    """Parse 'Duration: HH:MM:SS.ss' from ffmpeg's stderr."""
    try:
        r = subprocess.run(
            [exe, "-i", str(video)], capture_output=True, text=True, timeout=120
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("ffmpeg probe failed (%s).", exc)
        return None
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", r.stderr)
    if not m:
        return None
    hh, mm, ss = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return hh * 3600 + mm * 60 + ss


def _extract_frame(exe: str, video: Path, t: float, dest: Path) -> bool:
    try:
        r = subprocess.run(
            [exe, "-y", "-ss", f"{t:.2f}", "-i", str(video),
             "-frames:v", "1", "-q:v", "2", str(dest)],
            capture_output=True, text=True, timeout=120,
        )
        return r.returncode == 0 and dest.exists() and dest.stat().st_size > 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("frame extract at %.1fs failed (%s).", t, exc)
        return False


def _score_frame(path: Path) -> float:
    """Sharpness score (edge energy), penalizing too-dark / too-blown frames."""
    from PIL import Image, ImageFilter
    import numpy as np

    im = Image.open(path).convert("L")
    im.thumbnail((480, 480))
    arr = np.asarray(im, dtype="float32")
    brightness = arr.mean()
    edges = np.asarray(im.filter(ImageFilter.FIND_EDGES), dtype="float32")
    sharp = float(edges.var())
    # penalize extremes of brightness (near-black or blown-out frames)
    if brightness < 40 or brightness > 225:
        sharp *= 0.4
    return sharp


def _best_frame(exe: str, video: Path, workdir: Path) -> Optional[Path]:
    dur = _probe_duration(exe, video) or 0.0
    # sample across the middle (avoid the very first/last beats)
    if dur > 0:
        fracs = [0.15, 0.30, 0.45, 0.60, 0.75]
        times = [dur * f for f in fracs]
    else:
        times = [2.0, 5.0, 9.0, 14.0]  # fallback if duration unknown
    best, best_score = None, -1.0
    for i, t in enumerate(times):
        dest = workdir / f"cand_{i}.jpg"
        if not _extract_frame(exe, video, t, dest):
            continue
        try:
            s = _score_frame(dest)
        except Exception as exc:  # noqa: BLE001
            logger.debug("scoring failed for %s (%s)", dest, exc)
            s = 0.0
        if s > best_score:
            best, best_score = dest, s
    if best:
        logger.info("Selected thumbnail base frame %s (score %.0f).", best.name, best_score)
    return best


# --- composition -------------------------------------------------------------
def _load_photo(src: Path, pw: int, ph: int, face_bias: float = 0.40):
    """Cover-fit `src` into a pw x ph panel, biased to keep the face (upper-mid)."""
    from PIL import Image, ImageEnhance

    im = Image.open(src).convert("RGB")
    im = ImageEnhance.Contrast(im).enhance(1.12)
    im = ImageEnhance.Color(im).enhance(1.18)
    scale = max(pw / im.width, ph / im.height)  # cover
    new = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))))
    left = (new.width - pw) // 2
    top = int(new.height * face_bias) - ph // 2
    top = max(0, min(top, new.height - ph))
    return new.crop((left, top, left + pw, top + ph))


def _feather_left(photo, feather: int = 90):
    from PIL import Image
    import numpy as np

    photo = photo.convert("RGBA")
    grad = np.linspace(0, 255, feather).astype("uint8")
    mask = np.full((photo.height, photo.width), 255, dtype="uint8")
    mask[:, :feather] = grad[None, :]
    photo.putalpha(Image.fromarray(mask, "L"))
    return photo


def _draw_lemon(d, cx, cy, r, color=LEMON):
    d.ellipse([cx - r, cy - int(r * 0.8), cx + r, cy + int(r * 0.8)], fill=color)
    d.ellipse([cx + r - 6, cy - 6, cx + r + 6, cy + 6], fill=color)
    d.polygon(
        [(cx - 2, cy - int(r * 0.8)), (cx + 14, cy - int(r * 0.8) - 14),
         (cx + 18, cy - int(r * 0.8) + 2)],
        fill=(90, 150, 60),
    )


def _fit_font(path: str, text: str, max_w: int, start: int, min_size: int = 40):
    from PIL import ImageFont

    s = start
    while s > min_size:
        f = ImageFont.truetype(path, s)
        if f.getbbox(text)[2] <= max_w:
            return f
        s -= 4
    return ImageFont.truetype(path, min_size)


def _text_outlined(d, xy, text, font, fill, outline=INK, ow=4, anchor="la"):
    x, y = xy
    for dx in range(-ow, ow + 1):
        for dy in range(-ow, ow + 1):
            if dx * dx + dy * dy <= ow * ow:
                d.text((x + dx, y + dy), text, font=font, fill=outline, anchor=anchor)
    d.text(xy, text, font=font, fill=fill, anchor=anchor)


def _split_hook(hook: str, max_lines: int = 2) -> List[str]:
    """Balance a short hook across up to max_lines lines (by word)."""
    words = hook.strip().split()
    if len(words) <= 1:
        return words or [""]
    if len(words) == 2:
        return words  # one word per line reads big & punchy
    # 3-4 words: split near the middle
    mid = (len(words) + 1) // 2
    return [" ".join(words[:mid]), " ".join(words[mid:])][:max_lines]


def compose(frame: Path, out: Path, hook: str, sub: str = "", style: str = "ink") -> bool:
    """Compose the branded thumbnail. Returns True on success."""
    from PIL import Image, ImageDraw, ImageFont

    panel_bg, txt, accent = STYLES.get(style, STYLES["ink"])
    pw = 600
    photo = _feather_left(_load_photo(frame, pw, H), 90)

    canvas = Image.new("RGB", (W, H), panel_bg)
    canvas.paste(photo, (W - pw, 0), photo)
    d = ImageDraw.Draw(canvas)

    lines = _split_hook(hook)
    line_max = W - pw - 80
    fonts = [_fit_font(str(FONT_ANTON), ln, line_max, 150) for ln in lines]
    total_h = sum(f.getbbox(ln)[3] for f, ln in zip(fonts, lines)) + (len(lines) - 1) * 8
    tx, ty = 48, (H - total_h) // 2 - 30
    hook_outline = accent if style != "red" else INK
    for f, ln in zip(fonts, lines):
        _text_outlined(d, (tx, ty), ln, f, txt, outline=hook_outline, ow=4)
        ty += f.getbbox(ln)[3] + 8

    if sub:
        sf = _fit_font(str(FONT_MARKER), sub, line_max, 56, 28)
        d.text((tx + 4, ty + 14), sub, font=sf, fill=accent)

    _draw_lemon(d, tx + 22, H - 52, 22)
    d.text((tx + 58, H - 78), "CRAPPYRV", font=ImageFont.truetype(str(FONT_ANTON), 46), fill=txt)

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, "JPEG", quality=90)  # JPEG keeps it well under YouTube's 2MB cap
    return True


# --- public entry point ------------------------------------------------------
def generate(video: Path, hook: str, sub: str = "", style: str = "ink",
             out: Optional[Path] = None) -> Optional[Path]:
    """
    Build a branded thumbnail for `video`. Returns the image Path, or None if
    anything went wrong (best-effort — caller treats None as "no custom thumb").
    """
    if not FONT_ANTON.exists():
        logger.warning("Brand font missing at %s; skipping thumbnail.", FONT_ANTON)
        return None
    exe = _ffmpeg_exe()
    if not exe:
        return None
    try:
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            frame = _best_frame(exe, video, workdir)
            if not frame:
                logger.warning("No usable frame extracted; skipping thumbnail.")
                return None
            if out is None:
                out = video.parent / f"{video.stem}.thumb.jpg"
            if compose(frame, out, hook=hook, sub=sub, style=style):
                logger.info("Thumbnail written: %s", out)
                return out
    except Exception as exc:  # noqa: BLE001 - never let a thumbnail break publishing
        logger.error("Thumbnail generation failed (%s); continuing without one.", exc, exc_info=True)
    return None
