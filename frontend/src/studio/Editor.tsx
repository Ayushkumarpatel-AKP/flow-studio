import { useState } from "react";
import { mediaUrl } from "../api/client";
import { Preview } from "./Preview";
import type { Bubble, Format, Panel } from "./types";

interface Props {
  panels: Panel[];
  format: Format;
  title: string;
  selected: number;
  busy: boolean;
  canDraw: boolean;
  ai: string;
  onSelect(i: number): void;
  onTitle(t: string): void;
  onPanel(i: number, patch: Partial<Panel>): void;
  onMove(i: number, d: number): void;
  onRemove(i: number): void;
  onAdd(): void;
  onDraw(i: number): void;
  onImprove(kind: "scene" | "dialogue", text: string, context: string, apply: (t: string) => void, key: string): void;
}

/** Canva-style editor: filmstrip on top, live preview left, properties right. */
export function Editor(p: Props) {
  const [sel, setSel] = useState(-1); // selected bubble (-1 none, -2 sfx)
  const panel = p.panels[p.selected];
  if (!panel) return null;
  const i = p.selected;
  const bubbles = panel.bubbles;

  const setBubble = (k: number, patch: Partial<Bubble>) =>
    p.onPanel(i, { bubbles: bubbles.map((b, j) => (j === k ? { ...b, ...patch } : b)) });
  const addSpeaker = () => {
    const side = bubbles[bubbles.length - 1]?.side === "left" ? "right" : "left";
    p.onPanel(i, { bubbles: [...bubbles, { side, text: "" }] });
    setSel(bubbles.length);
  };

  return (
    <div className="ed">
      <div className="ed-head">
        <input className="story-title" value={p.title} onChange={(e) => p.onTitle(e.target.value)} aria-label="Story title" />
        <span className="muted">{p.panels.filter((x) => x.mediaId).length}/{p.panels.length} drawn</span>
      </div>

      <div className="film" role="listbox" aria-label="Panels">
        {p.panels.map((x, k) => (
          <button key={k} role="option" aria-selected={k === i} className={`film-item${k === i ? " on" : ""}`}
            onClick={() => { p.onSelect(k); setSel(-1); }}>
            <span className="film-thumb">
              {x.mediaId ? <img src={mediaUrl(x.mediaId)} alt="" /> : <i>{x.state === "working" ? "…" : "＋"}</i>}
              {x.videoId && <em>▶</em>}
            </span>
            <b>{k + 1}</b>
          </button>
        ))}
        <button className="film-item film-add" onClick={p.onAdd} title="Add a panel"><span className="film-thumb"><i>＋</i></span><b>Add</b></button>
      </div>

      <div className="ed-body">
        <div className="ed-stage">
          <Preview
            panel={panel}
            format={p.format}
            selected={sel}
            onSelect={setSel}
            onBubble={setBubble}
            onSfx={(x, y) => p.onPanel(i, { sfxX: x, sfxY: y })}
          />
          <div className="ed-stage-bar">
            <button disabled={i === 0} onClick={() => { p.onMove(i, -1); p.onSelect(i - 1); }}>← Earlier</button>
            <span>Panel {i + 1} of {p.panels.length}</span>
            <button disabled={i === p.panels.length - 1} onClick={() => { p.onMove(i, 1); p.onSelect(i + 1); }}>Later →</button>
            <button className="danger" onClick={() => p.onRemove(i)} title="Delete this panel">🗑</button>
          </div>
          <p className="muted ed-tip">Tip: drag a bubble to move it. With a bubble selected, drag the <b className="dot dot--r" /> dot to aim its tail and the <b className="dot dot--b" /> dot to resize.</p>
        </div>

        <aside className="ed-props">
          <button className="draw" disabled={p.busy || !p.canDraw || panel.state === "working"} onClick={() => p.onDraw(i)}>
            {panel.state === "working" ? "Drawing…" : panel.mediaId ? "↻ Redraw this panel" : "🖌️ Draw this panel"}
          </button>
          {panel.error && <p className="err">{panel.error}</p>}

          <label className="fld">
            <span>Scene
              <button className="mini" disabled={!!p.ai || !panel.scene.trim()} title="Improve with AI"
                onClick={() => p.onImprove("scene", panel.scene, p.title, (t) => p.onPanel(i, { scene: t }), `s${i}`)}>
                {p.ai === `s${i}` ? "…" : "✨"}
              </button>
            </span>
            <textarea rows={4} value={panel.scene} onChange={(e) => p.onPanel(i, { scene: e.target.value })} />
          </label>

          <label className="fld"><span>Caption (bottom strip)</span>
            <input value={panel.caption} placeholder="Narration…" onChange={(e) => p.onPanel(i, { caption: e.target.value })} />
          </label>

          <div className="fld">
            <span>Speech bubbles</span>
            {bubbles.map((b, k) => (
              <div key={k} className={`bub-row${sel === k ? " on" : ""}`} onFocus={() => setSel(k)}>
                <button className="side" title="Switch speaker side"
                  onClick={() => setBubble(k, { side: b.side === "left" ? "right" : "left", x: undefined, y: undefined, tx: undefined, ty: undefined })}>
                  {b.side === "left" ? "L" : "R"}
                </button>
                <input value={b.text} placeholder="What do they say?" onChange={(e) => setBubble(k, { text: e.target.value })} />
                <button className="mini" disabled={!!p.ai || !b.text.trim()} title="Punch it up with AI"
                  onClick={() => p.onImprove("dialogue", b.text, panel.scene, (t) => setBubble(k, { text: t }), `d${i}-${k}`)}>
                  {p.ai === `d${i}-${k}` ? "…" : "✨"}
                </button>
                <button className="mini" title="Reset to the default spot"
                  onClick={() => setBubble(k, { x: undefined, y: undefined, w: undefined, tx: undefined, ty: undefined })}>⟲</button>
                <button className="mini danger" title="Remove" onClick={() => { p.onPanel(i, { bubbles: bubbles.filter((_, j) => j !== k) }); setSel(-1); }}>✕</button>
              </div>
            ))}
            <button className="link" onClick={addSpeaker}>+ Add a speaker</button>
          </div>

          <label className="fld"><span>Sound effect</span>
            <input value={panel.sfx} placeholder="BONK!" onFocus={() => setSel(-2)} onChange={(e) => p.onPanel(i, { sfx: e.target.value })} />
          </label>
        </aside>
      </div>
    </div>
  );
}
