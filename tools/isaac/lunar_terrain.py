"""The lunar ground shared by the physics mission (mission.py) and the replay render (render_replay.py).

Pure numpy, deterministic: both scripts build the same heights, so the rovers PhysX drove are drawn
on the ground they drove on. heightfield() -> (xs, Z) at 1 m spacing, the work areas graded level
(flats, work_flats()). Two grounds:
  * procedural (default): a 260 m square of value noise and craters around a gentle site;
  * LOLA (lola=load_lola(npz), from prep_lola.py): +-200 m of real south-pole terrain, NASA LOLA
    5 m/px heights interpolated to 1 m, with the roughness and the craters smaller than 5 m pixels
    resolve added on top (none on the site strip or in front of the lens); far_arrays() is the
    rest of the 16 km tile at 20 m, for the renderer's backdrop."""
import math

import numpy as np

SIZE, N = 260.0, 261
CAMERA_EYES = [(20.0, -60.0), (15.0, -54.0), (56.0, -58.0), (76.0, -22.0)]   # render_replay.SHOTS
LOLA_EYES = [(-30.0, -66.0), (-24.0, -60.0), (-70.0, -100.0), (-120.0, -95.0)]  # render_replay.SHOTS_LOLA


def load_lola(path):
    d = np.load(path)
    return {k: d[k] for k in d.files}


def heightfield(eyes=None, flats=(), lola=None):
    """flats: (x, y, r_in, r_out) graded work areas, level at the height of their centre inside
    r_in, blending into the ground by r_out"""
    if lola is not None:
        xs, Z = _lola(lola, LOLA_EYES if eyes is None else eyes)
    else:
        xs, Z = _ground(CAMERA_EYES if eyes is None else eyes)
    X, Y = np.meshgrid(xs, xs)
    for cx, cy, r_in, r_out in flats:
        level = height_at(xs, Z, cx, cy)
        u = np.clip((np.hypot(X - cx, Y - cy) - r_in) / (r_out - r_in), 0.0, 1.0)
        w = 1 - u * u * (3 - 2 * u)
        Z = Z * (1 - w) + level * w
    return xs, Z


def _ground(eyes):
    xs = np.linspace(-SIZE / 2, SIZE / 2, N)
    X, Y = np.meshgrid(xs, xs)
    rng = np.random.default_rng(7)

    def value_noise(cells, amp):
        g = rng.normal(0, 1, (cells + 1, cells + 1))
        u = (X + SIZE / 2) / SIZE * cells; v = (Y + SIZE / 2) / SIZE * cells
        i, j = np.floor(u).astype(int).clip(0, cells - 1), np.floor(v).astype(int).clip(0, cells - 1)
        fu, fv = u - i, v - j
        fu, fv = fu * fu * (3 - 2 * fu), fv * fv * (3 - 2 * fv)
        a = g[j, i] * (1 - fu) + g[j, i + 1] * fu
        b = g[j + 1, i] * (1 - fu) + g[j + 1, i + 1] * fu
        return amp * (a * (1 - fv) + b * fv)

    Z = value_noise(6, 0.9) + value_noise(18, 0.25) + value_noise(60, 0.06)
    for _ in range(38):   # shallow impact craters of many sizes, none on the graded site
        cx, cy = rng.uniform(-125, 125, 2)
        if abs(cx) < 30 and abs(cy) < 55: continue
        if any(math.hypot(cx - e[0], cy - e[1]) < 45 for e in eyes): continue   # none in front of the lens
        r = float(np.exp(rng.uniform(np.log(2.5), np.log(16))))
        d = np.sqrt((X - cx) ** 2 + (Y - cy) ** 2) / r
        Z += -0.18 * r * np.clip(1 - d ** 2, 0, None) + 0.05 * r * np.exp(-((d - 1.0) ** 2) / 0.03)
    site = np.exp(-((X / 30) ** 2 + (Y / 55) ** 2) ** 2)
    return xs, Z * (1 - site)


