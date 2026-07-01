 quickbeam data osm --place wi--eagle-river --volume 3 --output-dir $STAGE
🌍 Geocoding: wi--eagle-river
📦 bbox (W,S,E,N) = (-89.284401, 45.8925538, -89.224724, 45.947188)
🔎 food & drink → Business
   +21 business nodes
🔎 lodging → Business
   +5 business nodes
🔎 recreation → Business
   +5 business nodes
🔎 shops → Business
   +52 business nodes
🔎 services → Business
🖼  resolving 1 wikidata images…
   ✅ 0 images resolved
   +18 business nodes
🔎 trails → Trail
   +3 trail nodes
🔎 water bodies → Lake
   +9 lake nodes
🔎 natural features → Landmark
   +0 landmark nodes
🔎 outdoors → Landmark
🖼  resolving 1 wikidata images…
   ✅ 1 images resolved
   +6 landmark nodes
🔎 attractions → Landmark
   +4 landmark nodes
🔎 historic → Landmark
   +0 landmark nodes

   ✅ Business : 101 → volume_3_osm_businesses.json
   ✅ Trail    : 3 → volume_3_osm_trails.json
   ✅ Lake     : 9 → volume_3_osm_lakes.json
   ✅ Landmark : 10 → volume_3_osm_landmarks.json
   ✅ Category : 66 → volume_3_osm_categories.json
   ✅ Locality : 3 → volume_3_osm_localities.json
   ✅ edges    : 258

✅ Wrote 192 OSM nodes → /home/driemworks/fangorn/embeddings/stage_volumes/volume_3_osm_*.json
📊 Next: quickbeam data schemagen --volume 0 --prefix fangorn.local --bundle-name localcore
(venv) driemworks@DESKTOP-RN9BJOQ:~/fangorn/embeddings$ quickbeam data schemagen --input-dir $STAGE --volume 3 \
  --prefix eagleriver.sond3r.com.osm --bundle-name placecore --version v2
🔎 Inferring schemas from 6 node file(s)...
   ✅ Business       → eagleriver.sond3r.com.osm.business.v2  (14 fields, sampled 101)  [identity @id=osmId; aliases osm:osmId]
   ✅ Category       → eagleriver.sond3r.com.osm.category.v2  (6 fields, sampled 66)
   ✅ Lake           → eagleriver.sond3r.com.osm.lake.v2  (10 fields, sampled 9)  [identity @id=osmId; aliases osm:osmId]
   ✅ Landmark       → eagleriver.sond3r.com.osm.landmark.v2  (13 fields, sampled 10)  [identity @id=osmId; aliases osm:osmId]
   ✅ Locality       → eagleriver.sond3r.com.osm.locality.v2  (6 fields, sampled 3)
   ✅ Trail          → eagleriver.sond3r.com.osm.trail.v2  (13 fields, sampled 3)  [identity @id=osmId; aliases osm:osmId]

🔗 Inferring bundle edges from 1 edges file(s)...
   ✅ inCategory               Business → Category  (113 observed)
   ✅ locatedIn                Business → Locality  (101 observed)
   ✅ locatedIn                Landmark → Locality  (10 observed)
   ✅ inCategory               Landmark → Category  (10 observed)
   ✅ locatedIn                Lake → Locality  (9 observed)
   ✅ inCategory               Lake → Category  (9 observed)
   ✅ locatedIn                Trail → Locality  (3 observed)
   ✅ inCategory               Trail → Category  (3 observed)

📦 Wrote 6 node schema(s) + bundle 'eagleriver.sond3r.com.osm.placecore.v2' (8 edge shapes) → ./stage_volumes/schemas/
   Register with the Fangorn SDK in this order: node schemas first, then the bundle.
(venv) driemworks@DESKTOP-RN9BJOQ:~/fangorn/embeddings$ quickbeam data events-fetch --source tribe --site https://eagleriver.org \
  --no-db --raw-out tribe_events.jsonl
