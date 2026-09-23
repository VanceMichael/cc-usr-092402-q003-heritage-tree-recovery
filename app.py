from flask import Flask, jsonify
import sqlite3

app = Flask(__name__)

@app.get("/health")
def health():
    with sqlite3.connect("data/app.db") as db:
        db.execute("select 1")
    return jsonify(status="ok")
