"""Karaoke-style subtitles for Studio.

Each spoken line is shown as big outlined text whose current word lights up as
it is spoken — the style short-video viewers expect.

Word timing comes from one of two places:
  * a speech-to-text engine the user connected in Engines (Groq or OpenAI
    Whisper) for exact timestamps, or
  * an estimate from the audio length when none is connected. It is free and
    surprisingly close because speech pace follows word length.

The result is an ASS subtitle file that ffmpeg burns into the picture.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import httpx

from flowboard.services import engines

logger = logging.getLogger(__name__)

Word = tuple[str, float, float]  # (text, start, end) in seconds, relative to the line start


def tokens(text: str) -> list[str]:
    return [t for t in re.split(r"\s+", text.strip()) if t]


def estimate_words(text: str, duration: float) -> list[Word]:
    """Spread ``duration`` over the words, weighted by their length."""
    toks = tokens(text)
    if not toks:
        return []
    weights = [len(re.sub(r"\W", "", t)) + 2 for t in toks]
    total = float(sum(weights))
    out: list[Word] = []
    t = 0.0
    for tok, w in zip(toks, weights):
        d = duration * w / total
        out.append((tok, t, t + d))
        t += d
    return out


def estimate_duration(text: str) -> float:
    """How long a line takes to say when no audio exists to measure."""
    return max(1.2, len(tokens(text)) * 0.32 + 0.4)


def _stt_provider() -> Optional[tuple[str, str, str, str]]:
    """(provider id, base url, key, model) of the best connected speech-to-text engine."""
    data = engines._load()
    chosen = engines.assigned_engine(data, "subtitles")
    order = ([chosen] if chosen else []) + ["groq", "openai"]
    for pid in order:
        key = (data.get("keys") or {}).get(pid)
        model = ((data.get("state") or {}).get(pid) or {}).get("models", {}).get("stt")
        if key and model:
            return pid, engines.PROVIDERS[pid]["base_url"], key, model
    return None


def whisper_words(audio: Path, text: str) -> Optional[list[Word]]:
    """Exact word timings from a connected Whisper engine, or None if unavailable."""
    prov = _stt_provider()
    if prov is None:
        return None
    pid, base, key, model = prov
    try:
        with audio.open("rb") as fh:
            r = httpx.post(
                f"{base}/audio/transcriptions", headers={"Authorization": f"Bearer {key}"}, timeout=60,
                files={"file": (audio.name, fh)},
                data={"model": model, "response_format": "verbose_json", "timestamp_granularities[]": "word"},
            )
        if r.status_code != 200:
            logger.info("whisper (%s) replied %s", pid, r.status_code)
            return None
        heard = [(w["word"], float(w["start"]), float(w["end"])) for w in r.json().get("words") or []]
    except (httpx.HTTPError, ValueError, KeyError, OSError) as exc:
        logger.info("whisper failed: %s", exc)
        return None
    return align_to_text(text, heard)


def align_to_text(text: str, heard: list[Word]) -> Optional[list[Word]]:
    """Put the *script's* words on the timeline Whisper measured.

    Whisper may split or merge words differently from the script, so when the
    counts match we use its timings directly; otherwise we keep only the overall
    span it measured and spread the script's own words across it.
    """
    toks = tokens(text)
    if not toks or not heard:
        return None
    if len(toks) == len(heard):
        return [(t, h[1], h[2]) for t, h in zip(toks, heard)]
    start, end = heard[0][1], heard[-1][2]
    span = estimate_words(text, max(0.3, end - start))
    return [(w, s + start, e + start) for w, s, e in span]


def line_words(text: str, audio: Optional[Path], duration: float) -> list[Word]:
    exact = whisper_words(audio, text) if audio is not None else None
    return exact or estimate_words(text, duration)


# ── ASS output ──────────────────────────────────────────────────────────────

def _ts(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _esc(s: str) -> str:
    return s.replace("\\", "").replace("{", "(").replace("}", ")")


def build_ass(lines: list[dict], width: int, height: int, *, karaoke: bool, font: str, size: int, margin_v: int) -> str:
    """``lines``: [{"start": s, "end": s, "words": [(text, start, end), …]}] — word times relative to ``start``."""
    outline = max(3, round(size / 8))
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        # Primary = highlight (yellow), Secondary = not-yet-spoken (white). ASS colours are &HAABBGGRR.
        f"Style: Cap,{font},{size},&H0046E1FF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,"
        f"{outline},0,2,{int(width * 0.06)},{int(width * 0.06)},{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for ln in lines:
        words: list[Word] = ln["words"]
        if not words:
            continue
        if karaoke:
            parts = []
            for i, (w, s, e) in enumerate(words):
                # Hold each word until the next one starts so there is no flicker between them.
                nxt = words[i + 1][1] if i + 1 < len(words) else max(e, s + 0.05)
                parts.append(f"{{\\k{max(1, round((nxt - s) * 100))}}}{_esc(w)} ")
            text = "".join(parts).rstrip()
        else:
            text = "{\\c&HFFFFFF&}" + " ".join(_esc(w) for w, _, _ in words)
        events.append(f"Dialogue: 0,{_ts(ln['start'])},{_ts(ln['end'])},Cap,,0,0,0,,{text}")
    return head + "\n".join(events) + "\n"
