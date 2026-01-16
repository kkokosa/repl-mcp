# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

repl-mcp is an interactive REPL for testing Model Context Protocol (MCP) servers. It's a Python CLI tool that allows developers to interact with MCP servers through a terminal interface.

## Development Commands

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Format code
ruff format .

# Lint
ruff check .

# Run all tests
pytest

# Run a single test
pytest tests/test_basic.py::test_imports
```

## Architecture

The entire implementation is in a single file: `repl_mcp.py` (~1,200 lines).

### Key Components

- **Transport Layer**: `StdioTransport` and `HttpTransport` dataclasses define server connection configs. `connect_transport()` is an async context manager that abstracts connection logic for stdio, SSE, and streamable HTTP transports.

- **Configuration**: `load_config()` and `get_servers_from_config()` handle JSON/YAML config parsing. Supports both single-server flat format and multi-server `mcpServers` format (compatible with Cursor/Claude Desktop).

- **REPL Commands**: Each command has an async handler (`list_tools()`, `call_tool()`, etc.) that uses the MCP client session.

- **UI**: Uses Rich for formatted output (tables, panels, syntax highlighting) and prompt-toolkit for the interactive shell with history and autocompletion.

### Async Pattern

The codebase is async/await throughout. Entry point is `main_sync()` which wraps the async `main()` with `asyncio.run()`.

### MCP Client Usage

Connection to MCP servers happens via context managers from the `mcp` package. The session object provides methods like `list_tools()`, `call_tool()`, `list_prompts()`, `get_prompt()`, `list_resources()`, `read_resource()`.

## Configuration Formats

Two config styles are supported:
1. Single server: `{"command": "...", "args": [...]}` or `{"url": "..."}`
2. Multiple servers: `{"mcpServers": {"name": {...}, ...}}`

YAML is optional (requires pyyaml).
