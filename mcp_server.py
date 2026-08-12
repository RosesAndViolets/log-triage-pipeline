"""Stateless MCP server exposing the triage toolset over stdio.

Thin on purpose: every tool is a plain function in mcp_tools.py, and this file
only publishes them. No port, no session state, no shared memory with the
pipeline — the log store is a file precisely so a separate process can read it.

Run standalone (it will sit waiting for a client on stdin):
    python mcp_server.py
Normally you don't: triage.py launches it as a subprocess over stdio.
"""

from mcp.server.fastmcp import FastMCP

import mcp_tools

# mcp must stay <2. google-genai 2.17 reads tool.inputSchema; mcp 2.0 renamed it
# input_schema, so the SDK's own MCP adapter raises AttributeError against 2.x.
server = FastMCP(
    name="triage-tools",
    instructions=(
        "Tools for diagnosing a production error: read source code, search the "
        "repository, query the log store, and find when a line last changed."
    ),
)

for fn in mcp_tools.TOOLS:
    server.tool()(fn)

if __name__ == "__main__":
    server.run()
