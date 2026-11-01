import { useRef } from "react";
import { mediaUrl } from "../api/client";
import { clamp, defaultBubblePos, FRAME, type Bubble, type Format, type Panel } from "./types";

type Drag =
  | { kind: "move"; i: number; dx: number; dy: number }
  | { kind: "tail"; i: number }
  | { kind: "size"; i: number }
  | { kind: "sfx" };

interface Props {
  panel: Panel;
  format: Format;
  selected: number; // selected bubble index, -1 none, -2 sfx
  onSelect(i: number): void;
  onBubble(i: number, patch: Partial<Bubble>): void;
  onSfx(x: number, y: number): void;
}

/**
 * Live preview of one panel with its speech bubbles. Bubbles, tail tips and the
 * sound effect can be dragged; sizes are in container units so the picture
 * matches what the renderer draws, whatever the preview's on-screen size.
 */
export function Preview({ panel, format, selected, onSelect, onBubble, onSfx }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const drag = useRef<Drag | null>(null);
  const [fw, fh] = FRAME[format];
  const portrait = fh > fw;
  const fontCq = portrait ? 4.49 : 2.66; // 34px design size, in % of frame width

  const pos = (b: Bubble, i: number): [number, number] => {
    const [dx, dy] = defaultBubblePos(b.side, i, format);
    return [b.x ?? dx, b.y ?? dy];
  };
  const tip = (b: Bubble, i: number): [number, number] => {
    const [x, y] = pos(b, i);
    return [b.tx ?? x, b.ty ?? y + 24];
  };

  const toPct = (e: React.PointerEvent) => {
    const r = box.current!.getBoundingClientRect();
    return [((e.clientX - r.left) / r.width) * 100, ((e.clientY - r.top) / r.height) * 100] as const;
  };

  function start(e: React.PointerEvent, d: Drag) {
    e.stopPropagation();
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    drag.current = d;
    if (d.kind !== "sfx") onSelect(d.i);
  }
  function move(e: React.PointerEvent) {
    const d = drag.current;
    if (!d) return;
    const [px, py] = toPct(e);
    if (d.kind === "move") {
      const b = panel.bubbles[d.i];
      const [ox, oy] = pos(b, d.i);
      const [tx, ty] = tip(b, d.i);
      const nx = clamp(px - d.dx, 4, 96);
      const ny = clamp(py - d.dy, 4, 96);
      // Keep the tail tip anchored relative to the bubble while it moves.
      onBubble(d.i, { x: nx, y: ny, tx: tx + (nx - ox), ty: ty + (ny - oy) });
    } else if (d.kind === "tail") {
      onBubble(d.i, { tx: clamp(px, 0, 100), ty: clamp(py, 0, 100) });
    } else if (d.kind === "size") {
      const [cx] = pos(panel.bubbles[d.i], d.i);
      onBubble(d.i, { w: clamp(Math.abs(px - cx) * 2, 18, 80) });
    } else {
      onSfx(clamp(px, 8, 92), clamp(py, 8, 92));
    }
  }
  const end = () => { drag.current = null; };

  return (
    <div
      ref={box}
      className={`pv pv--${format.replace(":", "x")}`}
      style={{ aspectRatio: `${fw} / ${fh}`, ["--ar" as string]: fw / fh, ["--fs" as string]: `${fontCq}cqw` }}
      onPointerMove={move}
      onPointerUp={end}
      onPointerCancel={end}
      onPointerDown={() => onSelect(-1)}
    >
      {panel.mediaId ? (
        <img className="pv-img" src={mediaUrl(panel.mediaId)} alt="" draggable={false} />
      ) : (
        <div className="pv-empty">{panel.state === "working" ? "Drawing…" : "Not drawn yet — press Draw"}</div>
      )}
      {panel.videoId && <em className="pv-badge">animated</em>}

      {/* tails live under the bubbles so the white body hides their base */}
      <svg className="pv-tails" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
        {panel.bubbles.map((b, i) => {
          if (!b.text.trim()) return null;
          const [x, y] = pos(b, i);
          const [tx, ty] = tip(b, i);
          const half = portrait ? 2.1 : 1.25; // tail base half-width, % of width (matches the renderer)
          const bx = clamp(tx, x - 8, x + 8);
          return (
            <polygon key={i} points={`${bx - half},${y} ${bx + half},${y} ${tx},${ty}`}
              fill="#fff" stroke="#12101c" strokeWidth="0.45" vectorEffect="non-scaling-stroke" />
          );
        })}
      </svg>

      {panel.bubbles.map((b, i) => {
        if (!b.text.trim() && selected !== i) return null;
        const [x, y] = pos(b, i);
        const [tx, ty] = tip(b, i);
        const w = b.w ?? 36;
        const sel = selected === i;
        return (
          <div key={i}>
            <div
              className={`pv-bubble${sel ? " is-sel" : ""}`}
              style={{ left: `${x}%`, top: `${y}%`, maxWidth: `${w + 9}%`, fontSize: `var(--fs)` }}
              onPointerDown={(e) => {
                const [px, py] = toPct(e);
                start(e, { kind: "move", i, dx: px - x, dy: py - y });
              }}
            >
              {b.text || "…"}
            </div>
            {sel && (
              <>
                <span className="pv-handle pv-handle--tail" title="Drag to aim the tail"
                  style={{ left: `${tx}%`, top: `${ty}%` }} onPointerDown={(e) => start(e, { kind: "tail", i })} />
                <span className="pv-handle pv-handle--size" title="Drag to change the width"
                  style={{ left: `${clamp(x + w / 2 + 4.5, 0, 98)}%`, top: `${y}%` }} onPointerDown={(e) => start(e, { kind: "size", i })} />
              </>
            )}
          </div>
        );
      })}

      {panel.sfx.trim() && (
        <div
          className={`pv-sfx${selected === -2 ? " is-sel" : ""}`}
          style={{ left: `${panel.sfxX ?? 50}%`, top: `${panel.sfxY ?? 46}%`, fontSize: `calc(var(--fs) * 3.5)` }}
          onPointerDown={(e) => { onSelect(-2); start(e, { kind: "sfx" }); }}
        >
          {panel.sfx}
        </div>
      )}

      {panel.caption.trim() && (
        <div className="pv-caption" style={{ fontSize: `calc(var(--fs) * 0.9)` }}>{panel.caption}</div>
      )}
    </div>
  );
}
