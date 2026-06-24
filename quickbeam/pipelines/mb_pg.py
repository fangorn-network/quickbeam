"""
MusicBrainz Postgres → Fangorn creative-core graph.

This is the "convert a relational DB into a semantically searchable space"
pipeline. Where mb.py scrapes a flat track list out of the *release* JSON dump,
this connects directly to a MusicBrainz **Postgres** database (imported from the
official mbdump via musicbrainz-docker) and walks the real relational schema —
the core entity tables plus the `l_*` link tables and the `link_type` taxonomy —
emitting a typed knowledge graph:

  Nodes : Artist, ReleaseGroup, Release, Recording, Work
  Edges : performanceOf, composer/lyricist/arranger, producer/vocal/instrument,
          memberOfBand/collaboration, samples/remixOf, hasRelease, hasTrack,
          byArtist  (relationship names come straight from link_type.name)

Output matches mb.py's convention so the existing publish → bundle-embed → UMAP
path is unchanged: one `volume_<n>_<schema>.json` array of {name, fields} per
node type, plus one `volume_<n>_edges.json` array of {rel, from, to, ...}. Node
`name` is the MusicBrainz MBID (a globally-unique UUID across entity types), so
edges reference MBIDs directly with no namespacing needed.

Requires: psycopg[binary]  (pip install 'psycopg[binary]')
"""
import os
import json
import argparse
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

try:
    import psycopg
except ImportError:  # pragma: no cover - surfaced at runtime with a clear hint
    psycopg = None

SCHEMA_VERSION = 1
DEFAULT_DSN = "postgresql://musicbrainz:musicbrainz@localhost:5432/musicbrainz_db"

# The set of entities we can materialise as nodes is the ENTITIES registry below
# (one declarative entry per MB table). Add an entity = add one registry entry.


# ===========================================================================
# SQL  (kept as named constants so they're easy to validate against the live DB)
#   Conventions: every entity exposes `gid` (MBID) as the node name. Tag/genre
#   aggregation pulls the highest-voted community tags. Ratings come from the
#   `*_meta` tables shipped in mbdump-derived.
# ===========================================================================
def _tags_subquery(link_table: str, fk: str) -> str:
    return f"""(
        SELECT array_agg(t.name ORDER BY tt.count DESC)
        FROM {link_table} tt JOIN tag t ON tt.tag = t.id
        WHERE tt.{fk} = e.id AND tt.count > 0
    )"""


# --- schema introspection (lets a node SELECT adapt to whatever the DB has) ---
def _table_exists(conn, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (name,))
        return cur.fetchone()[0] is not None


def _column_exists(conn, table: str, col: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s LIMIT 1",
            (table, col),
        )
        return cur.fetchone() is not None


def _mb_core_sql(conn, table: str, *, extra_cols=(), extra_joins=()) -> str:
    """Convention-based SELECT for a MusicBrainz core entity: pulls gid/name/comment
    plus type / dates / rating / tags *only where those companion tables & columns
    exist*. Generalizes the per-entity SQL so adding an entity is ~one line — and it
    won't reference a `<table>_meta`/`<table>_type` that a given dump doesn't ship."""
    cols = ["e.gid", "e.name", "e.comment"]
    joins = []
    if _table_exists(conn, f"{table}_type"):
        cols.append("ty.name AS type")
        joins.append(f"LEFT JOIN {table}_type ty ON e.type = ty.id")
    if _column_exists(conn, table, "begin_date_year"):
        cols += ["e.begin_date_year", "e.end_date_year", "e.ended"]
    if _table_exists(conn, f"{table}_meta") and _column_exists(conn, f"{table}_meta", "rating"):
        cols.append("mm.rating")
        joins.append(f"LEFT JOIN {table}_meta mm ON mm.id = e.id")
    cols += list(extra_cols)
    if _table_exists(conn, f"{table}_tag"):
        cols.append(f"{_tags_subquery(table + '_tag', table)} AS tags")
    joins += list(extra_joins)
    return f"SELECT {', '.join(cols)}\nFROM {table} e\n" + "\n".join(joins)


SQL_ARTIST = f"""
SELECT e.gid, e.name, e.sort_name,
       at.name  AS type,
       ar.name  AS area,
       g.name   AS gender,
       e.begin_date_year, e.end_date_year, e.ended, e.comment,
       am.rating,
       {_tags_subquery('artist_tag', 'artist')} AS tags
FROM artist e
LEFT JOIN artist_type at ON e.type = at.id
LEFT JOIN area        ar ON e.area = ar.id
LEFT JOIN gender      g  ON e.gender = g.id
LEFT JOIN artist_meta am ON am.id = e.id
"""

