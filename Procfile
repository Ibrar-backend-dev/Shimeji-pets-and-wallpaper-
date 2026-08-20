# Railway / Heroku-style process declaration.
# migrate runs in the start command because free tiers have no release phase.
web: python manage.py migrate --noinput && gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers ${WEB_CONCURRENCY:-2} --threads ${WEB_THREADS:-4} --worker-class gthread --timeout 30 --max-requests 1000 --max-requests-jitter 100 --access-logfile - --error-logfile -

# Optional scheduled jobs. Railway can run these on a cron schedule; on Render's
# free plan call the /internal/cron/* endpoints from an external scheduler instead.
reap: python manage.py reap_orphans
reconcile: python manage.py reconcile_counts
