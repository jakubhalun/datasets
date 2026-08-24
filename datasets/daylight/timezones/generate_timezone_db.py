#!/usr/bin/env python3
"""
Generates app/src/main/assets/timezones.db -- the offline timezone-boundary database used by
`BoundaryTimeZoneResolver` to turn a hand-entered latitude/longitude into an IANA zone id
(ADR 029).

Input is the `timezones-with-oceans` GeoJSON release of timezone-boundary-builder, which tiles the
whole globe (land *and* ocean) with disjoint polygons, so every coordinate on Earth resolves.

The output is not the polygons. It is a raster: the globe is cut into ROWS_PER_DEGREE rows per
degree of latitude and COLS_PER_DEGREE columns per degree of longitude, every cell is painted with
the zone covering its centre, and each row is stored run-length encoded. Run count depends on how
many zones a parallel crosses, not on the longitude resolution -- so longitude precision is nearly
free (capped only by the uint16 column index) and only the row count costs bytes.

Run from the repository root:
    python3 scripts/generate_timezone_db.py
    python3 scripts/generate_timezone_db.py --verify 20000
"""

import array
import json
import os
import random
import re
import sqlite3
import struct
import sys
import time
from datetime import date
from math import ceil, floor

# --- Source -----------------------------------------------------------------------------------
SOURCE_RELEASE = "2026c"
SOURCE_PROJECT = "timezone-boundary-builder"
SOURCE_URL = (
    "https://github.com/evansiroky/timezone-boundary-builder/releases/download/"
    f"{SOURCE_RELEASE}/timezones-with-oceans.geojson.zip"
)
INPUT_FILE = "test_dir/combined-with-oceans.json"
OUTPUT_DB = "app/src/main/assets/timezones.db"

# --- Grid -------------------------------------------------------------------------------------
# Must match TimeZoneGrid.kt. FORMAT_VERSION is bumped whenever the blob encoding changes; the app
# refuses a database whose metadata disagrees with its compiled-in constants.
FORMAT_VERSION = 1
ROWS_PER_DEGREE = 60    # 10 800 rows, ~1.9 km north-south
COLS_PER_DEGREE = 180   # 64 800 columns (must stay <= 65 535 so a column index fits a uint16)

ROWS = 180 * ROWS_PER_DEGREE
COLS = 360 * COLS_PER_DEGREE
assert COLS <= 0xFFFF + 1, "column index must fit in a uint16"

UNPAINTED = -1


def log(message):
    print(message, flush=True)


# ------------------------------------------------------------------------------------------------
# Reading the source
# ------------------------------------------------------------------------------------------------

def iter_features(path):
    """
    Yields (tzid, geometry) one feature at a time.

    The file is ~180 MB, so it is never handed to json.load() whole. The FeatureCollection is
    scanned to its "features" array and each element is pulled off with raw_decode over a sliding
    buffer, which keeps only one feature in memory at a time.
    """
    decoder = json.JSONDecoder()
    chunk_size = 8 << 20
    with open(path, encoding="utf-8") as handle:
        buffer = ""
        # Advance to the first element of the features array.
        while '"features"' not in buffer:
            more = handle.read(chunk_size)
            if not more:
                raise ValueError(f"{path} has no 'features' member")
            buffer += more
        start = buffer.index('"features"')
        start = buffer.index("[", start) + 1
        buffer = buffer[start:]

        while True:
            buffer = buffer.lstrip(" \t\r\n,")
            if buffer.startswith("]") or buffer == "":
                return
            while True:
                try:
                    feature, end = decoder.raw_decode(buffer)
                    break
                except ValueError:
                    more = handle.read(chunk_size)
                    if not more:
                        raise
                    buffer += more
            buffer = buffer[end:]
            geometry = feature.get("geometry") or {}
            yield feature["properties"]["tzid"], geometry


def polygons_of(geometry):
    """Yields each polygon of a Polygon/MultiPolygon as its list of rings."""
    kind = geometry.get("type")
    if kind == "Polygon":
        yield geometry["coordinates"]
    elif kind == "MultiPolygon":
        for polygon in geometry["coordinates"]:
            yield polygon
    elif kind is not None:
        raise ValueError(f"unsupported geometry type {kind!r}")