SQL_RELEASE_GROUP = f"""
SELECT e.gid, e.name,
       pt.name AS primary_type,
       e.comment,
       rgm.rating,
       ac.name AS artist_credit_name,
       {_tags_subquery('release_group_tag', 'release_group')} AS tags
FROM release_group e
LEFT JOIN release_group_primary_type pt ON e.type = pt.id
LEFT JOIN release_group_meta rgm ON rgm.id = e.id
LEFT JOIN artist_credit ac ON e.artist_credit = ac.id
"""

SQL_RELEASE = """
SELECT e.gid, e.name,
       rs.name AS status,
       e.barcode, e.comment,
       rg.gid  AS release_group_gid,
       ac.name AS artist_credit_name,
       (SELECT string_agg(l.name, ', ')
          FROM release_label rl JOIN label l ON rl.label = l.id
          WHERE rl.release = e.id) AS labels,
       (SELECT min(rc.date_year) FROM release_country rc WHERE rc.release = e.id) AS year
FROM release e
LEFT JOIN release_status rs ON e.status = rs.id
LEFT JOIN release_group  rg ON e.release_group = rg.id
LEFT JOIN artist_credit  ac ON e.artist_credit = ac.id
"""

SQL_RECORDING = f"""
SELECT e.gid, e.name, e.length, e.video, e.comment,
       ac.name AS artist_credit_name,
       rm.rating,
       (SELECT array_agg(i.isrc) FROM isrc i WHERE i.recording = e.id) AS isrcs,
       {_tags_subquery('recording_tag', 'recording')} AS tags
FROM recording e
LEFT JOIN artist_credit ac ON e.artist_credit = ac.id
LEFT JOIN recording_meta rm ON rm.id = e.id
"""

SQL_WORK = f"""
SELECT e.gid, e.name,
       wt.name AS type,
       e.comment,
       (SELECT array_agg(w2.iswc) FROM iswc w2 WHERE w2.work = e.id) AS iswcs,
       {_tags_subquery('work_tag', 'work')} AS tags
FROM work e
LEFT JOIN work_type wt ON e.type = wt.id
"""

# (ENTITY_SQL / NODE_SHAPERS / FILE_STEM / type-name maps are unified into the
# declarative ENTITIES registry below. Relationship `l_*` tables are no longer a
# hardcoded list — they're auto-discovered among the selected entities.)


def _link_sql(table: str, t0: str, t1: str) -> str:
    return f"""
SELECT e0.gid AS from_gid,
       e1.gid AS to_gid,
       lt.name AS rel,
       lt.entity_type0, lt.entity_type1,
       l.begin_date_year, l.end_date_year, l.ended,
       (SELECT array_agg(lat.name)
          FROM link_attribute la JOIN link_attribute_type lat ON la.attribute_type = lat.id
          WHERE la.link = l.id) AS attributes
FROM {table} ln
JOIN link      l  ON ln.link = l.id
JOIN link_type lt ON l.link_type = lt.id
JOIN {t0} e0 ON ln.entity0 = e0.id
JOIN {t1} e1 ON ln.entity1 = e1.id
"""


# Structural edges derived from foreign keys (not the relationship system).
SQL_EDGE_RG_RELEASE = """
SELECT rg.gid AS from_gid, r.gid AS to_gid
FROM release r JOIN release_group rg ON r.release_group = rg.id
"""

SQL_EDGE_RELEASE_TRACK = """
SELECT r.gid AS from_gid, rec.gid AS to_gid
FROM track t
JOIN medium m   ON t.medium = m.id
JOIN release r  ON m.release = r.id
JOIN recording rec ON t.recording = rec.id
"""


def _byartist_sql(entity: str) -> str:
    return f"""
SELECT e.gid AS from_gid, a.gid AS to_gid, acn.position
FROM {entity} e
JOIN artist_credit_name acn ON e.artist_credit = acn.artist_credit
JOIN artist a ON acn.artist = a.id
"""


