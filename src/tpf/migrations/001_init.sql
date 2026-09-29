-- TPF Agent Platform: core schema (pilot)

CREATE TABLE IF NOT EXISTS orders (
    id              BIGSERIAL PRIMARY KEY,
    state           TEXT        NOT NULL,
    paused_from     TEXT,                       -- state to return to after an escalation
    client_name     TEXT        NOT NULL,
    client_email    TEXT        NOT NULL,
    client_token    TEXT        NOT NULL,       -- secret the client uses to act on the order
    channel         TEXT        NOT NULL DEFAULT 'site',
    language        TEXT        NOT NULL DEFAULT 'ru',
    request_text    TEXT        NOT NULL,
    check_attempts  INT         NOT NULL DEFAULT 0,
    token_budget    BIGINT      NOT NULL,
    tokens_used     BIGINT      NOT NULL DEFAULT 0,
    staging_url     TEXT,
    production_url  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS orders_state_idx ON orders (state);

-- Everything that happens to an order. Append-only: the client portal, audit and metrics read from here.
CREATE TABLE IF NOT EXISTS events (
    id          BIGSERIAL PRIMARY KEY,
    order_id    BIGINT      NOT NULL REFERENCES orders(id),
    kind        TEXT        NOT NULL,           -- transition | agent | tool | client | system
    name        TEXT        NOT NULL,
    from_state  TEXT,
    to_state    TEXT,
    actor       TEXT        NOT NULL,           -- agent name, 'client', 'operator', 'system'
    data        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_order_idx ON events (order_id, id);

CREATE OR REPLACE FUNCTION events_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'events is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS events_no_update ON events;
CREATE TRIGGER events_no_update BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION events_append_only();

-- Work queue. Workers claim with FOR UPDATE SKIP LOCKED; a lease protects against crashed workers.
CREATE TABLE IF NOT EXISTS tasks (
    id            BIGSERIAL PRIMARY KEY,
    order_id      BIGINT      NOT NULL REFERENCES orders(id),
    kind          TEXT        NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'queued',   -- queued | running | done | failed | cancelled
    attempts      INT         NOT NULL DEFAULT 0,
    payload       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    result        JSONB,
    error         TEXT,
    run_after     TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_until  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS tasks_claim_idx ON tasks (status, run_after);
-- At most one live task per order: agents never work on the same order concurrently in the pilot.
CREATE UNIQUE INDEX IF NOT EXISTS tasks_one_live_per_order
    ON tasks (order_id) WHERE status IN ('queued', 'running');

-- Versioned work products: spec, estimate, architecture, code, qa report, deployment.
CREATE TABLE IF NOT EXISTS artifacts (
    id          BIGSERIAL PRIMARY KEY,
    order_id    BIGINT      NOT NULL REFERENCES orders(id),
    kind        TEXT        NOT NULL,
    version     INT         NOT NULL,
    content     JSONB       NOT NULL,
    author      TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (order_id, kind, version)
);

-- Client messages and decisions, kept verbatim.
CREATE TABLE IF NOT EXISTS messages (
    id          BIGSERIAL PRIMARY KEY,
    order_id    BIGINT      NOT NULL REFERENCES orders(id),
    direction   TEXT        NOT NULL,            -- in (from client) | out (to client)
    text        TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every model call, for budgets and cost reporting.
CREATE TABLE IF NOT EXISTS llm_usage (
    id             BIGSERIAL PRIMARY KEY,
    order_id       BIGINT      NOT NULL REFERENCES orders(id),
    agent          TEXT        NOT NULL,
    model          TEXT        NOT NULL,
    input_tokens   INT         NOT NULL,
    output_tokens  INT         NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    name        TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
