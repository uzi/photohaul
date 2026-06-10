# Plan: Windows and Linux support

Status: implemented (2026-06-10)

> **Revision (2026-06-10):** the Linux **lock-detection** piece (the FAT
> `ioctl` described in §1/§2 below) was deliberately *not* shipped. It was the only
> path CI can't exercise with a real protect bit (it needs a FAT mount), and the
> lock→Purple workflow exists to feed Lightroom, which doesn't run on Linux — so the
> value didn't justify carrying un-CI-testable code. On Linux `is_locked` is now a flat
> `return False` and `clear_lock` just ensures the file is writable (`chmod +w`).
> Everything *else* in this plan shipped as written: Linux/Windows now run the full
> pipeline (discovery, copy, rename, sidecars, captions), CI covers all three OSes, and
> macOS/Windows detect a real lock. The §1/§2 ioctl details are kept below as the record
> of the road not taken.

photohaul is macOS-only today, for one reason: in-camera lock detection reads the
BSD `UF_IMMUTABLE` flag (`os.stat(...).st_flags`), which exists only on macOS/BSD.
On Linux and Windows `st_flags` is absent, so `getattr(st, 'st_flags', 0)` is always
0 and `is_locked` silently returns `False` — every frame looks unlocked, nothing
gets Purple-labelled, and `--locked`/`--unlocked` filter nothing. Everything else
(Exif reader, naming, sidecars, captions, copy) is already pure stdlib and portable.

The card is always some flavour of **FAT/exFAT**, and the camera's "protect" simply
sets the FAT directory entry's **read-only attribute** (`ATTR_RO`, bit `0x01`). That
one bit is the ground truth on every OS — each just surfaces it through a different
API. So this is, as suspected, mostly a `sys.platform` dispatch around lock
detection — plus three smaller portability fixes that fall out of it.

Builds on the lock-detection invariant from [[20260604_initial_plan]] and the
unlock-in-place behaviour from [[20260606_local_mode_plan]]; must preserve the
read-only-card invariant in `AGENTS.md`.

## The four platform touchpoints

1. **Lock detection** (read) — `is_locked`, the crux. (`src/photohaul.py:462`)
2. **Clearing the lock** (write, *only ever on files we placed/renamed*, never the
   card) — `copy_file`'s `os.chflags(dest, 0)` (`:961`) and `--local`'s
   `os.chflags(f.src, 0)` / audio unlock (`:1448`, `:1480`).
3. **Card auto-discovery** — `find_card` scans `/Volumes` (`:472`).
4. **Write-path portability audit** — `os.rename` / `os.replace` / `os.fsync` /
   `os.chmod` / filename charset (mostly already fine; verify per OS).

Nothing in the Exif reader, XMP writer, caption builder, or naming needs to change.

## 1. Lock detection — the dispatch

The camera sets FAT `ATTR_RO` (`0x01`). Surfaced as:

| OS | API | Detect |
|----|-----|--------|
| macOS | `st_flags` (msdosfs/exfat maps `ATTR_RO` → `UF_IMMUTABLE`) | `st.st_flags & UF_IMMUTABLE` — *current code* |
| Windows | `st_file_attributes` (native DOS attribute) | `st.st_file_attributes & FILE_ATTRIBUTE_READONLY` |
| Linux | `ioctl(FAT_IOCTL_GET_ATTRIBUTES)` on the vfat/exfat driver | returned attr `& 0x01` |

`stat.UF_IMMUTABLE` and `stat.FILE_ATTRIBUTE_READONLY` are both in the stdlib `stat`
module (the latter Windows-only); the Linux ioctl is hand-rolled:

```python
FAT_IOCTL_GET_ATTRIBUTES = 0x80047210   # _IOR('r', 0x10, __u32)
ATTR_RO                   = 0x01
# locked:
import array, fcntl
buf = array.array('I', [0])
with open(path, 'rb') as f:
    fcntl.ioctl(f.fileno(), FAT_IOCTL_GET_ATTRIBUTES, buf, True)
locked = bool(buf[0] & ATTR_RO)
```