# ===========================================================================
# OUTPUT
# ===========================================================================
class JsonArrayWriter:
    """Streams objects into a single JSON-array file (matches mb.py output)."""
    def __init__(self, path: str):
        self._f = open(path, "w", encoding="utf-8")
        self._f.write("[\n")
        self._first = True
        self.count = 0

    def write(self, obj: dict):
        sep = "" if self._first else ",\n"
        # default=str coerces psycopg's UUID (gid columns) — and any stray
        # Decimal/datetime — into JSON-safe strings.
        self._f.write(f"{sep}  {json.dumps(obj, ensure_ascii=False, default=str)}")
        self._first = False
        self.count += 1

    def close(self):
        self._f.write("\n]")
        self._f.close()


def _clean(d: dict) -> dict:
    """Drop null/empty values so node payloads stay compact."""
    return {k: v for k, v in d.items()
            if v is not None and v != "" and v != [] and v != {}}


def _date(y, m=None, d=None) -> Optional[str]:
    return str(y) if y else None


# ---------------------------------------------------------------------------
# Per-entity field shaping + natural-language verbalisation for embedding.
# Each node carries a `text` summary of its own attributes; the typed edges
# (and optionally the bundle edge-walk) fold in the 1-hop neighbourhood.
# ---------------------------------------------------------------------------
def _node_artist(row: dict) -> dict:
    yrs = ""
    if row.get("begin_date_year") or row.get("end_date_year"):
        yrs = f" ({row.get('begin_date_year') or '?'}–{row.get('end_date_year') or ''})"
    text = (f"{row['name']}{yrs}: {row.get('type') or 'artist'}"
            + (f" from {row['area']}" if row.get("area") else "")
            + (f". Tags: {', '.join(row['tags'][:8])}" if row.get("tags") else ""))
    return _clean({
        "schemaVersion": SCHEMA_VERSION, "entityType": "Artist",
        "mbid": row["gid"], "title": row["name"], "byArtist": row["name"],
        "sortName": row.get("sort_name"), "artistType": row.get("type"),
        "area": row.get("area"), "gender": row.get("gender"),
        "beginYear": _date(row.get("begin_date_year")),
        "endYear": _date(row.get("end_date_year")),
        "disambiguation": row.get("comment"),
        "rating": row.get("rating"), "tags": row.get("tags"),
        "text": text,
    })


def _node_release_group(row: dict) -> dict:
    text = (f"{row['name']} — {row.get('primary_type') or 'release group'}"
            + (f" by {row['artist_credit_name']}" if row.get("artist_credit_name") else "")
            + (f". Tags: {', '.join(row['tags'][:8])}" if row.get("tags") else ""))
    return _clean({
        "schemaVersion": SCHEMA_VERSION, "entityType": "ReleaseGroup",
        "mbid": row["gid"], "title": row["name"],
        "byArtist": row.get("artist_credit_name"),
        "primaryType": row.get("primary_type"),
        "disambiguation": row.get("comment"),
        "rating": row.get("rating"), "tags": row.get("tags"),
        "text": text,
    })


def _node_release(row: dict) -> dict:
    text = (f"{row['name']} — release"
            + (f" by {row['artist_credit_name']}" if row.get("artist_credit_name") else "")
            + (f", {row['year']}" if row.get("year") else "")
            + (f" on {row['labels']}" if row.get("labels") else ""))
    return _clean({
        "schemaVersion": SCHEMA_VERSION, "entityType": "Release",
        "mbid": row["gid"], "title": row["name"],
        "byArtist": row.get("artist_credit_name"),
        "status": row.get("status"), "barcode": row.get("barcode"),
        "labelName": row.get("labels"), "datePublished": _date(row.get("year")),
        "disambiguation": row.get("comment"), "text": text,
    })


def _node_recording(row: dict) -> dict:
    mins = ""
    if row.get("length"):
        s = int(row["length"] / 1000); mins = f" ({s // 60}:{s % 60:02d})"
    text = (f"{row['name']}{mins} — recording"
            + (f" by {row['artist_credit_name']}" if row.get("artist_credit_name") else "")
            + (f". Tags: {', '.join(row['tags'][:8])}" if row.get("tags") else ""))
    return _clean({
        "schemaVersion": SCHEMA_VERSION, "entityType": "Recording",
        "mbid": row["gid"], "title": row["name"],
        "byArtist": row.get("artist_credit_name"),
        "durationMs": row.get("length"),
        "video": row.get("video"),
        "isrcCodes": row.get("isrcs"),
        "disambiguation": row.get("comment"),
        "rating": row.get("rating"), "tags": row.get("tags"),
        "text": text,
    })


