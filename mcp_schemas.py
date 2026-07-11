import asyncio, json
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
async def main():
    async with streamablehttp_client("http://localhost:8765/mcp") as (r,w,_):
        async with ClientSession(r,w) as s:
            await s.initialize()
            t=await s.list_tools()
            for x in t.tools:
                if x.name in ("aggregate","search","neighbors","get","export"):
                    print("###",x.name)
                    print(json.dumps(x.inputSchema))
asyncio.run(main())
