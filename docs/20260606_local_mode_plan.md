# Plan: --local mode (rename-in-place, no card)

Status: implemented (2026-06-06)

**Amendment (2026-06-07):** the original design wrote create-if-absent sidecars for
already-named frames too (the "missing-sidecar gap-fill" below). That backfired: dropping
even a minimal rights-only sidecar next to a raw already imported and edited in Lightroom
made LR sync from the new (develop-less) sidecar and **revert catalog-only develop edits**
(it also induced LR to start writing `.xmp`/`.acr` mask files for those frames). So
`--local` now writes sidecars for the **renamed (placed) frames only**; already-named
frames are left strictly alone (no rename, no sidecar). The gap-fill case is intentionally
dropped — use `--rewrite` to (re)write sidecars on files already in place. The
create-if-absent passages below describe the original behavior and are superseded by this.

A third mode for the "I already copied a few frames into a folder by hand" workflow
(X100VI, Ricoh GR), preserving what the old `~/bin/rawsort.py` did: rename camera-named
files in a folder to the stable timestamp name **in place**, no card, no copy. Unlike
rawsort it also writes the XMP sidecars (rights from `~/.photohaul`, caption/IPTC from
`photohaul.json` if present). Builds on the naming/idempotency rules of
[[20260604_initial_plan]] and supports capture-time correction
([[20260605_capture_time_offset_plan]]) via a crash-safe copy-patch-replace (see below),
since the X100VI/GR are travel cameras where the timezone fix matters most.

## What rawsort.py did
Glob `*.EXT` (uppercase = fresh from camera), read `DateTimeOriginal` via exiftool,
`os.rename` each to `YYYYMMDD-HHMMSS.ext` (lowercase), letter-suffix (`a`,`b`,…) on a
same-second collision. Already-renamed lowercase files were skipped because the glob only
matched the uppercase extension. No sidecars, second precision, exiftool dependency.

## Verdict: does it throw things on its head?
**No — it reuses the seam `--rewrite` already established.** `--rewrite` proved the
pattern: a card-free mode with its own scanner (`scan_dest`) and no copy step, reusing
`classify`/`fields_for`/`write_metadata`/sidecar wholesale. `--local` is the same shape:
its own scanner (`scan_local`), and a *rename* instead of a *copy*. The dataclasses don't
change — `Frame` already carries distinct `src` and `dest`; in local mode they're two
names in the **same** directory.

The real cost is **conceptual, in the invariants**, not structural: photohaul stops being
"copy off a read-only card" exclusively and gains "rename files in place." Three invariant
statements must be made mode-aware (see *Invariants* below). That is the honest "head"
impact — the code is additive; the doc's promises widen.

## Mode model & CLI
Today `rewrite` is a bool. Adding `local` makes three modes with pairwise-invalid combos.
Recommended (lowest blast radius, mirrors how `rewrite` landed): add a `local` bool and
guard the invalid combinations in `main`, exactly like the existing `--rewrite` guards.
(If a 4th mode ever appears, collapse the bools into a single `mode='card'|'local'|'rewrite'`
field — three modes is the threshold where the enum starts paying off, but converting now
touches ~8 `self.rewrite` sites for no user-visible gain.)

- `--local` operates on `--dest` (default: cwd). **Source = dest.** No `find_card`.
- Mutually exclusive with `--source`, `--rewrite`, and `--locked`/`--unlocked` (the
  `--locked`/`--unlocked` *filters* select a subset off a card; there is no card to filter
  here). Error on combo, mirroring the existing `--rewrite` + filter guard. Note the lock
  *workflow* still applies: the in-camera protect bit survives a hand-copy off the card, so
  a camera-named locked frame is detected, unlocked in place, and marked Purple — see below.
- Extension still required (positional or `format=` in config), same as card mode.
- `--dry-run` reports the planned renames + sidecars, touching nothing.

