-- Online shops built for clients: the Stripe key the owner entered on their order page.
CREATE TABLE IF NOT EXISTS payment_connections (
    order_id      BIGINT      PRIMARY KEY REFERENCES orders(id),
    secret_key    TEXT        NOT NULL,
    mode          TEXT        NOT NULL CHECK (mode IN ('test', 'live')),
    connected_env TEXT        CHECK (connected_env IN ('stg', 'prod')),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE payment_connections ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON payment_connections TO tpf_app;
DROP POLICY IF EXISTS tpf_app_all ON payment_connections;
CREATE POLICY tpf_app_all ON payment_connections TO tpf_app USING (true) WITH CHECK (true);
