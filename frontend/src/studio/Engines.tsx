import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api/client";
import "./engines.css";

type Compat = { ok: boolean; model: string | null; wired: boolean };
type Tier = { label: string; free: boolean; note: string; estimate: boolean };
type Provider = {
  id: string;
  label: string;
  tagline: string;
  key_prefix: string;
  get_key_url: string;
  get_key_steps: string[];
  free_tier: string;
  best_for: string[];
  configured: boolean;
  key_hint: string | null;
  status: "ok" | "rate_limited" | "invalid" | null;
  message: string | null;
  tier: Tier | null;
  limits: { requests?: number | null; tokens?: number | null } | null;
  compat: Record<string, Compat> | null;
};
type Job = { id: string; label: string; kind: string; wired: boolean };
type ModelOpt = { id: string; recommended: boolean };
type JobCfg = {
  engine: string | null;
  model: string | null;
  backup: string | null;
  status: { state: "ok" | "limited" | "none"; engine: string | null; message: string };
  options: { id: string; label: string; models: ModelOpt[] }[];
};
type Overview = {
  providers: Provider[];
  jobs: Job[];
  config: Record<string, JobCfg>;
  assignments: Record<string, string | null>;
  recommended: Record<string, string | null>;
  tips: string[];
};

// Fired after any key is added, tested or removed so other panels (like Voice) refresh themselves.
const CHANGED = "flowboard:engines-changed";
const notify = () => window.dispatchEvent(new Event(CHANGED));

const STATUS: Record<string, { text: string; cls: string }> = {
  ok: { text: "Connected", cls: "ok" },
  rate_limited: { text: "Connected · rate-limited right now", cls: "warn" },
  invalid: { text: "Key rejected", cls: "bad" },
};

