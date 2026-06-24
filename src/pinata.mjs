#!/usr/bin/env node
/**
 * src/pinata.mjs — Pinata file management CLI
 *
 * Requires PINATA_JWT in environment (or a .env file at the project root).
 *
 * Commands:
 *   upload <file> [name]          Pin a local file to IPFS via Pinata
 *   list   [--name <prefix>]      List pinned files, optionally filtered by name
 *   delete <id...>                Delete one or more files by their Pinata file ID
 *   delete-pattern <prefix>       Bulk-delete every file whose name starts with <prefix>
 *   delete-all                    Delete every file in the account (prompts for confirmation)
 * 
 * e.g. PINATA_DELETE_RATE_PER_MIN=178 node src/pinata.mjs delete-pattern "manifest:bundle:"
 * 
 */

import "dotenv/config";
import { PinataSDK, getFileIdFromUrl } from "pinata";
import fs from "fs";
import { createInterface } from "readline";
import { program } from "commander";
import * as tus from "tus-js-client";

// Pinata uses standard uploads below 100 MB and requires resumable (TUS)
// uploads at or above it. Keep TUS chunks under Pinata's 50 MB ceiling.
const TUS_THRESHOLD = 50 * 1024 ** 2;
// Use large chunks (under Pinata's 50 MB ceiling) to minimize the number of
// PATCH requests per upload — Pinata's Cloudflare Durable Object backend returns
// 500 "Durable Object is overloaded" when chunks arrive too fast, and on that
// error it discards the partial, so fewer requests means fewer chances to fail.
const TUS_CHUNK_SIZE = 48 * 1024 ** 2;

// The Pinata SDK switches from a single multipart POST to the chunked/TUS
// Durable Object path above 90 MiB (94371840 bytes). That DO path is the one
// throwing 500s, so `upload-split` keeps every part under this so each one goes
// through the reliable single-POST endpoint. Leave a small safety margin.
const SINGLE_POST_MAX = 88 * 1024 ** 2;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeSdk() {
  const jwt = process.env.PINATA_JWT;
  if (!jwt) {
    console.error("Error: PINATA_JWT environment variable is not set.");
    process.exit(1);
  }
  return new PinataSDK({ pinataJwt: jwt });
}

async function confirm(question) {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  return new Promise(resolve => {
    rl.question(`${question} [y/N] `, answer => {
      rl.close();
      resolve(answer.trim().toLowerCase() === "y");
    });
  });
}

/** Paginate through all files matching an optional name prefix. */
async function* listAll(pinata, namePrefix) {
  let pageToken = undefined;
  while (true) {
    let q = pinata.files.public.list().limit(100);
    if (namePrefix) q = q.name(namePrefix);
    if (pageToken)  q = q.pageToken(pageToken);
    const page = await q;
    for (const f of (page.files ?? [])) yield f;
    if (!page.next_page_token) break;
    pageToken = page.next_page_token;
  }
}

// Pinata's SDK delete is latency-bound: passing an array of IDs makes it issue
// one DELETE per id SEQUENTIALLY with a hardcoded 300ms pause between each. So we
// instead fire single-id deletes in parallel chunks, throttled to stay under
// Pinata's ~180 req/min rate cap (which is the real throughput ceiling).
// Override with PINATA_DELETE_CONCURRENCY / PINATA_DELETE_RATE_PER_MIN.
const DELETE_CONCURRENCY  = parseInt(process.env.PINATA_DELETE_CONCURRENCY ?? "15", 10);
const DELETE_RATE_PER_MIN = parseInt(process.env.PINATA_DELETE_RATE_PER_MIN ?? "165", 10); // margin under 180

