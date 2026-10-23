"use client";
/**
 * Table twin of the Gantt: every task of the graph with its robot type, zone, quantity (with
 * the rate-model basis as a tooltip), duration, energy (the model's own estimate on hover when
 * the rate model re-priced the task), predecessors and (when scheduled) its robot and time
 * window. Sortable by column, default by start time with unscheduled tasks last. Row click (or
 * Enter / Space) selects a task; clicking an `after` chip selects that predecessor.
 */
import { useMemo, useState } from "react";
import type { KeyboardEvent } from "react";
import type { Requirements, Schedule, ScheduleEntry, Task, Unit } from "@/lib/types";
import { robotTypeColor, zoneColor } from "@/lib/palette";
import { fmtNum } from "@/lib/format";

export interface TaskTableProps {
  tasks: Task[];
  schedule: Schedule | null;
  requirements: Requirements;
  selectedTask?: string | null;
  onSelectTask?: (taskId: string | null) => void;
  /** Zone order used for colours; defaults to site zones then task-only zones (= zoneOrder()). */
  zoneIds?: string[];
}

type SortKey = "start" | "id" | "name" | "type" | "zone" | "quantity" | "duration" | "energy" | "robot";

const COLUMNS: { key: SortKey; label: string; align?: "right" }[] = [
  { key: "id", label: "Id" },
  { key: "name", label: "Task" },
  { key: "type", label: "Robot type" },
  { key: "zone", label: "Zone" },
  { key: "quantity", label: "Quantity", align: "right" },
  { key: "duration", label: "Duration", align: "right" },
  { key: "energy", label: "Energy", align: "right" },
];

export const UNIT_LABELS: Record<Unit, string> = { m3: "m³", m2: "m²", m: "m", ea: "ea" };

function naturalCompare(a: string, b: string): number {
  const ma = /^(.*?)(\d+)$/.exec(a);
  const mb = /^(.*?)(\d+)$/.exec(b);
  if (ma && mb && ma[1] === mb[1]) return Number(ma[2]) - Number(mb[2]);
  return a.localeCompare(b, "en");
}

/** "12", "12.5", "1,344" (hours: integers plain, otherwise one decimal, thousands grouped). */
function fmtH(h: number): string {
  return fmtNum(h, 1);
}

function fmtWh(wh: number): string {
  return Math.round(wh).toLocaleString("en-US");
}

/** "126 m³", "1,413.7 m²" (integers plain, otherwise one decimal, thousands grouped). */
export function fmtQuantity(q: number, unit: Unit | null): string {
  const n = fmtNum(q, 1);
  return unit ? `${n} ${UNIT_LABELS[unit]}` : n;
}

/** "model estimate 10 h / 1,000 Wh" when the model's own figures differ from the rate model's;
 *  null when the task was not re-priced (or the figures agree). */
export function estimateNote(t: Task): string | null {
  if (t.estimated_duration_h === null && t.estimated_energy_wh === null) return null;
  const sameH = t.estimated_duration_h === null || Math.abs(t.estimated_duration_h - t.duration_h) < 0.05;
  const sameWh = t.estimated_energy_wh === null || Math.abs(t.estimated_energy_wh - t.energy_wh) < 0.5;
  if (sameH && sameWh) return null;
  const h = t.estimated_duration_h !== null ? `${fmtH(t.estimated_duration_h)} h` : "—";
  const wh = t.estimated_energy_wh !== null ? `${fmtWh(t.estimated_energy_wh)} Wh` : "—";
  return `model estimate ${h} / ${wh}`;
}

