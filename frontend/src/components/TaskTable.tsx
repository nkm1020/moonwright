"use client";
/**
 * Table twin of the Gantt: every task of the graph with its robot type, zone, duration, energy,
 * predecessors and (when scheduled) its robot and time window. Sortable by column, default by
 * start time with unscheduled tasks last. Row click (or Enter / Space) selects a task; clicking an
 * `after` chip selects that predecessor.
 */
import { useMemo, useState } from "react";
import type { KeyboardEvent } from "react";
import type { Requirements, Schedule, ScheduleEntry, Task } from "@/lib/types";
import { robotTypeColor, zoneColor } from "@/lib/palette";

export interface TaskTableProps {
  tasks: Task[];
  schedule: Schedule | null;
  requirements: Requirements;
  selectedTask?: string | null;
  onSelectTask?: (taskId: string | null) => void;
  /** Zone order used for colours; defaults to site zones then task-only zones (= zoneOrder()). */
  zoneIds?: string[];
}

type SortKey = "start" | "id" | "name" | "type" | "zone" | "duration" | "energy" | "robot";

const COLUMNS: { key: SortKey; label: string; align?: "right" }[] = [
  { key: "id", label: "Id" },
  { key: "name", label: "Task" },
  { key: "type", label: "Robot type" },
  { key: "zone", label: "Zone" },
  { key: "duration", label: "Duration", align: "right" },
  { key: "energy", label: "Energy", align: "right" },
];

function naturalCompare(a: string, b: string): number {
  const ma = /^(.*?)(\d+)$/.exec(a);
  const mb = /^(.*?)(\d+)$/.exec(b);
  if (ma && mb && ma[1] === mb[1]) return Number(ma[2]) - Number(mb[2]);
  return a.localeCompare(b, "en");
}

function fmtH(h: number): string {
  return Number.isInteger(h) ? String(h) : h.toFixed(1);
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
  const totalWh = tasks.reduce((s, t) => s + t.energy_wh, 0);
  const totalH = tasks.reduce((s, t) => s + t.duration_h, 0);

  const sortIcon = (key: SortKey) => (sortKey === key ? (asc ? "▲" : "▼") : "");
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
        className="inline-flex items-center gap-1 rounded text-slate-400 hover:text-slate-200 focus:outline-none focus-visible:ring-1 focus-visible:ring-slate-300"
      >
        {col.label}
        <span className="w-2 text-[9px]" aria-hidden>
          {sortIcon(col.key)}
        </span>
      </button>
    </th>
  );

  return (
    <div className="w-full min-w-0 text-xs text-slate-200">
      <div className="overflow-x-auto rounded-md border border-white/10">
        <table className="w-full border-collapse" style={{ fontVariantNumeric: "tabular-nums" }}>
          <thead className="bg-white/[0.04] text-[11px]">
            <tr>
              {COLUMNS.map(headerCell)}
              <th scope="col" className="px-2 py-1.5 text-left font-medium text-slate-400">
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
                  <td className="whitespace-nowrap px-2 py-1.5 text-right">{fmtH(t.duration_h)} h</td>
                  <td className="whitespace-nowrap px-2 py-1.5 text-right">
                    {Math.round(t.energy_wh).toLocaleString("en-US")} Wh
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5">
                    {t.after.length === 0 ? (
                      <span className="text-slate-500">—</span>
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
                            className={`rounded border px-1 font-mono text-[10px] leading-4 ${
                              selectedTask === p
                                ? "border-slate-200 bg-white/15 text-slate-100"
                                : "border-white/15 bg-white/[0.04] text-slate-300 hover:border-white/40"
                            }`}
                            title={`select ${p}`}
                          >
                            {p}
                          </button>
                        ))}
                      </span>
                    )}
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5">
                    {e ? e.robot : <span className="text-slate-500">—</span>}
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 text-right">
                    {e ? (
                      `${fmtH(e.start_h)}–${fmtH(e.end_h)} h`
                    ) : (
                      <span className="text-slate-500">unscheduled</span>
                    )}
                  </td>
                </tr>
              );
            })}
            {rows.length === 0 ? (
              <tr>
                <td colSpan={9} className="px-2 py-4 text-center italic text-slate-500">
                  No tasks in the graph
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-3 text-[10px] text-slate-400">
        <span>
          {tasks.length} {tasks.length === 1 ? "task" : "tasks"}, {scheduled} scheduled
        </span>
        <span>· {fmtH(totalH)} robot-hours</span>
        <span>· {Math.round(totalWh).toLocaleString("en-US")} Wh</span>
        {schedule ? (
          <span>
            · makespan {fmtH(schedule.makespan_h)} h ({schedule.status})
          </span>
        ) : null}
      </div>
    </div>
  );
}
