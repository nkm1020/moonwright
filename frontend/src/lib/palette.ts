/**
 * Shared colours so the Gantt, the 2D replay, the before/after view and the histogram agree.
 *
 * Colour discipline of the mission console:
 * - amber (`--accent` in globals.css) is reserved for primary actions and the current stage;
 *   nothing in this file uses it;
 * - robot types are the saturated family (dots, stripes, replay markers);
 * - zones are a muted, low-chroma family (OKLCH L 0.72, C 0.05) used for Gantt bar fills and
 *   replay zone discs, so a bar fill never reads as a robot-type dot;
 * - statuses and events are the semantic colours of globals.css (ok / fail / warn / info).
 * All values read on the dark surface (`--surface`) and stay distinguishable in grayscale by
 * lightness.
 */

export const ROBOT_TYPE_COLORS: Record<string, string> = {
  excavator: "#fb923c", // orange
  hauler: "#38bdf8", // sky
  compactor: "#a78bfa", // violet
  printer: "#f472b6", // pink
  surveyor: "#34d399", // emerald
};

export const FALLBACK_ROBOT_COLOR = "#cbd5e1";

export function robotTypeColor(type: string): string {
  return ROBOT_TYPE_COLORS[type] ?? FALLBACK_ROBOT_COLOR;
}

/** Eight muted zone colours (relative luminance ~0.37: dark ink reads on all of them at
 *  ~7.8:1); index by zone order in Requirements.site.zones. */
export const ZONE_COLORS = [
  "#8da8c3", // slate blue
  "#8fae94", // sage
  "#b5a282", // sand
  "#bb99af", // dusty mauve
  "#80afae", // teal grey
  "#c29998", // dusty rose
  "#a4a0c2", // lavender grey
  "#a3a985", // olive
];

/** Ink for labels drawn on a ZONE_COLORS fill. */
export const ZONE_INK = "#0b0f14";

export function zoneColor(zoneId: string, zoneIds: string[]): string {
  const i = zoneIds.indexOf(zoneId);
  if (i >= 0) return ZONE_COLORS[i % ZONE_COLORS.length];
  // unknown zone (replanned graph added one): hash the id
  let h = 0;
  for (const ch of zoneId) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return ZONE_COLORS[h % ZONE_COLORS.length];
}

export const STATUS_COLORS: Record<string, string> = {
  idle: "#94a3b8",
  moving: "#38bdf8",
  working: "#22c55e",
  failed: "#ef4444",
  done: "#64748b",
};

/** robot_failure = `--fail`; battery_low is a forecast of failure (rose, red-adjacent, never
 *  amber); task_overrun = `--warn`. */
export const EVENT_COLORS: Record<string, string> = {
  robot_failure: "#ef4444",
  battery_low: "#fb7185",
  task_overrun: "#eab308",
};

/** Chart annotation colours shared by the Gantt and the replay timeline. */
export const CHART_COLORS = {
  /** the "now" line of a replay / before-after chart */
  now: "#ffffff",
  deadline: "#f87171",
  deadlineText: "#fca5a5",
  /** a schedule entry whose task is not in the graph */
  missing: "#64748b",
} as const;

/** Monte Carlo finish-time histogram: bins inside the horizon in info blue, bins past the
 *  deadline in the failure red (mirrors `--hist-in` / `--hist-late` in globals.css). */
export const HISTOGRAM_COLORS = {
  withinHorizon: "#60a5fa",
  pastHorizon: "#ef4444",
} as const;

export const STAGE_LABELS: Record<string, string> = {
  structure: "Structure",
  taskgraph: "Task graph",
  validate: "Validate",
  schedule: "Schedule",
  repair: "Repair",
  explain: "Explain",
  simulate: "Simulate",
  replan: "Replan",
};
