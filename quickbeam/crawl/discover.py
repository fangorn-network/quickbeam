#!/usr/bin/env python3
import urllib.request
import urllib.parse
import json
import sys
import argparse
import re

def normalize_url(url):
    """Strips tracking bloat, telemetry parameters, and standardizes structure."""
    url = re.sub(re.compile(r';jsessionid=[^?#]+', re.IGNORECASE), '', url)
    parsed = urllib.parse.urlparse(url)
    if not parsed.netloc:
        return None
        
    query_params = urllib.parse.parse_qsl(parsed.query)
    clean_params = [
        (k, v) for k, v in query_params 
        if not k.lower().startswith('utm_') 
        and k.lower() not in ['ref', '_bhlid', 'campaign', 'source', 'medium']
    ]
    
    clean_query = urllib.parse.urlencode(clean_params) if clean_params else ''
    host = parsed.netloc.lower()
    path = parsed.path.rstrip('/')
    
    return urllib.parse.urlunparse((parsed.scheme, host, path, parsed.params, clean_query, parsed.fragment))

def is_noise(url):
    """Aggressively eliminates binary downloads and platform system pages."""
    path = urllib.parse.urlparse(url).path.lower()
    if any(path.endswith(ext) for ext in ['.pdf', '.png', '.jpg', '.jpeg', '.css', '.js', '.ico', '.zip', '.docx']):
        return True
    
    noise_patterns = [r'/help/', r'/api/', r'/accessibility', r'/contact', r'/auth', r'/login', r'/404']
    return any(re.search(pattern, path) for pattern in noise_patterns)

def harvest_domain_paths(collection, domain, limit):
    """Hits the CDX registry for a specific domain and extracts matching paths."""
    base_url = f"https://index.commoncrawl.org/{collection}-index"
    params = {
        "url": f"{domain}/",
        "matchType": "prefix",
        "output": "json",
        "limit": str(limit),
        "status": "200"
    }
    
    full_url = f"{base_url}?{urllib.parse.urlencode(params)}"
    valid_paths = set()
    
    try:
        req = urllib.request.Request(full_url, headers={"User-Agent": "QuickbeamWideDiscover/2.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            for line in response:
                if not line.strip():
                    continue
                record = json.loads(line.decode('utf-8'))
                raw_url = record.get("url")
                if raw_url:
                    normalized = normalize_url(raw_url)
                    if normalized and not is_noise(normalized):
                        valid_paths.add(normalized)
    except Exception:
        # Gracefully skip domains that throw 404/400 cluster exceptions or timeout
        pass
        
    return valid_paths

def main():
    parser = argparse.ArgumentParser(description="Global High-Yield Discovery Engine for Quickbeam Execution Framework")
    parser.add_argument("domains", nargs="*", help="Specific domain strings to scan directly via CLI.")
    parser.add_argument("--file", help="Optional text file containing discovered domains to bulk process.")
    parser.add_argument("--limit", type=int, default=100, help="Max unique URLs to pull per distinct domain boundary.")
    parser.add_argument("--out", default="~/data/discovered_manifest.json", help="Destination mapping for quickbeam outputs.")
    
    args = parser.parse_args()
    collection = "CC-MAIN-2026-17"
    
    # Consolidate target list from positional arguments and input file stream
    target_domains = list(args.domains)
    if args.file:
        try:
            with open(args.file, 'r') as f:
                target_domains.extend([line.strip() for line in f if line.strip() and not line.startswith('#')])
        except Exception as e:
            print(f"❌ Failed to parse domain input file: {e}", file=sys.stderr)
            sys.exit(1)
            
    if not target_domains:
        print("❌ Error: No targets provided. Pass domains explicitly or supply a target domain --file.", file=sys.stderr)
        sys.exit(1)
        
    print(f"📡 Staging scan across {len(target_domains)} domains using index [{collection}]...", file=sys.stderr)
    master_url_pool = set()
    
    for i, domain in enumerate(target_domains, 1):
        # Clean domain string if it includes protocol artifacts
        domain = domain.replace("https://", "").replace("http://", "").split("/")[0]
        
        print(f" [{i}/{len(target_domains)}] Parsing: {domain}...", file=sys.stderr)
        discovered = harvest_domain_paths(collection, domain, args.limit)
        master_url_pool.update(discovered)
        
    if not master_url_pool:
        print("\n❌ Pipeline assembly aborted: Zero operational content targets survived filtering.", file=sys.stderr)
        sys.exit(1)
        
    print(f"\n🚀 Complete! Compiled {len(master_url_pool)} unique content nodes ready for processing.", file=sys.stderr)
    print("--------------------------------------------------------------------------------", file=sys.stderr)
    
    # Emit final quickbeam deployment execution block
    cmd = ["quickbeam data crawl \\"]
    cmd.append("  --routes ~/fangorn/embeddings/examples/cc/routes.json \\")
    cmd.append("  --extractors ~/fangorn/embeddings/examples/cc/extractors/ \\")
    
    for url in sorted(master_url_pool):
        cmd.append(f"  --url {url} \\")
        
    cmd.append("  --match-type exact \\")
    cmd.append(f"  --limit {len(master_url_pool)} \\")
    cmd.append("  --n-proc 8 \\")
    cmd.append(f"  --out {args.out} \\")
    cmd.append("  --aggregator free \\")
    cmd.append("  --cmon-bin ~/fangorn/embeddings/cmon_venv/bin/cmon")
    
    print("\n".join(cmd))

if __name__ == "__main__":
    main()