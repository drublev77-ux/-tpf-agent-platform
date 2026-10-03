-- Web applications built for clients: each order gets its own schema and login role in the tenant
-- database (a separate Postgres from this one), once for the review build and once for production.
CREATE TABLE IF NOT EXISTS app_databases (
    order_id        BIGINT      NOT NULL REFERENCES orders(id),
    env             TEXT        NOT NULL CHECK (env IN ('stg', 'prod')),
    role_name       TEXT        NOT NULL,
    db_password     TEXT        NOT NULL,
    session_secret  TEXT        NOT NULL,
    admin_password  TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (order_id, env)
);
ALTER TABLE app_databases ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON app_databases TO tpf_app;
DROP POLICY IF EXISTS tpf_app_all ON app_databases;
CREATE POLICY tpf_app_all ON app_databases TO tpf_app USING (true) WITH CHECK (true);
