from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import quote
from urllib.request import urlopen
import json
import time
import threading
import subprocess
import os

LOKI = "http://localhost:3100"
PROMETHEUS = "http://localhost:9090/api/v1/query"
PORT = 9091
ALLOWED_ORIGIN = "https://btea.dev"
REPO_PATH = os.path.dirname(os.path.abspath(__file__))

RATE_LIMIT = 60
RATE_WINDOW = 60
_rate_lock = threading.Lock()
_hits = {}

_last_updated_cache = None
_last_updated_cache_time = 0
_LAST_UPDATED_TTL = 300


def query(q):
    with urlopen(f"{PROMETHEUS}?query={quote(q)}") as r:
        result = json.loads(r.read())["data"]["result"]
        if not result:
            return 0
        return float(result[0]["value"][1])

def get_visitors():
    now = int(time.time())
    q = quote('{job="nginx"} | pattern `<remote_addr> - - [<_>] "<_>" <_> <_> "<_>" "<_>"` | remote_addr!="::1"')
    with urlopen(f"{LOKI}/loki/api/v1/query_range?query={q}&start={now - 86400}&end={now}&limit=5000") as r:
        streams = json.loads(r.read())["data"]["result"]
    ips = set()
    for stream in streams:
        for entry in stream["values"]:
            line = entry[1]
            ip = line.split(" ")[0]
            if ip and ip != "::1":
                ips.add(ip)
    return len(ips)

def get_last_updated():
    global _last_updated_cache, _last_updated_cache_time
    now = time.time()
    if _last_updated_cache and now - _last_updated_cache_time < _LAST_UPDATED_TTL:
        return _last_updated_cache
    try:
        iso = subprocess.check_output(
            ["git", "log", "-1", "--format=%cI"],
            cwd=REPO_PATH, stderr=subprocess.DEVNULL
        ).decode().strip()
        t = time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
        _last_updated_cache = time.strftime("%B %-d, %Y", t)
        _last_updated_cache_time = now
        return _last_updated_cache
    except Exception:
        return _last_updated_cache

def get_stats():
    return {
        "mem_used": round(query("(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes) / 1024 / 1024 / 1024"), 2),
        "cpu": round(query('100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100)'), 1),
        "uptime": int(query("time() - node_boot_time_seconds")),
        "unique_visitors_by_ip": get_visitors(),
        "last_updated": get_last_updated()
    }

def rate_limited(client_ip):
    now = time.time()
    with _rate_lock:
        first_hit, count = _hits.get(client_ip, (now, 0))
        if now - first_hit >= RATE_WINDOW:
            _hits[client_ip] = [now, 1]
        else:
            _hits[client_ip] = [first_hit, count + 1]
            if count + 1 > RATE_LIMIT:
                return True
        if len(_hits) > 10000:
            cutoff = now - RATE_WINDOW
            _hits = {k: v for k, v in _hits.items() if v[0] > cutoff}
    return False

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/stats":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        origin = self.headers.get("Origin", "")
        if rate_limited(self.client_address[0]):
            self.send_response(429)
            self.send_header("Content-Length", "0")
            self.send_header("Retry-After", str(RATE_WINDOW))
            self.end_headers()
            return

        try:
            data = json.dumps(get_stats()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if origin == ALLOWED_ORIGIN:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("", PORT), Handler).serve_forever()