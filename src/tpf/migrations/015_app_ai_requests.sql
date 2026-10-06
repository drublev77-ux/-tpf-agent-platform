-- The web side of the platform holds no key of the AI provider: a request from an application waits here for a
-- worker, which holds the key, asks the model and leaves the answer. Rows live for minutes.
CREATE TABLE IF NOT EXISTS app_ai_requests (
    id         BIGSERIAL   PRIMARY KEY,
    order_id   BIGINT      NOT NULL REFERENCES orders(id),
    status     TEXT        NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done')),
    request    JSONB       NOT NULL,
    response   JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS app_ai_requests_queued ON app_ai_requests (id) WHERE status = 'queued';
ALTER TABLE app_ai_requests ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON app_ai_requests TO tpf_app;
GRANT USAGE, SELECT ON SEQUENCE app_ai_requests_id_seq TO tpf_app;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'app_ai_requests' AND policyname = 'tpf_app_all') THEN
    CREATE POLICY tpf_app_all ON app_ai_requests TO tpf_app USING (true) WITH CHECK (true);
  END IF;
END $$;
