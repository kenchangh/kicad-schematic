#!/usr/bin/env python3
"""
KiCad Schematic Helper Library

Provides reliable, computed coordinate transforms and S-expression generation
for KiCad 8 .kicad_sch files. Eliminates the #1 source of ERC errors:
misaligned labels/wires due to guessed pin positions.

Usage:
    from kicad_sch_helpers import SchematicBuilder, SymbolLibrary

The key insight: KiCad symbol libraries use Y-up (math convention),
but .kicad_sch files use Y-down (screen convention). When placing a
symbol at (sx, sy), a pin defined at library position (px, py) maps
to schematic position using:

    Rotation 0:   (sx + px, sy - py)
    Rotation 90:  (sx + py, sy + px)
    Rotation 180: (sx - px, sy + py)
    Rotation 270: (sx - py, sy - px)

This library handles all of this automatically.
"""

import uuid as _uuid
import re
import json
import math
import subprocess
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


# =============================================================================
# Grid and coordinate utilities
# =============================================================================

GRID = 1.27  # KiCad default schematic grid in mm (50 mil)


def snap(v: float) -> float:
    """Snap a coordinate to the nearest 1.27mm grid point."""
    return round(v / GRID) * GRID


def uid() -> str:
    """Generate a UUID for KiCad elements."""
    return str(_uuid.uuid4())


def pin_transform(pin_x: float, pin_y: float, rotation: int = 0) -> tuple:
    """
    Transform a pin position from library space to schematic offset space.

    In library space: Y-up (positive Y = up on screen)
    In schematic space: Y-down (positive Y = down on screen)

    Args:
        pin_x, pin_y: Pin position in library (symbol) coordinates
        rotation: Symbol rotation in degrees (0, 90, 180, 270)

    Returns:
        (dx, dy): Offset to add to symbol placement position
    """
    transforms = {
        0:   ( pin_x, -pin_y),
        90:  ( pin_y,  pin_x),
        180: (-pin_x,  pin_y),
        270: (-pin_y, -pin_x),
    }
    if rotation not in transforms:
        raise ValueError(f"Rotation must be 0, 90, 180, or 270. Got {rotation}")
    return transforms[rotation]


def pin_absolute(sym_x: float, sym_y: float, pin_x: float, pin_y: float,
                 rotation: int = 0, mirror_y: bool = False) -> tuple:
    """
    Compute the absolute schematic position of a pin.

    Args:
        sym_x, sym_y: Symbol placement position in schematic
        pin_x, pin_y: Pin position in library coordinates
        rotation: Symbol rotation (0, 90, 180, 270)
        mirror_y: Whether the symbol is Y-mirrored

    Returns:
        (abs_x, abs_y): Absolute pin position in schematic coordinates
    """
    if mirror_y:
        pin_x = -pin_x
    dx, dy = pin_transform(pin_x, pin_y, rotation)
    return (sym_x + dx, sym_y + dy)


# =============================================================================
# Symbol library parser
# =============================================================================

@dataclass
class PinDef:
    """A pin definition from a symbol library."""
    name: str
    number: str
    x: float
    y: float
    angle: int
    length: float
    pin_type: str  # passive, power_in, power_out, input, output, bidirectional


@dataclass
class SymbolDef:
    """A symbol definition with its pins."""
    name: str
    pins: list  # List[PinDef]

    def get_pin_by_name(self, name: str) -> Optional[PinDef]:
        for p in self.pins:
            if p.name == name:
                return p
        return None

    def get_pin_by_number(self, number: str) -> Optional[PinDef]:
        for p in self.pins:
            if p.number == number:
                return p
        return None


