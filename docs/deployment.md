# Deployment

Target environment, confirmed with UZH Central Informatics on 2026-08-20 and
then against the ZI Container Services handbook
(<https://docs.cs.zi.uzh.ch/books/benutzerhandbuch>, UZH VPN only) on 2026-08-21:

- **Kubernetes cluster** run by the ZI team *Cloud & Container Services* (CCS),
  pulling images from the **private Harbor registry** at `registry.cs.zi.uzh.ch`
- **Postgres** reached as a ZI service at `postgres.uzh.ch` — running databases
  *inside* the cluster is explicitly discouraged, because the only storage class
  is NFS-backed and its I/O characteristics do not suit a database
- **Deployment is GitOps via ArgoCD**, not `kubectl apply`: Argo continuously
  syncs `*.yaml` in the root of a GitLab repo, and each file declares
  `appComponents` pointing at CCS's shared `general_helm_chart`

Everything below that is not yet filled in is marked `TODO(ci)` — a value we do
not have yet. Nothing here is guessed: an unverified registry hostname or DSN in
a committed manifest is worse than an obvious blank.

## What runs where

| Component | Runtime | Trigger | Manifest |
|---|---|---|---|
| `init-db` | one-shot `Job` | before every rollout | [`k8s/init-db-job.yaml`](../k8s/init-db-job.yaml) |
| `themis-zora harvest` | `CronJob` | incremental daily, full weekly | [`k8s/zora-harvest-*.yaml`](../k8s/) |
| `themis-matcher serve` | `Deployment` + `Service` | always on, HTTP on 8100 | **none** — image exists (`projects/matcher/`), default `CMD` |
| `themis-matcher index` | one-shot `Job` | a cold full build, by hand | **none** — same image, `command` override |
| `themis-gateway mcp` | `Deployment` + `Service` | always on, HTTP at `/mcp` | **none** — image exists (`projects/gateway/`), default `CMD` |
| `themis-scraper run` | `CronJob` | `fetch` then `run`, weekly | **none** — image exists (`projects/scraper/`) |

Every row but the first two lacks only a manifest. What used to block them — no image
installed the `[embeddings]` or `[mcp]` extra — is fixed; what remains is that
the committed manifests are in the wrong format for this cluster (see
**Deploying** below) and that two quota raises are outstanding. The matcher's
`Service` is **in-cluster only, with no `HTTPRoute`**: its index endpoints start
work measured in hours and it answers unauthenticated, so the namespace boundary
is what guards it.
See [`k8s/README.md`](../k8s/README.md).

Harvesting is a **cluster** concern. It is never run in GitHub Actions, and
harvest output is never committed to git — the repository is source code, not a
datastore.

### Repository size

That rule arrived late. `data/publications.jsonl` was tracked for most of the
project and reached 47 MB before the corpus moved into the `publication` table;
it and `data/state.json` are now untracked and gitignored. Untracking only stops
the growth — **every one of those blobs is still in git history**, so a fresh
clone still pays for them.

Excising them means rewriting history (`git filter-repo`), force-pushing, and
every collaborator re-cloning. That is a deliberate, coordinated decision for the
whole team, not a cleanup to slip into a branch, and it is deliberately **not**
done yet.

## Images

**One image per deployable role, not one image for everything.** All four now exist;
`projects/<member>/Dockerfile` is where each lives.

| Image | Role | Runtime | Extras it needs | Exists |
|---|---|---|---|---|
| `projects/zora/` | ZORA harvester | `CronJob` | none | **yes** |
| `projects/matcher/` | serve matching **and** build the index | `Deployment` (+ a `Job` for a cold build) | `[embeddings]` | **yes** |
| `projects/gateway/` | MCP adapter | `Deployment` | `[mcp]` | **yes** |
| `projects/scraper/` | posting scraper | `CronJob` | `[render]` | **yes** |

The split is not tidiness. `sentence-transformers` pulls in torch, which takes the
image from roughly 200 MB to a few GB; a harvester pod that imports neither would
otherwise pull all of it on every scheduled run. The posting scraper is the sharper
case, and now a measured one rather than a prediction: its dependencies are
`requests` / `beautifulsoup4` / `PyYAML` / `pypdf` / `openai`, and they intersect the
core's `httpx` / `psycopg` / `dspace-rest-client` only at pydantic and dotenv. One
image for both means each ships the other's dependency tree. (Those five were an
extra named `[scraping]` until 2026-08-27. The disjointness is why this member gets
its own image; it was never a reason for the dependencies to be optional, and as an
extra it made `uv sync --package themis-scraper` produce a broken CLI.)

