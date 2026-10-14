-- Run as the project's postgres administrator. Uses the existing Supabase database.
-- Only public health/status GET requests are sent; no credentials are stored.
-- The Japan-time guard prevents waking Render outside 07:00-23:00.
CREATE EXTENSION IF NOT EXISTS pg_cron WITH SCHEMA pg_catalog;
CREATE EXTENSION IF NOT EXISTS pg_net WITH SCHEMA extensions;
SELECT cron.schedule(
    'boat-ai-live-healthcheck',
    '*/5 * * * *',
    $job$
    SELECT net.http_get(
        url := endpoint,
        headers := '{"User-Agent":"BOAT-AI-Supabase-Cron/1.0","Cache-Control":"no-cache"}'::jsonb,
        timeout_milliseconds := 90000
    )
    FROM (VALUES
        ('https://boat-ai-prediction.onrender.com/health'),
        ('https://boat-ai-prediction.onrender.com/api/automation')
    ) AS endpoints(endpoint)
    WHERE EXTRACT(HOUR FROM NOW() AT TIME ZONE 'Asia/Tokyo') >= 7
      AND EXTRACT(HOUR FROM NOW() AT TIME ZONE 'Asia/Tokyo') < 23;
    $job$
);

-- To pause only this monitor (without dropping either extension):
-- SELECT cron.alter_job(job_id := (SELECT jobid FROM cron.job
--   WHERE jobname = 'boat-ai-live-healthcheck'), active := false);
