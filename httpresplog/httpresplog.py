#!/usr/bin/env python3
"""httpresplog - HTTP response time logging.

Similar to https://github.com/sii/httpresptime except it logs to a database,
and a lot of options have been stripped out.

THIS IS THE WORK OF JUST A FEW HOURS, it needs a lot of polishing and
cleaning, but it works. The goal was speed of development and being self
contained and easy to deploy.

Requires requests and pymysql
pip install requests
pip install PyMySQL

DB spec:

CREATE TABLE `urls` (
  `id` int NOT NULL AUTO_INCREMENT,
  `url` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `label` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL,
  `display` tinyint DEFAULT 1 NOT NULL,
  PRIMARY KEY (`id`),
  CONSTRAINT url_unique UNIQUE (url)
);
CREATE INDEX url_idx ON urls (url(50));

CREATE TABLE `5m_results` (
  `id` int NOT NULL AUTO_INCREMENT,
  `url_id` int NOT NULL,
  `min` int NOT NULL,
  `max` int NOT NULL,
  `avg` int NOT NULL,
  `status` tinyint NOT NULL,
  `timestamp` datetime(6) NOT NULL,
  PRIMARY KEY (`id`)
);
CREATE INDEX url_ts_idx ON 5m_results (url_id, timestamp);

CREATE TABLE `1h_avg_results` (
  `id` int NOT NULL AUTO_INCREMENT,
  `url_id` int NOT NULL,
  `avg` int NOT NULL,
  `timestamp` datetime(6) NOT NULL,
  PRIMARY KEY (`id`)
);
CREATE INDEX url_ts_idx ON 1h_avg_results (url_id, timestamp);
"""

import time
import argparse
import pymysql
import pymysql.cursors

import datetime
import requests
from urllib.parse import urlparse

# Disable warnings about not doing SSL verification
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CHROME_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/51.0.2704.103 Safari/537.36"


class URL:
    """Simple URL storage class.

    Gets the redirected URL (if any redirects are done).
    Matches against an URL id already stored in the DB.
    """
    def __init__(self, url, dbcon):
        self.dbcon = dbcon
        if "://" not in url:
            url = "https://%s" % url
        self.url = get_redirected_url(url)
        self.url_id = None
        self.db_init_url()
        self.results = []

    def __str__(self):
        return 'URL<id: %d, url: %s>' % (self.url_id, self.url)

    def db_init_url(self):
        self.url_id = self.db_get_url_id()
        if not self.url_id:
            self.db_add_url()
            self.url_id = self.db_get_url_id()

    def db_add_url(self):
        with self.dbcon.cursor() as cursor:
            q = "INSERT INTO `urls` (url) values (%s)"
            cursor.execute(q, (self.url,))
        self.dbcon.commit()

    def db_get_url_id(self):
        with self.dbcon.cursor() as cursor:
            q = "SELECT `id` FROM `urls` WHERE `url`=%s"
            cursor.execute(q, (self.url,))
            result = cursor.fetchone()
        ret = None
        if result:
            ret = result['id']
        return ret


def request_headers(headers={}):
    """Permanent store for request headers.

    Returns a dict of request headers that can be updated for future calls.
    """
    return headers


def get_redirected_url(url):
    """Get the final redirected URL for en URL."""
    resp = requests.get(url, verify=False, headers=request_headers(), timeout=10)
    return resp.url


def get_url_hostname(url):
    """Return the hostname part of an URL."""
    p_url = urlparse(url)
    return p_url.hostname


def time_url(url, num_requests, use_keepalive):
    """Perform response time measurements for an URL."""
    if use_keepalive:
        session = requests.Session()
        session.get(url, verify=False, headers=request_headers())
    else:
        session = requests
    resp_times = []
    for _ in range(num_requests):
        start = int(time.time() * 10000)
        r = session.get(url, verify=False, headers=request_headers(), timeout=30)
        r.raise_for_status()
        end = int(time.time() * 10000)
        resp_times.append(end - start)
    ret = calc_resp_times(resp_times)
    ret["last_status_code"] = r.status_code
    ret["last_size"] = len(r.text)
    return ret


