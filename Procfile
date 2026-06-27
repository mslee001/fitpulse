release: python manage.py migrate --noinput
web: gunicorn peloton_dashboard.wsgi --log-file -
