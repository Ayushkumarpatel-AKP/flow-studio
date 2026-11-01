import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";

export type VoiceCfg = { enabled: boolean; engine: string; left: string; right: string; speed: number };
export type CaptionCfg = { style: "off" | "karaoke" | "classic"; bubbles: boolean };

type VoiceEngine = {
  id: string;
  label: string;
  free: boolean;
  available: boolean;
  note: string;
  voices: { id: string; label: string; gender: string }[];
  default_left: string;
  default_right: string;
};
type VoicesResp = { engines: VoiceEngine[]; default_engine: string | null; exact_timing: string | null };

export const DEFAULT_VOICE: VoiceCfg = { enabled: false, engine: "", left: "", right: "", speed: 1 };
export const DEFAULT_CAPTIONS: CaptionCfg = { style: "off", bubbles: true };

export const ENGINES_CHANGED = "flowboard:engines-changed";

const CAPTION_STYLES: { key: CaptionCfg["style"]; label: string; hint: string }[] = [
  { key: "off", label: "Off", hint: "Speech bubbles only" },
  { key: "karaoke", label: "Karaoke ✨", hint: "Each word lights up as it is spoken" },
  { key: "classic", label: "Classic", hint: "Plain subtitles" },
];

interface Props {
  voice: VoiceCfg;
  captions: CaptionCfg;
  onVoice(v: VoiceCfg): void;
  onCaptions(c: CaptionCfg): void;
  onOpenEngines(): void;
}

/**
 * Voiceover and subtitles. Works with no key. Paste a voice key (ElevenLabs,
 * OpenAI) right here and it is recognised, tested and switched on by itself —
 * its voices simply appear in the menu.
 */
