# photohaul

A fast, dependency-free CLI for ingesting photos off a camera card.

Copies raw files from a mounted card into the current folder, renaming each to a
stable, millisecond-precise name derived from its Exif capture time
(`YYYYMMDD-hhmmss_mmm.ext`, e.g. `20260526-140024_708.ext`). Because the name
comes only from the frame's own metadata, re-running on the same card just skips
what already landed.

Pick the format with the `format` argument (e.g. `photohaul nef`), or set a
default `format` in `~/.photohaul` (below) and just run `photohaul`. The argument
wins when both are present; there is no built-in default, so with neither set
photohaul reports an error rather than guessing. photohaul reads Exif natively
(no exiftool) from each of:

- **ARW** (Sony) and **NEF** (Nikon) — TIFF at byte zero.
- **RAF** (Fuji) — Exif lives in an embedded JPEG, read transparently.
- **CR3** (Canon) — an MP4-style container; the Exif is pulled from its `moov`
  metadata box.

So `photohaul <format>` ingests any of them the same way — stable naming,
byte-exact copies, and (where the camera's in-camera Protect maps to the macOS
lock bit) Purple-labelling of protected frames.

Frames locked (protected) in-camera are detected, copied unlocked, and tagged
with a Purple color label for Lightroom via an `.xmp` sidecar. **The card is
never modified.**

## Usage

    src/photohaul.py [format] [--source PATH] [--dest PATH]
                     [--locked | --unlocked | --all]
                     [--dry-run] [--rewrite] [--init-template]

See `src/photohaul.py --help` for the full list of options and examples.

## Metadata (copyright, creator, captions)

photohaul writes all metadata to the `.xmp` sidecar — never the raw — so the
copied file stays a byte-exact clone of the card original and re-runs stay
idempotent. (The lone exception is an opt-in capture-time correction, below, which
rewrites the copy's EXIF date/offset fields in place.) Lightroom reads these on
import.

**Rights** come from an optional `~/.photohaul` config file (INI). Keys in
`[default]` are inherited by every profile; a profile section overrides or adds.
Missing file or field → simply not written:

```ini
[default]
format    = arw                    ; default format when none is given on the CLI
creator   = Your Name              ; -> dc:creator
copyright = © {year} Your Name     ; -> dc:rights  ({year} = capture year)

[work]
copyright = © {year} Your Name / yoursite.com
credit    = Your Name/yoursite.com ; default caption byline
```

**Profiles** let one camera serve multiple contexts (e.g. personal vs work)
with different rights. The active profile is chosen by, in order: `--profile NAME`,
then a `"profile"` key in the folder's `photohaul.json`, then `[default]`. A folder
with no template stays on `[default]` — so personal shots never pick up the
branded copyright. (A section-less legacy config is read as `[default]`.)

**Per-shoot captions** come from a `photohaul.json` in the destination folder,
auto-detected on ingest. Scaffold a blank one with `photohaul --init-template`:

```json
{
  "profile": "",
  "teamA": "",
  "teamB": "",
  "event": "",
  "venue": "",
  "location": "",
  "credit": "",
  "time_shift": "",
  "shot_tz": ""
}
```

Blank caption fields are omitted (`profile` selects the rights preset, and
`time_shift` / `shot_tz` are the capture-time correction below — none are part of
the caption text). A fully filled template yields, in
`dc:description`:

> Team A vs Team B, Event, at Venue, City, ST on May 30, 2026. Photo by Your Name/yoursite.com.

(`date` is auto-filled per frame; `credit` falls back to the config `credit`,
then `creator`.)

## Capture-time correction (clock drift & timezone)

Two optional, composable keys in `photohaul.json` fix a wrong capture time. Both
drive the destination filename, the caption date, and `{year}`, **and** rewrite
the copied raw's EXIF date/offset fields in place (a same-length overwrite — no
structural change, size unchanged). The **card is never touched**; only the copy
is corrected.

```json
{
  "time_shift": "+2h30m",
  "shot_tz": "-04:00"
}
```

- **`time_shift`** — *clock drift.* A wall-clock-only nudge of the date fields
  (the camera clock was simply wrong). Signed, units `d`/`h`/`m`/`s`, combinable
  (`+2h30m`, `-15s`, `90m`); whole seconds only (the millisecond key is never
  disturbed). The UTC-offset tags are left alone.
- **`shot_tz`** — *timezone / travel* (the main use case). "These frames were
  actually shot at this UTC offset." It derives the shift from the camera's
  recorded offset, so it both moves the displayed time **and** restamps the three
  offset tags — the absolute instant is preserved. Strict `±HH:MM`. **`shot_tz`
  alone is the complete travel fix** — you don't also set `time_shift`. Worked
  example: home `-07:00`, you fly three zones east and forget to change the
  camera, shoot 3:00 pm local → the camera stamps `12:00 / -07:00`; `shot_tz:
  "-04:00"` yields `15:00 / -04:00`, same instant.

When both are set they compose (date fields shift by the sum; offsets set to
`shot_tz`). Both are validated once at startup — bad input aborts before any copy.

> **Set these before the first ingest of a folder.** The corrections feed the
> filename, so changing them after files exist produces *new names* (duplicates),
> not updates — re-ingest the folder cleanly instead. There is deliberately no CLI
> flag (a forgotten flag could silently rename everything). `--rewrite` ignores
> both keys, since the destination files already carry the corrected time.

Sidecars are **create-if-absent** by default. `photohaul --rewrite` refreshes
them on files **already in the destination** — no card is needed and nothing is
copied. It **merges** only photohaul's fields (copyright, creator, caption),
preserving everything else (e.g. Lightroom develop edits). An existing **Purple
label is kept as-is**; because lock status is only known from the card, rewrite
never adds or removes a label. An existing sidecar that can't be parsed is
reported and left untouched, never overwritten. `--rewrite` cannot be combined
with `--locked`/`--unlocked`.

## Requirements

Python 3, standard library only — no external packages.

In-camera lock detection is macOS-specific: it reads the BSD `uchg` (immutable)
flag that the exFAT driver maps the camera's protect bit onto.

## Design

Design notes live in [`docs/`](docs/) as dated, per-topic plan documents
(`YYYYMMDD_<topic>_plan.md`), so the directory reads as a chronological record
of how the design evolved.
