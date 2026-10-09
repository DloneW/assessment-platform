#!/usr/bin/env bash
# Build step for the hosting platform: install dependencies and collect static files.
# Database migrations run at start (see render.yaml) because the platform's free tier has no pre-deploy hook.
set -o errexit
pip install -r requirements.txt
python manage.py collectstatic --noinput
