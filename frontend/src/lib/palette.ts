/**
 * Shared colours so the Gantt, the 2D replay and the before/after view agree.
 * Zones get a stable colour by their position in the site's zone list; robot types and
 * statuses have fixed colours. All values are chosen to read on the dark surface used by the
 * app (`--surface` in globals.css) and to stay distinguishable in grayscale by lightness.
 */

export const ROBOT_TYPE_COLORS: Record<string, string> = {
  excavator: "#f59e0b", // amber
  hauler: "#38bdf8", // sky
  compactor: "#a78bfa", // violet
  printer: "#f472b6", // pink
  surveyor: "#34d399", // emerald
  crane: "#fb7185", // rose
};

export const FALLBACK_ROBOT_COLOR = "#cbd5e1";

export function robotTypeColor(type: string): string {
  return ROBOT_TYPE_COLORS[type] ?? FALLBACK_ROBOT_COLOR;
}

/** Eight zone colours; index by zone order in Requirements.site.zones. */
export const ZONE_COLORS = [
  "#60a5fa",
  "#fbbf24",
  "#4ade80",
  "#f472b6",
  "#c084fc",
  "#2dd4bf",
  "#fb923c",
  "#a3e635",
];

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

export const EVENT_COLORS: Record<string, string> = {
  robot_failure: "#ef4444",
  battery_low: "#f59e0b",
  task_overrun: "#eab308",
};

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