class Edges:
    """
    Every polygon edge of the whole dataset, in flat arrays, plus an index from grid row to the
    edges whose latitude span touches that row's band.

    Coordinates are stored as float32: at a magnitude of 180 that is ~1e-5 degrees, roughly a
    metre, which is three orders of magnitude finer than the grid it feeds.
    """

    def __init__(self):
        self.x0 = array.array("f")
        self.y0 = array.array("f")
        self.x1 = array.array("f")
        self.y1 = array.array("f")
        self.polygon = array.array("i")
        self.zone_of_polygon = array.array("i")
        self.zones = []                 # zone index -> tzid
        self.row_offsets = None         # ROWS + 1 entries
        self.row_edges = None           # flat, sliced by row_offsets
        self.antimeridian_edges = 0

    def add_polygon(self, rings, zone_index):
        polygon_index = len(self.zone_of_polygon)
        self.zone_of_polygon.append(zone_index)
        for ring in rings:
            previous = ring[0]
            for point in ring[1:]:
                ax, ay = previous
                bx, by = point
                previous = point
                if ay == by:
                    continue  # horizontal edges never cross a scanline under the half-open rule
                if abs(bx - ax) > 180.0:
                    # GeoJSON requires antimeridian-crossing polygons to be split; a surviving
                    # wrap-around edge would paint a bogus span right across the map.
                    self.antimeridian_edges += 1
                    continue
                self.x0.append(ax)
                self.y0.append(ay)
                self.x1.append(bx)
                self.y1.append(by)
                self.polygon.append(polygon_index)

    def build_row_index(self):
        """Counting sort of edge indices into per-row buckets (an edge lands in every row band its
        latitude span overlaps, which for all but a handful of edges is one)."""
        counts = array.array("i", bytes(4 * (ROWS + 1)))
        y0, y1 = self.y0, self.y1
        for i in range(len(y0)):
            a, b = y0[i], y1[i]
            low, high = (a, b) if a < b else (b, a)
            first = row_of(low)
            last = row_of(high)
            for row in range(first, last + 1):
                counts[row] += 1

        offsets = array.array("i", bytes(4 * (ROWS + 1)))
        running = 0
        for row in range(ROWS):
            offsets[row] = running
            running += counts[row]
        offsets[ROWS] = running

        cursor = array.array("i", offsets)
        edges = array.array("i", bytes(4 * running))
        for i in range(len(y0)):
            a, b = y0[i], y1[i]
            low, high = (a, b) if a < b else (b, a)
            for row in range(row_of(low), row_of(high) + 1):
                edges[cursor[row]] = i
                cursor[row] += 1

        self.row_offsets = offsets
        self.row_edges = edges

    def crossings_at(self, row, latitude):
        """{polygon index: [longitudes]} for every edge in `row` that crosses `latitude`."""
        crossings = {}
        x0, y0, x1, y1, polygon = self.x0, self.y0, self.x1, self.y1, self.polygon
        edges = self.row_edges
        for slot in range(self.row_offsets[row], self.row_offsets[row + 1]):
            i = edges[slot]
            ay = y0[i]
            by = y1[i]
            # Half-open rule: a vertex sitting exactly on the scanline is counted once, not twice.
            if (ay <= latitude) == (by <= latitude):
                continue
            ax = x0[i]
            x = ax + (latitude - ay) * (x1[i] - ax) / (by - ay)
            crossings.setdefault(polygon[i], []).append(x)
        return crossings


def row_of(latitude):
    row = int((latitude + 90.0) * ROWS_PER_DEGREE)
    return 0 if row < 0 else (ROWS - 1 if row >= ROWS else row)


def column_of(longitude):
    column = int((longitude + 180.0) * COLS_PER_DEGREE)
    return 0 if column < 0 else (COLS - 1 if column >= COLS else column)


def row_latitude(row):
    """Centre latitude of a row -- the latitude every cell in that row is painted from."""
    return -90.0 + (row + 0.5) / ROWS_PER_DEGREE


def column_longitude(column):
    return -180.0 + (column + 0.5) / COLS_PER_DEGREE


