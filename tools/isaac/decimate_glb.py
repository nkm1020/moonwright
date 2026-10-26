"""Shrink a glTF model for the web: decode (Draco too), bake the scene's transforms, clean and decimate
with Open3D's quadric decimation (it copes with non-manifold scans such as NASA's RASSOR shrinkwrap,
where other decimators stall), and write a plain glb with smooth normals.

    uv run --no-project --with trimesh --with numpy --with DracoPy --with open3d \
        python decimate_glb.py rassor.glb rassor_web.glb 40000

The site (design/landing-1/assets/rassor.glb) uses 40,000 triangles: 0.8 MB instead of 6.3 MB and
2.08 million vertices; it overrides the materials, so only the shape is kept."""
import sys

import numpy as np
import open3d as o3d
import trimesh

src, dst, target = sys.argv[1], sys.argv[2], int(sys.argv[3])
scene = trimesh.load(src)
mesh = scene.to_geometry() if isinstance(scene, trimesh.Scene) else scene
m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(np.asarray(mesh.vertices, float)),
                              o3d.utility.Vector3iVector(np.asarray(mesh.faces, np.int32)))
m.merge_close_vertices(1e-5)
m.remove_duplicated_triangles()
m.remove_degenerate_triangles()
m.remove_unreferenced_vertices()
d = m.simplify_quadric_decimation(target_number_of_triangles=target)
d.remove_degenerate_triangles()
d.remove_unreferenced_vertices()
out = trimesh.Trimesh(np.asarray(d.vertices), np.asarray(d.triangles), process=False)
_ = out.vertex_normals  # computed and cached, so the glb carries smooth normals
out.export(dst, include_normals=True)
print(f"{len(mesh.vertices)} vertices, {len(mesh.faces)} triangles -> {len(out.vertices)}, {len(out.faces)}; "
      f"bounds {out.bounds.round(3).tolist()}")
