#!/bin/bash
set -e
gunicorn config.wsgi --bind 0.0.0.0:${PORT:-8000} --log-file - --timeout 120 --workers 2
