# photohaul

A fast, dependency-free CLI for ingesting photos off a camera card.

Copies raw files from a mounted card into the current folder, renaming each to a
stable, millisecond-precise name derived from its Exif capture time
(`YYYYMMDD-hhmmss_mmm.ext`, e.g. `20260526-140024_708.arw`). Because the name
comes only from the frame's own metadata, re-running on the same card just skips
what already landed.

Frames locked (protected) in-camera are detected, copied unlocked, and tagged
with a Purple color label for Lightroom via an `.xmp` sidecar. **The card is
never modified.**

## Usage

    src/photohaul.py [extension] [--source PATH] [--dest PATH]
                     [--locked | --unlocked | --all] [--dry-run]

See `src/photohaul.py --help` for the full list of options and examples.

## Requirements

Python 3, standard library only — no external packages.

In-camera lock detection is macOS-specific: it reads the BSD `uchg` (immutable)
flag that the exFAT driver maps the camera's protect bit onto.

## Design

See [docs/PLAN.md](docs/PLAN.md).
