import { create } from "zustand";
import { createNode, patchNode } from "../api/client";
import { layeredLayout } from "../canvas/layout";
import { useBoardStore, type FlowNode, type NodeType } from "./board";

interface Snapshot {
  type: NodeType;
  x: number;
  y: number;
  data: Record<string, unknown>;
  edges: { source: string; target: string }[];
  gone: string; // the rf id that was deleted
}

/** One-click starter graphs, shown on an empty board and in the menu. */
export const RECIPES: Record<string, { label: string; emoji: string; blurb: string; nodes: NodeType[]; edges: [number, number][] }> = {
  hero: { label: "Hero shot", emoji: "🦸", blurb: "Character → Image → Video", nodes: ["character", "image", "video"], edges: [[0, 1], [1, 2]] },
  prompt: { label: "Prompt to picture", emoji: "✨", blurb: "Prompt → Image", nodes: ["prompt", "image"], edges: [[0, 1]] },
  story: { label: "Storyboard film", emoji: "🎞️", blurb: "Storyboard → Video", nodes: ["Storyboard", "video"], edges: [[0, 1]] },
  duo: { label: "Duo scene", emoji: "👯", blurb: "2 Characters → Scene", nodes: ["character", "character", "image"], edges: [[0, 2], [1, 2]] },
};

interface MenuState { x: number; y: number; nodeId: string | null }

interface CanvasToolsState {
  pending: Snapshot | null;
  menu: MenuState | null;
  openMenu(m: MenuState): void;
  closeMenu(): void;
  deleteWithUndo(rfId: string): Promise<void>;
  undoDelete(): Promise<void>;
  dismissUndo(): void;
  autoArrange(): Promise<void>;
  addRecipe(key: keyof typeof RECIPES): Promise<void>;
  setTag(rfId: string, tag: string | undefined): void;
  toggleCollapsed(rfId: string): void;
}

let undoTimer: ReturnType<typeof setTimeout> | null = null;

export const useCanvasTools = create<CanvasToolsState>((set, get) => ({
  pending: null,
  menu: null,
  openMenu: (menu) => set({ menu }),
  closeMenu: () => set({ menu: null }),

  async deleteWithUndo(rfId) {
    const { nodes, edges, deleteNodeByRfId } = useBoardStore.getState();
    const n = nodes.find((x) => x.id === rfId);
    if (!n) return;
    const snap: Snapshot = {
      type: n.data.type,
      x: n.position.x,
      y: n.position.y,
      data: { ...n.data },
      edges: edges.filter((e) => e.source === rfId || e.target === rfId).map((e) => ({ source: e.source, target: e.target })),
      gone: rfId,
    };
    await deleteNodeByRfId(rfId);
    if (undoTimer) clearTimeout(undoTimer);
    set({ pending: snap });
    undoTimer = setTimeout(() => set({ pending: null }), 9000);
  },

  async undoDelete() {
    const snap = get().pending;
    const { boardId } = useBoardStore.getState();
    if (!snap || boardId === null) return;
    set({ pending: null });
    if (undoTimer) clearTimeout(undoTimer);
    const { type: _t, shortId: _s, autoPromptStatus: _a, ...keep } = snap.data as Record<string, unknown>;
    try {
      const dto = await createNode({ board_id: boardId, type: snap.type, x: Math.round(snap.x), y: Math.round(snap.y), data: keep });
      const status = (snap.data.status as FlowNode["data"]["status"]) ?? dto.status;
      if (status && status !== dto.status) void patchNode(dto.id, { status });
      const node: FlowNode = {
        id: String(dto.id),
        type: dto.type,
        position: { x: dto.x, y: dto.y },
        data: { ...(snap.data as FlowNode["data"]), type: snap.type, shortId: dto.short_id, status },
      };
      useBoardStore.setState((s) => ({ nodes: [...s.nodes, node] }));
      const add = useBoardStore.getState().addEdgeFromConnection;
      for (const e of snap.edges) {
        const source = e.source === snap.gone ? node.id : e.source;
        const target = e.target === snap.gone ? node.id : e.target;
        await add(source, target);
      }
    } catch {
      useBoardStore.setState({ error: "Could not restore the node." });
    }
  },
  dismissUndo: () => set({ pending: null }),

  async autoArrange() {
    const { nodes, edges, setNodes } = useBoardStore.getState();
    if (!nodes.length) return;
    const pos = layeredLayout(nodes, edges);
    setNodes(nodes.map((n) => ({ ...n, position: pos.get(n.id) ?? n.position })));
    await Promise.all(
      nodes.map((n) => {
        const p = pos.get(n.id);
        const id = parseInt(n.id, 10);
        return p && !isNaN(id) ? patchNode(id, { x: Math.round(p.x), y: Math.round(p.y) }).catch(() => undefined) : undefined;
      }),
    );
  },

  async addRecipe(key) {
    const r = RECIPES[key];
    const { nodes, addNodeOfType, addEdgeFromConnection } = useBoardStore.getState();
    // Start below/right of whatever is already there so a recipe never lands on top of work.
    const startX = nodes.length ? Math.max(...nodes.map((n) => n.position.x)) + 340 : 80;
    const startY = nodes.length ? Math.min(...nodes.map((n) => n.position.y)) : 120;
    const ids: (string | null)[] = [];
    // Two-column fan-in recipe stacks its first nodes vertically; chains run left→right.
    const fan = key === "duo";
    for (let i = 0; i < r.nodes.length; i++) {
      const x = fan ? startX + (i === 2 ? 340 : 0) : startX + i * 340;
      const y = fan ? startY + (i === 2 ? 170 : i * 340) : startY;
      ids.push(await addNodeOfType(r.nodes[i], { x, y }, { exact: true }));
    }
    for (const [a, b] of r.edges) {
      if (ids[a] && ids[b]) await addEdgeFromConnection(ids[a] as string, ids[b] as string);
    }
  },

  setTag(rfId, tag) {
    useBoardStore.getState().updateNodeData(rfId, { tag });
    const id = parseInt(rfId, 10);
    if (!isNaN(id)) void patchNode(id, { data: { tag: tag ?? null } }).catch(() => undefined);
  },
  toggleCollapsed(rfId) {
    const n = useBoardStore.getState().nodes.find((x) => x.id === rfId);
    const next = !n?.data.collapsed;
    useBoardStore.getState().updateNodeData(rfId, { collapsed: next });
    const id = parseInt(rfId, 10);
    if (!isNaN(id)) void patchNode(id, { data: { collapsed: next } }).catch(() => undefined);
  },
}));
