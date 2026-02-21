---
name: kicad-schematic-agent
description: "Generate, validate, and fix KiCad 8 schematic files (.kicad_sch) programmatically. Use this skill whenever the user wants to create or modify KiCad schematics, generate netlists from circuit descriptions, or fix ERC errors. Triggers on: KiCad, schematic, .kicad_sch, ERC, electrical rules check, circuit design, PCB schematic, netlist generation, S-expression schematic."
---

# KiCad Schematic Agent

Generate ERC-clean KiCad 8 schematics by writing Python scripts that use computed pin positions — never guess coordinates.

## Critical Principle

**The #1 cause of broken schematics is guessed pin positions.** When connecting labels to IC pins, you MUST compute exact coordinates using the symbol definition's pin positions and the coordinate transform formula. The helper library in `scripts/kicad_sch_helpers.py` does this automatically.

## Architecture

```
User describes circuit
        ↓
Read symbol libraries (.kicad_sym) to get pin positions
        ↓
Write Python script using SchematicBuilder (from helper library)
        ↓
Generate .kicad_sch file
        ↓
Run ERC validation: kicad-cli sch erc --format json
        ↓
Parse errors → fix script → regenerate → repeat (max 5 iterations)
```

## Step-by-step Workflow

### 0. Ensure kicad-cli is Available

Before running any ERC validation, verify that `kicad-cli` is on the system PATH. Run:

```bash
which kicad-cli 2>/dev/null || where kicad-cli 2>/dev/null
```

If **not found**, check for a local KiCad installation and offer to create a symlink:

**macOS:**
```bash
# Check if KiCad is installed as an app
KICAD_CLI="/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
if [ -f "$KICAD_CLI" ]; then
    echo "Found kicad-cli inside KiCad.app. Creating symlink..."
    sudo ln -sf "$KICAD_CLI" /usr/local/bin/kicad-cli
    echo "Done! kicad-cli is now available on PATH."
else
    echo "KiCad not found. Install from https://www.kicad.org/download/macos/"
fi
```

**Linux:**
```bash
# kicad-cli is typically installed alongside KiCad via package manager
# Check common locations
for p in /usr/bin/kicad-cli /usr/local/bin/kicad-cli /snap/kicad/current/bin/kicad-cli; do
    if [ -f "$p" ]; then
        echo "Found kicad-cli at $p"
        # If not on PATH, symlink it
        if ! command -v kicad-cli &>/dev/null; then
            sudo ln -sf "$p" /usr/local/bin/kicad-cli
        fi
        break
    fi
done
# If still not found:
# Ubuntu/Debian: sudo apt install kicad
# Fedora: sudo dnf install kicad
# Arch: sudo pacman -S kicad
# Or install from https://www.kicad.org/download/linux/
```

**Windows:**
```powershell
# Check standard install path
$kicadCli = "C:\Program Files\KiCad\8.0\bin\kicad-cli.exe"
if (Test-Path $kicadCli) {
    Write-Host "Found kicad-cli. Add to PATH:"
    Write-Host '  [Environment]::SetEnvironmentVariable("PATH", $env:PATH + ";C:\Program Files\KiCad\8.0\bin", "User")'
} else {
    Write-Host "KiCad not found. Install from https://www.kicad.org/download/windows/"
}
```

Tell the user what you found and ask for confirmation before creating any symlinks. If kicad-cli is truly not installed, provide the download link for their OS and stop — ERC validation requires it.

### 1. Understand the Circuit

Before writing any code, gather:
- Component list with specific part numbers
- Power architecture (voltage rails, regulators)
- Signal connections (which pins connect to which)
- Symbol libraries needed (standard KiCad libs + any custom .kicad_sym files)

### 2. Read Symbol Libraries

For every IC (not simple passives), read its .kicad_sym definition to get exact pin names, numbers, positions, and types. This is non-negotiable — you cannot connect pins correctly without knowing their positions.

```python
from kicad_sch_helpers import SymbolLibrary

lib = SymbolLibrary()
lib.load_from_kicad_sym("path/to/library.kicad_sym")

# Now you know exact pin positions
ad9363 = lib.get("AD9363ABCZ")
for pin in ad9363.pins:
    print(f"{pin.name} ({pin.number}): at ({pin.x}, {pin.y}), type={pin.pin_type}")
```

### 3. Write the Generator Script

Use `SchematicBuilder` for all schematic construction. The key method is `connect_pin()` which computes exact pin positions automatically:

```python
from kicad_sch_helpers import SchematicBuilder, SymbolLibrary, snap

lib = SymbolLibrary()
lib.load_from_kicad_sym("custom_symbols.kicad_sym")

sch = SchematicBuilder(symbol_lib=lib, project_name="my_project")
sch.set_lib_symbols(lib_symbols_content)  # Raw S-expression for embedded symbols

# Place an IC
sch.place("CubeSat_SDR:AD9363ABCZ", "U1", "AD9363ABCZ",
          x=320, y=200, footprint="CubeSat_SDR:AD9363_BGA144")

# Connect pins by NAME — coordinates computed automatically
sch.connect_pin("U1", "TX1A_P", "TX1A_P", wire_dx=-5.08)
sch.connect_pin("U1", "SPI_CLK", "SPI_CLK", wire_dy=-5.08)
sch.connect_pin("U1", "GND", "GND", wire_dy=5.08)

# For unused pins, add no-connect flags
sch.connect_pin_noconnect("U1", "AUXDAC1")

# For 2-pin passives, use convenience helpers
from kicad_sch_helpers import place_2pin_vertical
place_2pin_vertical(sch, "Device:C", "C1", "100nF",
                    x=snap(230), y=snap(155),
                    top_net="VCC_3V3", bottom_net="GND",
                    footprint="Capacitor_SMD:C_0402_1005Metric")
```

