"""Cut the mission's ground out of a NASA LOLA 5 m/px south-pole DEM (run once, on any machine).

    curl -L -o Site04_surf.tif https://pgda.gsfc.nasa.gov/data/LOLA_5mpp/Site04/Site04_final_adj_5mpp_surf.tif
    uv run --no-project --with tifffile --with scipy --with numpy python prep_lola.py Site04_surf.tif 1238 1502 lola_site.npz

Site04 is the rim of Shackleton crater; pixel (1238, 1502) is the centre of the gentlest 120 x 40 m
north-south strip within 1.5 km of the drop into the crater (mean slope 1.8 deg, max 5.2 deg at 5 m).
Writes, heights in metres relative to the site centre, x along the image columns, y against the rows:
  near   401 x 401 at 1 m (+-200 m), cubic from the DEM: the physics ground (lunar_terrain.py adds the
         craters smaller than the DEM resolves)
  far    the whole tile at 20 m, lowered by the Moon's curvature: the backdrop the renderer draws
  sun    the azimuths (deg, counter-clockwise from +x) where the sun at --sun-el is above the local
         horizon seen from the site
Data: Barker et al. 2021, Planetary & Space Science 203, 105119 (LOLA, NASA PGDA)."""
import math
import sys

import numpy as np
import tifffile
from scipy.ndimage import map_coordinates

src, ci, cj, out = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
SUN_EL = 1.5          # deg: the sun near 89.8 deg S never climbs above about 1.8 deg
PX, R_MOON = 5.0, 1737.4e3

with tifffile.TiffFile(src) as tf:
    Z = tf.pages[0].asarray().astype(np.float64)
    tie = tf.pages[0].tags["ModelTiepointTag"].value
z0 = Z[cj, ci]
X0, Y0 = tie[3] + PX * ci, tie[4] - PX * cj            # polar stereographic metres of the site
lat = -90 + math.degrees(2 * math.atan(math.hypot(X0, Y0) / (2 * R_MOON)))

# near: 1 m, +-200 m, cubic
n = np.arange(-200, 201, 1.0)
XX, YY = np.meshgrid(n, n)
cols, rows = ci + XX / PX, cj - YY / PX
near = map_coordinates(Z, [rows.ravel(), cols.ravel()], order=3, mode="nearest").reshape(XX.shape) - z0

# far: the whole tile at 20 m, curvature drop r^2 / 2R
step = 4
far = Z[::step, ::step] - z0
fc = (np.arange(far.shape[1]) * step - ci) * PX          # x of each column
fr = -(np.arange(far.shape[0]) * step - cj) * PX         # y of each row
FX, FY = np.meshgrid(fc, fr)
far = far - (FX ** 2 + FY ** 2) / (2 * R_MOON)
far_x = fc.astype(np.float32); far_y = fr[::-1].astype(np.float32)
far = far[::-1, :]                                          # rows ascending in y

# the local horizon from 2 m above the site, every 2 deg, out to the tile's edge
lit = []
for az in range(0, 360, 2):
    a = math.radians(az)
    best = -90.0
    for d in np.arange(10.0, 9000.0, 5.0):
        c, r = ci + d * math.cos(a) / PX, cj - d * math.sin(a) / PX
        if not (0 <= c < Z.shape[1] - 1 and 0 <= r < Z.shape[0] - 1): break
        h = map_coordinates(Z, [[r], [c]], order=1)[0] - z0 - 2.0 - d * d / (2 * R_MOON)
        best = max(best, math.degrees(math.atan2(h, d)))
    if best < SUN_EL - 0.3: lit.append((az, round(best, 2)))

# where Shackleton's interior is: the mean direction of the ground 500 m below the site within 3 km
rr, cc = np.nonzero(Z[cj - 600:cj + 600, ci - 600:ci + 600] < z0 - 500)
deep = math.degrees(math.atan2(-(rr.mean() - 600), cc.mean() - 600)) if len(rr) else None

np.savez_compressed(out, near=near.astype(np.float32), near_x=n.astype(np.float32),
                    far=far.astype(np.float32), far_x=far_x, far_y=far_y,
                    sun_az=np.array([a for a, _ in lit], dtype=np.float32), sun_el=np.float32(SUN_EL),
                    crater_az=np.float32(deep if deep is not None else np.nan),
                    meta=np.array([f"LOLA {src.split('/')[-1]} px ({ci},{cj}) stereo ({X0:.0f},{Y0:.0f}) m "
                                   f"lat {lat:.3f} z {z0:.1f} m"]))
print(f"site lat {lat:.3f}, z {z0:.0f} m, near relief {near.min():.1f}..{near.max():.1f} m, "
      f"far relief {far.min():.0f}..{far.max():.0f} m")
print(f"Shackleton's interior lies toward {deep:.0f} deg; sun at {SUN_EL} deg clears the horizon at "
      f"{len(lit)} of 180 azimuths: {[a for a, _ in lit]}")
