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
| `thesis_matchmaker.zora.harvest` | `CronJob` | incremental daily, full weekly | [`k8s/zora-harvest-*.yaml`](../k8s/) |
| `thesis-matchmaker index` | `CronJob` | after each harvest | **none** — image exists (`projects/matcher/`) |
| `thesis-matchmaker-mcp` | `Deployment` + `Service` | always on, HTTP at `/mcp` | **none** — image exists (`projects/gateway/`) |
| `thesis_matchmaker.scraper.main` | `CronJob` | `fetch` then `run`, weekly | **none** — image exists (`projects/scraper/`) |

The last two rows now lack only a manifest. What used to block them — no image
installed the `[embeddings]` or `[mcp]` extra — is fixed; what remains is that
the committed manifests are in the wrong format for this cluster (see
**Deploying** below) and that two quota raises are outstanding.
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
| `projects/matcher/` | build the vector index | `CronJob` | `[embeddings]` | **yes** |
| `projects/gateway/` | MCP adapter | `Deployment` | `[embeddings]`, `[mcp]` | **yes** |
| `projects/scraper/` | posting scraper | `CronJob` | `[scraping]`, `[render]` | **yes** |

The split is not tidiness. `sentence-transformers` pulls in torch, which takes the
image from roughly 200 MB to a few GB; a harvester pod that imports neither would
otherwise pull all of it on every scheduled run. The posting scraper is the sharper
case, and now a measured one rather than a prediction: the `[scraping]` extra is
`requests` / `beautifulsoup4` / `PyYAML` / `pypdf` / `openai`, and it intersects the
core's `httpx` / `psycopg` / `dspace-rest-client` only at pydantic and dotenv. One
image for both means each ships the other's dependency tree.

The scraper image also carries chromium's headless shell (`[render]` +
`playwright install --with-deps --only-shell`), which moves it from the ~250 MB
class to 1.64 GB uncompressed (measured; mostly the `--with-deps` OS libraries).
That passes the compliance policy's "absolut notwendigen
Komponenten" test rather than straining it: `ibw--1` and `ivw--4` serve their
listings JS-only and `iff--3`'s profile pages render client-side, so without a
browser three sources are permanently unreadable. The browser is baked in at build
time — a CronJob must not download 100 MB from a CDN on every scheduled run.

The scraper image also carries something none of the others do: `data/scraper/`.
`uv sync --no-editable` builds the project into a wheel, so nothing outside
`src/thesis_matchmaker/` survives on its own — and the 103 `spec.yaml` files are read
at *runtime*, not just by tests. Without that COPY the image starts and finds nothing
to scrape.

The indexer and the serving adapter both need `[embeddings]` — the query has to be
embedded with the same model as the corpus — so splitting them buys lifecycle
separation rather than size: one is a batch job that writes and exits, the other is
a long-lived read-only process. That is reason enough to version and roll them
independently.

**Both install a CPU-only torch, and that is a lock decision rather than a
Dockerfile one.** PyPI's default linux `torch` wheel is the CUDA build — a wheel
tag cannot express "linux, but no accelerator" — which drags in roughly 1.55 GB of
`nvidia-*` and `triton`. CUDA wheels are a *capability*, not a speedup: with no
GPU scheduled, torch runs on CPU at identical speed either way, and the shared
Helm chart exposes no GPU, `nodeSelector` or `tolerations` key at all, so nothing
we deploy can ever land on an accelerator. `pyproject.toml` therefore pins the
`pytorch-cpu` index behind a `sys_platform == 'linux'` marker; macOS never matches
it, so Apple Silicon keeps the stock wheel and MPS keeps working locally. Measured
effect: the CPU pin alone drops the linux resolution from 132 packages to 114
(capping `mcp` below 2.0 took it to 109), and an
`linux/amd64` indexer image weighs **~1.6 GB unpacked / ~0.45 GB compressed**
instead of the several GB a CUDA build produces. Compressed is the number that
matters for the Harbor quota and for pull time, since that is what a registry
stores; `docker images` reports the unpacked figure, which is why the two disagree.

