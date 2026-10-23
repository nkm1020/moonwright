// Where every scene sits in the final video. Three joins:
//   cut   the scenes butt together;
//   fade  a cross-dissolve: the scene overlaps the previous one by its duration;
//   dip   no overlap: the previous scene fades out to the console background (#0b0f14) over half
//         the duration and this one fades in from it over the other half, so two text-dense
//         frames are never stacked (used between the slides and the app).
// The first scene may fade in from black and the video fades out to black at the end
// (defaults.fadeIn / defaults.fadeOut).

export function normalizeTransition(tr) {
  if (!tr || tr.type === "cut") return { type: "cut", duration: 0 };
  if (tr.type === "fade") return { type: "fade", duration: Number(tr.duration ?? 0.5) };
  if (tr.type === "dip") return { type: "dip", duration: Number(tr.duration ?? 0.5) };
  throw new Error(`unknown transition type ${tr.type} (cut | fade | dip)`);
}

/** Durations and overlaps are snapped to whole frames so every cut and dissolve lands on a frame. */
export function buildTimeline(scenes, durations, fps = 30) {
  const snap = (t) => Math.round(t * fps) / fps;
  const out = [];
  let end = 0;
  scenes.forEach((s, i) => {
    const duration = snap(durations[s.id]);
    const tr = i === 0 ? { type: "cut", duration: 0 } : normalizeTransition(s.transition);
    const prev = out[i - 1];
    const overlap = tr.type === "fade" ? snap(Math.min(tr.duration, duration / 2, prev ? prev.duration / 2 : 0)) : 0;
    // a dip spends half its length on each side of the join, never more than a third of either scene
    const dip = tr.type === "dip" ? snap(Math.min(tr.duration / 2, duration / 3, prev ? prev.duration / 3 : 0)) : 0;
    const start = i === 0 ? 0 : snap(end - overlap);
    const transition = overlap > 0 ? "fade" : dip > 0 ? "dip" : "cut";
    out.push({ id: s.id, index: i, start, duration, end: start + duration, overlap, dip, transition });
    end = snap(start + duration);
  });
  return { scenes: out, total: end, fps };
}
