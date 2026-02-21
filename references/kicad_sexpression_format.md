# KiCad 8 Schematic S-Expression Format Reference

## File Structure

```
(kicad_sch
  (version 20231120)
  (generator "name")
  (generator_version "8.0")
  (uuid "...")
  (paper "A1")
  (title_block ...)

  (lib_symbols
    ;; Embedded symbol definitions (copied from .kicad_sym libraries)
    (symbol "Library:SymbolName" ...)
  )

  ;; Placed components, wires, labels, etc.
  (symbol (lib_id "Library:SymbolName") (at X Y ANGLE) ...)
  (wire (pts (xy X1 Y1) (xy X2 Y2)) ...)
  (label "NetName" (at X Y ANGLE) ...)
  (no_connect (at X Y) ...)

  (sheet_instances
    (path "/" (page "1"))
  )
)
```

## Coordinate System

- **Units**: millimeters
- **X axis**: positive = right
- **Y axis**: positive = DOWN (screen convention)
- **Default grid**: 1.27 mm (50 mil)
- **Angles**: degrees, counterclockwise (0=right, 90=up, 180=left, 270=down)

### Library vs Schematic Y-axis

Symbol libraries (.kicad_sym) use **Y-up** (math convention).
Schematics (.kicad_sch) use **Y-down** (screen convention).

When a symbol is placed at (sx, sy) with rotation R:
- **Rotation 0**:   pin at lib (px, py) → schematic (sx + px, sy - py)
- **Rotation 90**:  pin at lib (px, py) → schematic (sx + py, sy + px)
- **Rotation 180**: pin at lib (px, py) → schematic (sx - px, sy + py)
- **Rotation 270**: pin at lib (px, py) → schematic (sx - py, sy - px)

## Embedded lib_symbols Section

Every symbol used in the schematic must be defined in `(lib_symbols ...)`.
These are copies from the library files, embedded for portability.

### Critical Rule: Sub-symbol Naming

Parent symbols use the full library-prefixed name: `(symbol "Device:R" ...)`
Sub-symbols (for units and body styles) must NOT include the library prefix:
- CORRECT: `(symbol "R_0_1" ...)` and `(symbol "R_1_1" ...)`
- WRONG: `(symbol "Device:R_0_1" ...)` ← causes "Invalid symbol unit name prefix" error

The naming convention is: `{SymbolName}_{unit}_{bodystyle}`
- `_0_1`: Body style drawings (rectangles, lines, arcs)
- `_1_1`: Pin definitions for unit 1

### Pin Definition Format

```
(pin TYPE STYLE (at X Y ANGLE) (length L)
  (name "PinName" (effects (font (size 1.0 1.0))))
  (number "PinNum" (effects (font (size 1.0 1.0)))))
```

- TYPE: passive, power_in, power_out, input, output, bidirectional
- STYLE: line, inverted, clock, etc.
- (at X Y ANGLE): Connection point position (where wires attach)
- ANGLE: Direction the pin points INTO the symbol body
  - 0 = pin points right (connection is on LEFT side)
  - 90 = pin points up (connection is on BOTTOM)
  - 180 = pin points left (connection is on RIGHT side)
  - 270 = pin points down (connection is on TOP)

## Placed Symbol Format

```
(symbol (lib_id "Library:Name") (at X Y ANGLE) [mirror]
  (uuid "...")
  (property "Reference" "U1" (at X Y 0) (effects ...))
  (property "Value" "IC_Name" (at X Y 0) (effects ...))
  (property "Footprint" "Package:FP" (at X Y 0) (effects ... hide))
  (instances
    (project "project_name"
      (path "/ROOT_UUID"
        (reference "U1")
        (unit 1)
      )
    )
  )
)
```

Required fields: lib_id, at, uuid, Reference, Value, Footprint, instances.

## Wire Format

```
(wire (pts (xy X1 Y1) (xy X2 Y2))
  (stroke (width 0) (type default))
  (uuid "...")
)
```

Wires connect components via their pin endpoints. A wire endpoint must
exactly coincide with a pin's connection point to make a connection.

## Label Format

```
(label "NetName" (at X Y ANGLE)
  (effects (font (size 1.27 1.27)) (justify left))
  (uuid "...")
)
```

A label must be placed at a wire endpoint or directly at a pin's
connection point. Labels at coordinates that don't match any wire
or pin will cause "label_dangling" ERC errors.

## Power Symbol Format

Power symbols (GND, VCC) are special symbols with the `(power)` flag.
They have a single pin that defines the power net.

## No-Connect Flag

```
(no_connect (at X Y)
  (uuid "...")
)
```

Place at pin connection points for intentionally unconnected pins.

## PWR_FLAG

To avoid "power_pin_not_driven" errors, place PWR_FLAG symbols on nets
that are driven by components that KiCad doesn't recognize as power sources
(e.g., voltage regulators with passive output pins). Define PWR_FLAG in
lib_symbols and place it on the relevant net.

```
(symbol "power:PWR_FLAG"
  (power) (pin_numbers hide) (pin_names hide) (in_bom no) (on_board yes)
  (property "Reference" "#FLG" (at 0 2.54 0) (effects (font (size 1.27 1.27)) hide))
  (property "Value" "PWR_FLAG" (at 0 3.81 0) (effects (font (size 1.0 1.0))))
  (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
  (symbol "PWR_FLAG_0_1"
    (polyline (pts (xy 0 0) (xy 0 1.27) (xy -1.016 2.032) (xy 0 2.794) (xy 1.016 2.032) (xy 0 1.27))
      (stroke (width 0) (type default)) (fill (type none)))
  )
  (symbol "PWR_FLAG_1_1"
    (pin power_out line (at 0 0 90) (length 0)
      (name "pwr" (effects (font (size 1.0 1.0))))
      (number "1" (effects (font (size 1.0 1.0)))))
  )
)
```

## Common ERC Error Types

| Error Type | Cause | Fix |
|---|---|---|
| pin_not_connected | Pin has no wire/label | Add wire+label or no_connect |
| label_dangling | Label not at wire/pin | Move label to pin position |
| power_pin_not_driven | Power input with no power source | Add PWR_FLAG |
| pin_not_driven | Input pin with no driver | Connect to output or add pull-up |
| endpoint_off_grid | Wire/pin not on 1.27mm grid | Snap coordinates to grid |
| lib_symbol_mismatch | Embedded symbol differs from library | Update embedded copy |
| lib_symbol_issues | Symbol not found in library | Add to lib_symbols section |
| unconnected_wire_endpoint | Wire end not connected | Extend wire to pin/label |
