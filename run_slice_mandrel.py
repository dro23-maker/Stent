#!/usr/bin/env python3
"""
Headless Mesh_form slice -> Cortex-style G-code -> Hyrel mandrel Y+B -> plot.

Uses Fractal Cortex shell logic (slicing_functions) on the Z-up lattice stent,
shells-only (no solid/sparse infill — lattice struts are the path), then maps:
  A = 90°, Y = part Z (axial), B = atan2(Y, X), X fixed, Z = radius.
"""

from __future__ import annotations

import math
import re
import sys
import time
from pathlib import Path

import concurrent.futures
import matplotlib.pyplot as plt
import numpy as np
import trimesh

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import slicing_functions as sf
from hyrel_postprocessor import HyrelPostConfig, build_hyrel_footer, build_hyrel_header, _fmt


WORD_RE = re.compile(r"([A-Za-z])([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)")


def prepare_mesh(stl_path: Path, z_max: float | None = 10.0) -> trimesh.Trimesh:
    mesh = trimesh.load(stl_path, force="mesh")
    if not isinstance(mesh, trimesh.Trimesh):
        raise TypeError(f"Expected Trimesh, got {type(mesh)}")

    z0 = float(mesh.bounds[0, 2])
    mesh.apply_translation([0.0, 0.0, -z0])

    if z_max is not None and mesh.bounds[1, 2] > z_max:
        kept = mesh.slice_plane(plane_origin=[0, 0, z_max], plane_normal=[0, 0, -1], cap=True)
        if kept is not None and not kept.is_empty:
            mesh = kept

    print(
        f"Prepared mesh bounds={mesh.bounds.tolist()} extents={mesh.extents.tolist()} "
        f"faces={len(mesh.faces)}"
    )
    return mesh


def slice_shells_only(mesh: trimesh.Trimesh, layer_height: float = 0.03, shell_thickness: int = 1):
    """
    Cortex-compatible shell toolpaths without manifold/solid infill
    (avoids OOM on dense lattice strut fill).
    """
    line_width = layer_height * 2.0
    z_bottom = float(mesh.bounds[0, 2])
    z_top = float(mesh.bounds[1, 2])
    if z_bottom <= 0:
        z_extents = [0.0, z_top]
    else:
        z_extents = [z_bottom, z_top]

    z_levels = np.arange(*z_extents, step=layer_height)
    slice_levels = [round(z - (layer_height / 2), 5) for z in z_levels]
    if slice_levels:
        del slice_levels[0]

    print(f"{len(slice_levels)} layers  LH={layer_height} LW={line_width}")

    sf.mesh = mesh.copy()
    workers = max(2, min(4, getattr(sf, "workerBees", 2) or 2))

    t0 = time.time()
    args_list = [(sf.mesh, z) for z in slice_levels]
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as ex:
        mesh_sections = list(ex.map(sf.apply_slicing_function, args_list))
    print(f"Mesh sections: {time.time() - t0:.2f}s")

    from shapely.geometry import Polygon

    shapely_polygons_list = [
        [Polygon(p) for p in layer.polygons_full] if layer is not None else []
        for layer in mesh_sections
    ]
    transform3d_list = [
        layer.metadata["to_3D"] if layer is not None else np.array([])
        for layer in mesh_sections
    ]

    t0 = time.time()
    shell_args = [
        (polys, line_width, shell_thickness) for polys in shapely_polygons_list
    ]
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as ex:
        _inner, shell_rings_list_list = zip(
            *ex.map(sf.apply_get_shells_for_one_layer, shell_args)
        )
    shell_rings_list_list = list(shell_rings_list_list)
    print(f"Shells: {time.time() - t0:.2f}s")

    nonempty = sum(1 for layer in shell_rings_list_list if layer)
    print(f"Layers with shells: {nonempty} / {len(shell_rings_list_list)}")
    return transform3d_list, shell_rings_list_list, layer_height, line_width


