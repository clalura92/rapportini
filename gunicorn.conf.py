# Gunicorn configuration.
#
# Gunicorn auto-loads ./gunicorn.conf.py from the working directory, so these
# settings apply even when the start command (render.yaml / Procfile / the
# Render dashboard "Start Command") does NOT pass the matching flags.
#
# Report generation is CPU-bound and can run for minutes, so raise the worker
# timeout well above gunicorn's 30s default to stop workers being SIGKILLed
# mid-run.
timeout = 300
graceful_timeout = 300
