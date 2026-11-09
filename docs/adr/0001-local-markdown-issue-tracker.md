# 0001 — Local markdown issue tracker

## Status

Accepted (2026-11-09).

## Context

The peer needs the Matt Pocock engineering skills but may have no GitHub remote
and no network egress. Work items must stay usable offline and must reach the
Master without a new knowledge channel.

## Decision

Track issues under `.scratch/issues/` with the `sparknet-issue/1` schema. Push
them to the Master over the signed `POST /api/v1/issues` route under the
peer-held `issue-report` capability.

## Consequences

- Issues survive an offline session and disappear with the machine-local
  `.scratch/` directory; the Master keeps the recorded copy.
- The pusher must redact before send and the Master must re-check on receipt.
- No issue may enter the vault, the memory gateway, `4223` or a user's KB.
