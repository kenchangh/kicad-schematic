#!/usr/bin/env python3
"""
KiCad Schematic Helper Library — v2 (battle-tested)

Provides reliable, computed coordinate transforms and S-expression generation
for KiCad 8 .kicad_sch files. Eliminates the #1 source of ERC errors:
misaligned labels/wires due to guessed pin positions.

LESSONS LEARNED from real-world debugging:
1. NEVER guess pin positions — always compute from symbol definitions
2. ALL coordinates must be snapped to 1.27mm grid (snap() everything)
3. Sub-symbols in lib_symbols must NOT have library prefix (Device:R_0_1 → R_0_1)
4. Labels must be at EXACT pin positions or connected via wires
5. PWR_FLAG needed on every power output net (voltage regulator outputs)
6. SOT-23-5 LDOs: VOUT is at (7.62, 2.54), NC is at (7.62, -2.54) — don't mix them up!
7. Every pin must be either: wired+labeled, connected to power, or have no_connect flag

Usage:
    from kicad_sch_helpers import SchematicBuilder, SymbolLibrary, snap, pin_abs

The key insight: KiCad symbol libraries use Y-up (math convention),
but .kicad_sch files use Y-down (screen convention). When placing a
symbol at (sx, sy), a pin defined at library position (px, py) maps
to schematic position using:

    Rotation 0:   (sx + px, sy - py)
    Rotation 90:  (sx + py, sy + px)
    Rotation 180: (sx - px, sy + py)
    Rotation 270: (sx - py, sy - px)

This library handles all of this automatically via pin_abs().
"""

import uuid as _uuid
import re
import json
import subprocess
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


# =============================================================================
# Grid and coordinate utilities
# =============================================================================

GRID = 1.27  # KiCad default schematic grid in mm (50 mil)


def snap(v: float) -> float:
    """Snap a coordinate to the nearest 1.27mm grid point.
    ALWAYS use this for every coordinate in the schematic."""
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


def pin_abs(sx: float, sy: float, px: float, py: float,
            rotation: int = 0, mirror_y: bool = False) -> tuple:
    """
    Compute absolute schematic position of a pin. THE key function.

    Args:
        sx, sy: Symbol placement position in schematic (should be grid-snapped)
        px, py: Pin position in library (symbol) coordinates
        rotation: Symbol rotation (0, 90, 180, 270)
        mirror_y: Whether symbol is Y-mirrored

    Returns:
        (abs_x, abs_y): Absolute pin position, grid-snapped

    Example:
        # AD9363 at (320, 200), TX1A_P pin at library (-17.78, 25.40)
        x, y = pin_abs(320, 200, -17.78, 25.40)
        # Returns (302.26, 174.63) — snapped to grid
    """
    if mirror_y:
        px = -px

    dx, dy = pin_transform(px, py, rotation)
    return (snap(sx + dx), snap(sy + dy))


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

    def get_pin(self, name: str) -> Optional[PinDef]:
        """Get pin by name."""
        for p in self.pins:
            if p.name == name:
                return p
        return None

    def get_pin_by_name(self, name: str) -> Optional[PinDef]:
        """Get pin by name (alias for get_pin)."""
        return self.get_pin(name)

    def get_pin_by_number(self, number: str) -> Optional[PinDef]:
        """Get pin by number string."""
        for p in self.pins:
            if p.number == number:
                return p
        return None

    def pin_pos(self, name: str) -> tuple:
        """Get (x, y) library position of a pin by name. Raises if not found."""
        p = self.get_pin(name)
        if not p:
            raise KeyError(f"Pin '{name}' not found in symbol '{self.name}'. "
                          f"Available: {[pin.name for pin in self.pins]}")
        return (p.x, p.y)


