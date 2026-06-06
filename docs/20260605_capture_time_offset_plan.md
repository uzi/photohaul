# Plan: capture-time correction (clock drift + timezone)

Status: planned (2026-06-05)

Builds on the timestamp-driven naming in [[20260604_initial_plan]] and the
per-folder template in [[20260605_caption_and_template_plan]]. Relaxes, in one
narrow place, the "byte-exact clone" guarantee from [[20260604_initial_plan]].

## Purpose
Two related problems, one machinery:

1. **Clock drift** — the camera clock is simply wrong (battery reset, slow
   drift). Fix the displayed wall-clock time; the real instant genuinely changes.
2. **Timezone / travel** (the main use case) — you fly to another zone and
   *neglect the camera's clock*, so every frame is stamped in home wall-clock
   time with the home offset. You want the frames to read as *destination-local*
   while keeping the real instant.

Both apply a single correction per card/folder to the destination filename, the
caption/rights date, and the copied raw's embedded EXIF. The card is still
**never touched** — only the destination *copy* is corrected.

The A1 carries the fields we need (probed on the card, 2026-06-05):

```
DateTime (IFD0 0x0132)        '2026:05:30 12:12:56'   20 bytes
DateTimeOriginal (0x9003)     '2026:05:30 12:12:56'   20 bytes
DateTimeDigitized (0x9004)    '2026:05:30 12:12:56'   20 bytes
SubSecTimeOriginal (0x9291)   '967'                    4 bytes
OffsetTime (0x9010)           '-07:00'                 7 bytes
OffsetTimeOriginal (0x9011)   '-07:00'                 7 bytes
OffsetTimeDigitized (0x9012)  '-07:00'                 7 bytes
```

Every date field is fixed-width 20-byte ASCII; every offset is `±HH:MM` (always
6 chars, 7 with the null). So the same same-length in-place overwrite is safe for
both sets — no IFD restructuring, no MakerNote pointer touched.

## Two corrections, composable (the layered model)
Two **orthogonal** folder-level knobs in `photohaul.json`. They are *not*
mutually exclusive — they compose. The thing to keep clear in docs is what each
means so you never double-count.

```jsonc
"time_shift": "+2h"      // DRIFT: wall-clock-only nudge of the date fields.
                         //   Offset tags untouched. Changes the absolute instant
                         //   (because the old clock was genuinely wrong).
"shot_tz":    "-04:00"   // TIMEZONE: "these were actually shot at this UTC offset."
                         //   zone_delta = target - recorded OffsetTimeOriginal.
                         //   Shifts the date fields by zone_delta (instant-PRESERVING)
                         //   AND restamps the three offset tags to target.
```

- **`shot_tz` alone is the complete travel fix.** It derives the wall-clock shift
  from the camera's recorded offset, so you give one human input ("I was in
  `-04:00`") and it both moves the displayed time and corrects the zone. You do
  *not* also set `time_shift`. Worked example: home `-07:00`, fly 3 zones east,
  shoot 3:00pm local → camera stamps `12:00 / -07:00` → `shot_tz: "-04:00"` gives
  `zone_delta = (-04:00) − (-07:00) = +3h` → `15:00 / -04:00`, same instant
  (`19:00 UTC`).
- **Composition.** When both are set, the date fields shift by
  `zone_delta + time_shift`, and the offset tags are set to `shot_tz`'s target.
  Use this for the rare single-body case where you traveled *and* the clock was
  also independently drifting.
- **Derivation assumption** (exactly the "neglected the clock" case): `shot_tz`
  trusts the recorded `OffsetTimeOriginal` as the zone the camera *thought* it was
  in. If you'd half-changed the camera (bumped the hour but not the area), that
  basis is unreliable — use explicit `time_shift` instead.

## Format & validation
- `time_shift` — signed, units `d`/`h`/`m`/`s`, combinable (`+2h30m`, `-15s`,
  `90m`). **Whole-second granularity only** so `SubSecTimeOriginal` and the
  `_mmm` filename key are never disturbed.
