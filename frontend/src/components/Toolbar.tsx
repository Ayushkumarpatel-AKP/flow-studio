import { Suspense, lazy, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useBoardStore } from "../store/board";
import { ActivityBell } from "./activity/ActivityBell";
import { AiProviderBadge } from "./AiProviderBadge";

// The Engines drawer is shared with Studio and only downloaded when first opened.
const Engines = lazy(() => import("../studio/Engines").then((m) => ({ default: m.Engines })));
const Connect = lazy(() => import("../studio/Connect").then((m) => ({ default: m.Connect })));
const OPEN_ENGINES = "flowboard:open-engines";

export function Toolbar() {
  const [showEngines, setShowEngines] = useState(false);
  const [showConnect, setShowConnect] = useState(false);
  useEffect(() => {
    const open = () => setShowEngines(true);
    window.addEventListener(OPEN_ENGINES, open);
    return () => window.removeEventListener(OPEN_ENGINES, open);
  }, []);
  const boardName = useBoardStore((s) => s.boardName);
  const renameBoard = useBoardStore((s) => s.renameBoard);

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  function startEdit() {
    setDraft(boardName);
    setEditing(true);
    requestAnimationFrame(() => inputRef.current?.select());
  }

  function commitEdit() {
    setEditing(false);
    const trimmed = draft.trim();
    if (trimmed && trimmed !== boardName) {
      renameBoard(trimmed);
    }
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") inputRef.current?.blur();
    if (e.key === "Escape") {
      setEditing(false);
    }
  }

  return (
    <div className="toolbar">
      <span className="toolbar-wordmark">Flowboard</span>
      <span className="toolbar-sep" aria-hidden="true">/</span>
      {editing ? (
        <input
          ref={inputRef}
          className="toolbar-name-input"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commitEdit}
          onKeyDown={onKeyDown}
          aria-label="Board name"
        />
      ) : (
        <button
          className="toolbar-name-btn"
          onClick={startEdit}
          aria-label="Rename board"
          title="Click to rename"
        >
          {boardName || "Untitled"}
        </button>
      )}

      <div className="toolbar-actions">
        <ActivityBell />
        <button className="toolbar-engines" onClick={() => setShowConnect(true)} title="Google Flow in the background, extra picture services, and posting videos">
          🔗 Connect
        </button>
        <button className="toolbar-engines" onClick={() => setShowEngines(true)} title="Add your own AI keys (Groq, Gemini, NVIDIA, OpenAI…)">
          🔌 Engines
        </button>
        <AiProviderBadge />
        <span className="made-by" title="Made by Ayush">Made by Ayush</span>
      </div>
      {showConnect && (
        <Suspense fallback={null}>
          <Connect onClose={() => setShowConnect(false)} />
        </Suspense>
      )}
      {showEngines && (
        <Suspense fallback={null}>
          <Engines onClose={() => setShowEngines(false)} />
        </Suspense>
      )}
    </div>
  );
}
