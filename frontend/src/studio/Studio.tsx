import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  createBoard,
  createRequest,
  ensureBoardProject,
  getHealth,
  getRequest,
  listBoards,
} from "../api/client";
import { STYLE_META, TEMPLATES, TONES, type StoryTemplate } from "./templates";
import { Editor } from "./Editor";
import { Engines } from "./Engines";
import { DEFAULT_CAPTIONS, DEFAULT_VOICE, VoiceBox, type CaptionCfg, type VoiceCfg } from "./VoiceBox";
import { FORMATS, type Format, type Panel } from "./types";
import "./studio.css";

type ScriptResp = { title: string; characters?: string; panels: Omit<Panel, "state">[]; note?: string };
type StyleResp = { styles: Record<string, string>; suffix: string };
type Idea = { title: string; idea: string };

// Small remembered settings. Storage can be blocked, so never let it break the page.
function load<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? { ...fallback, ...(JSON.parse(raw) as T) } : fallback;
  } catch {
    return fallback;
  }
}
function save(key: string, value: unknown) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const isExt = (h: unknown) => !!(h as { extension_connected?: boolean }).extension_connected;

// Plain-language versions of the errors people actually hit.
function friendly(err: string): string {
  if (/QUOTA/i.test(err)) return "Your Flow credits/limit are used up for now. Wait for them to reset, then press Retry.";
  if (/CAPTCHA/i.test(err)) return "Flow could not verify the browser. Keep the Flow tab in front and press Retry.";
  if (/NO_FLOW_TAB|NO_AT_TOKEN|extension/i.test(err)) return "Open flow.google.com in Chrome (signed in) with the Flowboard extension loaded, then Retry.";
  if (/ACCESS_DENIED/i.test(err)) return "Your Flow plan can't use that video model. Studio retries on the Lite model automatically.";
  if (/UNSAFE|FILTER/i.test(err)) return "Flow blocked this prompt. Reword the scene and Retry.";
  return err;
}

// Flow rejects overlapping generates from a single tab, so every generation
// request goes through one queue and runs strictly one after another.
let queue: Promise<unknown> = Promise.resolve();
function serial<T>(fn: () => Promise<T>): Promise<T> {
  const run = queue.then(fn, fn);
  queue = run.then(() => undefined, () => undefined);
  return run;
}

// Always ask the agent which Flow project the board is bound to right now,
// instead of trusting a value cached when the page loaded.
async function freshProjectId(): Promise<string> {
  const boards = await listBoards();
  const board = boards[0] ?? (await createBoard("Studio"));
  return (await ensureBoardProject(board.id)).flow_project_id;
}

async function runRequest(type: string, params: Record<string, unknown>, tries = 3): Promise<{ mediaIds: string[] }> {
  let last = "failed";
  for (let attempt = 0; attempt < tries; attempt++) {
    const req = await createRequest({ type, params });
    for (let i = 0; i < 180; i++) {
      await sleep(type === "gen_video" ? 5000 : 3000);
      const s = await getRequest(req.id);
      if (s.status === "done") {
        const ids = ((s.result as { media_ids?: (string | null)[] } | null)?.media_ids ?? []).filter((x): x is string => !!x);
        if (ids.length) return { mediaIds: ids };
        last = "no media returned";
        break;
      }
      if (s.status === "failed" || s.status === "canceled" || s.status === "timeout") {
        last = String(s.error ?? s.status);
        break;
      }
    }
    // Quota / access errors will not fix themselves in seconds — stop retrying.
    if (/QUOTA|ACCESS_DENIED|UNSAFE/i.test(last)) break;
    await sleep(2000);
  }
  throw new Error(last);
}