## scan_local — the new scanner
Effectively `scan_dest` (read folder files' EXIF for metadata) **plus** rename
computation. List the dest dir (non-recursive — it's a working folder, not a DCIM tree)
for files whose extension matches (case-insensitive). For each, read Exif; on failure,
report and skip (leave the file untouched).

Split by the **idempotency key — does the stem already match the timestamp pattern**
`^\d{8}-\d{6}(_\d{3})?(-\d+)?$`:
- **Already-named** (matches) → `dest = src` (trust the name; no rename). Included as a
  frame anyway so its sidecar gets create-if-absent attention (closes the gap where a
  prior run renamed a file but was interrupted before writing its sidecar).
- **Camera-named** (doesn't match) → compute `dest` from corrected-free capture time via
  the existing `base_name(dt, sub)`; this frame is a rename candidate.

This is case-independent — more robust than rawsort's uppercase/lowercase heuristic
(which still happens to hold: timestamp names are lowercase digits).

**Lock bit (added 2026-06-07).** The in-camera protect bit (uchg/immutable) survives a
hand-copy off the card, so a *camera-named* frame is checked with `is_locked(st)`: a locked
one sets `Frame.locked`, bumps `featured`, and `relocate` clears its flags (`os.chflags(src, 0)`)
before the rename/remove — without which `os.rename`/`os.remove` fail with EPERM (the original
bug: a hard error instead of unlock+Purple). `Frame.locked` then carries the Purple label into
the sidecar via `fields_for`, exactly as in card mode. **Already-named** frames are the one
exception: left strictly alone (name, bytes, *and* lock bit), preserving re-run idempotency —
in practice they were renamed by a prior photohaul run and are already unlocked + Purpled.

## Collision handling (deterministic, cross-run)
Reuse the existing `used_names` + `dsc_number` disambiguator for within-run collisions,
**and** a `_name_taken` check against files already on disk. A new frame that maps onto a
name taken by an earlier frame *or* an existing file becomes `…-<dscnumber>.ext` (intrinsic,
deterministic across runs). This is what keeps a genuinely new photo that shares a timestamp
with an older one — **both are kept**, the existing file untouched. Do **not** use rawsort's
`a`/`b` letter scheme — encounter-order suffixes aren't deterministic and would violate the
"names derive purely from intrinsic capture time, stable across runs" invariant. The
X100VI/GR record sub-second, so true collisions are rare anyway; this is the safety net.
Critically, because every camera file is given a *non-existing* dest at scan time, the
relocate step never has to overwrite.

## The "do" step: relocate (rename, or copy-patch-replace under correction)
A new `relocate()` replaces `copy()` for local mode, acting on the to_copy (to-rename)
set. scan_local already guaranteed each dest does not exist (collisions were suffixed
there), so relocate never overwrites:

- **No correction** → atomic `os.rename(src, dest)`. No temp file, no byte movement; an
  interrupted rename leaves the original in place (never a partial), so no orphan is even
  possible.
- **Correction set** → reuse `copy_file`'s machinery: write a patched `.partial` beside
  dest (copy bytes, fsync, `shift_exif_in_place` — a same-length overwrite), atomic-rename
  `.partial` → dest, then **remove the original src**. This is the *one planned byte
  exception*, identical to card mode's. The only crash window is between the (complete,
  correct) dest and the src removal; a re-run then renames the leftover original (auto-
  suffixed) and removes it — self-healing into a harmless duplicate, never an overwrite or
  data loss. (No dedicated orphan-cleanup path is needed, and adding one keyed on size
  would be unsafe — two distinct raws can share a size.)

`classify` is reused unchanged: to_copy = camera files to rename; to_skip = already-named
files (`src == dest`, a no-op kept only for sidecar create-if-absent); conflict is a rare
backstop (a *suffixed* dest somehow already present at a different size) — reported, never
overwritten, as in card mode.

## Metadata — unchanged
`fields_for` / `_iptc_fields` / `write_metadata` are reused verbatim, **create-if-absent**
(`merge=False`, like card mode — not `--rewrite`'s merge). A re-run never clobbers a
sidecar you've since edited in Lightroom. Personal folders with no `photohaul.json` and no
`[default]` rights write minimal/no sidecars — consistent with today's "personal shots stay
minimal."

## Idempotency
Guaranteed by the name-pattern key: after a run, renamed files match the timestamp pattern,
so a re-run treats them as already-named (no rename) and only create-if-absent their
sidecars. Dropping new camera files and re-running renames just those. No state file needed.

## Invariants — the amendments (the actual impact)
`CLAUDE.md` must make these mode-aware:
- **"The card is read-only."** → add: *in card mode.* In local mode there is no card;
  files in the working folder are **renamed in place** (bytes preserved, never copied or
  duplicated).
- **"The raw is a byte-exact clone of the card original."** → in local mode there is no
  clone; the raw **is** the original, renamed — bytes untouched **unless** a capture-time
  correction is set, in which case the EXIF date/offset fields are patched (same-length
  overwrite), exactly the one planned exception card mode already allows.
- **"Re-runs are idempotent."** → restate the key: card mode skips a present file of
  matching size; local mode skips a file whose name already matches the timestamp pattern.
- **`--rewrite` is card-free** section gains a sibling note that `--local` is also
  card-free but mutates names (rewrite never renames).

## Capture-time correction (in v1, via copy-patch-replace)
Supported, because the travel cameras are where the tz fix matters. A bare rename changes
no bytes, so a corrected *name* with uncorrected *EXIF* would disagree; the relocate step
therefore switches to copy-patch-replace whenever `time_shift`/`shot_tz` are set (see *The
"do" step*): patched `.partial` → fsync → atomic-rename over dest → remove original. Same
crash-safety as `copy_file`, at the cost of momentary double disk for the one file in
flight. The dest name and the patched EXIF then agree, just as in card mode. Corrections
are still configured only in `photohaul.json` (no CLI flag), so `--local` reads them the
same way card mode does — the existing `main` parsing/validation is reused unchanged.

## Code touch points (`src/photohaul.py`)
- `Haul`: add `local: bool` field; `scan()` dispatches to `scan_local()` when set (as it
  already does to `scan_dest()` for rewrite).
- New `scan_local()` (above) and `relocate()`/local branch in `run()` (mirror `run_rewrite`).
- `report_plan()`: a `Local: <dir>` header branch.
- `main`: resolve mode; guards for `--local` vs `--source`/`--rewrite`/filter; source =
  dest; skip `find_card`. Capture-time parsing is reused as-is (corrections are supported).
- `build_parser`: `--local` flag; epilog example + note.
- `README.md`, `CLAUDE.md`: the invariant amendments + a `--local` usage section.

## Validation
1. Folder with mixed `DSCF*.RAF` (uppercase) + already-renamed `2026…raf`: `--local`
   renames only the camera files; re-run is a no-op; sidecars created for all.
2. `--dry-run --local` reports renames + sidecar counts, changes nothing.
3. Two same-second camera files → distinct `…-<dscnum>` names, stable across a second run.
4. A computed name colliding with an existing different-size file → reported conflict, not
   overwritten.
5. Unreadable-Exif file → reported, left untouched (not renamed).
6. `--local` with `--source`/`--rewrite`/`--locked` → clean error.
7. `--local` with `time_shift`/`shot_tz` set: camera files are relocated with EXIF patched
   (dest name + patched EXIF agree, size unchanged), and the original src is removed.
8. Missing-sidecar gap-fill: delete an already-named file's sidecar → re-run recreates it
   (create-if-absent on already-named frames).
9. Auto-suffix: a new camera file colliding with a *different* existing on-disk timestamp
   file → both kept (`…-<dscnum>`), the existing file's bytes untouched.

## Non-goals (v1)
- Recursive folder walk (local is a flat working folder).
- Moving/renaming pre-existing sidecars of camera-named files (fresh files have none).