def write_shell_gcode(
    path: Path,
    shell_rings_list_list,
    transform3d_list,
    *,
    layer_height: float,
    line_width: float,
    print_feed: float = 3600.0,
    travel_feed: float = 7200.0,
) -> None:
    """Minimal Cortex-like G-code from shell rings only."""
    e = 0.0
    nozzle_h = 0.0
    lines = [
        ";SLICER:       Fractal Cortex (shells-only headless)",
        ";FIRMWARE:     Klipper",
        ";FILE:         Mesh_form",
        f";layerHeight:         {layer_height}",
        f";lineWidth:           {line_width}",
        f";shellThickness:      1",
        "G28",
        "M82",
        "G92 E0",
        ";END OF HEADER",
        ";Chunk 0",
    ]

    for k, (rings, transform) in enumerate(zip(shell_rings_list_list, transform3d_list)):
        lines.append(f";Layer {k}")
        if not isinstance(transform, np.ndarray) or transform.shape != (4, 4):
            continue
        if not rings:
            continue
        nozzle_h = float(transform[2, 3]) + 0.5 * layer_height
        lines.append(f"G0 F{travel_feed / 5:.1f} Z{nozzle_h:.5f}")
        lines.append(";Shell(s)")
        for ring in rings:
            coords = list(ring.coords)
            if len(coords) < 2:
                continue
            for i, (x, y) in enumerate(coords):
                if i == 0:
                    lines.append(f"G0 F{travel_feed:.1f} X{x:.5f} Y{y:.5f}")
                else:
                    px, py = coords[i - 1]
                    dist = math.hypot(x - px, y - py)
                    e += (4.0 * layer_height * line_width * dist) / (math.pi * (1.75**2))
                    lines.append(
                        f"G1 F{print_feed:.1f} X{x:.5f} Y{y:.5f} E{e:.5f}"
                    )

    lines.extend(
        [
            ";FOOTER:",
            f"G0 F{travel_feed / 5:.1f} Z{nozzle_h + layer_height:.5f}",
            "M104 S0",
            "M140 S0",
            ";End of GCODE",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote Cortex shells G-code: {path} ({len(lines)} lines)")


def convert_z_up_cylinder_to_mandrel(
    cortex_lines: list[str],
    *,
    lock_a: float = 90.0,
    fixed_x: float = 0.0,
    mandrel_radius: float = 5.09,
    travel_feedrate: float = 3000.0,
    ab_feedrate: float = 600.0,
) -> list[str]:
    cfg = HyrelPostConfig(
        mandrel=True,
        mandrel_radius=mandrel_radius,
        lock_a=lock_a,
        fixed_x=fixed_x,
        initial_a=lock_a,
        travel_feedrate=travel_feedrate,
        ab_feedrate=ab_feedrate,
    )
    out = build_hyrel_header(cfg, source_name="Mesh_form_z_up_lattice")
    out.append("; Mapping: Cortex Z->Y axial, B=atan2(Y,X), A locked, X/Z held")

    state = {"x": 0.0, "y": 0.0, "z": 0.0, "e": 0.0}
    last_b: float | None = None
    in_footer = False

    for raw in cortex_lines:
        line = raw.rstrip("\n")
        upper = line.upper()
        if "FOOTER" in upper or "END OF GCODE" in upper:
            in_footer = True
            continue
        if in_footer:
            continue

        if ";" in line:
            code, comment = line.split(";", 1)
            comment = ";" + comment
        else:
            code, comment = line, ""
        stripped = code.strip()
        if not stripped:
            continue
        if stripped.startswith(";"):
            out.append(line)
            continue

        u = stripped.upper()
        if u.startswith(("M104", "M109", "M140", "M190", "M105", "M82", "M83", "G90", "G91", "G92")):
            out.append(stripped + (f" {comment.strip()}" if comment else ""))
            continue
        if u.startswith(("HOME_AB", "Z_TILT", "DIAG_", "MANUAL_STEPPER", "ACTIVATE_", "G28")):
            continue

        if u.startswith(("G0", "G1")):
            words = {m.group(1).upper(): float(m.group(2)) for m in WORD_RE.finditer(stripped)}
            cmd = "G0" if u.startswith("G0") else "G1"
            if "X" in words:
                state["x"] = words["X"]
            if "Y" in words:
                state["y"] = words["Y"]
            if "Z" in words:
                state["z"] = words["Z"]
            if "E" in words:
                state["e"] = words["E"]

            y_out = state["z"]
            b_raw = math.degrees(math.atan2(state["y"], state["x"]))
            if last_b is None:
                b_out = b_raw
            else:
                b_out = b_raw
                while b_out - last_b > 180:
                    b_out -= 360
                while b_out - last_b < -180:
                    b_out += 360
            last_b = b_out

            parts = [cmd]
            if "F" in words:
                parts.append(f"F{_fmt(words['F'])}")
            parts.append(f"X{_fmt(fixed_x)}")
            parts.append(f"Y{_fmt(y_out)}")
            parts.append(f"Z{_fmt(mandrel_radius)}")
            parts.append(f"A{_fmt(lock_a)}")
            parts.append(f"B{_fmt(b_out)}")
            if "E" in words:
                parts.append(f"E{_fmt(words['E'])}")
            out.append(" ".join(parts) + " ; Y+B mandrel (Z-up map)")
            continue

        if u.startswith("M"):
            out.append(stripped + (f" {comment.strip()}" if comment else ""))

    out.extend(build_hyrel_footer(cfg))
    return out


def plot_mandrel_path(gcode_path: Path, out_png: Path, title: str) -> None:
    lines = gcode_path.read_text(encoding="utf-8", errors="replace").splitlines()
    segs = []
    x = y = z = b = 0.0
    for ln in lines:
        if not ln.startswith(("G0", "G1")) or "Y+B mandrel" not in ln:
            continue
        words = {m.group(1).upper(): float(m.group(2)) for m in WORD_RE.finditer(ln.split(";", 1)[0])}
        if "X" in words:
            x = words["X"]
        if "Y" in words:
            y = words["Y"]
        if "Z" in words:
            z = words["Z"]
        if "B" in words:
            b = words["B"]
        extrude = ln.startswith("G1") and "E" in words and words["E"] >= 0
        radius = z if z > 0 else 5.09
        brad = math.radians(b)
        segs.append(
            (radius * math.cos(brad), y, radius * math.sin(brad), extrude, b, y)
        )

    if len(segs) < 2:
        raise RuntimeError(f"No mandrel moves found in {gcode_path}")

    # Downsample travel for readability if huge
    if len(segs) > 80000:
        step = max(1, len(segs) // 60000)
        segs = segs[::step]

    fig = plt.figure(figsize=(12, 5.8), facecolor="#0f1419")
    ax = fig.add_subplot(1, 2, 1, projection="3d", facecolor="#0f1419")

    # Draw extrude first visually denser
    for i in range(1, len(segs)):
        x0, y0, z0, _, _, _ = segs[i - 1]
        x1, y1, z1, e1, _, _ = segs[i]
        if not e1:
            continue
        ax.plot([x0, x1], [y0, y1], [z0, z1], color="#e8a54b", lw=0.9, alpha=0.85)

    for i in range(1, len(segs)):
        x0, y0, z0, _, _, _ = segs[i - 1]
        x1, y1, z1, e1, _, _ = segs[i]
        if e1:
            continue
        ax.plot([x0, x1], [y0, y1], [z0, z1], color="#4a6a8a", lw=0.35, alpha=0.25)

    r = 5.09
    theta = np.linspace(0, 2 * np.pi, 80)
    ymin = min(s[1] for s in segs) - 0.5
    ymax = max(s[1] for s in segs) + 0.5
    for yy in (ymin, ymax):
        ax.plot(
            r * np.cos(theta),
            np.full_like(theta, yy),
            r * np.sin(theta),
            color="#2a3540",
            lw=0.5,
        )
    ax.plot([0, 0], [ymin, ymax], [0, 0], color="#6b7c8a", lw=1.0, linestyle="--")
    ax.set_xlabel("X (mm)", color="#c5d0da")
    ax.set_ylabel("Y axial (mm)", color="#c5d0da")
    ax.set_zlabel("Z (mm)", color="#c5d0da")
    ax.tick_params(colors="#8a9aaa", labelsize=7)
    ax.set_title("3D mandrel pathway\nA=90°, Y axial + B rotate", color="#e8eef4")
    ax.view_init(elev=18, azim=-55)

    ax2 = fig.add_subplot(1, 2, 2, facecolor="#0f1419")
    # Unrolled: only extrude segments
    for i in range(1, len(segs)):
        if not segs[i][3]:
            continue
        ax2.plot(
            [segs[i - 1][4], segs[i][4]],
            [segs[i - 1][5], segs[i][5]],
            color="#e8a54b",
            lw=0.7,
            alpha=0.75,
        )
    ax2.set_xlabel("B rotation (°)", color="#c5d0da")
    ax2.set_ylabel("Y along mandrel (mm)", color="#c5d0da")
    ax2.set_title("Unrolled extrude path\n(lattice struts)", color="#e8eef4")
    ax2.tick_params(colors="#8a9aaa")
    for spine in ax2.spines.values():
        spine.set_color("#2a3540")
    ax2.grid(True, color="#1e2832", lw=0.7)

    n_ext = sum(1 for s in segs if s[3])
    fig.suptitle(
        f"{title}\n{len(segs)} plotted moves ({n_ext} extrude)",
        color="#e8eef4",
        fontsize=12,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.92])
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=160, facecolor=fig.get_facecolor())
    print(f"Wrote plot {out_png}")


def main() -> int:
    stl = ROOT / "samples" / "Mesh_form.stl"
    out_dir = ROOT / "samples"
    cortex_path = out_dir / "Mesh_form_cortex.gcode"
    mandrel_path = out_dir / "Mesh_form_hyrel_mandrel.gcode"
    plot_path = Path("/opt/cursor/artifacts/mesh_form_mandrel_pathway.png")
    plot_local = out_dir / "mesh_form_mandrel_pathway.png"

    mesh = prepare_mesh(stl, z_max=10.0)
    transform3d_list, shell_rings, lh, lw = slice_shells_only(
        mesh, layer_height=0.03, shell_thickness=1
    )
    if sum(1 for layer in shell_rings if layer) == 0:
        print("ERROR: empty shells", file=sys.stderr)
        return 1

    write_shell_gcode(
        cortex_path,
        shell_rings,
        transform3d_list,
        layer_height=lh,
        line_width=lw,
    )

    cortex_lines = cortex_path.read_text(encoding="utf-8", errors="replace").splitlines()
    g1 = sum(1 for ln in cortex_lines if ln.startswith("G1") and " E" in ln)
    print(f"Cortex extrude moves: {g1}")

    mandrel_lines = convert_z_up_cylinder_to_mandrel(cortex_lines)
    mandrel_path.write_text("\n".join(mandrel_lines) + "\n", encoding="utf-8")
    print(f"Wrote mandrel G-code: {mandrel_path}")

    title = "Mesh_form.stl lattice — Cortex shells → Hyrel mandrel Y+B"
    plot_mandrel_path(mandrel_path, plot_path, title)
    plot_mandrel_path(mandrel_path, plot_local, title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
