-- Execute against the destination after storage_schema.sql has been applied.
BEGIN;
CREATE EXTENSION IF NOT EXISTS pgtap WITH SCHEMA extensions;
SET LOCAL search_path = public, extensions;
SELECT plan(12);
SELECT ok(c.relrowsecurity, format('%s enables RLS', c.relname))
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relname IN
    ('performance_ledger','ai_model_state','boat_automation_state','boat_storage_migrations')
ORDER BY c.relname;
SELECT ok(NOT has_table_privilege(r.role, 'public.' || t.name, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'),
          format('%s cannot access %s', r.role, t.name))
FROM (VALUES ('anon'), ('authenticated')) AS r(role)
CROSS JOIN (VALUES ('performance_ledger'), ('ai_model_state'),
                   ('boat_automation_state'), ('boat_storage_migrations')) AS t(name)
ORDER BY r.role, t.name;
SELECT * FROM finish();
ROLLBACK;