The scraper image also carries chromium's headless shell (`[render]` +
`playwright install --with-deps --only-shell`), which moves it from the ~250 MB
class to 1.64 GB uncompressed (measured; mostly the `--with-deps` OS libraries).
That passes the compliance policy's "absolut notwendigen
Komponenten" test rather than straining it: `ibw--1` and `ivw--4` serve their
listings JS-only and `iff--3`'s profile pages render client-side, so without a
browser three sources are permanently unreadable. The browser is baked in at build
time — a CronJob must not download 100 MB from a CDN on every scheduled run.

The scraper image also carries something none of the others do: `data/scraper/`.
`uv sync --no-editable` builds the member into a wheel, so nothing outside
`projects/scraper/src/themis_scraper/` survives on its own — and the 103 `spec.yaml` files are read
at *runtime*, not just by tests. Without that COPY the image starts and finds nothing
to scrape.

**The gateway no longer needs `[embeddings]`, and that is the point of the HTTP
split.** It used to, because it embedded the incoming query in-process — so an
always-on pod carried its own copy of a 2.27 GB model and peaked around 3.6 GiB,
against a namespace ceiling of 4 GiB *in total*. The two could not coexist. The
matcher now embeds both the corpus and the query, in one process with one copy of
the weights, and the gateway is 296 MB of `httpx` and the MCP SDK. Verified on the
built image: `torch`, `sentence_transformers` and `themis_matcher` are all absent.

**The matcher image serves two roles, chosen by `CMD`.** `ENTRYPOINT` is
`themis-matcher` and the default `CMD` is **`serve`**; `command: ["index",
"--source", "db"]` in the chart's `shellCommand` switches it to the batch build.
One image because both roles want the same model and the same closure.

The default is `serve` because it is the role with consumers. The gateway holds no
model and no database and reaches this image over HTTP for every match, and both
ingestion jobs end their runs by POSTing to `/v1/index/publications` and
`/v1/index/postings`. A pod that came up in the batch role would refuse all three —
and since a chart with no `shellCommand` gets the image default, that default is
what a forgotten override falls back to.

The batch role stays available because a cold full index is measured in days under
the CPU quota, and that is not something to start over HTTP and hope the pod
outlives it. Steady-state incremental indexing goes through the REST triggers, which
run inside the serving process — the one qualifier on invariant 1, and the reason
one process does both.

**Both install a CPU-only torch, and that is a lock decision rather than a
Dockerfile one.** PyPI's default linux `torch` wheel is the CUDA build — a wheel
tag cannot express "linux, but no accelerator" — which drags in roughly 1.55 GB of
`nvidia-*` and `triton`. CUDA wheels are a *capability*, not a speedup: with no
GPU scheduled, torch runs on CPU at identical speed either way, and the shared
Helm chart exposes no GPU, `nodeSelector` or `tolerations` key at all, so nothing
we deploy can ever land on an accelerator. `pyproject.toml` therefore pins the
`pytorch-cpu` index behind a `sys_platform == 'linux'` marker; macOS never matches
it, so Apple Silicon keeps the stock wheel and MPS keeps working locally. Measured
effect: **zero `nvidia-*` packages in the lock**, confirmed inside a built linux image as
`torch 2.12.1+cpu`. A `linux/amd64` matcher image weighs **~1.6 GB unpacked / ~0.45 GB compressed**
instead of the several GB a CUDA build produces. Compressed is the number that
matters for the Harbor quota and for pull time, since that is what a registry
stores; `docker images` reports the unpacked figure, which is why the two disagree.

**The model weights are not baked in.** `BAAI/bge-m3` is 2.27 GB and publishes no
safetensors, so it is a single `pytorch_model.bin`. Both images set
`HF_HOME=/var/cache/huggingface` and expect that path to be an **RWX** PVC shared
between the matcher and the gateway pod, so the download happens once. Baking it
in would re-push 2.27 GB of unchanged weights on every dependency rebuild. Cluster
egress to the Internet is permitted, so the one-time fetch works.