def _node_work(row: dict) -> dict:
    text = (f"{row['name']} — {row.get('type') or 'work'}"
            + (f". Tags: {', '.join(row['tags'][:8])}" if row.get("tags") else ""))
    return _clean({
        "schemaVersion": SCHEMA_VERSION, "entityType": "Work",
        "mbid": row["gid"], "title": row["name"],
        "workType": row.get("type"), "iswcCodes": row.get("iswcs"),
        "disambiguation": row.get("comment"), "tags": row.get("tags"),
        "text": text,
    })


# --- cultural-layer entities (place / event / area / instrument) -------------
# These use the convention-based _mb_core_sql (gid/name/comment/type/dates/tags/
# rating where present) plus a few entity-specific columns, and a `text` template.
def _tags_text(row: dict) -> str:
    return f". Tags: {', '.join(row['tags'][:8])}" if row.get("tags") else ""


def _node_area(row: dict) -> dict:
    text = f"{row['name']} — {row.get('type') or 'area'}" + _tags_text(row)
    return _clean({
        "schemaVersion": SCHEMA_VERSION, "entityType": "Area",
        "mbid": row["gid"], "title": row["name"], "areaType": row.get("type"),
        "beginYear": _date(row.get("begin_date_year")), "endYear": _date(row.get("end_date_year")),
        "disambiguation": row.get("comment"), "tags": row.get("tags"), "text": text,
    })


def _node_place(row: dict) -> dict:
    text = (f"{row['name']} — {row.get('type') or 'place'}"
            + (f" in {row['area']}" if row.get("area") else "")
            + (f", {row['address']}" if row.get("address") else "")
            + _tags_text(row))
    return _clean({
        "schemaVersion": SCHEMA_VERSION, "entityType": "Place",
        "mbid": row["gid"], "title": row["name"], "placeType": row.get("type"),
        "area": row.get("area"), "address": row.get("address"),
        "coordinates": row.get("coordinates"),
        "beginYear": _date(row.get("begin_date_year")), "endYear": _date(row.get("end_date_year")),
        "disambiguation": row.get("comment"), "tags": row.get("tags"), "text": text,
    })


def _node_event(row: dict) -> dict:
    when = _date(row.get("begin_date_year"))
    text = (f"{row['name']} — {row.get('type') or 'event'}"
            + (f" ({when})" if when else "")
            + (" [cancelled]" if row.get("cancelled") else "")
            + _tags_text(row))
    return _clean({
        "schemaVersion": SCHEMA_VERSION, "entityType": "Event",
        "mbid": row["gid"], "title": row["name"], "eventType": row.get("type"),
        "time": row.get("time"), "cancelled": row.get("cancelled"),
        "setlist": (row.get("setlist") or "")[:2000] or None,
        "datePublished": when,
        "disambiguation": row.get("comment"), "tags": row.get("tags"), "text": text,
    })


def _node_instrument(row: dict) -> dict:
    text = (f"{row['name']} — {row.get('type') or 'instrument'}"
            + (f": {row['description']}" if row.get("description") else "")
            + _tags_text(row))
    return _clean({
        "schemaVersion": SCHEMA_VERSION, "entityType": "Instrument",
        "mbid": row["gid"], "title": row["name"], "instrumentType": row.get("type"),
        "description": row.get("description"),
        "disambiguation": row.get("comment"), "tags": row.get("tags"), "text": text,
    })


# ===========================================================================
# ENTITY REGISTRY — one declarative entry per MB table. To ingest a new entity
# (e.g. place, event, label, area), add a SQL_* SELECT + a _node_* shaper above
# and one Entity(...) line here; nothing else changes. `table` must be the real
# MusicBrainz table name (it's also the `l_<a>_<b>` link-table component used by
# auto-discovery).
# ===========================================================================
@dataclass(frozen=True)
class Entity:
    table: str                              # MB table name (= registry key, = l_* component)
    type_name: str                          # Fangorn node type ("Recording", "Place", …)
    file_stem: str                          # output filename stem (volume_<n>_<stem>.json)
    sql: "str | Callable[[object], str]"    # SELECT string, or a builder(conn)->SELECT (introspecting)
    shaper: Callable[[dict], dict]          # row → node fields


