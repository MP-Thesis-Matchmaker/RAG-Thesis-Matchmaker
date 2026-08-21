# Kubernetes manifests

What is here runs the **ZORA harvester** and the **schema step**, and nothing else
yet. Both are things the one image we currently build can actually execute.

| Manifest | Kind | What it does |
|---|---|---|
| `init-db-job.yaml` | `Job` | `thesis-matchmaker init-db` — applies `schema.sql`. Run before a rollout. |
| `zora-harvest-full-cronjob.yaml` | `CronJob` | `--mode full`, Mondays 01:00 UTC. Authoritative snapshot; prunes withdrawn items. |
| `zora-harvest-incremental-cronjob.yaml` | `CronJob` | `--mode incremental`, the other six days at 01:00 UTC. Upserts only, deletes nothing. |

## Not here yet, and why

Two components in `docs/deployment.md`'s "What runs where" table have no manifest,
and the reason is not Harbor:

- **`thesis-matchmaker index` (CronJob)** needs `sentence-transformers`.
- **`thesis-matchmaker-mcp` (Deployment + Service)** needs `mcp`.

`pyproject.toml` keeps both behind optional extras — `[embeddings]` and `[mcp]` —
and `docker/zora/Dockerfile` installs neither, deliberately: `sentence-transformers`
pulls in torch, and a harvester pod has no use for it. So a manifest for either
would reference an image whose entrypoint cannot import its own dependencies:

```console
$ docker run --rm --entrypoint thesis-matchmaker-mcp <image> --stdio
    from mcp.server.fastmcp import FastMCP
ModuleNotFoundError: No module named 'mcp'
``` They
land when `docker/indexer/` and `docker/serving/` exist. See the Images section of
[`../docs/deployment.md`](../docs/deployment.md).

The posting scraper is a third case: it is still a separate repository.

## Before applying: replace every TODO(ci)

`grep -rn 'TODO(ci)' k8s/` finds them all. As committed, these manifests **cannot
be applied** — that is intentional. An unverified registry hostname in a committed
manifest is worse than an obvious blank, so nothing here is guessed.

| Placeholder | Needs | Blocked on |
|---|---|---|
| `image:` host / project / tag | `harbor.example/project/thesis-matchmaker:0.0.1` | Harbor hostname, project name, robot account |
| `timeZone` support | Kubernetes >= 1.27 (where CronJob `timeZone` is GA) | cluster version |

## Secrets are not committed, not even as placeholders

Create them out of band. Neither command's value should ever reach a file in this
repository:

```bash
# Postgres DSN. Ask Central Informatics; do not reuse the compose password.
kubectl create secret generic thesis-matchmaker-db \
  --from-literal=database-url='postgresql://USER:PASSWORD@HOST:5432/DBNAME?sslmode=require'

# ZORA personal API token, mounted as a file at /run/secrets/zora/token.
# config.resolve_api_token prefers the file over the inline variable, so the file
# is the deployed truth. Read it from disk rather than pasting it into a shell,
# which would put it in your history.
kubectl create secret generic zora-api-token --from-file=token=./token.secret
```

Append `--dry-run=client -o yaml` to either to inspect what would be created
without creating it.

## Verifying these files

**No schema validation has been run against them.** `kubectl apply
--dry-run=client` sounds offline but is not: it fetches the OpenAPI schema from the
API server, and without a cluster it fails before looking at the file at all. The
same is true of `--validate=false`, which still needs the server to resolve kinds.

What *has* been checked, offline:

- every file parses as YAML, and each `volumeMounts` name matches a declared `volume`
- every `image:` is a placeholder rather than a plausible-looking real reference
- the two schedules are disjoint and together cover all seven days

Field names and value types remain **unverified**. Either run
`kubectl apply --dry-run=server -f k8s/` once cluster access exists, or add
[`kubeconform`](https://github.com/yannh/kubeconform) to CI, which validates
against bundled schemas with no cluster.

## Deliberately unset

**`resources`** — no requests or limits on any container. Nothing here has been
profiled in a cluster: a full harvest of ~22,541 records has only ever been run on
a laptop, and an invented memory limit would OOM-kill it mid-run for no reason.
Without a `LimitRange` these pods are BestEffort and first to be evicted under
pressure, which is a real trade-off, not an oversight. Fill them in from a measured
run, not from a guess.

**The raw-response cache** — `harvest.write_raw_dump` writes one JSONL per run to
`ZORA_DATA_DIR/raw/`, which exists so ingestion is reproducible without re-hitting
ZORA. Here it is mounted as an `emptyDir`, which is discarded when the pod exits,
so in the cluster that reproducibility is currently lost. The alternatives are a
PVC (needs a storage class we have not been given) or moving the cache into
Postgres as a `jsonb` table. `emptyDir` is the placeholder that does not pretend
otherwise.

## Running things by hand

```bash
# Re-apply the schema step. A Job's pod template is immutable, so delete first.
kubectl delete job/thesis-matchmaker-init-db --ignore-not-found
kubectl apply -f k8s/init-db-job.yaml

# Backfill, or a one-off full harvest outside the Monday slot.
kubectl create job --from=cronjob/zora-harvest-full zora-harvest-manual-1

# Did the last scheduled run actually commit? The CronJob knows when a pod fired;
# harvest_state knows when a harvest committed, and the retention rail can roll one
# back while the pod still exits 0.
kubectl get cronjob zora-harvest-incremental
psql "$DATABASE_URL" -c 'SELECT * FROM harvest_state'
```
