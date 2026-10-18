#!/bin/bash
echo "Building the project..."
pip install -r requirements.txt
python manage.py collectstatic --noinput --clear
python manage.py migrate --noinput