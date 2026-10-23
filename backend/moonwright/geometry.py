"""Shared site geometry: zone layout, distances and the travel model.

The planner (``moonwright.pipeline.schedule``) and the simulator (``moonwright.sim.simulator``)
must agree on where things are and what a leg costs, so both import this module:

    depot_position(site)                 where robots start (``site.depot`` or bottom centre)
    zone_positions(site, zone_ids)       zone id -> centre cell, declared zones first, the rest
                                         laid out deterministically (see :func:`place_zones`)
    distance(a, b)                       Euclidean cells: the straight line the simulator drives
    travel_hours(cells, speed)           whole hours a leg takes (ceil): the planner's time unit
    arrival_hour(free_h, cells, speed)   the whole hour a robot free at free_h has driven a leg
                                         (free_h + travel_hours, or less from a fractional hour)
    travel_wh(cells)                     TRAVEL_WH_PER_CELL (0.5 Wh) per cell: the simulator's
                                         travel cost, which the planner charges
    leg_mwh(a, b)                        that cost of the leg a -> b in whole milli-Wh, rounded
                                         up: what CP-SAT charges an arc and check_schedule a step
    start_positions(req, positions)      robot id -> where it is when the plan starts (the depot
                                         unless ``positions`` says otherwise)
    cheapest_legs(points, leg)           the cheapest chain of legs between two points
    spanning_cost(origins, zones, cost)  a lower bound on what robots starting at ``origins``
                                         pay to reach every zone (the travel a hint quotes)
    route_cost(origin, zones, cost)      the same for one robot: its cheapest route through
                                         every zone (Held-Karp), a tighter bound

Travel in the planner is exact and sequence-dependent (phase 3k): CP-SAT orders each robot's
tasks and times and prices the legs of that order, straight lines between zone centres, not
terrain, exactly as the simulator drives them. Because :func:`travel_hours` rounds up, a robot
that leaves as soon as it is free always arrives on or before the planned start.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping

from moonwright.schemas import MWH_PER_WH, Requirements, Site

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
    unit of every leg it times."""
    if speed_cells_per_h <= 0:
        raise ValueError("speed_cells_per_h must be positive")
    if cells <= _EPS:
        return 0
    return max(0, math.ceil(cells / speed_cells_per_h - _EPS))


def arrival_hour(free_h: float, cells: float, speed_cells_per_h: float) -> int:
    """The first whole hour by which a robot that may leave at ``free_h`` has driven a leg of
    ``cells``: ``free_h + travel_hours(cells, speed)`` for a whole ``free_h``. A replan at a
    fractional simulator hour hands the planner a fractional one (``SolveOptions
    .robot_ready_h``), and then the leg is not rounded up on its own: free at 0.5 h, a 0.4 h
    leg arrives by hour 1, and even standing in the zone the robot can start no earlier."""
    whole = math.floor(free_h + _EPS)
    if free_h - whole <= _EPS:
        return whole + travel_hours(cells, speed_cells_per_h)
    if speed_cells_per_h <= 0:
        raise ValueError("speed_cells_per_h must be positive")
    return math.ceil(free_h + max(0.0, cells) / speed_cells_per_h - _EPS)


def travel_wh(cells: float) -> float:
    """Energy a leg of ``cells`` costs (:data:`TRAVEL_WH_PER_CELL` per cell)."""
    return max(0.0, cells) * TRAVEL_WH_PER_CELL


def leg_mwh(a: Point, b: Point) -> int:
    """The leg from ``a`` to ``b`` in the fixed-point unit of every energy comparison:
    :func:`travel_wh` of its distance in whole milli-Wh, rounded up per leg (the 41.146-cell
    leg is 20,574 mWh), exactly as CP-SAT charges an arc of a robot's sequence and
    :func:`moonwright.pipeline.schedule.check_schedule` a step of it. Rounding up means the
    planner never charges a leg less than the simulator drains for it, so a plan verified to
    the last milli-Wh cannot strand a robot over a fraction of one."""
    return max(0, math.ceil(travel_wh(distance(a, b)) * MWH_PER_WH - _EPS))


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