def calc_resp_times(resp_times):
    """Calculate response times collected by time_url."""
    first = True
    ret = {"min_time": None, "max_time": None, "avg_time": None}
    total_time = 0
    for resp_time in resp_times:
        if first:
            ret["min_time"] = ret["max_time"] = resp_time
            first = False
        ret["min_time"] = min(ret["min_time"], resp_time)
        ret["max_time"] = max(ret["max_time"], resp_time)
        total_time += resp_time
    ret["avg_time"] = int(total_time / len(resp_times))
    return ret


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


def db_log_5m_result(url, dbcon, result):
    ts = datetime.datetime.now()
    q = """INSERT INTO 5m_results (url_id, min, max, avg, status, timestamp) values (%s, %s, %s, %s, %s, %s)"""
    q_args = (url.url_id, result['min_time'], result['max_time'], result['avg_time'], 1, ts)
    with dbcon.cursor() as cursor:
        cursor.execute(q, q_args)
    dbcon.commit()


def monitor_url(url, dbcon, num_requests, keepalive):
    """Monitor an URL and store the results in the DB.

    A HTTP keepalive connection is established with the web server
    and five (by default) requests are sent for the given URL.
    min max and average values are stored.
    """
    print('Checking %s' % url)
    try:
        result = time_url(url.url, num_requests, keepalive)
    except Exception as e:
        print('ERROR: ', e)
    else:
        db_log_5m_result(url, dbcon, result)
        url.results.append(result['avg_time'])
        print(result)


def db_log_1h_result(url, dbcon, value, ts):
    q = """INSERT INTO 1h_avg_results (url_id, avg, timestamp) values (%s, %s, %s)"""
    q_args = (url.url_id, value, ts)
    with dbcon.cursor() as cursor:
        cursor.execute(q, q_args)
    dbcon.commit()


def log_1h_results(urls, dbcon):
    """Calculate stored averages for all urls and saving them to the DB.

    This function should (and is) called once per hour (every 12 requests).
    It calculates an average of the saved requests from the last hour
    and stores the result in the 1h_avg_results table.
    This is the data that is displayed by httpresplog-web.
    """
    print('Saving 1h results')
    ts = datetime.datetime.now()
    for url in urls:
        if not url.results:
            continue
        total = 0
        for value in url.results:
            total += value
        avg = int(total / len(url.results))
        url.results = []
        db_log_1h_result(url, dbcon, avg, ts)
        print('%s %d' % (url, avg))


def parse_args():
    """Command line argument handler."""
    parser = argparse.ArgumentParser(description="HTTP response time checker..")
    parser.add_argument(
        "-n",
        "--requests",
        default=5,
        type=int,
        help="number of requests to run per check",
    )
    parser.add_argument(
        "--no-keepalive",
        default=True,
        dest="keepalive",
        action="store_false",
        help="disable http keepalive",
    )
    parser.add_argument("--db-name", type=str, help="DB name", required=True)
    parser.add_argument("--db-user", type=str, help="DB user", required=True)
    parser.add_argument("--db-pass", type=str, help="DB password (yes this is unsafe)", required=True)
    parser.add_argument(
        "--ua-spoof",
        default=False,
        action="store_true",
        help="spoof a Chrome user agent",
    )
    parser.add_argument("--urls", help="URLs to check", action="append", required=True)

    args = parser.parse_args()
    return args


def main():
    args = parse_args()
    if args.ua_spoof:
        request_headers()["User-Agent"] = CHROME_USER_AGENT

    dbcon = db_connect(args.db_name, args.db_user, args.db_pass)

    urls = []
    for url in args.urls:
        urls.append(URL(url, dbcon))

    # Main loop.
    # Check all urls every 5 minutes, store average results once per hour
    # (every 12 iterations).
    loop_count = 0
    while True:
        print('----------- Monitoring')
        for url in urls:
            monitor_url(url, dbcon, args.requests, args.keepalive)
        print('----------- Done')
        loop_count += 1
        if loop_count >= 12:
            log_1h_results(urls, dbcon)
            loop_count = 0
        time.sleep(300)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