class SymbolLibrary:
    """
    Parse and store symbol definitions from .kicad_sym files
    or from the lib_symbols section of a .kicad_sch file.
    """

    def __init__(self):
        self.symbols: dict = {}  # name -> SymbolDef

    def load_from_kicad_sym(self, filepath: str):
        """Load symbols from a .kicad_sym library file."""
        with open(filepath) as f:
            content = f.read()
        self._parse_symbols(content)

    def _parse_symbols(self, content: str):
        """Parse symbol definitions from S-expression content."""
        # Find each top-level (symbol "name" ...) block
        pin_pattern = re.compile(
            r'\(pin\s+(\w+)\s+\w+\s+'
            r'\(at\s+([-\d.]+)\s+([-\d.]+)\s+(\d+)\)\s+'
            r'\(length\s+([-\d.]+)\)\s+'
            r'\(name\s+"([^"]*)".*?\)\s+'
            r'\(number\s+"([^"]*)".*?\)\)',
            re.DOTALL
        )

        # Simple block extraction by tracking parens
        lines = content.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            # Look for top-level symbol definitions (not sub-symbols with _N_N)
            m = re.match(r'^\(symbol\s+"([^"]+)"\s*$', line)
            if not m:
                m = re.match(r'^\(symbol\s+"([^"]+)"', line)

            if m and not re.search(r'_\d+_\d+$', m.group(1)):
                sym_name = m.group(1)
                # Collect all lines of this symbol block
                depth = line.count('(') - line.count(')')
                block_lines = [line]
                j = i + 1
                while j < len(lines) and depth > 0:
                    l = lines[j].strip()
                    depth += l.count('(') - l.count(')')
                    block_lines.append(l)
                    j += 1

                block = '\n'.join(block_lines)
                pins = []
                for pm in pin_pattern.finditer(block):
                    pins.append(PinDef(
                        name=pm.group(6),
                        number=pm.group(7),
                        x=float(pm.group(2)),
                        y=float(pm.group(3)),
                        angle=int(pm.group(4)),
                        length=float(pm.group(5)),
                        pin_type=pm.group(1),
                    ))

                if pins:
                    self.symbols[sym_name] = SymbolDef(name=sym_name, pins=pins)
                i = j
            else:
                i += 1

    def get(self, name: str) -> Optional[SymbolDef]:
        """Get a symbol definition by name (tries with and without prefix)."""
        if name in self.symbols:
            return self.symbols[name]
        # Try stripping library prefix
        if ':' in name:
            short = name.split(':', 1)[1]
            if short in self.symbols:
                return self.symbols[short]
        return None


# =============================================================================
# Schematic builder with computed connectivity
# =============================================================================

@dataclass
class PlacedComponent:
    """A component placed in the schematic."""
    lib_id: str
    ref: str
    value: str
    x: float
    y: float
    rotation: int
    footprint: str
    lcsc: str
    mirror_y: bool
    unit: int
    uuid: str


