# MCP REPL

[![PyPI](https://img.shields.io/pypi/v/repl-mcp)](https://pypi.org/project/repl-mcp/)
[![License](https://img.shields.io/github/license/kkokosa/repl-mcp)](https://github.com/kkokosa/repl-mcp/blob/main/LICENSE)
[![Python Versions](https://img.shields.io/pypi/pyversions/repl-mcp)](https://pypi.org/project/repl-mcp/)

Interactive REPL for testing Model Context Protocol (MCP) servers.

![Demo of repl-mcp](repl-mcp-demo.gif)

## Features

- List and inspect tools, prompts, and resources
- Call tools with JSON or interactive parameter input
- Tab autocompletion for commands and tool names
- Command history (arrow keys)
- Rich terminal UI with syntax highlighting

## Installation

### Option 1: Install from PyPI (Recommended)

```bash
# Using uv (fast, Rust-based)
uv tool install repl-mcp

# Using pipx
pipx install repl-mcp

# Using pip
pip install repl-mcp
```

### Option 2: Run directly without installing

```bash
# Using uvx
uvx repl-mcp -c "npx mcp-remote https://mcp.atlassian.com/v1/mcp"
```

### Option 3: Development Setup

```bash
git clone https://github.com/kkokosa/repl-mcp.git
cd repl-mcp
pip install -e .
```

## Usage

> [!NOTE] 
> `mcp-remote` sidenote - Some MCP servers (like Atlassian) use OAuth for authentication and don't support direct HTTP connections. The `mcp-remote` MCP bridges this gap by handling OAuth flows locally and/or converting HTTP-based MCP servers to stdio transport. Beware that for OAuth-based servers, `mcp-remote` needs to receive the OAuth callback. If you're running in an environment without a public URL (like WSL or a remote server), you'll need a tunneling tool like [ngrok](https://ngrok.com/) to expose the callback endpoint.


### Stdio Transport (command-based)

```bash
# Remote MCP server via mcp-remote
repl-mcp -c "npx mcp-remote https://mcp.atlassian.com/v1/mcp"

# Local Python MCP server
repl-mcp -c "python my_mcp_server.py"

# Using uvx
repl-mcp -c "uvx mcp-server-sqlite --db-path test.db"
```

### HTTP Transport (URL-based)

```bash
# GitHub Copilot MCP
repl-mcp --url https://api.githubcopilot.com/mcp/ \
         --header "Authorization: Bearer YOUR_GITHUB_TOKEN"

# Notion MCP
repl-mcp -u https://mcp.notion.com/mcp

# Multiple headers
repl-mcp -u https://example.com/mcp \
         -H "Authorization: Bearer TOKEN" \
         -H "X-Custom-Header: value"
```

### Using a config file

```bash
# If multiple servers in config, you'll be prompted to choose
repl-mcp --config sample-mcp-config.json

# Or select specific server directly
repl-mcp --config sample-mcp-config.json --server github
```

#### Config file formats

**Stdio transport:**

```json
{
  "command": "npx",
  "args": ["mcp-remote", "https://mcp.atlassian.com/v1/mcp"]
}
```

**HTTP transport:**

```json
{
  "url": "https://api.githubcopilot.com/mcp/",
  "headers": {
    "Authorization": "Bearer YOUR_TOKEN"
  }
}
```

**Multiple servers (Cursor/Claude Desktop format):**

```json
{
  "mcpServers": {
    "atlassian": {
      "command": "npx",
      "args": ["mcp-remote", "https://mcp.atlassian.com/v1/mcp"]
    },
    "github": {
      "url": "https://api.githubcopilot.com/mcp/",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN"
      }
    },
    "notion": {
      "url": "https://mcp.notion.com/mcp",
      "headers": {}
    }
  }
}
```

**YAML format:**

```yaml
command: npx
args:
  - mcp-remote
  - https://mcp.atlassian.com/v1/mcp
```

## REPL Commands

| Command | Description |
|---------|-------------|
| `tools` | List all available tools |
| `tool <name>` | Show detailed info about a specific tool |
| `prompts` | List all available prompts |
| `resources` | List all available resources |
| `call <tool> [args]` | Call a tool (interactive input if no args) |
| `prompt <name> [args]` | Get a prompt with optional JSON arguments |
| `read <uri>` | Read a resource by URI |
| `info` | Show server info and capabilities |
| `help` | Show help message |
| `quit` / `exit` / `q` | Exit the REPL |

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Tab` | Autocomplete commands and tool names |
| `↑` / `↓` | Navigate command history |
| `Ctrl+C` | Cancel current input |
| `Ctrl+D` | Exit REPL |

## Examples

```
mcp> tools
┌─────────────────────────────────────────────────────────┐
│ Available Tools (15)                                    │
├──────────────────┬───────────────────────────┬──────────┤
│ Name             │ Description               │ Params   │
├──────────────────┼───────────────────────────┼──────────┤
│ get_issue        │ Retrieves a Jira issue... │ issueKey │
│ search_issues    │ Search for issues using...│ jql      │
└──────────────────┴───────────────────────────┴──────────┘

mcp> call get_issue {"issueKey": "PROJ-123"}
┌─────────────────────────────────────────────────────────┐
│ Result                                                  │
├─────────────────────────────────────────────────────────┤
│ {                                                       │
│   "key": "PROJ-123",                                    │
│   "summary": "Example issue",                           │
│   ...                                                   │
│ }                                                       │
└─────────────────────────────────────────────────────────┘

mcp> quit
Goodbye!
```

## Requirements

- Python 3.10+
- Node.js (if using `npx mcp-remote`)