def _noise(rng, X, Y, x0, size, cells, amp):
    g = rng.normal(0, 1, (cells + 1, cells + 1))
    u = (X - x0) / size * cells; v = (Y - x0) / size * cells
    i, j = np.floor(u).astype(int).clip(0, cells - 1), np.floor(v).astype(int).clip(0, cells - 1)
    fu, fv = u - i, v - j
    fu, fv = fu * fu * (3 - 2 * fu), fv * fv * (3 - 2 * fv)
    a = g[j, i] * (1 - fu) + g[j, i + 1] * fu
    b = g[j + 1, i] * (1 - fu) + g[j + 1, i + 1] * fu
    return amp * (a * (1 - fv) + b * fv)


def _far_height(lola, x, y):
    """bilinear height of the 20 m backdrop grid (arrays of points)"""
    fx, fy, F = lola["far_x"], lola["far_y"], lola["far"]
    u = (x - fx[0]) / (fx[1] - fx[0]); v = (y - fy[0]) / (fy[1] - fy[0])
    i = np.clip(np.floor(u).astype(int), 0, len(fx) - 2); j = np.clip(np.floor(v).astype(int), 0, len(fy) - 2)
    fu, fv = u - i, v - j
    return (F[j, i] * (1 - fu) * (1 - fv) + F[j, i + 1] * fu * (1 - fv)
            + F[j + 1, i] * (1 - fu) * fv + F[j + 1, i + 1] * fu * fv)


def _lola(lola, eyes):
    xs = lola["near_x"].astype(np.float64)
    Z = lola["near"].astype(np.float64).copy()
    X, Y = np.meshgrid(xs, xs)
    size = xs[-1] - xs[0]
    rng = np.random.default_rng(11)
    Z += _noise(rng, X, Y, xs[0], size, 80, 0.03) + _noise(rng, X, Y, xs[0], size, 200, 0.02)
    for _ in range(220):  # craters of 1-6 m, which 5 m pixels do not resolve
        cx, cy = rng.uniform(xs[0] + 8, xs[-1] - 8, 2)
        r = float(np.exp(rng.uniform(np.log(1.0), np.log(6.0))))
        if abs(cx) < 34 + r and abs(cy) < 64 + r: continue
        if any(math.hypot(cx - e[0], cy - e[1]) < 22 for e in eyes): continue
        d = np.sqrt((X - cx) ** 2 + (Y - cy) ** 2) / r
        Z += -0.2 * r * np.clip(1 - d ** 2, 0, None) + 0.05 * r * np.exp(-((d - 1.0) ** 2) / 0.03)
    # the last 30 m meet the backdrop: blend into its 20 m surface
    edge = np.minimum(xs[-1] - np.maximum(np.abs(X), np.abs(Y)), 30.0) / 30.0
    w = edge * edge * (3 - 2 * edge)
    return xs, Z * w + _far_height(lola, X, Y) * (1 - w)


def far_arrays(lola, hole=199.0, drop=0.2, radius=4500.0):
    """the backdrop mesh: the 20 m tile without the faces inside the near ground, a little lower,
    cut at `radius` (6 km north of Site04's rim the tile has a gap LOLA filled with a plane)"""
    fx, fy, F = lola["far_x"], lola["far_y"], lola["far"].astype(np.float64)
    nx_, ny_ = len(fx), len(fy)
    X, Y = np.meshgrid(fx, fy)
    pts = np.stack([X.ravel(), Y.ravel(), F.ravel() - drop], axis=1)
    j, i = np.meshgrid(np.arange(ny_ - 1), np.arange(nx_ - 1), indexing="ij")
    cx = (fx[i] + fx[i + 1]) / 2; cy = (fy[j] + fy[j + 1]) / 2
    keep = ~((np.abs(cx) < hole) & (np.abs(cy) < hole)) & (np.hypot(cx, cy) < radius)
    v = (j * nx_ + i)[keep]
    idx = np.stack([v, v + 1, v + nx_ + 1, v + nx_], axis=1).ravel()
    gy, gx = np.gradient(F, fy, fx)
    nrm = np.stack([-gx.ravel(), -gy.ravel(), np.ones(nx_ * ny_)], axis=1)
    nrm /= np.linalg.norm(nrm, axis=1, keepdims=True)
    return pts, [4] * int(keep.sum()), idx, nrm