- `shot_tz` — strict `±HH:MM` (`-04:00`, `+05:30`, `+00:00`).
- Empty/absent → that correction is off. Both absent → behaves exactly as today.
- **No CLI flags in v1** — keeping corrections only in the file is what makes
  re-runs idempotent (a flag you forget would silently rename everything).
  `--init-template` prefills blank `"time_shift": ""` and `"shot_tz": ""`.
- Parsed/validated **once at startup, before any copy**. Bad input → hard error,
  exit nonzero. `shot_tz` set but the frame lacks `OffsetTimeOriginal` (nothing to
  derive from) → hard error.

## Data flow — one corrected timestamp drives everything
In `Haul.scan()`, read the card's original time *and* its recorded offset, compute
the total date delta, and store the **corrected** string on the `Frame`:

```
dt, sub, rec_off = read_exif_datetime(src)        # original + recorded offset, from card
zone_delta = parse_tz(shot_tz) - parse_tz(rec_off) if shot_tz else 0
date_delta = zone_delta + time_shift
dt = shift_exif_datetime(dt, date_delta)           # corrected wall-clock
base = base_name(dt, sub)                           # filename uses corrected time
Frame(src, size, locked, dest, captured=dt)         # caption + {year} use corrected time
```

`fields_for` (caption date, `{year}`) needs **no change** — it inherits the
corrected `Frame.captured`.

## In-place EXIF patch of the copy
- **Date fields** (shifted by `date_delta`, each if present): `DateTimeOriginal`
  (0x9003), `DateTimeDigitized` (0x9004), IFD0 `DateTime` (0x0132). Shift each
  individually (preserves any rare per-field difference).
- **Offset tags** (set to `shot_tz` target, only when `shot_tz` is set; each if
  present): `OffsetTimeOriginal` (0x9011), `OffsetTime` (0x9010),
  `OffsetTimeDigitized` (0x9012).
- **Why it's safe:** every field is fixed-width ASCII stored at an offset pointer
  the parser already locates; we `seek()` and overwrite the exact same byte length
  (19 of 20 for dates, 6 of 7 for offsets). No IFD offset moves; no Sony MakerNote
  pointer is invalidated; the file is never re-serialized. "Same size" is the
  symptom — "no structural rewrite" is the reason it's safe.
