-- Supabase exposes the public schema over its REST API. The platform talks to Postgres directly
-- (as the table owner, which bypasses RLS), so every table gets RLS with no policies:
-- nothing is reachable through the anon/authenticated API keys.
ALTER TABLE orders            ENABLE ROW LEVEL SECURITY;
ALTER TABLE events            ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks             ENABLE ROW LEVEL SECURITY;
ALTER TABLE artifacts         ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages          ENABLE ROW LEVEL SECURITY;
ALTER TABLE llm_usage         ENABLE ROW LEVEL SECURITY;
ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;

ALTER FUNCTION events_append_only() SET search_path = public;
