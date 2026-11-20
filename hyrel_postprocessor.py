"""
hyrel_postprocessor.py

Convert Fractal Cortex 5-axis (Klipper / Fractal 5 Pro) G-code into
Hyrel Hydra-compatible G-code for an A/B trunnion.

Fractal Cortex emits:
  MANUAL_STEPPER STEPPER=stepper_a MOVE=<deg> ...
  MANUAL_STEPPER STEPPER=stepper_b MOVE=<deg> ...
plus Klipper macros (home_ab, Z_TILT_ADJUST, DIAG_CENTRALIZE, ...).

Hyrel expects ordinary G0/G1 with A/B words, G90, and typically:
  M229 E1 D1   ; honor slicer E values

Default axis mapping (Hyrel trunnion convention from Hydra demos):
  Cortex / Fractal A (bed spin)  -> Hyrel B
  Cortex / Fractal B (bed tilt)  -> Hyrel A
Use --no-swap-ab if your machine matches Fractal naming instead.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


MANUAL_STEPPER_RE = re.compile(
    r"MANUAL_STEPPER\s+STEPPER=stepper_([ab])\s+MOVE=([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)",
    re.IGNORECASE,
)
SPEED_RE = re.compile(
    r"SPEED=([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)",
    re.IGNORECASE,
)

# Lines / commands that are Klipper- or Fractal-specific and must not reach Hyrel.
DROP_PREFIXES = (
    "home_ab",
    "z_tilt_adjust",
    "diag_centralize",
    "manual_stepper",
    "set_gcode_variable",
    "activate_extruder",
)

KEEP_TEMP_PREFIXES = ("m104", "m109", "m140", "m190", "m105")


@dataclass
class HyrelPostConfig:
    """Runtime options for Hydra A/B export."""

    swap_ab: bool = True
    offset_x: float = 0.0
    offset_y: float = 0.0
    offset_z: float = 0.0
    park_x: float = 0.0
    park_y: float = 0.0
    clear_z: float = 50.0
    ab_feedrate: float = 600.0  # deg/min-ish; Hyrel uses F on G0/G1
    travel_feedrate: float = 3000.0
    use_e_values: bool = True
    tool_index: Optional[int] = None  # if set, emit T{n}
    apply_xy_offset_to_moves: bool = True
    initial_a: float = 0.0
    initial_b: float = 0.0
    comment_prefix: str = "; "


def _strip_comment(line: str) -> Tuple[str, str]:
    if ";" in line:
        code, comment = line.split(";", 1)
        return code.rstrip(), ";" + comment
    return line.rstrip(), ""


def _is_drop_line(code: str) -> bool:
    token = code.strip().lower()
    if not token:
        return False
    first = token.split()[0]
    return any(token.startswith(p) or first == p for p in DROP_PREFIXES)


def _map_axis(cortex_axis: str, swap_ab: bool) -> str:
    axis = cortex_axis.lower()
    if not swap_ab:
        return axis.upper()
    # Fractal A (spin) -> Hyrel B ; Fractal B (tilt) -> Hyrel A
    return "B" if axis == "a" else "A"


def _parse_manual_stepper(line: str) -> Optional[Tuple[str, float, Optional[float]]]:
    match = MANUAL_STEPPER_RE.search(line)
    if not match:
        return None
    axis = match.group(1).lower()
    move = float(match.group(2))
    speed_match = SPEED_RE.search(line)
    speed = float(speed_match.group(1)) if speed_match else None
    return axis, move, speed


def _apply_xy_offset(code: str, cfg: HyrelPostConfig) -> str:
    if not cfg.apply_xy_offset_to_moves:
        return code
    if cfg.offset_x == 0.0 and cfg.offset_y == 0.0 and cfg.offset_z == 0.0:
        return code

    def repl_x(m: re.Match) -> str:
        return f"X{float(m.group(1)) + cfg.offset_x:.5f}".rstrip("0").rstrip(".")

    def repl_y(m: re.Match) -> str:
        return f"Y{float(m.group(1)) + cfg.offset_y:.5f}".rstrip("0").rstrip(".")

    def repl_z(m: re.Match) -> str:
        return f"Z{float(m.group(1)) + cfg.offset_z:.5f}".rstrip("0").rstrip(".")

    out = re.sub(r"\bX([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)", repl_x, code)
    out = re.sub(r"\bY([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)", repl_y, out)
    if cfg.offset_z:
        out = re.sub(r"\bZ([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)", repl_z, out)
    return out


def _fmt(value: float) -> str:
    text = f"{value:.5f}".rstrip("0").rstrip(".")
    return text if text else "0"


def build_hyrel_header(cfg: HyrelPostConfig, source_name: str = "") -> List[str]:
    lines = [
        "; =========================================================",
        "; Fractal Cortex -> Hyrel Hydra A/B post-processed G-code",
        f"; Source: {source_name or 'stdin'}",
        f"; Axis map: Fractal A/B -> Hyrel "
        f"{'B/A (swapped)' if cfg.swap_ab else 'A/B (direct)'}",
        f"; XY offset applied to moves: X{cfg.offset_x} Y{cfg.offset_y}",
        f"; Config: {asdict(cfg)}",
        "; =========================================================",
        "G90 ; absolute positioning",
        "G21 ; millimeters",
    ]
    if cfg.use_e_values:
        lines.append("M229 E1 D1 ; use slicer E values on Hyrel")
        lines.append("G4 P1")
    if cfg.tool_index is not None:
        lines.append(f"T{cfg.tool_index}")
    lines.extend(
        [
            "G28 ; home X/Y (Hyrel physical home)",
            "G92 E0 ; reset extruder",
            f"G0 F{_fmt(cfg.travel_feedrate)} Z{_fmt(cfg.clear_z)} "
            f"; raise before trunnion motion",
            f"G0 F{_fmt(cfg.travel_feedrate)} X{_fmt(cfg.park_x + cfg.offset_x)} "
            f"Y{_fmt(cfg.park_y + cfg.offset_y)} ; park clear of trunnion",
            f"G1 F{_fmt(cfg.ab_feedrate)} A{_fmt(cfg.initial_a)} B{_fmt(cfg.initial_b)} "
            f"; level / start pose",
            "; --- begin converted Cortex body ---",
        ]
    )
    return lines


def build_hyrel_footer(cfg: HyrelPostConfig) -> List[str]:
    return [
        "; --- end converted Cortex body ---",
        "; FOOTER (Hyrel)",
        f"G0 F{_fmt(cfg.travel_feedrate)} Z{_fmt(cfg.clear_z)} ; clear before home/AB reset",
        "M104 S0 ; nozzle off",
        "M140 S0 ; bed off",
        f"G0 F{_fmt(cfg.travel_feedrate)} X{_fmt(cfg.park_x + cfg.offset_x)} "
        f"Y{_fmt(cfg.park_y + cfg.offset_y)}",
        f"G1 F{_fmt(cfg.ab_feedrate)} A{_fmt(cfg.initial_a)} B{_fmt(cfg.initial_b)} "
        f"; return trunnion to start pose",
        "G28 ; home X/Y",
        "; End of Hyrel G-code",
    ]


def _flush_pending_ab(
    pending: dict,
    cfg: HyrelPostConfig,
    out: List[str],
) -> None:
    """Emit one combined G1 A/B move from any buffered MANUAL_STEPPER values."""
    if not pending:
        return
    a = pending.get("A")
    b = pending.get("B")
    feed = pending.get("F", cfg.ab_feedrate)
    parts = [f"G1 F{_fmt(feed)}"]
    if a is not None:
        parts.append(f"A{_fmt(a)}")
    if b is not None:
        parts.append(f"B{_fmt(b)}")
    out.append(" ".join(parts) + " ; trunnion reorient")
    pending.clear()


def convert_cortex_gcode_to_hyrel(
    lines: Iterable[str],
    cfg: Optional[HyrelPostConfig] = None,
    source_name: str = "",
) -> List[str]:
    """
    Convert Fractal Cortex G-code lines to Hyrel Hydra A/B G-code.
    """
    cfg = cfg or HyrelPostConfig()
    out: List[str] = build_hyrel_header(cfg, source_name=source_name)

    in_header = True
    pending_ab: dict = {}
    in_cortex_footer = False

    for raw in lines:
        line = raw.rstrip("\n")
        full_upper = line.upper()
        code, comment = _strip_comment(line)
        stripped = code.strip()

        # Cortex footer: drop remaining Cortex teardown; we emit a Hyrel footer.
        if "FOOTER" in full_upper or "END OF GCODE" in full_upper:
            in_cortex_footer = True
            _flush_pending_ab(pending_ab, cfg, out)
            continue
        if in_cortex_footer:
            continue

        # Detect end of Cortex header comments / start of motion body.
        if in_header and "END OF HEADER" in full_upper:
            in_header = False
            out.append(";END OF CORTEX HEADER (temperatures retained above if present)")
            continue

        # Keep Cortex setting comments.
        if stripped.startswith(";") or (stripped == "" and line.lstrip().startswith(";")):
            out.append(line if line else ";")
            continue
        if stripped == "":
            out.append(";")
            continue

        upper = stripped.upper()

        # Temperatures from Cortex header/body are useful on Hyrel too.
        if any(upper.startswith(p.upper()) for p in KEEP_TEMP_PREFIXES):
            _flush_pending_ab(pending_ab, cfg, out)
            out.append(stripped + (f" {comment.strip()}" if comment else ""))
            in_header = False
            continue

        # Convert MANUAL_STEPPER blocks into buffered A/B targets.
        parsed = _parse_manual_stepper(stripped)
        if parsed is not None:
            cortex_axis, move, speed = parsed
            hyrel_axis = _map_axis(cortex_axis, cfg.swap_ab)
            pending_ab[hyrel_axis] = move
            if speed is not None and speed > 0:
                # Cortex SPEED is roughly deg/s style scalar used on MANUAL_STEPPER;
                # map to a conservative F in deg/min for Hyrel.
                pending_ab["F"] = max(speed * 60.0, cfg.ab_feedrate)
            continue

        if _is_drop_line(stripped):
            continue

        # Replace Fractal clear-to-park moves that use Y-175 with configured park.
        if re.search(r"\bY-?175(?:\.0+)?\b", upper) and upper.startswith("G0"):
            _flush_pending_ab(pending_ab, cfg, out)
            out.append(
                f"G0 F{_fmt(cfg.travel_feedrate)} "
                f"X{_fmt(cfg.park_x + cfg.offset_x)} "
                f"Y{_fmt(cfg.park_y + cfg.offset_y)} "
                f"; park clear of trunnion"
            )
            continue

        # Flush any pending AB before ordinary motion so reorient completes first.
        if upper.startswith(("G0", "G1", "G2", "G3")):
            _flush_pending_ab(pending_ab, cfg, out)
            converted = _apply_xy_offset(stripped, cfg)
            out.append(converted + (f" {comment.strip()}" if comment else ""))
            in_header = False
            continue

        # Absolute extrusion / unit modes from Cortex.
        if upper.startswith(("G90", "G91", "G92", "M82", "M83")):
            _flush_pending_ab(pending_ab, cfg, out)
            out.append(stripped + (f" {comment.strip()}" if comment else ""))
            continue

        # Unknown / passthrough with note for exotic commands.
        if upper.startswith("M"):
            _flush_pending_ab(pending_ab, cfg, out)
            out.append(stripped + (f" {comment.strip()}" if comment else ""))
            continue

        _flush_pending_ab(pending_ab, cfg, out)
        out.append(stripped + (f" {comment.strip()}" if comment else ""))

    _flush_pending_ab(pending_ab, cfg, out)
    out.extend(build_hyrel_footer(cfg))
    return out


def postprocess_file(
    input_path: str | Path,
    output_path: str | Path,
    cfg: Optional[HyrelPostConfig] = None,
) -> Path:
    cfg = cfg or HyrelPostConfig()
    in_path = Path(input_path)
    out_path = Path(output_path)
    text = in_path.read_text(encoding="utf-8", errors="replace").splitlines()
    converted = convert_cortex_gcode_to_hyrel(text, cfg=cfg, source_name=in_path.name)
    out_path.write_text("\n".join(converted) + "\n", encoding="utf-8")
    return out_path


def default_hyrel_output_path(cortex_path: str | Path) -> Path:
    path = Path(cortex_path)
    return path.with_name(path.stem + "_hyrel.gcode")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Post-process Fractal Cortex 5-axis G-code for Hyrel Hydra A/B trunnion."
    )
    p.add_argument("input", help="Cortex .gcode file")
    p.add_argument(
        "-o",
        "--output",
        help="Output path (default: <input>_hyrel.gcode)",
    )
    p.add_argument(
        "--no-swap-ab",
        action="store_true",
        help="Do not swap axes (keep Fractal A->Hyrel A, B->B)",
    )
    p.add_argument("--offset-x", type=float, default=0.0, help="Add to all X moves")
    p.add_argument("--offset-y", type=float, default=0.0, help="Add to all Y moves")
    p.add_argument("--offset-z", type=float, default=0.0, help="Add to all Z moves")
    p.add_argument("--park-x", type=float, default=0.0, help="Safe park X before AB moves")
    p.add_argument("--park-y", type=float, default=0.0, help="Safe park Y before AB moves")
    p.add_argument("--clear-z", type=float, default=50.0, help="Safe Z before AB / home")
    p.add_argument("--ab-feedrate", type=float, default=600.0, help="F for A/B moves")
    p.add_argument("--travel-feedrate", type=float, default=3000.0, help="F for park/clear travels")
    p.add_argument("--tool", type=int, default=None, help="Optional Hyrel tool index Tn")
    p.add_argument(
        "--no-e-values",
        action="store_true",
        help="Do not emit M229 E1 D1 (use Hyrel native flow)",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cfg = HyrelPostConfig(
        swap_ab=not args.no_swap_ab,
        offset_x=args.offset_x,
        offset_y=args.offset_y,
        offset_z=args.offset_z,
        park_x=args.park_x,
        park_y=args.park_y,
        clear_z=args.clear_z,
        ab_feedrate=args.ab_feedrate,
        travel_feedrate=args.travel_feedrate,
        use_e_values=not args.no_e_values,
        tool_index=args.tool,
    )
    output = args.output or str(default_hyrel_output_path(args.input))
    out_path = postprocess_file(args.input, output, cfg=cfg)
    print(f"Wrote Hyrel G-code: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
