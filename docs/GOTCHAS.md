Architectural Deep Dive & "Gotchas" to Watch For

Since we're looking closely at your current specifications, a few architectural challenges are worth keeping an eye on as this scales:
🛡️ The "Identity-Jacking" Security Vulnerability

In Section D, you allow an external business wallet to publish a profile containing:
JSON

{ "placeId": "ChIJ...shotskis", "officialName": "Shotskis Bar & Grill" }

Because their data model asserts an identity alias (gplace: "placeId"), your View's union-find algorithm merges them for free.

    The Risk: What stops a malicious wallet from publishing a bundle claiming that exact same placeId, but rewriting the menuUrl to a phishing link or inserting malicious text? If your View automatically runs a union-find on all inputs blindly, a rogue data source can inject poison data into an elite entity.

    The Fix: Your View registration needs an explicit Publisher Whitelist or an explicit validation check alongside the minConfidence trust policy to ensure foreign wallets can only append data to identities they genuinely own or are permitted to modify.

📍 The Coordinate Match Degradation (linkgen)

Your linkgen utilizes a fixed spatial radius (--radius-m 75) and string similarity. This works wonderfully in rural areas like Eagle River, Wisconsin. However, if you run this in Manhattan or Tokyo, a 75-meter radius can encompass three different skyscrapers containing five different sushi restaurants with similar names (e.g., "Sushi Ichiko" vs "Sushi Ichiba").

    Tip: You might want to introduce an adaptive radius threshold based on local entity density, or lean harder on normalized phone numbers/website domains during linkgen passes when spatial data gets crowded.

What are your plans for handling the client-side graph traversal—are you writing a custom SDK to fetch and stitch these static CDN shards on the fly, or are you utilizing something existing?