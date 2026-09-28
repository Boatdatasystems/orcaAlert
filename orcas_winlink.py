#!/usr/bin/env python3
"""
orcas_winlink.py - Fetch the latest Iberian orca incidents from orcas.pt and
write (a) a tiny plain-ASCII text report for retrieval over Winlink via a
Saildocs "send <url>" request, and/or (b) a GPX file of waypoints for OpenCPN.

Source: https://www.orcas.pt/lastincidents2
  (the server-rendered twin of /lastincidents - the /lastincidents page draws
   its table on a <canvas> via JavaScript, so it can't be scraped directly)

Input can also be a text report produced by this script (--report), so you can
receive the small text file over Winlink and build the GPX aboard, offline.

Standard library only. Python 3.9+ (uses zoneinfo for local->UTC times).

Usage examples:
  ./orcas_winlink.py                                  # text report to stdout
  ./orcas_winlink.py -o /var/www/html/orcas.txt       # write report (atomic)
  ./orcas_winlink.py --html-out /var/www/html/orcas.html  # report as HTML page
  ./orcas_winlink.py --gpx orcas.gpx                  # GPX, last 7 days
  ./orcas_winlink.py --gpx orcas.gpx --days 14        # GPX, last 14 days
  ./orcas_winlink.py --report orcas.txt --gpx orcas.gpx   # aboard, offline
  ./orcas_winlink.py --report orcas.html --gpx orcas.gpx  # saved HTML copy works too
  ./orcas_winlink.py --pos 42.24 -8.72 --radius 150 --sort dist
  ./orcas_winlink.py --html saved_page.html           # parse a saved copy
"""

import argparse
import html
import math
import os
import re
import sys
import tempfile
import unicodedata
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from xml.sax.saxutils import escape as xml_escape

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

SOURCE_URL = "https://www.orcas.pt/lastincidents2"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
FIELDS = ("date", "time", "type", "lat", "lon", "loc")

# Country tag at the end of the location -> time zone of the reported time.
# Portugal is one hour behind Spain/Gibraltar, so this matters for GPX times.
TZ_BY_COUNTRY = {
    "PT": "Europe/Lisbon",
    "SP": "Europe/Madrid",
    "ES": "Europe/Madrid",
    "GI": "Europe/Gibraltar",
    "MO": "Africa/Casablanca",
    "MA": "Africa/Casablanca",
    "FR": "Europe/Paris",
}
DEFAULT_TZ = "Europe/Madrid"

# OpenCPN built-in icon names. Unknown names simply show OpenCPN's default mark.
SYM_ATTACK = "xmred"
SYM_SIGHTING = "triangle"

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