**Both are multi-stage**, copying only `/app/.venv` into a fresh
`python:3.12-slim`, which keeps `uv` and the build context out of the artefact.
`projects/zora/Dockerfile` is still single-stage; on a 426 MB image the difference is
cosmetic, on a 1.95 GB one it is not.

**`init-db` shares the harvester image**, and `docker-compose.yml` builds it for
both. That is now a convenience rather than a structural fact: since the split,
`themis-init-db` belongs to `themis-shared`, which the harvester image installs anyway as a
dependency. A dedicated image whose closure is pydantic + psycopg is the cheaper option if the
init Job ever needs to be independent of a harvest rollout.

**Images install from `uv.lock`, never from version ranges.** The Dockerfile copies
the lockfile in and runs `uv sync --locked --no-default-groups --no-editable`, so
rebuilding the same commit gives the same dependency set, and the `dev` group —
pytest, ruff — never reaches the artefact. uv itself is pinned in the
`COPY --from=ghcr.io/astral-sh/uv:0.11.17` line, because it is the tool that reads
the lock. That pin is a tag rather than a digest, which is a weaker guarantee than
it looks: a re-pushed tag would go unnoticed.

### Why five distributions

The split landed on 2026-08-26. `src/thesis_matchmaker/` is gone; in its place are five members —
`libs/shared` plus `projects/{matcher,gateway,zora,scraper}` — each its own distribution, resolved
by one root `uv.lock`.

What it bought is **invariant 4 mechanically rather than socially**: an image built with
`uv sync --package themis-zora` does not have `themis_matcher` on disk, so a harvester that
imports `retrieval` fails at import time instead of at review time. CI's `boundaries` job installs
each member alone for the same reason.

The measured seam that made this cheap: the two producers share only `contracts/`, `config.py` and
`db.py` — now `themis-shared` — and their dependency sets are disjoint apart from pydantic and
dotenv. `themis-gateway` was the one deliberate cross-edge, importing `themis_matcher` from
`service.py` and nowhere else — which is exactly what made the HTTP swap, on 2026-08-26, a
one-file change. There is now no cross-edge at all: every member depends on `themis-shared` and
nothing else of ours, and the `boundaries` job enforces it.

Cost, for the record: six `pyproject.toml` files, a regenerated `uv.lock`, every import path
rewritten, and the whole test suite relocated. The scraper port went first, deliberately, so that
one commit was not answering two questions at once.

**One trap the split introduced.** `uv sync --locked` validates the *entire* workspace against the
lock, not just the selected package. An image that copies only its own member sees fewer members
than the lock describes and fails with `the lockfile needs to be updated`. Every Dockerfile
therefore copies all five manifests before any source — manifests only, so the layer stays warm.

## Deploying

**We do not run `kubectl apply`.** ArgoCD watches a GitLab repository and syncs
whatever is in it, continuously. Every `*.yaml` in that repo's root becomes an
ArgoCD Application; a `data/` folder holds ConfigMaps.

The file format is not a raw Kubernetes manifest. It is a list of
`appComponents`, each naming CCS's shared chart:

```yaml
appComponents:
- name: thesis-matchmaker-indexer
  sources:
    - repoURL: https://gitlab.uzh.ch/zi-container-services/helm-charts.git
      helmpath: "general_helm_chart"
      targetRevision: dev
      values: |-
        image: registry.cs.zi.uzh.ch/TODO(ci)/thesis-matchmaker-indexer:<git-sha>
        ...
```

The image names in these examples still read `thesis-matchmaker-*`, matching what `k8s/` says
today. They are renamed as a set when those manifests are rewritten — see [`k8s/README.md`](../k8s/README.md).

**This is why the manifests in [`k8s/`](../k8s/) cannot be used as they stand** —
they are raw `Job` and `CronJob` objects, which is a different thing from chart
values. Converting them is a task of its own; the value keys are known:

| Need | Chart key |
|---|---|
| the harvest and index CronJobs | `cronJob.enabled`, `.schedule`, `.backoffLimit`, `.restartPolicy` |
| the `init-db` one-shot | `job.enabled`, `.backoffLimit`, `.restartPolicy` |
| the model cache | `pvc.enabled`, `.mountPath`, `.storage` |
| scratch space | `emptyDir.dirs[].mountPath`, `.sizeLimit`, `.sharedMountPath` |
| **mandatory** requests/limits | `resources.requests/limits.{cpu,memory}` |
| secrets from Vault | `vault.vso.{name,path,mount}`, `vaultEnv` |
| the registry credential | `vault.dockerPullSecret`, `serviceAccount.imagePullSecretsName` |
| overriding an image entrypoint | `shellCommand.enabled`, `.command`, `.args` |
| non-root UID | `securityContext.runAsUser` |
| exposing the MCP endpoint | `httproute.enabled`, `.hosts[]` |
| Prometheus scraping | `serviceMonitor.enabled`, `.path`, `.interval` |

Note **`httproute`, not `ingress`**: the handbook documents a migration from
Ingress to Gateway API `HTTPRoute`, and CCS's own newer examples use the latter.

### Admission policies

Kyverno runs in the cluster. The **enforced** set is Pod Security Standards
baseline — no privileged containers, no host namespaces, no `hostPath`, no host
ports, no added capabilities, seccomp and AppArmor restrictions. Our images
satisfy all of it without doing anything special.

Four policies that would bite are **Audit** only, meaning they report rather than
reject: `require-run-as-nonroot`, `require-run-as-non-root-user`,
`require-ro-rootfs`, and `require-requests-limits`. Do not read that as licence to
ignore them — `projects/matcher/` and `projects/gateway/` both run as UID 10001
anyway, and requests/limits are separately mandatory because of the
ResourceQuota, whatever Kyverno's verdict.

## Local equivalent

`docker-compose.yml` mirrors the cluster: a `pgvector/pgvector` container, the
init-db one-shot, and the harvester as a manually invoked job.

```bash
docker compose up -d postgres
docker compose run --rm init-db
docker compose run --rm harvester harvest --mode incremental   # = what the CronJob does
```

For real timing locally, the timer belongs to the host, not the app — the same
two schedules as the CronJobs:

```cron
0 1 * * 1      cd <repo> && docker compose run --rm harvester harvest --mode full
0 1 * * 0,2-6  cd <repo> && docker compose run --rm harvester harvest --mode incremental
```

## Configuration

Secrets come from **Vault**, not from `kubectl create secret`. The shared chart
exposes three routes: `vault.vso.{name,path,mount}` materialises a
`VaultStaticSecret`, `vaultEnv` maps its keys onto environment variables, and
`vault.injector` writes a file into the pod (an `.env`, say). `vault.dockerPullSecret`
covers the registry credential.

Variables are prefixed by the member that reads them — `MATCHER_`, `GATEWAY_`,
`ZORA_`, `SCRAPER_` — except `DATABASE_URL` and `MATCHER_BASE_URL`, which more
than one member reads and which are pinned unprefixed. A variable set under the
wrong name is **not an error**: it is ignored and the default applies, so check
this table rather than assuming a chart value took effect. The full inventory is
`.env.example`; this is what the cluster has to supply.

| Variable | Purpose | Source in the cluster |
|---|---|---|
| `DATABASE_URL` | Postgres DSN, pointing at `postgres.uzh.ch` | Vault via `vaultEnv` — `TODO(ci)` |
| `MATCHER_BASE_URL` | where the harvester, scraper and gateway reach the matcher | chart `env`, the in-cluster Service address |
| `ZORA_UZH_API_KEY_FILE` | ZORA API token path | Vault, mounted as a file |
| `ZORA_UZH_API_KEY` | ZORA API token, inline | local only — the file above wins |
| `ZORA_DATA_DIR` | root of the raw-response cache | chart `env`; an `emptyDir` in both CronJobs |
| `MATCHER_LLM_BASE_URL` / `MATCHER_LLM_API_KEY` | LibreChat / AI Buddy gateway | Vault via `vaultEnv` |
| `MATCHER_EMBEDDING_MODEL` | `BAAI/bge-m3`, or `hash-fake` offline | chart `env` |
| `MATCHER_API_HOST` / `MATCHER_API_PORT` | must be `0.0.0.0` in a container | baked into `projects/matcher/Dockerfile` |
| `GATEWAY_MCP_HOST` / `GATEWAY_MCP_PORT` | must be `0.0.0.0` in a container | baked into `projects/gateway/Dockerfile` |
| `SCRAPER_CONTACT` | the address advertised in the scraper's User-Agent | chart `env`; the scraper refuses to fetch without it |
| `HF_HOME` | where bge-m3 is cached; the RWX PVC | baked into the matcher image |