def load_edges(path):
    edges = Edges()
    zone_indices = {}
    started = time.time()
    features = 0
    for tzid, geometry in iter_features(path):
        zone_index = zone_indices.get(tzid)
        if zone_index is None:
            zone_index = len(edges.zones)
            zone_indices[tzid] = zone_index
            edges.zones.append(tzid)
        for rings in polygons_of(geometry):
            edges.add_polygon(rings, zone_index)
        features += 1
        if features % 50 == 0:
            log(f"  ... {features} features, {len(edges.x0):,} edges")
    log(
        f"  {features} features, {len(edges.zone_of_polygon):,} polygons, "
        f"{len(edges.x0):,} edges, {len(edges.zones)} zones "
        f"({time.time() - started:.0f}s)"
    )
    if edges.antimeridian_edges:
        log(f"  WARNING: skipped {edges.antimeridian_edges} antimeridian-spanning edges")
    return edges


# ------------------------------------------------------------------------------------------------
# Rasterising
# ------------------------------------------------------------------------------------------------

NONZERO_BYTE = re.compile(rb"[^\x00]")
UNPAINTED_WORD = re.compile(rb"\xff\xff")


EMPTY_ROW = array.array("h", [UNPAINTED]) * COLS


def paint_row(edges, row, buffer):
    """Fills `buffer` (one int16 zone index per column) from the polygons crossing this row."""
    buffer[:] = EMPTY_ROW

    latitude = row_latitude(row)
    zone_of_polygon = edges.zone_of_polygon
    for polygon_index, longitudes in edges.crossings_at(row, latitude).items():
        longitudes.sort()
        zone = zone_of_polygon[polygon_index]
        # Even-odd: consecutive pairs of crossings bracket the inside of the polygon, which is
        # what makes holes (rings wound the other way) fall out for free.
        for pair in range(0, len(longitudes) - 1, 2):
            # A cell belongs to a span when its centre does.
            first = ceil((longitudes[pair] + 180.0) * COLS_PER_DEGREE - 0.5)
            last = floor((longitudes[pair + 1] + 180.0) * COLS_PER_DEGREE - 0.5)
            if first < 0:
                first = 0
            if last > COLS - 1:
                last = COLS - 1
            if last < first:
                continue
            buffer[first:last + 1] = array.array("h", [zone]) * (last - first + 1)


