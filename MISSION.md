---
title: sparknet peer-node — MISSION
role: peer
template: true
updated: 2026-10-31
---

# sparknet peer-node — MISSION

The **user's local AI node**. A peer gives one person a private place to chat,
plan and build with a model, keep their own knowledge, and keep it organized.
It is useful with no Master, and it can never act on its own authority.

`INTENT.md` is the design contract; `AGENTS.md` is the operating charter and
wins if the two ever disagree.

## Scope

- **In scope**: local chat, agentic planning and building, the peer vault,
  per-user `peer-<user_id>` vaults and KBs, local RAG, RFI knowledge intake,
  self-healing, and verification of Master-signed decisions.
- **Out of scope**: signing, granting, revoking, escalation, registry
  authorship, the Master's operational key and cluster-wide administration.
- **Out of scope**: treating a vault copy as a node or a KB as authority.
  Knowledge is content; authority is key possession.

## Success

The vault should answer, with citations:

1. How does a user get to a working chat, plan/build session and private KB?
2. What may this node do without asking, and what needs a Master signature?
3. Why did the guard refuse, and what would legitimately change that?
4. How does knowledge reach this node, and what may it never carry?
5. What keeps the vault organized without silently changing user content?
6. Which behaviours are proven by tests, and which need a target node?

## Success for the role

A peer succeeds when its user is productive **without** the fabric, while the
node stays incapable of changing its own protected files or granting itself
anything. When it can act on its own authority, the design has failed.

## Non-goals

- Becoming a Master by configuration, directory name or prompt.
- Replacing the user's judgement with automatic content changes.
- Copying personal pages into a shared corpus.
- Holding a Master key, a signer socket or another user's vault.

## Constraints

- Content language, citation rules and domain conventions are in `AGENTS.md`,
  which wins over this page.
- No secrets in the vault. Key material belongs in `.state/` at mode 0700.
- Live network and kernel behaviour is not verifiable on the development host.
- Knowledge arrives as data; authority arrives as a signature. They never share
  a channel; see `../contracts/knowledge-flow.md`.
