A new appraoch for the embeddings builder is where your graph starts acting less like a database export and more like a knowledge graph.

Right now your builder is effectively doing:

```text
Track node
  + immediate neighbors
    → document
      → embedding
```

With hierarchical roots, you're doing:

```text
Graph
  ├─ Track projection
  ├─ Place projection
  ├─ Event projection
  └─ Artist projection
```

Each projection produces a different document from the same underlying graph.

---

## What changes conceptually

Imagine this graph:

```text
Place: Berlin
  ├─ Event: Berghain Night
  │      ├─ Artist: Marcel Dettmann
  │      └─ Track: Transmission 09
  │
  └─ Event: Tresor Session
         └─ Artist: Helena Hauff
```

Today you might emit:

```json
{
  "type": "Track",
  "title": "Transmission 09",
  "artist": "Marcel Dettmann"
}
```

With hierarchical roots you'd emit multiple records:

### Track projection

```json
{
  "entityType": "Track",
  "title": "Transmission 09",
  "artist": "Marcel Dettmann",
  "event": "Berghain Night",
  "place": "Berlin"
}
```

### Artist projection

```json
{
  "entityType": "Artist",
  "name": "Marcel Dettmann",
  "tracks": ["Transmission 09"],
  "events": ["Berghain Night"],
  "places": ["Berlin"]
}
```

### Place projection

```json
{
  "entityType": "Place",
  "name": "Berlin",
  "artists": [
    "Marcel Dettmann",
    "Helena Hauff"
  ],
  "events": [
    "Berghain Night",
    "Tresor Session"
  ]
}
```

All three become separate embeddings.

---

## First thing I'd change

Instead of:

```python
parser.add_argument(
    "--root-type",
    default="Track"
)
```

I'd make:

```python
parser.add_argument(
    "--root-profile",
    action="append",
    default=[]
)
```

Usage:

```bash
--root-profile track
--root-profile place
--root-profile artist
```

---

## Then define profiles

Something like:

```python
ROOT_PROFILES = {
    "track": {
        "root_type": "Track",
        "max_depth": 2,
        "include": [
            "Artist",
            "Genre",
            "Place",
            "Event"
        ]
    },

    "place": {
        "root_type": "Place",
        "max_depth": 3,
        "include": [
            "Artist",
            "Track",
            "Event"
        ]
    },

    "artist": {
        "root_type": "Artist",
        "max_depth": 2,
        "include": [
            "Track",
            "Place",
            "Event"
        ]
    }
}
```

The profile controls traversal.

---

## Build a graph first

Right now you're building:

```python
out[from].append(to)
```

which is enough for a one-hop join.

For hierarchical projections I'd build:

```python
out_edges = {}
in_edges = {}

for edge in edges:
    out_edges.setdefault(edge["from"], []).append(edge["to"])
    in_edges.setdefault(edge["to"], []).append(edge["from"])
```

Now you have bidirectional traversal.

---

## Generic graph walk

Something like:

```python
def walk_graph(
    root_id,
    nodes,
    out_edges,
    max_depth
):
    visited = set()
    queue = [(root_id, 0)]

    collected = []

    while queue:
        node_id, depth = queue.pop(0)

        if node_id in visited:
            continue

        visited.add(node_id)

        node = nodes[node_id]
        collected.append(node)

        if depth >= max_depth:
            continue

        for neighbor in out_edges.get(node_id, []):
            queue.append((neighbor, depth + 1))

    return collected
```

Now a Place can collect hundreds of related entities.

---

## Create projection builders

Instead of:

```python
fields.update(neighbor_fields)
```

have:

```python
def project_place(root, related):
```

```python
def project_track(root, related):
```

```python
def project_artist(root, related):
```

Each builds a different document.

Example:

```python
def project_place(root, related):

    artists = []
    tracks = []
    events = []

    for node in related:

        if node["type"] == "Artist":
            artists.append(node["fields"].get("name"))

        elif node["type"] == "Track":
            tracks.append(node["fields"].get("title"))

        elif node["type"] == "Event":
            events.append(node["fields"].get("title"))

    return {
        "entityType": "Place",
        "name": root["fields"].get("name"),
        "artists": artists[:50],
        "tracks": tracks[:50],
        "events": events[:50]
    }
```

---

## Store entity type

Your payload should gain:

```python
payload={
    "id": doc_id,
    "entityType": projection_type,
    "fields": projected_fields,
}
```

so search can filter:

```python
entityType == "Place"
```

or

```python
entityType == "Artist"
```

---

## Biggest architectural benefit

Right now:

```text
Track
  → embedding
```

is a 1:1 mapping.

Hierarchical roots become:

```text
Graph
  ↓

Track View
Artist View
Place View
Event View

  ↓

Embeddings
```

which means later you can add new semantic views without changing the graph model.

For example:

```text
Genre View
Label View
Playlist View
Festival View
City View
```

all generated from the same node/edge data.

That's usually where graph-native search systems end up: the graph is the source of truth, and embeddings are just different projections of that graph.