def fill_gaps(buffer):
    """
    Gives any cell no polygon claimed the zone of its nearest painted neighbour in the same row.

    With the with-oceans dataset the globe is tiled completely, so this only ever catches
    single-cell slivers where two polygons meet exactly on a cell centre.
    """
    raw = buffer.tobytes()
    holes = [m.start() // 2 for m in UNPAINTED_WORD.finditer(raw) if m.start() % 2 == 0]
    if not holes:
        return 0
    if len(holes) == COLS:
        # A row no polygon touched at all. Fall back to the ocean lunes, which is how
        # timezone-boundary-builder defines Etc/GMT+-N in the first place.
        return -1
    # Slivers are isolated, so walking outwards from each one finds a painted neighbour within a
    # few steps; a full scan of the row per hole would dominate the whole script.
    for column in holes:
        distance = 1
        while True:
            left = column - distance
            if left >= 0 and buffer[left] != UNPAINTED:
                buffer[column] = buffer[left]
                break
            right = column + distance
            if right < COLS and buffer[right] != UNPAINTED:
                buffer[column] = buffer[right]
                break
            distance += 1
    return len(holes)


def encode_runs(buffer):
    """
    Run-length encodes a painted row into (uint16 startColumn, uint16 zoneIndex) big-endian records.

    Finding the run starts by iterating 64 800 columns in Python would dominate the whole script, so
    the row is XORed against itself shifted by one 16-bit word as a single big integer and the
    non-zero bytes of the result -- a few dozen per row -- are located with a regex at C speed.
    """
    raw = buffer.tobytes()
    value = int.from_bytes(raw, "big")
    difference = (value ^ (value >> 16)).to_bytes(len(raw), "big")
    starts = {0}
    for match in NONZERO_BYTE.finditer(difference):
        starts.add(match.start() // 2)
    ordered = sorted(starts)
    return b"".join(struct.pack(">HH", c, buffer[c]) for c in ordered), len(ordered)


def ocean_zone(longitude):
    """The Etc/GMT zone timezone-boundary-builder assigns to the 15-degree lune around a meridian."""
    offset = int(round(longitude / 15.0))
    offset = max(-12, min(12, offset))
    if offset == 0:
        return "Etc/GMT"
    # POSIX signs are inverted: Etc/GMT+5 is UTC-5.
    return f"Etc/GMT-{offset}" if offset > 0 else f"Etc/GMT+{-offset}"


def rasterise(edges):
    """Yields (row, runs blob, run count) for every row, painting them south to north."""
    buffer = array.array("h", bytes(2 * COLS))
    total_holes = 0
    empty_rows = 0
    started = time.time()
    for row in range(ROWS):
        paint_row(edges, row, buffer)
        holes = fill_gaps(buffer)
        if holes < 0:
            empty_rows += 1
            buffer[:] = ocean_row(edges)
        else:
            total_holes += holes
        blob, runs = encode_runs(buffer)
        yield row, blob, runs
        if row % 500 == 0:
            elapsed = time.time() - started
            log(f"  ... row {row}/{ROWS} ({elapsed:.0f}s)")
    log(f"  filled {total_holes:,} sliver cells; {empty_rows} rows had no polygon at all")


_OCEAN_ROW = None


def ocean_row(edges):
    """A whole row of ocean lunes, built once, for the (expected-empty) uncovered-row fallback."""
    global _OCEAN_ROW
    if _OCEAN_ROW is None:
        indices = {tzid: index for index, tzid in enumerate(edges.zones)}
        row = array.array("h", bytes(2 * COLS))
        for column in range(COLS):
            tzid = ocean_zone(column_longitude(column))
            index = indices.get(tzid)
            if index is None:
                index = len(edges.zones)
                edges.zones.append(tzid)
                indices[tzid] = index
            row[column] = index
        _OCEAN_ROW = row
    return _OCEAN_ROW


# ------------------------------------------------------------------------------------------------
# Writing
# ------------------------------------------------------------------------------------------------

def write_database(edges):
    os.makedirs(os.path.dirname(OUTPUT_DB), exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(OUTPUT_DB + suffix):
            os.remove(OUTPUT_DB + suffix)

    connection = sqlite3.connect(OUTPUT_DB)
    cursor = connection.cursor()
    cursor.executescript(
        """
        PRAGMA journal_mode = DELETE;
        CREATE TABLE tz_meta  (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE tz_zones (id INTEGER PRIMARY KEY, tzid TEXT NOT NULL);
        CREATE TABLE tz_rows  (row INTEGER PRIMARY KEY, runs BLOB NOT NULL);
        """
    )

    log("Rasterising ...")
    total_runs = 0
    batch = []
    for row, blob, runs in rasterise(edges):
        total_runs += runs
        batch.append((row, blob))
        if len(batch) >= 500:
            cursor.executemany("INSERT INTO tz_rows VALUES (?, ?)", batch)
            batch.clear()
    if batch:
        cursor.executemany("INSERT INTO tz_rows VALUES (?, ?)", batch)

    cursor.executemany(
        "INSERT INTO tz_zones VALUES (?, ?)",
        [(index, tzid) for index, tzid in enumerate(edges.zones)],
    )
    cursor.executemany(
        "INSERT INTO tz_meta VALUES (?, ?)",
        [
            ("format_version", str(FORMAT_VERSION)),
            ("rows_per_degree", str(ROWS_PER_DEGREE)),
            ("cols_per_degree", str(COLS_PER_DEGREE)),
            ("source", SOURCE_PROJECT),
            ("source_release", SOURCE_RELEASE),
            ("source_url", SOURCE_URL),
            ("licence", "ODbL 1.0 - OpenStreetMap contributors"),
            ("generated", date.today().isoformat()),
        ],
    )
    connection.commit()
    cursor.execute("VACUUM")
    connection.commit()
    connection.close()
    return total_runs


def warn_about_unknown_zones(zones):
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:
        return
    unknown = []
    for tzid in zones:
        try:
            ZoneInfo(tzid)
        except (ZoneInfoNotFoundError, ValueError):
            unknown.append(tzid)
    if unknown:
        log(f"  WARNING: {len(unknown)} zone ids unknown to this machine's tzdata: {unknown}")


# ------------------------------------------------------------------------------------------------
# Verification
# ------------------------------------------------------------------------------------------------

def claimants(edges, latitude, longitude):
    """
    Every zone whose source geometry contains this point, by brute-force point-in-polygon.

    Usually one, but not always: about 0.3% of points fall inside two overlapping
    timezone-boundary-builder polygons, so --verify has to accept any of them rather than pick one.
    """
    found = set()
    for polygon_index, longitudes in edges.crossings_at(row_of(latitude), latitude).items():
        if sum(1 for x in longitudes if x > longitude) % 2 == 1:
            found.add(edges.zone_of_polygon[polygon_index])
    return found


def raster_zone_at(connection, latitude, longitude):
    row = row_of(latitude)
    column = column_of(longitude)
    blob = connection.execute(
        "SELECT runs FROM tz_rows WHERE row = ?", (row,)
    ).fetchone()[0]
    count = len(blob) // 4
    low, high, found = 0, count - 1, 0
    while low <= high:
        middle = (low + high) // 2
        start = struct.unpack_from(">H", blob, middle * 4)[0]
        if start <= column:
            found = middle
            low = middle + 1
        else:
            high = middle - 1
    return struct.unpack_from(">H", blob, found * 4 + 2)[0]


def verify(edges, samples):
    log(f"Verifying {samples:,} random points against the source geometry ...")
    connection = sqlite3.connect(f"file:{OUTPUT_DB}?mode=ro", uri=True)
    generator = random.Random(20260823)
    mismatches = 0
    quantisation = 0
    unexplained = []
    for _ in range(samples):
        latitude = generator.uniform(-89.999, 89.999)
        longitude = generator.uniform(-179.999, 179.999)
        expected = claimants(edges, latitude, longitude)
        if not expected:
            continue
        actual = raster_zone_at(connection, latitude, longitude)
        if actual in expected:
            continue
        mismatches += 1
        # Faithful quantisation: the raster gives the zone at the cell's own centre, and the sample
        # simply sits on the far side of a boundary that runs through the cell.
        centre = claimants(
            edges, row_latitude(row_of(latitude)), column_longitude(column_of(longitude))
        )
        if actual in centre:
            quantisation += 1
        else:
            unexplained.append((latitude, longitude, sorted(expected), actual))
    connection.close()
    log(
        f"  {mismatches:,} of {samples:,} samples differ "
        f"({100.0 * mismatches / samples:.3f}%); {quantisation:,} are grid quantisation, "
        f"{len(unexplained):,} are unexplained"
    )
    for latitude, longitude, expected, actual in unexplained[:20]:
        names = ", ".join(edges.zones[z] for z in expected)
        log(f"    {latitude:.5f}, {longitude:.5f}: expected {names}, got {edges.zones[actual]}")
    return len(unexplained)


# ------------------------------------------------------------------------------------------------

def main():
    verify_samples = 0
    if "--verify" in sys.argv:
        index = sys.argv.index("--verify")
        verify_samples = int(sys.argv[index + 1]) if len(sys.argv) > index + 1 else 20000

    if not os.path.exists(INPUT_FILE):
        log(f"Missing {INPUT_FILE}.")
        log(f"Download {SOURCE_URL} and unzip it into test_dir/.")
        return 1

    log(f"Reading {INPUT_FILE} ({os.path.getsize(INPUT_FILE) / 1e6:.0f} MB) ...")
    edges = load_edges(INPUT_FILE)
    log("Indexing edges by row ...")
    edges.build_row_index()
    warn_about_unknown_zones(edges.zones)

    total_runs = write_database(edges)
    size = os.path.getsize(OUTPUT_DB)
    log("")
    log(f"Generated {OUTPUT_DB}  ({size // 1024:,} KB)")
    log(
        f"  grid {ROWS} x {COLS} ({ROWS_PER_DEGREE}/deg lat, {COLS_PER_DEGREE}/deg lon), "
        f"{len(edges.zones)} zones, {total_runs:,} runs "
        f"({total_runs / ROWS:.1f} per row)"
    )

    if verify_samples:
        return 1 if verify(edges, verify_samples) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
