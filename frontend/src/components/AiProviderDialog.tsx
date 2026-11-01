import { useEffect, useRef } from "react";
import { AiProvidersSection } from "./settings/AiProvidersSection";

/**
 * Standalone dialog for the AI Providers panel.
 *
 * Two modes:
 *  - **User-opened** (`force=false`): backdrop click + ESC + ✕ all close.
 *  - **Forced setup** (`force=true`): no close affordances. The parent
 *    (ForcedSetupGate at the App level) controls visibility based on
 *    `/api/llm/config.configured`. Once the user Applies with a provider
 *    pinned on all 3 features, the next /config poll flips
 *    `configured=true` and the parent unmounts the dialog.
 *
 * SettingsPanel (Google Flow tier / video quality / image model) is a
 * separate dialog — Flow billing decisions don't belong with LLM
 * provider switches.
 */

interface AiProviderDialogProps {
  open: boolean;
  onClose(): void;
  /** When true, hide ✕, ignore ESC, ignore backdrop click. */
  force?: boolean;
}

export function AiProviderDialog({ open, onClose, force = false }: AiProviderDialogProps) {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open || force) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose, force]);

  if (!open) return null;

  return (
    <div
      className={`ai-provider-dialog-backdrop${force ? " ai-provider-dialog-backdrop--force" : ""}`}
      role="presentation"
      onClick={(e) => {
        if (force) return;
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        className="ai-provider-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="AI Providers"
      >
        <div className="ai-provider-dialog__header">
          <span className="ai-provider-dialog__title">
            {force ? "Set up your AI providers" : "AI Providers · Advanced"}
          </span>
          {!force && (
            <button
              type="button"
              className="ai-provider-dialog__close"
              onClick={onClose}
              aria-label="Close AI Providers"
            >
              ×
            </button>
          )}
        </div>
        {force && (
          <div className="ai-provider-dialog__force-banner" role="alert">
            Flowboard needs a provider for each of Auto-prompt, Vision and
            Planner before it can run them. Pick one per row — model and
            effort are optional — then Apply to continue.
          </div>
        )}
        <div className="ai-provider-dialog__easy">
          <span>
            <b>💡 Easier way:</b> paste your own key (Groq, Gemini, NVIDIA, OpenAI…) in <b>Engines</b> and it will power
            auto-prompt, picture description and the planner too. This panel is only for the Claude/Codex command-line tools.
          </span>
          <button
            onClick={() => {
              onClose();
              window.dispatchEvent(new Event("flowboard:open-engines"));
            }}
          >
            🔌 Open Engines
          </button>
        </div>
        <AiProvidersSection />
      </div>
    </div>
  );
}
