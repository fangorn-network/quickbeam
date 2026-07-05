#!/usr/bin/env python
"""Minimal driver for the quickbeam streamable-http MCP server.

Usage: python mcp_call.py <tool> '<json-args>'
       python mcp_call.py --list
Prints the tool result text (JSON) to stdout.
"""
import sys, json, asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = "http://localhost:8765/mcp"


async def main():
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if sys.argv[1] == "--list":
                tools = await session.list_tools()
                print(json.dumps([t.name for t in tools.tools]))
                return
            tool = sys.argv[1]
            args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
            res = await session.call_tool(tool, args)
            out = []
            for c in res.content:
                out.append(getattr(c, "text", str(c)))
            print("\n".join(out))


asyncio.run(main())
