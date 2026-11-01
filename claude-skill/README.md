# Flow Studio skill for Claude Code

Give this folder to your Claude Code and it can make comic videos by command: "make a 6-panel stickman video about a zombie eclipse
and save it as zombie.mp4" — then optionally post it.

## Install (30 seconds)

1. Install and start Flow Studio first (see the main README: `start.ps1`, then load the extension and sign in to Google Flow).
2. Copy the `flow-studio` folder into your Claude skills folder:

   | Where | Path |
   |---|---|
   | All your projects | `~/.claude/skills/flow-studio` (Windows: `C:\Users\<you>\.claude\skills\flow-studio`) |
   | One project only | `<project>/.claude/skills/flow-studio` |

3. Start a new Claude Code session. Check it with `/skills` — **flow-studio** should be listed.

## Use it

Just ask in plain words:

- "Make a 6-panel stickman comic video about a zombie eclipse. My Flow project is https://flow.google.com/project/…"
- "Same story but vertical 9:16 with voiceover and karaoke subtitles."
- "Write only the script first, don't draw yet."
- "Post the video to Telegram."

Claude reads `flow-studio/SKILL.md`, runs `scripts/flowstudio.py`, and tells you where the finished MP4 is.

You can also run the helper yourself:

```bash
python flow-studio/scripts/flowstudio.py status
python flow-studio/scripts/flowstudio.py make "A robot learns to cook" --panels 6 --out robot.mp4
```

## Good to know

- It controls the Flow Studio app on **your own computer** (http://localhost:8434). It does not work if the app is not running.
- Keys and sign-ins are entered in the web app, never in the chat.
- Uses your own Google Flow account and daily limits. Flow is reached through unofficial pages and may stop working if Google changes them.
- Made by Ayush. MIT license, same as the main project.
