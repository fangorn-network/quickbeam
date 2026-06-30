"""Global-identity helpers for cross-publisher linking (Phase 1).

quickbeam has to map a Composed View's declared *source resourceIds* back to the
on-chain ManifestPublished events that carry them. A resourceId is
`keccak256(owner ++ schemaId ++ keccak256(name))` (see fangorn
`DataSourceRegistry.resourceId`), and the subgraph hands us `owner`, `schemaId`
and `nameHash` per event — so we can recompute each event's resourceId and keep
the ones a view asked for.

keccak256 (the Ethereum hash) is *not* Python's `hashlib.sha3_256` (that's NIST
SHA3, a different padding). Rather than pull in web3/eth-hash as a build
dependency, we vendor a small, self-contained keccak-f[1600]. It is exercised by
tests/test_identity.py against known vectors and a fangorn-produced resourceId.
"""

# Round constants for keccak-f[1600].
_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]

# Rho rotation offsets, indexed [x][y].
_ROFF = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]

_MASK = (1 << 64) - 1


def _rotl(v: int, n: int) -> int:
    n &= 63
    return ((v << n) | (v >> (64 - n))) & _MASK


def _keccak_f(state):
    for rnd in range(24):
        # theta
        C = [state[x][0] ^ state[x][1] ^ state[x][2] ^ state[x][3] ^ state[x][4] for x in range(5)]
        D = [C[(x - 1) % 5] ^ _rotl(C[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x][y] ^= D[x]
        # rho + pi
        B = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                B[y][(2 * x + 3 * y) % 5] = _rotl(state[x][y], _ROFF[x][y])
        # chi
        for x in range(5):
            for y in range(5):
                state[x][y] = B[x][y] ^ ((~B[(x + 1) % 5][y]) & B[(x + 2) % 5][y])
        # iota
        state[0][0] ^= _RC[rnd]
    return state


def keccak256(data: bytes) -> bytes:
    """Ethereum keccak256 of `data` → 32 raw bytes."""
    rate = 136  # 1088-bit rate for the 256-bit capacity variant
    state = [[0] * 5 for _ in range(5)]

    # ── absorb, with keccak (0x01 .. 0x80) multi-rate padding ──
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] ^= 0x80

    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(0, rate, 8):
            lane = int.from_bytes(block[i:i + 8], "little")
            x = (i // 8) % 5
            y = (i // 8) // 5
            state[x][y] ^= lane
        _keccak_f(state)

    # ── squeeze (one block is enough for 32 bytes) ──
    out = bytearray()
    for i in range(0, 32, 8):
        x = (i // 8) % 5
        y = (i // 8) // 5
        out += state[x][y].to_bytes(8, "little")
    return bytes(out[:32])


def _strip0x(h: str) -> str:
    return h[2:] if h.startswith(("0x", "0X")) else h


def name_hash(name: str) -> str:
    """keccak256(utf8(name)) as 0x-prefixed hex — the on-chain dataset nameHash."""
    return "0x" + keccak256(name.encode("utf-8")).hex()


def resource_id(owner: str, schema_id: str, name_or_hash: str, *, is_hash: bool = False) -> str:
    """Recompute a datasource resourceId from event metadata.

    Mirrors fangorn `DataSourceRegistry.resourceId`:
        keccak256( owner(20) ++ schemaId(32) ++ nameHash(32) )
    `name_or_hash` is the raw dataset name, or its nameHash when `is_hash=True`
    (the subgraph already hands us `nameHash`, so we avoid re-hashing).
    """
    owner_b = bytes.fromhex(_strip0x(owner).rjust(40, "0"))[-20:]
    schema_b = bytes.fromhex(_strip0x(schema_id).rjust(64, "0"))[-32:]
    nh = name_or_hash if is_hash else name_hash(name_or_hash)
    name_b = bytes.fromhex(_strip0x(nh).rjust(64, "0"))[-32:]
    return "0x" + keccak256(owner_b + schema_b + name_b).hex()


def norm_hex(h: str) -> str:
    """Lower-cased, 0x-prefixed form for set membership comparisons."""
    return "0x" + _strip0x(h).lower()
