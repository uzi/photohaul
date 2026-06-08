# Plan: voice-memo WAV sidecars (audio notes)

Status: implemented (2026-06-08)

Sony bodies (verified on the A1) and Nikon record an in-camera **audio note** as a
sidecar `.WAV` file sharing the photo's basename: `A1_02696.ARW` + `A1_02696.WAV`. The
WAV is a plain RIFF/WAVE clip, no Exif. When we ingest the photo, the audio note should
ride along — copied/renamed to the photo's stable timestamp name so the pair stays
together in the destination (`20260608-140024_708.arw` + `20260608-140024_708.wav`).

Builds on the naming/idempotency rules of [[20260604_initial_plan]] and the rename-in-place
flow of [[20260606_local_mode_plan]].

## Behavior
- **Detection** is format-agnostic and by stem: for each scanned photo, `audio_sibling()`
  looks for `<stem>.WAV`/`<stem>.wav` next to it. A photo with no sibling is unaffected.
- **The WAV follows its photo**: its destination is the photo's final (collision-resolved,
  capture-time-corrected) base name + `.wav`. It carries **no Exif** (nothing to patch,
  even under `time_shift`/`shot_tz` — only the name tracks the photo) and **no XMP
  sidecar**. A locked photo's Purple label stays on the photo only.
- **Filter-following**: the WAV is attached to the `Frame`, so `--locked`/`--unlocked`
  carry it along with the photo it belongs to.
- **Idempotent**, like the raw: the WAV dest is classified independently
  (`audio_to_copy` / `audio_to_skip` / `audio_conflicts`) by presence + matching size, so
  a present same-size WAV is skipped and a *different* file at the name is never
  overwritten (reported as a conflict). The audio note is only placed when its photo's
  dest exists (placed this run or already present), so a failed photo copy never strands a
  lone audio note.

## Mode interactions
- **Card mode**: the WAV is copied byte-for-byte off the read-only card via `copy_file`
  (no-clobber + fsync), counted in the progress totals.
- **`--local`**: the camera-named WAV sits next to the camera-named raw; it is **renamed
  in place** to match (plain `os.rename`, never patched). A locked WAV is unlocked first
  (the folder is ours). Already-timestamp-named photos are left strictly alone, so their
  audio notes (already renamed in a prior run) are not reconsidered.
- **`--rewrite`**: card-free, metadata-only — raws and WAVs are already in place, so audio
  is not handled at all (`scan_dest` sets no `audio_src`).

## Invariants preserved
- Card read-only (card mode); WAV is only read.
- Re-runs idempotent; the WAV dest derives purely from the photo's intrinsic name.
- Byte-exact clone (card mode) / rename-only (`--local`) — the WAV is never modified.
- We never overwrite a different file at a WAV's name.

## Out of scope
- Orphan WAVs (no matching photo on the card) are not ingested — we only scan the photo
  extension. An audio note only exists alongside a frame.