async function deleteBatch(pinata, ids) {
  // Each chunk of DELETE_CONCURRENCY must span at least this long to respect the
  // per-minute cap; we sleep only the remainder after the requests resolve.
  const minChunkMs = Math.ceil((DELETE_CONCURRENCY / DELETE_RATE_PER_MIN) * 60_000);
  let deleted = 0, failed = 0;

  for (let i = 0; i < ids.length; i += DELETE_CONCURRENCY) {
    const chunk = ids.slice(i, i + DELETE_CONCURRENCY);
    const t0 = Date.now();

    const results = await Promise.allSettled(
      chunk.map(id => pinata.files.public.delete([id]))
    );
    for (const r of results) {
      if (r.status === "fulfilled") deleted++;
      else { failed++; if (failed <= 10) console.error(`\n  delete failed: ${r.reason?.message ?? r.reason}`); }
    }
    process.stdout.write(`\r  deleted ${deleted}/${ids.length}${failed ? ` (${failed} failed)` : ""}   `);

    // Throttle to the rate cap (skip the wait after the final chunk).
    const elapsed = Date.now() - t0;
    if (i + DELETE_CONCURRENCY < ids.length && elapsed < minChunkMs) {
      await new Promise(r => setTimeout(r, minChunkMs - elapsed));
    }
  }
  if (ids.length) process.stdout.write("\n");
  if (failed) console.log(`  ${failed} deletion(s) failed (likely already deleted or transient rate-limit).`);
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

program
  .name("pinata")
  .description("Pinata file management CLI")
  .version("1.0.0");

// ── upload ──────────────────────────────────────────────────────────────────
program
  .command("upload <file> [name]")
  .description("Pin a local file to IPFS via Pinata")
  .action(async (filePath, name) => {
    if (!fs.existsSync(filePath)) {
      console.error(`Error: file not found: ${filePath}`);
      process.exit(1);
    }

    const jwt = process.env.PINATA_JWT;
    if (!jwt) {
      console.error("Error: PINATA_JWT environment variable is not set.");
      process.exit(1);
    }

    const pinata = makeSdk();
    const fileName = name ?? filePath.split("/").pop();
    const stats = fs.statSync(filePath);
    const gb = (stats.size / 1024 ** 3).toFixed(2);

    try {
      let id, cid;
      if (stats.size >= TUS_THRESHOLD) {
        console.log(`Resumable upload of ${filePath} (${gb} GB) to Pinata...`);
        ({ id, cid } = await tusUpload(pinata, jwt, fs.createReadStream(filePath), fileName, stats.size));
      } else {
        console.log(`Uploading ${filePath} (${gb} GB) as "${fileName}"...`);
        const buffer = fs.readFileSync(filePath);
        const res = await pinata.upload.public.file(new File([buffer], fileName));
        ({ id, cid } = res);
      }
      console.log(`\n✅ Success!`);
      console.log(`ID   : ${id}`);
      if (cid) {
        console.log(`CID  : ${cid}`);
        console.log(`Link : https://gateway.pinata.cloud/ipfs/${cid}`);
      } else {
        console.log(`CID  : (still indexing — run \`pinata list --name "${fileName}"\` shortly to get it)`);
      }
    } catch (error) {
      console.error("\n❌ Upload failed:", error.message ?? error);
      process.exit(1);
    }
  });

/**
 * Stream data to Pinata's resumable (TUS) endpoint. PATCH chunks under 50 MB
 * are uploaded with automatic retry/resume, so a dropped connection no longer
 * restarts the whole transfer. `source` is any Node readable stream and `size`
 * its exact byte length. Resolves with { id, cid }.
 *
 * The TUS completion itself is the source of truth for success. The CID is not
 * in the (empty) completion body and Pinata can take minutes to index the file,
 * so we make a brief best-effort lookup and leave cid null if it isn't ready —
 * the upload still succeeded and the CID can be fetched later with `list`.
 */
function tusUpload(pinata, jwt, source, fileName, size) {
  return new Promise((resolve, reject) => {
    let lastSent = 0, peak = 0;
    const upload = new tus.Upload(source, {
      endpoint: "https://uploads.pinata.cloud/v3/files",
      chunkSize: TUS_CHUNK_SIZE,
      uploadSize: size, // required when the input is a stream
      // Keep delays short: if a retry waits too long, Pinata expires the partial
      // upload and the resume HEAD returns offset 0, forcing a restart from zero.
      retryDelays: [0, 2000, 5000, 10000],
      headers: { Authorization: `Bearer ${jwt}` },
      metadata: { filename: fileName, network: "public" },
      onError: reject,
      // Log why each retry happened so the real failure (timeout / 5xx / reset)
      // is visible instead of just the symptom of restarting.
      onShouldRetry: (err, attempt) => {
        const status = err?.originalResponse?.getStatus?.() ?? 0;
        process.stdout.write(`\n  ↻ retry #${attempt + 1}: status=${status} ${err?.message ?? err}\n`);
        if (status >= 400 && status < 500 && ![408, 409, 423, 429].includes(status)) return false;
        return attempt < 4;
      },
      onProgress: (sent, total) => {
        // Fire once at the moment of a backward jump (a true restart), rather
        // than on every event while below the previous peak.
        if (sent + TUS_CHUNK_SIZE < lastSent) {
          process.stdout.write(
            `\n  ⚠ restarted at ${(sent / 1024 ** 3).toFixed(2)} GB ` +
            `(peak was ${(peak / 1024 ** 3).toFixed(2)} GB) — Pinata dropped the partial upload\n`
          );
        }
        lastSent = sent;
        peak = Math.max(peak, sent);
        const pct = ((sent / total) * 100).toFixed(1);
        process.stdout.write(`\r  ${pct}%  (${(sent / 1024 ** 3).toFixed(2)} GB uploaded)   `);
      },
      onSuccess: async () => {
        const id = getFileIdFromUrl(upload.url);
        resolve({ id, cid: await resolveCid(pinata, id) });
      },
    });
    upload.start();
  });
}

/**
 * Best-effort CID lookup for a just-completed TUS upload. Pinata can take
 * minutes to index the file, so we try briefly and return null if it isn't
 * ready yet — callers report success regardless and point the user at `list`.
 */
async function resolveCid(pinata, id) {
  for (const delay of [0, 2000, 4000, 6000]) {
    if (delay) await new Promise(r => setTimeout(r, delay));
    try {
      const item = await pinata.files.public.get(id);
      if (item?.cid) return item.cid;
    } catch { /* not indexed yet */ }
  }
  return null;
}

/** Read a byte range [start, start+length) of a file into a Buffer. */
function readRange(filePath, start, length) {
  const buf = Buffer.alloc(length);
  const fd = fs.openSync(filePath, "r");
  try {
    let pos = 0;
    while (pos < length) {
      const n = fs.readSync(fd, buf, pos, length - pos, start + pos);
      if (n === 0) break;
      pos += n;
    }
  } finally {
    fs.closeSync(fd);
  }
  return buf;
}

// ── upload-split ──────────────────────────────────────────────────────────────
program
  .command("upload-split <file> [name]")
  .description("Split a file into parts uploaded via Pinata's reliable single-POST endpoint")
  .option("-s, --part-size <mb>", "Max size of each part in MB (capped at 88)", "88")
  .option("-r, --retries <n>", "Attempts per part before giving up", "8")
  .action(async (filePath, name, opts) => {
    if (!fs.existsSync(filePath)) {
      console.error(`Error: file not found: ${filePath}`);
      process.exit(1);
    }

    const jwt = process.env.PINATA_JWT;
    if (!jwt) {
      console.error("Error: PINATA_JWT environment variable is not set.");
      process.exit(1);
    }

    const pinata = makeSdk();
    let partSize = Math.floor(parseFloat(opts.partSize) * 1024 ** 2);
    if (!Number.isFinite(partSize) || partSize <= 0) {
      console.error(`Error: invalid --part-size: ${opts.partSize}`);
      process.exit(1);
    }
    if (partSize > SINGLE_POST_MAX) {
      console.log(`Capping part size to 88 MB so each part uses Pinata's reliable single-POST upload.`);
      partSize = SINGLE_POST_MAX;
    }
    const maxRetries = Math.max(1, parseInt(opts.retries, 10) || 1);

    const baseName = name ?? filePath.split("/").pop();
    const totalSize = fs.statSync(filePath).size;
    const partCount = Math.ceil(totalSize / partSize);
    const width = String(partCount - 1).length;
    const manifestPath = `${baseName}.manifest.json`;

    // Resume: reuse parts already recorded in an existing manifest so reruns
    // skip completed work. Pinata's TUS backend drops partials on its frequent
    // 500s, so making forward progress across reruns is what gets a big file up.
    const done = new Map(); // index -> part record
    if (fs.existsSync(manifestPath)) {
      try {
        for (const p of JSON.parse(fs.readFileSync(manifestPath, "utf8")).parts ?? []) {
          if (p.id) done.set(p.index, p);
        }
      } catch { /* ignore a corrupt manifest and re-upload */ }
    }

    console.log(
      `Splitting ${filePath} (${(totalSize / 1024 ** 3).toFixed(2)} GB) into ${partCount} part(s) ` +
      `of up to ${(partSize / 1024 ** 2).toFixed(0)} MB each` +
      (done.size ? `; resuming (${done.size} already done)` : "") + "..."
    );

    const manifest = {
      name: baseName,
      total_size: totalSize,
      part_size: partSize,
      parts: [...done.values()].sort((a, b) => a.index - b.index),
      created: new Date().toISOString(),
    };
    const save = () => fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));

    for (let i = 0; i < partCount; i++) {
      const partName = `${baseName}.part${String(i).padStart(width, "0")}`;
      if (done.has(i)) {
        console.log(`\n[${i + 1}/${partCount}] ${partName} — already uploaded, skipping`);
        continue;
      }

      const start = i * partSize;
      const length = Math.min(partSize, totalSize - start);
      process.stdout.write(`[${i + 1}/${partCount}] ${partName} (${(length / 1024 ** 2).toFixed(0)} MB)... `);

      let lastErr;
      for (let attempt = 1; attempt <= maxRetries; attempt++) {
        try {
          const buffer = readRange(filePath, start, length);
          const res = await pinata.upload.public.file(new File([buffer], partName));
          manifest.parts.push({ index: i, name: partName, id: res.id, cid: res.cid, size: length });
          manifest.parts.sort((a, b) => a.index - b.index);
          save(); // persist after every part so progress survives a crash
          console.log(`✅ ${res.cid}`);
          lastErr = null;
          break;
        } catch (error) {
          lastErr = error;
          const wait = Math.min(30000, 2000 * 2 ** (attempt - 1));
          console.error(`\n  attempt ${attempt}/${maxRetries} failed: ${error.message ?? error}`);
          if (attempt < maxRetries) {
            console.error(`  retrying part in ${wait / 1000}s...`);
            await new Promise(r => setTimeout(r, wait));
          }
        }
      }

      if (lastErr) {
        console.error(`\n❌ Gave up on ${partName} after ${maxRetries} attempts.`);
        console.error(`Progress saved to ${manifestPath} — re-run the same command to resume.`);
        process.exit(1);
      }
    }

    console.log(`\n✅ All ${partCount} part(s) uploaded. Manifest: ${manifestPath}`);
    console.log("\nTo reassemble after downloading every part by CID, in part order:");
    console.log(`  cat ${baseName}.part* > ${baseName}`);
  });