def site_keepout(x, y, zones, margin=0.0):
    """the drive lanes between the zones and the graded work areas: no crater or boulder there"""
    ys = [c[1] for c in zones]
    if abs(x) < 17 + margin and min(ys) - 14 - margin < y < max(ys) + margin: return True
    return any(math.hypot(x - c[0], y - c[1]) < 22 + margin for c in zones)


def detail_arrays(xs, Z, zones, half=120.0, step=0.5, seed=5):
    """the renderer's ground near the site: the physics heights (bilinear) at 0.5 m plus 1-3 cm of
    regolith roughness and pits of 0.4-2 m (none on the lanes or work areas), which no wheel would
    feel at the rovers' scale; a mesh for the eye only"""
    n = int(round(2 * half / step)) + 1
    g = np.linspace(-half, half, n)
    X, Y = np.meshgrid(g, g)
    step0 = xs[1] - xs[0]
    u = (X - xs[0]) / step0; v = (Y - xs[0]) / step0
    i = np.clip(np.floor(u).astype(int), 0, len(xs) - 2); j = np.clip(np.floor(v).astype(int), 0, len(xs) - 2)
    fu, fv = u - i, v - j
    H = (Z[j, i] * (1 - fu) * (1 - fv) + Z[j, i + 1] * fu * (1 - fv)
         + Z[j + 1, i] * (1 - fu) * fv + Z[j + 1, i + 1] * fu * fv)
    for _ in range(3):   # round off the 1 m facets, which a grazing sun turns into shards
        P = np.pad(H, 1, mode="edge")
        H = (P[:-2, 1:-1] + P[2:, 1:-1] + P[1:-1, :-2] + P[1:-1, 2:] + 4 * P[1:-1, 1:-1]) / 8
    rng = np.random.default_rng(seed)
    D = _noise(rng, X, Y, -half, 2 * half, 240, 0.018) + _noise(rng, X, Y, -half, 2 * half, 480, 0.01)
    for _ in range(1400):
        cx, cy = rng.uniform(-half + 3, half - 3, 2)
        r = float(np.exp(rng.uniform(np.log(0.4), np.log(2.0))))
        if site_keepout(cx, cy, zones, r): continue
        i0, i1 = np.searchsorted(g, [cx - 1.5 * r, cx + 1.5 * r]); j0, j1 = np.searchsorted(g, [cy - 1.5 * r, cy + 1.5 * r])
        d = np.hypot(X[j0:j1, i0:i1] - cx, Y[j0:j1, i0:i1] - cy) / r
        D[j0:j1, i0:i1] += -0.22 * r * np.clip(1 - d ** 2, 0, None) + 0.05 * r * np.exp(-((d - 1.0) ** 2) / 0.04)
    edge = np.clip((half - np.maximum(np.abs(X), np.abs(Y))) / 10.0, 0, 1)   # meet the 1 m ground at the rim
    return g, H + edge * D + 0.02


def rock_field(xs, Z, eyes, zones, n=4000, half=150.0, seed=21):
    """boulders for the renderer: (x, y, z, size, yaw, kind), sizes 0.06-1.4 m from a power law, none
    on the lanes, the work areas or at the lens; half buried"""
    rng = np.random.default_rng(seed)
    out = []
    while len(out) < n:
        x, y = rng.uniform(-half, half, 2)
        r = 0.06 * (1 - rng.uniform()) ** (-1 / 1.4)
        if r > 1.4: continue
        if site_keepout(x, y, zones, r + 1.0): continue
        if any(math.hypot(x - e[0], y - e[1]) < 2.5 + r for e in eyes): continue
        out.append((x, y, height_at(xs, Z, x, y) - 0.35 * r, r, rng.uniform(0, 360), int(rng.integers(0, 4))))
    return out