ENTITIES: dict[str, Entity] = {
    "artist":        Entity("artist",        "Artist",       "artists",       SQL_ARTIST,        _node_artist),
    "release_group": Entity("release_group", "ReleaseGroup", "releasegroups", SQL_RELEASE_GROUP, _node_release_group),
    "release":       Entity("release",       "Release",      "releases",      SQL_RELEASE,       _node_release),
    "recording":     Entity("recording",     "Recording",    "recordings",    SQL_RECORDING,     _node_recording),
    "work":          Entity("work",          "Work",         "works",         SQL_WORK,          _node_work),
    # cultural layer — convention-based SQL (introspected), bespoke text shapers
    "area":          Entity("area",          "Area",         "areas",
                            lambda c: _mb_core_sql(c, "area"), _node_area),
    "place":         Entity("place",         "Place",        "places",
                            lambda c: _mb_core_sql(c, "place",
                                extra_cols=("ar.name AS area", "e.address", "e.coordinates::text AS coordinates"),
                                extra_joins=("LEFT JOIN area ar ON e.area = ar.id",)), _node_place),
    "event":         Entity("event",         "Event",        "events",
                            lambda c: _mb_core_sql(c, "event",
                                extra_cols=("e.time::text AS time", "e.setlist", "e.cancelled")), _node_event),
    "instrument":    Entity("instrument",    "Instrument",   "instruments",
                            lambda c: _mb_core_sql(c, "instrument", extra_cols=("e.description",)), _node_instrument),
}


def _type_name(table: str) -> str:
    e = ENTITIES.get(table)
    return e.type_name if e else table.replace("_", " ").title().replace(" ", "")


# Structural edges from foreign keys (not the relationship system). Each is only
# emitted when both its endpoint entities are in the selected set.
STRUCTURAL_EDGES = [
    {"label": "releaseGroup→release", "sql": SQL_EDGE_RG_RELEASE,    "rel": "hasRelease", "from": "release_group", "to": "release"},
    {"label": "release→recording",    "sql": SQL_EDGE_RELEASE_TRACK, "rel": "hasTrack",   "from": "release",       "to": "recording"},
]
# Entities whose artist_credit yields a byArtist edge (emitted when artist is selected too).
BYARTIST_FROM = ["release_group", "release", "recording"]


# ===========================================================================
# STREAMING
# ===========================================================================
def _server_cursor(conn, sql: str, limit: int):
    """A named (server-side) cursor so we stream rows instead of buffering 37M
    recordings in client memory."""
    if limit:
        sql = sql + f"\nLIMIT {int(limit)}"
    cur = conn.cursor(name="mb_pg_stream", row_factory=psycopg.rows.dict_row)
    cur.itersize = 5000
    cur.execute(sql)
    return cur


def export_nodes(conn, entity: str, out_path: str, limit: int = 0, kept: bool = False) -> int:
    spec = ENTITIES[entity]
    sql = spec.sql(conn) if callable(spec.sql) else spec.sql  # builder(conn) introspects; str is literal
    if kept:
        sql = _filter_by_kept(sql, ["gid"])  # quality mode: only nodes in the kept set
    writer = JsonArrayWriter(out_path)
    print(f"   📤 {entity}: streaming nodes → {os.path.basename(out_path)}")
    try:
        cur = _server_cursor(conn, sql, limit)
        for row in cur:
            writer.write({"name": row["gid"], "fields": spec.shaper(row)})
            if writer.count % 250_000 == 0:
                print(f"      …{writer.count:,} {entity}s", flush=True)
        cur.close()
    finally:
        writer.close()
    print(f"   ✅ {entity}: {writer.count:,} nodes")
    return writer.count


def discover_link_tables(conn, entities: list[str]) -> list[tuple[str, str, str]]:
    """Every MusicBrainz `l_<a>_<b>` relationship table that exists among the
    selected entities. MB stores exactly one table per unordered pair (in a fixed
    a/b order), so we probe each ordered pair with to_regclass (respects the same
    search_path as the rest of our queries) and keep whichever name resolves.
    Returns (table, entity0, entity1) with the endpoint order the table expects."""
    found: list[tuple[str, str, str]] = []
    with conn.cursor() as cur:
        for a in entities:
            for b in entities:
                table = f"l_{a}_{b}"
                cur.execute("SELECT to_regclass(%s)", (table,))
                if cur.fetchone()[0] is not None:
                    found.append((table, a, b))
    return found


