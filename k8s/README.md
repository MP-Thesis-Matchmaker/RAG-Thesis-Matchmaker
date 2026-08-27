# Kubernetes manifests

> [!IMPORTANT]
> **These files are the wrong format for our cluster, and none of them can be
> applied as they stand.** Deployment is GitOps through ArgoCD, which syncs a
> GitLab repository whose `*.yaml` files declare `appComponents` referencing CCS's
> shared `general_helm_chart` — chart *values*, not raw Kubernetes objects. They
> also all omit `resources`, which this namespace rejects outright (see
> **Resources** below). They are kept because the intent encoded in them — the two
> disjoint schedules, the retention reasoning, the init-db ordering — is still
> correct and is what the conversion will carry over. See the **Deploying**
> section of [`../docs/deployment.md`](../docs/deployment.md) for the target
> format and the full list of chart keys.

What is here describes the **ZORA harvester** and the **schema step**.

> [!NOTE]
> These files still carry the pre-split names — `app.kubernetes.io/name: thesis-matchmaker`, the
> `thesis-matchmaker-db` Secret, and a single `thesis-matchmaker` image. That is **deliberate,
> not an oversight**: renaming the Secret means whoever created it has to recreate it, and these
> manifests are being replaced wholesale anyway (see below), so the identifiers get renamed once,
> as a set, rather than twice. Note also that one image can no longer serve both roles — since
> the workspace split, `init-db` belongs to `themis-shared` and the harvester to `themis-zora`.

| Manifest | Kind | What it does |
|---|---|---|
| `init-db-job.yaml` | `Job` | `themis-init-db` — applies `schema.sql`. Run before a rollout. |
| `zora-harvest-full-cronjob.yaml` | `CronJob` | `--mode full`, Mondays 01:00 UTC. Authoritative snapshot; prunes withdrawn items. |
| `zora-harvest-incremental-cronjob.yaml` | `CronJob` | `--mode incremental`, the other six days at 01:00 UTC. Upserts only, deletes nothing. |

**Two roles the rewrite has to add, and one it must not.** Since 2026-08-26 the
matcher is an HTTP service as well as a batch job, so it needs a `Deployment`
(`command: ["serve"]` on the same image) and a `Service`. It must **not** get an
`HTTPRoute`: its `/v1/index/*` endpoints start work measured in hours, it answers
unauthenticated, and the namespace boundary is the only thing guarding it. The
gateway then needs `MATCHER_BASE_URL` pointing at that in-cluster Service.

**And one role the rewrite can drop.** There is no "index after each harvest"
CronJob to write. The harvester and the scraper POST to the matcher when a run
commits, so the trigger is the ingestion job rather than a schedule guessing when
one finished. A one-shot `Job` on the default `CMD` is still the right way to start
a cold full build, because that one takes days and should not depend on an HTTP
request surviving.

There is deliberately **no separate CronJob for the `person` and `org_unit`
mirrors**: every harvest run refreshes them first, as full snapshots, before it
touches publications. They are steps of a harvest, not a job of their own — a
third schedule would just be a way for the mirrors to be stale relative to the
publications that reference them.

## Not here yet, and why

Two components in `docs/deployment.md`'s "What runs where" table still have no
manifest — but the reason has changed. It used to be that no image installed the
extras they need, so a manifest would have referenced an image whose entrypoint
could not import its own dependencies:

```console
$ docker run --rm --entrypoint themis-gateway-mcp <harvester-image> --stdio
    from mcp.server.fastmcp import FastMCP
ModuleNotFoundError: No module named 'mcp'
```

**`projects/matcher/` and `projects/gateway/` now exist**, so that blocker is gone.
What remains is the format problem above, plus two quota raises that have to be
granted before either could run: the namespace ceiling is `limits.memory: 4Gi` and
bge-m3 does not leave room for two pods holding it, and the Harbor project defaults
to 10 GB for all our images together.

The posting scraper is a third case, and a smaller one now: the code is in this
repository and `projects/scraper/Dockerfile` builds it, so what it lacks is only a
manifest. It wants the same treatment as the harvester — a CronJob, plus a PVC for
`data/scraper/cache/` so a scheduled run does not re-fetch 103 pages it already has,
plus one for `data/scraper/var/` (`state.json` is the only record of which sources
are onboarded; with an empty volume `run` refuses loudly and does nothing). Its image
carries chromium's headless shell for the three JS-only sources, and chromium wants a
real `/dev/shm`: give the pod an `emptyDir` with `medium: Memory` mounted there, or
heavy pages can crash the renderer.

