-- Settings the platform provisions for itself at runtime (e.g. the Stripe webhook signing secret,
-- which Stripe reveals only once, when the worker creates the endpoint). Readable by tpf_app only.
CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE app_settings ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON app_settings TO tpf_app;
DROP POLICY IF EXISTS tpf_app_all ON app_settings;
CREATE POLICY tpf_app_all ON app_settings TO tpf_app USING (true) WITH CHECK (true);