**The model weights are not baked in.** `BAAI/bge-m3` is 2.27 GB and publishes no
safetensors, so it is a single `pytorch_model.bin`. Both images set
`HF_HOME=/var/cache/huggingface` and expect that path to be an **RWX** PVC shared
between the indexer and the serving pod, so the download happens once. Baking it
in would re-push 2.27 GB of unchanged weights on every dependency rebuild. Cluster
egress to the Internet is permitted, so the one-time fetch works.

**Both are multi-stage**, copying only `/app/.venv` into a fresh
`python:3.12-slim`, which keeps `uv` and the build context out of the artefact.
`projects/zora/Dockerfile` is still single-stage; on a 426 MB image the difference is
cosmetic, on a 1.95 GB one it is not.

**`init-db` shares the harvester image**, and `docker-compose.yml` builds it for
both. That is correct rather than a shortcut: `init-db` is not a role, it is this
same distribution applying its own schema, and it needs no extras. It moves to a
shared image if `src/` is ever split.

**Images install from `uv.lock`, never from version ranges.** The Dockerfile copies
the lockfile in and runs `uv sync --locked --no-default-groups --no-editable`, so
rebuilding the same commit gives the same dependency set, and the `dev` group —
pytest, ruff — never reaches the artefact. uv itself is pinned in the
`COPY --from=ghcr.io/astral-sh/uv:0.11.17` line, because it is the tool that reads
the lock. That pin is a tag rather than a digest, which is a weaker guarantee than
it looks: a re-pushed tag would go unnoticed.

### Why `src/` is still one tree

Several images do not require several source trees: one distribution can produce
all four, differing only in entrypoint and installed extras. So the flat
`src/thesis_matchmaker/` layout and the per-role `docker/` layout are not in
conflict, they are answering different questions.

Whether to go further — a `uv` workspace of `projects/zora`, `projects/scraper`,
`projects/serving`, `projects/shared`, each its own distribution — is **open**. It
would make invariant 4 mechanical instead of social: a harvester image that never
installs `retrieval` cannot import it, and the boundary stops depending on review.
It also means four `pyproject.toml` files, a regenerated `uv.lock`, every import
path rewritten, and the whole test suite relocated.

**Trigger for deciding was the scraper migration, and it has now happened.** The
scraper landed as `src/thesis_matchmaker/scraper/` — a peer of `zora/`, in the same
flat tree — deliberately *without* taking the workspace question with it. Splitting
`src/` and porting 5,000 lines in one change would have meant one commit answering
two questions, with no way to tell which one had gone wrong if either did.