class SchematicBuilder:
    """
    Build a KiCad 8 schematic with guaranteed pin-label connectivity.

    Key method: connect_pin() computes the exact pin position and adds
    a wire stub + label, ensuring ERC-clean connections.
    """

    def __init__(self, symbol_lib: SymbolLibrary, project_name: str = "project"):
        self.symbol_lib = symbol_lib
        self.project_name = project_name
        self.root_uuid = uid()
        self.components: list = []
        self.placed: dict = {}  # ref -> PlacedComponent
        self.wires: list = []
        self.labels: list = []
        self.no_connects: list = []
        self.text_notes: list = []
        self.pwr_sym_counter = 0
        self._lib_symbols_content = ""

    def set_lib_symbols(self, content: str):
        """Set the raw lib_symbols content for the schematic."""
        self._lib_symbols_content = content

    def place(self, lib_id: str, ref: str, value: str, x: float, y: float,
              rotation: int = 0, footprint: str = "", lcsc: str = "",
              mirror_y: bool = False, unit: int = 1) -> PlacedComponent:
        """Place a component symbol at grid-snapped coordinates."""
        x, y = snap(x), snap(y)
        u = uid()

        mirror_str = "(mirror y)" if mirror_y else ""

        self.components.append(f"""  (symbol (lib_id "{lib_id}") (at {x:.2f} {y:.2f} {rotation}) {mirror_str}
    (uuid "{u}")
    (property "Reference" "{ref}" (at {x:.2f} {y - 3.81:.2f} 0)
      (effects (font (size 1.27 1.27))))
    (property "Value" "{value}" (at {x:.2f} {y + 3.81:.2f} 0)
      (effects (font (size 1.0 1.0))))
    (property "Footprint" "{footprint}" (at {x:.2f} {y + 5.08:.2f} 0)
      (effects (font (size 1.27 1.27)) hide))
    (property "LCSC" "{lcsc}" (at {x:.2f} {y + 6.35:.2f} 0)
      (effects (font (size 1.27 1.27)) hide))
    (instances
      (project "{self.project_name}"
        (path "/{self.root_uuid}"
          (reference "{ref}")
          (unit {unit})
        )
      )
    )
  )""")

        comp = PlacedComponent(
            lib_id=lib_id, ref=ref, value=value,
            x=x, y=y, rotation=rotation,
            footprint=footprint, lcsc=lcsc,
            mirror_y=mirror_y, unit=unit, uuid=u
        )
        self.placed[ref] = comp
        return comp

    def place_power(self, lib_id: str, value: str, x: float, y: float,
                    rotation: int = 0) -> str:
        """Place a power symbol (GND, VCC, etc.)."""
        x, y = snap(x), snap(y)
        self.pwr_sym_counter += 1
        ref = f"#PWR{self.pwr_sym_counter:03d}"
        u = uid()
        self.components.append(f"""  (symbol (lib_id "{lib_id}") (at {x:.2f} {y:.2f} {rotation})
    (uuid "{u}")
    (property "Reference" "{ref}" (at {x:.2f} {y + 2.54:.2f} 0)
      (effects (font (size 1.27 1.27)) hide))
    (property "Value" "{value}" (at {x:.2f} {y + 3.81:.2f} 0)
      (effects (font (size 0.8 0.8))))
    (property "Footprint" "" (at {x:.2f} {y:.2f} 0)
      (effects (font (size 1.27 1.27)) hide))
    (instances
      (project "{self.project_name}"
        (path "/{self.root_uuid}"
          (reference "{ref}")
          (unit 1)
        )
      )
    )
  )""")
        return ref

    def connect_pin(self, ref: str, pin_name: str, net_label: str,
                    wire_dx: float = 0, wire_dy: float = 0,
                    label_angle: int = 0, by_number: bool = False):
        """
        Connect a component's pin to a net label with a wire stub.
        This is THE key method that eliminates dangling label and
        unconnected pin ERC errors.

        Args:
            ref: Component reference (e.g., "U1")
            pin_name: Pin name or number to connect
            net_label: Net label text (e.g., "VCC_3V3")
            wire_dx, wire_dy: Wire extension from pin (for routing space)
            label_angle: Label rotation (0, 90, 180, 270)
            by_number: If True, look up pin by number instead of name
        """
        comp = self.placed.get(ref)
        if not comp:
            print(f"WARNING: Component {ref} not found", file=sys.stderr)
            return

        sym_def = self.symbol_lib.get(comp.lib_id)
        if not sym_def:
            print(f"WARNING: Symbol {comp.lib_id} not in library", file=sys.stderr)
            return

        pin = (sym_def.get_pin_by_number(pin_name) if by_number
               else sym_def.get_pin_by_name(pin_name))
        if not pin:
            print(f"WARNING: Pin '{pin_name}' not found on {comp.lib_id}", file=sys.stderr)
            return

        # Compute absolute pin position
        abs_x, abs_y = pin_absolute(comp.x, comp.y, pin.x, pin.y,
                                     comp.rotation, comp.mirror_y)

        # Snap to grid
        abs_x, abs_y = snap(abs_x), snap(abs_y)
        end_x, end_y = snap(abs_x + wire_dx), snap(abs_y + wire_dy)

        # Add wire from pin to label position
        if wire_dx != 0 or wire_dy != 0:
            self.wire(abs_x, abs_y, end_x, end_y)
            self.label(net_label, end_x, end_y, label_angle)
        else:
            self.label(net_label, abs_x, abs_y, label_angle)

    def connect_pin_noconnect(self, ref: str, pin_name: str,
                               by_number: bool = False):
        """Place a no-connect flag on a pin."""
        comp = self.placed.get(ref)
        if not comp:
            return
        sym_def = self.symbol_lib.get(comp.lib_id)
        if not sym_def:
            return
        pin = (sym_def.get_pin_by_number(pin_name) if by_number
               else sym_def.get_pin_by_name(pin_name))
        if not pin:
            return
        abs_x, abs_y = pin_absolute(comp.x, comp.y, pin.x, pin.y,
                                     comp.rotation, comp.mirror_y)
        self.no_connect(snap(abs_x), snap(abs_y))

    def wire(self, x1: float, y1: float, x2: float, y2: float):
        """Draw a wire between two points (auto-snapped to grid)."""
        x1, y1, x2, y2 = snap(x1), snap(y1), snap(x2), snap(y2)
        self.wires.append(f"""  (wire (pts (xy {x1:.2f} {y1:.2f}) (xy {x2:.2f} {y2:.2f}))
    (stroke (width 0) (type default))
    (uuid "{uid()}")
  )""")

    def label(self, name: str, x: float, y: float, angle: int = 0):
        """Place a net label at grid-snapped coordinates."""
        x, y = snap(x), snap(y)
        self.labels.append(f"""  (label "{name}" (at {x:.2f} {y:.2f} {angle})
    (effects (font (size 1.27 1.27)) (justify left))
    (uuid "{uid()}")
  )""")

    def no_connect(self, x: float, y: float):
        """Place a no-connect flag."""
        x, y = snap(x), snap(y)
        self.no_connects.append(f"""  (no_connect (at {x:.2f} {y:.2f})
    (uuid "{uid()}")
  )""")

    def text_note(self, text: str, x: float, y: float, size: float = 2.54):
        """Add a text annotation."""
        self.text_notes.append(f"""  (text "{text}" (at {x:.2f} {y:.2f} 0)
    (effects (font (size {size} {size})) (justify left))
    (uuid "{uid()}")
  )""")

    def build(self, title: str = "Schematic", date: str = "2026-01-01",
              rev: str = "1.0", paper: str = "A1",
              comments: list = None) -> str:
        """Generate the complete .kicad_sch file content."""
        comment_lines = ""
        if comments:
            for i, c in enumerate(comments, 1):
                comment_lines += f'    (comment {i} "{c}")\n'

        header = f"""(kicad_sch
  (version 20231120)
  (generator "kicad_sch_agent")
  (generator_version "8.0")
  (uuid "{self.root_uuid}")
  (paper "{paper}")
  (title_block
    (title "{title}")
    (date "{date}")
    (rev "{rev}")
{comment_lines}  )"""

        lib_sym_section = f"""  (lib_symbols
{self._lib_symbols_content}
  )"""

        all_items = (self.components + self.wires + self.labels +
                     self.no_connects + self.text_notes)
        items_str = '\n'.join(all_items)

        return f"""{header}

{lib_sym_section}

{items_str}

  (sheet_instances
    (path "/"
      (page "1")
    )
  )
)"""