/** Engines drawer: paste a key, see what it can do, and choose who does what. */
export function Engines({ onClose }: { onClose: () => void }) {
  const [ov, setOv] = useState<Overview | null>(null);
  const [key, setKey] = useState("");
  const [detected, setDetected] = useState<{ engine: string | null; label: string | null } | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "bad" | "warn"; text: string } | null>(null);
  const [busy, setBusy] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  // Model and backup pickers are for people who want them; everyone else stays on Auto.
  const [adv, setAdv] = useState<boolean>(() => {
    try { return localStorage.getItem("engines.adv") === "1"; } catch { return false; }
  });
  const toggleAdv = (v: boolean) => {
    setAdv(v);
    try { localStorage.setItem("engines.adv", v ? "1" : "0"); } catch { /* ignore */ }
  };
  const seq = useRef(0);

  const load = useCallback(async () => {
    try { setOv(await api<Overview>("/api/engines")); } catch (e) { setMsg({ kind: "bad", text: String((e as Error).message) }); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  // Close on Escape.
  useEffect(() => {
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);

  // Work out which service a pasted key belongs to.
  useEffect(() => {
    const k = key.trim();
    if (k.length < 4) { setDetected(null); return; }
    const mine = ++seq.current;
    const t = setTimeout(() => {
      void api<{ engine: string | null; label: string | null }>("/api/engines/detect", { method: "POST", body: JSON.stringify({ key: k }) })
        .then((d) => { if (mine === seq.current) setDetected(d); }).catch(() => {});
    }, 250);
    return () => clearTimeout(t);
  }, [key]);

  async function addKey(pid: string, k: string) {
    setBusy(`add-${pid}`); setMsg(null);
    try {
      const r = await api<{ result: { status: string; message: string }; overview: Overview }>(`/api/engines/${pid}/key`, {
        method: "PUT", body: JSON.stringify({ key: k.trim() }),
      });
      setOv(r.overview);
      notify();
      const ok = r.result.status === "ok" || r.result.status === "rate_limited";
      setMsg({ kind: ok ? (r.result.status === "ok" ? "ok" : "warn") : "bad", text: r.result.message });
      if (ok) { setKey(""); setDetected(null); }
    } catch (e) {
      setMsg({ kind: "bad", text: String((e as Error).message) });
    } finally { setBusy(""); }
  }

  async function retest(pid: string) {
    setBusy(`test-${pid}`); setMsg(null);
    try {
      const r = await api<{ result: { message: string; status: string }; overview: Overview }>(`/api/engines/${pid}/test`, { method: "POST" });
      setOv(r.overview);
      notify();
      setMsg({ kind: r.result.status === "ok" ? "ok" : "warn", text: r.result.message });
    } catch (e) { setMsg({ kind: "bad", text: String((e as Error).message) }); }
    finally { setBusy(""); }
  }

  async function remove(pid: string) {
    if (!window.confirm("Remove this key from your computer?")) return;
    setOv(await api<Overview>(`/api/engines/${pid}/key`, { method: "DELETE" }));
    notify();
  }

  // Save one job's choice. Changing the engine resets its model; a backup can never equal the engine.
  async function setJob(job: string, patch: Partial<{ engine: string | null; model: string | null; backup: string | null }>) {
    const c = ov?.config[job];
    if (!c) return;
    const next = { engine: c.engine, model: c.model, backup: c.backup, ...patch };
    if (patch.engine !== undefined) next.model = null;
    if (next.backup && next.backup === next.engine) next.backup = null;
    try {
      setOv(await api<Overview>(`/api/engines/assign/${job}`, {
        method: "PUT",
        body: JSON.stringify({ engine: next.engine || null, model: next.model || null, backup: next.backup || null }),
      }));
    } catch (e) { setMsg({ kind: "bad", text: String((e as Error).message) }); }
  }

  const label = (id: string | null) => ov?.providers.find((p) => p.id === id)?.label ?? "—";

  // Rendered on <body> so no parent's stacking context can put the chat dock or toolbar above it.
  return createPortal(
    <div className="eng-back" onClick={onClose}>
      <aside className="eng" role="dialog" aria-label="Engines" onClick={(e) => e.stopPropagation()}>
        <header className="eng-head">
          <h2>🔌 Engines</h2>
          <button onClick={onClose} aria-label="Close">✕</button>
        </header>
        <p className="muted">Add your own AI keys to power writing and more. Keys are saved only on this computer and are never shown again.</p>

        {ov && ov.tips.length > 0 && (
          <div className="eng-tips">
            <b>Recommended for you</b>
            <ul>{ov.tips.map((t) => <li key={t}>{t}</li>)}</ul>
          </div>
        )}

        <section className="eng-paste">
          <label htmlFor="eng-key"><b>Paste any key</b> — we will work out which service it is.</label>
          <div className="eng-row">
            <input id="eng-key" type="password" autoComplete="off" spellCheck={false} value={key}
              onChange={(e) => { setKey(e.target.value); setMsg(null); }} placeholder="gsk_…  nvapi-…  sk-…  AIza…" />
            <button className="primary" disabled={!detected?.engine || !!busy} onClick={() => detected?.engine && void addKey(detected.engine, key)}>
              {busy.startsWith("add") ? "Testing…" : detected?.engine ? `Add ${detected.label}` : "Add key"}
            </button>
          </div>
          {key.trim().length >= 4 && detected && !detected.engine && (
            <p className="eng-note warn">We do not recognise that key. You can still add it from a card below.</p>
          )}
          {msg && <p className={`eng-note ${msg.kind}`}>{msg.text}</p>}
        </section>

        <section>
          <h3>Your engines</h3>
          <div className="eng-cards">
            {ov?.providers.map((p) => {
              const st = p.status ? STATUS[p.status] : null;
              return (
                <article key={p.id} className={`eng-card${p.configured ? " on" : ""}`}>
                  <div className="eng-card-top">
                    <div>
                      <b className="eng-name">{p.label}</b>
                      <div className="muted">{p.tagline}</div>
                    </div>
                    {st ? <span className={`pill ${st.cls}`}>{st.text}</span> : <span className="pill">Not added</span>}
                  </div>

                  {p.configured && p.status !== "invalid" ? (
                    <>
                      {p.tier && (
                        <div className={`tier ${p.tier.free ? "free" : "paid"}`} title={p.tier.note}>
                          {p.tier.free ? "🆓" : "💳"} {p.tier.label}{p.tier.estimate ? " (estimate)" : ""}
                        </div>
                      )}
                      {p.limits && (p.limits.tokens || p.limits.requests) && (
                        <div className="muted">Limits: {p.limits.requests ? `${p.limits.requests} requests` : ""}{p.limits.requests && p.limits.tokens ? " · " : ""}{p.limits.tokens ? `${p.limits.tokens} tokens/min` : ""}</div>
                      )}
                      <div className="compat" aria-label="What this key can do">
                        {ov.jobs.map((j) => {
                          const c = p.compat?.[j.id];
                          const cls = !c?.ok ? "no" : c.wired ? "yes" : "soon";
                          return (
                            <span key={j.id} className={`chip2 ${cls}`} title={c?.ok ? `${c.model}${c.wired ? "" : " — supported, not used by the app yet"}` : "This key cannot do this"}>
                              {cls === "no" ? "✕" : cls === "yes" ? "✓" : "◐"} {j.label}
                            </span>
                          );
                        })}
                      </div>
                      <div className="eng-actions">
                        <span className="muted">Key {p.key_hint}</span>
                        <button disabled={!!busy} onClick={() => void retest(p.id)}>{busy === `test-${p.id}` ? "Testing…" : "Test again"}</button>
                        <button className="danger" onClick={() => void remove(p.id)}>Remove</button>
                      </div>
                    </>
                  ) : (
                    <>
                      <div className="tier free">🆓 {p.free_tier}</div>
                      <div className="muted">Best for: {p.best_for.map((b) => ov.jobs.find((j) => j.id === b)?.label ?? b).join(", ")}</div>
                      {p.status === "invalid" && <p className="eng-note bad">{p.message}</p>}
                      <button className="link" onClick={() => setOpen(open === p.id ? null : p.id)}>{open === p.id ? "Hide steps" : "How do I get a key?"}</button>
                      {open === p.id && (
                        <ol className="steps">
                          {p.get_key_steps.map((s) => <li key={s}>{s}</li>)}
                          <li><a href={p.get_key_url} target="_blank" rel="noopener noreferrer">Open {p.label} →</a></li>
                        </ol>
                      )}
                    </>
                  )}
                </article>
              );
            })}
          </div>
        </section>

        {ov && (
          <section>
            <h3>Who does what</h3>
            <p className="muted">Leave everything on Auto and the app picks the best connected engine. If one fails or runs out, the next one takes over.</p>
            <label className="check eng-adv">
              <input type="checkbox" checked={adv} onChange={(e) => toggleAdv(e.target.checked)} />
              Advanced: choose the model and a backup engine
            </label>
            <div className="eng-jobs">
              {ov.jobs.map((j) => {
                const c = ov.config[j.id];
                if (!c) return null;
                const eng = c.options.find((o) => o.id === c.engine);
                const dot = c.status.state === "ok" ? "ok" : c.status.state === "limited" ? "warn" : "no";
                return (
                  <div className="job" key={j.id}>
                    <div className="job-top">
                      <b>{j.label}</b>
                      {!j.wired && <small> · coming soon</small>}
                      <span className={`jdot ${dot}`} aria-hidden="true" />
                    </div>
                    <div className="muted job-msg">{j.wired ? c.status.message : "Pictures come from your Google Flow plan."}</div>
                    {j.wired && c.options.length > 0 && (
                      <div className={`job-ctl${adv ? " adv" : ""}`}>
                        <label>Engine
                          <select value={c.engine ?? ""} onChange={(e) => void setJob(j.id, { engine: e.target.value || null })}>
                            <option value="">Auto ({label(c.status.engine ?? ov.recommended[j.id])})</option>
                            {c.options.map((o) => <option key={o.id} value={o.id}>{o.label}{ov.recommended[j.id] === o.id ? " ★" : ""}</option>)}
                          </select>
                        </label>
                        {adv && (
                          <>
                            <label>Model
                              <select value={c.model ?? ""} disabled={!eng} onChange={(e) => void setJob(j.id, { model: e.target.value || null })}>
                                <option value="">{eng ? "Recommended" : "Auto picks the best"}</option>
                                {eng?.models.map((m) => <option key={m.id} value={m.id}>{m.id}{m.recommended ? " ★" : ""}</option>)}
                              </select>
                            </label>
                            <label>Backup
                              <select value={c.backup ?? ""} onChange={(e) => void setJob(j.id, { backup: e.target.value || null })}>
                                <option value="">None</option>
                                {c.options.filter((o) => o.id !== c.engine).map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
                              </select>
                            </label>
                          </>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
            <p className="muted">Pictures and animation still use your Google Flow plan.</p>
          </section>
        )}
      </aside>
    </div>,
    document.body,
  );
}
