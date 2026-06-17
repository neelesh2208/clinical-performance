import os

DB_CONFIG = {
    "host":     os.environ.get("DB_HOST"),
    "database": os.environ.get("DB_NAME"),
    "user":     os.environ.get("DB_USER"),
    "password": os.environ.get("DB_PASSWORD"),
    "port":     os.environ.get("DB_PORT", "5432"),
}