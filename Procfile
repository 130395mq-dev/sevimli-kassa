web: python manage.py migrate --noinput && python manage.py collectstatic --noinput && python manage.py setup_local && gunicorn config.wsgi --bind 0.0.0.0:$PORT --workers 2 --timeout 120
