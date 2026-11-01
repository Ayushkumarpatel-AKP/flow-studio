import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import "./connect.css";

type Field = { name: string; label: string; secret: boolean; placeholder?: string; value: string; set: boolean };
export type Connector = {
  id: string;
  group: "picture" | "publish" | "soon";
  label: string;
  icon: string | null;
  emoji?: string | null;
  color: string | null;
  tagline: string;
  free: string | null;
  steps: string[];
  get_key_url?: string | null;
  oauth: boolean;
  custom: boolean;
  fields: Field[];
  status: "ok" | "error" | "off" | "needs_signin" | "soon";
  enabled: boolean;
  detail: string | null;
};
type Overview = { connectors: Connector[]; picture_order: string[] };
type Runner = {
  state: "no_browser" | "working" | "waiting" | "other_browser" | "needs_setup" | "stopped";
  hint: string;
  running: boolean;
  bridge_connected: boolean;
  setup_done: boolean;
  browsers: string[];
  browser: string | null;
  keep_running: boolean;
  extension_dir: string;
};

export const CONNECTORS_CHANGED = "flowboard:connectors-changed";
const notify = () => window.dispatchEvent(new Event(CONNECTORS_CHANGED));

/** Like api() but keeps the server's plain-English reason. */
async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { ...init, headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) } });
  const j = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof j.detail === "string" ? j.detail : `${res.status} ${res.statusText}`);
  return j as T;
}
const post = (b: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(b) });
const put = (b: unknown): RequestInit => ({ method: "PUT", body: JSON.stringify(b) });

/** A service's real logo, tinted in its brand colour. */
export function Brand({ c, size = 44 }: { c: Pick<Connector, "icon" | "emoji" | "color" | "label">; size?: number }) {
  return (
    <span className="brand" style={{ width: size, height: size }} aria-hidden>
      {c.icon
        ? <i style={{ background: c.color ?? "#111", WebkitMaskImage: `url(/icons/${c.icon}.svg)`, maskImage: `url(/icons/${c.icon}.svg)` }} />
        : <b>{c.emoji ?? c.label.slice(0, 1)}</b>}
    </span>
  );
}

const STATE_PILL: Record<string, { text: string; cls: string }> = {
  ok: { text: "Connected", cls: "ok" },
  error: { text: "Needs attention", cls: "bad" },
  needs_signin: { text: "Sign in needed", cls: "warn" },
  off: { text: "Not connected", cls: "" },
  soon: { text: "Coming soon", cls: "" },
};

type Msg = { kind: "ok" | "bad" | "warn"; text: string };
type Tab = "flow" | "picture" | "publish" | "more";

