# Role transition: peer → master (elected, not approved)

There is no approval step, and nothing is submitted to an existing master.
A peer becomes master by winning a unanimous election: three members, three
signed votes for one candidate, then a cluster trust root co-signed by all
three that names that node's operational key. Normative:
`../../contracts/consensus-protocol.md` (Election, Exchange, The cluster trust
root). This page is the wiring.

Flow:

1. **Membership, out of band.** Three nodes swap their WireGuard and cluster
   public keys into `cluster-members.json` (exactly three, distinct node ids).
   Only the exchange reads it — `shared/sparknet-election.py serve --members
   cluster-members.json`, which one node runs for the duration of the
   ceremony (loopback `127.0.0.1:4210` by default; the overlay address on a
   real cluster).

2. **Cluster keys.** Every member holds one cluster key, used for bids, votes
   and root co-signatures only. Create it with
   `python shared/sparknet-election.py keygen --key-dir <keys-dir>` —
   the peer's `sparknet-keygen.sh` needs `wg(8)` and does not mint cluster
   keys. The member's operational key is generated on demand when it bids.

3. **The ceremony, on all three nodes.** `make election-request` on the
   requester (it votes for itself), and on the other two nodes:
   `CANDIDATE=<requester> make election-request`. Each run files the bid,
   casts the vote, waits for the root, co-signs it and installs the result on
   that node's identity. Re-running is safe: an interrupted ceremony resumes
   from whatever is already at the exchange. On the winner's master-node side,
   `EXPECT=<requester> make election-approve` runs the same ceremony and
   additionally writes the formed root into
   `master-node/manifests/cluster-trust-root.json`.

4. **Unanimity is enforced, not requested.** Two votes form no root — the
   cluster stays in peer-only mode, which is a working state. And a member
   refuses to co-sign a root naming anyone but the candidate its own ballot
   named (`unexpected_master`), so a lying exchange cannot launder two honest
   votes into a third signature.

5. **The flip is a data change, not a rebuild.** On the node the root names:
   `role: master` in the identity, `role_note` recording the root id,
   `operational_key.held: true` plus the root id, and a `trust_root` block —
   and nothing else. The write itself refuses to land if any other field would
   move. Services, containers and make targets are identical before and after
   (charter: the master loads peer services by path); `make shims-check`,
   `keys-check` and the contract tests keep passing.

What does **not** confer authority, and never did: an operational key sitting
in a key directory (generate one and it still signs nothing), running the
`master-node/` subvault, or editing any manifest. Only a root all three
members co-signed does.
