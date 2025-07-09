#!/bin/sh

# Run the Flask app
exec gunicorn --bind 0.0.0.0:5000 app.main:app