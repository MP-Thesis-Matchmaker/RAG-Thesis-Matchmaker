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
    -- Token window the corpus was embedded at, and how many documents reached it.
    -- The window belongs here for the same reason embedding_model does: changing it
    -- changes every vector, but it does NOT change any document's content hash, so
    -- a plain re-index would skip everything and leave two incompatible
    -- generations of vector side by side. indexing/indexer.py guards on it.
    -- Nullable: the offline hash-fake embedder has no window to record.
    max_seq_length  int,
    truncated_docs  int,
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

-- ---------------------------------------------------------------------------
-- Scraped thesis postings
-- ---------------------------------------------------------------------------

-- Written only by scraper/store.py. Until this table existed, `ThesisPosting`
-- had no producer at all and the posting half of the index came from 20 invented
-- fixtures in data/samples.
CREATE TABLE posting (
    -- The scraper's topic_id: sha1 over the source url and the record's own seed,
    -- so it is stable across runs and lines up with the indexer's content-hash
    -- diff on the same key that `publication.id` does.
    id             text PRIMARY KEY,
    title          text,
    description    text,
    -- Everyone the page names as supervising this topic: [{name, email,
    -- profile_url, chair}]. jsonb rather than text[] because an entry is a record,
    -- not a string -- only `name` is dependable, and 120 of 264 scraped entries
    -- carry a profile link instead of an email.
    supervisors    jsonb NOT NULL DEFAULT '[]'::jsonb,
    faculty        text,
    department     text,
    -- Every level the topic is open to. A real array, and the reason it has to be:
    -- 121 of 247 scraped topics read "Bachelor, Master". A scalar column would
    -- force half the corpus to pick one and vanish from the other's queries.
    -- `&&` against it is an overlap test, which is exactly the question asked.
    degree_levels  text[] NOT NULL DEFAULT '{}',
    -- open / assigned / pending / private. Pages mark topics as taken rather than
    -- removing them, so without this an assigned topic is indistinguishable from
    -- an available one. 26 of 247 are not open.
    status         text,
    keywords       text[] NOT NULL DEFAULT '{}',
    language       text,
    url            text,
    -- Only set when the page gave an unambiguous ISO date; a locale guess would
    -- put wrong dates in the row.
    listed_on      date,
    -- Which registry source produced this, for drift attribution.
    source_id      text,
    scraped_at     timestamptz,
    stored_at      timestamptz NOT NULL DEFAULT now()
);

-- Overlap queries ("open to a master's student") without a table scan.
CREATE INDEX posting_degree_levels_gin ON posting USING gin (degree_levels);

-- The indexer reads open postings far more often than any other slice.
CREATE INDEX posting_status ON posting (status);

-- A researcher as their own department page describes them, rather than as ZORA
-- infers them from authorship. Written by scraper/store.py; nothing reads it yet
-- -- see the Known gaps section of scraper/README.md.
CREATE TABLE researcher_profile (
    id                text PRIMARY KEY,
    name              text,
    email             text,
    role              text,
    -- The reason this table is worth having: a person stating their interests in
    -- their own words is an independent signal from what publications imply.
    research_interest text,
    research_field    text,
    research_group    text,
    bio               text,
    personal_website  text,
    profile_url       text,
    faculty           text,
    department        text,
    source_id         text,
    scraped_at        timestamptz,
    stored_at         timestamptz NOT NULL DEFAULT now()
);

-- How to apply, per unit and degree level. One row per (unit, level): the scraper
-- consolidates a Bachelor page and a Bachelor PDF into one entry, which is why
-- source_ids is plural. Written by scraper/store.py; not read yet.
CREATE TABLE application_process (
    id             text PRIMARY KEY,
    degree_level   text,
    description    text,
    -- [{url, description}] pulled off the page.
    relevant_links jsonb NOT NULL DEFAULT '[]'::jsonb,
    url            text,
    faculty        text,
    -- NULL for a faculty-scope page covering no single institute.
    department     text,
    source_ids     text[] NOT NULL DEFAULT '{}',
    scraped_at     timestamptz,
    stored_at      timestamptz NOT NULL DEFAULT now()
);