**What goes to the LLM endpoint.** The synthesis step puts retrieved supervisor and author
names, publication titles, abstracts and posting descriptions into the prompt, and the LLM
parser sends the student's query. Pointing `MATCHER_LLM_BASE_URL` at a hosted API therefore sends
UZH personal data off-campus on every recommendation, with no warning in the logs. The
cluster target is a UZH-hosted LibreChat endpoint, so this is a development-machine
concern rather than a deployment one — but it is one variable away in either direction.

There is deliberately no variable for the ZORA API origin. It is a `ClassVar` on
`ZoraSettings`, so pydantic registers no field and no chart value can move it: a
harvest pointed at another DSpace would write that server's records into
`publication` under our provenance with nothing in the log to say so.

## Open questions for Central Informatics

Blocking for deployment, not for local development against a Postgres container.

1. **Is `CREATE EXTENSION vector` permitted for our database role?** It normally
   requires superuser unless the extension is marked trusted. If it is not, the
   extension has to be pre-created for us. **Ask this first** — it is the most
   likely blocker, and `libs/shared/src/themis_shared/schema.sql` opens with that
   statement.
2. **pgvector version** and the Postgres major version. This is not idle
   curiosity: HNSW needs >= 0.5.0, and **`hnsw.iterative_scan` needs >= 0.8.0**.
   That setting is what stops a filtered vector search from silently returning
   fewer rows than asked for, so on an older pgvector the retrieval quality
   depends entirely on the partial indexes in `schema.sql`. The code tolerates
   its absence; the evaluation numbers would not be comparable.
3. **Own database, or a schema inside a shared one** on `postgres.uzh.ch`? Affects
   migration scoping and `search_path`.
4. **Connection limit** for our role — sets the `ConnectionPool` max size.
5. **TLS**: required `sslmode`, and any CA certificate we must mount.
6. **How credentials reach the pod**: plain k8s `Secret`, or an external secret
   store / sealed-secrets setup.
7. **Harbor project.** *Mostly answered.* The project is
   `uzh-dsi-askuzh-masterthesis-supervisor`, a robot account exists, and pushes
   come from `.gitlab-ci.yml` on `gitlab.uzh.ch` rather than by hand. Two pieces
   are still open: a quota above the 10 GB default, and a separate **pull** robot
   for the cluster -- the CI robot has push rights and should not be the identity
   a pod authenticates with.
8. **Backup and retention** policy for the harvested publication data — this is
   personal data (researcher names and affiliations), so retention is a legal
   question as much as an operational one.
9. **Storage for the raw-response cache.** Answered in part: PVCs are available on
   the `idnas21zb.uzh.ch` storage class (NFS; RWO/RWX/ROX; `Delete`, with a
   `retain.` variant), and the namespace allows 10 PVCs totalling 50 Gi. So the
   `emptyDir` in the committed CronJobs is now a choice rather than a limitation
   and should become a PVC. Still open is whether we would rather move the cache
   into Postgres as a `jsonb` table and drop the volume entirely.
10. **Two quota raises.** The default namespace quota is `limits.cpu: 2`,
   `limits.memory: 4Gi`, `pods: 10`. bge-m3 is a 568M-parameter model held in
   memory, and one matcher pod peaks near 3.6 GiB — so a 4 Gi *namespace-wide*
   ceiling leaves room for essentially one copy of the model. The HTTP split is
   what made that survivable rather than impossible: the gateway holds no model
   now, so the question is one matcher pod against the ceiling instead of two pods
   that could never both fit. A second matcher replica still cannot. The Harbor
   project's 10 GB is the second.
   Both go to `container.services.support@zi.uzh.ch`, and both want a measured
   number rather than an estimate — see **Known limitations**.

## Known limitations of our own tooling

Not questions for Central Informatics — things we owe ourselves.

