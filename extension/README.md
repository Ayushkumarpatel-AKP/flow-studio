# Flowboard Bridge (browser extension)

The browser half of Flow Studio. It runs generation requests inside **your own signed-in Google Flow tab** and hands the results to the local Flow Studio app. Made by Ayush.

## Install (unpacked)

1. Open `chrome://extensions` (or `edge://extensions`).
2. Turn on **Developer mode**.
3. Click **Load unpacked** and choose this folder.
4. Open <https://flow.google.com>, sign in, and open a project. Keep that tab open.

## What it does

- **Bridges the app and Flow.** The service worker connects to the local agent (`ws://127.0.0.1:8355`) and runs each Flow request inside the signed-in tab, because Flow signs every call in the page itself.
- **Follows your project.** It reads the project id from the Flow tab's address and tells the agent, so you never paste a link. It re-syncs on every tab change and reconnect.
- **Handles the captcha.** Flow asks for a reCAPTCHA token on each generation. The extension brings the Flow tab forward and retries once if the token times out.
- **Retries safely.** If the page returns nothing, it retries once with a fresh token instead of re-sending a spent one.

## Popup

Click the extension icon for:
- a setup checklist (app running, Flow tab open, signed in, project detected),
- plain-language hints for common errors (captcha, quota, plan, blocked prompt),
- the most recent requests,
- **Open Studio**, **Open Flow tab**, **Refresh token** and **Disconnect** buttons.

## Files

| File | Purpose |
|---|---|
| `manifest.json` | Manifest V3 definition and permissions |
| `background.js` | Service worker: connection to the agent, request running, captcha, project following |
| `content.js` / `injected.js` | Run on the Flow page to reach Google's captcha |
| `popup.html` / `popup.js` | The toolbar popup |
| `rules.json` | Network rules used by the extension |

## Notes

- It only talks to Google Flow's own domains (see `host_permissions` in `manifest.json`) and your own computer (`localhost`).
- Nothing is sent anywhere else. Keys for AI services are kept by the local app, not by this extension.
- This is an unofficial bridge and is not affiliated with Google.
