BEGIN;
CREATE EXTENSION IF NOT EXISTS pgtap WITH SCHEMA extensions;
SET LOCAL search_path = public, extensions;
SELECT plan(6);
SELECT ok(c.relrowsecurity, format('%s enables RLS', c.relname))
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relname IN ('boat_historical_job','boat_historical_days')
ORDER BY c.relname;
SELECT ok(NOT has_table_privilege(r.role, 'public.' || t.name, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'),
          format('%s cannot access %s', r.role, t.name))
FROM (VALUES ('anon'), ('authenticated')) AS r(role)
CROSS JOIN (VALUES ('boat_historical_job'), ('boat_historical_days')) AS t(name)
ORDER BY r.role, t.name;
SELECT * FROM finish();
ROLLBACK;
