-- Dedicated role for the platform (API + worker): data access only, no DDL.
-- The password is set outside the repo:  ALTER ROLE tpf_app LOGIN PASSWORD '<secret>';
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'tpf_app') THEN
    CREATE ROLE tpf_app NOLOGIN;
  END IF;
END $$;

GRANT USAGE ON SCHEMA public TO tpf_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON orders, tasks, artifacts, messages, llm_usage TO tpf_app;
GRANT SELECT, INSERT ON events TO tpf_app;
GRANT SELECT ON schema_migrations TO tpf_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO tpf_app;

-- RLS stays on for everyone else; tpf_app gets full access through its own policies.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['orders','events','tasks','artifacts','messages','llm_usage','schema_migrations'] LOOP
    EXECUTE format('DROP POLICY IF EXISTS tpf_app_all ON %I', t);
    EXECUTE format('CREATE POLICY tpf_app_all ON %I TO tpf_app USING (true) WITH CHECK (true)', t);
  END LOOP;
END $$;
