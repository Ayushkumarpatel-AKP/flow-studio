---
name: flow-studio
description: Make a comic-style video (script, pictures from Google Flow, speech bubbles, voiceover, subtitles, MP4) and optionally post it to YouTube Shorts, Telegram, Discord, Google Drive or a folder, using the user's local Flow Studio app. Use when the user asks for a comic, stickman or story video, a Short/Reel, or to post a finished video.
---

# Flow Studio

Flow Studio is a local app made by Ayush (https://github.com/Ayushkumarpatel-AKP/flow-studio). It writes the story, draws every
panel through the user's own signed-in Google Flow account, adds bubbles, voice and subtitles, and exports an MP4.
Everything runs on the user's computer. You control it with the helper script in this skill.

The helper is `scripts/flowstudio.py` (Python 3.9+, standard library only). Run it with the Bash tool.

## 1. Check the app is running

```bash
python <skill folder>/scripts/flowstudio.py status
```

It prints whether the agent runs, whether the Flow extension is connected, which Flow project is set, which AI engines work,
and the state of background Flow.

If it says it **cannot reach the agent**, start the app (needs Python 3.11+, Node 20+, ffmpeg):

- Windows: from the Flow Studio folder run `powershell -ExecutionPolicy Bypass -File start.ps1`
- Other: see the repo README, "Manual start".

If the **extension is not connected**: ask the user to open a signed-in tab at https://flow.google.com (and keep it open), or in the
web app press **🔗 Connect → Google Flow → Start in background**. First-time setup of the extension is in the repo README.

## 2. Point it at a Flow project (once)

**Whenever the user gives a Google Flow link** (`https://flow.google.com/project/<id>`), treat it as "draw everything in this project":
set it right away without asking, and use it for every video until they give a different link. The `make` command also finds a link
inside the idea text, so `make "zombie eclipse comic https://flow.google.com/project/<id>"` works too.

Ask the user for a Flow project address (it looks like `https://flow.google.com/project/<id>`), then:

```bash
python scripts/flowstudio.py project "https://flow.google.com/project/<id>"
```

## 3. Make a video

```bash
python scripts/flowstudio.py make "Two stickmen survive a zombie outbreak during a solar eclipse" \
  --panels 6 --style stickman --tone epic --format 16:9 --out zombie.mp4
```

Options: `--style comic|stickman|anime|watercolor`, `--tone funny|epic|dark|wholesome`, `--format 16:9|9:16|1:1`
(9:16 for Shorts/Reels/TikTok), `--voice` (free voiceover), `--captions karaoke|classic`, `--project <url>`,
`--post telegram,youtube,drive,folder,discord`, `--privacy private|unlisted|public`.

- It takes a few minutes: about 20–40 seconds per picture, then the render. Tell the user it is working.
- Use `--script-only` first if the user wants to see or edit the story without spending Flow credits.
- The script prints each panel as it is drawn, and `VIDEO SAVED: <path>` at the end. Give the user that path.

## 4. Posting

Posting only works to places the user has connected in the web app (**🔗 Connect → Post videos**). List them:

```bash
python scripts/flowstudio.py connectors
```

`ok` means connected. If YouTube is not connected, tell the user to open Connect and follow the steps there (they need a free Google
Cloud OAuth client; the app guides them). YouTube uploads start as **private** — never choose `--privacy public` unless the user asked.
Only post when the user clearly asked you to.

## Rules and troubleshooting

- **Never ask the user to paste API keys or tokens into the chat.** Keys go into the web app (🔌 Engines / 🔗 Connect) and are stored on their computer.
- "Flow's daily limit is used up" (QUOTA): the script tries a backup picture service if one is connected. Otherwise tell the user to wait for
  the limit to reset or connect a picture service in 🔗 Connect → Pictures.
- "UNSAFE": Flow refused that picture; change the idea or wording. Do not retry the same text.
- Captcha timeouts: the Flow tab must be open or background Flow running; retry once.
- Flow is used through the user's own browser tab with unofficial pages. It can break when Google changes things. Be upfront about that.
- Only the user's own Google account is used. Do not try to log in for them.
- Do not run the helper in a loop to make many videos at once; each picture spends the user's daily Flow credits.
