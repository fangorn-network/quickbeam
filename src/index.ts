#!/usr/bin/env npx tsx
/**
 * publish_embeddings.ts
 *
 * Builds and publishes an embeddings snapshot manifest to Fangorn.
 *
 * Usage:
 *   npx tsx publish_embeddings.ts \
 *     --bundle-cid QmYourBundleCID \
 *     --embeddings-cid QmYourEmbeddingsCID \
 *     --block 19284710 \
 *     --total 1000000 \
 *     --schema sond3r.embeddings.0 \
 *     --dataset sond3r.embeddings.snapshot.1 \
 *     --source "test.sond3r.track.invariants.3=0xc4103f...=19280001" \
 *     --source "test.sond3r.track.taxonomy.2=0x382fda...=19279843"
 */

import { Command } from "commander";
import { writeFileSync, unlinkSync } from "fs";
import { join } from "path";
import { tmpdir } from "os";
import { execSync } from "child_process";

interface SourceSchema {
    name: string;       
    schemaId: string;
    latestBlock: number;
}

const program = new Command();

program
    .name("publish_embeddings")
    .description("Publish an embeddings snapshot manifest to Fangorn")
    .requiredOption("--bundle-cid <cid>",       "IPFS CID of the full bundle.ndjson (fields + embeddings)")
    .requiredOption("--embeddings-cid <cid>",    "IPFS CID of the embeddings-only NDJSON (trackId + vector)")
    .requiredOption("--block <number>",          "Block height at time of export", parseInt)
    .requiredOption("--total <number>",          "Total track count in the bundle", parseInt)
    .requiredOption("--schema <name>",           "Fangorn schema name, e.g. sond3r.embeddings.0")
    .requiredOption("--dataset <name>",          "Dataset name, e.g. sond3r.embeddings.snapshot.1")
    .option(
        "--source <spec>",
        "Source schema in format name=schemaId=latestBlock. Repeatable.",
        (val: string, acc: string[]) => { acc.push(val); return acc; },
        [] as string[],
    )
    .option("--model <name>",      "Embedding model name", "nomic-ai/nomic-embed-text-v1.5")
    .option("--dimensions <n>",    "Embedding dimensions", parseInt, 768)
    .option("--dry-run",           "Print the record without publishing", false);

program.parse();
const opts = program.opts<{
    bundleCid:      string;
    embeddingsCid:  string;
    block:          number;
    total:          number;
    schema:         string;
    dataset:        string;
    source:         string[];
    model:          string;
    dimensions:     number;
    dryRun:         boolean;
}>();

// Parse --source name=schemaId=latestBlock
const sourceSchemas: SourceSchema[] = opts.source.map((s: any) => {
    const parts = s.split("=");
    if (parts.length !== 3) {
        console.error(`Invalid --source format: "${s}". Expected name=schemaId=latestBlock`);
        process.exit(1);
    }
    return {
        name:        parts[0],
        schemaId:    parts[1],
        latestBlock: parseInt(parts[2], 10),
    };
});

if (sourceSchemas.length === 0) {
    console.warn("Warning: no --source schemas provided. The snapshot will have no provenance.");
}

// Build the single PublishRecord
const record = {
    name: opts.dataset,
    fields: {
        model:          opts.model,
        dimensions:     opts.dimensions,
        createdAtBlock: opts.block,
        sourceSchemas,
        dataCid:        opts.bundleCid,
        embeddingsCid:  opts.embeddingsCid,
        totalCount:     opts.total,
    },
};

console.log("\nRecord to publish:");
console.log(JSON.stringify(record, null, 2));

if (opts.dryRun) {
    console.log("\n--dry-run set. Exiting without publishing.");
    process.exit(0);
}

// Write to a temp file and call the fangorn CLI
const tmpFile = join(tmpdir(), `fangorn_embeddings_${Date.now()}.json`);
writeFileSync(tmpFile, JSON.stringify([record], null, 2), "utf-8");

try {
    console.log(`\nPublishing to schema "${opts.schema}", dataset "${opts.dataset}"...`);
    const cmd = `fangorn publish upload ${tmpFile} -s ${opts.schema} -d ${opts.dataset}`;
    console.log(`Running: ${cmd}\n`);
    execSync(cmd, { stdio: "inherit" });
} catch (err) {
    console.error("Publish failed:", (err as Error).message);
    process.exit(1);
} finally {
    unlinkSync(tmpFile);
}