# ----------------------------------------------------------------------------
# Parsing: orcas.pt HTML
# ----------------------------------------------------------------------------
class RepeaterParser(HTMLParser):
    """Collect the <p> texts inside each Wix repeater item.

    Each incident is a <div class="... wixui-repeater__item"> containing six
    rich-text <p> elements: date, time, type, latitude, longitude, location.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self._div_depth = 0
        self._item_depth = None
        self._in_p = False
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "div":
            self._div_depth += 1
            cls = dict(attrs).get("class", "") or ""
            if self._item_depth is None and "wixui-repeater__item" in cls.split():
                self._item_depth = self._div_depth
                self.items.append([])
        elif tag == "p" and self._item_depth is not None:
            self._in_p = True
            self._buf = []
        elif tag == "br" and self._in_p:
            self._buf.append(" ")

    def handle_endtag(self, tag):
        if tag == "p" and self._in_p:
            self._in_p = False
            text = " ".join("".join(self._buf).split())
            if text:
                self.items[-1].append(text)
        elif tag == "div":
            if self._item_depth is not None and self._div_depth == self._item_depth:
                self._item_depth = None
            self._div_depth -= 1

    def handle_data(self, data):
        if self._in_p:
            self._buf.append(data)


def parse_incidents_html(page_html):
    p = RepeaterParser()
    p.feed(page_html)
    p.close()
    out = []
    for texts in p.items:
        if len(texts) < 6:
            continue
        rec = dict(zip(FIELDS, texts[:6]))
        rec["type"] = normalise_type(rec["type"])
        rec["latd"] = dm_to_decimal(rec["lat"])
        rec["lond"] = dm_to_decimal(rec["lon"])
        rec["dt"] = parse_dt(rec["date"], rec["time"])
        out.append(rec)
    return out


# ----------------------------------------------------------------------------
# Parsing: this script's own text report (for use aboard, after Winlink)
# ----------------------------------------------------------------------------
_REPORT_GEN_RE = re.compile(r"^Gen (\d{4})-(\d{2})-(\d{2})")
_REPORT_ROW_RE = re.compile(
    r"^(?P<day>\d{2})(?P<mon>[A-Z][a-z]{2}) (?P<hh>\d{2})(?P<mm>\d{2}) "
    r"(?P<t>[AS?]) "
    r"(?P<latd>\d{2}) (?P<latm>\d{2}\.\d)(?P<lath>[NS]) "
    r"(?P<lond>\d{3}) (?P<lonm>\d{2}\.\d)(?P<lonh>[EW])"
    r"(?:\s+\d+nm/\d{3}T)?"
    r"(?:\s+(?P<loc>.*))?$"
)


def parse_incidents_report(text):
    # Accept the --html-out page too (tags stripped, entities decoded)
    if "<pre" in text.lower() or "<html" in text.lower():
        m = re.search(r"<pre[^>]*>(.*?)</pre>", text, re.S | re.I)
        text = html.unescape(re.sub(r"<[^>]+>", "", m.group(1) if m else text))
    gen = None
    out = []
    for line in text.splitlines():
        line = line.rstrip()
        m = _REPORT_GEN_RE.match(line)
        if m:
            gen = datetime(int(m[1]), int(m[2]), int(m[3]))
            continue
        m = _REPORT_ROW_RE.match(line)
        if not m:
            continue
        year = gen.year if gen else datetime.now().year
        mon = MONTHS.get(m["mon"])
        if not mon:
            continue
        dt = datetime(year, mon, int(m["day"]), int(m["hh"]), int(m["mm"]))
        if gen and dt > gen + timedelta(days=2):   # Dec entries in a Jan report
            dt = dt.replace(year=year - 1)
        latd = int(m["latd"]) + float(m["latm"]) / 60
        lond = int(m["lond"]) + float(m["lonm"]) / 60
        if m["lath"] == "S":
            latd = -latd
        if m["lonh"] == "W":
            lond = -lond
        out.append({
            "date": dt.strftime("%d/%m/%Y"), "time": dt.strftime("%H:%M"),
            "type": {"A": "Attack", "S": "Sighting"}.get(m["t"], "Unknown"),
            "lat": "", "lon": "", "loc": (m["loc"] or "").strip(),
            "latd": latd, "lond": lond, "dt": dt,
        })
    return out


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
_DM_RE = re.compile(
    r"""(?P<deg>\d{1,3})\s*[°º\s]\s*
        (?P<min>0*\d{1,2}(?:[.,]\d+)?)\s*['′’]?\s*
        (?P<hem>[NSEWnsew])""",
    re.VERBOSE,
)
# ^ "0*" tolerates zero-padded minutes seen in the source, e.g. 007°026.919'W


def normalise_type(s):
    s = s.strip().lower()
    if s.startswith("att") or s.startswith("int"):
        return "Attack"
    if s.startswith("sig") or s.startswith("obs") or s.startswith("avis"):
        return "Sighting"
    return s.capitalize() or "Unknown"


def dm_to_decimal(s):
    """'44°16.655' N' -> 44.27758 ; '008°25.49'W' -> -8.42483. None if unparseable."""
    m = _DM_RE.search(html.unescape(s))
    if not m:
        return None
    deg = int(m["deg"])
    mins = float(m["min"].replace(",", "."))
    hem = m["hem"].upper()
    if mins >= 60 or deg > (90 if hem in "NS" else 180):
        return None  # garbled value: better flagged than plotted wrongly
    val = deg + mins / 60.0
    return -val if hem in "SW" else val


def fmt_dm(dec, is_lat):
    """Decimal degrees -> compact 'DD MM.MN' / 'DDD MM.MW' (one decimal)."""
    if dec is None:
        return "?"
    hem = ("N" if dec >= 0 else "S") if is_lat else ("E" if dec >= 0 else "W")
    a = abs(dec)
    d = int(a)
    m = round((a - d) * 60, 1)
    if m >= 60.0:
        d, m = d + 1, 0.0
    return f"{d:02d} {m:04.1f}{hem}" if is_lat else f"{d:03d} {m:04.1f}{hem}"


def parse_dt(date_s, time_s):
    """orcas.pt uses DD/MM/YYYY and HH:MM (local time of the reporting country)."""
    for fmt, val in (("%d/%m/%Y %H:%M", f"{date_s} {time_s}"),
                     ("%d/%m/%Y %H.%M", f"{date_s} {time_s}"),
                     ("%d/%m/%Y", date_s)):
        try:
            return datetime.strptime(val.strip(), fmt)
        except ValueError:
            continue
    return None


def country_of(loc):
    m = re.search(r"\(([A-Z]{2})\)\s*~?$", loc or "")
    return m.group(1) if m else None


def to_utc(dt_local, loc):
    """Naive local datetime -> aware UTC, using the country tag in the location."""
    if dt_local is None:
        return None
    if ZoneInfo is None:
        return None
    tzname = TZ_BY_COUNTRY.get(country_of(loc), DEFAULT_TZ)
    try:
        return dt_local.replace(tzinfo=ZoneInfo(tzname)).astimezone(timezone.utc)
    except Exception:
        return None


def ascii_only(s):
    """Strip accents so output is pure 7-bit ASCII (Hércules -> Hercules)."""
    s = unicodedata.normalize("NFKD", html.unescape(s))
    return s.encode("ascii", "ignore").decode("ascii")


def range_bearing(lat1, lon1, lat2, lon2):
    """Great-circle distance (nm) and initial true bearing (deg) from 1 to 2."""
    R_NM = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    dist = 2 * R_NM * math.asin(min(1.0, math.sqrt(a)))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    brg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return dist, brg


def fetch(url, timeout=45):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(charset, errors="replace")


# ----------------------------------------------------------------------------
# Filtering
# ----------------------------------------------------------------------------
def select_rows(incidents, args):
    rows = list(incidents)

    if args.days:
        cutoff = datetime.now() - timedelta(days=args.days)
        rows = [r for r in rows if r["dt"] is not None and r["dt"] >= cutoff]

    if args.pos:
        mylat, mylon = args.pos
        for r in rows:
            if r["latd"] is not None and r["lond"] is not None:
                r["dist"], r["brg"] = range_bearing(mylat, mylon, r["latd"], r["lond"])
            else:
                r["dist"], r["brg"] = None, None
        if args.radius:
            rows = [r for r in rows if r["dist"] is not None and r["dist"] <= args.radius]
        if args.sort == "dist":
            rows = sorted(rows, key=lambda r: (r["dist"] is None, r["dist"] or 0))
    return rows


# ----------------------------------------------------------------------------
# Output: text report
# ----------------------------------------------------------------------------
def build_report(rows, total, args):
    now = datetime.now(timezone.utc)
    lines = ["ORCAS.PT LAST INCIDENTS",
             f"Gen {now:%Y-%m-%d %H%MZ}  n={len(rows)}/{total}"]
    filt = []
    if args.days:
        filt.append(f"last {args.days}d")
    if args.pos:
        filt.append(f"ref {fmt_dm(args.pos[0], True)} {fmt_dm(args.pos[1], False)}")
        if args.radius:
            filt.append(f"r<={args.radius:g}nm")
    if filt:
        lines.append("Filter " + " ".join(filt))
    lines.append("A=Attack S=Sighting  time=local")
    lines.append("")

    for r in rows:
        t = r["type"][:1].upper() or "?"
        d = r["dt"].strftime("%d%b %H%M") if r["dt"] else f"{r['date']} {r['time']}"
        if r["latd"] is None or r["lond"] is None:
            # show the raw source text so the position isn't lost
            raw = ascii_only(f"{r.get('lat', '')} {r.get('lon', '')}").replace("'", "")
            line = f"{d} {t} ?? {raw.strip() or 'no position'}"
        else:
            line = f"{d} {t} {fmt_dm(r['latd'], True)} {fmt_dm(r['lond'], False)}"
        if args.pos and r.get("dist") is not None:
            line += f" {r['dist']:4.0f}nm/{r['brg']:03.0f}T"
        if not args.no_loc:
            loc = ascii_only(r["loc"])
            if args.loc_width and len(loc) > args.loc_width:
                # keep the "(PT)"/"(SP)" tag: it sets the time zone for GPX times
                tag = re.search(r"\s*\([A-Z]{2}\)$", loc)
                tail = tag.group(0) if tag else ""
                body = loc[: len(loc) - len(tail)]
                keep = max(1, args.loc_width - len(tail) - 1)
                loc = body[:keep].rstrip() + "~" + tail
            line += f" {loc}"
        lines.append(line)

    if not rows:
        lines.append("(no incidents match filter)")
    lines.append("")
    lines.append("src orcas.pt - verify, report yours")
    return ascii_only("\n".join(lines)) + "\n"


# ----------------------------------------------------------------------------
# Output: GPX for OpenCPN
# ----------------------------------------------------------------------------
def build_gpx(rows, args):
    now = datetime.now(timezone.utc)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<gpx version="1.1" creator="orcas_winlink.py" '
           'xmlns="http://www.topografix.com/GPX/1/1" '
           'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
           'xsi:schemaLocation="http://www.topografix.com/GPX/1/1 '
           'http://www.topografix.com/GPX/1/1/gpx.xsd">',
           '  <metadata>',
           f'    <name>Orca incidents (orcas.pt) {now:%Y-%m-%d}</name>',
           f'    <desc>{len(rows)} incidents'
           f'{", last " + str(args.days) + " days" if args.days else ""}. '
           'Source: orcas.pt</desc>',
           f'    <time>{now:%Y-%m-%dT%H:%M:%SZ}</time>',
           '  </metadata>']

    for r in rows:
        if r["latd"] is None or r["lond"] is None:
            continue
        is_attack = r["type"] == "Attack"
        code = "A" if is_attack else ("S" if r["type"] == "Sighting" else "?")
        stamp = r["dt"].strftime("%d%b %H%M") if r["dt"] else r["date"]
        name = f"ORCA {code} {stamp}"
        loc = ascii_only(r["loc"])
        utc = to_utc(r["dt"], r["loc"])

        desc_bits = [f"{r['type']} {r['date']} {r['time']} local"]
        if utc:
            desc_bits.append(f"({utc:%H%MZ})")
        if loc:
            desc_bits.append(f"- {loc}")
        if r.get("dist") is not None:
            desc_bits.append(f"- {r['dist']:.0f}nm/{r['brg']:03.0f}T from ref")
        desc = " ".join(desc_bits)

        out.append(f'  <wpt lat="{r["latd"]:.5f}" lon="{r["lond"]:.5f}">')
        if utc:
            out.append(f'    <time>{utc:%Y-%m-%dT%H:%M:%SZ}</time>')
        out.append(f'    <name>{xml_escape(name)}</name>')
        out.append(f'    <desc>{xml_escape(desc)}</desc>')
        out.append(f'    <sym>{SYM_ATTACK if is_attack else SYM_SIGHTING}</sym>')
        out.append(f'    <type>{xml_escape(r["type"])}</type>')
        out.append('  </wpt>')

    out.append('</gpx>')
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------
# Output: minimal HTML page (report inside <pre>) for Saildocs
# ----------------------------------------------------------------------------
def build_html(report_text):
    """Wrap the text report in the smallest sensible HTML page.

    Saildocs strips tags from web pages and returns the text, so this comes
    back over Winlink as the same plain report. No CSS, no scripts.
    """
    body = html.escape(report_text.rstrip("\n"), quote=False)
    return ("<!DOCTYPE html>\n"
            "<html><head><meta charset=\"utf-8\">"
            "<title>Orcas.pt incidents</title></head>\n"
            f"<body><pre>\n{body}\n</pre></body></html>\n")


# ----------------------------------------------------------------------------
def write_atomic(path, text):
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".orcas_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--url", default=SOURCE_URL, help="source page (default: %(default)s)")
    src.add_argument("--html", help="parse a saved orcas.pt HTML file instead of fetching")
    src.add_argument("--report", help="parse a text report made by this script "
                                      "(e.g. received via Winlink)")
    ap.add_argument("-o", "--output", help="write text report to this file (atomic)")
    ap.add_argument("--html-out", metavar="FILE",
                    help="write the text report wrapped in a minimal HTML <pre> page")
    ap.add_argument("--gpx", metavar="FILE", help="write a GPX waypoint file for OpenCPN")
    ap.add_argument("--days", type=int, default=None,
                    help="only incidents from the last N days "
                         "(default: 7 when --gpx is used, otherwise all)")
    ap.add_argument("--pos", nargs=2, type=float, metavar=("LAT", "LON"),
                    help="your position, decimal degrees (W/S negative)")
    ap.add_argument("--radius", type=float, help="only incidents within N nm of --pos")
    ap.add_argument("--sort", choices=("date", "dist"), default="date",
                    help="sort order (dist needs --pos). default: date")
    ap.add_argument("--no-loc", action="store_true", help="omit location names (smallest)")
    ap.add_argument("--loc-width", type=int, default=34,
                    help="truncate location names to N chars (0 = no limit). default 34")
    args = ap.parse_args()

    if args.radius and not args.pos:
        ap.error("--radius needs --pos")
    if args.sort == "dist" and not args.pos:
        ap.error("--sort dist needs --pos")
    if args.days is None and args.gpx:
        args.days = 7

    try:
        if args.report:
            with open(args.report, encoding="utf-8", errors="replace") as f:
                incidents = parse_incidents_report(f.read())
        else:
            if args.html:
                with open(args.html, encoding="utf-8", errors="replace") as f:
                    page = f.read()
            else:
                page = fetch(args.url)
            incidents = parse_incidents_html(page)
    except Exception as e:
        print(f"ERROR reading source: {e}", file=sys.stderr)
        return 2  # leave any existing output files untouched

    if not incidents:
        print("ERROR: no incidents parsed - page layout may have changed", file=sys.stderr)
        return 3  # don't overwrite a good file with an empty one

    for r in incidents:
        if r["latd"] is None or r["lond"] is None:
            print(f"WARNING: unparseable position {r['date']} {r['time']}: "
                  f"{r['lat']!r} {r['lon']!r}", file=sys.stderr)

    rows = select_rows(incidents, args)

    if args.gpx:
        write_atomic(args.gpx, build_gpx(rows, args))
        print(f"GPX: {len(rows)} waypoints -> {args.gpx}", file=sys.stderr)

    report = build_report(rows, len(incidents), args)
    if args.output:
        write_atomic(args.output, report)
    if args.html_out:
        write_atomic(args.html_out, build_html(report))
    if not (args.output or args.html_out or args.gpx):
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