def export_edges(conn, out_path: str, link_tables: list[tuple[str, str, str]],
                 selected: set[str], include_structural: bool = True,
                 limit: int = 0, kept: bool = False) -> int:
    # In quality mode, only edges whose BOTH endpoints are in the kept set are written.
    def _e(sql: str) -> str:
        return _filter_by_kept(sql, ["from_gid", "to_gid"]) if kept else sql

    writer = JsonArrayWriter(out_path)
    print(f"   📤 edges → {os.path.basename(out_path)}")
    try:
        # Relationship-system edges (the deep connections), auto-discovered among
        # the selected entities. `rel` is link_type.name; fromType/toType are the
        # Fangorn node-type names — together these become the bundle's edge shapes.
        for table, t0, t1 in link_tables:
            ft, tt = _type_name(t0), _type_name(t1)
            cur = _server_cursor(conn, _e(_link_sql(table, t0, t1)), limit)
            n0 = writer.count
            for row in cur:
                writer.write(_clean({
                    "rel": row["rel"], "from": row["from_gid"], "to": row["to_gid"],
                    "fromType": ft, "toType": tt,
                    "attributes": row.get("attributes"),
                    "begin": _date(row.get("begin_date_year")),
                    "end": _date(row.get("end_date_year")),
                }))
            cur.close()
            print(f"      {table}: {writer.count - n0:,} edges", flush=True)

        if include_structural:
            # FK-derived edges, each emitted only when both endpoints are selected.
            for spec in STRUCTURAL_EDGES:
                if spec["from"] not in selected or spec["to"] not in selected:
                    continue
                ft, tt = _type_name(spec["from"]), _type_name(spec["to"])
                cur = _server_cursor(conn, _e(spec["sql"]), limit)
                n0 = writer.count
                for row in cur:
                    writer.write({"rel": spec["rel"], "from": row["from_gid"], "to": row["to_gid"],
                                  "fromType": ft, "toType": tt})
                cur.close()
                print(f"      {spec['label']}: {writer.count - n0:,} edges", flush=True)

            if "artist" in selected:
                for entity in BYARTIST_FROM:
                    if entity not in selected:
                        continue
                    ft = _type_name(entity)
                    cur = _server_cursor(conn, _e(_byartist_sql(entity)), limit)
                    n0 = writer.count
                    for row in cur:
                        writer.write({"rel": "byArtist", "from": row["from_gid"], "to": row["to_gid"],
                                      "fromType": ft, "toType": "Artist"})
                    cur.close()
                    print(f"      {entity}→artist (byArtist): {writer.count - n0:,} edges", flush=True)
    finally:
        writer.close()
    print(f"   ✅ edges: {writer.count:,} total")
    return writer.count


# ===========================================================================
# QUALITY CUT (Stage 2) — symmetric per-type seeds + capped neighbor expansion
#   Seeds: top-N of each entity by score = w_rating·rating + w_tags·ln(1+tagVotes),
#          using whatever quality signals the entity actually has (areas/instruments
#          may have neither → they ride in only as neighbors of quality seeds).
#   Expand: each seed pulls ≤ neighbor-cap 1-hop neighbors over the discovered links.
#   Everything is materialised server-side into a TEMP TABLE `kept(gid, entity)`;
#   exports then JOIN it. No big client-side sets; bounded memory.
# ===========================================================================
def _seed_score_expr(conn, entity: str, w_rating: float, w_tags: float):
    """(score_sql, extra_joins) from the entity's available signals; '0' if none."""
    terms, joins = [], []
    if _table_exists(conn, f"{entity}_meta") and _column_exists(conn, f"{entity}_meta", "rating"):
        terms.append(f"{w_rating} * COALESCE(sm.rating, 0)")
        joins.append(f"LEFT JOIN {entity}_meta sm ON sm.id = e.id")
    if _table_exists(conn, f"{entity}_tag"):
        terms.append(f"{w_tags} * ln(1 + COALESCE(stag.tv, 0))")
        joins.append(f"LEFT JOIN LATERAL (SELECT sum(tg.count) AS tv FROM {entity}_tag tg "
                     f"WHERE tg.{entity} = e.id AND tg.count > 0) stag ON true")
    return (" + ".join(terms) if terms else "0"), joins


