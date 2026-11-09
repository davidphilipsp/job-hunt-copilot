"""Sonar: a local page over every job the finder kept (data/sonar.json), with status tracking.

./bin/sonar            → serve http://127.0.0.1:8770 and open it
./bin/sonar --refresh  → run the finder first
Status (saved / applied / interview / offer / hidden) is written to data/applications.json.
Local only: binds to 127.0.0.1, never applies anywhere.
"""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from finder import APPS_FILE, CUR_SYM, PAY_FLOOR_BRL_MONTH, PAY_TARGET_BRL_MONTH, SONAR_FILE, load_apps  # noqa: E402

PAGE = Path(__file__).resolve().parent / "sonar.html"
UPWORK_FILE = SONAR_FILE.parent / "upwork.json"  # optional: jobs you add by hand from Upwork, scored with a 20-point filter
STATUSES = {"", "saved", "applied", "interview", "offer", "hidden"}


def payload() -> dict:
    try:
        data = json.loads(SONAR_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {"updated": None, "stats": {}, "jobs": []}
    try:  # Upwork has no job feed; these are scored in a Claude session from screenshots/links
        for u in json.loads(UPWORK_FILE.read_text()):
            if not u.get("url"):  # from a screenshot: no job link, so open Upwork's search for the exact title
                u["url"] = "https://www.upwork.com/nx/search/jobs/?sort=recency&q=" + quote(f'"{u.get("upwork_title") or u["title"]}"')
                u["url_is_search"] = True
            data["jobs"].append({**u, "source": "Upwork", "upwork": True, "kind": "Contract",
                                 "score": 0 if u["verdict"].startswith("VETO") else round(u["total"] * 5), "company": u.get("client", ""),
                                 "first_seen": u.get("added")})
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    apps = load_apps()
    keys = set()
    for j in data["jobs"]:
        keys.add(j["key"])
        j.update(status=apps.get(j["key"], {}).get("status", ""), status_date=apps.get(j["key"], {}).get("date"))
    # Tracked jobs that left today's feeds still show under their status.
    for k, a in apps.items():
        if k not in keys and a.get("status") not in ("", "hidden"):
            data["jobs"].append({"key": k, "title": a.get("title", ""), "company": a.get("company", ""),
                                 "url": a.get("url", ""), "status": a["status"], "status_date": a.get("date"),
                                 "gone": True, "score": None})
    data["floor"], data["target"], data["cur"] = PAY_FLOOR_BRL_MONTH, PAY_TARGET_BRL_MONTH, CUR_SYM
    return data


def set_status(body: dict) -> dict:
    status = body.get("status", "")
    if status not in STATUSES or not body.get("key"):
        raise ValueError(f"bad status: {status!r}")
    apps = load_apps()
    if status:
        apps[body["key"]] = {"status": status, "date": date.today().isoformat(),
                             **{k: body.get(k, "") for k in ("title", "company", "url")}}
    else:
        apps.pop(body["key"], None)
    APPS_FILE.write_text(json.dumps(apps, indent=1, ensure_ascii=False))
    return apps.get(body["key"], {})


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/jobs":
            self._send(200, json.dumps(payload(), ensure_ascii=False).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/status":
            return self._send(404, b"not found", "text/plain")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            self._send(200, json.dumps(set_status(body)).encode(), "application/json")
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, str(e).encode(), "text/plain")

    def log_message(self, *a):
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sonar", description="Local page over every job the finder kept.")
    ap.add_argument("--refresh", action="store_true", help="run the finder before serving")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    args = ap.parse_args(argv)
    if args.refresh:
        import finder
        finder.main([])
    url = f"http://127.0.0.1:{args.port}"
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Sonar on {url} (Ctrl+C to stop)")
    if not args.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