// ── list ────────────────────────────────────────────────────────────────────
program
  .command("list")
  .description("List pinned files")
  .option("-n, --name <prefix>", "Filter to files whose name contains this string")
  .option("-l, --limit <n>",     "Stop after N results (0 = all)", "0")
  .action(async (opts) => {
    const pinata = makeSdk();
    const max    = parseInt(opts.limit, 10);
    let   count  = 0;
    const rows   = [];

    for await (const f of listAll(pinata, opts.name)) {
      rows.push(f);
      count++;
      if (max > 0 && count >= max) break;
    }

    if (!rows.length) {
      console.log("No files found.");
      return;
    }

    // Column-align output
    const nameWidth = Math.min(48, Math.max(...rows.map(r => (r.name ?? "").length)));
    console.log(
      `${"ID".padEnd(36)}  ${"NAME".padEnd(nameWidth)}  ${"CID".padEnd(59)}  SIZE`
    );
    console.log("-".repeat(36 + nameWidth + 59 + 10));
    for (const f of rows) {
      const name = (f.name ?? "").padEnd(nameWidth);
      const size = f.size != null ? `${(f.size / 1024).toFixed(1)} KB` : "?";
      console.log(`${f.id}  ${name}  ${f.cid}  ${size}`);
    }
    console.log(`\n${rows.length} file(s)${max > 0 && count >= max ? " (limit reached)" : ""}`);
  });

