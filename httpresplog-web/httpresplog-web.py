#!/usr/bin/env python3

"""httpresplog-web - a small web display of httpresplog data

Display a simple web page with a httpresplog data graph. Uses flask
and needs httpresplog for data generation.

THIS IS THE WORK OF JUST A FEW HOURS, it needs a lot of polishing and
cleaning, but it works. The goal was speed of development and being self
contained and easy to deploy.

Running with gunicorn:
    DB_NAME="DB-NAME" DB_USER="DB-USER" DB_PASS="DB-PASSWORD" gunicorn -w 4 -b 127.0.0.1:5000 httpresplog-web:app
"""

from flask import Flask
from flask import render_template
from flask import jsonify
import time
import os
import datetime
import pymysql
import pymysql.cursors

app = Flask(__name__)


DB_NAME = os.environ['DB_NAME']
DB_USER = os.environ['DB_USER']
DB_PASS = os.environ['DB_PASS']
DATA_URLS = (os.environ.get('DATA_URLS', None) or "/data/").strip().split()


def db_connect(db, username, password):
    dbcon = pymysql.connect(
        host="localhost",
        user=username,
        password=password,
        db=db,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    return dbcon


def db_get_1h_rows(dbcon):
    max_age_dt = datetime.datetime.now() - datetime.timedelta(days=30)
    max_age_str = "%04d-%02d-%02d" % (max_age_dt.year, max_age_dt.month, max_age_dt.day)
    with dbcon.cursor() as cursor:
        q = (
            "SELECT 1h_avg_results.url_id, 1h_avg_results.avg, 1h_avg_results.timestamp FROM `1h_avg_results`, `urls` WHERE urls.id=1h_avg_results.url_id and urls.display=1 and 1h_avg_results.timestamp > %s order by 1h_avg_results.timestamp"
            % (max_age_str)
        )
        cursor.execute(q)
        result = cursor.fetchall()
    return result


def db_get_url_label(dbcon, url_id):
    with dbcon.cursor() as cursor:
        q = "SELECT `label` FROM `urls` WHERE id = %s"
        cursor.execute(q, (url_id,))
        result = cursor.fetchone()['label']
    return result


def graph_data_set_labels(data, dbcon):
    for dataset in data['datasets'].values():
        dataset['label'] = db_get_url_label(dbcon, dataset['url_id'])


def get_graph_data():
    dbcon = db_connect(DB_NAME, DB_USER, DB_PASS)
    rows = db_get_1h_rows(dbcon)
    data = {"ts": time.time(), "datasets": {}}
    datasets = data["datasets"]
    for row in rows:
        url_id = row["url_id"]
        if url_id not in datasets:
            datasets[url_id] = {"url_id": url_id, "data": [], 'label': ''}
        row_ts = row["timestamp"]
        datasets[url_id]["data"].append(
            ((row_ts.year, row_ts.month, row_ts.day, row_ts.hour, row_ts.minute), float(row["avg"]) / 10000)
        )
    graph_data_set_labels(data, dbcon)
    return data


@app.route("/")
def index():
    return render_template("index.html", data_urls=DATA_URLS)


@app.route("/data/")
def data():
    ret = {"data": get_graph_data()}
    return jsonify(ret)