# --------------------------------------------------------------------------- lower bounds
def cheapest_legs(points: Iterable[Point],
                  leg: Callable[[Point, Point], int]) -> Callable[[Point, Point], int]:
    """``leg`` (whole hours or milli-Wh per leg, never negative) between any two of ``points``,
    replaced by the cheapest chain of legs through the others (Floyd-Warshall). ``leg`` rounds
    every leg on its own, so a detour through a third point can undercut the straight leg by
    one unit, and a bound built on straight legs would not be one; on the chains it is, as long
    as ``points`` holds every point a robot may stop at. The result looks up two of
    ``points``."""
    pts = list(dict.fromkeys(points))
    cost = [[leg(p, q) for q in pts] for p in pts]
    for k in range(len(pts)):
        via = cost[k]
        for row in cost:
            to_k = row[k]
            for j, onward in enumerate(via):
                row[j] = min(row[j], to_k + onward)
    index = {p: k for k, p in enumerate(pts)}
    return lambda a, b: cost[index[a]][index[b]]


def spanning_cost(origins: Iterable[Point], zones: Iterable[Point],
                  cost: Callable[[Point, Point], int]) -> int:
    """The cheapest tree joining every point of ``zones`` to one of ``origins`` under ``cost``
    (Prim's algorithm grown from all the origins at once). With ``cost`` from
    :func:`cheapest_legs` over every point the robots may stop at, this is a lower bound on
    what robots that start at ``origins`` (each from its own, any of them on any leg) pay to
    reach every zone, whatever order and split they drive: their routes, cut short to the
    zones, contain such a tree. 0 without origins or zones."""
    roots = list(dict.fromkeys(origins))
    if not roots:
        return 0
    best = {z: min(cost(o, z) for o in roots) for z in dict.fromkeys(zones)}
    total = 0
    while best:
        zone = min(best, key=best.__getitem__)
        total += best.pop(zone)
        best = {other: min(was, cost(zone, other)) for other, was in best.items()}
    return total


MAX_ROUTE_ZONES = 10   # route_cost is exact up to this many zones (2^10 subsets), a tree beyond


def route_cost(origin: Point, zones: Iterable[Point],
               cost: Callable[[Point, Point], int]) -> int:
    """The cheapest route under ``cost`` that starts at ``origin`` and visits every point of
    ``zones``, ending anywhere (Held-Karp over the subsets of zones; no way back, as a robot
    never drives home). With ``cost`` from :func:`cheapest_legs` over every point the robot may
    stop at, this is a lower bound on what one robot that starts at ``origin`` pays to reach
    every zone, whatever order it drives them in, and a far tighter one than a tree: a tree may
    branch, a robot cannot (three zones around a depot: a 37 Wh star against the 51 Wh the
    cheapest route really drives). Beyond :data:`MAX_ROUTE_ZONES` distinct zones it falls back
    to :func:`spanning_cost` from ``origin``, still a lower bound. 0 without zones."""
    pts = [z for z in dict.fromkeys(zones) if z != origin]
    if len(pts) > MAX_ROUTE_ZONES:
        return spanning_cost([origin], pts, cost)
    if not pts:
        return 0
    n = len(pts)
    legs = [[cost(p, q) for q in pts] for p in pts]
    unset = math.inf
    # best[seen][j]: the cheapest route from the origin through the zones of bitmask `seen`
    # that ends at zone j
    best = [[unset] * n for _ in range(1 << n)]
    for j, p in enumerate(pts):
        best[1 << j][j] = cost(origin, p)
    for seen in range(1, 1 << n):
        for j, here in enumerate(best[seen]):
            if here == unset:
                continue
            for k in range(n):
                if not seen >> k & 1:
                    row = best[seen | 1 << k]
                    row[k] = min(row[k], here + legs[j][k])
    return int(min(best[-1]))
