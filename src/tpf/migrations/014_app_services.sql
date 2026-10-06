-- What a delivered application asks the platform to do for it: send an e-mail, ask the AI model, and be woken
-- on a schedule; and the keys of outside services its owner entered on the order page.
CREATE TABLE IF NOT EXISTS app_mail (
    id         BIGSERIAL   PRIMARY KEY,
    order_id   BIGINT      NOT NULL REFERENCES orders(id),
    env        TEXT        NOT NULL,
    recipient  TEXT        NOT NULL,
    subject    TEXT        NOT NULL,
    delivered  BOOLEAN     NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS app_mail_order_time ON app_mail (order_id, created_at);
CREATE TABLE IF NOT EXISTS app_ai_usage (
    id            BIGSERIAL   PRIMARY KEY,
    order_id      BIGINT      NOT NULL REFERENCES orders(id),
    env           TEXT        NOT NULL,
    input_tokens  INTEGER     NOT NULL,
    output_tokens INTEGER     NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS app_ai_usage_order_time ON app_ai_usage (order_id, created_at);
CREATE TABLE IF NOT EXISTS app_ticks (
    order_id    BIGINT      NOT NULL REFERENCES orders(id),
    env         TEXT        NOT NULL,
    last_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_status INTEGER,
    failures    INTEGER     NOT NULL DEFAULT 0,
    PRIMARY KEY (order_id, env)
);
CREATE TABLE IF NOT EXISTS service_keys (
    order_id   BIGINT      NOT NULL REFERENCES orders(id),
    key_id     TEXT        NOT NULL,
    value      TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (order_id, key_id)
);
ALTER TABLE app_mail ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_ai_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_ticks ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_keys ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON app_mail, app_ai_usage, app_ticks, service_keys TO tpf_app;
GRANT USAGE, SELECT ON SEQUENCE app_mail_id_seq, app_ai_usage_id_seq TO tpf_app;
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['app_mail', 'app_ai_usage', 'app_ticks', 'service_keys'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = t AND policyname = 'tpf_app_all') THEN
      EXECUTE format('CREATE POLICY tpf_app_all ON %I TO tpf_app USING (true) WITH CHECK (true)', t);
    END IF;
  END LOOP;
END $$;
