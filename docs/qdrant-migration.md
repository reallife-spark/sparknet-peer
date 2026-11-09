# ChromaDB → Qdrant migration

This is the node-side runbook for the vector-store replacement. It is
operator-run; a development host cannot prove the live containers, the WebUI
reindex or the firewall.

## Before

1. Snapshot the existing Chroma volume (or the `infra_chroma_data` volume):
   `docker run --rm -v infra_chroma_data:/data -v /tmp:/backup alpine tar czf /backup/chroma-data.tar.gz -C /data .`
2. Record the Open WebUI knowledge bases and their file counts:
   `GET /api/v1/knowledge/` with the node service-account token.
3. Generate the Qdrant API key under the node state dir at `0600`; export it as
   `SPARKNET_QDRANT_API_KEY` for compose. Do not commit it.

## Switch

1. `docker compose -f peer-node/configs/ai-stack/docker-compose.qdrant.yml up -d`
   and confirm `curl -fsS http://127.0.0.1:6333/` answers.
2. Update the node's `~/infra` stack (or the committed stack) to
   `VECTOR_DB=qdrant`, `QDRANT_URI=http://127.0.0.1:6333`,
   `QDRANT_API_KEY=$SPARKNET_QDRANT_API_KEY`, `QDRANT_ON_DISK=true`.
3. If Open WebUI has a persisted `VECTOR_DB`/`QDRANT_URI` from an earlier boot,
   clear those rows or boot once with `ENABLE_PERSISTENT_CONFIG=false`; a
   persisted `VECTOR_DB=chroma` silently ignores the new environment.
4. Restart Open WebUI and run the admin reindex:
   `POST /api/v1/knowledge/reindex` (and `/api/v1/files/reindex` /
   `/api/v1/memories/reindex` where the release exposes them). This rebuilds
   the per-KB and per-file collections from stored content.

## Verify

- `GET /api/v1/knowledge/{id}/files` returns the same files as the pre-switch
  snapshot.
- A WebUI retrieval query against each KB returns a source; a single-file
  attachment returns a source too (the per-file collection is the one a partial
  reindex can miss).
- `make vector-inspect VECTOR_SCOPE=fabric` and, on the Master,
  `make -C master-node vector-reindex` build the Qdrant collections.
- `make -C master-node vector-serve` answers `/v1/health` with both
  `sparknet-fabric` and `sparknet-shared` over signed 4223.

## Rollback

Keep the Chroma volume for **14 days** after the Qdrant verification. To roll
back, stop Qdrant, restore the volume, set the old Chroma environment and
restart WebUI. The vault remains the source of truth for `sparknet-*` scopes,
so those can be rebuilt instead of restored.
