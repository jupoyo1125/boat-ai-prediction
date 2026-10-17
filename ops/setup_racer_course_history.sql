-- Server-only cache. Existing prediction and learning tables are unchanged.
CREATE TABLE IF NOT EXISTS public.boat_racer_course_days (
    day TEXT PRIMARY KEY CHECK (day ~ '^[0-9]{8}$'),
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE public.boat_racer_course_days ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.boat_racer_course_days FROM PUBLIC, anon, authenticated;