# =============================================================================
# Sub-symbol name fixer
# =============================================================================

def fix_subsymbol_names(content: str) -> str:
    """
    Fix sub-symbol names in lib_symbols section.
    KiCad requires sub-symbols to NOT have library prefix in their names.
    e.g., "Device:R_0_1" must be "R_0_1"
    """
    def fix_match(m):
        full_name = m.group(1)
        suffix = m.group(2)
        if ':' in full_name:
            name = full_name.split(':', 1)[1]
            return f'(symbol "{name}{suffix}"'
        return m.group(0)

    return re.sub(r'\(symbol "([^"]+?)(_\d+_\d+)"', fix_match, content)


# =============================================================================
# ERC validation loop
# =============================================================================

def run_erc(schematic_path: str, output_path: str = None,
            kicad_cli: str = "kicad-cli") -> dict:
    """
    Run KiCad ERC check and return structured results.

    Args:
        schematic_path: Path to .kicad_sch file
        output_path: Where to save the ERC report (default: alongside schematic)
        kicad_cli: Path to kicad-cli binary

    Returns:
        dict with keys: success, errors, warnings, total, details
    """
    if output_path is None:
        output_path = str(Path(schematic_path).with_suffix('.erc.json'))

    try:
        result = subprocess.run(
            [kicad_cli, "sch", "erc",
             "--output", output_path,
             "--format", "json",
             "--severity-all",
             schematic_path],
            capture_output=True, text=True, timeout=60
        )
    except FileNotFoundError:
        # kicad-cli not on PATH — try to locate it
        found = find_kicad_cli()
        if found:
            print(f"WARNING: kicad-cli not on PATH but found at: {found}",
                  file=sys.stderr)
            suggest_kicad_cli_symlink()
            # Retry with the discovered path
            try:
                result = subprocess.run(
                    [found, "sch", "erc",
                     "--output", output_path,
                     "--format", "json",
                     "--severity-all",
                     schematic_path],
                    capture_output=True, text=True, timeout=60
                )
            except Exception as e:
                return {"success": False, "errors": -1, "warnings": -1,
                        "total": -1, "details": [],
                        "raw": f"kicad-cli found at {found} but failed: {e}"}
        else:
            suggest_kicad_cli_symlink()
            return {"success": False, "errors": -1, "warnings": -1,
                    "total": -1, "details": [], "raw": "kicad-cli not found"}
    except subprocess.TimeoutExpired:
        return {"success": False, "errors": -1, "warnings": -1,
                "total": -1, "details": [], "raw": "timeout"}

    # Parse JSON report
    try:
        with open(output_path) as f:
            report = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        # Parse text output as fallback
        return _parse_text_erc(result.stdout + result.stderr)

    # Categorize
    errors = [v for v in report.get("violations", [])
              if v.get("severity") == "error"]
    warnings = [v for v in report.get("violations", [])
                if v.get("severity") == "warning"]

    return {
        "success": len(errors) == 0,
        "errors": len(errors),
        "warnings": len(warnings),
        "total": len(errors) + len(warnings),
        "details": report.get("violations", []),
        "error_types": _categorize_errors(errors),
        "warning_types": _categorize_errors(warnings),
    }


