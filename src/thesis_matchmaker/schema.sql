-- The whole database schema. Edited in place, not extended by numbered deltas:
-- see schema.py for why, and for the point at which that stops being true.
--
-- Two concerns live here. The ingestion tables (publication, harvest_state) are
-- written only by the zora package -- invariant 1, ingestion owns all writes.
-- The index tables (document, index_manifest) are written only by indexing/ and
-- read by retrieval/.
--
-- Replaces the ChromaDB on-disk index (data/index/). Chroma was chosen when the
-- deployment target was unknown; it is embedded and file-backed, which in
-- Kubernetes means a PersistentVolumeClaim holding a SQLite file that cannot be
-- shared between replicas, sitting outside the database that holds the data.

-- Requires either a superuser role or the extension being marked trusted. If
-- this statement fails with "permission denied to create extension", ask UZH
-- Central Informatics to pre-create it -- see docs/deployment.md.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE document (
    id           text PRIMARY KEY,
    -- Promoted out of metadata into a column of its own. It is the one filter
    -- every retrieval query applies and the one that partitions the corpus, and
    -- a partial index predicate has to be the *same expression* as the query's
    -- WHERE clause for the planner to use it. As jsonb it could only be written
    -- `metadata ->> 'source_type' = ...` in the index while queries use
    -- `metadata @> '{...}'`, and Postgres cannot prove one implies the other --
    -- so the partial indexes below would have been unusable at any scale.
    source_type  text NOT NULL,
    text         text NOT NULL,
    metadata     jsonb NOT NULL DEFAULT '{}'::jsonb,
    content_hash text NOT NULL,
    -- 1024 = BAAI/bge-m3. Kept in step with EMBEDDING_DIM in
    -- indexing/embedder.py; a model of a different width needs a migration.
    embedding    vector(1024) NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Retrieval filters are flat equality over metadata, which maps exactly onto
-- jsonb containment (`metadata @> '{"source_type": "publication"}'`).
-- jsonb_path_ops indexes only containment, which is all we ask of it, and is
-- smaller than the default jsonb_ops.
CREATE INDEX document_metadata_gin ON document USING gin (metadata jsonb_path_ops);

-- Two partial HNSW indexes rather than one global index. This is a correctness
-- measure, not an optimisation: pgvector applies the WHERE clause *after* the
-- index scan returns its candidate set, so a selective filter over one global
-- graph silently returns fewer than the requested top_k. Thesis postings are a
-- small minority of the corpus, which is exactly the case that breaks. A
-- per-source_type graph only ever contains eligible rows.
--
-- The predicates match the query's `WHERE source_type = ...` verbatim, which is
-- what makes them usable at all -- see the note on the column above.
CREATE INDEX document_hnsw_publication ON document
    USING hnsw (embedding vector_cosine_ops)
    WHERE source_type = 'publication';

CREATE INDEX document_hnsw_posting ON document
    USING hnsw (embedding vector_cosine_ops)
    WHERE source_type = 'thesis_posting';

-- Single-row table replacing data/index/manifest.json. Its existence is also
-- the "has an index been built?" signal the CLI and the MCP adapter check
-- before falling back to the fake retriever.
CREATE TABLE index_manifest (
    id              int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    embedding_model text        NOT NULL,
    embedding_dim   int         NOT NULL,
    document_count  int         NOT NULL,
    sources         text,
    built_at        timestamptz NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------------
-- Ingestion: harvested ZORA publications, and the incremental watermark.
-- Written only by thesis_matchmaker.zora (invariant 1).
-- ---------------------------------------------------------------------------

-- Replaces data/publications.jsonl. A 45 MB file rewritten in full on every
-- harvest, committed into git by a bot, and re-parsed end to end by every reader
-- that wanted one record.
CREATE TABLE publication (
    -- The ZORA handle. Stable across harvests, which is what makes upserts and
    -- the indexer's content-hash diff line up on the same key.
    id                   text PRIMARY KEY,
    doi                  text,
    title                text,
    abstract             text,
    -- Real arrays rather than JSON strings: unlike the index's metadata blob,
    -- these are queryable (see the GIN index below).
    authors              text[] NOT NULL DEFAULT '{}',
    -- Authors carrying a CRIS authority key, i.e. registered UZH researchers.
    -- Only these are supervisor-eligible.
    uzh_authors          text[] NOT NULL DEFAULT '{}',
    -- author name -> CRIS Person UUID, null for external co-authors. A map, so
    -- jsonb rather than an array.
    author_authority_map jsonb NOT NULL DEFAULT '{}'::jsonb,
    year                 int,
    publication_type     text,
    department           text,
    language             text,
    keywords             text[] NOT NULL DEFAULT '{}',
    url                  text,
    -- dc.date.accessioned as ZORA reports it. Kept per row so the incremental
    -- watermark can be recomputed from the data instead of being trusted blindly.
    accessioned          text,
    harvested_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX publication_department ON publication (department);

-- Answers "which publications is this researcher on?" without a table scan.
-- This is the join key a future researcher-level rollup or the ranking package
-- would need.
CREATE INDEX publication_uzh_authors_gin ON publication USING gin (uzh_authors);

-- Replaces data/state.json. Single row: the harvester is a singleton job, and
-- making that a constraint beats hoping.
CREATE TABLE harvest_state (
    id                        int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    -- High-water mark for incremental harvests: dc.date.accessioned of the newest
    -- item seen. Text, not timestamptz, because it is fed back verbatim into a
    -- Solr range query.
    last_accessioned          text,
    -- Row count at the end of the last successful run, for the retention safety
    -- check that refuses a harvest which lost most of the corpus.
    last_total_publications   int NOT NULL DEFAULT 0,
    last_run_at               timestamptz,
    -- Per-mode stamps. Only the in-process scheduler reads these; they become
    -- redundant the moment Kubernetes CronJobs own the cadence, since the cluster
    -- tracks its own run history.
    last_incremental_run_at   timestamptz,
    last_full_run_at          timestamptz
);