def rock_mesh(kind, subdiv=2):
    """one boulder prototype of unit radius: an icosphere with lumps, squashed a little"""
    t = (1 + 5 ** 0.5) / 2
    v = [(-1, t, 0), (1, t, 0), (-1, -t, 0), (1, -t, 0), (0, -1, t), (0, 1, t), (0, -1, -t), (0, 1, -t),
         (t, 0, -1), (t, 0, 1), (-t, 0, -1), (-t, 0, 1)]
    f = [(0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11), (1, 5, 9), (5, 11, 4), (11, 10, 2),
         (10, 7, 6), (7, 1, 8), (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9), (4, 9, 5), (2, 4, 11),
         (6, 2, 10), (8, 6, 7), (9, 8, 1)]
    v = [np.array(p, float) / np.linalg.norm(p) for p in v]
    for _ in range(subdiv):
        cache, nf = {}, []
        def mid(a, b):
            k = (min(a, b), max(a, b))
            if k not in cache:
                m = (v[a] + v[b]) / 2; v.append(m / np.linalg.norm(m)); cache[k] = len(v) - 1
            return cache[k]
        for a, b, c in f:
            ab, bc, ca = mid(a, b), mid(b, c), mid(c, a)
            nf += [(a, ab, ca), (b, bc, ab), (c, ca, bc), (ab, bc, ca)]
        f = nf
    rng = np.random.default_rng(100 + kind)
    lumps = rng.normal(0, 1, (6, 3))
    P = np.array(v)
    k = 1 + 0.18 * np.tanh(P @ lumps.T).sum(axis=1) / 3
    P = P * k[:, None] * np.array([1.0, 0.85, 0.62])
    return P, f


def boulder_mesh(kind, w, d, h):
    """a broken boulder w x d x h (m), centred on its box: an icosphere cut flat by nine random planes
    (fracture faces) and a flat base, a little rough; drawn faceted, it reads as rock where the round
    prototypes read as pebbles. mission.py drives into it, render_replay.py draws the same one"""
    P, f = rock_mesh(kind, subdiv=3)
    P = P / np.linalg.norm(P, axis=1)[:, None]          # back to the sphere: the cuts make the shape
    rng = np.random.default_rng(300 + kind)
    for n, off in [(rng.normal(size=3), rng.uniform(0.6, 0.85)) for _ in range(9)] + [((0, 0, -1), 0.55)]:
        n = np.asarray(n, float) / np.linalg.norm(n)
        P = P - np.clip(P @ n - off, 0, None)[:, None] * n
    P = P * (1 + 0.025 * rng.normal(size=(len(P), 1)))
    lo, hi = P.min(axis=0), P.max(axis=0)
    return (P - (lo + hi) / 2) / (hi - lo) * np.array([w, d, h]), f


def work_flats(zone_centres):
    """the graded area around each zone: the pad or berm and the rovers' work spots beside it"""
    return [(c[0], c[1], 18.0, 26.0) for c in zone_centres]


def mesh_arrays(xs, Z):
    """points (n*n, 3), quad face counts and indices, vertex normals: the USD mesh of the heights"""
    n = len(xs)
    X, Y = np.meshgrid(xs, xs)
    pts = np.stack([X.ravel(), Y.ravel(), Z.ravel()], axis=1)
    v = (np.arange(n - 1)[:, None] * n + np.arange(n - 1)[None, :]).ravel()
    idx = np.stack([v, v + 1, v + n + 1, v + n], axis=1).ravel()
    gy, gx = np.gradient(Z, xs, xs)
    nrm = np.stack([-gx.ravel(), -gy.ravel(), np.ones(n * n)], axis=1)
    nrm /= np.linalg.norm(nrm, axis=1, keepdims=True)
    return pts, [4] * ((n - 1) ** 2), idx, nrm


def height_at(xs, Z, x, y):
    """bilinear height of the ground at (x, y)"""
    step = xs[1] - xs[0]
    u = (x - xs[0]) / step; v = (y - xs[0]) / step
    i = int(min(max(math.floor(u), 0), len(xs) - 2)); j = int(min(max(math.floor(v), 0), len(xs) - 2))
    fu, fv = u - i, v - j
    return float(Z[j, i] * (1 - fu) * (1 - fv) + Z[j, i + 1] * fu * (1 - fv)
                 + Z[j + 1, i] * (1 - fu) * fv + Z[j + 1, i + 1] * fu * fv)