def _categorize_errors(violations: list) -> dict:
    """Group violations by type."""
    cats = {}
    for v in violations:
        t = v.get("type", "unknown")
        cats[t] = cats.get(t, 0) + 1
    return cats


def _parse_text_erc(text: str) -> dict:
    """Fallback parser for text-format ERC reports."""
    import re
    errors = len(re.findall(r';\s*error', text))
    warnings = len(re.findall(r';\s*warning', text))
    return {
        "success": errors == 0,
        "errors": errors,
        "warnings": warnings,
        "total": errors + warnings,
        "details": [],
        "raw": text
    }


def validate_and_fix_loop(schematic_path: str, fix_callback,
                           max_iterations: int = 5,
                           kicad_cli: str = "kicad-cli") -> dict:
    """
    Automated generate → validate → fix loop.

    Args:
        schematic_path: Path to the schematic file
        fix_callback: Function(erc_result, iteration) -> bool
                      Returns True if fixes were applied, False to stop
        max_iterations: Maximum fix attempts
        kicad_cli: Path to kicad-cli

    Returns:
        Final ERC result dict
    """
    for i in range(max_iterations):
        print(f"\n=== ERC Validation Iteration {i+1}/{max_iterations} ===")
        result = run_erc(schematic_path, kicad_cli=kicad_cli)
        print(f"Errors: {result['errors']}, Warnings: {result['warnings']}")

        if result["errors"] == 0:
            print("✓ No ERC errors!")
            return result

        if not fix_callback(result, i):
            print("Fix callback returned False, stopping.")
            return result

    print(f"Reached max iterations ({max_iterations})")
    return result


# =============================================================================
# Convenience: 2-pin component helpers
# =============================================================================

def place_2pin_vertical(builder: SchematicBuilder, lib_id: str, ref: str,
                        value: str, x: float, y: float,
                        top_net: str, bottom_net: str,
                        footprint: str = "", lcsc: str = "",
                        wire_ext: float = 3.81):
    """
    Place a 2-pin component vertically and wire both pins to net labels.
    Pin 1 (top) connects to top_net, Pin 2 (bottom) to bottom_net.
    Adds wire stubs extending wire_ext mm from each pin.
    """
    x, y = snap(x), snap(y)
    builder.place(lib_id, ref, value, x, y, rotation=0,
                  footprint=footprint, lcsc=lcsc)
    # Pin 1 at (0, 2.54) in library = (0, -2.54) in schematic
    pin1_y = y - 2.54
    pin2_y = y + 2.54
    builder.wire(x, pin1_y, x, pin1_y - wire_ext)
    builder.label(top_net, x, pin1_y - wire_ext)
    builder.wire(x, pin2_y, x, pin2_y + wire_ext)
    builder.label(bottom_net, x, pin2_y + wire_ext)