## Before applying: replace every TODO(ci)

`grep -rn 'TODO(ci)' k8s/` finds them all. As committed, these manifests **cannot
be applied** — that is intentional. An unverified registry hostname in a committed
manifest is worse than an obvious blank, so nothing here is guessed.

| Placeholder | Needs | Blocked on |
|---|---|---|
| `image:` project / tag | see below — the host, **project and tags are all now known** | nothing; the conversion below is what these wait on |
| `timeZone` support | Kubernetes >= 1.27 (where CronJob `timeZone` is GA) | cluster version |

The placeholders are **still** deliberately left in the YAML, even though the
values now exist. These three files are being replaced wholesale by Argo
`appComponents` (see the note at the top), so editing them is churn on files with
no future — and worse, a filled-in `image:` makes a manifest look applicable when
it is still the wrong kind of object and still omits the mandatory `resources`.
An obvious blank is the honest state.

The values, for whoever does the conversion. The Harbor project is
`uzh-dsi-askuzh-masterthesis-supervisor`; images are built and pushed by
[`../.gitlab-ci.yml`](../.gitlab-ci.yml) and carry two tags each:

```
registry.cs.zi.uzh.ch/uzh-dsi-askuzh-masterthesis-supervisor/themis-zora:<version>-test
registry.cs.zi.uzh.ch/uzh-dsi-askuzh-masterthesis-supervisor/themis-zora:latest-test
```

All three manifests here run from that one image. The note above says `init-db`
"belongs to `themis-shared`" since the split, and that is true of the *code* —
`themis-init-db` is a `themis-shared` console script — but `themis-shared` is a
library, not a deployable role, so no image is built for it. `init-db` runs from
the `themis-zora` image with its entrypoint overridden, which is exactly what
`docker-compose.yml` already does and why that is not the misnomer it looks like.
The other three roles have their own images (`themis-matcher`, `themis-gateway`,
`themis-scraper`) and no manifests here at all.

## Secrets are not committed, not even as placeholders

**In the cluster these come from Vault, not from `kubectl`.** The chart exposes
`vault.vso.{name,path,mount}` to materialise a `VaultStaticSecret`, `vaultEnv` to
map its keys onto environment variables, and `vault.injector` to write a file into
the pod — which is how the ZORA token should arrive, since
`ZoraSettings.api_token` prefers a file over the inline variable.

The commands below are therefore the **local / debugging** form, not the deployed
one. Neither command's value should ever reach a file in this repository:

```bash
# Postgres DSN. Ask Central Informatics; do not reuse the compose password.
kubectl create secret generic thesis-matchmaker-db \
  --from-literal=database-url='postgresql://USER:PASSWORD@HOST:5432/DBNAME?sslmode=require'

# ZORA personal API token, mounted as a file at /run/secrets/zora/token.
# ZoraSettings.api_token prefers the file over the inline variable, so the file
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

## Resources

**`resources` is unset on every container here, and that is a defect rather than a
trade-off.** The argument used to be: nothing has been profiled in a cluster, an
invented memory limit would OOM-kill a harvest mid-run, and BestEffort pods being
first to evict is a price worth paying. The premise was that omitting the field
degrades scheduling. In this cluster it does not — it prevents scheduling:

> "Da eine Ressourcenquota für den Namespace gesetzt wurde. Muss bei jedem Pod die
> Ressourcen für memory und cpu angegeben werden, ansonsten wird dieser nicht
> gestartet!"

The namespace quota is `limits.cpu: 2`, `limits.memory: 4Gi`, `requests.cpu: 2`,
`requests.memory: 2Gi`, `pods: 10`, `persistentvolumeclaims: 10`,
`requests.storage: 50Gi`. A pod with no requests and limits is rejected. The shared
chart supplies defaults through `resources.requests/limits.{cpu,memory}`, which are
meant to be adjusted rather than accepted.

Note the quota is **namespace-wide**, not per pod: 4 Gi is the sum across every
container we run. That is the constraint behind the outstanding quota request, since
bge-m3 is a 568M-parameter model and the indexer and serving pods each hold a copy.

The original instinct was still half right — fill these in from a measured run, not
a guess. It just cannot be left blank in the meantime.

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
