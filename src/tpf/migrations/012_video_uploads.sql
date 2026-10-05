-- A video a client attaches to a new order instead of (or besides) describing it in words. The file arrives in
-- pieces, waits here until a worker has watched it, and is deleted as soon as the order has its description.
CREATE TABLE IF NOT EXISTS video_uploads (
    id         BIGSERIAL   PRIMARY KEY,
    token      TEXT        NOT NULL,
    order_id   BIGINT      REFERENCES orders(id),
    filename   TEXT        NOT NULL DEFAULT '',
    size       BIGINT      NOT NULL,
    received   BIGINT      NOT NULL DEFAULT 0,
    ip         TEXT        NOT NULL DEFAULT '',
    status     TEXT        NOT NULL DEFAULT 'uploading' CHECK (status IN ('uploading', 'complete', 'processed', 'failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS video_chunks (
    upload_id BIGINT  NOT NULL REFERENCES video_uploads(id) ON DELETE CASCADE,
    n         INTEGER NOT NULL,
    data      BYTEA   NOT NULL,
    PRIMARY KEY (upload_id, n)
);
ALTER TABLE video_uploads ENABLE ROW LEVEL SECURITY;
ALTER TABLE video_chunks ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON video_uploads, video_chunks TO tpf_app;
GRANT USAGE, SELECT ON SEQUENCE video_uploads_id_seq TO tpf_app;
DROP POLICY IF EXISTS tpf_app_all ON video_uploads;
CREATE POLICY tpf_app_all ON video_uploads TO tpf_app USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS tpf_app_all ON video_chunks;
CREATE POLICY tpf_app_all ON video_chunks TO tpf_app USING (true) WITH CHECK (true);
