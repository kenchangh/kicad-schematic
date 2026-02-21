# kicad-schematic

An agent skill for generating, validating, and fixing KiCad 8 schematic files (`.kicad_sch`) programmatically. Eliminates the #1 source of broken schematics: guessed pin positions.

## Install

```bash
npx skills add kicad-schematic
```

Or from GitHub directly:

```bash
npx skills add kenchangh/kicad-schematic
```

## What it does

This skill teaches your AI coding agent to:

1. Read KiCad symbol libraries (`.kicad_sym`) to get exact pin positions
2. Write Python scripts using the `SchematicBuilder` helper — never manually guess coordinates
3. Generate `.kicad_sch` files with computed pin-label connectivity
4. Run ERC validation via `kicad-cli sch erc --format json`
5. Parse errors, fix the generator script, and re-run (up to 5 iterations)

## Prerequisites

- **KiCad 8** installed on your system
- `kicad-cli` available on PATH (the skill will detect your install and offer to symlink it if needed)
- Python 3.8+

## Skill contents

```
kicad-schematic/
├── SKILL.md                              # Skill instructions (agent reads this)
├── scripts/
│   └── kicad_sch_helpers.py              # Python helper library
└── references/
    └── kicad_sexpression_format.md       # KiCad S-expression format reference
```

## How it works

The core insight: KiCad symbol libraries use **Y-up** (math convention), but schematics use **Y-down** (screen convention). The `SchematicBuilder.connect_pin()` method handles this coordinate transform automatically, so every label lands exactly on its pin — no guessing.

```python
from kicad_sch_helpers import SchematicBuilder, SymbolLibrary

lib = SymbolLibrary()
lib.load_from_kicad_sym("my_symbols.kicad_sym")

sch = SchematicBuilder(symbol_lib=lib, project_name="my_project")
sch.place("Device:R", "R1", "10k", x=100, y=100, footprint="Resistor_SMD:R_0402")
sch.connect_pin("R1", "1", "VCC", wire_dy=-5.08, by_number=True)
sch.connect_pin("R1", "2", "NET1", wire_dy=5.08, by_number=True)
```

## Supported agents

Works with any agent that supports the [skills standard](https://github.com/vercel-labs/skills): Claude Code, Cursor, Codex, Windsurf, OpenCode, and more.

## License

MIT
