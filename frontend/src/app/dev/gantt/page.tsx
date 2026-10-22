import type { Metadata } from "next";
import { GanttDemo } from "./GanttDemo";

export const metadata: Metadata = {
  title: "Gantt dev page · Moonwright",
};

/** Dev page: Gantt + TaskTable against the recorded sample plans in public/samples. */
export default function GanttDevPage() {
  return <GanttDemo />;
}
