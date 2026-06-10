# photohaul

[![tests](https://github.com/uzi/photohaul/actions/workflows/test.yml/badge.svg)](https://github.com/uzi/photohaul/actions/workflows/test.yml)

A fast, dependency-free CLI that turns a card full of frames into a
deadline-ready Lightroom shoot.

Built for press and sports shooting (but applicable beyond that), photohaul copies
raw files off a mounted card and does the tedious prep on the way in: it renames each
frame to its exact capture time (`YYYYMMDD-hhmmss_mmm.ext`, e.g.
`20260406-123456_789.jpg`), takes the keepers you **flagged in-camera** and marks
them Purple in Lightroom, and writes an AP-style caption plus the copyright and IPTC
fields (credit, source, location, usage terms, keywords) a photo desk expects — all
into an `.xmp` sidecar, so the raw stays a byte-exact clone of the card original.

In-camera **voice memos** ride along, renamed to match their photo. Re-runs are
idempotent — the name comes only from the frame's own metadata, so running again
just skips whatever already landed. And **the card itself is never modified**.

## Quick start

Copy the script somewhere in your $PATH, then run it from the folder you want the photos copied *into*:

```sh
cd ~/Photos/some-shoot   # the destination
photohaul arw            # copy every .ARW off the mounted card
photohaul arw --locked   # ... or just the frames you locked in-camera
```

Set a default `format` in `~/.photohaul` (below) and you can drop the argument
and just run `photohaul`.

## Usage

    photohaul [format] [--source PATH] [--dest PATH]
              [--locked | --unlocked | --all] [--dry-run]
              [--rewrite] [--local] [--init] [--profile NAME]

Run from the destination folder, or point `--dest` at it. With no flags,
photohaul copies every frame of the chosen format off the auto-detected card
into that folder.

| Flag | What it does |
|------|--------------|
| `format` | File type to ingest — `arw`, `cr3`, `nef`, `raf`, `dng`, or `jpg`. Overrides `format` in `~/.photohaul`; there is no built-in default. |
| `--source PATH` | Card root to read from. Default: auto-detect the one mounted volume with a `DCIM/` folder under `/Volumes` (errors if there are zero or several). |
| `--dest PATH` | Destination directory. Default: the current folder. |
| `--locked` | Ingest only the frames you protected in-camera — your flagged keepers, copied unlocked and labelled Purple. |
| `--unlocked` | Ingest only the unprotected frames. |
| `--all` | Ingest everything (the default). |
| `-n`, `--dry-run` | Scan and report exactly what would happen — copies, skips, sidecars, conflicts — but touch nothing. |
| `--rewrite` | Refresh metadata on files **already in the destination**; no card, nothing copied. Merge-only — see *Refreshing metadata*, below. |
| `--local` | Rename camera-named files **already in the destination** in place; no card, no copy — see *Rename in place*, below. |
| `--init` | Scaffold a `photohaul.json` in the destination and exit (blank, or seeded from `--profile`) — see *Per-shoot setup*, below. |
| `--profile NAME` | Apply a named rights preset from `~/.photohaul` — see *Rights & profiles*, below. |

`--locked`, `--unlocked`, and `--all` are mutually exclusive. `--rewrite` and
`--local` are card-free modes: they can't be combined with each other or with the
filter flags, and `--local` also rejects `--source`. Run `photohaul --help` for
this same list plus worked examples.

## Rights & profiles — `~/.photohaul`

photohaul writes all metadata to the `.xmp` sidecar — never the raw — so the
copied file stays a byte-exact clone of the card original and re-runs stay
idempotent. (The lone exception is an opt-in capture-time correction, below, which
rewrites the copy's EXIF date/offset fields in place.) Lightroom reads these on
import.

Your identity and rights live in an optional `~/.photohaul` config file — set
once and reused across every shoot.

### Rights

Rights come from an optional `~/.photohaul` config file (INI). Keys in
`[default]` are inherited by every profile; a profile section overrides or adds.
Missing file or field → simply not written:

```ini
# Inline comments are not supported — keep comments on their own line, or the
# "; ..." would be read as part of the value.
[default]
# format: default file type to ingest when none is given on the CLI
format    = arw
# creator -> dc:creator ; copyright -> dc:rights  ({year} = capture year)
creator   = Your Name
copyright = © {year} Your Name

[work]
copyright = © {year} Your Name / yoursite.com
# credit -> photoshop:Credit and the default caption byline
credit    = Your Name/yoursite.com
```

A ready-to-edit [`example.photohaul`](example.photohaul) ships in the repo with a
`[default]` plus sample `highschool` / `college` / `club` profiles — copy it to
`~/.photohaul` as a starting point.

### Profiles

Profiles let one camera serve multiple contexts (e.g. personal vs. work)
with different rights. The active profile is chosen by, in order: `--profile NAME`,
then a `"profile"` key in the folder's `photohaul.json`, then `[default]`. A folder
with no template stays on `[default]` — so personal shots never pick up the
branded copyright. (A section-less legacy config is read as `[default]`.)

### Client profiles

A profile section may also carry the per-shoot template keys
below (home team, venue, city, state, conference, usage terms…) for a recurring
client or venue. `photohaul --init --profile NAME` then **seeds** the new
`photohaul.json` from them, so stable values aren't retyped each shoot:

```ini
[highschool]
homeTeam  = Union High School
homeShort = Union
venue     = Union High School Gymnasium
city      = Springfield
state     = California
```

This is a one-time copy at `--init`; the JSON is the source of truth thereafter
(those template keys are inert at copy time, where only `creator`/`copyright`/
`credit`/`format` are read from the config). Bare `--init` still seeds from
`[default]`.

## Per-shoot setup — `photohaul.json`

Everything specific to one shoot — the caption, the IPTC fields, and any
capture-time correction — lives in a `photohaul.json` in the destination folder,
auto-detected on ingest. Scaffold one with `photohaul --init` (blank, or seeded
from a profile, above).

### Captions & IPTC fields

The keys you can fill:

```json
{
  "profile": "",
  "sport": "",
  "event": "",
  "homeTeam": "",
  "awayTeam": "",
  "homeShort": "",
  "awayShort": "",
  "venue": "",
  "city": "",
  "state": "",
  "country": "",
  "conference": "",
  "credit": "",
  "source": "",
  "rightsUsage": "",
  "assignment": "",
  "time_shift": "",
  "shot_tz": ""
}
```

Blank fields are omitted (`profile` selects the rights preset; `time_shift` /
`shot_tz` are the capture-time correction below). From these, photohaul writes an
AP-style caption **and** the structured IPTC fields a photo desk / SID workflow
expects, all into the sidecar. What goes in each key, with examples:

| `photohaul.json` key | Example value | Written to |
|----------------------|---------------|------------|
| `profile`     | `college` | selects the `~/.photohaul` rights preset (not written to the sidecar) |
| `sport`       | `women's volleyball` | `photoshop:Headline` + keywords |
| `event`       | `NCAA women's volleyball match` | caption (generic event description) |
| `homeTeam`    | `State University Wolves` | keywords |
| `awayTeam`    | `City College Hawks` | keywords |
| `homeShort`   | `State` | caption (`State vs. City`), headline, keywords |
| `awayShort`   | `City` | caption, headline, keywords |
| `venue`       | `University Arena` | `Iptc4xmpCore:Location` (sublocation) + caption |
| `city`        | `Springfield` | `photoshop:City` + caption |
| `state`       | `California` | `photoshop:State` (full name; caption auto-abbreviates to `Calif.`) |
| `country`     | `USA` | `photoshop:Country` (omitted from the caption) |
| `conference`  | `Example Conference` | keywords |
| `credit`      | `Jane Roe / yoursite.com` | `photoshop:Credit` + caption byline |
| `source`      | `yoursite.com` | `photoshop:Source` |
| `rightsUsage` | `Editorial use only. No resale or commercial use without written permission.` | `xmpRights:UsageTerms` |
| `assignment`  | `Embargoed until 2025-10-04 06:00 PT` | `photoshop:Instructions` (Special Instructions) |
| `time_shift`  | `+2h30m` | capture-time correction (see below) |
| `shot_tz`     | `-04:00` | capture-time correction (see below) |

Three fields are assembled, not typed directly: **Caption** (`dc:description`,
below), **Keywords** (`dc:subject`, from `sport` + team names + `conference`), and
**Date created** (`photoshop:DateCreated`, per frame as ISO 8601 + offset).

The three attribution fields are easy to confuse: **creator** (`dc:creator`,
from the config) is the person who made the photo; **credit** (`photoshop:Credit`)
is how the credit line should read ("Photo by …"); **source** (`photoshop:Source`)
is the owner/agency that holds and licenses the image. **`rightsUsage`**
(`xmpRights:UsageTerms`) is the licensing language that travels with the file, and
**`assignment`** (`photoshop:Instructions`) is the Special Instructions field —
desk/assignment notes, embargoes, client-specific handling.

`city`/`state`/`country` go into the structured fields as typed, so use full
names (e.g. `state: "California"`). The **caption** abbreviates the state to AP
style on its own (`"California"` → `"Calif."`; the eight AP never abbreviates and
anything unrecognized pass through unchanged) — so `photoshop:State` reads
"California" while the caption reads "Calif." `photoshop:DateCreated` is derived
per frame from the (corrected) capture time and offset, so it agrees with the
filename and caption rather than the raw card EXIF. A fully filled template
yields, in `dc:description`:

> Lakeside vs. Riverside, NCAA women's volleyball match, at Memorial Arena, Springfield, Calif. on Friday, Oct. 3, 2025. (Photo by Your Name/yoursite.com)

The date is auto-filled per frame in **AP style** (weekday + abbreviated month,
except March–July spelled out); `credit` falls back to the config `credit`, then
`creator`. This caption is the **folder-level scaffold** — the per-image action
sentence and player IDs (e.g. "Jane Doe (7) goes up for a kill …") stay a manual
Lightroom pass; photohaul writes only what's constant for the shoot.

### Capture-time correction (clock drift & timezone)

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

## Refreshing metadata — `--rewrite`

photohaul writes a sidecar only for files it just **placed** (copied or renamed) — never
for a file already in the folder. Dropping even a minimal sidecar next to a raw you've
already imported and edited in Lightroom makes Lightroom sync from that new (develop-less)
sidecar and revert your catalog-only develop edits, so already-present files are left
strictly alone.

`photohaul --rewrite` refreshes metadata on files **already in the destination** — no card
is needed and nothing is copied. It is **merge-only**: it merges photohaul's fields
(copyright, creator, caption) into a sidecar that **already exists**, preserving everything
else (e.g. Lightroom develop edits), and **skips (and reports) files that have no
sidecar** rather than creating one — again so it can't revert catalog-only edits. An
existing **Purple label is kept as-is**; because lock status is only known from the card,
rewrite never adds or removes a label. An existing sidecar that can't be parsed is reported
and left untouched, never overwritten. `--rewrite` cannot be combined with
`--locked`/`--unlocked`.

## Rename in place — `--local`

For the "I copied a few frames off the card by hand" workflow (e.g. a personal
camera), `photohaul --local` renames camera-named files **already in the
destination** to the timestamp name, **in place** — no card, no copy:

```
$ photohaul --local raf            # operates on the current folder (or --dest)
DSCF1234.RAF -> 20260501-123456_708.raf
```

Files whose names already match the timestamp pattern are left **strictly untouched** —
not renamed, and **no sidecar is written for them** — so it's safe to re-run after
dropping in more photos; only the new ones are renamed and sidecar'd. (Writing a sidecar
next to a raw you've already imported and edited in Lightroom would make Lightroom sync
from that new sidecar and revert your catalog-only develop edits, so `--local` never does
it. `photohaul --rewrite` will *update* a sidecar that already exists, but likewise won't
create one for a file that has none.) A new photo whose timestamp collides with an existing one is kept under
a `…-<number>` suffix (the existing file is never overwritten). Newly renamed files get
create-if-absent sidecars (rights from `~/.photohaul`, caption/IPTC from a
`photohaul.json` if present). A capture-time correction (`time_shift` / `shot_tz`) is
honored, applied to the renamed file via a crash-safe copy-patch-replace. The in-camera **protect bit survives a hand-copy off the card**,
so a locked frame is unlocked in place and Purple-labelled, just as in card mode.
`--local` cannot be combined with `--rewrite`, `--source`, or `--locked`/`--unlocked`.

## Audio notes (voice memos)

Some bodies (Sony A1, various Nikons) record a spoken note as a sidecar `.WAV`
sharing the photo's basename — `A1_02696.ARW` + `A1_02696.WAV`. photohaul detects
that pairing and brings the WAV along with its photo, renamed to the same stable
timestamp (`20260526-140024_708.arw` + `20260526-140024_708.wav`) so the two stay
together in the destination.

The WAV is copied byte-for-byte (card mode) or renamed in place (`--local`); it
carries no Exif, so a `time_shift`/`shot_tz` correction only changes its *name*,
never its bytes, and it gets no `.xmp` sidecar of its own. Like the raw, it's
idempotent (a same-name, same-size WAV is skipped) and never overwrites a
different file at its name. `--rewrite` is metadata-only and ignores audio notes.

## Supported formats

photohaul reads Exif natively — no exiftool, no dependencies:

| Format | Camera | Where the Exif lives |
|--------|--------|----------------------|
| **ARW** / **NEF** / **DNG** | Sony / Nikon / Adobe·Ricoh | TIFF at byte zero |
| **JPG** | any | APP1 segment, read directly |
| **RAF** | Fuji | an embedded JPEG, read transparently |
| **CR3** | Canon | an MP4-style container's `moov` metadata box |

Pick the format with the `format` argument (e.g. `photohaul nef`) or set a
default in `~/.photohaul`. The argument wins when both are present; there is no
built-in default, so with neither set photohaul errors rather than guessing.

## Requirements

Python 3, standard library only — no external packages.

In-camera lock detection is macOS-specific: it reads the BSD `uchg` (immutable)
flag that the exFAT driver maps the camera's protect bit onto.

## Development

No build, no dependencies. Run the test suite with:

```sh
make test          # or: python3 -m unittest discover -s tests
```

The suite is stdlib `unittest` with zero dependencies and no committed binaries.
See [`CONTRIBUTING.md`](CONTRIBUTING.md) for house style and [`AGENTS.md`](AGENTS.md)
for the map of the source and the invariants that must not break.

## Design

Design notes live in [`docs/`](docs/) as dated, per-topic plan documents
(`YYYYMMDD_<topic>_plan.md`), so the directory reads as a chronological record
of how the design evolved.

## License

[MIT](LICENSE) © Joshua Uziel