def _filter_by_kept(sql: str, gid_cols: list[str]) -> str:
    """Wrap a SELECT so only rows whose given gid column(s) are in `kept` survive."""
    joins = " ".join(f"JOIN kept _k{i} ON _k{i}.gid = _q.{c}" for i, c in enumerate(gid_cols))
    return f"SELECT _q.* FROM (\n{sql}\n) _q {joins}"


def dry_run_seeds(conn, entities, *, targets, min_score, w_rating, w_tags):
    """Print how many seeds each entity would contribute — no temp tables, no output."""
    print("🔎 Dry run — seed counts (no extraction):")
    with conn.cursor() as cur:
        for e in entities:
            score, joins = _seed_score_expr(conn, e, w_rating, w_tags)
            where = f"WHERE ({score}) >= {min_score}" if min_score > 0 else ""
            cur.execute(f"SELECT count(*) FROM {e} e {' '.join(joins)} {where}")
            passing = cur.fetchone()[0]
            target = targets.get(e, 0)
            eff = min(passing, target) if target > 0 else passing
            sig = "" if score != "0" else "  ⚠️ no rating/tags (unranked)"
            cap = f"target {target:,}" if target > 0 else "no cap"
            print(f"   {e:>14}: {passing:,} pass → seed {eff:,} ({cap}){sig}")


def build_subgraph(conn, entities, link_tables, *, targets, neighbor_cap, min_score, w_rating, w_tags):
    """Materialise the quality subgraph into TEMP TABLE `kept`. Runs in the open
    transaction (no commit) so the export server-cursors see the temp tables."""
    cur = conn.cursor()
    for e in entities:
        score, joins = _seed_score_expr(conn, e, w_rating, w_tags)
        target = targets.get(e, 0)
        where = f"WHERE ({score}) >= {min_score}" if min_score > 0 else ""
        limit = f"LIMIT {int(target)}" if target > 0 else ""
        cur.execute(f"""
            CREATE TEMP TABLE seed_{e} AS
            SELECT e.id, e.gid
            FROM {e} e {' '.join(joins)}
            {where}
            ORDER BY ({score}) DESC
            {limit}
        """)
        cur.execute(f"CREATE INDEX ON seed_{e} (id)")
        cur.execute(f"SELECT count(*) FROM seed_{e}")
        print(f"   🌱 seed {e}: {cur.fetchone()[0]:,}" + ("" if score != "0" else "  (no rating/tags — unranked cap)"))

    cur.execute("CREATE TEMP TABLE kept (gid uuid PRIMARY KEY, entity text)")
    for e in entities:
        cur.execute(f"INSERT INTO kept (gid, entity) SELECT gid, %s FROM seed_{e} ON CONFLICT DO NOTHING", (e,))

    # 1-hop expansion, ≤ neighbor_cap neighbors per seed per relationship table.
    cap = f"WHERE rn <= {int(neighbor_cap)}" if neighbor_cap > 0 else ""
    for table, t0, t1 in link_tables:
        if t0 in entities:
            cur.execute(f"""
                INSERT INTO kept (gid, entity)
                SELECT gid, %s FROM (
                    SELECT e1.gid AS gid,
                           row_number() OVER (PARTITION BY ln.entity0 ORDER BY e1.id) AS rn
                    FROM {table} ln
                    JOIN seed_{t0} s ON ln.entity0 = s.id
                    JOIN {t1} e1 ON ln.entity1 = e1.id
                ) q {cap}
                ON CONFLICT DO NOTHING
            """, (t1,))
        if t1 in entities and t1 != t0:
            cur.execute(f"""
                INSERT INTO kept (gid, entity)
                SELECT gid, %s FROM (
                    SELECT e0.gid AS gid,
                           row_number() OVER (PARTITION BY ln.entity1 ORDER BY e0.id) AS rn
                    FROM {table} ln
                    JOIN seed_{t1} s ON ln.entity1 = s.id
                    JOIN {t0} e0 ON ln.entity0 = e0.id
                ) q {cap}
                ON CONFLICT DO NOTHING
            """, (t0,))
    cur.execute("CREATE INDEX ON kept (gid)")
    cur.execute("SELECT count(*) FROM kept")
    print(f"   📦 kept (seeds + capped neighbors): {cur.fetchone()[0]:,} nodes")
    cur.close()