export function Studio() {
  const [ext, setExt] = useState(false);
  const [appUp, setAppUp] = useState(false);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [projectInput, setProjectInput] = useState("");
  const [changing, setChanging] = useState(false);
  const [setupMsg, setSetupMsg] = useState("");

  const [idea, setIdea] = useState("");
  const [count, setCount] = useState(6);
  const [style, setStyle] = useState("stickman");
  const [tone, setTone] = useState("funny");
  const [styles, setStyles] = useState<StyleResp | null>(null);
  const [title, setTitle] = useState("");
  const [characters, setCharacters] = useState("");
  const [panels, setPanels] = useState<Panel[]>([]);
  const [note, setNote] = useState("");
  const [ideas, setIdeas] = useState<Idea[]>([]);
  const [busy, setBusy] = useState<"" | "script" | "images" | "videos" | "render">("");
  const [ai, setAi] = useState(""); // which AI helper is thinking
  const [animate, setAnimate] = useState(false);
  const [format, setFormat] = useState<Format>("16:9");
  const [selected, setSelected] = useState(0);
  const [showEngines, setShowEngines] = useState(false);
  const [voiceCfg, setVoiceCfg] = useState<VoiceCfg>(() => load("studio.voice", DEFAULT_VOICE));
  const [capCfg, setCapCfg] = useState<CaptionCfg>(() => load("studio.captions", DEFAULT_CAPTIONS));
  useEffect(() => save("studio.voice", voiceCfg), [voiceCfg]);
  useEffect(() => save("studio.captions", capCfg), [capCfg]);
  const [videoUrl, setVideoUrl] = useState("");
  const [progress, setProgress] = useState("");
  const stop = useRef(false);
  const workRef = useRef<HTMLDivElement>(null);

  // ── setup: agent + extension + Flow project ───────────────────────────────
  const checkSetup = useCallback(async () => {
    try {
      const h = await getHealth();
      setAppUp(true);
      setExt(isExt(h));
    } catch {
      setAppUp(false);
      setExt(false);
    }
    try {
      const boards = await listBoards();
      const board = boards[0] ?? (await createBoard("Studio"));
      const p = await ensureBoardProject(board.id);
      setProjectId(p.flow_project_id);
      setSetupMsg("");
    } catch (e) {
      setProjectId(null);
      setSetupMsg(String((e as Error).message));
    }
  }, []);

  useEffect(() => {
    void checkSetup();
    void api<StyleResp>("/api/studio/styles").then(setStyles).catch(() => {});
    const t = setInterval(() => void getHealth().then((h) => { setAppUp(true); setExt(isExt(h)); }).catch(() => { setAppUp(false); setExt(false); }), 5000);
    return () => clearInterval(t);
  }, [checkSetup]);

  async function pinProject() {
    const m = projectInput.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
    if (!m) { setSetupMsg("Paste the full project link from flow.google.com (it contains a long id)."); return; }
    await api("/api/flow/projects/pinned", { method: "PUT", body: JSON.stringify({ flow_project_id: m[0] }) });
    setChanging(false); setProjectInput("");
    await checkSetup();
  }

  // ── AI helpers ────────────────────────────────────────────────────────────
  async function improve(kind: "idea" | "scene" | "dialogue", text: string, context: string, apply: (t: string) => void, key: string) {
    if (!text.trim()) return;
    setAi(key);
    try {
      const r = await api<{ text: string }>("/api/studio/improve", { method: "POST", body: JSON.stringify({ text, kind, context }) });
      apply(r.text);
    } catch (e) {
      setNote("AI helper is unavailable right now — " + String((e as Error).message));
    } finally { setAi(""); }
  }

  async function surprise() {
    setAi("ideas"); setNote("");
    try {
      const r = await api<{ ideas: Idea[] }>("/api/studio/ideas", { method: "POST", body: JSON.stringify({ count: 6 }) });
      setIdeas(r.ideas);
    } catch (e) {
      setNote(String((e as Error).message));
    } finally { setAi(""); }
  }

  // ── script ────────────────────────────────────────────────────────────────
  async function writeScript(over?: Partial<{ idea: string; count: number; style: string; tone: string }>) {
    const o = { idea, count, style, tone, ...over };
    setBusy("script"); setNote(""); setVideoUrl("");
    try {
      const s = await api<ScriptResp>("/api/studio/script", {
        method: "POST",
        body: JSON.stringify({ idea: o.idea, panels: o.count, style: o.style, tone: o.tone }),
      });
      setTitle(s.title ?? o.idea);
      setCharacters(s.characters ?? "");
      setPanels(s.panels.map((p) => ({ ...p, bubbles: p.bubbles ?? [], caption: p.caption ?? "", sfx: p.sfx ?? "", state: "idle" })));
      setNote(s.note ?? "");
      setTimeout(() => workRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
    } catch (e) {
      setNote(String((e as Error).message));
    } finally { setBusy(""); }
  }

  function useTemplate(t: StoryTemplate) {
    setIdea(t.idea); setStyle(t.style); setTone(t.tone); setCount(t.panels);
    void writeScript({ idea: t.idea, style: t.style, tone: t.tone, count: t.panels });
  }

  // Undo/redo for edits. Rapid changes (typing, dragging) are coalesced into one step.
  const past = useRef<Panel[][]>([]);
  const future = useRef<Panel[][]>([]);
  const lastPush = useRef(0);
  const edit = (fn: (ps: Panel[]) => Panel[]) =>
    setPanels((ps) => {
      const now = Date.now();
      if (now - lastPush.current > 700) {
        past.current = [...past.current.slice(-49), ps];
        future.current = [];
      }
      lastPush.current = now;
      return fn(ps);
    });
  const undo = () => setPanels((ps) => {
    const prev = past.current.pop();
    if (!prev) return ps;
    future.current.push(ps);
    lastPush.current = 0;
    return prev;
  });
  const redo = () => setPanels((ps) => {
    const next = future.current.pop();
    if (!next) return ps;
    past.current.push(ps);
    lastPush.current = 0;
    return next;
  });
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
      if (!(e.ctrlKey || e.metaKey)) return;
      const k = e.key.toLowerCase();
      if (k === "z" && !e.shiftKey) { e.preventDefault(); undo(); }
      else if (k === "y" || (k === "z" && e.shiftKey)) { e.preventDefault(); redo(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });
  // Generation status changes (drawing / done) should not become undo steps.
  const update = (i: number, patch: Partial<Panel>) => setPanels((ps) => ps.map((p, k) => (k === i ? { ...p, ...patch } : p)));
  const editPanel = (i: number, patch: Partial<Panel>) => edit((ps) => ps.map((p, k) => (k === i ? { ...p, ...patch } : p)));
  const move = (i: number, d: number) => edit((ps) => {
    const j = i + d;
    if (j < 0 || j >= ps.length) return ps;
    const c = ps.slice();
    [c[i], c[j]] = [c[j], c[i]];
    return c;
  });
  const remove = (i: number) => {
    edit((ps) => (ps.length > 1 ? ps.filter((_, k) => k !== i) : ps));
    setSelected((s) => Math.max(0, Math.min(s, panels.length - 2)));
  };
  const addPanel = () => {
    edit((ps) => [...ps, { scene: "", caption: "", sfx: "", bubbles: [{ side: "left", text: "" }], state: "idle" }]);
    setSelected(panels.length);
  };

  // ── pictures ──────────────────────────────────────────────────────────────
  const imagePrompt = useMemo(
    () => (scene: string) => `${styles?.styles[style] ?? style}. ${characters ? characters + " " : ""}${scene}.${styles?.suffix ?? ""}`,
    [styles, style, characters],
  );

  async function makeImage(i: number) {
    if (!projectId) return;
    update(i, { state: "working", error: undefined });
    try {
      const pid = await freshProjectId();
      setProjectId(pid);
      const r = await serial(() => runRequest("gen_image", {
        prompt: imagePrompt(panels[i].scene), project_id: pid,
        aspect_ratio: format === "9:16" ? "IMAGE_ASPECT_RATIO_PORTRAIT" : "IMAGE_ASPECT_RATIO_LANDSCAPE", variant_count: 1,
      }));
      update(i, { mediaId: r.mediaIds[0], state: "done", videoId: undefined });
    } catch (e) {
      update(i, { state: "error", error: friendly((e as Error).message) });
      throw e;
    }
  }

  async function makeAllImages() {
    setBusy("images"); stop.current = false;
    for (let i = 0; i < panels.length && !stop.current; i++) {
      if (panels[i].mediaId) continue;
      setProgress(`Drawing panel ${i + 1} of ${panels.length}…`);
      try { await makeImage(i); } catch { break; }
    }
    setProgress(""); setBusy("");
  }

  // ── animate (optional) ────────────────────────────────────────────────────
  async function makeVideo(i: number) {
    const p = panels[i];
    if (!projectId || !p.mediaId) return;
    update(i, { state: "working", error: undefined });
    try {
      const pid = await freshProjectId();
      setProjectId(pid);
      const r = await serial(() => runRequest("gen_video", {
        prompt: `2D animation, keep the exact same art style and characters, no text. ${p.scene}`,
        project_id: pid, start_media_id: p.mediaId,
        aspect_ratio: format === "9:16" ? "VIDEO_ASPECT_RATIO_PORTRAIT" : "VIDEO_ASPECT_RATIO_LANDSCAPE", video_quality: "lite",
      }, 2));
      update(i, { videoId: r.mediaIds[0], state: "done" });
    } catch (e) {
      update(i, { state: "error", error: friendly((e as Error).message) });
      throw e;
    }
  }

  async function animateAll() {
    setBusy("videos"); stop.current = false;
    for (let i = 0; i < panels.length && !stop.current; i++) {
      if (panels[i].videoId || !panels[i].mediaId) continue;
      setProgress(`Animating panel ${i + 1} of ${panels.length}…`);
      try { await makeVideo(i); } catch { break; } // stop on quota; stills still render
    }
    setProgress(""); setBusy("");
  }

  // ── final video ───────────────────────────────────────────────────────────
  async function render() {
    setBusy("render"); setVideoUrl(""); setProgress("Building your video…");
    try {
      const { job_id } = await api<{ job_id: string }>("/api/studio/render", {
        method: "POST",
        body: JSON.stringify({
          format,
          voice: voiceCfg,
          captions: capCfg,
          panels: panels.map((p) => ({ media_id: p.mediaId, video_media_id: p.videoId, caption: p.caption, sfx: p.sfx, sfx_x: p.sfxX, sfx_y: p.sfxY, bubbles: p.bubbles })),
        }),
      });
      for (;;) {
        await sleep(2000);
        const j = await api<{ status: string; progress?: number; file?: string; error?: string }>(`/api/studio/jobs/${job_id}`);
        setProgress(`Building your video… ${Math.round((j.progress ?? 0) * 100)}%`);
        if (j.status === "done" && j.file) { setVideoUrl(j.file); break; }
        if (j.status === "failed") throw new Error(j.error ?? "render failed");
      }
    } catch (e) {
      setNote(String((e as Error).message));
    } finally { setProgress(""); setBusy(""); }
  }

  const drawn = panels.filter((p) => p.mediaId).length;
  const imagesReady = panels.length > 0 && drawn === panels.length;
  const setupOk = ext && !!projectId;
  const stepNow = videoUrl || imagesReady ? 4 : panels.length ? 3 : idea.trim() ? 2 : 1;
  const steps = ["Idea", "Script", "Pictures", "Video"];

  return (
    <div className="st">
      {/* ── top bar ─────────────────────────────────────────── */}
      <header className="st-top">
        <div className="st-brand">Flowboard <span>Studio</span></div>
        <ol className="st-steps" aria-label="Progress">
          {steps.map((s, i) => (
            <li key={s} className={i + 1 < stepNow ? "done" : i + 1 === stepNow ? "now" : ""}>
              <b>{i + 1 < stepNow ? "✓" : i + 1}</b> {s}
            </li>
          ))}
        </ol>
        <div className="st-pills">
          <span className={appUp ? "ok" : "no"}>● App</span>
          <span className={ext ? "ok" : "no"}>● Extension</span>
          <span className={projectId ? "ok" : "no"}>● Project</span>
          <button className="eng-btn" onClick={() => setShowEngines(true)} title="Add your own AI keys">🔌 Engines</button>
        </div>
        {showEngines && <Engines onClose={() => setShowEngines(false)} />}
      </header>

      {/* ── hero + templates ────────────────────────────────── */}
      <section className="st-hero">
        <div>
          <h1>What story should we draw today?</h1>
          <p>Pick a template for an instant start, or write your own idea below. The AI writes the script, draws every panel and builds the video.</p>
        </div>
        <div className="st-templates">
          {TEMPLATES.map((t) => (
            <button key={t.id} className="tpl" style={{ ["--c" as string]: t.color }} disabled={!!busy}
              onClick={() => useTemplate(t)} title={t.idea}>
              <span className="tpl-emoji">{t.emoji}</span>
              <span className="tpl-title">{t.title}</span>
              <span className="tpl-blurb">{t.blurb}</span>
              <span className="tpl-meta">{t.panels} panels · {STYLE_META[t.style]?.label}</span>
            </button>
          ))}
        </div>
      </section>

      {/* ── workspace ───────────────────────────────────────── */}
      <div className="st-work" ref={workRef}>
        <aside className="st-side">
          {/* Setup comes first, but only while something still blocks drawing. */}
          {!setupOk && (
            <details className="box acc warn" open>
              <summary>Finish setup</summary>
              <ul className="checks">
                <li className={appUp ? "ok" : "no"}>{appUp ? "✓" : "○"} Flowboard app is running</li>
                <li className={ext ? "ok" : "no"}>{ext ? "✓" : "○"} Chrome extension connected{!ext && " — load it, then open flow.google.com signed in"}</li>
                <li className={projectId ? "ok" : "no"}>{projectId ? "✓" : "○"} Flow project linked</li>
              </ul>
              {!projectId && ext && (
                <div className="link-row">
                  <input value={projectInput} onChange={(e) => setProjectInput(e.target.value)} placeholder="Paste your Flow project link" />
                  <button onClick={() => void pinProject()}>Link</button>
                </div>
              )}
              {setupMsg && !projectId && <p className="hint">{setupMsg.includes("NO_FLOW_PROJECT") ? "Create a project on flow.google.com, then paste its link above." : setupMsg}</p>}
              <button className="ghost" onClick={() => void checkSetup()}>Re-check</button>
            </details>
          )}

          <details className="box acc" open>
            <summary><b>1</b> Your idea</summary>
            <textarea rows={4} value={idea} onChange={(e) => setIdea(e.target.value)}
              placeholder="e.g. Two stickmen survive a zombie outbreak during a solar eclipse" />
            <div className="btn-row">
              <button className="ai" disabled={!idea.trim() || !!ai}
                onClick={() => void improve("idea", idea, "", setIdea, "idea")}>
                {ai === "idea" ? "Thinking…" : "✨ Improve my idea"}
              </button>
              <button className="ai" disabled={!!ai} onClick={() => void surprise()}>
                {ai === "ideas" ? "Thinking…" : "🎲 Surprise me"}
              </button>
            </div>
            {ideas.length > 0 && (
              <ul className="ideas">
                {ideas.map((i, k) => (
                  <li key={k}><button onClick={() => { setIdea(i.idea); setIdeas([]); }}><b>{i.title}</b><span>{i.idea}</span></button></li>
                ))}
              </ul>
            )}
          </details>

          <details className="box acc" open>
            <summary><b>2</b> Look &amp; shape</summary>
            <h3 className="acc-label">Art style</h3>
            <div className="chips">
              {Object.keys(styles?.styles ?? STYLE_META).map((k) => (
                <button key={k} className={`chip ${style === k ? "on" : ""}`} onClick={() => setStyle(k)}>
                  {STYLE_META[k]?.emoji ?? "🎨"} {STYLE_META[k]?.label ?? k}
                </button>
              ))}
            </div>
            <h3 className="acc-label">Mood</h3>
            <div className="chips">
              {TONES.map((t) => (
                <button key={t.key} className={`chip ${tone === t.key ? "on" : ""}`} onClick={() => setTone(t.key)}>{t.label}</button>
              ))}
            </div>
            <h3 className="acc-label">Video shape</h3>
            <div className="chips">
              {FORMATS.map((f) => (
                <button key={f.key} className={`chip ${format === f.key ? "on" : ""}`} onClick={() => setFormat(f.key)} title={f.hint}>
                  {f.key === "9:16" ? "📱" : f.key === "1:1" ? "⬜" : "🖥️"} {f.label}
                </button>
              ))}
            </div>
            {panels.some((p) => p.mediaId) && <small className="muted">Changing the shape? Redraw panels so the pictures fit.</small>}
            <h3 className="acc-label">Panels: {count} <small className="muted">≈ {count * 6} seconds</small></h3>
            <input type="range" min={3} max={12} value={count} onChange={(e) => setCount(Number(e.target.value))} />
          </details>

          {/* The main action stays visible no matter which sections are folded. */}
          <button className="go side-go" disabled={idea.trim().length < 3 || !!busy} onClick={() => void writeScript()}>
            {busy === "script" ? "Writing…" : panels.length ? "↻ Rewrite the script" : "✍️ Write the script"}
          </button>

          <VoiceBox voice={voiceCfg} captions={capCfg} onVoice={setVoiceCfg} onCaptions={setCapCfg}
            onOpenEngines={() => setShowEngines(true)} />

          {setupOk && (
            <details className="box acc proj">
              <summary>Flow project</summary>
              <p className="muted">Using <code>…{projectId?.slice(-8)}</code>. Keep that project open in your Flow tab.</p>
              {changing ? (
                <div className="link-row">
                  <input value={projectInput} onChange={(e) => setProjectInput(e.target.value)} placeholder="Paste the new project link" />
                  <button onClick={() => void pinProject()}>Link</button>
                </div>
              ) : <button className="ghost" onClick={() => setChanging(true)}>Change project</button>}
              {setupMsg && <p className="hint">{setupMsg}</p>}
            </details>
          )}
        </aside>

        <main className="st-main">
          {note && <p className="hint">{note}</p>}

          {panels.length === 0 ? (
            <div className="empty">
              <div className="empty-art">💬</div>
              <h2>Your storyboard will appear here</h2>
              <p>Choose a template above, or write an idea and press <b>Write the script</b>.</p>
              <ol>
                <li>The AI writes panels and dialogue</li>
                <li>You edit anything you like</li>
                <li>It draws each panel, adds speech bubbles and exports an MP4</li>
              </ol>
            </div>
          ) : (
            <>
              <Editor
                panels={panels}
                format={format}
                title={title}
                selected={Math.min(selected, panels.length - 1)}
                busy={!!busy}
                canDraw={!!projectId}
                ai={ai}
                onSelect={setSelected}
                onTitle={setTitle}
                onPanel={editPanel}
                onMove={move}
                onRemove={remove}
                onAdd={addPanel}
                onDraw={(i) => void makeImage(i).catch(() => {})}
                onImprove={(kind, text, ctx, apply, key) => void improve(kind, text, ctx, apply, key)}
              />

              {videoUrl && (
                <section className="result">
                  <h2>🎬 Your video is ready</h2>
                  <video src={videoUrl} controls />
                  <a className="dl" href={videoUrl} download="flowboard-comic.mp4">⬇ Download MP4</a>
                </section>
              )}
            </>
          )}
        </main>
      </div>

      {/* ── action bar ──────────────────────────────────────── */}
      {panels.length > 0 && (
        <div className="bar">
          <div className="bar-info">
            {progress ? <b className="spin">{progress}</b> : <span>{imagesReady ? "All panels drawn — ready!" : `${drawn} of ${panels.length} panels drawn`}</span>}
            <div className="meter"><i style={{ width: `${(drawn / panels.length) * 100}%` }} /></div>
          </div>
          <label className="check"><input type="checkbox" checked={animate} onChange={(e) => setAnimate(e.target.checked)} /> Animate (uses more credits)</label>
          {busy && busy !== "render" && <button onClick={() => { stop.current = true; }}>Stop</button>}
          {animate && imagesReady && <button disabled={!!busy} onClick={() => void animateAll()}>{busy === "videos" ? "Animating…" : "🎞 Animate panels"}</button>}
          {!imagesReady
            ? <button className="go" disabled={!!busy || !setupOk} onClick={() => void makeAllImages()}>{busy === "images" ? "Drawing…" : "🖌️ Draw all panels"}</button>
            : <button className="go" disabled={!!busy} onClick={() => void render()}>{busy === "render" ? "Building…" : "🎬 Make my video"}</button>}
        </div>
      )}

      <footer className="st-foot">Made by <strong>Ayush</strong></footer>
    </div>
  );
}
