import os

from flask import Flask, jsonify

from .api import bp, close_db
from .db import connect, migrate


def create_app(db_path="data/app.db"):
    app = Flask(__name__)
    app.config["DB_PATH"] = str(db_path)
    if str(db_path) != ":memory:":
        directory = os.path.dirname(str(db_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
    db = connect(app.config["DB_PATH"])
    try:
        migrate(db)
    finally:
        db.close()

    app.register_blueprint(bp)
    app.teardown_appcontext(close_db)

    @app.get("/health")
    def health():
        conn = connect(app.config["DB_PATH"])
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
        return jsonify(status="ok")

    return app
