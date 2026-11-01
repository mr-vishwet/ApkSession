"""Perceptual hashing for screen verification and annotated contact sheets."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .env import find_font

TINTS = ["#ff3b30", "#ffcc00", "#34c759", "#5ac8fa", "#af52de", "#ff9500"]


def _font(px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(find_font()), px)
    except Exception:
        return ImageFont.load_default()


def ahash(path: Path | str, size: int = 16) -> int:
    with Image.open(path) as opened:
        px = opened.convert("L").resize((size, size), Image.BILINEAR).tobytes()
    mean = sum(px) / len(px)
    bits = 0
    for p in px:
        bits = (bits << 1) | (1 if p > mean else 0)
    return bits


def differs(a: int, b: int, threshold: float = 0.18) -> bool:
    """True when two hashes are different screens (>= threshold of 256 bits differ)."""
    return bin(a ^ b).count("1") / 256.0 > threshold


def annotate(src: Path | str, out: Path, rects: list[dict], title: str = "") -> Path:
    """Draw numbered brand boxes on a still so placement can be eyeballed."""
    im = Image.open(src).convert("RGB")
    W, H = im.size
    draw = ImageDraw.Draw(im)
    f = _font(max(16, int(H * 0.018)))
    for i, r in enumerate(rects):
        x, y, w, h = (int(r[k]) for k in ("x", "y", "w", "h"))
        tint = TINTS[i % len(TINTS)]
        draw.rectangle([x, y, x + w, y + h], outline=tint,
                       width=max(2, int(H * 0.004)))
        draw.rectangle([x, max(0, y - int(H * 0.026)), x + int(H * 0.075),
                        max(0, y - int(H * 0.026)) + int(H * 0.024)], fill=tint)
        draw.text((x + 6, max(0, y - int(H * 0.0245))), f"{i}", font=f, fill="black")
    if title:
        draw.rectangle([0, 0, W, int(H * 0.03)], fill="black")
        draw.text((8, 4), title, font=_font(max(16, int(H * 0.02))), fill="white")
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out)
    return out


def _load(im: Path | str | Image.Image) -> Image.Image:
    return Image.open(im) if isinstance(im, (str, Path)) else im


def edge_energy(im: "Image.Image | Path | str", smooth: float = 0.0) -> float:
    """Mean edge response. Blurring destroys lettering; flat art never had any."""
    from PIL import ImageFilter

    im = _load(im)
    if smooth > 0:
        im = im.filter(ImageFilter.GaussianBlur(smooth))
    g = im.convert("L").filter(ImageFilter.FIND_EDGES)
    return sum(g.getdata()) / (g.size[0] * g.size[1])


def _interior(path, trim: float = 0.12) -> Image.Image:
    """Drop the border band: a blurred crop keeps the app-bar edges that cross it,
    and those straight lines are not branding."""
    im = _load(path)
    w, h = im.size
    mx, my = int(w * trim), int(h * trim)
    return im.crop((mx, my, max(mx + 8, w - mx), max(my + 8, h - my)))


def mask_state(before: Path | str, after: Path | str, mode: str = "blur") -> str:
    """Four-way verdict per region.

    Straight UI edges survive any blur, so this measures lettering-like detail in the
    interior only. `untouched` means the box changed nothing at all - wrong place or
    wrong time window. `review` means some structure survived and needs an eye.
    Neither is proof of a leak, and neither excuses skipping the full-frame sweep.
    """
    a = edge_energy(_interior(before))
    aft = _interior(after)
    b = edge_energy(aft, smooth=max(3.0, min(aft.size) / 12.0)
                    if mode == "pixelate" else 0.0)
    if a < 1.5:
        return "flat source"
    if b > a * 0.92:
        return "unchanged - box is wrong"
    ratio = b / a
    if ratio > 0.6:
        return "weakly reduced - read the crop"
    # Edge energy saturates: sigma 12 and sigma 100 differ only 0.23 vs 0.15 here,
    # and sigma 22 was still readable. A low ratio is necessary, never sufficient.
    return "strongly reduced - confirm by eye"


def contact_sheet(items: list[tuple[str, Path | str]], out: Path,
                  cols: int = 3, cell_h: int = 420) -> Path:
    """Labelled grid of stills/crops - the artifact the user reviews."""
    if not items:
        raise ValueError("nothing to put on the sheet")
    pad, cap_h = 10, 26
    cells = []
    for label, p in items:
        im = Image.open(p).convert("RGB")
        h = min(cell_h, im.size[1])
        im = im.resize((max(1, int(im.size[0] * h / im.size[1])), h), Image.LANCZOS)
        cells.append((label, im))
    rows = -(-len(cells) // cols)
    cw = max(im.size[0] for _, im in cells) + pad
    ch = cell_h + cap_h + pad
    sheet = Image.new("RGB", (cols * cw + pad, rows * ch + pad), (18, 18, 20))
    draw = ImageDraw.Draw(sheet)
    f = _font(16)
    for i, (label, im) in enumerate(cells):
        cx = pad + (i % cols) * cw
        cy = pad + (i // cols) * ch
        sheet.paste(im, (cx, cy))
        draw.text((cx + 2, cy + cell_h + 4), label[:110], font=f, fill="#e8e8ea")
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out


def crop_regions(src: Path | str, rects: list[dict], out_dir: Path,
                 prefix: str = "region") -> list[Path]:
    """Cut each brand rect out of a frame - proof that the blur covers the mark."""
    im = Image.open(src).convert("RGB")
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, r in enumerate(rects):
        box = (int(r["x"]), int(r["y"]),
               int(r["x"] + r["w"]), int(r["y"] + r["h"]))
        box = (max(0, box[0]), max(0, box[1]),
               min(im.size[0], box[2]), min(im.size[1], box[3]))
        if box[2] - box[0] < 4 or box[3] - box[1] < 4:
            continue
        p = out_dir / f"{prefix}_{i:02d}.png"
        im.crop(box).save(p)
        paths.append(p)
    return paths