- **`schema.sql` has no migration path.** `schema.py` fingerprints the schema and
  refuses to run when the stored fingerprint differs, which is a strong guarantee
  that no DDL edit goes unapplied. It used to hash the file's raw text, which made
  *comments part of the schema's identity*: editing one demanded `init-db --reset`,
  and the predictable result was a comment the team knew was wrong and left in place
  rather than pay for. Fixed 2026-08-25 — `_normalize_sql` strips comments and
  collapses whitespace before hashing, so the fingerprint answers "do the tables
  differ?" and documentation is free to be correct. Adopting it moved the
  fingerprint once, to `3d4f0475bf80`; the DDL was unchanged, so existing databases
  are re-stamped with an UPDATE rather than a reset.
- **`resources` are unset on every committed container, and in this cluster that
  means the pods do not start.** This was written up as a considered trade-off —
  better BestEffort than an invented limit that OOM-kills a harvest — and that
  reasoning is simply wrong here. The namespace carries a ResourceQuota, and the
  handbook is blunt about the consequence: "Muss bei jedem Pod die Ressourcen für
  memory und cpu angegeben werden, ansonsten wird dieser nicht gestartet!" A pod
  without requests and limits is rejected, not merely deprioritised. The shared
  chart sets defaults via `resources.requests/limits.{cpu,memory}`, and those
  defaults are meant to be adjusted rather than accepted. Fixing this is part of
  converting `k8s/` to the Argo format.
- **No image is pushed anywhere automatically.** Deliberate until the Harbor
  project exists, and manual work in the meantime. Manifests are a different
  story now — Argo applies them continuously once they are in the GitLab repo, so
  "no manifest is applied automatically" stops being true the moment we commit one
  there.
- **The Python version the image runs is never tested.** CI runs the suite on 3.11;
  every image is `python:3.12-slim`; `requires-python` says only `>=3.11`. `uv.lock`
  resolves for both, so nothing is broken today — but the interpreter production
  actually uses is the one no test has ever executed against. There is now a reason
  to prefer 3.12 rather than leave it open: the UZH side standardises on it (the
  `ai-buddy` evaluation repo builds from `ghcr.io/astral-sh/uv:python3.12-bookworm`
  onto `python:3.12-slim`). Closing it means a 3.12 leg in the CI matrix, or 3.12
  everywhere.
- **A full index takes the better part of a week under the default quota.**
  Measured on an Apple M-series laptop, in the `projects/matcher/` image, embedding
  the 50 checked-in samples with real `BAAI/bge-m3` and extrapolating linearly to
  the **214,685**-row corpus the 2026-08-21 full harvest produced:

  | Container CPU | torch threads | docs/s | 214,685 docs | Peak RSS |
  |---|---|---|---|---|
  | unrestricted (18 cores) | default (18) | 2.05 | ~29 h | 3.55 GiB |
  | `--cpus 2` | default (18), before the fix | 0.20 | **~12.1 days** | 3.65 GiB |
  | `--cpus 2` | `OMP_NUM_THREADS=2` set externally | 0.42 | ~5.9 days | 3.62 GiB |
  | `--cpus 2` | **detected from the quota, empty env** | 0.37 | **~6.7 days** | 3.61 GiB |

  **~57% of that work is discarded at query time.** `indexing/sources.py`'s
  `_SELECT_PUBLICATIONS` has no `WHERE` clause, so every publication is embedded,
  while `retrieval/`'s pre-filter only ever returns the ~91,700 with a UZH author.
  Restricting the indexing query would cut a full index roughly in half. It is a
  behaviour change — those rows would stop being searchable at all, which matters
  if the pre-filter is ever relaxed — so it is not done here.

  Two things follow. First, **peak RSS is ~3.6 GiB whatever the CPU budget**, and
  the namespace ceiling is 4 Gi *in total* — so one matcher pod all but exhausts
  the quota on its own. When the gateway also held a copy of the model the two
  could not coexist at all; since 2026-08-26 it holds none, so the figure now
  bounds *replicas of the matcher* rather than ruling out the pair. That is the
  number the quota request rests on. Note the corollary: with a single replica,
  a long index run and query traffic share one pod's CPU.

  Second, **a Kubernetes CPU limit does not reduce `os.cpu_count()`**. It is a CFS
  bandwidth quota, so torch sees every core on the node and starts that many
  threads to contend over a 2-core budget — measured at 18 threads under
  `--cpus 2`, and `os.sched_getaffinity()` reports 18 as well, so nothing in the
  standard library exposes the real allowance.

  **This is now handled in code, so no manifest has to remember it.**
  `indexing/embedder.cpu_limit()` reads `/sys/fs/cgroup/cpu.max` (falling back to
  the v1 `cpu.cfs_quota_us`/`cpu.cfs_period_us` pair) and
  `SentenceTransformerEmbedder._load()` applies it — to `OMP_NUM_THREADS` before
  the first torch import, because OpenMP reads that at library init, and to
  `torch.set_num_threads()` after. An `OMP_NUM_THREADS` the operator set is never
  overridden. The last table row is that fix with an empty environment: 1.8× over
  the unfixed run, about 12% behind pinning the variable externally, which is
  within single-run variance on a laptop and not worth chasing. It does not make a
  full index cheap — 6.7 days is not 12.1, but neither is a working schedule; the
  quota raise is what actually fixes this.

  All of these are laptop numbers extrapolated from 50 documents to 214,685 — four
  orders of magnitude, linearly, where a real run has batch and cache effects.
  Present them to CCS with that method attached; do not restate them as cluster
  measurements. The honest summary is "days, not hours, and the CPU limit is the
  dominant term".
