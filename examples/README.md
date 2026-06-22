# Common Crawl examples — cross-domain data crawls

These examples use `quickbeam data crawl` to pull structured data **across many
domains** from [Common Crawl](https://commoncrawl.org), extract it into uniform
`{name, fields}` records, and (optionally) publish + embed them into one searchable
collection. The crawl picks the *sources*; the vector search is what makes it
**cross-domain** — every record lands in one schema, so a semantic query ranks
results from all sites together.

Everything here lives under [`cc/`](cc/):

| File | What it is |
|------|------------|
| `cc/extractors/generic_structured.py` | **domain-agnostic** schema.org JSON-LD + OpenGraph harvester — use this for cross-domain crawls |
| `cc/routes_generic.json` | catch-all route (`.*`) → the generic extractor |
| `cc/extractors/digikey_products.py`, `cc/routes_digikey.json` | a site-specific product extractor (kept as a reference; **DigiKey is bot-blocked in CC**, so it returns nothing there) |
| `cc/recipes.json`, `cc/generic.json` | real example output |

---

## Two rules that decide whether a crawl works

1. **Common Crawl is indexed by URL, not content.** You can't ask CC "pages about
   pants." You enumerate **domains / path-prefixes that map to content pages**, crawl
   them, and let the embeddings do the topical search. Targeting `/recipe/` gives you
   recipes; targeting `/recipes` (a category index) gives you `ItemList` landing
   pages. Choose prefixes that land on *individual items*.
2. **It's a historical snapshot, not live.** CC is a periodic crawl, so you get the
   page as captured (often months old). For some use cases (news-over-time, "what did
   this look like in crawl month X") that's a feature; for live prices/stock it isn't.
   Also: large bot-protected sites (most big retailers — DigiKey, Amazon, …) are
   captured as block stubs, so pick sites that serve real HTML to crawlers.

---

## Setup

Install CmonCrawl in its own venv (it pins an old pydantic) and point `--cmon-bin`
at it:

```sh
python -m venv ~/fangorn/embeddings/cmon_venv
~/fangorn/embeddings/cmon_venv/bin/pip install cmoncrawl
```

All commands below assume `--cmon-bin ~/fangorn/embeddings/cmon_venv/bin/cmon`.

---

## Use cases

Each command is one job that crawls **several domains at once** through the generic
extractor. Tune `--limit` up for real datasets; raise/lower `--since`/`--to` to pick
a crawl window.

### 🍳 Recipes across food sites  *(verified — rich `Recipe` JSON-LD)*

Search across cuisines/ingredients/time, e.g. *"quick vegetarian Italian pasta."*

```sh
quickbeam data crawl \
  --routes ./examples/cc/routes_generic.json \
  --extractors ./examples/cc/extractors/ \
  --url https://www.bbcgoodfood.com/recipes/ \
  --url https://www.simplyrecipes.com/recipes/ \
  --url https://www.seriouseats.com/ \
  --match-type prefix \
  --limit 300 \
  --out ./examples/cc/recipes.json \
  --cmon-bin ~/fangorn/embeddings/cmon_venv/bin/cmon
```

Yields fields like `title, description, ingredients, cuisine, recipeCategory,
totalTime, tags, author, image, text, url, domain`. (`bbcgoodfood.com` is verified
to return real `Recipe` pages; `allrecipes.com` is **bot-blocked in CC** and yields
nothing — test any new domain with `--limit 5` first.)

### 📰 News & opinion across outlets  *(historical snapshots are a feature here)*

Track a topic across publishers and over time — `NewsArticle` JSON-LD is widespread.

```sh
quickbeam data crawl \
  --routes ./examples/cc/routes_generic.json \
  --extractors ./examples/cc/extractors/ \
  --url https://www.theguardian.com/technology \
  --url https://www.npr.org/sections/technology \
  --url https://apnews.com/article/ \
  --match-type prefix \
  --limit 200 \
  --out ./examples/cc/news.json \
  --cmon-bin ~/fangorn/embeddings/cmon_venv/bin/cmon
```

### 💼 Job postings across boards

`JobPosting` JSON-LD on ATS hosts → search roles/skills/locations across employers.

```sh
quickbeam data crawl \
  --routes ./examples/cc/routes_generic.json \
  --extractors ./examples/cc/extractors/ \
  --url https://boards.greenhouse.io/ \
  --url https://jobs.lever.co/ \
  --match-type prefix \
  --limit 200 \
  --out ./examples/cc/jobs.json \
  --cmon-bin ~/fangorn/embeddings/cmon_venv/bin/cmon
```

### 🧑‍💻 Developer articles & blogs

Techniques across the dev ecosystem — `Article` / `BlogPosting`.

```sh
quickbeam data crawl \
  --routes ./examples/cc/routes_generic.json \
  --extractors ./examples/cc/extractors/ \
  --url https://dev.to/ \
  --url https://stackoverflow.blog/ \
  --match-type domain \
  --limit 200 \
  --out ./examples/cc/devblogs.json \
  --cmon-bin ~/fangorn/embeddings/cmon_venv/bin/cmon
```

### 🎬 Films & 📚 books

Plot/genre/theme search. Wikipedia has no JSON-LD but the generic extractor falls
back to title/description/main-text, which still embeds well.

```sh
quickbeam data crawl \
  --routes ./examples/cc/routes_generic.json \
  --extractors ./examples/cc/extractors/ \
  --url https://openlibrary.org/works/ \
  --url https://en.wikipedia.org/wiki/ \
  --match-type prefix \
  --limit 200 \
  --out ./examples/cc/media.json \
  --cmon-bin ~/fangorn/embeddings/cmon_venv/bin/cmon
```

---

## Tips

- **`--limit` is per-domain.** Each `--url` gets its own budget (quickbeam runs one
  `cmon download` per URL), so a bot-blocked or huge domain can't starve the others.
  The records you keep are usually **far fewer** than `--limit × domains`: blocked
  captures and URL-dedup drop a lot. Raise `--limit` (hundreds–thousands) for real
  datasets.
- **`--match-type`**: `prefix` to pin a path (individual items); `domain` for a whole
  site; `host` for one subdomain; `exact` for a single URL.
- **Test each prefix first** with `--limit 5` per domain — confirm it returns *content*
  pages (e.g. `Recipe`), not block stubs (empty) or `ItemList` category pages (narrow
  the prefix), before scaling up.
- **De-dup**: records are keyed by URL, so `http`/`https`/trailing-slash and repeat
  captures of the same page collapse to one. Expect duplicates in a crawl slice.
- **Which sites work?** Sites that serve real HTML to crawlers (most food blogs, news,
  docs, Wikipedia, ATS job boards). **Bot-protected sites are captured as empty/block
  stubs** in CC and yield nothing — e.g. `allrecipes.com` and most big retailers
  (DigiKey, Amazon) are blocked. The generic extractor drops block stubs and
  contentless pages by design, so they cost a little crawl time but no bad records.

---

## From a crawl to a searchable API

The offline `data crawl` writes JSON for inspection. To make it a live, searchable
**cross-domain API**, publish the records and embed them (see
[`../CC_Fangorn.md`](../CC_Fangorn.md) for the full flow):

```sh
# 1. publish the crawled records under one output schema
node src/publish.mjs --records ./examples/cc/recipes.json \
  --schema fangorn.webpage.v1 --dataset ds.recipes.demo \
  --schema-def schemas/webpage.json

# 2. embed + serve — now semantic search spans every crawled domain at once
quickbeam watch --bundle fangorn.webpage.v1=0x<schemaId>
quickbeam serve
```

Then query *"crispy weeknight tofu under 30 minutes"* and get the best matches from
**all** the sites you crawled, ranked by meaning rather than by source.
