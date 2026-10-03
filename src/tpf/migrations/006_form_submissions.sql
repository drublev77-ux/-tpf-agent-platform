-- Messages sent through the contact form of a client's site built by the platform.
CREATE TABLE IF NOT EXISTS form_submissions (
    id          BIGSERIAL PRIMARY KEY,
    order_id    BIGINT      NOT NULL REFERENCES orders(id),
    data        JSONB       NOT NULL,
    delivered   BOOLEAN     NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS form_submissions_order_idx ON form_submissions (order_id, created_at);

ALTER TABLE form_submissions ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON form_submissions TO tpf_app;
GRANT USAGE, SELECT ON SEQUENCE form_submissions_id_seq TO tpf_app;
DROP POLICY IF EXISTS tpf_app_all ON form_submissions;
CREATE POLICY tpf_app_all ON form_submissions TO tpf_app USING (true) WITH CHECK (true);
