# orcaAlert

Publishes recent Iberian orca incident reports (Strait of Gibraltar / Iberian
coast orca-boat interactions) somewhere you can fetch them at sea over
Winlink/Saildocs, with no internet needed on the boat.

## What it does

A GitHub Actions workflow runs `orcas_winlink.py` four times a day, at
**00:05, 06:05, 12:05 and 18:05 UTC**. It scrapes
[orcas.pt/lastincidents2](https://www.orcas.pt/lastincidents2) — the
server-rendered twin of orcas.pt's `/lastincidents` page — and publishes the
last 7 days of incidents as a tiny plain-text report, an HTML page, and a GPX
waypoint file. The results are committed to `docs/` and served by GitHub
Pages.

## Published URLs

- https://boatdatasystems.github.io/orcaAlert/or.html
- https://boatdatasystems.github.io/orcaAlert/or.txt
- https://boatdatasystems.github.io/orcaAlert/or.gpx

## Fetching over Winlink

Send an email to `query@saildocs.com` with this as the body:

```
send https://boatdatasystems.github.io/orcaAlert/or.html
```

Saildocs strips it down for Winlink delivery. `or.txt` also works and is
slightly smaller; `or.html` is the one Saildocs handles most reliably.

## Building the GPX aboard, offline

Once you've received the text/HTML report over Winlink, turn it into a GPX
file for OpenCPN without needing to fetch anything:

```
python orcas_winlink.py --report or.html --gpx orcas.gpx
```

(`--report or.txt` also works.)

## Script options

`orcas_winlink.py` is stdlib-only Python 3.9+. Main options:

| Option | Meaning |
|---|---|
| `--days N` | only include incidents from the last N days (default: 7 when `--gpx` is used, otherwise all) |
| `--pos LAT LON` | your position in decimal degrees (W/S negative) |
| `--radius N` | only incidents within N nm of `--pos` |
| `--sort dist` | sort by distance from `--pos` instead of date |
| `--no-loc` | omit location names, for the smallest possible report |

Run `python orcas_winlink.py --help` for the full list, including `--url`,
`--html`, `--report`, `-o`, `--html-out`, `--gpx`, and `--loc-width`.

## Data source

Incident data comes from [orcas.pt](https://www.orcas.pt), a non-profit
tracking orca-boat interactions on the Iberian coast. The data is theirs —
always verify before relying on it, and report your own sightings/incidents
to them.
