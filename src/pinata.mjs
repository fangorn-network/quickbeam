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
 */

import "dotenv/config";
import { PinataSDK } from "pinata";
import fs from "fs";
import { createInterface } from "readline";
import { program } from "commander";

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

/** Delete files in batches of up to 100 IDs (Pinata's documented limit). */
async function deleteBatch(pinata, ids) {
  const BATCH = 100;
  let deleted = 0;
  for (let i = 0; i < ids.length; i += BATCH) {
    const slice = ids.slice(i, i + BATCH);
    await pinata.files.public.delete(slice);
    deleted += slice.length;
    process.stdout.write(`\r  deleted ${deleted}/${ids.length} files`);
  }
  if (ids.length) process.stdout.write("\n");
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
    const pinata   = makeSdk();
    const fileName = name ?? filePath.split("/").pop();
    const buffer   = fs.readFileSync(filePath);
    const file     = new File([new Blob([buffer])], fileName);

    console.log(`Uploading ${filePath} as "${fileName}"...`);
    const res = await pinata.upload.public.file(file);
    console.log(`CID  : ${res.cid}`);
    console.log(`ID   : ${res.id}`);
    console.log(`Link : https://gateway.pinata.cloud/ipfs/${res.cid}`);
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
