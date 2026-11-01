import { useEffect } from "react";
import { useReactFlow } from "@xyflow/react";
import { useBoardStore } from "../store/board";
import { RECIPES, useCanvasTools } from "../store/canvasTools";
import { mediaUrl } from "../api/client";

/**
 * Everything that makes the canvas easier for a first-timer, mounted next to
 * the Add-node palette: a tools bar, one-click recipes on an empty board, a
 * right-click menu and an Undo bar after deleting.
 */
export function CanvasExtras() {
  const { fitView } = useReactFlow();
  const nodes = useBoardStore((s) => s.nodes);
  const loading = useBoardStore((s) => s.loading);
  const { pending, menu, autoArrange, addRecipe, closeMenu, undoDelete, dismissUndo, deleteWithUndo } = useCanvasTools();
  const selected = nodes.filter((n) => n.selected);

  // Close the context menu on any outside click / Escape.
  useEffect(() => {
    if (!menu) return;
    const close = () => closeMenu();
    const esc = (e: KeyboardEvent) => e.key === "Escape" && closeMenu();
    window.addEventListener("click", close);
    window.addEventListener("keydown", esc);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("keydown", esc);
    };
  }, [menu, closeMenu]);

  // Ctrl/Cmd+Z undoes the last delete.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && useCanvasTools.getState().pending) {
        e.preventDefault();
        void undoDelete();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [undoDelete]);

  function deleteSelected() {
    if (!selected.length) return;
    if (selected.length === 1) return void deleteWithUndo(selected[0].id);
    if (!window.confirm(`Delete ${selected.length} nodes?`)) return;
    const del = useBoardStore.getState().deleteNodeByRfId;
    selected.forEach((n) => void del(n.id));
  }

  // Blank cards nobody ever filled in (no picture, no prompt, never run).
  const blanks = nodes.filter(
    (n) => !n.data.mediaId && !n.data.prompt && (!n.data.status || n.data.status === "idle") && n.data.type !== "note",
  );
  function cleanBlanks() {
    if (!blanks.length) return;
    if (!window.confirm(`Remove ${blanks.length} empty node${blanks.length > 1 ? "s" : ""}? Nodes with pictures, videos or prompts are kept.`)) return;
    const del = useBoardStore.getState().deleteNodeByRfId;
    blanks.forEach((n) => void del(n.id));
  }

  const menuNode = menu?.nodeId ? nodes.find((n) => n.id === menu.nodeId) : null;

  return (
    <>
      <div className="canvas-tools" role="toolbar" aria-label="Canvas tools">
        <button onClick={() => void autoArrange()} disabled={!nodes.length} title="Tidy everything into neat columns">⚡ Arrange</button>
        <button onClick={() => void fitView({ padding: 0.2, duration: 300 })} disabled={!nodes.length} title="Zoom to fit all nodes">⤢ Fit</button>
        <button onClick={deleteSelected} disabled={!selected.length} title="Delete the selected nodes">
          🗑 Delete{selected.length > 1 ? ` (${selected.length})` : ""}
        </button>
        <button onClick={cleanBlanks} disabled={!blanks.length} title="Remove cards that are still empty">
          🧹 Clean{blanks.length ? ` (${blanks.length})` : ""}
        </button>
        <span className="canvas-tools__menu">
          <button title="Add a ready-made chain of nodes">🧩 Recipes ▾</button>
          <span className="canvas-tools__drop">
            {Object.entries(RECIPES).map(([k, r]) => (
              <button key={k} onClick={() => void addRecipe(k as keyof typeof RECIPES)}>
                {r.emoji} {r.label}
                <small>{r.blurb}</small>
              </button>
            ))}
          </span>
        </span>
      </div>

      {!loading && nodes.length === 0 && (
        <div className="canvas-empty">
          <h2>Start your board</h2>
          <p>Pick a ready-made chain, or add a node from the bar above. Drag from one node&apos;s dot to another to connect them.</p>
          <div className="canvas-empty__grid">
            {Object.entries(RECIPES).map(([k, r]) => (
              <button key={k} onClick={() => void addRecipe(k as keyof typeof RECIPES)}>
                <span>{r.emoji}</span>
                <b>{r.label}</b>
                <small>{r.blurb}</small>
              </button>
            ))}
          </div>
        </div>
      )}

      {menu && (
        <div className="ctx-menu" style={{ left: menu.x, top: menu.y }} role="menu" onClick={(e) => e.stopPropagation()}>
          {menuNode ? (
            <>
              <button onClick={() => { void useBoardStore.getState().cloneNodeWithUpstream(menuNode.id); closeMenu(); }}>⧉ Duplicate</button>
              {menuNode.data.mediaId && (
                <a href={mediaUrl(menuNode.data.mediaId)} download onClick={closeMenu}>⬇ Download</a>
              )}
              <button onClick={() => { useCanvasTools.getState().toggleCollapsed(menuNode.id); closeMenu(); }}>
                {menuNode.data.collapsed ? "▸ Unfold" : "▾ Fold"}
              </button>
              <button className="danger" onClick={() => { void deleteWithUndo(menuNode.id); closeMenu(); }}>✕ Delete</button>
            </>
          ) : (
            <>
              <button onClick={() => { void autoArrange(); closeMenu(); }}>⚡ Arrange all</button>
              <button onClick={() => { void fitView({ padding: 0.2, duration: 300 }); closeMenu(); }}>⤢ Fit view</button>
            </>
          )}
        </div>
      )}

      {pending && (
        <div className="undo-bar" role="status">
          <span>Node deleted</span>
          <button onClick={() => void undoDelete()}>↶ Undo</button>
          <button className="x" onClick={dismissUndo} aria-label="Dismiss">×</button>
        </div>
      )}
    </>
  );
}
