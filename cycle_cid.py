import asyncio, json
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

SYMS = "BE SGOV INTC CRWV AAPL ORCL USO SPCX QQQ GOOGL AMD SPY AMZN SLV META NVDA MSFT PLTR USAR SNDK MU TSLA COIN CRCL".split()


async def main():
    async with streamablehttp_client("http://localhost:8765/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            out = {}
            for sym in SYMS:
                res = await s.call_tool("get", {"dataset": "robinhood", "id": "rh:asset:" + sym})
                d = json.loads(res.content[0].text)
                prov = d.get("record", {}).get("provenance") or {}
                out[sym] = prov.get("source_cid")
            print(json.dumps(out))


asyncio.run(main())