🔎 Tribe calendar https://eagleriver.org/wp-json/tribe/events/v1/events
   page 1/42: +50 (have 50)
   page 2/42: +50 (have 100)
   page 3/42: +50 (have 150)
   page 4/42: +50 (have 200)
   page 5/42: +50 (have 250)
   page 6/42: +50 (have 300)
   page 7/42: +50 (have 350)
   page 8/42: +50 (have 400)
   page 9/42: +50 (have 450)
   page 10/42: +50 (have 500)
   collected 500 event row(s)
📄 Raw JSONL → tribe_events.jsonl

✅ Saved 500 events to tribe_events.jsonl (10 HTTP requests, source=tribe).
(venv) driemworks@DESKTOP-RN9BJOQ:~/fangorn/embeddings$ quickbeam data eventspg --raw-in tribe_events.jsonl --volume 4 --output-dir $STAGE
🔗 Business index: 263 from volume_1_businesses.json
📄 Source: tribe_events.jsonl
   ✅ Event    : 1,000 → volume_4_events.json
   ✅ Organizer: 55 → volume_4_organizers.json
   ✅ Category : 7 → volume_4_event_categories.json
   ✅ Locality : 14 → volume_4_event_localities.json
   ✅ edges    : 4,213
   🔗 hostedAt : 312 event(s) linked to a Business (of 263 businesses)

📊 Done. Next: quickbeam data schemagen --input-dir ./stage_volumes --prefix fangorn.places --bundle-name localcore --version v1
(venv) driemworks@DESKTOP-RN9BJOQ:~/fangorn/embeddings$ quickbeam data schemagen --input-dir $STAGE --volume 4 \
  --prefix eagleriver.sond3r.com.evt --bundle-name eventcore --version v2
🔎 Inferring schemas from 4 node file(s)...
   ✅ Category       → eagleriver.sond3r.com.evt.category.v2  (5 fields, sampled 7)
   ✅ Locality       → eagleriver.sond3r.com.evt.locality.v2  (6 fields, sampled 14)
   ✅ Event          → eagleriver.sond3r.com.evt.event.v2  (26 fields, sampled 1,000)
   ✅ Organizer      → eagleriver.sond3r.com.evt.organizer.v2  (7 fields, sampled 55)

🔗 Inferring bundle edges from 1 edges file(s)...
   ✅ inCategory               Event → Category  (2,057 observed)
   ✅ hostedBy                 Event → Organizer  (910 observed)
   ✅ locatedIn                Event → Locality  (622 observed)
   ⚠️  edge hostedAt Event→Business: endpoint type not among node schemas, skipping
   ⚠️  edge hostsEvent Business→Event: endpoint type not among node schemas, skipping

📦 Wrote 4 node schema(s) + bundle 'eagleriver.sond3r.com.evt.eventcore.v2' (3 edge shapes) → ./stage_volumes/schemas/
   Register with the Fangorn SDK in this order: node schemas first, then the bundle.
(venv) driemworks@DESKTOP-RN9BJOQ:~/fangorn/embeddings$ quickbeam build --view "eagleriver.sond3r.com.localview.v2=0xda07418a717d82f34adb7df7be8df13f97bb5c22d83099c5ca330c86bf6ec103" $BUIL
D_AUTH     --profiles-file ~/fangorn/embeddings/osm_profiles.json     --root-profile business 
--root-profile review --root-profile localevent     --root-profile lake --root-profile trail -
-root-profile landmark --reset

[Builder] View mode: 'eagleriver.sond3r.com.localview.v2' — projections: business→Business, review→Review, localevent→Event, lake→Lake, trail→Trail, landmark→Landmark
[Builder] Resetting collection 'fangorn'...

[View] Resolving view schema 0xda07418a717d82f34adb7df7be8df13f97bb5c22d83099c5ca330c86bf6ec103...
  ↳ View Manifest: 100%|████████████████████████████████████| 1/1 [00:00<00:00,  1.30 file/s]