def place_2pin_horizontal(builder: SchematicBuilder, lib_id: str, ref: str,
                          value: str, x: float, y: float,
                          left_net: str, right_net: str,
                          footprint: str = "", lcsc: str = "",
                          wire_ext: float = 3.81):
    """
    Place a 2-pin component horizontally (rotation=90) and wire both pins.
    Pin 1 connects to right_net, Pin 2 to left_net.
    """
    x, y = snap(x), snap(y)
    builder.place(lib_id, ref, value, x, y, rotation=90,
                  footprint=footprint, lcsc=lcsc)
    # Rotation 90: pin1 at library (0,2.54) -> schematic (2.54, 0)
    pin1_x = x + 2.54
    pin2_x = x - 2.54
    builder.wire(pin1_x, y, pin1_x + wire_ext, y)
    builder.label(right_net, pin1_x + wire_ext, y)
    builder.wire(pin2_x, y, pin2_x - wire_ext, y)
    builder.label(left_net, pin2_x - wire_ext, y)


# =============================================================================
# kicad-cli discovery and symlink helper
# =============================================================================

def find_kicad_cli() -> Optional[str]:
    """
    Locate kicad-cli on the system. Returns the path if found, None otherwise.
    Checks PATH first, then common installation directories per OS.
    """
    import shutil
    import platform

    # Check if already on PATH
    found = shutil.which("kicad-cli")
    if found:
        return found

    system = platform.system()

    candidates = []
    if system == "Darwin":
        candidates = [
            "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
            "/Applications/KiCad 8.0/KiCad.app/Contents/MacOS/kicad-cli",
            Path.home() / "Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
        ]
    elif system == "Linux":
        candidates = [
            "/usr/bin/kicad-cli",
            "/usr/local/bin/kicad-cli",
            "/snap/kicad/current/bin/kicad-cli",
            Path.home() / ".local/bin/kicad-cli",
        ]
    elif system == "Windows":
        candidates = [
            Path(r"C:\Program Files\KiCad\8.0\bin\kicad-cli.exe"),
            Path(r"C:\Program Files\KiCad\bin\kicad-cli.exe"),
            Path(r"C:\Program Files (x86)\KiCad\8.0\bin\kicad-cli.exe"),
        ]

    for candidate in candidates:
        if Path(candidate).is_file():
            return str(candidate)

    return None


def suggest_kicad_cli_symlink() -> Optional[str]:
    """
    Find kicad-cli and print instructions to make it available on PATH.
    Returns the found path, or None if not installed.
    """
    import platform

    found = find_kicad_cli()
    if not found:
        system = platform.system()
        urls = {
            "Darwin": "https://www.kicad.org/download/macos/",
            "Linux": "https://www.kicad.org/download/linux/",
            "Windows": "https://www.kicad.org/download/windows/",
        }
        url = urls.get(system, "https://www.kicad.org/download/")
        print(f"kicad-cli not found. Install KiCad 8 from: {url}", file=sys.stderr)
        return None

    import shutil
    if shutil.which("kicad-cli"):
        # Already on PATH
        return found

    system = platform.system()
    if system in ("Darwin", "Linux"):
        print(f"Found kicad-cli at: {found}", file=sys.stderr)
        print(f"To add to PATH, run:", file=sys.stderr)
        print(f"  sudo ln -sf '{found}' /usr/local/bin/kicad-cli", file=sys.stderr)
    elif system == "Windows":
        bin_dir = str(Path(found).parent)
        print(f"Found kicad-cli at: {found}", file=sys.stderr)
        print(f"To add to PATH, run in PowerShell (as admin):", file=sys.stderr)
        print(f'  [Environment]::SetEnvironmentVariable("PATH", $env:PATH + ";{bin_dir}", "User")',
              file=sys.stderr)

    return found


if __name__ == "__main__":
    print("KiCad Schematic Helper Library")
    print(f"Grid: {GRID} mm")
    print(f"snap(42.5) = {snap(42.5)}")
    print(f"snap(50.0) = {snap(50.0)}")
    print(f"pin_absolute(320, 200, -17.78, 25.40, rotation=0) = "
          f"{pin_absolute(320, 200, -17.78, 25.40, rotation=0)}")
    print()
    print("Checking kicad-cli availability...")
    cli_path = find_kicad_cli()
    if cli_path:
        print(f"kicad-cli found: {cli_path}")
    else:
        suggest_kicad_cli_symlink()
