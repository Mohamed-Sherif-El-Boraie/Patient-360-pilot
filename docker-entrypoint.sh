#!/bin/sh
# Image entrypoint: prepare the database, then start the command (CMD). Both jobs are idempotent and take advisory
# locks, so several instances starting together are safe. A failure stops the container, so App Service shows it.
# RUN_STARTUP_JOBS=0 skips them (for one-off commands that reuse the image).
set -eu

if [ "${RUN_STARTUP_JOBS:-1}" = "1" ]; then
    echo "startup: applying database migrations"
    python -m patient360.migrate
    echo "startup: loading the catalogue workbook"
    python -m patient360.load_catalogue
else
    echo "startup: RUN_STARTUP_JOBS=${RUN_STARTUP_JOBS}, skipping migrations and catalogue load"
fi

exec "$@"
