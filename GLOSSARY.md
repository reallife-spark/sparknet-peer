# Peer glossary

- **peer** — this node: the user's local AI surface and vault.
- **user vault** — `peer-<user_id>/`, the knowledge slice a signed-in user may
  read and grow.
- **guard** — the loopback/overlay service that verifies Master-signed requests
  before a protected file changes.
- **dispatch bridge** — the loopback OpenAI-compatible proxy for local agents.
- **engine** — the per-user OpenCode sandbox reached through the engine adapter.
- **share** — a bounded, granted read or copy between peers.
- **RFI** — a scoped question to the Master; the signed answer is installed in
  `01_Inbox/` and must be accepted before an AI may use it.
- **issue** — a work item under `.scratch/issues/`, pushed to the Master under
  `issue-report`. It is not knowledge and not authority.
- **understand graph** — the derived `.ua/knowledge-graph.json`; a build
  artifact, never an answer corpus.
