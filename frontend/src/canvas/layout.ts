import type { Edge } from "@xyflow/react";
import type { FlowNode } from "../store/board";

// A node card is ~240px wide and 260-340px tall depending on its body; these
// boxes are deliberately a little bigger so neighbours never touch.
export const NODE_W = 280;
export const NODE_H = 340;
const GAP = 24;

/** Nearest position to `want` that does not overlap an existing node. */
export function freeSlot(nodes: FlowNode[], want: { x: number; y: number }) {
  const hit = (x: number, y: number) =>
    nodes.some((n) => Math.abs(n.position.x - x) < NODE_W && Math.abs(n.position.y - y) < NODE_H);
  if (!hit(want.x, want.y)) return want;
  for (let r = 1; r < 14; r++) {
    for (let dy = -r; dy <= r; dy++) {
      for (let dx = -r; dx <= r; dx++) {
        if (Math.max(Math.abs(dx), Math.abs(dy)) !== r) continue;
        const x = want.x + dx * (NODE_W + GAP);
        const y = want.y + dy * (NODE_H + GAP);
        if (!hit(x, y)) return { x, y };
      }
    }
  }
  return want;
}

/** Left→right layered layout: a node sits one column right of its deepest parent. */
export function layeredLayout(nodes: FlowNode[], edges: Edge[]): Map<string, { x: number; y: number }> {
  const layer = new Map<string, number>(nodes.map((n) => [n.id, 0]));
  for (let pass = 0; pass < nodes.length; pass++) {
    let changed = false;
    for (const e of edges) {
      if (!layer.has(e.source) || !layer.has(e.target)) continue;
      const want = (layer.get(e.source) ?? 0) + 1;
      if (want > (layer.get(e.target) ?? 0) && want <= nodes.length) {
        layer.set(e.target, want);
        changed = true;
      }
    }
    if (!changed) break;
  }
  const columns = new Map<number, FlowNode[]>();
  for (const n of nodes) {
    const l = layer.get(n.id) ?? 0;
    columns.set(l, [...(columns.get(l) ?? []), n]);
  }
  const out = new Map<string, { x: number; y: number }>();
  for (const [l, col] of columns) {
    col.sort((a, b) => a.position.y - b.position.y);
    col.forEach((n, i) => out.set(n.id, { x: 60 + l * (NODE_W + 70), y: 60 + i * (NODE_H + 30) }));
  }
  return out;
}
