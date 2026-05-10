#!/usr/bin/env python3
"""
cache_tiles.py — pre-cache map tiles into an MBTiles file for offline use with mesh-mapper.

Example:
    python tools/cache_tiles.py \
        --bbox -122.6 37.6 -122.3 37.9 \
        --zoom 0 16 \
        --source esriWorldImagery \
        --out tiles/bay_area.mbtiles

The output file can be dropped into mesh-mapper's `tiles/` dir and will appear
in the basemap dropdown automatically.

Be respectful of free tile providers — keep concurrency low and respect their
terms of service. OSM's main tile server in particular forbids bulk download.
"""
import argparse
import math
import os
import sqlite3
import sys
import time

import requests

TILE_SOURCES = {
    'osmStandard':      {'url': 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',                                                          'fmt': 'png',  'attrib': '© OpenStreetMap'},
    'osmHumanitarian':  {'url': 'https://a.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png',                                                      'fmt': 'png',  'attrib': '© HOT OSM'},
    'cartoPositron':    {'url': 'https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png',                                                'fmt': 'png',  'attrib': '© CARTO'},
    'cartoDarkMatter':  {'url': 'https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png',                                                 'fmt': 'png',  'attrib': '© CARTO'},
    'esriWorldImagery': {'url': 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',            'fmt': 'jpg',  'attrib': '© Esri'},
    'esriWorldTopo':    {'url': 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',           'fmt': 'jpg',  'attrib': '© Esri'},
    'esriDarkGray':     {'url': 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', 'fmt': 'png', 'attrib': '© Esri'},
    'openTopoMap':      {'url': 'https://a.tile.opentopomap.org/{z}/{x}/{y}.png',                                                           'fmt': 'png',  'attrib': '© OpenTopoMap'},
}


def deg2num(lat_deg, lon_deg, zoom):
    lat_rad = math.radians(lat_deg)
    n = 1 << zoom
    x = int((lon_deg + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    x = max(0, min(n - 1, x))
    y = max(0, min(n - 1, y))
    return x, y


def tiles_for_bbox(bbox, zmin, zmax):
    w, s, e, n = bbox
    for z in range(zmin, zmax + 1):
        x0, y1 = deg2num(n, w, z)
        x1, y0 = deg2num(s, e, z)
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                yield z, x, y


def count_tiles(bbox, zmin, zmax):
    total = 0
    w, s, e, n = bbox
    for z in range(zmin, zmax + 1):
        x0, y1 = deg2num(n, w, z)
        x1, y0 = deg2num(s, e, z)
        total += (abs(x1 - x0) + 1) * (abs(y1 - y0) + 1)
    return total


def open_mbtiles(path, source, bbox, zmin, zmax):
    new = not os.path.exists(path)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT)")
    conn.execute("""CREATE TABLE IF NOT EXISTS tiles (
        zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB,
        PRIMARY KEY (zoom_level, tile_column, tile_row))""")
    src = TILE_SOURCES[source]
    base_name = os.path.splitext(os.path.basename(path))[0]
    meta = {
        'name': base_name, 'format': src['fmt'], 'type': 'baselayer', 'version': '1.1',
        'description': f'Cached from {source}', 'attribution': src['attrib'],
        'minzoom': str(zmin), 'maxzoom': str(zmax),
        'bounds': ','.join(str(x) for x in bbox),
    }
    for k, v in meta.items():
        conn.execute("INSERT OR REPLACE INTO metadata(name, value) VALUES(?,?)", (k, v))
    return conn, new


PRESETS = {
    # name: (bbox, zmin, zmax)
    'world':       ((-180.0, -85.0, 180.0, 85.0), 0, 6),
    'world-z8':    ((-180.0, -85.0, 180.0, 85.0), 0, 8),
    'world-z5':    ((-180.0, -85.0, 180.0, 85.0), 0, 5),
}


def cache_one(source, bbox, zmin, zmax, out_path, rate, dry_run):
    """Fetch a single source into a single mbtiles. Returns (fetched, skipped, errors)."""
    total = count_tiles(bbox, zmin, zmax)
    print(f"[i] {total:,} tiles · zooms {zmin}-{zmax} · source={source} · -> {out_path}")
    if dry_run:
        return 0, 0, 0

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or '.', exist_ok=True)
    conn, _ = open_mbtiles(out_path, source, bbox, zmin, zmax)
    src = TILE_SOURCES[source]
    sess = requests.Session()
    sess.headers.update({'User-Agent': 'drone-mesh-mapper/cache_tiles.py'})

    done = fetched = skipped = errors = 0
    started = time.time()
    try:
        for z, x, y in tiles_for_bbox(bbox, zmin, zmax):
            tms_y = (1 << z) - 1 - y
            row = conn.execute(
                "SELECT 1 FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=? LIMIT 1",
                (z, x, tms_y)).fetchone()
            if row:
                skipped += 1
            else:
                url = src['url'].replace('{z}', str(z)).replace('{x}', str(x)).replace('{y}', str(y))
                try:
                    r = sess.get(url, timeout=15)
                    if r.status_code == 200 and r.content:
                        conn.execute(
                            "INSERT OR REPLACE INTO tiles(zoom_level, tile_column, tile_row, tile_data) VALUES(?,?,?,?)",
                            (z, x, tms_y, r.content))
                        fetched += 1
                    elif r.status_code == 429:
                        time.sleep(2.0)
                        errors += 1
                    else:
                        errors += 1
                except Exception:
                    errors += 1
                time.sleep(rate)
            done += 1
            if done % 50 == 0 or done == total:
                pct = 100.0 * done / total if total else 100.0
                tps = done / max(1.0, time.time() - started)
                eta = (total - done) / tps if tps > 0 else 0
                print(f"\r[+] {source}: {done:,}/{total:,} ({pct:5.1f}%) · fetched={fetched} skipped={skipped} errors={errors} · {tps:5.1f} t/s · ETA {eta:6.0f}s", end='', flush=True)
    except KeyboardInterrupt:
        print("\n[!] interrupted; partial mbtiles preserved")
        raise
    finally:
        conn.close()
    print(f"\n[✓] wrote {out_path}  ({fetched} fetched, {skipped} skipped, {errors} errors)")
    return fetched, skipped, errors


def expand_sources(spec):
    """'all' → every source. comma-list → split. single → [single]."""
    if spec == 'all':
        return sorted(TILE_SOURCES.keys())
    return [s.strip() for s in spec.split(',') if s.strip()]


def main():
    ap = argparse.ArgumentParser(
        description="Pre-cache map tiles into one or more MBTiles files.",
        epilog="Examples:\n"
               "  cache_tiles.py --preset world --source esriWorldImagery --out tiles/\n"
               "  cache_tiles.py --preset world --source all --out tiles/\n"
               "  cache_tiles.py --bbox -122.6 37.6 -122.3 37.9 --zoom 0 16 --source esriWorldImagery,cartoDarkMatter --out tiles/sf.mbtiles\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--bbox', nargs=4, type=float,
                    metavar=('WEST', 'SOUTH', 'EAST', 'NORTH'),
                    help='bounding box (omit if --preset is set)')
    ap.add_argument('--zoom', nargs=2, type=int, metavar=('ZMIN', 'ZMAX'),
                    help='zoom range (omit if --preset is set)')
    ap.add_argument('--preset', choices=sorted(PRESETS.keys()),
                    help='preset bbox + zoom range (overrides --bbox/--zoom)')
    ap.add_argument('--source', required=True,
                    help='single source, comma-separated list, or "all". '
                         'Sources: ' + ','.join(sorted(TILE_SOURCES.keys())))
    ap.add_argument('--out', required=True,
                    help='output .mbtiles path. For multi-source, pass a directory '
                         'and files will be named world_<source>.mbtiles or <stem>_<source>.mbtiles')
    ap.add_argument('--rate', type=float, default=0.05, help='sleep seconds between tile fetches')
    ap.add_argument('--dry-run', action='store_true', help='just count tiles, do not fetch')
    args = ap.parse_args()

    # Resolve bbox + zooms (preset wins)
    if args.preset:
        bbox, zmin, zmax = PRESETS[args.preset]
    else:
        if not args.bbox or not args.zoom:
            sys.exit("must pass either --preset or both --bbox and --zoom")
        bbox = tuple(args.bbox)
        zmin, zmax = args.zoom
    if zmax < zmin:
        sys.exit("zMax must be >= zMin")

    sources = expand_sources(args.source)
    for s in sources:
        if s not in TILE_SOURCES:
            sys.exit(f"unknown source: {s}. options: {','.join(sorted(TILE_SOURCES.keys()))}")

    # Resolve output paths.
    out_is_dir = (args.out.endswith('/') or args.out.endswith(os.sep)
                  or os.path.isdir(args.out)
                  or (len(sources) > 1 and not args.out.endswith('.mbtiles')))

    if out_is_dir:
        os.makedirs(args.out, exist_ok=True)
        stem = 'world' if args.preset and args.preset.startswith('world') else 'cache'
        out_paths = {s: os.path.join(args.out, f"{stem}_{s}.mbtiles") for s in sources}
    elif len(sources) == 1:
        out_paths = {sources[0]: args.out}
    else:
        # multi-source but a single .mbtiles path given: derive sibling files
        base, ext = os.path.splitext(args.out)
        out_paths = {s: f"{base}_{s}{ext}" for s in sources}

    # Show plan up front so the user can ctrl-C before any network hits
    print(f"[i] preset={args.preset or '(custom)'}  bbox={bbox}  zooms={zmin}-{zmax}")
    print(f"[i] {len(sources)} source(s): {', '.join(sources)}")
    grand = 0
    for s in sources:
        n = count_tiles(bbox, zmin, zmax)
        grand += n
        print(f"    - {s:<20s} → {out_paths[s]}  ({n:,} tiles)")
    print(f"[i] grand total ≈ {grand:,} tiles")
    if args.dry_run:
        return

    totals = {'fetched': 0, 'skipped': 0, 'errors': 0}
    for s in sources:
        try:
            f, sk, er = cache_one(s, bbox, zmin, zmax, out_paths[s], args.rate, args.dry_run)
        except KeyboardInterrupt:
            return
        totals['fetched'] += f
        totals['skipped'] += sk
        totals['errors'] += er
    print(f"\n[✓] all sources done · fetched={totals['fetched']:,} skipped={totals['skipped']:,} errors={totals['errors']:,}")


if __name__ == '__main__':
    main()
