import { useEffect, useState } from "react";
import { Brand, useConnectedTargets } from "./Connect";

type Result = { target: string; label: string; ok: boolean; detail: string; url?: string | null };

async function call<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof j.detail === "string" ? j.detail : `${res.status} ${res.statusText}`);
  return j as T;
}

/** "Post your video" panel shown next to the finished video. */
export function Publish({ fileUrl, title, story, vertical, onOpenConnect }: {
  fileUrl: string; title: string; story: string; vertical: boolean; onOpenConnect: () => void;
}) {
  const { targets } = useConnectedTargets();
  const jobId = fileUrl.split("/").pop() ?? "";
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const [name, setName] = useState(title);
  const [desc, setDesc] = useState("");
  const [tags, setTags] = useState("");
  const [privacy, setPrivacy] = useState("private");
  const [busy, setBusy] = useState("");
  const [results, setResults] = useState<Result[]>([]);
  const [err, setErr] = useState("");

  useEffect(() => { setName(title); setDesc(""); setTags(""); setResults([]); setErr(""); }, [fileUrl, title]);
  // Every connected place starts ticked, except YouTube asks the user to confirm (it posts publicly if set so).
  useEffect(() => {
    setPicked((p) => {
      const next = { ...p };
      for (const t of targets) if (next[t.id] === undefined) next[t.id] = true;
      return next;
    });
  }, [targets]);

  const chosen = targets.filter((t) => picked[t.id]);

  async function writeForMe() {
    setBusy("kit"); setErr("");
    try {
      const k = await call<{ title: string; description: string; tags: string[] }>("/api/publish/kit", { title: name || title, story, platform: chosen[0]?.id ?? "youtube" });
      setName(k.title); setDesc(k.description); setTags(k.tags.join(", "));
    } catch (e) { setErr((e as Error).message); } finally { setBusy(""); }
  }

  async function send() {
    setBusy("send"); setErr(""); setResults([]);
    try {
      const r = await call<{ results: Result[] }>("/api/publish", {
        job_id: jobId, targets: chosen.map((t) => t.id), title: name.trim() || "My comic story", description: desc,
        tags: tags.split(/[,\s]+/).map((t) => t.replace(/^#/, "").trim()).filter(Boolean), privacy, vertical,
      });
      setResults(r.results);
    } catch (e) { setErr((e as Error).message); } finally { setBusy(""); }
  }

  if (targets.length === 0) {
    return (
      <section className="pub">
        <h3>📣 Post your video</h3>
        <p className="muted">Send it to YouTube Shorts, Telegram, Discord, Google Drive or a folder with one click.</p>
        <button className="go" onClick={onOpenConnect}>🔗 Connect a place to post</button>
      </section>
    );
  }

  return (
    <section className="pub">
      <h3>📣 Post your video</h3>
      <div className="pub-targets">
        {targets.map((t) => (
          <label key={t.id} className={`pub-target ${picked[t.id] ? "on" : ""}`}>
            <input type="checkbox" checked={!!picked[t.id]} onChange={(e) => setPicked((p) => ({ ...p, [t.id]: e.target.checked }))} />
            <Brand c={t} size={26} /> <span>{t.label}</span>
          </label>
        ))}
        <button className="pub-more" onClick={onOpenConnect} title="Connect more places">＋</button>
      </div>
      <label className="cn-field"><span>Title</span><input value={name} maxLength={120} onChange={(e) => setName(e.target.value)} /></label>
      <label className="cn-field"><span>Description</span><textarea rows={3} value={desc} onChange={(e) => setDesc(e.target.value)} /></label>
      <label className="cn-field"><span>Hashtags (separate with commas)</span><input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="comic, funny, stickman" /></label>
      <div className="pub-row">
        <button onClick={() => void writeForMe()} disabled={!!busy}>{busy === "kit" ? "Writing…" : "✨ Write title & hashtags"}</button>
        {picked.youtube && targets.some((t) => t.id === "youtube") && (
          <label className="pub-priv">YouTube visibility
            <select value={privacy} onChange={(e) => setPrivacy(e.target.value)}>
              <option value="private">Private (safe)</option><option value="unlisted">Unlisted</option><option value="public">Public</option>
            </select>
          </label>
        )}
      </div>
      {err && <p className="eng-note bad">{err}</p>}
      <button className="go" disabled={!!busy || chosen.length === 0 || !name.trim()} onClick={() => void send()}>
        {busy === "send" ? "Posting…" : `Post to ${chosen.length} place${chosen.length === 1 ? "" : "s"}`}
      </button>
      {results.length > 0 && (
        <ul className="pub-results">
          {results.map((r) => (
            <li key={r.target} className={r.ok ? "ok" : "bad"}>
              <b>{r.ok ? "✓" : "✕"} {r.label}</b> {r.detail}
              {r.url && <> <a href={r.url} target="_blank" rel="noreferrer">Open ↗</a></>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