- **The `[mcp]` extra now requires SDK 2.x, and the adapter was ported to it.**
  `mcp>=1.2` had resolved to **2.0.0**, which removed `FastMCP`, so
  `themis-gateway mcp` could not start at all — CI never installs the extra, so
  nothing caught it until an image was built. The adapter uses `MCPServer` from
  `mcp.server.mcpserver`, and passes `host`/`port` to `run()` instead of poking
  `mcp.settings`. The wire format was checked before and after: tool names,
  descriptions, output schemas and the `tools/call` response shape are unchanged.
  One field moved on purpose — `serverInfo.version` was reporting the SDK's version
  (`1.29.0`) and now reports the distribution's (`0.0.1`).
  **The module is still untested**, which is how this got missed; see
  [`projects/gateway/README.md`](../projects/gateway/README.md).

## Registry

The private registry is **Harbor at `https://registry.cs.zi.uzh.ch/`**, run
on-premises by CCS. It is reachable over HTTPS from Datacenter Zones 1 and 2, the
UZH VPN, and `gitlab.uzh.ch` including its runners — so a GitLab pipeline can push
to it. Images are scanned automatically once a day with Trivy, which is what
satisfies the Compliance Policy's pre-deployment scan requirement without any
pipeline work on our side.

Two things about it are not negotiable and one is a real constraint:

- **Self-built UZH applications must live in a private registry.** The policy
  lists exactly two as trusted: `registry.cs.zi.uzh.ch` and `cr.gitlab.uzh.ch`.
  GHCR is neither, which is why the deleted build-and-push workflow was pointed at
  the wrong place — not merely inconvenient, non-compliant.
- **Projects are created only by admins.** Request one by mail to
  `container.services.support@zi.uzh.ch`. Nothing can be pushed before the project
  exists. Ask for a **project**, not a repository: Harbor holds one repository per
  image and we have four roles. Ours is
  **`uzh-dsi-askuzh-masterthesis-supervisor`**. The four repositories inside it
  need no request at all — Harbor creates a repository implicitly on the first
  push to its name, so `REPOSITORY` in Harbor's push-command hint is a blank we
  fill in, not something to wait for.
- **A new project has a 10 GB quota by default.** At ~0.45 GB compressed per
  image, two roles leave room for roughly twenty tags between them — workable, but
  worth knowing before tagging every commit, and Harbor does not garbage-collect
  old tags on its own. Raising the quota needs an admin. Two consequences, one
  already acted on: the pipeline pushes only from the default branch, and
  re-pushing `latest-test` orphans the digest it replaced, so a **Tag Retention**
  policy (Harbor -> project -> Policy) keeping the most recent handful of
  artefacts per repository is worth configuring once the first images land. No
  pipeline can do that part for itself.

