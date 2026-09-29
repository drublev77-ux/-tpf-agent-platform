-- Client payments: one row per checkout link (a new link replaces an expired one).
CREATE TABLE IF NOT EXISTS payments (
    id                   BIGSERIAL PRIMARY KEY,
    order_id             BIGINT        NOT NULL REFERENCES orders(id),
    stage                TEXT          NOT NULL CHECK (stage IN ('prepayment', 'final')),
    amount               NUMERIC(12,2) NOT NULL CHECK (amount > 0),
    currency             TEXT          NOT NULL,
    provider             TEXT          NOT NULL,              -- stripe | manual
    provider_session_id  TEXT          UNIQUE,                -- Stripe Checkout Session id
    url                  TEXT,
    status               TEXT          NOT NULL DEFAULT 'pending'
                                       CHECK (status IN ('pending', 'paid', 'expired', 'cancelled')),
    created_at           TIMESTAMPTZ   NOT NULL DEFAULT now(),
    paid_at              TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS payments_order_idx ON payments (order_id, stage);
-- At most one paid row per order and stage: a duplicate webhook can never double-count.
CREATE UNIQUE INDEX IF NOT EXISTS payments_one_paid_per_stage ON payments (order_id, stage) WHERE status = 'paid';

ALTER TABLE payments ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON payments TO tpf_app;
GRANT USAGE, SELECT ON SEQUENCE payments_id_seq TO tpf_app;
DROP POLICY IF EXISTS tpf_app_all ON payments;
CREATE POLICY tpf_app_all ON payments TO tpf_app USING (true) WITH CHECK (true);
