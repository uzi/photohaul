# Plan: XMP sidecar writing & merge

Status: implemented (2026-06-05)

## Principle
The copied raw is **immutable** — a byte-for-byte clone of the card file. This
is what makes the size-match skip / idempotent re-run work. Therefore *all*
photohaul metadata (label, copyright, creator, caption) lives in the `.xmp`
sidecar, never in the raw. (Editing EXIF would change the file size and break
the skip — the reason we go sidecar-only.)

## Fields photohaul owns
Inside `rdf:Description`:
- `xmp:Label` — `"Purple"` for locked/featured frames.
- `dc:rights` (rdf:Alt, `x-default`) — copyright ([[20260605_config_file_plan]]).
- `dc:creator` (rdf:Seq) — creator.
- `dc:description` (rdf:Alt, `x-default`) — caption
  ([[20260605_caption_and_template_plan]]).

## Merge (option B)
We **merge**, never blanket-overwrite:
- Parse the existing sidecar with stdlib `xml.etree.ElementTree`, registering
  the known namespaces (`x`, `rdf`, `xmp`, `dc`).
- Set/replace **only the four properties above** within `rdf:Description`;
  leave everything else intact — crucially any Lightroom develop edits (`crs:`)
  and other namespaces.
- Handle a property appearing either as an attribute or as a child element.
- Re-emit the standard `<?xpacket?>` wrapper around the output.
- Write to a temp file, then atomic-rename over the sidecar.

## Safety rules
- **Never clobber what we can't parse.** If an existing sidecar fails to parse,
  warn and skip it — do not overwrite.
- create-if-absent on a normal haul; full re-apply (still a merge) only under
  `--rewrite`.

## Known, accepted limitation
A merge **re-serializes** the XMP, so Adobe's exact whitespace padding and any
comments are not preserved (the `xpacket` wrapper is regenerated). XMP readers
key on namespace URIs, not formatting, so this is semantically safe — but it is
a real cosmetic change to sidecars Lightroom has already written. Accepted
trade-off (confirmed 2026-06-05).
