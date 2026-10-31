import React, { Suspense, lazy, useState } from "react";
import ReactDOM from "react-dom/client";
import "@xyflow/react/dist/style.css";
import "./styles.css";
import "./theme-comic.css";

// Two views over the same agent: the simple Studio (idea → video) for new
// users, and the full node canvas. Each is its own chunk, so opening Studio
// never downloads the canvas (React Flow) and vice versa.
const Studio = lazy(() => import("./studio/Studio").then((m) => ({ default: m.Studio })));
const App = lazy(() => import("./App").then((m) => ({ default: m.App })));

function Shell() {
  const [mode, setMode] = useState<"studio" | "canvas">(
    new URLSearchParams(location.search).has("studio") ? "studio" : "canvas",
  );
  const go = (m: "studio" | "canvas") => {
    setMode(m);
    const u = new URL(location.href);
    if (m === "studio") u.searchParams.set("studio", "1");
    else u.searchParams.delete("studio");
    history.replaceState(null, "", u);
  };
  return (
    <>
      <div className="studio-switch" role="tablist">
        <button role="tab" aria-selected={mode === "studio"} className={mode === "studio" ? "on" : ""} onClick={() => go("studio")}>Studio</button>
        <button role="tab" aria-selected={mode === "canvas"} className={mode === "canvas" ? "on" : ""} onClick={() => go("canvas")}>Canvas</button>
      </div>
      <Suspense fallback={<div className="app-loading">Inking the page…</div>}>
        {mode === "studio" ? <Studio /> : <App />}
      </Suspense>
    </>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Shell />
  </React.StrictMode>,
);
