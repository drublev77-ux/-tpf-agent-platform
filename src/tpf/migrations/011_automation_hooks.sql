-- Where a delivered product reports its events for the client's own automation (n8n, Zapier, Make or a CRM):
-- the address the client entered on their order page and the key each delivery is signed with.
CREATE TABLE IF NOT EXISTS automation_hooks (
    order_id    BIGINT      PRIMARY KEY REFERENCES orders(id),
    url         TEXT        NOT NULL,
    secret      TEXT        NOT NULL,
    last_status INTEGER,
    last_at     TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE automation_hooks ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON automation_hooks TO tpf_app;
DROP POLICY IF EXISTS tpf_app_all ON automation_hooks;
CREATE POLICY tpf_app_all ON automation_hooks TO tpf_app USING (true) WITH CHECK (true);