class SymbolLibrary:
    """
    Parse and store symbol definitions from .kicad_sym files
    or from the lib_symbols section of a .kicad_sch file.

    Usage:
        lib = SymbolLibrary()
        lib.load_from_kicad_sym("path/to/library.kicad_sym")
        ad9363 = lib.get("AD9363ABCZ")
        px, py = ad9363.pin_pos("TX1A_P")
    """

    def __init__(self):
        self.symbols: dict = {}  # name -> SymbolDef

    def load_from_kicad_sym(self, filepath: str):
        """Load symbols from a .kicad_sym library file."""
        with open(filepath) as f:
            content = f.read()
        self._parse(content)

    # Keep 'load' as alias for backward compatibility
    load = load_from_kicad_sym

    def _parse(self, content: str):
        """Parse symbol definitions from S-expression content."""
        pin_pattern = re.compile(
            r'\(pin\s+(\w+)\s+\w+\s+'
            r'\(at\s+([-\d.]+)\s+([-\d.]+)\s+(\d+)\)\s+'
            r'\(length\s+([-\d.]+)\)\s+'
            r'\(name\s+"([^"]*)".*?\)\s+'
            r'\(number\s+"([^"]*)".*?\)\)',
            re.DOTALL
        )

        lines = content.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            m = re.match(r'^\(symbol\s+"([^"]+)"', line)
            # Skip sub-symbols (those ending in _digit_digit)
            if m and not re.search(r'_\d+_\d+$', m.group(1)):
                sym_name = m.group(1)
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
                        name=pm.group(6), number=pm.group(7),
                        x=float(pm.group(2)), y=float(pm.group(3)),
                        angle=int(pm.group(4)), length=float(pm.group(5)),
                        pin_type=pm.group(1),
                    ))
                if pins:
                    self.symbols[sym_name] = SymbolDef(name=sym_name, pins=pins)
                i = j
            else:
                i += 1

    def get(self, name: str) -> Optional[SymbolDef]:
        """Get symbol by name (tries with and without library prefix)."""
        if name in self.symbols:
            return self.symbols[name]
        if ':' in name:
            short = name.split(':', 1)[1]
            if short in self.symbols:
                return self.symbols[short]
        return None


# =============================================================================
# lib_symbols template generators
# =============================================================================

def lib_sym_2pin(lib_id: str, ref_prefix: str, default_val: str,
                 pin1_name: str = "1", pin2_name: str = "2",
                 pin1_type: str = "passive", pin2_type: str = "passive",
                 body: str = "rect") -> str:
    """
    Generate lib_symbol for a 2-pin component.

    IMPORTANT: Sub-symbol names use ONLY the symbol name, not the library prefix.
    This is handled automatically by this function.

    Standard 2-pin pin positions:
        Pin 1: (0, 2.54) pointing down (angle 270) — TOP in schematic
        Pin 2: (0, -2.54) pointing up (angle 90) — BOTTOM in schematic
    """
    sym_name = lib_id.split(':')[-1] if ':' in lib_id else lib_id

    bodies = {
        "rect": """      (rectangle (start -1.016 1.27) (end 1.016 -1.27)
        (stroke (width 0.254) (type default)) (fill (type none)))""",
        "cap": """      (polyline (pts (xy -1.27 0.508) (xy 1.27 0.508))
        (stroke (width 0.254) (type default)) (fill (type none)))
      (polyline (pts (xy -1.27 -0.508) (xy 1.27 -0.508))
        (stroke (width 0.254) (type default)) (fill (type none)))""",
        "inductor": """      (arc (start 0 -1.27) (mid 0.635 -0.635) (end 0 0)
        (stroke (width 0.254) (type default)) (fill (type none)))
      (arc (start 0 0) (mid 0.635 0.635) (end 0 1.27)
        (stroke (width 0.254) (type default)) (fill (type none)))""",
        "diode": """      (polyline (pts (xy -1.27 1.016) (xy -1.27 -1.016) (xy 1.27 0) (xy -1.27 1.016))
        (stroke (width 0.254) (type default)) (fill (type none)))
      (polyline (pts (xy 1.27 1.016) (xy 1.27 -1.016))
        (stroke (width 0.254) (type default)) (fill (type none)))""",
        "led": """      (polyline (pts (xy -1.27 1.016) (xy -1.27 -1.016) (xy 1.27 0) (xy -1.27 1.016))
        (stroke (width 0.254) (type default)) (fill (type none)))
      (polyline (pts (xy 1.27 1.016) (xy 1.27 -1.016))
        (stroke (width 0.254) (type default)) (fill (type none)))""",
    }
    drawing = bodies.get(body, bodies["rect"])

    return f"""    (symbol "{lib_id}"
      (pin_numbers hide) (pin_names hide) (in_bom yes) (on_board yes)
      (property "Reference" "{ref_prefix}" (at 2.54 0.508 0)
        (effects (font (size 1.27 1.27)) (justify left)))
      (property "Value" "{default_val}" (at 2.54 -1.016 0)
        (effects (font (size 1.27 1.27)) (justify left)))
      (property "Footprint" "" (at 0 0 0)
        (effects (font (size 1.27 1.27)) hide))
      (symbol "{sym_name}_0_1"
{drawing}
      )
      (symbol "{sym_name}_1_1"
        (pin {pin1_type} line (at 0 2.54 270) (length 1.27)
          (name "{pin1_name}" (effects (font (size 1.0 1.0))))
          (number "1" (effects (font (size 1.0 1.0)))))
        (pin {pin2_type} line (at 0 -2.54 90) (length 1.27)
          (name "{pin2_name}" (effects (font (size 1.0 1.0))))
          (number "2" (effects (font (size 1.0 1.0)))))
      )
    )"""


