"""Re-score a saved evaluation with the current code: no model call, no solver run.

``moonwright eval --csv ... --save-plans DIR`` keeps every run's PlanResult and names it in the
row's ``plan_path`` (relative to the CSV's directory). This script reads such a directory
(``eval.csv`` and the plans its rows name) and recomputes, per row, on the final task graph and
the requirements the run planned from (their ``source_brief`` included):

* ``in_scope`` (verified, and no :func:`moonwright.pipeline.rates.scope_gaps`),
  ``min_volume_share`` and ``min_area_share``, the columns the eval writes, so a change to the
  scope rules (phase 3g: the lift count the brief states, a placed body judged only on a base
  width the brief states) can be read against a paid run without paying for it again;
* ``supply_conflicts``: the ``supply:<haul>`` conflicts the validator now gives the final graph
  (:func:`moonwright.pipeline.validate.validate_graph` with only the supply check on: a haul
  that waits for no source of material, or for too little while another dig could supply
  it), with the haul ids in ``supply_hauls``, and ``supply_advisories``: the balance notes on
  it (:func:`~moonwright.pipeline.validate.supply_advisories`, one per structure whose hauls
  move more than the digs before them remove);
* ``double_counts`` (phase 3i): the structures whose dig or haul parts the validator now
  rejects as a double count (``volume:<structure>`` from 175 % (+1 m3) of the largest volume
  the brief allows, :func:`moonwright.pipeline.rates.volume_double_counts`: live, a berm hauled
  as its length x height x base width box), with the arithmetic in ``double_structures`` (``S2
  560 of 280 m3 (200 %)``);
* ``footprint_changes`` (phase 3i): every structure whose judged volume or area share moves
  when a recorded diameter is read as the rectangle its entry in the brief states
  (:func:`moonwright.pipeline.rates.footprint`; ``brief_footprint=False`` judges the
  footprint as recorded), as recorded -> as judged (``F1 volume 1 -> 0.785``).

It prints one row per condition (runs, verified, verified in scope before -> after, of those
the ones neither the supply check nor the double-count check rejects, verified plans the
supply check would now reject, verified plans the double-count check would now reject, plans
whose footprint judgment changed, verified plans with a supply advisory), the rows whose
``in_scope`` changed, the verified plans the supply and the double-count checks reject and
the plans whose footprint judgment changed, and writes ``eval.rescored.csv`` next to the
input (``--out`` elsewhere): the input's columns with the three scope columns recomputed in
place, then ``in_scope_before``, ``min_volume_share_before``, ``min_area_share_before``,
``supply_conflicts``, ``supply_hauls``, ``supply_advisories``, ``scope_gaps`` (the gap lines
joined by `` | ``), ``double_counts``, ``double_structures`` and ``footprint_changes``. A row
without a readable plan (none named, the file gone, or a file that is not a PlanResult) keeps
its values and is reported as ``no plan``.

Usage (from backend/)::

    uv run python scripts/rescore_eval.py ../docs/eval/2026-10-23-live-eval-run2 --out out/run2.rescored.csv
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from moonwright.pipeline.rates import (
    area_shares,
    footprint,
    scope_gaps,
    volume_double_counts,
    volume_shares,
)
from moonwright.pipeline.validate import supply_advisories, validate_graph
from moonwright.schemas import PlanResult, Requirements, TaskGraph
from moonwright.text import pct, qty

RESCORED = ("in_scope", "min_volume_share", "min_area_share")
ADDED = ("in_scope_before", "min_volume_share_before", "min_area_share_before",
         "supply_conflicts", "supply_hauls", "supply_advisories", "scope_gaps",
         "double_counts", "double_structures", "footprint_changes")


@dataclass
class Rescored:
    """One row: the input values (``row``), the recomputed ones, the supply verdicts, the
    double counts (``S2 560 of 280 m3 (200 %)``) and the footprint changes; ``plan`` is False
    when the row names no readable plan (its values are kept)."""

    row: dict[str, str]
    plan: bool
    success: bool
    in_scope_before: bool | None
    in_scope: bool | None
    min_volume_share: float | None = None
    min_area_share: float | None = None
    supply_hauls: list[str] = field(default_factory=list)
    supply_advisories: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    doubles: list[str] = field(default_factory=list)
    footprints: list[str] = field(default_factory=list)

    def out(self) -> dict[str, Any]:
        """The CSV row: the input columns, the three scope columns recomputed, the added ones."""
        row: dict[str, Any] = dict(self.row)
        for key in RESCORED:
            row[f"{key}_before"] = self.row.get(key, "")
        if self.plan:
            row["in_scope"] = self.in_scope
            row["min_volume_share"] = _share(self.min_volume_share)
            row["min_area_share"] = _share(self.min_area_share)
        row["supply_conflicts"] = len(self.supply_hauls)
        row["supply_hauls"] = " ".join(self.supply_hauls)
        row["supply_advisories"] = len(self.supply_advisories)
        row["scope_gaps"] = " | ".join(self.gaps)
        row["double_counts"] = len(self.doubles)
        row["double_structures"] = " | ".join(self.doubles)
        row["footprint_changes"] = " | ".join(self.footprints)
        return row


def _flag(text: str | None) -> bool | None:
    """'True' / 'False' as the eval writes them; None for an empty cell (an older row)."""
    if text in (None, ""):
        return None
    return text.strip().lower() == "true"


def _share(value: float | None) -> float | str:
    return "" if value is None else round(value, 3)


def _footprint_changes(req: Requirements, graph: TaskGraph) -> list[str]:
    """'F1 volume 1 -> 0.785' per structure whose judged volume or area share moves when its
    recorded diameter is read as the rectangle the brief states (as recorded -> as judged,
    '-' where nothing is judged), in requirements order; [] when no structure records a
    diameter its entry corrects."""
    if not any(footprint(s, req) is not None for s in req.structures):
        return []
    pairs = (("volume", volume_shares(req, graph, brief_footprint=False),
              volume_shares(req, graph)),
             ("area", area_shares(req, graph, brief_footprint=False), area_shares(req, graph)))
    out: list[str] = []
    for s in req.structures:
        moved = [f"{what} {_judged(was.get(s.id))} -> {_judged(now.get(s.id))}"
                 for what, was, now in pairs if not _same(was.get(s.id), now.get(s.id))]
        if moved:
            out.append(f"{s.id} {', '.join(moved)}")
    return out


def _judged(value: float | None) -> str:
    """'0.785', or '-' where nothing is judged."""
    return "-" if value is None else f"{round(value, 3):g}"


def _same(a: float | None, b: float | None) -> bool:
    return a is None and b is None or (
        a is not None and b is not None and math.isclose(a, b, abs_tol=1e-9))


def _read_plan(path: Path | None) -> PlanResult | None:
    """The PlanResult saved at ``path``, or None when there is none or it does not parse (a
    truncated file, another JSON)."""
    if path is None or not path.is_file():
        return None
    try:
        return PlanResult.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValidationError):
        return None


def rescore_row(row: dict[str, str], base: Path) -> Rescored:
    """Recompute one CSV row from the plan its ``plan_path`` names (relative to ``base``)."""
    success = _flag(row.get("success")) is True
    before = _flag(row.get("in_scope"))
    rel = (row.get("plan_path") or "").strip()
    path = base / rel if rel else None
    result = _read_plan(path)
    if result is None:
        return Rescored(row=row, plan=False, success=success, in_scope_before=before,
                        in_scope=before)
    graph, req = result.task_graph, result.requirements
    if graph is None:
        return Rescored(row=row, plan=True, success=result.success, in_scope_before=before,
                        in_scope=False)
    gaps = scope_gaps(req, graph)
    volumes, areas = volume_shares(req, graph), area_shares(req, graph)
    failure = validate_graph(req, graph, coverage=False, volume=False, supply=True)
    hauls = [c.removeprefix("supply:") for c in (failure.conflicts if failure else [])
             if c.startswith("supply:")]
    return Rescored(
        row=row, plan=True, success=result.success, in_scope_before=before,
        in_scope=result.success and not gaps,
        min_volume_share=min(volumes.values()) if volumes else None,
        min_area_share=min(areas.values()) if areas else None,
        supply_hauls=hauls, supply_advisories=supply_advisories(req, graph), gaps=gaps,
        doubles=[f"{c.structure.id}{' key trench' if c.shape.kind == 'trench' else ''} "
                 f"{qty(c.excess_m3)} of {qty(c.largest_m3 or c.shape.m3)} m3 "
                 f"({pct(c.share_largest)})" for c in volume_double_counts(req, graph)],
        footprints=_footprint_changes(req, graph))


def rescore(csv_path: Path) -> tuple[list[str], list[Rescored]]:
    """The input's column names and every row rescored, in file order."""
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        columns = list(reader.fieldnames or [])
    return columns, [rescore_row(r, csv_path.parent) for r in rows]