### 4. Handle the lib_symbols Section

Every symbol referenced must be embedded in the schematic's lib_symbols. Two rules:

1. **Parent symbols** use the full lib_id: `(symbol "Device:R" ...)`
2. **Sub-symbols** must NOT have the library prefix: `(symbol "R_0_1" ...)` not `(symbol "Device:R_0_1" ...)`

Use `fix_subsymbol_names()` as a post-processing step:
```python
from kicad_sch_helpers import fix_subsymbol_names

content = sch.build(title="My Schematic")
content = fix_subsymbol_names(content)
```

### 5. Add PWR_FLAG Symbols

For every power net that originates from a voltage regulator (not a power symbol), add a PWR_FLAG to prevent "power_pin_not_driven" errors:

```python
# Define PWR_FLAG in lib_symbols (see references/kicad_sexpression_format.md)
# Then place on each power net:
sch.place_power("power:PWR_FLAG", "PWR_FLAG", x=70, y=78)
sch.label("VCC_3V3A", 70, 78)  # On the net that needs the flag
```

### 6. Validate with kicad-cli

```python
from kicad_sch_helpers import run_erc

result = run_erc("output/schematic.kicad_sch")
print(f"Errors: {result['errors']}, Warnings: {result['warnings']}")

if result['errors'] > 0:
    for detail in result['details']:
        if detail.get('severity') == 'error':
            print(f"  {detail['type']}: {detail.get('description', '')}")
```

### 7. Automated Fix Loop

For complex schematics, use the validation loop:

```python
from kicad_sch_helpers import validate_and_fix_loop

def my_fixer(erc_result, iteration):
    """Analyze ERC errors and apply fixes. Return True if fixes applied."""
    error_types = erc_result.get('error_types', {})

    if 'pin_not_connected' in error_types:
        # Read the schematic, find unconnected pins, add connections
        # ... fix logic ...
        return True

    if 'label_dangling' in error_types:
        # Move labels to correct pin positions
        # ... fix logic ...
        return True

    return False  # No fixable errors found

final = validate_and_fix_loop("output/schematic.kicad_sch", my_fixer)
```

## Common Patterns

### Decoupling Capacitor Array
```python
for i, (ref, val) in enumerate(zip(refs, values)):
    place_2pin_vertical(sch, "Device:C", ref, val,
                        x=snap(start_x + i * 8), y=snap(cap_y),
                        top_net=power_net, bottom_net="GND",
                        footprint=f"Capacitor_SMD:{fp}")
```

### Multi-pin IC Connection
```python
# Always use connect_pin — never compute positions manually
signal_map = {
    "TX1A_P": "TX1A_P",
    "TX1A_N": "TX1A_N",
    "SPI_CLK": "SPI_CLK",
    # ... all signal pins
}
for pin_name, net_name in signal_map.items():
    sch.connect_pin("U1", pin_name, net_name, wire_dx=-7.62)

# Power pins
for pin_name in ["VDDD1P3", "VDDA1P3", "VDDD1P8"]:
    sch.connect_pin("U1", pin_name, f"VCC_{pin_name}", wire_dy=5.08)

# Unused pins
for pin_name in ["AUXDAC1", "AUXDAC2", "AUXADC", "TEMP_SENS"]:
    sch.connect_pin_noconnect("U1", pin_name)
```

### Power Regulator with PWR_FLAG
```python
sch.place("CubeSat_SDR:AMS1117", "U3", "AMS1117-3.3", x=50, y=80, ...)
sch.connect_pin("U3", "VIN", "VCC_5V", wire_dx=-7.62)
sch.connect_pin("U3", "GND", "GND", wire_dy=5.08)
sch.connect_pin("U3", "VOUT", "VCC_3V3", wire_dx=7.62)
# PWR_FLAG on output
sch.place_power("power:PWR_FLAG", "PWR_FLAG", x=snap(65), y=snap(80))
sch.label("VCC_3V3", snap(65), snap(80))
```

## Reference Files

- `scripts/kicad_sch_helpers.py` — Python helper library (always use this)
- `references/kicad_sexpression_format.md` — KiCad S-expression format specification, coordinate system, common ERC errors and fixes

Read `references/kicad_sexpression_format.md` before generating any schematic to understand the coordinate system, sub-symbol naming rules, and PWR_FLAG requirements.

## Checklist Before Delivery

1. All coordinates snapped to 1.27mm grid
2. Every IC pin either connected via `connect_pin()` or flagged with `connect_pin_noconnect()`
3. Sub-symbol names fixed with `fix_subsymbol_names()`
4. PWR_FLAG on every power output net
5. ERC validation run (0 errors target, warnings acceptable)
6. Parenthesis balance verified (depth 0 at end of file)
