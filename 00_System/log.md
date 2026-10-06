# sparknet peer-node — append-only audit trail

One line per transition, oldest first. Never rewrite history here; corrections
are new lines. The fabric root keeps its own `log.md` — this is the role's.

- [2026-10-06] LAYOUT vault restructured to `00_System`–`07_Archives`; the legacy category names became shims, content moved with no collisions, and `index.md`/`log.md` now live in `00_System/` with root-level symlinks