So the seam is now observable, which is what this section asked for. What it shows:
the two producers share `contracts/`, `config.py` and `db.py` and nothing else, and
their dependency sets are disjoint apart from pydantic and dotenv. That is a clean
enough boundary that a workspace would buy import-time *enforcement* rather than
decoupling — real, but a different and smaller claim than the one made before there
was anything to look at. Decide it together with the `ingestion/` parent question,
which is the same question at a different granularity.

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
docker compose run --rm harvester --mode incremental   # = what the CronJob does
```

For real timing locally, the timer belongs to the host, not the app — the same
two schedules as the CronJobs:

```cron
0 1 * * 1      cd <repo> && docker compose run --rm harvester --mode full
0 1 * * 0,2-6  cd <repo> && docker compose run --rm harvester --mode incremental
```

## Configuration

Secrets come from **Vault**, not from `kubectl create secret`. The shared chart
exposes three routes: `vault.vso.{name,path,mount}` materialises a
`VaultStaticSecret`, `vaultEnv` maps its keys onto environment variables, and
`vault.injector` writes a file into the pod (an `.env`, say). `vault.dockerPullSecret`
covers the registry credential.

| Variable | Purpose | Source in the cluster |
|---|---|---|
| `DATABASE_URL` | Postgres DSN, pointing at `postgres.uzh.ch` | Vault via `vaultEnv` — `TODO(ci)` |
| `ZORA_UZH_API_KEY_FILE` | ZORA API token path | Vault, mounted as a file |
| `ZORA_UZH_API_KEY` | ZORA API token, inline | local only — the file above wins |
| `LLM_BASE_URL` / `LLM_API_KEY` | LibreChat / AI Buddy gateway | Vault via `vaultEnv` |
| `EMBEDDING_MODEL` | `BAAI/bge-m3`, or `hash-fake` offline | chart `env` |
| `MCP_HOST` / `MCP_PORT` | must be `0.0.0.0` in a container | baked into `projects/gateway/` |
| `HF_HOME` | where bge-m3 is cached; the RWX PVC | baked into both `[embeddings]` images |

## Open questions for Central Informatics

Blocking for deployment, not for local development against a Postgres container.

1. **Is `CREATE EXTENSION vector` permitted for our database role?** It normally
   requires superuser unless the extension is marked trusted. If it is not, the
   extension has to be pre-created for us. **Ask this first** — it is the most
   likely blocker, and `src/thesis_matchmaker/schema.sql` opens with that
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
7. **Harbor project.** The hostname is known (`registry.cs.zi.uzh.ch`); what is
   not is our project name, a robot account for pulls, and a quota above the 10 GB
   default. Also whether pushes should come from a GitLab pipeline (the registry is
   reachable from `gitlab.uzh.ch` runners) or by hand.
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
   memory, so an always-on serving pod and a scheduled indexer pod cannot both fit
   under a 4 Gi *namespace-wide* ceiling. The Harbor project's 10 GB is the second.
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
  the namespace ceiling is 4 Gi *in total* — so one indexer pod all but exhausts
  the quota on its own, and an always-on serving pod holding its own copy of the
  model cannot coexist with it. That is the number the quota request rests on.

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
  `thesis-matchmaker-mcp` could not start at all — CI never installs the extra, so
  nothing caught it until an image was built. The adapter uses `MCPServer` from
  `mcp.server.mcpserver`, and passes `host`/`port` to `run()` instead of poking
  `mcp.settings`. The wire format was checked before and after: tool names,
  descriptions, output schemas and the `tools/call` response shape are unchanged.
  One field moved on purpose — `serverInfo.version` was reporting the SDK's version
  (`1.29.0`) and now reports the distribution's (`0.0.1`).
  **The module is still untested**, which is how this got missed; see
  [`adapters/README.md`](../src/thesis_matchmaker/adapters/README.md).

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
  image and we have four roles.
- **A new project has a 10 GB quota by default.** At ~0.45 GB compressed per
  image, two roles leave room for roughly twenty tags between them — workable, but
  worth knowing before tagging every commit, and Harbor does not garbage-collect
  old tags on its own. Raising the quota needs an admin.

`cr.gitlab.uzh.ch` is equally trusted, and is what CCS's own workshop example
uses. It was not chosen because the policy makes GitLab Container Scanning
*mandatory and ours to configure* there — unconfigured, we would be out of
compliance rather than merely unscanned.

Until the project exists, images are built and pushed by hand:

```bash
docker build -f projects/matcher/Dockerfile \
  -t registry.cs.zi.uzh.ch/TODO(ci)/thesis-matchmaker-indexer:<git-sha> .
docker push registry.cs.zi.uzh.ch/TODO(ci)/thesis-matchmaker-indexer:<git-sha>
```

Tag with the full commit SHA rather than a version: that is what CCS's examples
do, and it is what makes an Argo app file name one exact artefact.

**Build for `linux/amd64` explicitly** when building on an Apple Silicon machine.
`docker build` defaults to the host architecture, and an arm64 image will not run
on the cluster's nodes: `docker buildx build --platform linux/amd64 --load ...`.

### Base images

Compliance §2.3 asks for "offizielle und gehärtete Basis-Images" and recommends
**Alpine** or **Red Hat UBI**. All four of our Dockerfiles use
`python:3.12-slim` instead. That is defensible but is a deliberate deviation worth
stating rather than hiding: the image is official with verifiable provenance
(§2.1.1), and **Alpine is not available to us at all** for the two embeddings
images, because Alpine is musl-based and torch publishes no musl wheels. A UBI
Python base would satisfy the recommendation directly and has not been evaluated.
