1) clear storage
2) re-upload
pnpm dotenvx run -f .env -- tsx src/test/publish_bundle.ts   --input-dir /home/driemworks/fangorn/embeddings/stage_volumes   --shard-roots 200000 --root-type Recording 

then rebuild embeddings (small set)