/** Connect hub: Flow in the background, extra picture services, and where finished videos go. */
export function Connect({ onClose, start = "flow" }: { onClose: () => void; start?: Tab }) {
  const [tab, setTab] = useState<Tab>(start);
  const [ov, setOv] = useState<Overview | null>(null);
  const [runner, setRunner] = useState<Runner | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [vals, setVals] = useState<Record<string, Record<string, string>>>({});
  const [msg, setMsg] = useState<Record<string, Msg>>({});
  const [busy, setBusy] = useState("");
  const [adding, setAdding] = useState(false);
  const [top, setTop] = useState<Msg | null>(null);

  const load = useCallback(async () => {
    try { setOv(await call<Overview>("/api/connectors")); } catch (e) { setTop({ kind: "bad", text: (e as Error).message }); }
  }, []);
  const loadRunner = useCallback(async () => {
    try { setRunner(await call<Runner>("/api/flow-runner")); } catch { /* agent not reachable */ }
  }, []);

  useEffect(() => { void load(); void loadRunner(); }, [load, loadRunner]);
  // Keep status fresh while the drawer is open (Google sign-in finishes in another tab).
  useEffect(() => {
    const t = setInterval(() => { void loadRunner(); if (tab !== "flow") void load(); }, 4000);
    return () => clearInterval(t);
  }, [load, loadRunner, tab]);
  useEffect(() => {
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);

  const setMsgFor = (id: string, m: Msg | null) => setMsg((p) => { const n = { ...p }; if (m) n[id] = m; else delete n[id]; return n; });

  async function run<T>(id: string, fn: () => Promise<T>) {
    setBusy(id); setMsgFor(id, null);
    try { return await fn(); } catch (e) { setMsgFor(id, { kind: "bad", text: (e as Error).message }); }
    finally { setBusy(""); }
  }

  async function saveCard(c: Connector) {
    const values = vals[c.id] ?? {};
    const r = await run(c.id, () => call<{ result: { ok: boolean; detail: string }; overview: Overview }>(`/api/connectors/${c.id}`, put({ values })));
    if (!r) return;
    setOv(r.overview); notify();
    setMsgFor(c.id, { kind: r.result.ok ? "ok" : "bad", text: r.result.detail });
    if (r.result.ok) setVals((p) => ({ ...p, [c.id]: {} }));
  }
  async function testCard(c: Connector) {
    const r = await run(c.id, () => call<{ result: { ok: boolean; detail: string }; overview: Overview }>(`/api/connectors/${c.id}/test`, post({})));
    if (!r) return;
    setOv(r.overview); notify();
    setMsgFor(c.id, { kind: r.result.ok ? "ok" : "bad", text: r.result.detail });
  }
  async function findChat(c: Connector) {
    const r = await run(c.id, () => call<{ result: { ok: boolean; detail: string }; overview: Overview }>("/api/connectors/telegram/find-chat", post({})));
    if (!r) return;
    setOv(r.overview); notify();
    setMsgFor(c.id, { kind: r.result.ok ? "ok" : "bad", text: r.result.detail });
  }
  async function toggle(c: Connector, on: boolean) {
    const r = await run(c.id, () => call<Overview>(`/api/connectors/${c.id}/enable`, post({ enabled: on })));
    if (r) { setOv(r); notify(); }
  }
  async function removeCard(c: Connector) {
    if (!window.confirm(`Remove ${c.label} from this computer?`)) return;
    const r = await run(c.id, () => call<Overview>(`/api/connectors/${c.id}`, { method: "DELETE" }));
    if (r) { setOv(r); notify(); setOpen(null); }
  }
  async function signIn(c: Connector) {
    const r = await run(c.id, () => call<{ url: string }>(`/api/connectors/${c.id}/signin`, post({})));
    if (r) {
      window.open(r.url, "_blank", "noopener");
      setMsgFor(c.id, { kind: "warn", text: "Finish signing in on the Google page that just opened. This card turns green by itself." });
    }
  }
  async function move(id: string, dir: -1 | 1) {
    if (!ov) return;
    const o = [...ov.picture_order];
    const i = o.indexOf(id), j = i + dir;
    if (i < 0 || j < 0 || j >= o.length) return;
    [o[i], o[j]] = [o[j], o[i]];
    setOv(await call<Overview>("/api/connectors/order/picture", put({ order: o }))); notify();
  }

  const list = (g: Connector["group"]) => (ov?.connectors ?? []).filter((c) => c.group === g);

  function card(c: Connector) {
    const pill = STATE_PILL[c.status];
    const isOpen = open === c.id;
    const keyless = c.id === "pollinations";
    const m = msg[c.id];
    return (
      <div key={c.id} className={`cn-card ${c.status === "ok" ? "on" : ""} ${c.status === "soon" ? "soon" : ""}`}>
        <button className="cn-top" onClick={() => c.status !== "soon" && setOpen(isOpen ? null : c.id)} aria-expanded={isOpen} disabled={c.status === "soon"}>
          <Brand c={c} />
          <span className="cn-text">
            <span className="cn-name">{c.label}</span>
            <span className="cn-tag">{c.tagline}</span>
          </span>
          <span className={`pill ${pill.cls}`}>{pill.text}</span>
        </button>
        {c.status === "soon" && c.free && <p className="muted cn-free">{c.free}</p>}
        {isOpen && (
          <div className="cn-body">
            {c.free && <p className="cn-free">💡 {c.free}</p>}
            {c.detail && c.status !== "soon" && <p className={`eng-note ${c.status === "ok" ? "ok" : "warn"}`}>{c.detail}</p>}
            {c.steps.length > 0 && !keyless && (
              <details open={c.status === "off"}>
                <summary>How to get this</summary>
                <ol className="steps">{c.steps.map((s) => <li key={s}>{s}</li>)}</ol>
                {c.get_key_url && <a href={c.get_key_url} target="_blank" rel="noreferrer">Open the page ↗</a>}
              </details>
            )}
            {c.fields.map((f) => (
              <label key={f.name} className="cn-field">
                <span>{f.label}{f.set && <em> · saved{f.secret ? ` (${f.value})` : ""}</em>}</span>
                <input
                  type={f.secret ? "password" : "text"} autoComplete="off" spellCheck={false}
                  placeholder={f.set ? (f.secret ? "Paste to replace" : f.value) : f.placeholder}
                  value={vals[c.id]?.[f.name] ?? ""}
                  onChange={(e) => setVals((p) => ({ ...p, [c.id]: { ...(p[c.id] ?? {}), [f.name]: e.target.value } }))}
                />
              </label>
            ))}
            {m && <p className={`eng-note ${m.kind}`}>{m.text}</p>}
            <div className="eng-actions">
              {keyless
                ? <button className="go" disabled={busy === c.id} onClick={() => void toggle(c, !c.enabled)}>{c.enabled ? "Turn off" : "Turn on"}</button>
                : c.custom
                  ? <button disabled={busy === c.id} onClick={() => void testCard(c)}>{busy === c.id ? "Testing…" : "Test"}</button>
                  : <button className="go" disabled={busy === c.id} onClick={() => void saveCard(c)}>{busy === c.id ? "Saving…" : "Save & test"}</button>}
              {c.oauth && c.fields.every((f) => f.set) && c.status !== "error" && (
                <button disabled={busy === c.id} onClick={() => void signIn(c)}>
                  {c.status === "ok" ? "Sign in again" : "Sign in with Google"}
                </button>
              )}
              {c.id === "telegram" && c.fields[0]?.set && (
                <button className="go" disabled={busy === c.id} onClick={() => void findChat(c)}>{busy === c.id ? "Looking…" : "Find my chat"}</button>
              )}
              {c.status === "ok" && !keyless && !c.oauth && !c.custom && <button disabled={busy === c.id} onClick={() => void testCard(c)}>Test again</button>}
              {c.status === "ok" && c.group === "picture" && !keyless && (
                <button onClick={() => void toggle(c, !c.enabled)}>{c.enabled ? "Pause" : "Resume"}</button>
              )}
              <span className="muted" />
              {(c.status !== "off" || c.custom) && !keyless && <button className="danger" onClick={() => void removeCard(c)}>Remove</button>}
            </div>
          </div>
        )}
      </div>
    );
  }

  const pictureOrder = (ov?.picture_order ?? []).map((id) => ov!.connectors.find((c) => c.id === id)).filter(Boolean) as Connector[];

  return createPortal(
    <div className="eng-back" role="dialog" aria-label="Connect" onClick={onClose}>
      <aside className="eng cn" onClick={(e) => e.stopPropagation()}>
        <div className="eng-head">
          <h2>🔗 Connect</h2>
          <button onClick={onClose} aria-label="Close">✕</button>
        </div>
        <p className="muted">Plug in places to draw pictures and places to post your videos. Everything stays on your computer.</p>
        {top && <p className={`eng-note ${top.kind}`}>{top.text}</p>}

        <div className="cn-tabs" role="tablist">
          {([["flow", "Google Flow"], ["picture", "Pictures"], ["publish", "Post videos"], ["more", "More"]] as [Tab, string][]).map(([k, t]) => (
            <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{t}</button>
          ))}
        </div>

        {tab === "flow" && runner && <FlowPanel r={runner} reload={loadRunner} />}

        {tab === "picture" && (
          <>
            <p className="muted">Google Flow draws first. If it is out of credits, these are tried in the order below.</p>
            {pictureOrder.length > 0 && (
              <ol className="cn-order">
                {pictureOrder.map((c, i) => (
                  <li key={c.id}>
                    <Brand c={c} size={28} /> <span>{c.label}</span>
                    <button onClick={() => void move(c.id, -1)} disabled={i === 0} aria-label={`Move ${c.label} up`}>↑</button>
                    <button onClick={() => void move(c.id, 1)} disabled={i === pictureOrder.length - 1} aria-label={`Move ${c.label} down`}>↓</button>
                  </li>
                ))}
              </ol>
            )}
            <div className="eng-cards">{list("picture").map(card)}</div>
            {adding
              ? <CustomForm onDone={(o) => { if (o) { setOv(o); notify(); } setAdding(false); }} />
              : <button className="cn-add" onClick={() => setAdding(true)}>＋ Add any picture service with an API</button>}
          </>
        )}

        {tab === "publish" && (
          <>
            <p className="muted">After your video is made, send it to all of these with one click.</p>
            <div className="eng-cards">{list("publish").map(card)}</div>
          </>
        )}

        {tab === "more" && (
          <>
            <p className="muted">On the road map. Tell me which one you want next.</p>
            <div className="eng-cards">{list("soon").map(card)}</div>
          </>
        )}
      </aside>
    </div>,
    document.body,
  );
}

/* ── Google Flow in the background ─────────────────────────────────────────── */

function FlowPanel({ r, reload }: { r: Runner; reload: () => Promise<void> }) {
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const act = async (name: string, fn: () => Promise<unknown>) => {
    setBusy(name); setErr("");
    try { await fn(); await reload(); } catch (e) { setErr((e as Error).message); } finally { setBusy(""); }
  };
  const tone = r.state === "working" ? "ok" : r.state === "no_browser" ? "bad" : r.state === "stopped" ? "" : "warn";
  const label: Record<Runner["state"], string> = {
    working: "Running in the background", waiting: "Starting…", other_browser: "Connected (other window)",
    needs_setup: "Setup needed", stopped: "Stopped", no_browser: "No browser found",
  };
  const finishSetup = () => act("finish", async () => {
    await call("/api/flow-runner/stop", post({}));
    await call("/api/flow-runner/setup", put({ done: true }));
    await call("/api/flow-runner/start", post({ mode: "background" }));
  });

  return (
    <div className="cn-flow">
      <div className="cn-card on">
        <div className="cn-top static">
          <Brand c={{ icon: "google", color: "#1a73e8", label: "Google Flow" }} />
          <span className="cn-text">
            <span className="cn-name">Google Flow</span>
            <span className="cn-tag">Your signed-in Flow account, running by itself</span>
          </span>
          <span className={`pill ${tone}`}>{label[r.state]}</span>
        </div>
        <div className="cn-body">
          <p className={`eng-note ${tone || "ok"}`}>{r.hint}</p>
          {err && <p className="eng-note bad">{err}</p>}

          {!r.setup_done && r.state !== "no_browser" && (
            <div className="cn-setup">
              <h3>One-time setup (about 2 minutes)</h3>
              <ol className="steps">
                <li>Press <b>Open Flow window</b>. A separate browser window opens just for Flow Studio.</li>
                <li>In that window, sign in to your Google account on Flow.</li>
                <li>Type <code>chrome://extensions</code> (or <code>edge://extensions</code>) in that window, turn on <b>Developer mode</b>, press <b>Load unpacked</b> and pick this folder:
                  <code className="cn-path">{r.extension_dir}</code></li>
                <li>Come back here and press <b>Finish setup</b>. The window then hides itself.</li>
              </ol>
              <div className="eng-actions">
                <button className="go" disabled={!!busy || r.running} onClick={() => void act("setup", () => call("/api/flow-runner/start", post({ mode: "setup" })))}>
                  {busy === "setup" ? "Opening…" : r.running ? "Window is open" : "Open Flow window"}
                </button>
                <button disabled={!!busy || !r.running} onClick={() => void finishSetup()}>{busy === "finish" ? "Starting…" : "Finish setup"}</button>
              </div>
            </div>
          )}

          {r.setup_done && (
            <div className="eng-actions">
              {r.running
                ? <button onClick={() => void act("stop", () => call("/api/flow-runner/stop", post({})))} disabled={!!busy}>Stop background Flow</button>
                : <button className="go" onClick={() => void act("start", () => call("/api/flow-runner/start", post({ mode: "background" })))} disabled={!!busy}>
                  {busy === "start" ? "Starting…" : "Start in background"}
                </button>}
              <button onClick={() => void act("again", async () => {
                await call("/api/flow-runner/stop", post({}));
                await call("/api/flow-runner/setup", put({ done: false }));
              })} disabled={!!busy} title="Open the visible window again, for example to sign in again">Sign in again</button>
            </div>
          )}

          {r.setup_done && (
            <>
              <label className="cn-check">
                <input type="checkbox" checked={r.keep_running} onChange={(e) => void act("keep", () => call("/api/flow-runner/config", put({ keep_running: e.target.checked })))} />
                Open it by itself when I open Flow Studio, and close it when I leave. (If I close the window myself, it stays closed.)
              </label>
              {r.browsers.length > 1 && (
                <label className="cn-field">
                  <span>Browser for the Flow window</span>
                  <select value={r.browser ?? ""} onChange={(e) => void act("browser", () => call("/api/flow-runner/config", put({ browser: e.target.value })))}>
                    {r.browsers.map((b) => <option key={b} value={b}>{b === "edge" ? "Microsoft Edge" : "Google Chrome"}</option>)}
                  </select>
                </label>
              )}
            </>
          )}
          <p className="muted cn-fine">
            The window is real but kept out of sight, because Google's captcha does not work in a fully hidden browser.
            Your own browser stays free. The computer must stay on, and Google may sometimes ask you to sign in again.
          </p>
        </div>
      </div>
    </div>
  );
}

/* ── Your own picture service ──────────────────────────────────────────────── */

function CustomForm({ onDone }: { onDone: (o: Overview | null) => void }) {
  const [f, setF] = useState({
    label: "", url: "", header: "Authorization", secret: "", method: "POST",
    body: '{"prompt": "{prompt}", "width": {width}, "height": {height}}', result_kind: "url", result_path: "data.0.url",
  });
  const [msg, setMsg] = useState<Msg | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF((p) => ({ ...p, [k]: e.target.value }));
  async function save() {
    setBusy(true); setMsg(null);
    try {
      const r = await call<{ result: { ok: boolean; detail: string }; overview: Overview }>("/api/connectors/custom", post({
        label: f.label, url: f.url, method: f.method, body: f.body, result_kind: f.result_kind, result_path: f.result_path,
        headers: f.secret.trim() ? { [f.header.trim() || "Authorization"]: f.secret.trim() } : {},
      }));
      setMsg({ kind: r.result.ok ? "ok" : "warn", text: r.result.detail + (r.result.ok ? "" : " It is saved — fix the details and add it again with the same name.") });
      if (r.result.ok) setTimeout(() => onDone(r.overview), 900);
    } catch (e) { setMsg({ kind: "bad", text: (e as Error).message }); } finally { setBusy(false); }
  }
  return (
    <div className="cn-card on cn-custom">
      <div className="cn-body">
        <h3>Your own picture service</h3>
        <p className="muted">Works with any service that has a web API. Use <code>{"{prompt}"}</code>, <code>{"{width}"}</code>, <code>{"{height}"}</code>, <code>{"{aspect}"}</code> in the address or body.</p>
        <label className="cn-field"><span>Name</span><input value={f.label} onChange={set("label")} placeholder="My image server" /></label>
        <label className="cn-field"><span>Web address</span><input value={f.url} onChange={set("url")} placeholder="https://api.example.com/v1/images" /></label>
        <div className="cn-two">
          <label className="cn-field"><span>Key header name</span><input value={f.header} onChange={set("header")} /></label>
          <label className="cn-field"><span>Key value (kept on this computer)</span><input type="password" autoComplete="off" value={f.secret} onChange={set("secret")} placeholder="Bearer sk-…" /></label>
        </div>
        <div className="cn-two">
          <label className="cn-field"><span>Method</span>
            <select value={f.method} onChange={set("method")}><option>POST</option><option>GET</option></select></label>
          <label className="cn-field"><span>The picture comes back as</span>
            <select value={f.result_kind} onChange={set("result_kind")}>
              <option value="url">A link to the picture</option><option value="base64">Base64 text</option><option value="binary">The picture itself</option>
            </select></label>
        </div>
        <label className="cn-field"><span>Request body (JSON)</span><textarea rows={3} value={f.body} onChange={set("body")} spellCheck={false} /></label>
        {f.result_kind !== "binary" && (
          <label className="cn-field"><span>Where is it in the reply?</span><input value={f.result_path} onChange={set("result_path")} placeholder="data.0.url" /></label>
        )}
        {msg && <p className={`eng-note ${msg.kind}`}>{msg.text}</p>}
        <div className="eng-actions">
          <button className="go" disabled={busy || !f.label || !f.url} onClick={() => void save()}>{busy ? "Testing…" : "Save & draw a test picture"}</button>
          <button onClick={() => onDone(null)}>Cancel</button>
        </div>
      </div>
    </div>
  );
}

/** Small hook for "is anything connected?" used by other panels. */
export function useConnectedTargets() {
  const [targets, setTargets] = useState<Connector[]>([]);
  const alive = useRef(true);
  const load = useCallback(async () => {
    try {
      const ov = await call<Overview>("/api/connectors");
      if (alive.current) setTargets(ov.connectors.filter((c) => c.group === "publish" && c.status === "ok"));
    } catch { /* ignore */ }
  }, []);
  useEffect(() => {
    alive.current = true;
    void load();
    window.addEventListener(CONNECTORS_CHANGED, load);
    return () => { alive.current = false; window.removeEventListener(CONNECTORS_CHANGED, load); };
  }, [load]);
  return { targets, reload: load };
}
