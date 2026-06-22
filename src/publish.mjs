#!/usr/bin/env node
/**
 * src/publish.mjs — publish Fangorn `{name, fields}` records via the SDK.
 *
 * Thin, scriptable bridge over @fangorn-network/sdk used by the Python scraper
 * service (quickbeam/fangorn_publish.py) as a subprocess, and runnable by hand.
 * It (1) optionally registers the target schema (idempotent — the SDK no-ops if
 * it already exists), then (2) publishes the records as a dataset, printing the
 * resulting manifest URI.
 *
 * The default network config (FangornConfig.ArbitrumSepolia) already carries the
 * live SchemaRegistry / DataSourceRegistry / SettlementRegistry addresses, so no
 * contract addresses are needed here.
 *
 * Env:
 *   FANGORN_PRIVATE_KEY  (required)  signer for register/publish txs
 *   PINATA_JWT           (required)  IPFS pinning for manifest + chunks
 *   PINATA_GATEWAY       (optional)  e.g. your-gw.mypinata.cloud
 *   RPC_URL              (optional)  override the default Arbitrum Sepolia RPC
 *
 * Usage:
 *   node src/publish.mjs --records recs.jsonl --schema fangorn.webpage.v1 \
 *     --dataset ds.hotwheels.2026 [--schema-def schema.json] [--chunk-size 1000]
 *
 * --records   JSONL ('{"name","fields"}' per line) or a JSON array; '-' = stdin.
 * --schema-def  optional resolver SchemaDefinition JSON to register first.
 * --register-only  register the schema and exit (no publish).
 *
 * On success prints a final line:  __FANGORN_RESULT__ {"manifestUri": "...", ...}
 */

import { readFileSync } from "node:fs";
import { program } from "commander";
import { Fangorn, FangornConfig } from "@fangorn-network/sdk";

const RESULT_MARKER = "__FANGORN_RESULT__";

function die(msg) {
  console.error(`[publish] ${msg}`);
  process.exit(1);
}

function readInput(path) {
  return path === "-" ? readFileSync(0, "utf8") : readFileSync(path, "utf8");
}

/** Parse records from JSONL or a top-level JSON array. */
function parseRecords(raw) {
  const text = raw.trim();
  if (!text) return [];
  if (text[0] === "[") {
    const arr = JSON.parse(text);
    if (!Array.isArray(arr)) die("--records JSON must be an array");
    return arr;
  }
  return text
    .split("\n")
    .map(l => l.trim())
    .filter(Boolean)
    .map((l, i) => {
      try { return JSON.parse(l); }
      catch (e) { die(`bad JSONL on line ${i + 1}: ${e.message}`); }
    });
}

function validateRecords(records) {
  for (const r of records) {
    if (!r || typeof r !== "object" || typeof r.name !== "string" || typeof r.fields !== "object") {
      die(`every record must be { name: string, fields: object }; got ${JSON.stringify(r)?.slice(0, 120)}`);
    }
  }
}

program
  .requiredOption("--schema <name>", "Schema name to publish under")
  .option("--records <path>", "JSONL or JSON-array file of {name,fields} records ('-' for stdin)", "-")
  .option("--dataset <name>", "Dataset name (default: ds.<schema>.<timestamp>)")
  .option("--schema-def <path>", "Resolver SchemaDefinition JSON to register (idempotent)")
  .option("--register-only", "Register the schema and exit without publishing")
  .option("--chunk-size <n>", "Records per chunk", "1000")
  .parse();

const opts = program.opts();

async function main() {
  const pk = process.env.FANGORN_PRIVATE_KEY;
  const pinataJwt = process.env.PINATA_JWT;
  if (!pk) die("FANGORN_PRIVATE_KEY is required");
  if (!pinataJwt) die("PINATA_JWT is required");

  const config = FangornConfig.ArbitrumSepolia;
  if (process.env.RPC_URL) config.rpcUrl = process.env.RPC_URL;

  const fangorn = Fangorn.create({
    privateKey: pk,
    config,
    storage: { pinata: { jwt: pinataJwt, gateway: process.env.PINATA_GATEWAY } },
  });

  // ── optional schema registration (idempotent) ──────────────────────────────
  if (opts.schemaDef) {
    const def = JSON.parse(readFileSync(opts.schemaDef, "utf8"));
    const definition = def.definition ?? def;        // accept {definition,types} or a bare def
    const types = def.types;
    console.error(`[publish] registering schema ${opts.schema} (idempotent)…`);
    const reg = await fangorn.schema.register({ name: opts.schema, definition, types });
    console.error(`[publish] schema id: ${reg.schemaId}`);
    if (opts.registerOnly) {
      process.stdout.write(`${RESULT_MARKER} ${JSON.stringify({ schemaId: reg.schemaId, name: opts.schema })}\n`);
      return;
    }
  } else if (opts.registerOnly) {
    die("--register-only requires --schema-def");
  }

  // ── publish records ─────────────────────────────────────────────────────────
  const records = parseRecords(readInput(opts.records));
  validateRecords(records);
  if (records.length === 0) die("no records to publish");

  const dataset = opts.dataset ?? `ds.${opts.schema}.${Date.now()}`;
  console.error(`[publish] publishing ${records.length} record(s) → schema=${opts.schema} dataset=${dataset}`);

  const result = await fangorn.publisher.publishRecords({
    records,
    schemaName: opts.schema,
    datasetName: dataset,
    chunkSize: parseInt(opts.chunkSize, 10),
  });

  console.error(`[publish] ✓ manifest: ${result.manifestUri} (${result.entryCount} entries)`);
  process.stdout.write(`${RESULT_MARKER} ${JSON.stringify({
    manifestUri: result.manifestUri,
    schemaId: result.schemaId,
    dataset,
    entryCount: result.entryCount,
  })}\n`);
}

main().catch(err => {
  console.error("[publish] failed:", err?.stack || err);
  process.exit(1);
});
