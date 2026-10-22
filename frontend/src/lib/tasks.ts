import type { PlanResult, SimResult, Task } from "./types";

/** Every task ever planned in a run: the original graph plus the graphs of all replans
 *  (later definitions win, since a replan may re-scope a task). */
export function allTasks(plan: PlanResult, sim?: SimResult | null): Task[] {
  const byId = new Map<string, Task>();
  for (const t of plan.task_graph?.tasks ?? []) byId.set(t.id, t);
  for (const r of sim?.replans ?? []) {
    for (const t of r.after.task_graph?.tasks ?? []) byId.set(t.id, t);
  }
  return [...byId.values()];
}

export function tasksById(tasks: Task[]): Record<string, Task> {
  return Object.fromEntries(tasks.map((t) => [t.id, t]));
}

/** Zone ids in a stable order: the site's declared zones first, then any zone only tasks use. */
export function zoneOrder(plan: PlanResult, tasks: Task[]): string[] {
  const ids = plan.requirements.site.zones.map((z) => z.id);
  for (const t of tasks) if (!ids.includes(t.zone)) ids.push(t.zone);
  return ids;
}
