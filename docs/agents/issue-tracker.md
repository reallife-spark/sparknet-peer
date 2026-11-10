# Issue tracker

The peer workspace uses a **local markdown tracker**.

## Location and schema

- Root: `.scratch/issues/`
- One issue per file: `.scratch/issues/<id>.md`
- Frontmatter: `sparknet-issue/1` from `../contracts/issue-schema.json`.
- `.scratch/` is machine-local and gitignored. It is not a vault directory and
  is never indexed by the memory gateway.

## Lifecycle

`open` → `triaged` → `in-progress` → `blocked`/`done`/`wontfix`.

The five labels are `needs-triage`, `needs-info`, `ready-for-agent`,
`ready-for-human` and `wontfix`. See `triage-labels.md`.

## Push to the Master

A peer pushes its local issues to the Master with:

```sh
python3 services/sparknet-issue-push.py --root . --once
```

The pusher reads `.scratch/issues/` through the shared issue module, validates
the local frontmatter and `body_sha256`, refuses a secret-shaped or over-long
body, signs the request with the node key through `shared/sparknet-http.py`, and
sends `POST /api/v1/issues` to the Master's 8090 intake. The Master requires the
peer-held `issue-report` capability and a registered, non-revoked overlay
address.

An issue is operational work. It never enters the vault, never becomes a
knowledge answer and never grants authority.
