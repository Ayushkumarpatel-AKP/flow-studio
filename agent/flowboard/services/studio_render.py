"""Comic-video renderer for Studio.

Takes panels (a generated still and, optionally, an animated clip) plus the
comic text for each, draws speech bubbles / captions / sound effects over
them and joins everything into one MP4 with ffmpeg.

Bubbles are positioned the same way the editor shows them: ``x``/``y`` are the
bubble centre and ``w`` its text width, all in percent of the frame, and
``tx``/``ty`` is where the tail points. A bubble with no position falls back to
a sensible default for its ``side``, so scripts written by an LLM need no layout
knowledge.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from flowboard.services import subtitles, voice as voice_mod

logger = logging.getLogger(__name__)

FPS = 30
STILL_SECONDS = 6.0
CLIP_SECONDS = 7.9

# Output frame size for each supported format.
FORMATS: dict[str, tuple[int, int]] = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
    "1:1": (1024, 1024),
}

_BOLD_FONTS = [
    "C:/Windows/Fonts/comicbd.ttf",
    "/usr/share/fonts/truetype/comic/comicbd.ttf",
    "/System/Library/Fonts/Supplemental/Comic Sans MS Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_SFX_FONTS = [
    "C:/Windows/Fonts/impact.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Impact.ttf",
]


def _font(candidates: list[str], size: int) -> ImageFont.ImageFont:
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def ffmpeg_path() -> Optional[str]:
    return shutil.which("ffmpeg")


def frame_size(fmt: str) -> tuple[int, int]:
    return FORMATS.get(fmt, FORMATS["16:9"])


def _unit(w: int, h: int) -> float:
    """Pixels per 'design pixel'. Text is designed against a 1280-wide frame
    and scaled so portrait frames keep readable type."""
    return w / 1280 if w >= h else w / 720 * 0.95


def default_bubble_pos(side: str, index: int, w: int, h: int) -> tuple[float, float]:
    """(x%, y%) for a bubble that was never dragged."""
    if w >= h:
        return (26.0 if side == "left" else 74.0), 14.0
    return 50.0, (9.0 + 13.0 * index)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: float) -> list[str]:
    lines: list[str] = []
    cur = ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def _bubble(b: dict[str, Any], index: int, w: int, h: int) -> Image.Image:
    u = _unit(w, h)
    side = "right" if b.get("side") == "right" else "left"
    dx, dy = default_bubble_pos(side, index, w, h)
    cx = (b.get("x") if b.get("x") is not None else dx) / 100 * w
    cy = (b.get("y") if b.get("y") is not None else dy) / 100 * h
    max_w = (b.get("w") if b.get("w") is not None else 36.0) / 100 * w
    tx = (b.get("tx") if b.get("tx") is not None else (cx / w * 100)) / 100 * w
    ty = (b.get("ty") if b.get("ty") is not None else (cy / h * 100 + 24)) / 100 * h
    text = (b.get("text") or "").strip()

    ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    size = int((34 if len(text) <= 60 else 28) * u)
    f = _font(_BOLD_FONTS, size)
    lines = _wrap(d, text, f, max_w)
    lh = size + int(6 * u)
    tw = max(d.textlength(line, font=f) for line in lines)
    th = lh * len(lines)
    # An ellipse must clear the corners of the text block, not just its edges.
    rx, ry = tw / 2 * 1.16 + 34 * u, th / 2 * 1.32 + 26 * u
    mask = Image.new("L", (w, h), 0)
    m = ImageDraw.Draw(mask)
    m.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=255)
    bx = max(cx - rx * 0.45, min(cx + rx * 0.45, tx))
    m.polygon([(bx - 16 * u, cy + ry - 14 * u), (bx + 16 * u, cy + ry - 14 * u), (tx, ty)], fill=255)
    border = mask.filter(ImageFilter.MaxFilter(max(3, int(9 * u) | 1)))
    ov.paste((0, 0, 0, 255), mask=border)
    ov.paste((255, 255, 255, 255), mask=mask)
    d = ImageDraw.Draw(ov)
    y = cy - th / 2
    for line in lines:
        d.text((cx - d.textlength(line, font=f) / 2, y), line, font=f, fill=(0, 0, 0, 255))
        y += lh
    return ov


def _caption(text: str, w: int, h: int) -> Image.Image:
    u = _unit(w, h)
    ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    f = _font(_BOLD_FONTS, int(30 * u))
    tw = d.textlength(text, font=f)
    x0, y0, x1, y1 = 24 * u, h - 72 * u, 24 * u + tw + 40 * u, h - 20 * u
    o = 6 * u
    d.rectangle([x0 + o, y0 + o, x1 + o, y1 + o], fill=(0, 0, 0, 255))
    d.rectangle([x0, y0, x1, y1], fill=(255, 225, 70, 255), outline=(0, 0, 0, 255), width=max(2, int(4 * u)))
    d.text((x0 + 20 * u, y0 + 7 * u), text, font=f, fill=(0, 0, 0, 255))
    return ov


def _sfx(text: str, w: int, h: int, pos: Optional[tuple[float, float]] = None) -> Image.Image:
    u = _unit(w, h)
    f = _font(_SFX_FONTS, int(120 * u))
    band = int(300 * u)
    tmp = Image.new("RGBA", (w, band), (0, 0, 0, 0))
    d = ImageDraw.Draw(tmp)
    d.text((w / 2, band / 2), text, font=f, fill=(255, 60, 40, 255), anchor="mm",
           stroke_width=max(4, int(10 * u)), stroke_fill=(0, 0, 0, 255))
    tmp = tmp.rotate(-8, resample=Image.BICUBIC, center=(w / 2, band / 2))
    px, py = pos if pos else (50.0, 46.0)
    ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ov.paste(tmp, (int(px / 100 * w - w / 2), int(py / 100 * h - band / 2)), tmp)
    return ov


def _run(cmd: list[str], cwd: Optional[Path] = None) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd) if cwd else None)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-400:]}")


_FONT_FAMILY = {"comicbd.ttf": "Comic Sans MS", "arialbd.ttf": "Arial", "DejaVuSans-Bold.ttf": "DejaVu Sans"}


def _caption_font(work: Path) -> str:
    """Copy a bold font next to the subtitle file and return its family name."""
    for path in _BOLD_FONTS:
        if os.path.exists(path):
            shutil.copy(path, work / "capfont.ttf")
            return _FONT_FAMILY.get(os.path.basename(path), "Arial")
    return "Arial"


def _plan_panel(panel: dict[str, Any], voice: Optional[dict], captions: Optional[dict]) -> list[dict[str, Any]]:
    """Decide when each line appears (and is spoken) in a panel.

    With voiceover or captions on, lines follow each other as long as they take to
    say; otherwise the original fixed rhythm is kept.
    """
    speaking = bool(voice and voice.get("enabled"))
    style = (captions or {}).get("style", "off")
    timed = speaking or style != "off"
    speed = float((voice or {}).get("speed") or 1.0)
    items: list[dict[str, Any]] = []
    t = 0.8
    for k, b in enumerate(panel.get("bubbles") or []):
        text = (b.get("text") or "").strip()
        if not text:
            continue
        audio: Optional[Path] = None
        if speaking:
            vid = (voice.get("right") if b.get("side") == "right" else voice.get("left")) or ""
            try:
                audio, _used = voice_mod.synthesize(text, voice.get("engine", "edge"), vid, speed)
                dur = max(0.6, voice_mod.audio_duration(audio))
            except voice_mod.VoiceError as exc:
                logger.info("no voice for a line: %s", exc)
                audio, dur = None, subtitles.estimate_duration(text)
        else:
            dur = subtitles.estimate_duration(text) if timed else 2.0
        start = t if timed else 1.0 + 2.0 * k
        words = subtitles.line_words(text, audio, dur) if style != "off" else []
        items.append({"k": k, "bubble": b, "text": text, "audio": audio, "start": start, "end": start + dur, "words": words})
        t = start + dur + 0.35
    return items


def _render_panel(ff: str, panel: dict[str, Any], idx: int, work: Path, w: int, h: int,
                  voice: Optional[dict] = None, captions: Optional[dict] = None) -> Path:
    still: Optional[Path] = panel.get("still_path")
    clip: Optional[Path] = panel.get("clip_path")
    animated = bool(clip)
    base = CLIP_SECONDS if animated else STILL_SECONDS
    cap_cfg = captions or {}
    style = cap_cfg.get("style", "off")
    show_bubbles = cap_cfg.get("bubbles", True)

    items = _plan_panel(panel, voice, captions)
    dur = max(base, (items[-1]["end"] + 0.9) if items else 0.0)

    layers: list[tuple[Path, float]] = []
    caption = (panel.get("caption") or "").strip()
    if caption:
        p = work / f"{idx}_cap.png"
        _caption(caption, w, h).save(p)
        layers.append((p, 0.2))
    sfx = (panel.get("sfx") or "").strip()
    if sfx:
        p = work / f"{idx}_sfx.png"
        pos = (panel["sfx_x"], panel["sfx_y"]) if panel.get("sfx_x") is not None and panel.get("sfx_y") is not None else None
        _sfx(sfx, w, h, pos).save(p)
        layers.append((p, 0.6))
    if show_bubbles:
        for it in items:
            p = work / f"{idx}_b{it['k']}.png"
            _bubble(it["bubble"], it["k"], w, h).save(p)
            layers.append((p, it["start"]))

    cmd = [ff, "-y", "-loglevel", "error"]
    if animated:
        cmd += ["-i", str(clip)]
    else:
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.3f}", "-i", str(still)]
    for p, _ in layers:
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.3f}", "-i", str(p)]
    voiced = [(it["audio"], it["start"]) for it in items if it["audio"] is not None]
    a_first = len(layers) + 1
    for path, _ in voiced:
        cmd += ["-i", str(path)]
    silent = a_first + len(voiced)
    cmd += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=44100:cl=stereo"]

    extra = max(0.0, dur - CLIP_SECONDS)
    if animated:
        hold = f",tpad=stop_mode=clone:stop_duration={extra:.3f}" if extra > 0.05 else ""
        fc = f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={FPS}{hold}[v0];"
    else:
        zw, zh = int(w * 1.25) // 2 * 2, int(h * 1.25) // 2 * 2
        fc = (f"[0:v]scale={zw}:{zh}:force_original_aspect_ratio=increase,crop={zw}:{zh},"
              f"zoompan=z='1+0.07*on/{int(dur * FPS)}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
              f"d=1:s={w}x{h}:fps={FPS}[v0];")
    last = "v0"
    for n, (_, start) in enumerate(layers, 1):
        fc += (f"[{n}:v]format=rgba,fade=t=in:st={start:.3f}:d=0.2:alpha=1[o{n}];"
               f"[{last}][o{n}]overlay=0:0:format=auto[v{n}];")
        last = f"v{n}"

    # Burned-in captions: karaoke (word lights up as it is spoken) or classic.
    lines = [{"start": it["start"], "end": it["end"] + 0.25, "words": it["words"]} for it in items if it["words"]]
    if style != "off" and lines:
        u = _unit(w, h)
        ass = subtitles.build_ass(lines, w, h, karaoke=(style == "karaoke"), font=_caption_font(work),
                                  size=int(62 * u), margin_v=int(104 * u))
        (work / f"{idx}.ass").write_text(ass, encoding="utf-8")
        fc += f"[{last}]ass=filename={idx}.ass:fontsdir=.[vs];"
        last = "vs"

    fc += f"[{last}]fade=t=in:st=0:d=0.3,fade=t=out:st={dur - 0.3:.3f}:d=0.3,format=yuv420p[out];"

    chains: list[str] = []
    labels: list[str] = []
    if animated:
        gain = 0.35 if voiced else 1.0
        chains.append(f"[0:a]aresample=44100,aformat=channel_layouts=stereo,volume={gain}[ab]")
        labels.append("[ab]")
    chains.append(f"[{silent}:a]anull[as]")
    labels.append("[as]")
    for j, (_, st) in enumerate(voiced):
        chains.append(f"[{a_first + j}:a]aresample=44100,aformat=channel_layouts=stereo,"
                      f"adelay={int(st * 1000)}:all=1[va{j}]")
        labels.append(f"[va{j}]")
    chains.append("".join(labels) + f"amix=inputs={len(labels)}:duration=longest:normalize=0,"
                  f"atrim=0:{dur:.3f},afade=t=in:d=0.3,afade=t=out:st={dur - 0.3:.3f}:d=0.3[aout]")
    fc += ";".join(chains)

    out = work / f"clip_{idx:02d}.mp4"
    cmd += ["-filter_complex", fc, "-map", "[out]", "-map", "[aout]", "-t", f"{dur:.3f}",
            "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", str(out)]
    _run(cmd, cwd=work)
    return out


def render_video(panels: list[dict[str, Any]], out_path: Path, work: Path,
                 on_progress=None, fmt: str = "16:9",
                 voice: Optional[dict] = None, captions: Optional[dict] = None) -> Path:
    """Render ``panels`` to ``out_path``. Blocking — call from a thread.

    Each panel: ``still_path`` and/or ``clip_path`` (a local file), ``caption``,
    ``sfx`` (+ optional ``sfx_x``/``sfx_y``), ``bubbles`` = [{"text", "side",
    "x", "y", "w", "tx", "ty"}].

    ``voice`` = {"enabled", "engine", "left", "right", "speed"} reads each bubble
    aloud and paces the panel to it; ``captions`` = {"style": off|karaoke|classic,
    "bubbles": bool} burns subtitles in.
    """
    ff = ffmpeg_path()
    if ff is None:
        raise RuntimeError("ffmpeg_not_found: install ffmpeg and make sure it is on PATH")
    w, h = frame_size(fmt)
    work.mkdir(parents=True, exist_ok=True)
    clips: list[Path] = []
    for i, panel in enumerate(panels, 1):
        if not panel.get("still_path") and not panel.get("clip_path"):
            raise RuntimeError(f"panel {i} has no image or clip")
        clips.append(_render_panel(ff, panel, i, work, w, h, voice, captions))
        if on_progress:
            on_progress(i, len(panels))
    listing = work / "list.txt"
    listing.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips), encoding="utf-8")
    _run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
          "-c", "copy", str(out_path)])
    return out_path
