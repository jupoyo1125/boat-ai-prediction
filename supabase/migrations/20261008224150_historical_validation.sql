CREATE TABLE IF NOT EXISTS public.boat_historical_job (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.boat_historical_days (
    date DATE PRIMARY KEY,
    summary JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.boat_historical_job ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.boat_historical_days ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.boat_historical_job, public.boat_historical_days FROM anon, authenticated;
