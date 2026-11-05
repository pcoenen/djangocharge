#!/bin/bash
set -e
python manage.py migrate --noinput
python manage.py ensure_superuser
