-- Pictures drawn for client sites (tools/imagegen.py). One row per order and prompt: a build that keeps the
-- prompt reuses the picture instead of paying for it again.
CREATE TABLE IF NOT EXISTS generated_images (
    order_id   BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    key        TEXT NOT NULL,              -- sha256 of model, aspect and prompt
    file       TEXT NOT NULL,
    prompt     TEXT NOT NULL,
    aspect     TEXT NOT NULL,
    model      TEXT NOT NULL,
    data       BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (order_id, key)
);
ALTER TABLE generated_images ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON generated_images TO tpf_app;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'generated_images' AND policyname = 'tpf_app_all') THEN
    CREATE POLICY tpf_app_all ON generated_images TO tpf_app USING (true) WITH CHECK (true);
  END IF;
END $$;
