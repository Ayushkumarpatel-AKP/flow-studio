// Shared Studio types. Bubble geometry is in percent of the frame so the
// editor preview and the ffmpeg renderer agree for every output format.
export type Format = "16:9" | "9:16" | "1:1";

export type Bubble = {
  side: "left" | "right";
  text: string;
  x?: number; // centre, % of width
  y?: number; // centre, % of height
  w?: number; // text width, % of width
  tx?: number; // tail tip, % of width
  ty?: number; // tail tip, % of height
};

export type Panel = {
  scene: string;
  caption: string;
  sfx: string;
  sfxX?: number;
  sfxY?: number;
  // "top" asks the picture for calm background at the top (for a long bubble). The picture still
  // fills the frame; this is chosen by the script, not forced on every panel.
  space?: "none" | "top";
  bubbles: Bubble[];
  mediaId?: string;
  videoId?: string;
  state?: "idle" | "working" | "done" | "error";
  error?: string;
};

export const FORMATS: { key: Format; label: string; hint: string }[] = [
  { key: "16:9", label: "16:9", hint: "YouTube, TV" },
  { key: "9:16", label: "9:16", hint: "Reels, Shorts, TikTok" },
  { key: "1:1", label: "1:1", hint: "Instagram post" },
];

/** Frame size used by the renderer, so the preview can match its proportions. */
export const FRAME: Record<Format, [number, number]> = {
  "16:9": [1280, 720],
  "9:16": [720, 1280],
  "1:1": [1024, 1024],
};

/** Where an un-dragged bubble sits. Mirrors `default_bubble_pos` in studio_render.py. */
export function defaultBubblePos(side: "left" | "right", index: number, fmt: Format): [number, number] {
  const [w, h] = FRAME[fmt];
  if (w >= h) return [side === "left" ? 26 : 74, 14];
  return [50, 9 + 13 * index];
}

export const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