The in-kernel `exfat` driver (Linux 5.7+) implements the same FAT ioctls, so one
path covers both vfat and exfat. If the ioctl raises `OSError` (ENOTTY — the source
isn't on a FAT mount, e.g. someone pointed `--source` at a normal dir), treat it as
**unlocked** rather than crashing; lock semantics only exist on a real card.

### Signature change

`is_locked(st)` becomes `is_locked(path, st)` — the Linux ioctl needs an fd, the
other two only the stat. Both callers (`scan`, `scan_local`) already hold both `src`
and `st`, so the one-stat-for-size-and-lock optimization is preserved. Bind the
implementation once at import:

```python
if sys.platform == 'darwin':
    def is_locked(path, st): return bool(getattr(st, 'st_flags', 0) & stat.UF_IMMUTABLE)
elif sys.platform == 'win32':
    def is_locked(path, st): return bool(getattr(st, 'st_file_attributes', 0)
                                         & stat.FILE_ATTRIBUTE_READONLY)
else:
    def is_locked(path, st): ...ioctl, OSError -> False...
```

## 2. Clearing the lock (on our own files only)

A `clear_lock(path)` helper replaces the raw `os.chflags(..., 0)` calls. Still only
ever called on a file we just **copied** (card mode) or are **renaming** (`--local`)
— never the card.

| OS | Clear |
|----|-------|
| macOS | `os.chflags(path, st.st_flags & ~UF_IMMUTABLE)` (or `0`) |
| Windows | `os.chmod(path, st.st_mode \| stat.S_IWRITE)` — on Windows `chmod` *only* toggles the read-only bit, so this clears it natively |
| Linux | `ioctl(FAT_IOCTL_SET_ATTRIBUTES)` clearing `ATTR_RO` *if on FAT*; else `os.chmod(path, mode \| S_IWUSR)` |

Note the current `copy_file` already follows `chflags` with
`os.chmod(dest, ... | S_IWUSR)` and wraps the flag clear in `try/except OSError`.
Two fixes: (a) `os.chflags` doesn't *exist* on Windows — referencing it raises
`AttributeError`, not `OSError`, so the bare call must move behind the dispatch /
`hasattr(os, 'chflags')`; (b) in card mode the freshly-written copy lands on a normal
disk and usually carries no DOS attribute at all, so the clear is belt-and-suspenders
there — but in `--local` the hand-copied file may genuinely retain it (see caveat),
so `clear_lock` must actually work per-OS, not just on macOS.

## 3. Card auto-discovery

`find_card` (and the `--source` fallback) gains a per-OS default search root; the
existing "dir containing `DCIM/`" test is unchanged, only *where* it looks:

| OS | Search roots (best-effort) |
|----|----------------------------|
| macOS | `/Volumes/*` — *current* |
| Linux | `/run/media/$USER/*`, `/media/$USER/*`, `/media/*`, `/mnt/*` |
| Windows | drive letters `C:\..Z:\` (probe each root for `DCIM\`), via `ctypes.windll.kernel32.GetLogicalDrives` or a plain `os.path.isdir` probe |

Auto-detect is inherently fuzzier off macOS (Linux mount points vary by distro/
desktop; Windows cards may or may not have a `DCIM`). The "zero or several candidates
→ tell the user to use `--source`" logic already handles ambiguity gracefully, so the
honest fallback everywhere is **`--source`**. Update the `--help`/README example that
hardcodes `/Volumes/CARD`.

## 4. Write-path portability audit

Mostly already portable; confirm and tidy:

- **`os.replace(tmp, sidecar)`** — atomic on all three. ✓
- **`os.rename(partial, dest)`** — POSIX silently replaces; Windows raises if dest
  exists. We already guard with an `os.path.exists(dest)` check + abort immediately
  before, so dest never exists at rename time. ✓ (keep the guard; it doubles as the
  no-clobber invariant.)
- **`os.fsync`** — works on file handles on all three. ✓
- **Filenames** `YYYYMMDD-hhmmss_mmm.ext` — no `:\*?"<>|`, Windows-safe. ✓
- **`\r` progress line** — fine in modern Windows Terminal / conhost. ✓ (`IS_TTY`
  already gates it.)

No structural change expected here — this is a verification pass, captured so the
implementer ticks each box on each OS.

## Testing

- **Abstract lock-setting in tests.** Replace the `chflags`/`UF_IMMUTABLE` calls and
  the `@skipUnless(darwin)` guards (`tests:1008`, `:1211`, plus the cleanup at `:157`)
  with a `set_lock(path)` test helper mirroring `clear_lock`. The lock→Purple tests
  then run on whatever OS hosts CI instead of skipping off macOS.
- **Real-attribute coverage by OS:**
  - *Windows*: `FILE_ATTRIBUTE_READONLY` is a generic attribute (works on NTFS), so
    `os.chmod(path, stat.S_IREAD)` sets a real, detectable lock on the normal CI disk.
    Full real coverage. ✓
  - *macOS*: `chflags UF_IMMUTABLE` on the CI disk — as today. ✓
  - *Linux*: the FAT ioctl only works on an actual FAT mount, which ext4 CI isn't. Two
    options: (a) a loopback FAT image (`dd` + `mkfs.vfat` + `mount`, set the bit with
    `fatattr +r`) in an integration test gated on `mount` privileges, or (b) unit-test
    the dispatch with a **mocked `fcntl.ioctl`** and document that the real-FAT path is
    verified manually against a card. Recommend (b) for unit speed + a (a) smoke test.
- **CI matrix:** expand `.github/workflows/test.yml` from `macos-latest` to
  `os: [macos-latest, ubuntu-latest, windows-latest]` × the Python matrix. Still zero
  install steps.

## Docs to update when this lands

- `AGENTS.md` — the read-only-card invariant's lock-detection note (currently
  "`st_flags & UF_IMMUTABLE` … verified on Sony and Fuji") becomes per-OS; add the
  FAT `ATTR_RO` ground-truth framing.
- `README.md` — "Requirements" currently says lock detection is macOS-specific; the
  `/Volumes/CARD` example; a short per-OS auto-detect / `--source` note.
- `--help` epilog — the `--source /Volumes/CARD` example.

## Caveats / open questions

- **`--local` lock retention is OS-dependent.** The in-camera protect bit survives a
  hand-copy off the card differently per OS: macOS preserves it (as `UF_IMMUTABLE`),
  Windows preserves `FILE_ATTRIBUTE_READONLY` onto NTFS, but a plain `cp` of a FAT
  read-only file onto **ext4 loses the DOS attribute** (it has nowhere to live, and at
  best maps to a stripped write-permission bit). So `--local` lock→Purple is reliable
  on macOS/Windows and **best-effort on Linux**. Document it; don't pretend otherwise.
- **`ATTR_RO` is the whole story.** No camera I know of uses any other FAT attribute
  bit for protection, so detection stays a single-bit test on every platform.
- **No new dependencies** — `fcntl`/`array` (Linux) and `ctypes` (Windows drive
  enumeration, optional) are all stdlib. Invariant intact.
- Should auto-discovery on Linux honour `$XDG_RUNTIME_DIR`-style paths or just the
  common four roots above? Lean simple; `--source` is the escape hatch.