def _count(n: int, total: int) -> str:
    return f"{n} ({n / total:.0%})" if total else str(n)


def summary(rows: Sequence[Rescored]) -> list[str]:
    """The per-condition table (markdown), then the in-scope flips, the supply and the
    double-count rejects and the footprint changes."""
    lines = [("| condition | runs | verified | verified in scope, before -> after | "
              "in scope after, no supply or double-count conflict | verified, rejected by "
              "supply | verified, rejected by double count | footprint judgment changed | "
              "verified, supply advisory | no plan |"),
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    conditions = list(dict.fromkeys(r.row.get("condition", "") for r in rows))
    for cond in conditions:
        group = [r for r in rows if r.row.get("condition", "") == cond]
        n = len(group)
        ok = [r for r in group if r.success]
        before = sum(r.in_scope_before is True for r in group)
        after = sum(r.in_scope is True for r in group)
        clean = sum(r.in_scope is True and not r.supply_hauls and not r.doubles for r in group)
        rejected = sum(bool(r.supply_hauls) for r in ok)
        doubled = sum(bool(r.doubles) for r in ok)
        advised = sum(bool(r.supply_advisories) for r in ok)
        lines.append(f"| `{cond}` | {n} | {_count(len(ok), n)} | {_count(before, n)} -> "
                     f"{_count(after, n)} | {_count(clean, n)} | {rejected} of {len(ok)} | "
                     f"{doubled} of {len(ok)} | {sum(bool(r.footprints) for r in group)} | "
                     f"{advised} of {len(ok)} | {sum(not r.plan for r in group)} |")
    flips = [r for r in rows if r.plan and r.in_scope_before is not None
             and r.in_scope != r.in_scope_before]
    lines.append("")
    lines.append(f"in_scope changed in {len(flips)} row(s):")
    for r in flips:
        why = r.gaps[0] if r.gaps else (
            f"min_area_share {r.row.get('min_area_share') or '-'} -> {_share(r.min_area_share)}")
        lines.append(f"  {_name(r)}: {r.in_scope_before} -> {r.in_scope} ({why})")
    rejected_rows = [r for r in rows if r.success and r.supply_hauls]
    lines.append("")
    lines.append(f"verified plans the supply check rejects: {len(rejected_rows)}")
    for r in rejected_rows:
        lines.append(f"  {_name(r)}: supply:{', supply:'.join(r.supply_hauls)}")
    doubled_rows = [r for r in rows if r.success and r.doubles]
    lines.append("")
    lines.append(f"verified plans the double-count check rejects: {len(doubled_rows)}")
    for r in doubled_rows:
        lines.append(f"  {_name(r)}: {'; '.join(r.doubles)}")
    moved = [r for r in rows if r.footprints]
    lines.append("")
    lines.append(f"footprint judgment changed in {len(moved)} plan(s):")
    for r in moved:
        scope = (f" (in scope {r.in_scope_before} -> {r.in_scope})"
                 if r.in_scope_before is not None and r.in_scope != r.in_scope_before else "")
        lines.append(f"  {_name(r)}: {'; '.join(r.footprints)}{scope}")
    return lines


def _name(r: Rescored) -> str:
    return f"{r.row.get('brief', '?')} {r.row.get('condition', '?')} #{r.row.get('repeat', '?')}"


def write_csv(path: Path, columns: Sequence[str], rows: Sequence[Rescored]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    names = list(columns) + [c for c in ADDED if c not in columns]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=names, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r.out())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("source", help="an eval directory holding eval.csv, or the CSV itself")
    ap.add_argument("--out", help="where to write the rescored CSV (default: eval.rescored.csv "
                                  "next to the input)")
    args = ap.parse_args(argv)
    source = Path(args.source)
    csv_path = source / "eval.csv" if source.is_dir() else source
    if not csv_path.is_file():
        print(f"no eval CSV at {csv_path}", file=sys.stderr)
        return 2
    columns, rows = rescore(csv_path)
    out = Path(args.out) if args.out else csv_path.with_name(f"{csv_path.stem}.rescored.csv")
    write_csv(out, columns, rows)
    print("\n".join(summary(rows)))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