def lib_sym_power(name: str, net_name: str) -> str:
    """Generate a power symbol definition (GND, +3.3V, etc.)."""
    sym_name = name.split(':')[-1] if ':' in name else name
    if "GND" in name:
        drawing = """      (polyline (pts (xy 0 0) (xy 0 -1.27) (xy -1.27 -1.27) (xy 0 -2.54) (xy 1.27 -1.27) (xy 0 -1.27))
        (stroke (width 0) (type default)) (fill (type none)))"""
        pin_at = "(at 0 0 0)"
    else:
        drawing = """      (polyline (pts (xy -0.762 1.27) (xy 0.762 1.27))
        (stroke (width 0.254) (type default)) (fill (type none)))
      (polyline (pts (xy 0 0) (xy 0 1.27))
        (stroke (width 0) (type default)) (fill (type none)))"""
        pin_at = "(at 0 0 90)"

    return f"""    (symbol "{name}"
      (power) (pin_numbers hide) (pin_names hide) (in_bom no) (on_board yes)
      (property "Reference" "#PWR" (at 0 2.54 0)
        (effects (font (size 1.27 1.27)) hide))
      (property "Value" "{net_name}" (at 0 3.81 0)
        (effects (font (size 1.0 1.0))))
      (property "Footprint" "" (at 0 0 0)
        (effects (font (size 1.27 1.27)) hide))
      (symbol "{sym_name}_0_1"
{drawing}
      )
      (symbol "{sym_name}_1_1"
        (pin power_in line {pin_at} (length 0)
          (name "{net_name}" (effects (font (size 1.0 1.0))))
          (number "1" (effects (font (size 1.0 1.0)))))
      )
    )"""


def lib_sym_pwr_flag() -> str:
    """Generate PWR_FLAG symbol. Place on every power output net to avoid
    'power_pin_not_driven' ERC errors."""
    return """    (symbol "power:PWR_FLAG"
      (power) (pin_numbers hide) (pin_names hide) (in_bom no) (on_board yes)
      (property "Reference" "#FLG" (at 0 2.54 0)
        (effects (font (size 1.27 1.27)) hide))
      (property "Value" "PWR_FLAG" (at 0 3.81 0)
        (effects (font (size 1.0 1.0))))
      (property "Footprint" "" (at 0 0 0)
        (effects (font (size 1.27 1.27)) hide))
      (symbol "PWR_FLAG_0_1"
        (polyline (pts (xy 0 0) (xy 0 1.27) (xy -1.016 2.032) (xy 0 2.794) (xy 1.016 2.032) (xy 0 1.27))
          (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "PWR_FLAG_1_1"
        (pin power_out line (at 0 0 90) (length 0)
          (name "pwr" (effects (font (size 1.0 1.0))))
          (number "1" (effects (font (size 1.0 1.0)))))
      )
    )"""


# =============================================================================
# Sub-symbol name fixer (post-processing)
# =============================================================================

