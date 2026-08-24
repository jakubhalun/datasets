# Daylight — offline time-zone boundary database

`timezones.db` resolves a latitude/longitude pair to an IANA time-zone id, entirely offline. It is
the copy embedded in the **Daylight: Sun & Polar Tracker** Android app, published here to satisfy
the ODbL share-alike condition (see [Licence](#licence)).

It is a **Derivative Database** of the
[timezone-boundary-builder](https://github.com/evansiroky/timezone-boundary-builder)
`timezones-with-oceans` release, which is itself derived from OpenStreetMap.

| | |
|---|---|
| Source project | [timezone-boundary-builder](https://github.com/evansiroky/timezone-boundary-builder) |
| Source release | **2026c** (`timezones-with-oceans.geojson.zip`) |
| Derived on | 2026-08-23 |
| Format version | 1 |
| Size | 2 179 072 bytes (2.1 MB) |
| Zones | 444 |
| Grid | 10 800 rows × 64 800 columns (60 rows/° latitude, 180 columns/° longitude) |
| Accuracy | ~1.9 km north–south, ~0.62 km east–west at the equator |
| Licence | ODbL 1.0 |

## What the derivation does

The source is ~182 MB of GeoJSON polygons. This database is a **raster**: the globe is divided into
a grid, every cell is labelled with the zone covering its centre, and each latitude row is stored
run-length encoded. That is the whole alteration — no zone was added, removed or renamed.

The grid is deliberately asymmetric. The number of runs in a row is the number of zones that
parallel crosses, and does **not** depend on the longitude resolution — so longitude precision costs
almost nothing (it is capped only by the 16-bit column index), while the row count is what costs
bytes. Most time-zone boundaries run roughly north–south, so the cheap axis is the one carrying the
accuracy.

The result is 2.1 MB and 492 585 runs, an average of 45.6 per row.

**Fidelity.** Sampling 20 000 random points and resolving each against both this raster and the
source polygons, 8 disagree (0.04%) — every one of them a point whose cell the boundary passes
through, where the raster faithfully returns the zone at the cell's own centre. There are no
unexplained differences. Re-run it yourself with `--verify` (below).

## Why the oceans variant

`timezones-with-oceans` tiles the **whole globe**, land and sea, so every coordinate resolves. Its
ocean areas are 15°-wide lunes carrying the `Etc/GMT±N` zones, which are real IANA ids. The
land-only release would leave most of the planet unanswered.

## Schema

```sql
CREATE TABLE tz_meta  (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE tz_zones (id INTEGER PRIMARY KEY, tzid TEXT NOT NULL);
CREATE TABLE tz_rows  (row INTEGER PRIMARY KEY, runs BLOB NOT NULL);
```

- `tz_meta` — provenance and grid resolution: `format_version`, `rows_per_degree`,
  `cols_per_degree`, `source`, `source_release`, `source_url`, `licence`, `generated`.
- `tz_zones` — zone index → IANA id.
- `tz_rows` — one run-length-encoded row per latitude band. The blob is a sequence of four-byte
  **big-endian** `(uint16 startColumn, uint16 zoneIndex)` records, ordered by `startColumn`, the
  first always starting at column 0.

Index arithmetic, with `RPD` = `rows_per_degree` and `CPD` = `cols_per_degree`:

```
row    = clamp(floor((latitude  +  90) * RPD), 0, 180 * RPD - 1)
column = clamp(floor((longitude + 180) * CPD), 0, 360 * CPD - 1)
```

A lookup is one primary-key read of `tz_rows` plus a binary search for the last record whose
`startColumn` is ≤ `column`.

**Zone ids only.** No UTC offsets and no daylight-saving rules are stored. Feed the resolved id to
your platform's own tz database (`ZoneId.of(...)`, `zoneinfo.ZoneInfo(...)`, …) so offsets and DST
come from data that is kept current independently of this file.

## Reading it

```python
import sqlite3, struct

con   = sqlite3.connect("file:timezones.db?mode=ro", uri=True)
meta  = dict(con.execute("SELECT key, value FROM tz_meta"))
RPD   = int(meta["rows_per_degree"])
CPD   = int(meta["cols_per_degree"])
ROWS, COLS = 180 * RPD, 360 * CPD
zones = dict(con.execute("SELECT id, tzid FROM tz_zones"))

def zone_at(latitude, longitude):
    row    = min(max(int((latitude  +  90) * RPD), 0), ROWS - 1)
    column = min(max(int((longitude + 180) * CPD), 0), COLS - 1)
    blob   = con.execute("SELECT runs FROM tz_rows WHERE row = ?", (row,)).fetchone()[0]
    low, high, found = 0, len(blob) // 4 - 1, 0
    while low <= high:
        middle = (low + high) // 2
        if struct.unpack_from(">H", blob, middle * 4)[0] <= column:
            found, low = middle, middle + 1
        else:
            high = middle - 1
    return zones[struct.unpack_from(">H", blob, found * 4 + 2)[0]]

print(zone_at(50.0614, 19.9366))    # Europe/Warsaw
print(zone_at(-48.876, -123.393))   # Etc/GMT+8   (Point Nemo)
print(zone_at(-80.0, 40.0))         # Antarctica/Syowa
```

### A note on freshly split zones

Boundary releases outpace platform tz databases. Release 2026c names `America/Coyhaique` (Chile's
Aysén region, added in tzdata 2025a), which older runtimes reject. If `ZoneId.of` / `ZoneInfo`
raises, prefer probing the grid outwards for the nearest zone your platform *does* accept — for
Aysén that yields `America/Santiago`, which is what an older tz database says about that place
anyway — rather than falling back to the local machine's own zone.

## Regenerating

`generate_timezone_db.py` is the exact script that produced this file. Python 3.7+, standard library
only.

```sh
# 1. Fetch the pinned source release and unzip it into test_dir/
curl -LO https://github.com/evansiroky/timezone-boundary-builder/releases/download/2026c/timezones-with-oceans.geojson.zip
mkdir -p test_dir && unzip timezones-with-oceans.geojson.zip -d test_dir/

# 2. Build (a few minutes)
python3 generate_timezone_db.py

# 3. Cross-check the raster against the source polygons
python3 generate_timezone_db.py --verify 20000
```

`--verify` exits non-zero if any difference is something other than grid quantisation.

To track a newer upstream release, bump `SOURCE_RELEASE` at the top of the script. `COLS_PER_DEGREE`
must stay at or below 182 — a column index is a `uint16`. Bump `FORMAT_VERSION` if you change the
blob encoding.

## Verifying this copy

```sh
sha256sum -c SHA256SUMS.txt
```

The same bytes ship inside the app package at `assets/timezones.db`, so a copy extracted from the
APK will match this checksum.

## Licence

**Data — `timezones.db`:** [Open Database License (ODbL) 1.0](LICENSE-ODbL-1.0.txt).

> Contains information from
> [timezone-boundary-builder](https://github.com/evansiroky/timezone-boundary-builder), which is
> made available under the [Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/1-0/).
> That data is derived from [OpenStreetMap](https://www.openstreetmap.org/copyright),
> © OpenStreetMap contributors.

You are free to share, adapt and use this database, including commercially, provided you attribute
it, keep the licence notices, and license any derivative *database* you publicly use under ODbL or a
compatible licence. Note that ODbL §4.5(b) is explicit that using the database to create a **Produced
Work** — a map, an application, a set of computed times — does not make that work a derivative
database, so share-alike does not reach it.

**Script — `generate_timezone_db.py`:** [MIT](LICENSE-MIT.txt).

The upstream timezone-boundary-builder *code* is MIT-licensed (© Evan Siroky); only its *data output*
is ODbL. This script is independent work and does not include any upstream code.