- **Patch the `.partial` *before* the atomic rename**, never after. This keeps the
  invariant *a file at its final path is always fully corrected*; patching
  post-rename then failing would strand an unpatched file that the size-match skip
  rule keeps forever. New `copy_file` order: write `.partial` → fsync → size check
  → **patch `.partial`** → rename → chflags/chmod. Same-length patch, so the
  existing `total != src_size` guard still holds and dest size still equals card
  size (so `classify()`'s skip/conflict logic is unchanged).

## Hard-error policy
A present field of unexpected type/width, an unparseable stored value, or
`shot_tz` with no recorded offset to derive from, raises a dedicated
`ExifPatchError`. Unlike the per-file `OSError` handler in `copy()` (log and
continue), this **propagates and aborts the run**, naming the file; the `.partial`
is removed so no bad file is left behind. Frames already renamed earlier in the
run are correct and stay; a re-run retries the failed frame.

## Idempotency
Both corrections are deterministic from **card-original** values (the recorded
offset and original time are intrinsic; the deltas are constants), so every run
derives the same corrected name → existing file, size matches → skip, no
re-patch.

Caveat to document: **set the corrections before the first ingest of a folder.**
Changing them after files exist yields new names → duplicates, not updates;
changing a correction means re-ingesting that folder cleanly.

## `--rewrite` interaction (correctness)
`--rewrite` is card-free and reads EXIF from the **already-copied** destination
files, which already carry the corrected time *and* offset. So rewrite must
**ignore both `time_shift` and `shot_tz`** (apply zero, patch no raws) — otherwise
it double-shifts. `main()` forces the applied corrections to zero on the rewrite
path; it still reads the JSON for caption content.

## The relaxed guarantee
This is the one place we intentionally relax "the raw stays a byte-exact clone of
the card original": the copy's date/offset bytes differ from the card. So
checksum-vs-card verification won't match, and we write into the raw rather than a
throwaway sidecar. Mitigation: the **card stays read-only and pristine**, and we
patch only the destination copy after the crash-safe copy — so any offset-math bug
is fully recoverable by deleting the copy and re-ingesting.

## Code touch points (`src/photohaul.py`)
- `parse_time_shift(s) -> timedelta` and `parse_tz(s) -> timedelta` (with a
  normalized `±HH:MM` formatter) — grammar + validation; hard error on bad input.
- `shift_exif_datetime(dt_str, delta) -> dt_str` — parse `YYYY:MM:DD HH:MM:SS`,
  add delta (handles midnight/date rollover), reformat to identical width.
- `read_exif_datetime` also returns the recorded `OffsetTimeOriginal` (0x9011).
- Lift the nested `read_ifd` / `ascii_val` out of `read_exif_datetime` to module
  scope so the patcher reuses one parser and can expose each field's **file offset
  + length**. (Also the groundwork for reading `Model`/`BodySerialNumber` later —
  see Non-goals.)
- `shift_exif_in_place(path, date_delta, target_offset_or_None)` — walk IFD0 +
  EXIF IFD; shift the present date fields by `date_delta`; if a target offset is
  given, overwrite the present offset tags with it; validate width/type; raise
  `ExifPatchError` on anomaly; return what was patched.
- `copy_file(...)` — accept `date_delta` + target offset; patch the `.partial`
  pre-rename.
- `Haul` — store both corrections; compute `zone_delta`/`date_delta` in `scan()`;
  pass to `copy_file`; force zero on the rewrite path; add a
  `Time: +3h (tz -04:00)` line to `report_plan`.
- `main()` — load + validate `time_shift` and `shot_tz` from the template dict;
  zero them under `--rewrite`.
- `--init-template` scaffold, `--help` epilog, and `README.md` — document both
  keys, formats, the set-before-first-ingest caveat, that `shot_tz` alone is the
  full travel fix, and that the raw's EXIF is corrected in place.

## Non-goals (noted for later)
- **Multi-body / per-serial drift sync.** Inter-camera drift (body A vs body B
  clocks out of sync at the same event) is inherently *per-camera*, so it does not
  belong in a folder-level knob — a folder-wide `time_shift` would nudge both
  bodies equally and never fix their *relative* offset. Its home is a future
  per-serial offset layer (with a `--camera-info` flag to read
  `BodySerialNumber`). That layer **composes on top** of folder-level `shot_tz` /
  `time_shift`: e.g. folder `shot_tz: -04:00` (+3h to all frames) plus per-serial
  `[camera:SERIAL_B] shift=+4s` → body A frames get +3h, body B frames get +3h+4s.
  The module-level IFD refactor above is the groundwork.
- **Embedded preview JPEG's** own EXIF is not patched in v1 — Lightroom uses the
  main metadata.

## Validation before done
1. Unit: `parse_time_shift` / `parse_tz` accept/reject; `shift_exif_datetime`
   rollover and negative deltas; `zone_delta` arithmetic incl. half-hour zones
   (`+05:30`).
2. Drift round-trip on a real ARW: ingest `time_shift: +Xh`, re-read
   0x9003/0x9004/0x0132 → each shifted by X, **offset tags unchanged**, file size
   unchanged, header + MakerNote intact, filename reflects the shift.
3. Timezone round-trip: ingest `shot_tz: -04:00` against the `-07:00` card → date
   fields +3h, all three offset tags now `-04:00`, absolute instant preserved,
   file size unchanged.
4. Composition: `time_shift` + `shot_tz` together → date fields shifted by the
   sum, offsets set to target.
5. Idempotency: second run skips, no double-shift, size matches.
6. `--rewrite` after a corrected ingest: caption date / offset not re-applied.
7. Hard error: corrupted date field, or `shot_tz` with no recorded offset → run
   aborts, `.partial` removed, no final file.