export function TaskTable({
  tasks,
  schedule,
  requirements,
  selectedTask = null,
  onSelectTask,
  zoneIds: zoneIdsProp,
}: TaskTableProps) {
  const [sortKey, setSortKey] = useState<SortKey>("start");
  const [asc, setAsc] = useState(true);

  const zoneIds = useMemo(() => {
    if (zoneIdsProp) return zoneIdsProp;
    const ids = requirements.site.zones.map((z) => z.id);
    for (const t of tasks) if (!ids.includes(t.zone)) ids.push(t.zone);
    return ids;
  }, [zoneIdsProp, requirements, tasks]);

  const entryByTask = useMemo(() => {
    const m = new Map<string, ScheduleEntry>();
    for (const e of schedule?.entries ?? []) if (!m.has(e.task)) m.set(e.task, e);
    return m;
  }, [schedule]);

  const rows = useMemo(() => {
    const dir = asc ? 1 : -1;
    const byStart = (t: Task) => entryByTask.get(t.id)?.start_h ?? Number.POSITIVE_INFINITY;
    const cmp = (a: Task, b: Task): number => {
      switch (sortKey) {
        case "start":
          return byStart(a) - byStart(b) || naturalCompare(a.id, b.id);
        case "id":
          return naturalCompare(a.id, b.id);
        case "name":
          return a.name.localeCompare(b.name, "en");
        case "type":
          return a.robot_type.localeCompare(b.robot_type, "en") || naturalCompare(a.id, b.id);
        case "zone":
          return zoneIds.indexOf(a.zone) - zoneIds.indexOf(b.zone) || naturalCompare(a.id, b.id);
        case "quantity":
          return (
            (a.unit ?? "").localeCompare(b.unit ?? "", "en") ||
            (a.quantity ?? -1) - (b.quantity ?? -1) ||
            naturalCompare(a.id, b.id)
          );
        case "duration":
          return a.duration_h - b.duration_h || naturalCompare(a.id, b.id);
        case "energy":
          return a.energy_wh - b.energy_wh || naturalCompare(a.id, b.id);
        case "robot": {
          const ra = entryByTask.get(a.id)?.robot ?? "~";
          const rb = entryByTask.get(b.id)?.robot ?? "~";
          return naturalCompare(ra, rb) || byStart(a) - byStart(b);
        }
      }
    };
    return [...tasks].sort((a, b) => {
      const c = cmp(a, b);
      // keep unscheduled tasks last whatever the direction when sorting by start
      if (sortKey === "start" && Number.isNaN(c)) return 0;
      return c * dir;
    });
  }, [tasks, sortKey, asc, entryByTask, zoneIds]);

  const toggleSort = (key: SortKey) => {
    if (key === sortKey) setAsc((v) => !v);
    else {
      setSortKey(key);
      setAsc(true);
    }
  };

  const select = (id: string) => onSelectTask?.(selectedTask === id ? null : id);

  const onRowKey = (e: KeyboardEvent<HTMLTableRowElement>, id: string) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      select(id);
    } else if (e.key === "Escape" && selectedTask !== null) {
      onSelectTask?.(null);
    }
  };

  const scheduled = tasks.filter((t) => entryByTask.has(t.id)).length;
  const priced = tasks.filter((t) => t.quantity !== null).length;
  const totalWh = tasks.reduce((s, t) => s + t.energy_wh, 0);
  const totalH = tasks.reduce((s, t) => s + t.duration_h, 0);

  /** An 11 px triangle (the design floor for any glyph) on the sorted column. */
  const sortIcon = (key: SortKey) =>
    sortKey === key ? (
      <svg className="size-[11px]" viewBox="0 0 11 11" fill="currentColor" aria-hidden="true">
        <path d={asc ? "M5.5 2.5l3.5 5h-7z" : "M5.5 8.5l-3.5-5h7z"} />
      </svg>
    ) : null;
  const headerCell = (col: { key: SortKey; label: string; align?: "right" }) => (
    <th
      key={col.key}
      scope="col"
      aria-sort={sortKey === col.key ? (asc ? "ascending" : "descending") : "none"}
      className={`px-2 py-1.5 font-medium ${col.align === "right" ? "text-right" : "text-left"}`}
    >
      <button
        type="button"
        onClick={() => toggleSort(col.key)}
        className="inline-flex items-center gap-1 rounded text-muted hover:text-fg focus:outline-none focus-visible:ring-1 focus-visible:ring-white/40"
      >
        {col.label}
        <span className="inline-flex w-[11px] justify-center" aria-hidden>
          {sortIcon(col.key)}
        </span>
      </button>
    </th>
  );

  const estimateCell = "underline decoration-dotted decoration-white/30 underline-offset-2";

  return (
    <div className="w-full min-w-0 text-xs text-fg">
      <div className="overflow-x-auto rounded-md border border-white/10">
        <table className="w-full border-collapse" style={{ fontVariantNumeric: "tabular-nums" }}>
          <thead className="bg-white/[0.04] text-[11px]">
            <tr>
              {COLUMNS.map(headerCell)}
              <th scope="col" className="px-2 py-1.5 text-left font-medium text-muted">
                After
              </th>
              {headerCell({ key: "robot", label: "Robot" })}
              {headerCell({ key: "start", label: "Window", align: "right" })}
            </tr>
          </thead>
          <tbody>
            {rows.map((t) => {
              const e = entryByTask.get(t.id);
              const isSel = selectedTask === t.id;
              const note = estimateNote(t);
              const quantityTitle =
                t.quantity !== null
                  ? t.basis
                    ? `${fmtQuantity(t.quantity, t.unit)} · ${t.basis}`
                    : fmtQuantity(t.quantity, t.unit)
                  : "no quantity stated: duration and energy are the model's own estimate";
              return (
                <tr
                  key={t.id}
                  tabIndex={0}
                  aria-selected={isSel}
                  onClick={() => select(t.id)}
                  onKeyDown={(ev) => onRowKey(ev, t.id)}
                  className={`cursor-pointer border-t border-white/[0.06] outline-none transition-colors focus-visible:bg-white/[0.08] focus-visible:shadow-[inset_3px_0_0_0_#94a3b8] ${
                    isSel ? "bg-white/[0.1] shadow-[inset_3px_0_0_0_#e5e7eb]" : "hover:bg-white/[0.05]"
                  }`}
                >
                  <td className="whitespace-nowrap px-2 py-1.5 font-mono font-semibold">{t.id}</td>
                  {/* the one flexible column: it takes what the fixed ones leave and truncates,
                      so the Window column on the right never falls off the card */}
                  <td className="w-full max-w-0 truncate px-2 py-1.5" title={t.name}>
                    {t.name}
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5">
                    <span className="inline-flex items-center gap-1.5">
                      <span
                        className="inline-block h-2 w-2 rounded-full"
                        style={{ background: robotTypeColor(t.robot_type) }}
                      />
                      {t.robot_type}
                    </span>
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5">
                    <span className="inline-flex items-center gap-1.5">
                      <span
                        className="inline-block h-2 w-2 rounded-sm"
                        style={{ background: zoneColor(t.zone, zoneIds) }}
                      />
                      {t.zone}
                    </span>
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 text-right" title={quantityTitle}>
                    {t.quantity !== null ? (
                      <span className={t.basis ? estimateCell : undefined}>{fmtQuantity(t.quantity, t.unit)}</span>
                    ) : (
                      <span className="text-dim">—</span>
                    )}
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 text-right" title={note ?? undefined}>
                    <span className={note ? estimateCell : undefined}>{fmtH(t.duration_h)} h</span>
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 text-right" title={note ?? undefined}>
                    <span className={note ? estimateCell : undefined}>{fmtWh(t.energy_wh)} Wh</span>
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5">
                    {t.after.length === 0 ? (
                      <span className="text-dim">—</span>
                    ) : (
                      <span className="inline-flex flex-wrap gap-1">
                        {t.after.map((p) => (
                          <button
                            key={p}
                            type="button"
                            onClick={(ev) => {
                              ev.stopPropagation();
                              select(p);
                            }}
                            className={`rounded border px-1 font-mono text-[11px] leading-4 ${
                              selectedTask === p
                                ? "border-white/60 bg-white/15 text-fg"
                                : "border-white/15 bg-white/[0.04] text-muted hover:border-white/40 hover:text-fg"
                            }`}
                            title={`select ${p}`}
                          >
                            {p}
                          </button>
                        ))}
                      </span>
                    )}
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 font-mono">
                    {e ? e.robot : <span className="text-dim">—</span>}
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 text-right">
                    {e ? (
                      `${fmtH(e.start_h)}–${fmtH(e.end_h)} h`
                    ) : (
                      <span className="text-dim">unscheduled</span>
                    )}
                  </td>
                </tr>
              );
            })}
            {rows.length === 0 ? (
              <tr>
                <td colSpan={10} className="px-2 py-4 text-center italic text-dim">
                  No tasks in the graph
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
      <div className="mt-2 flex flex-wrap gap-x-3 text-[11px] text-muted">
        <span>
          {tasks.length} {tasks.length === 1 ? "task" : "tasks"}, {scheduled} scheduled
        </span>
        <span>· {fmtH(totalH)} robot-hours</span>
        <span>· {fmtWh(totalWh)} Wh</span>
        {tasks.length > 0 ? (
          <span
            title={
              priced > 0
                ? "tasks with a stated quantity: their duration and energy come from the rate table (hover a cell for the model's own estimate)"
                : "no task states a quantity: every duration and energy is the model's own estimate"
            }
          >
            · {priced} of {tasks.length} priced by the rate model
          </span>
        ) : null}
        {schedule ? (
          <span>
            · makespan {fmtH(schedule.makespan_h)} h ({schedule.status})
          </span>
        ) : null}
      </div>
    </div>
  );
}