[View] fusing 2 source(s) + 1 linkset(s)
  ↳ view schema hint: resolved 3/3 source(s) via 3 per-schema query(ies)
  ↳   Src 0x307629a8: 100%|█████████████████████████████████| 1/1 [00:00<00:00,  1.07 file/s]
  ↳   Chunks: 100%|█████████████████████████████████████████| 7/7 [00:01<00:00,  4.74 file/s]
  ↳   Src 0x1f808790: 100%|█████████████████████████████████| 1/1 [00:00<00:00,  1.63 file/s]
  ↳   Chunks: 100%|███████████████████████████████████████| 33/33 [00:02<00:00, 11.03 file/s]
  ↳   Link 0xf29ada69: 100%|████████████████████████████████| 1/1 [00:00<00:00,  1.28 file/s]
  ↳   Links: 100%|██████████████████████████████████████████| 1/1 [00:01<00:00,  1.42s/ file]
[View] applied 50 link(s) (50 sameAs); skipped 0.
[View] fused 2776 nodes → 1685 entities.
[index] created fields.title
[index] created fields.byArtist
[index] created owner
[index] created entityType
[index] created fields.rating
[index] created fields.priceLevel
[index] created fields.amenities
[index] created fields.categories
[index] created fields.locality
[index] created fields.source
[index] created fields.isPast
[index] created fields.hostBusinessId
[Builder] model dim=768, output dim=256 (truncate=True)
[Builder] Manifest 1: bafkreidlopvjs2x... — 456 records
  ↳ Embedding: 100%|████████████████████████████████████| 456/456 [00:04<00:00, 104.93 doc/s]
[index] created fields.title
[index] created fields.byArtist
[index] created owner
[index] created entityType
[index] created fields.rating
[index] created fields.priceLevel
[index] created fields.amenities
[index] created fields.categories
[index] created fields.locality
[index] created fields.source
[index] created fields.isPast
[index] created fields.hostBusinessId

[Builder] All tasks complete.
(venv) driemworks@DESKTOP-RN9BJOQ:~/fangorn/embeddings$ quickbeam cdn bake --collection fangorn --domain places --cdn-dir ./cdn
[cdn] connecting to local Qdrant: localhost:6333
[bake] domain 'places': scrolling fangorn ...
[bake]   places: 282 points...
[bake]   warning: bundle_schema not found: 'stage_volumes/schemas/fangorn.places.eagleriver-localcore.0.json' (skipping)
[bake] domain 'places': 282 points in 1 shard(s), 1.5 MB; types=['Business', 'Event', 'Landmark', 'Lake', 'Trail']; title<-'title' tags<-['categories']
[bake] catalog written: ./cdn/catalog.json (3 domain(s))
(venv) driemworks@DESKTOP-RN9BJOQ:~/fangorn/embeddings$ quickbeam cdn serve --cdn-dir ./cdn --port 8090 --cors
[serve] Semantic CDN on http://0.0.0.0:8090 (dir: /home/driemworks/fangorn/embeddings/cdn)
INFO:     Started server process [422821]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8090 (Press CTRL+C to quit)
INFO:     127.0.0.1:39906 - "GET /domains/places/manifest HTTP/1.1" 200 OK
INFO:     127.0.0.1:39906 - "GET /domains/places/shards/shard-0000-a4a1859da877.ndjson.gz HTTP/1.1" 200 OK
INFO:     127.0.0.1:58280 - "GET /domains/places/manifest HTTP/1.1" 200 OK
INFO:     127.0.0.1:58280 - "GET /domains/places/shards/shard-0000-a4a1859da877.ndjson.gz HTTP/1.1" 200 OK
^CINFO:     Shutting down
INFO:     Waiting for application shutdown.
INFO:     Application shutdown complete.
INFO:     Finished server process [422821]
(venv) driemworks@DESKTOP-RN9BJOQ:~/fangorn/embeddings$ quickbeam cdn serve --cdn-dir ./cdn --port 8090 --cors
[serve] Semantic CDN on http://0.0.0.0:8090 (dir: /home/driemworks/fangorn/embeddings/cdn)
INFO:     Started server process [457246]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8090 (Press CTRL+C to quit)
INFO:     127.0.0.1:40134 - "GET /domains/places/manifest HTTP/1.1" 200 OK
INFO:     127.0.0.1:44844 - "GET /domains/places/manifest HTTP/1.1" 200 OK
^CINFO:     Shutting down
INFO:     Waiting for application shutdown.
INFO:     Application shutdown complete.
INFO:     Finished server process [457246]