`cr.gitlab.uzh.ch` is equally trusted, and is what CCS's own workshop example
uses. It was not chosen because the policy makes GitLab Container Scanning
*mandatory and ours to configure* there — unconfigured, we would be out of
compliance rather than merely unscanned.

### Building and pushing

Images are built and pushed by [`.gitlab-ci.yml`](../.gitlab-ci.yml), which runs
on `gitlab.uzh.ch` and nowhere else. Do not confuse it with
`.github/workflows/ci.yml`: that one is the correctness gate (lint, format,
tests, boundaries, wheels) and never builds an image; this one builds images and
never runs a test. Neither substitutes for the other, and a green pipeline here
says nothing about whether the code works.

Every branch builds all four images; **only the default branch pushes**. Two tags
are published per image:

```
registry.cs.zi.uzh.ch/uzh-dsi-askuzh-masterthesis-supervisor/themis-<role>:<version>-test
registry.cs.zi.uzh.ch/uzh-dsi-askuzh-masterthesis-supervisor/themis-<role>:latest-test
```

Both name the same digest. `<version>` is parsed out of
`projects/<role>/pyproject.toml` at build time and the repository name is
`project.name` from that same manifest, so bumping `version` there is the entire
release procedure -- the pipeline hardcodes neither, and the Harbor layout cannot
drift from the distribution names.

**This departs from the SHA tagging this document originally specified, and the
departure is deliberate.** A `-test` channel is not what an Argo app file
consumes, and a version is what a human reading a Harbor tag list actually wants.
The exactness that motivated SHA tags is preserved as a label the pipeline stamps
on every image:

```bash
docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
  registry.cs.zi.uzh.ch/uzh-dsi-askuzh-masterthesis-supervisor/themis-gateway:latest-test
```

When a production channel is defined, SHA tags are still the right answer for it.

Two CI/CD variables are required on the GitLab project, and the first has a trap
in it:

| Variable | What | Notes |
|---|---|---|
| `HARBOR_THEMIS_ROBOT_USER` | the robot account's full name | `robot$uzh-dsi-askuzh-masterthesis-supervisor+<account>`. **Untick "Expand variable reference"** -- GitLab expands `$` inside variable *values*, and the name contains one; without that, login fails with a 401 that looks like a wrong password. Cannot be masked (masking rejects `$`) and does not need to be. |
| `HARBOR_THEMIS_ROBOT_PASSWORD` | the robot's generated secret | Mask it. Harbor robots **expire by default** -- check the *Expires* column, because a pipeline that worked all semester and then stopped is usually this. |

Harbor refuses to grant a robot `Push Repository` without `Pull Repository`. Pull
is also what makes the pipeline's `--cache-from` work, so the combination is not
merely a formality.

Building by hand still works, and is what to do while the runners are unavailable:

```bash
docker build -f projects/matcher/Dockerfile \
  -t registry.cs.zi.uzh.ch/uzh-dsi-askuzh-masterthesis-supervisor/themis-matcher:0.0.1-test .
docker push registry.cs.zi.uzh.ch/uzh-dsi-askuzh-masterthesis-supervisor/themis-matcher:0.0.1-test
```

Note the image name -- `themis-matcher`, matching the distribution. The
`thesis-matchmaker-*` names elsewhere in this document are pre-split and get
renamed as a set when the `k8s/` manifests are converted.

**Build for `linux/amd64` explicitly** when building on an Apple Silicon machine.
`docker build` defaults to the host architecture, and an arm64 image will not run
on the cluster's nodes: `docker buildx build --platform linux/amd64 --load ...`.
The pipeline needs no such flag: GitLab's runners are amd64 already. This applies
to hand-built images only, which is also why the two heavy images (matcher, with
torch; scraper, with chromium) are best left to CI on an Apple Silicon machine --
under emulation they are punishing.

### Base images

Compliance §2.3 asks for "offizielle und gehärtete Basis-Images" and recommends
**Alpine** or **Red Hat UBI**. All four of our Dockerfiles use
`python:3.12-slim` instead. That is defensible but is a deliberate deviation worth
stating rather than hiding: the image is official with verifiable provenance
(§2.1.1), and **Alpine is not available to us at all** for the two embeddings
images, because Alpine is musl-based and torch publishes no musl wheels. A UBI
Python base would satisfy the recommendation directly and has not been evaluated.