def fix_subsymbol_names(content: str) -> str:
    """
    Fix sub-symbol names in lib_symbols section.

    KiCad REQUIRES that sub-symbols (those with _N_N suffix) do NOT include
    the library prefix. This is the most common cause of "Invalid symbol unit
    name prefix" errors when opening generated schematics.

    Examples of what this fixes:
        "Device:R_0_1"              → "R_0_1"
        "Device:C_Polarized_0_1"    → "C_Polarized_0_1"
        "CubeSat_SDR:AD9363ABCZ_1_1" → "AD9363ABCZ_1_1"
        "Connector:Barrel_Jack_0_1" → "Barrel_Jack_0_1"
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
# Schematic builder
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

    All coordinates are automatically grid-snapped.
    Use connect_pin() for IC pins — it computes exact positions.
    Use place_2pin_vertical/horizontal for passive components.
    """

    def __init__(self, symbol_lib: SymbolLibrary = None, project_name: str = "project"):
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
        self.flg_counter = 0
        self._lib_symbols_content = ""

    def set_symbol_library(self, lib: SymbolLibrary):
        """Set the symbol library for pin position lookups."""
        self.symbol_lib = lib

    def set_lib_symbols(self, content: str):
        """Set raw lib_symbols S-expression content."""
        self._lib_symbols_content = content

    def place(self, lib_id: str, ref: str, value: str, x: float, y: float,
              rotation: int = 0, footprint: str = "", lcsc: str = "",
              mirror_y: bool = False, unit: int = 1) -> PlacedComponent:
        """Place a component at grid-snapped coordinates."""
        x, y = snap(x), snap(y)
        u = uid()
        ms = "(mirror y)" if mirror_y else ""

        self.components.append(f"""  (symbol (lib_id "{lib_id}") (at {x:.2f} {y:.2f} {rotation}) {ms}
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
        (path "/{self.root_uuid}" (reference "{ref}") (unit {unit}))
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

    def place_power(self, lib_id: str, value: str, x: float, y: float, rotation: int = 0):
        """Place a power symbol (GND, VCC, etc.)."""
        x, y = snap(x), snap(y)
        self.pwr_sym_counter += 1
        ref = f"#PWR{self.pwr_sym_counter:03d}"
        self.components.append(f"""  (symbol (lib_id "{lib_id}") (at {x:.2f} {y:.2f} {rotation})
    (uuid "{uid()}")
    (property "Reference" "{ref}" (at {x:.2f} {y + 2.54:.2f} 0)
      (effects (font (size 1.27 1.27)) hide))
    (property "Value" "{value}" (at {x:.2f} {y + 3.81:.2f} 0)
      (effects (font (size 0.8 0.8))))
    (property "Footprint" "" (at {x:.2f} {y:.2f} 0)
      (effects (font (size 1.27 1.27)) hide))
    (instances
      (project "{self.project_name}"
        (path "/{self.root_uuid}" (reference "{ref}") (unit 1))
      )
    )
  )""")

    def place_pwr_flag(self, x: float, y: float, net_name: str):
        """Place a PWR_FLAG on a power net. Essential for regulator outputs."""
        x, y = snap(x), snap(y)
        self.flg_counter += 1
        ref = f"#FLG{self.flg_counter:03d}"
        self.components.append(f"""  (symbol (lib_id "power:PWR_FLAG") (at {x:.2f} {y:.2f} 0)
    (uuid "{uid()}")
    (property "Reference" "{ref}" (at {x:.2f} {y + 2.54:.2f} 0)
      (effects (font (size 1.27 1.27)) hide))
    (property "Value" "PWR_FLAG" (at {x:.2f} {y + 3.81:.2f} 0)
      (effects (font (size 0.8 0.8))))
    (property "Footprint" "" (at {x:.2f} {y:.2f} 0)
      (effects (font (size 1.27 1.27)) hide))
    (instances
      (project "{self.project_name}"
        (path "/{self.root_uuid}" (reference "{ref}") (unit 1))
      )
    )
  )""")
        self.label(net_name, x, y)

    def connect_pin(self, ref: str, pin_name: str, net_label: str,
                    wire_dx: float = 0, wire_dy: float = 0,
                    label_angle: int = 0, by_number: bool = False):
        """
        THE key method. Connect a component's pin to a net label with exact
        computed coordinates and an optional wire stub.

        This eliminates dangling label and unconnected pin ERC errors.

        Args:
            ref: Component reference (e.g., "U1")
            pin_name: Pin name (or number if by_number=True)
            net_label: Net label text
            wire_dx, wire_dy: Wire extension from pin for routing room
            label_angle: Label rotation (0, 90, 180, 270)
            by_number: Look up pin by number instead of name
        """
        comp = self.placed.get(ref)
        if not comp:
            print(f"WARNING: Component {ref} not found", file=sys.stderr)
            return

        if not self.symbol_lib:
            print(f"WARNING: No symbol library set. Pass symbol_lib to constructor "
                  f"or call set_symbol_library() first.", file=sys.stderr)
            return

        sym_def = self.symbol_lib.get(comp.lib_id)
        if not sym_def:
            print(f"WARNING: Symbol {comp.lib_id} not in library", file=sys.stderr)
            return

        pin = (sym_def.get_pin_by_number(pin_name) if by_number
               else sym_def.get_pin(pin_name))
        if not pin:
            print(f"WARNING: Pin '{pin_name}' not found on {comp.lib_id}",
                  file=sys.stderr)
            return

        abs_x, abs_y = pin_abs(comp.x, comp.y, pin.x, pin.y,
                                comp.rotation, comp.mirror_y)
        end_x = snap(abs_x + wire_dx)
        end_y = snap(abs_y + wire_dy)

        if wire_dx != 0 or wire_dy != 0:
            self.wire(abs_x, abs_y, end_x, end_y)
            self.label(net_label, end_x, end_y, label_angle)
        else:
            self.label(net_label, abs_x, abs_y, label_angle)

    def connect_pin_noconnect(self, ref: str, pin_name: str, by_number: bool = False):
        """Place a no-connect flag on an unused pin."""
        comp = self.placed.get(ref)
        if not comp or not self.symbol_lib:
            return
        sym_def = self.symbol_lib.get(comp.lib_id)
        if not sym_def:
            return
        pin = (sym_def.get_pin_by_number(pin_name) if by_number
               else sym_def.get_pin(pin_name))
        if not pin:
            return
        abs_x, abs_y = pin_abs(comp.x, comp.y, pin.x, pin.y,
                                comp.rotation, comp.mirror_y)
        self.no_connect(abs_x, abs_y)

    # Alias for backward compatibility
    connect_pin_nc = connect_pin_noconnect

    def wire(self, x1: float, y1: float, x2: float, y2: float):
        """Draw a wire (auto-snapped). Skips zero-length wires."""
        x1, y1, x2, y2 = snap(x1), snap(y1), snap(x2), snap(y2)
        if x1 == x2 and y1 == y2:
            return
        self.wires.append(f"""  (wire (pts (xy {x1:.2f} {y1:.2f}) (xy {x2:.2f} {y2:.2f}))
    (stroke (width 0) (type default))
    (uuid "{uid()}")
  )""")

    # Short alias
    w = wire

    def label(self, name: str, x: float, y: float, angle: int = 0):
        """Place a net label (auto-snapped)."""
        x, y = snap(x), snap(y)
        self.labels.append(f"""  (label "{name}" (at {x:.2f} {y:.2f} {angle})
    (effects (font (size 1.27 1.27)) (justify left))
    (uuid "{uid()}")
  )""")

    def no_connect(self, x: float, y: float):
        """Place a no-connect flag (auto-snapped)."""
        x, y = snap(x), snap(y)
        self.no_connects.append(f"""  (no_connect (at {x:.2f} {y:.2f})
    (uuid "{uid()}")
  )""")

    # Short alias
    nc = no_connect

    def text_note(self, text: str, x: float, y: float, size: float = 2.54):
        """Add a text annotation."""
        self.text_notes.append(f"""  (text "{text}" (at {x:.2f} {y:.2f} 0)
    (effects (font (size {size} {size})) (justify left))
    (uuid "{uid()}")
  )""")

    def build(self, title: str = "Schematic", date: str = "2026-01-01",
              rev: str = "1.0", paper: str = "A1", comments: list = None) -> str:
        """Generate the complete .kicad_sch file.
        IMPORTANT: Always run fix_subsymbol_names() on the output!"""
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

        all_items = (self.components + self.wires + self.labels +
                     self.no_connects + self.text_notes)

        return f"""{header}

  (lib_symbols
{self._lib_symbols_content}
  )

{chr(10).join(all_items)}

  (sheet_instances
    (path "/"
      (page "1")
    )
  )
)"""


# =============================================================================
# Convenience helpers for 2-pin components
# =============================================================================

def place_2pin_vertical(builder: SchematicBuilder, lib_id: str, ref: str,
                        value: str, x: float, y: float,
                        top_net: str, bottom_net: str,
                        footprint: str = "", lcsc: str = "",
                        wire_ext: float = 3.81):
    """
    Place a 2-pin component vertically and wire both pins to net labels.

    Pin layout (rotation 0):
        Pin 1 at lib (0, 2.54) -> schematic TOP  -> connects to top_net
        Pin 2 at lib (0, -2.54) -> schematic BOTTOM -> connects to bottom_net

    Wire stubs extend wire_ext mm from each pin.
    """
    x, y = snap(x), snap(y)
    builder.place(lib_id, ref, value, x, y, footprint=footprint, lcsc=lcsc)
    p1y = snap(y - 2.54)  # Pin 1 in schematic (Y negated)
    p2y = snap(y + 2.54)  # Pin 2 in schematic
    builder.wire(x, p1y, x, snap(p1y - wire_ext))
    builder.label(top_net, x, snap(p1y - wire_ext))
    builder.wire(x, p2y, x, snap(p2y + wire_ext))
    builder.label(bottom_net, x, snap(p2y + wire_ext))


def place_2pin_horizontal(builder: SchematicBuilder, lib_id: str, ref: str,
                          value: str, x: float, y: float,
                          left_net: str, right_net: str,
                          footprint: str = "", lcsc: str = "",
                          wire_ext: float = 3.81):
    """
    Place a 2-pin component horizontally (rotation=90) and wire both pins.

    Pin layout (rotation 90):
        Pin 1 at lib (0, 2.54) -> schematic RIGHT -> connects to right_net
        Pin 2 at lib (0, -2.54) -> schematic LEFT -> connects to left_net
    """
    x, y = snap(x), snap(y)
    builder.place(lib_id, ref, value, x, y, rotation=90,
                  footprint=footprint, lcsc=lcsc)
    p1x = snap(x + 2.54)  # Pin 1 in schematic (rotation 90)
    p2x = snap(x - 2.54)  # Pin 2
    builder.wire(p1x, y, snap(p1x + wire_ext), y)
    builder.label(right_net, snap(p1x + wire_ext), y)
    builder.wire(p2x, y, snap(p2x - wire_ext), y)
    builder.label(left_net, snap(p2x - wire_ext), y)


# =============================================================================
# ERC validation
# =============================================================================

def run_erc(schematic_path: str, output_path: str = None,
            kicad_cli: str = "kicad-cli") -> dict:
    """
    Run KiCad ERC check via kicad-cli and return structured results.

    Returns:
        dict with: success (bool), errors (int), warnings (int),
                   error_types (dict), details (list)
    """
    if output_path is None:
        output_path = str(Path(schematic_path).with_suffix('.erc.json'))

    try:
        result = subprocess.run(
            [kicad_cli, "sch", "erc",
             "--output", output_path, "--format", "json",
             "--severity-all", schematic_path],
            capture_output=True, text=True, timeout=60
        )
    except FileNotFoundError:
        # Try to auto-discover kicad-cli
        found = find_kicad_cli()
        if found:
            print(f"WARNING: kicad-cli not on PATH but found at: {found}",
                  file=sys.stderr)
            suggest_kicad_cli_symlink()
            try:
                result = subprocess.run(
                    [found, "sch", "erc",
                     "--output", output_path, "--format", "json",
                     "--severity-all", schematic_path],
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

    try:
        with open(output_path) as f:
            report = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return _parse_text_erc(result.stdout + result.stderr)

    errors = [v for v in report.get("violations", []) if v.get("severity") == "error"]
    warnings = [v for v in report.get("violations", []) if v.get("severity") == "warning"]

    return {
        "success": len(errors) == 0,
        "errors": len(errors), "warnings": len(warnings),
        "total": len(errors) + len(warnings),
        "details": report.get("violations", []),
        "error_types": _categorize(errors),
        "warning_types": _categorize(warnings),
    }


def validate_and_fix_loop(schematic_path: str, fix_callback,
                           max_iterations: int = 5,
                           kicad_cli: str = "kicad-cli") -> dict:
    """
    Automated generate -> validate -> fix loop.

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
            print("No ERC errors!")
            return result

        if not fix_callback(result, i):
            print("Fix callback returned False, stopping.")
            return result

    print(f"Reached max iterations ({max_iterations})")
    return result


def _categorize(violations):
    cats = {}
    for v in violations:
        cats[v.get("type", "unknown")] = cats.get(v.get("type", "unknown"), 0) + 1
    return cats


def _parse_text_erc(text):
    errors = len(re.findall(r';\s*error', text))
    warnings = len(re.findall(r';\s*warning', text))
    return {"success": errors == 0, "errors": errors, "warnings": warnings,
            "total": errors + warnings, "details": [], "raw": text}


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
    print("KiCad Schematic Helper Library v2")
    print(f"Grid: {GRID} mm")
    print(f"snap(42.5) = {snap(42.5)}")
    print(f"pin_abs(320, 200, -17.78, 25.40, rotation=0) = "
          f"{pin_abs(320, 200, -17.78, 25.40, rotation=0)}")
    print(f"pin_abs(320, 200, 0, 2.54, rotation=90) = "
          f"{pin_abs(320, 200, 0, 2.54, rotation=90)}")
    print()
    print("Checking kicad-cli availability...")
    cli_path = find_kicad_cli()
    if cli_path:
        print(f"kicad-cli found: {cli_path}")
    else:
        suggest_kicad_cli_symlink()
