-- Telegram bots built for clients: the bot token the client entered on their order page, and where the
-- bot's webhook currently points.
CREATE TABLE IF NOT EXISTS bot_connections (
    order_id      BIGINT      PRIMARY KEY REFERENCES orders(id),
    token         TEXT        NOT NULL,
    username      TEXT        NOT NULL DEFAULT '',
    connected_env TEXT        CHECK (connected_env IN ('stg', 'prod')),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE bot_connections ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON bot_connections TO tpf_app;
DROP POLICY IF EXISTS tpf_app_all ON bot_connections;
CREATE POLICY tpf_app_all ON bot_connections TO tpf_app USING (true) WITH CHECK (true);