# ===========================================================================
# CLI
# ===========================================================================
def parse_args():
    p = argparse.ArgumentParser(
        description="Convert a MusicBrainz Postgres DB into a Fangorn creative-core graph.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--dsn", default=os.environ.get("MB_PG_DSN", DEFAULT_DSN),
                   help="Postgres connection string (or env MB_PG_DSN).")
    p.add_argument("--output-dir", default="./stage_volumes")
    p.add_argument("--volume", type=int, default=1)
    p.add_argument("--entities", default=",".join(ENTITIES),
                   help="Comma-separated subset of the registry: " + ",".join(ENTITIES))
    p.add_argument("--limit", type=int, default=0,
                   help="Per-query row cap for smoke tests (0 = all).")
    p.add_argument("--no-edges", dest="edges", action="store_false", default=True)
    p.add_argument("--no-structural-edges", dest="structural", action="store_false", default=True)
    # ── quality cut (Stage 2). Enabled when a target/min-score is set. ──
    p.add_argument("--target-count", type=int, default=0,
                   help="Top-N seeds per entity by quality score (0 = no per-entity cap).")
    p.add_argument("--targets", default="",
                   help="Per-entity overrides, e.g. 'recording=1000000,artist=300000'.")
    p.add_argument("--neighbor-cap", type=int, default=50,
                   help="Max 1-hop neighbors pulled per seed per relationship table (0 = unlimited).")
    p.add_argument("--min-score", type=float, default=0.0,
                   help="Drop seeds scoring below this (0 = no threshold).")
    p.add_argument("--w-rating", type=float, default=1.0, help="Weight on community rating (0–100).")
    p.add_argument("--w-tags", type=float, default=20.0, help="Weight on ln(1+tag votes).")
    p.add_argument("--dry-run", action="store_true", default=False,
                   help="Print per-entity seed counts and exit (no extraction).")
    return p.parse_args()


def run():
    args = parse_args()
    if psycopg is None:
        raise SystemExit("psycopg not installed. Run: pip install 'psycopg[binary]'")

    os.makedirs(args.output_dir, exist_ok=True)
    requested = [e.strip() for e in args.entities.split(",") if e.strip()]
    entities = []
    for e in requested:
        if e in ENTITIES:
            entities.append(e)
        else:
            print(f"   ⚠️  unknown entity '{e}' (not in registry: {', '.join(ENTITIES)}), skipping")
    if not entities:
        raise SystemExit("No valid entities selected.")
    selected = set(entities)

    # Per-entity targets: default --target-count for all, then --targets overrides.
    targets = {e: args.target_count for e in entities}
    for kv in args.targets.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            if k.strip() in targets:
                targets[k.strip()] = int(v)
    # Quality mode kicks in when any cap or threshold is requested.
    quality = args.dry_run or args.min_score > 0 or any(t > 0 for t in targets.values())
    score_kw = dict(w_rating=args.w_rating, w_tags=args.w_tags)

    print(f"🔌 Connecting: {args.dsn.rsplit('@', 1)[-1]}")
    with psycopg.connect(args.dsn) as conn:
        if args.dry_run:
            dry_run_seeds(conn, entities, targets=targets, min_score=args.min_score, **score_kw)
            return

        # Links are needed for neighbor expansion (quality) and/or edge output.
        link_tables = discover_link_tables(conn, entities) if (quality or args.edges) else []
        if link_tables:
            print(f"🔗 Auto-discovered {len(link_tables)} relationship table(s) among "
                  f"{len(entities)} entit(ies): {', '.join(t for t, _, _ in link_tables) or '(none)'}")

        if quality:
            print(f"⭐ Quality cut: seeds=top-N by score (w_rating={args.w_rating}, w_tags={args.w_tags}), "
                  f"neighbor-cap={args.neighbor_cap}, min-score={args.min_score}")
            build_subgraph(conn, entities, link_tables, targets=targets,
                           neighbor_cap=args.neighbor_cap, min_score=args.min_score, **score_kw)

        totals = {}
        for entity in entities:
            path = os.path.join(args.output_dir, f"volume_{args.volume}_{ENTITIES[entity].file_stem}.json")
            totals[entity] = export_nodes(conn, entity, path, args.limit, kept=quality)

        if args.edges:
            epath = os.path.join(args.output_dir, f"volume_{args.volume}_edges.json")
            totals["edges"] = export_edges(conn, epath, link_tables, selected, args.structural, args.limit, kept=quality)

    print("\n📊 Done:")
    for k, v in totals.items():
        print(f"   {k:>16}: {v:,}")


if __name__ == "__main__":
    run()
