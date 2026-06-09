import "dotenv/config";
import { PinataSDK } from "pinata";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

// Emulate __dirname inside ES modules
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

async function uploadSnapshot() {
  const jwt = process.env.PINATA_JWT;
  
  if (!jwt) {
    console.error("❌ Error: PINATA_JWT environment variable is missing.");
    process.exit(1);
  }

  // Initialize Pinata V3 SDK
  const pinata = new PinataSDK({
    pinataJwt: jwt,
  });

  // Target your snapshot file location
  const filePath = "/home/driemworks/.snapshot.gz"; 
  const fileName = "sond3r.snapshot.2026-06-08-17-26-31.1.gz";

  if (!fs.existsSync(filePath)) {
    console.error(`❌ Error: Snapshot file not found at ${filePath}`);
    process.exit(1);
  }

  try {
    console.log("⏳ Reading snapshot file into memory...");
    const fileBuffer = fs.readFileSync(filePath);
    
    console.log("📦 Creating Web API compliant File container...");
    const blob = new Blob([fileBuffer]);
    const file = new File([blob], fileName, { type: "application/gzip" });

    console.log("🚀 Uploading to Pinata network via V3 SDK processing engine...");
    const response = await pinata.upload.public.file(file);
    
    console.log("\n🎉 SUCCESS! Your file has processed completely.");
    console.log(`▶ CID:  ${response.cid}`);
    console.log(`▶ ID:   ${response.id}`);
    console.log(`▶ Link: https://gateway.pinata.cloud/ipfs/${response.cid}`);
    
  } catch (error) {
    console.error("\n❌ SDK Processing error:", error);
  }
}

uploadSnapshot();