// ── delete ──────────────────────────────────────────────────────────────────
program
  .command("delete <id...>")
  .description("Delete one or more files by Pinata file ID")
  .action(async (ids) => {
    const pinata = makeSdk();
    console.log(`Deleting ${ids.length} file(s)...`);
    await deleteBatch(pinata, ids);
    console.log("Done.");
  });

// ── delete-pattern ───────────────────────────────────────────────────────────
program
  .command("delete-pattern <prefix>")
  .description("Bulk-delete every file whose name contains <prefix>")
  .option("-y, --yes", "Skip confirmation prompt")
  .action(async (prefix, opts) => {
    const pinata = makeSdk();

    console.log(`Listing files matching "${prefix}"...`);
    const ids = [];
    for await (const f of listAll(pinata, prefix)) {
      ids.push(f.id);
      process.stdout.write(`\r  found ${ids.length} file(s)`);
    }
    process.stdout.write("\n");

    if (!ids.length) {
      console.log("No matching files found.");
      return;
    }

    if (!opts.yes) {
      const ok = await confirm(`Delete ${ids.length} file(s) matching "${prefix}"?`);
      if (!ok) { console.log("Aborted."); return; }
    }

    await deleteBatch(pinata, ids);
    console.log(`Deleted ${ids.length} file(s).`);
  });

// ── delete-all ───────────────────────────────────────────────────────────────
program
  .command("delete-all")
  .description("Delete every file in the Pinata account")
  .option("-y, --yes", "Skip confirmation prompt")
  .action(async (opts) => {
    const pinata = makeSdk();

    console.log("Listing all files...");
    const ids = [];
    for await (const f of listAll(pinata)) {
      ids.push(f.id);
      process.stdout.write(`\r  found ${ids.length} file(s)`);
    }
    process.stdout.write("\n");

    if (!ids.length) {
      console.log("No files found.");
      return;
    }

    if (!opts.yes) {
      const ok = await confirm(
        `This will permanently delete ALL ${ids.length} file(s) from your Pinata account. Continue?`
      );
      if (!ok) { console.log("Aborted."); return; }
    }

    await deleteBatch(pinata, ids);
    console.log(`Deleted ${ids.length} file(s).`);
  });

program.parse();
