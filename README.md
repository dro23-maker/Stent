# Stent — Fractal Cortex + Hyrel Hydra21 mandrel

Fractal Cortex sources customized for **tracheal stent** printing on a **Hyrel Hydra21** A/B trunnion with a rotating mandrel.

## Goal kinematics

| Axis | Behavior |
|------|----------|
| **A** | Fixed at **90°** (trunnion tipped; mandrel axis along machine **Y**) |
| **Y** | Nozzle travels forward / back along the mandrel |
| **B** | Mandrel rotates continuously while printing |
| **X / Z** | Held during print passes (no planar bottom→top stacking) |

Stock Fractal Cortex still slices into multidirectional planar chunks. For Hydra / Repetrel, convert the Cortex G-code with the mandrel post-processor so motion becomes **Y + B** with **A locked at 90°**.

## Workflow

1. Slice the stent STL in Fractal Cortex (`slicer_main.py`) with thin-wall-friendly settings:
   - Layer height ≤ **0.05 mm** (line width = 2× layer height)
   - Shells = **1**, infill **100%** (or thicken the mesh wall first)
   - Keep slicing directions that clear the bed (Z clearance > 12 mm for tilted planes)
2. Export Cortex `.gcode`.
3. Post-process for Hyrel / Repetrel:

```bash
python hyrel_postprocessor.py Toolpath.gcode --mandrel \
  --mandrel-radius 5.09 \
  --lock-a 90 \
  --fixed-x 0
```

Output: `Toolpath_hyrel_mandrel.gcode`

### What `--mandrel` does

- Locks **A = 90°**
- Keeps **Y** as axial travel along the mandrel
- Maps Cortex **X → B** via `B = B₀ + degrees(X / radius)`
- Holds **X** at `--fixed-x` and **Z** at `--mandrel-radius` (+ `--offset-z`)
- Strips Klipper-only macros (`home_ab`, `MANUAL_STEPPER`, `Z_TILT_ADJUST`, …)
- Emits Repetrel-friendly `G0`/`G1` with `A`/`B` words and `M229 E1 D1`

### Useful flags

| Flag | Meaning |
|------|---------|
| `--mandrel-radius R` | Stent / mandrel outer radius (mm). Default `5.09` ≈ Mesh_form Ø10.18 / 2 |
| `--lock-a DEG` | Fixed A tip angle (default 90) |
| `--fixed-x X` | Nozzle X line while printing (default 0) |
| `--use-cortex-z-as-radius` | Use Cortex Z as print radius instead of fixed radius |
| `--no-swap-ab` | Keep Fractal A→Hyrel A, B→B (default swaps A↔B for Hydra demos) |
| `--offset-x/y/z` | Machine offsets |
| `--park-x/y`, `--clear-z` | Safe park / clearance |

Without `--mandrel`, the post-processor only remaps Cortex `MANUAL_STEPPER` A/B into Hyrel `G1 A… B…` (planar trunnion mode).

## Crash fixes in this fork

`slicing_functions.py` now:

- Skips **None** mesh sections in bed/nozzle collision checks (no `NoneType.to_3D`)
- Handles **empty shell layers** when transforming paths (no `printable_coords_3d` UnboundLocalError)
- Initializes **`nozzleHeight`** before writing G-code so empty exports still save a footer

## Files

| File | Role |
|------|------|
| `slicer_main.py` | Fractal Cortex UI entry |
| `slicing_functions.py` | 3/5-axis slicing + G-code writers |
| `widget_functions.py` / `fractal_widgets.py` | UI widgets |
| `hyrel_postprocessor.py` | Cortex → Hyrel / Repetrel (+ `--mandrel`) |

## Recommended Cortex settings for Mesh_form

- Translate Z so tilted planes stay valid (often **Z ≈ 30** after load; Cortex drops Z-min to the bed)
- Shells **1**, LH **0.05**, infill **100%**
- Prefer fewer directions once a usable toolpath exists; post with `--mandrel` for live B rotation
