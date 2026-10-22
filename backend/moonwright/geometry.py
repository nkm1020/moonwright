"""Shared site geometry: zone layout, distances and the travel model.

The planner (``moonwright.pipeline.schedule``) and the simulator (``moonwright.sim.simulator``)
must agree on where things are and what a leg costs, so both import this module:

    depot_position(site)                 where robots start (``site.depot`` or bottom centre)
    zone_positions(site, zone_ids)       zone id -> centre cell, declared zones first, the rest
                                         laid out deterministically (see :func:`place_zones`)
    distance(a, b)                       Euclidean cells: the straight line the simulator drives
    travel_hours(cells, speed)           whole hours a leg takes (ceil): the planner's time unit
    travel_wh(cells)                     TRAVEL_WH_PER_CELL (0.5 Wh) per cell: the simulator's
                                         travel cost, which the planner budgets for
    start_positions(req, positions)      robot id -> where it is when the plan starts (the depot
                                         unless ``positions`` says otherwise)

Travel in the planner is a per-task transit allowance (an upper bound over the zones a robot
can be sent to), not terrain; the simulator pays the actual straight-line distance. Because
:func:`travel_hours` rounds up and the allowance is a maximum, a robot driving the real leg
always arrives on or before the planned start.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

from moonwright.schemas import Requirements, Site

TRAVEL_WH_PER_CELL = 0.5            # Wh per grid cell driven (simulator and planner alike)

_EPS = 1e-9
_DEPOT_KEEP_OUT_CELLS = 4.0         # auto-placed zone centres stay this far from the depot
_LAYOUT_SPACINGS = (8, 6, 4, 3, 2, 1)

Point = tuple[float, float]
Cell = tuple[int, int]


# --------------------------------------------------------------------------- layout
def depot_position(site: Site) -> Cell:
    """Where robots start: ``site.depot`` or the bottom centre of the grid."""
    if site.depot is not None:
        return (int(site.depot[0]), int(site.depot[1]))
    return (site.grid[0] // 2, 1)


def zone_positions(site: Site, zone_ids: Iterable[str] = ()) -> dict[str, Cell]:
    """Zone id -> centre cell, for every site zone and every id in ``zone_ids``.

    Zones declared in ``site.zones`` keep their centre. The others are laid out
    deterministically: sorted by id, placed row-major from the far side of the grid towards
    the depot on the widest spacing that fits them all, skipping cells already taken and a
    keep-out circle around the depot.
    """
    layout = {z.id: (int(z.center[0]), int(z.center[1])) for z in site.zones}
    missing = sorted(set(zone_ids) - layout.keys())
    layout.update(place_zones(site, missing, set(layout.values())))
    return layout


def place_zones(site: Site, zone_ids: list[str], taken: set[Cell]) -> dict[str, Cell]:
    """Deterministic centres for ``zone_ids`` that avoid ``taken`` cells and the depot."""
    if not zone_ids:
        return {}
    depot = depot_position(site)
    for spacing in _LAYOUT_SPACINGS:
        cells = [c for c in _candidate_cells(site.grid, spacing)
                 if c not in taken and math.dist(c, depot) >= _DEPOT_KEEP_OUT_CELLS]
        if len(cells) >= len(zone_ids):
            return dict(zip(zone_ids, cells))
    # Grid too small for that many zones: use any free cell and share cells if we must.
    cells = [c for c in _candidate_cells(site.grid, 1) if c not in taken] or [depot]
    return {z: cells[i % len(cells)] for i, z in enumerate(zone_ids)}


def _candidate_cells(grid: tuple[int, int], spacing: int) -> list[Cell]:
    """Lattice of cells with the given spacing: far side of the grid first, left to right."""
    gx, gy = grid
    margin = max(1, spacing // 2)
    xs = range(margin, max(margin, gx - margin) + 1, spacing)
    ys = range(max(margin, gy - margin), margin - 1, -spacing)
    return [(x, y) for y in ys for x in xs if 0 <= x < gx and 0 <= y < gy]


# --------------------------------------------------------------------------- travel model
def distance(a: Point, b: Point) -> float:
    """Straight-line distance in cells (what the simulator drives)."""
    return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))


def travel_hours(cells: float, speed_cells_per_h: float) -> int:
    """Whole hours a leg of ``cells`` takes at ``speed_cells_per_h``, rounded up (a 0.3 h leg
    costs 1 h; no distance costs nothing). The planner works in integer hours, so this is the
    unit of every transit allowance."""
    if speed_cells_per_h <= 0:
        raise ValueError("speed_cells_per_h must be positive")
    if cells <= _EPS:
        return 0
    return max(0, math.ceil(cells / speed_cells_per_h - _EPS))


def travel_wh(cells: float) -> float:
    """Energy a leg of ``cells`` costs (:data:`TRAVEL_WH_PER_CELL` per cell)."""
    return max(0.0, cells) * TRAVEL_WH_PER_CELL


def start_positions(req: Requirements,
                    positions: Mapping[str, Point] | None = None) -> dict[str, Point]:
    """Robot id -> where the robot is when the plan starts: ``positions`` where given (a
    replan passes the simulator frame), the depot for every other robot."""
    depot = depot_position(req.site)
    start: dict[str, Point] = {r.id: (float(depot[0]), float(depot[1])) for r in req.robots}
    for robot_id, pos in (positions or {}).items():
        if robot_id in start:
            start[robot_id] = (float(pos[0]), float(pos[1]))
    return start