export function VoiceBox({ voice, captions, onVoice, onCaptions, onOpenEngines }: Props) {
  const [info, setInfo] = useState<VoicesResp | null>(null);
  const [playing, setPlaying] = useState("");
  const [err, setErr] = useState("");
  const [notice, setNotice] = useState("");
  const [keyText, setKeyText] = useState("");
  const [detected, setDetected] = useState<{ engine: string | null; label: string | null } | null>(null);
  const [adding, setAdding] = useState(false);
  const audio = useRef<HTMLAudioElement | null>(null);
  const known = useRef<Set<string> | null>(null); // voice engines we have already seen as available
  const voiceRef = useRef(voice);
  voiceRef.current = voice;

  const load = useCallback(async () => {
    try {
      const v = await api<VoicesResp>("/api/studio/voices");
      setInfo(v);
      const nowAvailable = new Set(v.engines.filter((e) => e.available).map((e) => e.id));
      const before = known.current;
      known.current = nowAvailable;
      const cur = voiceRef.current;

      // A better engine just became available (a key was pasted): switch to it and turn voiceover on.
      const fresh = before ? v.engines.find((e) => e.available && !before.has(e.id) && !e.free) : undefined;
      if (fresh) {
        onVoice({ ...cur, enabled: true, engine: fresh.id, left: fresh.default_left, right: fresh.default_right });
        setErr("");
        setNotice(`✓ ${fresh.label} is connected — ${fresh.voices.length || "its"} voices are ready and voiceover is on.`);
        return;
      }
      // First visit, or the chosen engine went away (key removed): fall back to the best available one.
      const chosen = v.engines.find((e) => e.id === cur.engine && e.available);
      if (!chosen) {
        const e = v.engines.find((x) => x.id === v.default_engine);
        if (e) onVoice({ ...cur, engine: e.id, left: e.default_left, right: e.default_right });
      }
    } catch {
      setErr("Could not load voices — is the app running?");
    }
  }, [onVoice]);

  useEffect(() => {
    void load();
    const again = () => void load();
    window.addEventListener(ENGINES_CHANGED, again);
    return () => window.removeEventListener(ENGINES_CHANGED, again);
  }, [load]);

  // Recognise a pasted key as soon as it is typed.
  useEffect(() => {
    const k = keyText.trim();
    if (k.length < 4) { setDetected(null); return; }
    const t = setTimeout(() => {
      void api<{ engine: string | null; label: string | null }>("/api/engines/detect", { method: "POST", body: JSON.stringify({ key: k }) })
        .then(setDetected).catch(() => {});
    }, 250);
    return () => clearTimeout(t);
  }, [keyText]);

  async function addKey() {
    if (!detected?.engine) return;
    setAdding(true); setErr(""); setNotice("");
    try {
      const r = await api<{ result: { status: string; message: string } }>(`/api/engines/${detected.engine}/key`, {
        method: "PUT", body: JSON.stringify({ key: keyText.trim() }),
      });
      if (r.result.status === "ok" || r.result.status === "rate_limited") {
        setKeyText(""); setDetected(null);
        window.dispatchEvent(new Event(ENGINES_CHANGED));   // this box and the Engines drawer both refresh
        // A key that cannot speak (e.g. Groq) is still saved for writing; say so.
        setTimeout(() => {
          if (!known.current?.has(detected.engine as string)) setNotice(`${detected.label} key saved. It has no voices, but it now powers writing and subtitles.`);
        }, 800);
      } else {
        setErr(r.result.message);
      }
    } catch (e) {
      setErr(String((e as Error).message));
    } finally { setAdding(false); }
  }

  const engine = info?.engines.find((e) => e.id === voice.engine);
  const usable = info?.engines.filter((e) => e.available) ?? [];

  function pickEngine(id: string) {
    const e = info?.engines.find((x) => x.id === id);
    if (e) onVoice({ ...voice, engine: id, left: e.default_left, right: e.default_right });
  }

  async function hear(side: "left" | "right") {
    const id = side === "left" ? voice.left : voice.right;
    setErr(""); setPlaying(side);
    try {
      const res = await fetch("/api/studio/voice-preview", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: side === "left" ? "Hi! This is my voice. Ready for the story?" : "And this is mine. Let's go!", engine: voice.engine, voice: id, speed: voice.speed }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? `Error ${res.status}`);
      const url = URL.createObjectURL(await res.blob());
      audio.current?.pause();
      const a = new Audio(url);
      audio.current = a;
      a.onended = () => setPlaying("");
      await a.play();
    } catch (e) {
      setErr(String((e as Error).message)); setPlaying("");
    }
  }

  return (
    <details className="box acc vb">
      <summary>Voice &amp; captions</summary>

      {notice && <p className="vb-notice">{notice}</p>}

      <label className="check vb-toggle">
        <input type="checkbox" checked={voice.enabled} disabled={!usable.length}
          onChange={(e) => onVoice({ ...voice, enabled: e.target.checked })} />
        🎙 Read the dialogue aloud
      </label>
      {!usable.length && info && <p className="muted">No voice engine is available. Install ffmpeg and Edge voices, or paste a voice key below.</p>}

      {voice.enabled && info && (
        <>
          <label className="fld"><span>Voice engine</span>
            <select value={voice.engine} onChange={(e) => pickEngine(e.target.value)}>
              {usable.map((e) => <option key={e.id} value={e.id}>{e.label}{e.free ? " · free" : " · your key"}</option>)}
            </select>
          </label>
          {engine && <p className="muted">{engine.note}</p>}

          {(["left", "right"] as const).map((side) => (
            <div className="vb-row" key={side}>
              <label className="fld"><span>{side === "left" ? "Left speaker" : "Right speaker"}</span>
                <select value={side === "left" ? voice.left : voice.right}
                  onChange={(e) => onVoice({ ...voice, [side]: e.target.value })}>
                  {engine?.voices.map((v) => <option key={v.id} value={v.id}>{v.label}</option>)}
                </select>
              </label>
              <button className="mini" onClick={() => void hear(side)} disabled={!!playing} title={engine?.free ? "Hear this voice" : "Hear this voice (uses a few credits)"}>
                {playing === side ? "…" : "▶ Hear"}
              </button>
            </div>
          ))}

          <label className="fld"><span>Speed: {voice.speed.toFixed(2)}×</span>
            <input type="range" min={0.8} max={1.3} step={0.05} value={voice.speed}
              onChange={(e) => onVoice({ ...voice, speed: Number(e.target.value) })} />
          </label>
        </>
      )}

      {/* Paste a key anywhere in the app and it configures itself. */}
      <div className="vb-key">
        <label htmlFor="vb-key-input"><b>🔑 Got a voice key?</b> Paste it — ElevenLabs or OpenAI — and your voices appear.</label>
        <div className="vb-key-row">
          <input id="vb-key-input" type="password" autoComplete="off" spellCheck={false} value={keyText}
            onChange={(e) => { setKeyText(e.target.value); setErr(""); }}
            placeholder="sk_… (ElevenLabs)   sk-… (OpenAI)" />
          <button className="mini" disabled={!detected?.engine || adding} onClick={() => void addKey()}>
            {adding ? "Testing…" : detected?.engine ? `Add ${detected.label}` : "Add"}
          </button>
        </div>
        {keyText.trim().length >= 4 && detected && !detected.engine && <p className="muted">We do not recognise that key.</p>}
        <p className="muted">
          Saved only on this computer.{" "}
          <a href="https://elevenlabs.io/app/settings/api-keys" target="_blank" rel="noopener noreferrer">Get an ElevenLabs key</a>
          {" · "}
          <button className="link" onClick={onOpenEngines}>All engines</button>
        </p>
      </div>

      <h3 className="acc-label">Subtitles</h3>
      <div className="chips">
        {CAPTION_STYLES.map((c) => (
          <button key={c.key} className={`chip ${captions.style === c.key ? "on" : ""}`} title={c.hint}
            onClick={() => onCaptions({ ...captions, style: c.key })}>{c.label}</button>
        ))}
      </div>
      {captions.style !== "off" && (
        <>
          <label className="check"><input type="checkbox" checked={captions.bubbles}
            onChange={(e) => onCaptions({ ...captions, bubbles: e.target.checked })} /> Keep speech bubbles too</label>
          <p className="muted">
            {info?.exact_timing
              ? `✓ Exact word timing from ${info.exact_timing === "groq" ? "Groq" : "OpenAI"} Whisper.`
              : "Word timing is estimated from the voice. "}
            {!info?.exact_timing && <button className="link" onClick={onOpenEngines}>Add a free Groq key for exact timing</button>}
          </p>
        </>
      )}
      {err && <p className="err">{err}</p>}
    </details>
  );
}
