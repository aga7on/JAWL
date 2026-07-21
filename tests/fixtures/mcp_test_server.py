"""Small real MCP stdio server used by integration tests."""

from mcp.server.fastmcp import FastMCP


server = FastMCP("jawl-test-server")


@server.tool()
def echo(message: str) -> str:
    """Return a message unchanged."""

    return message


@server.tool()
def blocked_tool(value: str) -> str:
    """A tool intentionally absent from the test allowlist."""

    return value


@server.resource("memo://status")
def status_resource() -> str:
    """Return a stable resource body."""

    return "ready"


@server.prompt()
def greet(name: str) -> str:
    """Build a tiny greeting prompt."""

    return f"Hello, {name}."


if __name__ == "__main__":
    server.run(transport="stdio")
