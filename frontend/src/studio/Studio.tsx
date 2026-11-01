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
import { CATEGORIES, STYLE_META, TEMPLATES, TONES, type Category, type StoryTemplate } from "./templates";
import { Editor } from "./Editor";
import { Engines } from "./Engines";
import { Connect, CONNECTORS_CHANGED } from "./Connect";
import { Publish } from "./Publish";
import "./connect.css";
import { DEFAULT_CAPTIONS, DEFAULT_VOICE, VoiceBox, type CaptionCfg, type VoiceCfg } from "./VoiceBox";
import { FORMATS, type Format, type Panel } from "./types";
import "./studio.css";

type ScriptResp = { idea?: string; pinned_project?: string; title: string; characters?: string; panels: Omit<Panel, "state">[]; note?: string };
type StyleResp = { styles: Record<string, string>; suffix: string; headroom_suffix?: string };
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
  if (/Insufficient credits|HTTP 402/i.test(err)) return "Flow's daily limit is reached, and your picture engine has no credits left. Add credits there (OpenRouter: openrouter.ai/credits), add an OpenAI key in 🔌 Engines, or wait for Flow to reset, then press Retry.";
  if (/QUOTA/i.test(err)) return "Google Flow's daily limit is used up (Studio already tried both Flow picture models). Add an OpenAI key, or an OpenRouter key with credits, in 🔌 Engines to keep drawing — or wait for Flow to reset and press Retry.";
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
  const [connectTab, setConnectTab] = useState<"flow" | "picture" | "publish" | "more" | null>(null);
  const [category, setCategory] = useState<Category | "All">("All");
  const [showResult, setShowResult] = useState(false);
  const [pictureEngine, setPictureEngine] = useState<string | null>(null);   // label of a connected picture backup
  type Quota = { left: number; resets: string; model?: string; models?: { key: string; label: string; note: string; per_day: number }[] };
  const [quota, setQuota] = useState<Quota | null>(null);   // free Cloudflare pictures left today, and the model choice
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

  const loadQuota = useCallback(async () => {
    try {
      const r = await api<{ cloudflare: Quota | null }>("/api/studio/picture-quota");
      setQuota(r.cloudflare);
    } catch { setQuota(null); }
  }, []);

  const [modelMenu, setModelMenu] = useState(false);
  // Flow is the default. "cloudflare" draws on Cloudflare first and only asks Flow when that fails.
  const [source, setSource] = useState<"flow" | "cloudflare">(() => (load<{ v: string }>("studio.pictureSource", { v: "flow" }).v === "cloudflare" ? "cloudflare" : "flow"));
  useEffect(() => save("studio.pictureSource", { v: source }), [source]);

  async function switchPictureModel(model: string) {
    try {
      const r = await api<{ cloudflare: Quota }>("/api/studio/picture-model", { method: "PUT", body: JSON.stringify({ model }) });
      setQuota(r.cloudflare);
      setModelMenu(false);
    } catch (e) { setNote(String((e as Error).message)); }
  }

  const loadPictureEngine = useCallback(async () => {
    void loadQuota();
    try {
      const ov = await api<{ config: Record<string, { status: { state: string; engine: string | null } }>; providers: { id: string; label: string }[] }>("/api/engines");
      const st = ov.config.image?.status;
      if (st?.state === "ok") { setPictureEngine(ov.providers.find((p) => p.id === st.engine)?.label ?? "your engine"); return; }
      // No key-based engine: a picture service from the Connect hub also counts.
      const cn = await api<{ picture_order: string[]; connectors: { id: string; label: string }[] }>("/api/connectors");
      const first = cn.connectors.find((c) => c.id === cn.picture_order[0]);
      setPictureEngine(first ? first.label : null);
    } catch { setPictureEngine(null); }
  }, [loadQuota]);

  useEffect(() => {
    void loadPictureEngine();
    window.addEventListener("flowboard:engines-changed", loadPictureEngine);
    window.addEventListener(CONNECTORS_CHANGED, loadPictureEngine);
    return () => {
      window.removeEventListener("flowboard:engines-changed", loadPictureEngine);
      window.removeEventListener(CONNECTORS_CHANGED, loadPictureEngine);
    };
  }, [loadPictureEngine]);

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
      if (s.pinned_project) { setProjectId(s.pinned_project); setSetupMsg(""); }
      if (s.idea !== undefined && s.idea !== o.idea) setIdea(s.idea);   // the Flow link was taken out of the prompt
      setTitle(s.title ?? o.idea);
      setCharacters(s.characters ?? "");
      setPanels(s.panels.map((p) => ({ ...p, bubbles: p.bubbles ?? [], caption: p.caption ?? "", sfx: p.sfx ?? "", space: p.space === "top" ? "top" : "none", state: "idle" })));
      setNote(s.note ?? (s.pinned_project ? "Using the Flow project from your link. Pictures will be drawn there." : ""));
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
  function newStory() {
    if (panels.some((p) => p.mediaId) && !window.confirm("Start a new story? The current panels will be cleared.")) return;
    setPanels([]); setTitle(""); setVideoUrl(""); setShowResult(false); setSelected(0); setNote("");
    past.current = []; future.current = [];
  }

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
    () => (scene: string, space?: "none" | "top") =>
      `${styles?.styles[style] ?? style}. ${characters ? characters + " " : ""}${scene}.${styles?.suffix ?? ""}${space === "top" ? styles?.headroom_suffix ?? "" : ""}`,
    [styles, style, characters],
  );

  // One character reference picture per story. Every panel is drawn with it attached, so the
  // faces stay the same instead of being re-invented from words each time.
  const charRef = useRef<{ key: string; promise: Promise<string | null> } | null>(null);
  useEffect(() => { charRef.current = null; }, [projectId, characters, style, format]);

  function ensureCharRef(pid: string): Promise<string | null> {
    const key = `${pid}|${style}|${characters}|${format}`;
    if (charRef.current?.key === key) return charRef.current.promise;
    const promise = (async () => {
      if (!characters.trim()) return null;
      // The filter sometimes blocks a sheet at random, so try a plainer wording once before giving up.
      const wordings = [
        `Character reference sheet on a plain white background: ${characters} Show each character clearly, front view, full body, face fully visible, one fixed outfit and design per character, no text.`,
        `Character design lineup on a plain white background: ${characters} Friendly, family-safe, front view, no text.`,
      ];
      for (const w of wordings) {
        try {
          const r = await serial(() => runRequest("gen_image", {
            prompt: `${styles?.styles[style] ?? style}. ${w}${styles?.suffix ?? ""}`,
            project_id: pid,
            aspect_ratio: "IMAGE_ASPECT_RATIO_LANDSCAPE", variant_count: 1,
          }, 1));
          if (r.mediaIds[0]) return r.mediaIds[0];
        } catch { /* try the next wording */ }
      }
      return null;
    })();
    charRef.current = { key, promise };
    // A failed attempt must not stick — let the next panel try again.
    void promise.then((id) => { if (!id && charRef.current?.promise === promise) charRef.current = null; });
    return promise;
  }

  // Point each speech bubble at its speaker's head. Runs in the background after a picture is
  // drawn; bubbles the user already dragged are left alone, and any failure keeps the defaults.
  async function placeBubbles(i: number, mediaId: string) {
    const p = panels[i];
    const todo = p.bubbles.map((b, k) => ({ b, k })).filter(({ b }) => b.text.trim() && b.x === undefined);
    if (!todo.length) return;
    try {
      const r = await api<{ bubbles: { x: number; y: number; tx: number; ty: number }[] }>("/api/studio/place-bubbles", {
        method: "POST",
        body: JSON.stringify({ media_id: mediaId, scene: p.scene, characters, bubbles: todo.map(({ b }) => ({ side: b.side, text: b.text })) }),
      });
      if (r.bubbles.length !== todo.length) return;
      setPanels((ps) => ps.map((q, idx) => idx !== i || q.mediaId !== mediaId ? q : {
        ...q,
        bubbles: q.bubbles.map((b, k) => {
          const at = todo.findIndex((t) => t.k === k);
          return at >= 0 && b.x === undefined ? { ...b, ...r.bubbles[at] } : b;
        }),
      }));
    } catch { /* keep default positions */ }
  }

  // Flow draws first. If it cannot (daily limit, tab not ready…), the user's own picture
  // engine takes over, so a story can still be finished.
  async function makeImage(i: number) {
    if (!projectId && !pictureEngine) return;
    update(i, { state: "working", error: undefined });

    // Each helper returns null when it drew the picture, otherwise the reason it could not.
    const viaFlow = async (): Promise<string | null> => {
      if (!projectId) return "No Flow project is linked.";
      try {
        const pid = await freshProjectId();
        setProjectId(pid);
        // No sheet (blocked / failed)? Anchor on the first panel already drawn instead.
        const ref = (await ensureCharRef(pid)) ?? panels.find((p, k) => k !== i && p.mediaId)?.mediaId ?? null;
        const base = imagePrompt(panels[i].scene, panels[i].space);
        const prompt = ref ? `Use the exact same characters, faces, outfits and art style as the reference image. ${base}` : base;
        const r = await serial(() => runRequest("gen_image", {
          prompt, project_id: pid,
          ...(ref ? { ref_media_ids: [ref] } : {}),
          aspect_ratio: format === "9:16" ? "IMAGE_ASPECT_RATIO_PORTRAIT" : "IMAGE_ASPECT_RATIO_LANDSCAPE", variant_count: 1,
        }));
        update(i, { mediaId: r.mediaIds[0], state: "done", videoId: undefined, error: undefined });
        void placeBubbles(i, r.mediaIds[0]);
        return null;
      } catch (e) {
        return (e as Error).message;
      }
    };

    const viaBackup = async (earlier: string, flowFirst: boolean): Promise<string | null> => {
      if (!pictureEngine) return earlier || "No picture engine is ready.";
      // A blocked prompt would be blocked everywhere, so do not spend the user's credits on it.
      if (/UNSAFE|FILTER/i.test(earlier)) return earlier;
      // Keep the same faces: attach the character sheet, or the first panel already drawn.
      const sheet = charRef.current ? await charRef.current.promise : null;
      const ref = sheet ?? panels.find((p, k) => k !== i && p.mediaId)?.mediaId ?? null;
      const base = imagePrompt(panels[i].scene, panels[i].space);
      const prompt = ref ? `Use the exact same characters, faces, outfits and art style as the reference image. ${base}` : base;
      try {
        const r = await serial(() => api<{ media_id: string; engine: string; quota?: Quota }>("/api/studio/image", {
          method: "POST", body: JSON.stringify({ prompt, format, ref_media_id: ref }),
        }));
        update(i, { mediaId: r.media_id, state: "done", videoId: undefined, error: undefined });
        void placeBubbles(i, r.media_id);
        if (r.quota) setQuota(r.quota);
        const left = r.quota ? ` About ${r.quota.left} free picture${r.quota.left === 1 ? "" : "s"} left today (resets ${r.quota.resets}).` : "";
        const who = r.engine === "Cloudflare" ? "Cloudflare" : pictureEngine;
        if (flowFirst && earlier) setNote(`Flow could not draw panel ${i + 1} (${/QUOTA/i.test(earlier) ? "daily limit reached" : "not ready"}), so ${who} drew it.${left}`);
        else if (r.quota) setNote(`Panel ${i + 1} drawn by ${who}.${left}`);
        return null;
      } catch (e) {
        return String((e as Error).message);
      }
    };

    let failure: string;
    if (source === "flow") {
      const flowErr = await viaFlow();
      if (flowErr === null) return;
      const backupErr = await viaBackup(flowErr, true);
      if (backupErr === null) return;
      // Show the backup's reason (e.g. no credits) when it is the one the person can act on.
      failure = projectId && !/credits|402/i.test(backupErr) ? flowErr : backupErr;
    } else {
      const backupErr = await viaBackup("", false);
      if (backupErr === null) return;
      const flowErr = projectId ? await viaFlow() : backupErr;
      if (flowErr === null) { setNote(`${pictureEngine ?? "The backup engine"} could not draw panel ${i + 1}, so Flow drew it.`); return; }
      failure = backupErr;
    }
    update(i, { state: "error", error: friendly(failure) });
    throw new Error(failure);
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
        if (j.status === "done" && j.file) { setVideoUrl(j.file); setShowResult(true); break; }
        if (j.status === "failed") throw new Error(j.error ?? "render failed");
      }
    } catch (e) {
      setNote(String((e as Error).message));
    } finally { setProgress(""); setBusy(""); }
  }

  const drawn = panels.filter((p) => p.mediaId).length;
  const imagesReady = panels.length > 0 && drawn === panels.length;
  const setupOk = ext && !!projectId;
  const canDraw = !!projectId || !!pictureEngine;
  const stepNow = videoUrl || imagesReady ? 4 : panels.length ? 3 : idea.trim() ? 2 : 1;
  const steps = ["Idea", "Script", "Pictures", "Video"];

  const styleLabel = STYLE_META[style]?.label ?? style;
  const toneLabel = TONES.find((t) => t.key === tone)?.label ?? tone;

  return (
    <div className={`st st--app${panels.length ? " st--editing" : ""}`}>
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
          <button className="eng-btn" onClick={() => setConnectTab("flow")} title="Google Flow in the background, extra picture services, and where to post your videos">🔗 Connect</button>
          <button className="eng-btn" onClick={() => setShowEngines(true)} title="Add your own AI keys">🔌 Engines</button>
        </div>
        {showEngines && <Engines onClose={() => setShowEngines(false)} />}
        {connectTab && <Connect start={connectTab} onClose={() => setConnectTab(null)} />}
      </header>

      <div className="st-app" ref={workRef}>
        {/* ── left: settings ─────────────────────────────────── */}
        <aside className="st-side">
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
            <summary>Look &amp; shape</summary>
            <h3 className="acc-label">Art style</h3>
            <div className="style-tiles">
              {Object.keys(styles?.styles ?? STYLE_META).map((k) => (
                <button key={k} className={`style-tile${style === k ? " on" : ""}`} onClick={() => setStyle(k)}
                  title={STYLE_META[k]?.hint} aria-pressed={style === k}>
                  {STYLE_META[k]?.sample && <img src={STYLE_META[k].sample} alt="" loading="lazy" decoding="async" onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }} />}
                  <span>{STYLE_META[k]?.emoji ?? "🎨"} {STYLE_META[k]?.label ?? k}</span>
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
            {panels.length > 0 && (
              <button className="go side-go" disabled={idea.trim().length < 3 || !!busy} onClick={() => void writeScript()}
                title="Write a fresh script with these settings">
                {busy === "script" ? "Writing…" : "↻ Rewrite the script"}
              </button>
            )}
          </details>

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

          <p className="side-credit">Made by <strong>Ayush</strong></p>
        </aside>

        {/* ── centre ─────────────────────────────────────────── */}
        <main className="st-main">
          {note && <p className="hint">{note}</p>}

          {panels.length === 0 ? (
            <div className="start">
              <section className="start-prompt" aria-label="Your idea">
                <h1>What story should we draw?</h1>
                <textarea rows={2} value={idea} onChange={(e) => setIdea(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && idea.trim().length >= 3 && !busy) void writeScript(); }}
                  placeholder="e.g. Two stickmen survive a zombie outbreak during a solar eclipse" />
                <div className="start-actions">
                  <button className="ai" disabled={!idea.trim() || !!ai} onClick={() => void improve("idea", idea, "", setIdea, "idea")}>
                    {ai === "idea" ? "Thinking…" : "✨ Improve"}
                  </button>
                  <button className="ai" disabled={!!ai} onClick={() => void surprise()}>
                    {ai === "ideas" ? "Thinking…" : "🎲 Surprise me"}
                  </button>
                  <span className="start-summary" title="Change these in the panel on the left">
                    <i>{styleLabel}</i><i>{toneLabel}</i><i>{format}</i><i>{count} panels</i>
                  </span>
                  <span className="model-pick">
                    <button className="model-chip" aria-haspopup="menu" aria-expanded={modelMenu}
                      title="Which engine draws the pictures"
                      onClick={() => setModelMenu((v) => !v)}>
                      🖼 {source === "flow" || !quota?.models ? "Flow" : `${quota.models.find((m) => m.key === quota.model)?.label.replace("FLUX.2 klein ", "Cloudflare ") ?? "Cloudflare"} · ~${quota.left} left`} ▾
                    </button>
                    {modelMenu && (
                      <div className="model-menu" role="menu">
                        <b>Picture model</b>
                        <button role="menuitemradio" aria-checked={source === "flow"} className={source === "flow" ? "on" : ""}
                          onClick={() => { setSource("flow"); setModelMenu(false); }}>
                          <span>{source === "flow" ? "●" : "○"} Google Flow <em>default</em></span>
                          <small>Best quality. {quota?.models ? "Cloudflare draws only if Flow's daily limit is reached." : "Add Cloudflare below as a free backup."}</small>
                        </button>
                        {quota?.models?.map((m) => (
                          <button key={m.key} role="menuitemradio" aria-checked={source === "cloudflare" && quota.model === m.key}
                            className={source === "cloudflare" && quota.model === m.key ? "on" : ""}
                            onClick={() => { setSource("cloudflare"); void switchPictureModel(m.key); }}>
                            <span>{source === "cloudflare" && quota.model === m.key ? "●" : "○"} Cloudflare {m.label.replace("FLUX.2 klein ", "")}</span>
                            <small>{m.note} (~{m.per_day} a day). Flow is used if this fails.</small>
                          </button>
                        ))}
                        {quota?.models
                          ? <small className="muted">Free Cloudflare pictures left today: ~{quota.left}. Resets {quota.resets}.</small>
                          : <button onClick={() => { setModelMenu(false); setConnectTab("picture"); }}><span>☁️ Add Cloudflare (free)</span><small>Free FLUX.2 pictures that keep your characters the same.</small></button>}
                      </div>
                    )}
                  </span>
                  <button className="go" disabled={idea.trim().length < 3 || !!busy} onClick={() => void writeScript()} title="Ctrl+Enter">
                    {busy === "script" ? "Writing…" : "✍️ Write the script"}
                  </button>
                </div>
                {ideas.length > 0 && (
                  <ul className="ideas">
                    {ideas.map((i, k) => (
                      <li key={k}><button onClick={() => { setIdea(i.idea); setIdeas([]); }}><b>{i.title}</b><span>{i.idea}</span></button></li>
                    ))}
                  </ul>
                )}
              </section>

              <section className="start-tpl" aria-label="Templates">
                <div className="start-tpl-head">
                  <h2>Or start from a template</h2>
                  <div className="tpl-cats" role="tablist" aria-label="Template categories">
                    {(["All", ...CATEGORIES] as const).map((c) => {
                      const n = c === "All" ? TEMPLATES.length : TEMPLATES.filter((t) => t.category === c).length;
                      return (
                        <button key={c} role="tab" aria-selected={category === c} className={`tpl-cat${category === c ? " on" : ""}`}
                          onClick={() => setCategory(c)}>{c} <small>{n}</small></button>
                      );
                    })}
                  </div>
                </div>
                <div className="st-templates">
                  {TEMPLATES.filter((t) => category === "All" || t.category === category).map((t) => (
                    <button key={t.id} className="tpl" style={{ ["--c" as string]: t.color }} disabled={!!busy}
                      onClick={() => useTemplate(t)} title={t.idea}>
                      <span className="tpl-cover">
                        <img src={t.cover} alt="" loading="lazy" decoding="async" onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }} />
                        <span className="tpl-emoji">{t.emoji}</span>
                      </span>
                      <span className="tpl-title">{t.title}</span>
                      <span className="tpl-meta">{t.panels} panels · {STYLE_META[t.style]?.label}</span>
                    </button>
                  ))}
                </div>
              </section>
            </div>
          ) : (
            <Editor
              panels={panels}
              format={format}
              title={title}
              selected={Math.min(selected, panels.length - 1)}
              busy={!!busy}
              canDraw={canDraw}
              ai={ai}
              onSelect={setSelected}
              onTitle={setTitle}
              onPanel={editPanel}
              onMove={move}
              onRemove={remove}
              onAdd={addPanel}
              onNew={newStory}
              onDraw={(i) => void makeImage(i).catch(() => {})}
              onImprove={(kind, text, ctx, apply, key) => void improve(kind, text, ctx, apply, key)}
            />
          )}
        </main>
      </div>

      {/* ── bottom action bar (editing only) ─────────────────── */}
      {panels.length > 0 && (
        <div className="bar">
          <div className="bar-info">
            {progress ? <b className="spin">{progress}</b> : <span>{imagesReady ? "All panels drawn — ready!" : `${drawn} of ${panels.length} panels drawn`}</span>}
            {quota && <small className="muted" title="Cloudflare's free allowance. It resets every day at 00:00 UTC."> · Free Cloudflare pictures left today: ~{quota.left}</small>}
            {quota?.models && (
              <select className="muted" aria-label="Cloudflare picture model" value={quota.model} disabled={!!busy}
                onChange={(e) => void switchPictureModel(e.target.value)}
                title={quota.models.find((m) => m.key === quota.model)?.note}>
                {quota.models.map((m) => <option key={m.key} value={m.key}>{m.label} (~{m.per_day}/day)</option>)}
              </select>
            )}
            <div className="meter"><i style={{ width: `${(drawn / panels.length) * 100}%` }} /></div>
          </div>
          {videoUrl && <button onClick={() => setShowResult(true)}>▶ View video</button>}
          <label className="check"><input type="checkbox" checked={animate} onChange={(e) => setAnimate(e.target.checked)} /> Animate (uses more credits)</label>
          {busy && busy !== "render" && <button onClick={() => { stop.current = true; }}>Stop</button>}
          {animate && imagesReady && <button disabled={!!busy} onClick={() => void animateAll()}>{busy === "videos" ? "Animating…" : "🎞 Animate panels"}</button>}
          {!imagesReady
            ? <button className="go" disabled={!!busy || !canDraw} onClick={() => void makeAllImages()}>{busy === "images" ? "Drawing…" : "🖌️ Draw all panels"}</button>
            : <button className="go" disabled={!!busy} onClick={() => void render()}>{busy === "render" ? "Building…" : "🎬 Make my video"}</button>}
        </div>
      )}

      {/* ── finished video ───────────────────────────────────── */}
      {showResult && videoUrl && (
        <div className="result-back" role="dialog" aria-label="Your video" onClick={() => setShowResult(false)}>
          <section className="result" onClick={(e) => e.stopPropagation()}>
            <div className="result-head">
              <h2>🎬 Your video is ready</h2>
              <button onClick={() => setShowResult(false)} aria-label="Close">✕</button>
            </div>
            <div className="result-grid has-pub">
              <div className="result-main">
                <video src={videoUrl} controls autoPlay />
                <a className="dl" href={videoUrl} download="flowboard-comic.mp4">⬇ Download MP4</a>
              </div>
              <Publish fileUrl={videoUrl} title={title} story={idea} vertical={format !== "16:9"}
                onOpenConnect={() => setConnectTab("publish")} />
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
