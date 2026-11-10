"""Copy road closures from JKR eBencana into data/jkr_roads.json.

Runs on GitHub Actions. Uses only the Python standard library.
It never fails the job: problems are written to data/jkr_status.json
so the Flood Watch page can show them.
"""
import datetime as dt
import html
import json
import os
import re
import ssl
import urllib.request
from html.parser import HTMLParser

URLS = [
    "https://bencana.jkr.gov.my/",
    "https://bencanaalam.jkr.gov.my/",
    "http://bencanaalam.jkr.gov.my/",
]
OUT = "data"
UA = "Mozilla/5.0 (compatible; FloodWatch/1.0; +https://fiq-code.github.io/flood-watch/)"

STATES = {
    "MY-01": ["JOHOR"], "MY-02": ["KEDAH"], "MY-03": ["KELANTAN"],
    "MY-04": ["MELAKA", "MALACCA"], "MY-05": ["NEGERI SEMBILAN", "N. SEMBILAN", "N.SEMBILAN"],
    "MY-06": ["PAHANG"], "MY-07": ["PULAU PINANG", "PENANG"], "MY-08": ["PERAK"],
    "MY-09": ["PERLIS"], "MY-10": ["SELANGOR"], "MY-11": ["TERENGGANU"],
    "MY-12": ["SABAH"], "MY-13": ["SARAWAK"], "MY-14": ["KUALA LUMPUR"],
    "MY-15": ["LABUAN"], "MY-16": ["PUTRAJAYA"],
}

# Order matters: the first match wins.
STATUS = [
    (r"ditutup\s+kepada\s+semua|tutup\s+sepenuhnya|tidak\s+boleh\s+dilalui", "closed_all"),
    (r"(dibuka|dibenarkan)\s+kepada\s+kenderaan\s+berat|ditutup\s+kepada\s+kenderaan\s+ringan|kenderaan\s+berat\s+sahaja", "closed_light"),
    (r"dibuka\s+(semula|sepenuhnya|kepada\s+semua)|telah\s+dibuka", "reopened"),
    (r"berhati[\s-]*hati|boleh\s+dilalui|separa|satu\s+lorong", "caution"),
    (r"\bditutup\b", "closed_all"),
]
BLOCK = {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "td", "th", "section", "article"}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Extract(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None
        self.lines, self.buf = [], []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        if tag == "tr":
            self.row = []
        if tag in ("td", "th") and self.row is not None:
            self.cell = []
        if tag in BLOCK:
            self.flush()

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)
        if tag in ("td", "th") and self.row is not None and self.cell is not None:
            self.row.append(" ".join(" ".join(self.cell).split()))
            self.cell = None
        if tag == "tr" and self.row is not None:
            if any(self.row):
                self.rows.append(self.row)
            self.row = None
        if tag in BLOCK:
            self.flush()

    def handle_data(self, data):
        if self.skip:
            return
        if self.cell is not None:
            self.cell.append(data)
        self.buf.append(data)

    def flush(self):
        t = " ".join(" ".join(self.buf).split())
        if t:
            self.lines.append(t)
        self.buf = []


def status_of(text):
    for pat, code in STATUS:
        if re.search(pat, text, re.I):
            return code
    return None


def state_of(text):
    up = text.upper()
    for code, names in STATES.items():
        if any(n in up for n in names):
            return code
    return None


CODE = re.compile(r"\b(FT\s?\d{1,4}|[A-Z]{1,2}\d{1,4})\b")
NAME = re.compile(r"\bJalan\s+[^,;|]{2,80}", re.I)


def route_of(texts):
    texts = [t for t in texts if t and not status_of(t)] or texts
    code = next((m.group(1).replace(" ", "") for t in texts for m in [CODE.search(t)] if m), "")
    name = next((m.group(0).strip() for t in texts for m in [NAME.search(t)] if m), "")
    label = f"{code} {name}" if code and name and code not in name else (name or code)
    return label[:100] if label else (texts[0][:80] if texts else "Road")


def fetch():
    errors = []
    for url in URLS:
        for verify in (True, False):
            try:
                ctx = ssl.create_default_context()
                if not verify:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ms,en"})
                with urllib.request.urlopen(req, timeout=40, context=ctx) as r:
                    raw = r.read(3_000_000)
                    charset = r.headers.get_content_charset() or "utf-8"
                    return url, r.status, raw.decode(charset, "replace"), errors
            except Exception as e:  # noqa: BLE001
                errors.append(f"{url} ({'verified' if verify else 'unverified'} TLS): {e}")
    return None, None, None, errors


def parse(page):
    p = Extract()
    p.feed(page)
    p.flush()
    items = []
    for row in p.rows:
        text = " | ".join(c for c in row if c)
        st = status_of(text)
        if st:
            items.append({"name": route_of(row), "state": state_of(text), "status": st, "note": text[:300]})
    if not items:
        lines = p.lines
        for i, line in enumerate(lines):
            st = status_of(line)
            if not st:
                continue
            ctx = []
            for back in range(i - 1, max(-1, i - 6), -1):
                if status_of(lines[back]):
                    break
                ctx.insert(0, lines[back])
            text = " · ".join(ctx + [line])
            items.append({"name": route_of(ctx), "state": state_of(text), "status": st, "note": text[:300]})
    seen, out = set(), []
    for it in items:
        key = (it["name"].lower(), it["status"], (it["state"] or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    hints = sorted(set(re.findall(r"""["'](\/?api\/[^"']{2,120}|https?:\/\/[^"']*(?:api|json|ajax)[^"']{0,120})["']""", page)))[:20]
    return out, hints, len(p.rows), len(p.lines)


def main():
    os.makedirs(OUT, exist_ok=True)
    fetched = now()
    url, code, page, errors = fetch()
    status = {"fetched_at": fetched, "source": url, "http_status": code, "ok": False,
              "count": 0, "errors": errors[-6:], "hints": [], "rows_seen": 0, "lines_seen": 0}
    items = []
    if page:
        with open(os.path.join(OUT, "jkr_raw.html"), "w", encoding="utf-8") as f:
            f.write(page[:1_500_000])
        try:
            items, hints, nrows, nlines = parse(page)
            status.update(ok=True, count=len(items), hints=hints, rows_seen=nrows, lines_seen=nlines)
        except Exception as e:  # noqa: BLE001
            status["errors"].append(f"parse: {e}")
    for i, it in enumerate(items):
        it.update(id=f"jkr{i}", source="JKR eBencana (auto)", at=fetched, district="")
        it["name"] = html.unescape(it["name"])
    with open(os.path.join(OUT, "jkr_roads.json"), "w", encoding="utf-8") as f:
        json.dump({"fetched_at": fetched, "source": url, "ok": status["ok"], "items": items}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT, "jkr_status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)
    print(json.dumps(status, indent=1))


if __name__ == "__main__":
    main()
