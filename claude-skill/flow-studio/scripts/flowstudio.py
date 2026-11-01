#!/usr/bin/env python3
"""Flow Studio command line — lets Claude (or you) make a comic video with one command.

Uses only the Python standard library. Talks to the local Flow Studio agent (default http://localhost:8434).

  python flowstudio.py status
  python flowstudio.py project https://flow.google.com/project/<id>
  python flowstudio.py make "Two stickmen survive a zombie eclipse" --panels 6 --out zombie.mp4
  python flowstudio.py make "..." --format 9:16 --voice --captions karaoke --post telegram,folder
  python flowstudio.py connectors
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:8434"


def call(method: str, path: str, body: dict | None = None, timeout: float = 180, raw: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            return payload if raw else json.loads(payload or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail", "")
        except Exception:  # noqa: BLE001
            detail = ""
        raise SystemExit(f"{method} {path} failed: HTTP {e.code} {detail}")
    except urllib.error.URLError:
        raise SystemExit(f"Cannot reach the Flow Studio agent at {BASE}. Start it first (see SKILL.md: 'Start the app').")


def say(msg: str) -> None:
    print(msg, flush=True)


def cmd_status(_: argparse.Namespace) -> None:
    h = call("GET", "/api/health")
    say(f"agent: running | extension (Flow bridge): {'connected' if h.get('extension_connected') else 'NOT connected'}")
    try:
        say("Flow project: " + str(call("GET", "/api/flow/projects/pinned").get("flow_project_id")))
    except SystemExit:
        pass
    eng = call("GET", "/api/engines")
    say("AI engines: " + ", ".join(f"{j}={c['status']['state']}" for j, c in eng["config"].items()))
    try:
        r = call("GET", "/api/flow-runner")
        say(f"background Flow: {r['state']} — {r['hint']}")
    except SystemExit:
        pass


def cmd_connectors(_: argparse.Namespace) -> None:
    for c in call("GET", "/api/connectors")["connectors"]:
        say(f"{c['group']:8} {c['id']:14} {c['status']}")


def pin_project(url_or_id: str) -> str:
    m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", url_or_id)
    if not m:
        raise SystemExit("That does not contain a Flow project id (looks like 36c458aa-d5b3-...).")
    call("PUT", "/api/flow/projects/pinned", {"flow_project_id": m.group(0)})
    return m.group(0)


def cmd_project(a: argparse.Namespace) -> None:
    say("Flow project set to " + pin_project(a.url))


def draw(prompt: str, project_id: str, vertical: bool, fmt: str) -> tuple[str, str]:
    """One picture: Flow first, then the connected picture services. Returns (media_id, who drew it)."""
    err = ""
    for _attempt in range(3):
        req = call("POST", "/api/requests", {"type": "gen_image", "params": {
            "prompt": prompt, "project_id": project_id, "variant_count": 1,
            "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT" if vertical else "IMAGE_ASPECT_RATIO_LANDSCAPE"}})
        for _ in range(100):
            time.sleep(3)
            s = call("GET", f"/api/requests/{req['id']}")
            if s["status"] == "done":
                ids = [x for x in (s.get("result") or {}).get("media_ids", []) if x]
                if ids:
                    return ids[0], "Google Flow"
                err = "no picture returned"
                break
            if s["status"] in ("failed", "canceled", "timeout"):
                err = str(s.get("error") or s["status"])
                break
        if re.search(r"QUOTA|ACCESS_DENIED|UNSAFE", err):
            break
    if "UNSAFE" in err.upper():
        raise SystemExit(f"Flow refused this picture as unsafe: {err}")
    try:                                              # backup: OpenRouter/OpenAI key or a Connect picture service
        r = call("POST", "/api/studio/image", {"prompt": prompt, "format": fmt})
        return r["media_id"], r.get("engine", "backup")
    except SystemExit as exc:
        raise SystemExit(f"Could not draw a picture. Flow said: {err}. Backup said: {exc}")


def cmd_make(a: argparse.Namespace) -> None:
    h = call("GET", "/api/health")
    if not h.get("extension_connected") and not a.script_only:
        say("WARNING: the Flow extension is not connected. Open the Flow tab (or start background Flow) first.")
    link = re.search(r"https?://\S*flow\.google\S*/project/[0-9a-f-]{36}\S*", a.idea, re.I)
    if link:                                  # a Flow link inside the idea means "draw in this project"
        a.project = a.project or link.group(0)
        a.idea = re.sub(r"\s{2,}", " ", a.idea.replace(link.group(0), "")).strip()
        if len(a.idea) < 3:
            raise SystemExit("Flow project understood. Now give the story idea too, e.g. make \"zombie eclipse comic\" <link>.")
    if a.project:
        say("Flow project set to " + pin_project(a.project))
    project_id = call("GET", "/api/flow/projects/pinned").get("flow_project_id") or ""
    if not project_id and not a.script_only:
        raise SystemExit("No Flow project is set. Run: flowstudio.py project <flow project url>")

    script = call("POST", "/api/studio/script", {"idea": a.idea, "panels": a.panels, "style": a.style, "tone": a.tone}, timeout=180)
    say(f"Script: \"{script.get('title')}\" — {len(script['panels'])} panels (written by: {script.get('source')})")
    if a.script_only:
        print(json.dumps(script, indent=1))
        return

    styles = call("GET", "/api/studio/styles")
    style_text = styles["styles"].get(a.style, styles["styles"]["comic"])
    vertical = a.format == "9:16"
    panels = []
    for i, p in enumerate(script["panels"], 1):
        space = styles.get("headroom_suffix", "") if p.get("space") == "top" else ""
        prompt = f"{style_text}. Characters: {script.get('characters', '')}. Scene: {p['scene']}.{styles['suffix']}{space}"
        media, who = draw(prompt, project_id, vertical, a.format)
        say(f"Panel {i}/{len(script['panels'])} drawn by {who}")
        panels.append({"media_id": media, "caption": p.get("caption", ""), "sfx": p.get("sfx", ""), "bubbles": p.get("bubbles", [])})

    job = call("POST", "/api/studio/render", {
        "panels": panels, "format": a.format,
        "voice": {"enabled": a.voice, "engine": a.voice_engine},
        "captions": {"style": a.captions, "bubbles": True}})["job_id"]
    while True:
        time.sleep(3)
        j = call("GET", f"/api/studio/jobs/{job}")
        if j["status"] == "failed":
            raise SystemExit("Render failed: " + str(j.get("error")))
        if j["status"] == "done":
            break
    video = call("GET", j["file"], raw=True)
    with open(a.out, "wb") as fh:
        fh.write(video)
    say(f"VIDEO SAVED: {a.out} ({len(video) // 1024} KB)")

    if a.post:
        kit = call("POST", "/api/publish/kit", {"title": script.get("title", ""), "story": a.idea})
        res = call("POST", "/api/publish", {
            "job_id": job, "targets": [t.strip() for t in a.post.split(",") if t.strip()], "title": kit["title"],
            "description": kit["description"], "tags": kit["tags"], "privacy": a.privacy, "vertical": vertical}, timeout=900)
        for r in res["results"]:
            say(f"{'posted' if r['ok'] else 'NOT posted'}: {r['label']} — {r['detail']} {r.get('url') or ''}")


def main() -> None:
    global BASE
    ap = argparse.ArgumentParser(description="Flow Studio command line")
    ap.add_argument("--agent", default=BASE, help="agent address (default %(default)s)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("connectors").set_defaults(fn=cmd_connectors)
    p = sub.add_parser("project")
    p.add_argument("url")
    p.set_defaults(fn=cmd_project)
    m = sub.add_parser("make")
    m.add_argument("idea")
    m.add_argument("--panels", type=int, default=6)
    m.add_argument("--style", default="stickman", choices=["comic", "stickman", "anime", "watercolor"])
    m.add_argument("--tone", default="funny", choices=["funny", "epic", "dark", "wholesome"])
    m.add_argument("--format", default="16:9", choices=["16:9", "9:16", "1:1"])
    m.add_argument("--voice", action="store_true", help="add a voiceover (free Edge voices)")
    m.add_argument("--voice-engine", default="edge")
    m.add_argument("--captions", default="off", choices=["off", "karaoke", "classic"])
    m.add_argument("--project", help="Flow project URL to use")
    m.add_argument("--out", default="flowstudio-video.mp4")
    m.add_argument("--post", help="comma list of connected places, e.g. telegram,youtube,drive,folder,discord")
    m.add_argument("--privacy", default="private", choices=["private", "unlisted", "public"])
    m.add_argument("--script-only", action="store_true", help="only write the script (no pictures, no credits used)")
    m.set_defaults(fn=cmd_make)
    a = ap.parse_args()
    BASE = a.agent.rstrip("/")
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
