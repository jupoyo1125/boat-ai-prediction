-- Backend-only application tables. No client API access is granted.
CREATE TABLE IF NOT EXISTS public.performance_ledger (
    id TEXT PRIMARY KEY,
    record JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS public.ai_model_state (
    id INTEGER PRIMARY KEY,
    state JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS public.boat_automation_state (
    id INTEGER PRIMARY KEY,
    state JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS public.boat_storage_migrations (
    id TEXT PRIMARY KEY,
    receipt JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE public.performance_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ai_model_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.boat_automation_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.boat_storage_migrations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.performance_ledger, public.ai_model_state,
    public.boat_automation_state, public.boat_storage_migrations
    FROM PUBLIC, anon, authenticated;
