"""Lightweight tests for Hyrel / mandrel post-processing (no GUI deps)."""

from __future__ import annotations

from hyrel_postprocessor import HyrelPostConfig, convert_cortex_gcode_to_hyrel, _x_to_b_degrees


SAMPLE = """
;SLICER:       Fractal Cortex
;END OF HEADER
M104 S200
G0 F3000.0 Z5.0
G0 F3000 X0.0 Y0.0
G1 F1200 X1.0 Y0.0 E0.1
G1 F1200 X1.0 Y10.0 E0.5
G0 F3000 X0.0 Y-175.0
MANUAL_STEPPER STEPPER=stepper_a MOVE=90.0 SPEED=10.0 SYNC=0
MANUAL_STEPPER STEPPER=stepper_b MOVE=45.0 SPEED=10.0 SYNC=1 STOP_ON_ENDSTOP=2
G0 F3000 X2.0 Y0.0
G1 F1200 X2.0 Y5.0 E1.0
;FOOTER:
G28
;End of GCODE
""".strip().splitlines()


def test_x_to_b():
    # Full circumference at r=5 → 360 deg when X = 2*pi*r
    import math

    assert abs(_x_to_b_degrees(2 * math.pi * 5.0, 5.0) - 360.0) < 1e-6
    assert abs(_x_to_b_degrees(0.0, 5.0)) < 1e-9


def test_standard_trunnion_maps_manual_stepper():
    cfg = HyrelPostConfig(swap_ab=True, mandrel=False)
    out = convert_cortex_gcode_to_hyrel(SAMPLE, cfg=cfg, source_name="sample.gcode")
    text = "\n".join(out)
    assert "M229 E1 D1" in text
    assert "MANUAL_STEPPER" not in text
    assert "home_ab" not in text.lower()
    # swap: Cortex A=90 -> Hyrel B, Cortex B=45 -> Hyrel A
    assert any("A45" in line and "B90" in line for line in out) or (
        any("A45" in line for line in out) and any("B90" in line for line in out)
    )


def test_mandrel_locks_a_and_maps_x_to_b():
    cfg = HyrelPostConfig(
        swap_ab=True,
        mandrel=True,
        mandrel_radius=5.0,
        lock_a=90.0,
        fixed_x=0.0,
    )
    out = convert_cortex_gcode_to_hyrel(SAMPLE, cfg=cfg, source_name="sample.gcode")
    body = [ln for ln in out if ln.startswith(("G0", "G1")) and "Y+B mandrel" in ln]
    assert body, "expected mandrel-remapped moves"
    for ln in body:
        assert "A90" in ln.replace("A90.0", "A90") or "A90" in ln
        assert "X0" in ln or "X0.0" in ln
        # No free X motion away from fixed_x
        assert "X1" not in ln and "X2" not in ln
    # Y travel preserved on print moves
    assert any("Y10" in ln or "Y10.0" in ln for ln in body)
    # Footer present; Cortex footer dropped
    assert any("End of Hyrel" in ln for ln in out)
    assert "home_ab" not in "\n".join(out).lower()


def test_mandrel_skips_pure_z_hops():
    cfg = HyrelPostConfig(mandrel=True, mandrel_radius=5.0)
    lines = [
        ";END OF HEADER",
        "G0 F1000 Z3.0",
        "G1 F1200 X0 Y1 E0.1",
        ";FOOTER:",
    ]
    out = convert_cortex_gcode_to_hyrel(lines, cfg=cfg)
    mandrel_moves = [ln for ln in out if "Y+B mandrel" in ln]
    # Z-only hop should be skipped; Y move kept
    assert len(mandrel_moves) == 1
    assert "Y1" in mandrel_moves[0]


if __name__ == "__main__":
    test_x_to_b()
    test_standard_trunnion_maps_manual_stepper()
    test_mandrel_locks_a_and_maps_x_to_b()
    test_mandrel_skips_pure_z_hops()
    print("All hyrel_postprocessor tests passed.")
