#!/usr/bin/env python3
"""
MCP REPL - Interactive REPL for testing Model Context Protocol servers.

Usage:
    mcp-repl --command "npx mcp-remote -u https://mcp.atlassian.com/v1/mcp"
    mcp-repl --url https://api.githubcopilot.com/mcp/ --header "Authorization: Bearer TOKEN"
    mcp-repl --config mcp-config.json
"""

import argparse
import asyncio
import json
import shlex
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import AsyncIterator

from mcp import ClientSession, StdioServerParameters
from pydantic import AnyUrl
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client

# Try to import streamable HTTP client (available in newer mcp versions)
try:
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    _has_streamable_http = True
except ImportError:
    _has_streamable_http = False
    streamable_http_client = None  # type: ignore
    create_mcp_http_client = None  # type: ignore
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.syntax import Syntax
from rich.text import Text
from rich import box


@dataclass
class StdioTransport:
    """Stdio transport configuration."""

    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None


@dataclass
class HttpTransport:
    """HTTP/SSE transport configuration."""

    url: str
    headers: dict[str, str] = field(default_factory=dict)
    transport_type: str = "sse"  # "sse" or "streamable_http"


console = Console()

# Base commands for autocompletion
BASE_COMMANDS = [
    "tools",
    "tool",
    "prompts",
    "resources",
    "call",
    "prompt",
    "read",
    "info",
    "help",
    "quit",
    "exit",
    "q",
]

# Prompt style
PROMPT_STYLE = Style.from_dict(
    {
        "prompt": "bold magenta",
    }
)


class MCPCompleter(Completer):
    """Custom completer for MCP REPL with dynamic tool name completion."""

    def __init__(self):
        self.tool_names: list[str] = []
        self.prompt_names: list[str] = []
        self.resource_uris: list[str] = []

    def update_tools(self, tools: list[str]):
        self.tool_names = tools

    def update_prompts(self, prompts: list[str]):
        self.prompt_names = prompts

    def update_resources(self, resources: list[str]):
        self.resource_uris = resources

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        words = text.split()

        # Empty or first word - complete commands
        if not words or (len(words) == 1 and not text.endswith(" ")):
            word = words[0] if words else ""
            for cmd in BASE_COMMANDS:
                if cmd.startswith(word.lower()):
                    yield Completion(cmd, start_position=-len(word))

        # Second word - context-dependent completion
        elif len(words) >= 1:
            cmd = words[0].lower()
            current_word = words[-1] if not text.endswith(" ") else ""
            start_pos = -len(current_word)

            if cmd in ("tool", "call"):
                # Complete tool names
                for name in self.tool_names:
                    if name.lower().startswith(current_word.lower()):
                        yield Completion(name, start_position=start_pos)

            elif cmd == "prompt":
                # Complete prompt names
                for name in self.prompt_names:
                    if name.lower().startswith(current_word.lower()):
                        yield Completion(name, start_position=start_pos)

            elif cmd == "read":
                # Complete resource URIs
                for uri in self.resource_uris:
                    if uri.lower().startswith(current_word.lower()):
                        yield Completion(uri, start_position=start_pos)


TransportConfig = StdioTransport | HttpTransport


def parse_command(command: str) -> StdioTransport:
    """Parse a shell command string into StdioTransport."""
    parts = shlex.split(command)
    if not parts:
        raise ValueError("Empty command")
    return StdioTransport(command=parts[0], args=parts[1:])


def parse_url(
    url: str, headers: list[str] | None = None, transport_type: str = "sse"
) -> HttpTransport:
    """Parse URL and headers into HttpTransport."""
    header_dict = {}
    if headers:
        for h in headers:
            if ":" in h:
                key, value = h.split(":", 1)
                header_dict[key.strip()] = value.strip()
            else:
                console.print(
                    f"[yellow]Warning: Invalid header format '{h}', expected 'Key: Value'[/yellow]"
                )
    return HttpTransport(url=url, headers=header_dict, transport_type=transport_type)


def load_config(config_path: str, server_name: str | None = None) -> TransportConfig:
    """Load MCP configuration from a JSON or YAML file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    content = path.read_text()

    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]

            config = yaml.safe_load(content)
        except ImportError:
            console.print(
                "[red]PyYAML not installed. Install with: pip install pyyaml[/red]"
            )
            sys.exit(1)
    else:
        config = json.loads(content)

    # Support flat format (single server)
    if "command" in config:
        return StdioTransport(
            command=config["command"],
            args=config.get("args", []),
            env=config.get("env"),
        )
    elif "url" in config:
        # Detect transport type: "http" means streamable HTTP, "sse" or unspecified means SSE
        cfg_type = config.get("type", "sse")
        transport_type = "streamable_http" if cfg_type == "http" else "sse"
        return HttpTransport(
            url=config["url"],
            headers=config.get("headers", {}),
            transport_type=transport_type,
        )

    # Support mcpServers format (multiple servers)
    elif "mcpServers" in config:
        servers = config["mcpServers"]
        if not servers:
            raise ValueError("No servers defined in config")

        # Select server by name, interactive prompt, or use first one
        if server_name:
            if server_name not in servers:
                available = ", ".join(servers.keys())
                raise ValueError(
                    f"Server '{server_name}' not found. Available: {available}"
                )
            name = server_name
        elif len(servers) > 1:
            # Multiple servers - let user choose interactively
            server_list = get_servers_from_config(config)
            name = select_server_interactive(server_list)
        else:
            # Single server - use it directly
            name = next(iter(servers.keys()))

        server_config = servers[name]
        console.print(f"[dim]Using server: {name}[/dim]")

        # Detect transport type
        if "url" in server_config:
            # "type": "http" means streamable HTTP transport
            cfg_type = server_config.get("type", "sse")
            transport_type = "streamable_http" if cfg_type == "http" else "sse"
            return HttpTransport(
                url=server_config["url"],
                headers=server_config.get("headers", {}),
                transport_type=transport_type,
            )
        elif "command" in server_config:
            return StdioTransport(
                command=server_config["command"],
                args=server_config.get("args", []),
                env=server_config.get("env"),
            )
        else:
            raise ValueError(f"Server '{name}' has no 'command' or 'url' defined")
    else:
        raise ValueError(
            "Invalid config format. Expected 'command', 'url', or 'mcpServers' key."
        )


def get_servers_from_config(config: dict) -> list[tuple[str, str]]:
    """Get available servers from parsed config. Returns list of (name, type) tuples."""
    if "mcpServers" not in config:
        return []

    servers = []
    for name, server_config in config["mcpServers"].items():
        if "url" in server_config:
            servers.append((name, "http"))
        elif "command" in server_config:
            servers.append((name, "stdio"))
        else:
            servers.append((name, "unknown"))
    return servers


def select_server_interactive(servers: list[tuple[str, str]]) -> str:
    """Interactively prompt user to select a server from the list."""
    console.print()
    table = Table(title="Available Servers", box=box.ROUNDED, border_style="cyan")
    table.add_column("#", style="bold yellow", justify="right")
    table.add_column("Name", style="bold cyan")
    table.add_column("Transport", style="dim")

    for i, (name, transport_type) in enumerate(servers, 1):
        table.add_row(str(i), name, transport_type)

    console.print(table)
    console.print()

    while True:
        console.print(
            "[bold]Select a server[/bold] [dim](enter number or name)[/dim]: ", end=""
        )
        choice = input().strip()

        if not choice:
            continue

        # Try as number
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(servers):
                return servers[idx][0]
            console.print(f"[red]Invalid number. Enter 1-{len(servers)}[/red]")
            continue
        except ValueError:
            pass

        # Try as name
        for name, _ in servers:
            if name.lower() == choice.lower():
                return name

        console.print(f"[red]Server '{choice}' not found[/red]")


@asynccontextmanager
async def connect_transport(transport: TransportConfig) -> AsyncIterator[ClientSession]:
    """Connect to MCP server using the appropriate transport."""
    if isinstance(transport, StdioTransport):
        server_params = StdioServerParameters(
            command=transport.command,
            args=transport.args,
            env=transport.env,
        )
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:
        # HttpTransport - choose between SSE and Streamable HTTP
        if transport.transport_type == "streamable_http":
            if (
                not _has_streamable_http
                or streamable_http_client is None
                or create_mcp_http_client is None
            ):
                console.print(
                    "[yellow]Warning: Streamable HTTP not available in this mcp version, falling back to SSE[/yellow]"
                )
                async with sse_client(transport.url, headers=transport.headers) as (
                    read,
                    write,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
            else:
                # streamable_http_client takes http_client instead of headers
                # Use MCP's factory for proper defaults (follow_redirects, timeout, etc.)
                async with create_mcp_http_client(
                    headers=transport.headers
                ) as http_client:
                    async with streamable_http_client(
                        transport.url, http_client=http_client
                    ) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            yield session
        else:
            # SSE transport
            async with sse_client(transport.url, headers=transport.headers) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session


def print_banner():
    """Print the MCP REPL banner."""
    banner = Text()
    banner.append("MCP REPL", style="bold magenta")
    banner.append(" - Interactive Model Context Protocol Tester", style="dim")
    console.print(Panel(banner, box=box.ROUNDED, border_style="magenta"))


def print_help():
    """Print available commands."""
    table = Table(title="Available Commands", box=box.ROUNDED, border_style="cyan")
    table.add_column("Command", style="bold green")
    table.add_column("Description", style="white")

    commands = [
        ("tools", "List all available tools"),
        ("tool <name>", "Show detailed info about a specific tool"),
        ("prompts", "List all available prompts"),
        ("resources", "List all available resources"),
        ("call <tool> [args]", "Call a tool with optional JSON arguments"),
        ("prompt <name> [args]", "Get a prompt with optional JSON arguments"),
        ("read <uri>", "Read a resource by URI"),
        ("info", "Show server info and capabilities"),
        ("help", "Show this help message"),
        ("quit / exit / q", "Exit the REPL"),
    ]

    for cmd, desc in commands:
        table.add_row(cmd, desc)

    console.print(table)


async def list_tools(session: ClientSession):
    """List all available tools."""
    try:
        result = await session.list_tools()
        if not result.tools:
            console.print("[yellow]No tools available[/yellow]")
            return

        table = Table(
            title=f"Available Tools ({len(result.tools)})",
            box=box.ROUNDED,
            border_style="green",
        )
        table.add_column("Name", style="bold cyan", no_wrap=True)
        table.add_column("Description", style="white", max_width=60)
        table.add_column("Parameters", style="dim")

        for tool in result.tools:
            desc = tool.description or "[dim]No description[/dim]"
            if len(desc) > 60:
                desc = desc[:57] + "..."

            # Extract parameter names from input schema
            params = ""
            if tool.inputSchema and "properties" in tool.inputSchema:
                param_names = list(tool.inputSchema["properties"].keys())
                required = tool.inputSchema.get("required", [])
                param_strs = []
                for p in param_names[:5]:  # Show max 5 params
                    if p in required:
                        param_strs.append(f"[bold]{p}[/bold]")
                    else:
                        param_strs.append(p)
                params = ", ".join(param_strs)
                if len(param_names) > 5:
                    params += f" (+{len(param_names) - 5})"

            table.add_row(tool.name, desc, params)

        console.print(table)
    except Exception as e:
        console.print(f"[red]Error listing tools: {e}[/red]")


async def show_tool(session: ClientSession, tool_name: str):
    """Show detailed info about a specific tool."""
    try:
        result = await session.list_tools()
        tool = next((t for t in result.tools if t.name == tool_name), None)

        if not tool:
            console.print(f"[red]Tool not found: {tool_name}[/red]")
            # Suggest similar tools
            similar = [
                t.name for t in result.tools if tool_name.lower() in t.name.lower()
            ]
            if similar:
                console.print(f"[dim]Did you mean: {', '.join(similar[:5])}?[/dim]")
            return

        # Tool name and description
        console.print(
            Panel(
                tool.description or "[dim]No description[/dim]",
                title=f"[bold cyan]{tool.name}[/bold cyan]",
                border_style="green",
            )
        )

        # Parameters table
        if tool.inputSchema and "properties" in tool.inputSchema:
            props = tool.inputSchema["properties"]
            required = tool.inputSchema.get("required", [])

            table = Table(title="Parameters", box=box.ROUNDED, border_style="cyan")
            table.add_column("Name", style="bold")
            table.add_column("Type", style="dim")
            table.add_column("Required", style="yellow")
            table.add_column("Description", max_width=50)

            for name, schema in props.items():
                param_type = schema.get("type", "any")
                if "enum" in schema:
                    param_type = f"enum: {schema['enum']}"
                is_required = "✓" if name in required else ""
                desc = schema.get("description", "")
                table.add_row(name, str(param_type), is_required, desc)

            console.print(table)

        # Show full schema
        if tool.inputSchema:
            console.print(
                Panel(
                    Syntax(
                        json.dumps(tool.inputSchema, indent=2), "json", theme="monokai"
                    ),
                    title="Input Schema",
                    border_style="dim",
                )
            )

    except Exception as e:
        console.print(f"[red]Error getting tool info: {e}[/red]")


async def list_prompts(session: ClientSession):
    """List all available prompts."""
    try:
        result = await session.list_prompts()
        if not result.prompts:
            console.print("[yellow]No prompts available[/yellow]")
            return

        table = Table(
            title=f"Available Prompts ({len(result.prompts)})",
            box=box.ROUNDED,
            border_style="blue",
        )
        table.add_column("Name", style="bold cyan", no_wrap=True)
        table.add_column("Description", style="white", max_width=60)
        table.add_column("Arguments", style="dim")

        for prompt in result.prompts:
            desc = prompt.description or "[dim]No description[/dim]"
            if len(desc) > 60:
                desc = desc[:57] + "..."

            args = ""
            if prompt.arguments:
                arg_strs = []
                for arg in prompt.arguments[:5]:
                    if arg.required:
                        arg_strs.append(f"[bold]{arg.name}[/bold]")
                    else:
                        arg_strs.append(arg.name)
                args = ", ".join(arg_strs)
                if len(prompt.arguments) > 5:
                    args += f" (+{len(prompt.arguments) - 5})"

            table.add_row(prompt.name, desc, args)

        console.print(table)
    except Exception as e:
        if "Method not found" in str(e):
            console.print("[yellow]Prompts not supported by this server[/yellow]")
        else:
            console.print(f"[red]Error listing prompts: {e}[/red]")


async def list_resources(session: ClientSession):
    """List all available resources."""
    try:
        result = await session.list_resources()
        if not result.resources:
            console.print("[yellow]No resources available[/yellow]")
            return

        table = Table(
            title=f"Available Resources ({len(result.resources)})",
            box=box.ROUNDED,
            border_style="yellow",
        )
        table.add_column("Name", style="bold cyan", no_wrap=True)
        table.add_column("URI", style="white", max_width=50)
        table.add_column("MIME Type", style="dim")
        table.add_column("Description", style="dim", max_width=30)

        for resource in result.resources:
            desc = resource.description or ""
            if len(desc) > 30:
                desc = desc[:27] + "..."
            table.add_row(
                resource.name,
                str(resource.uri),
                resource.mimeType or "-",
                desc,
            )

        console.print(table)
    except Exception as e:
        if "Method not found" in str(e):
            console.print("[yellow]Resources not supported by this server[/yellow]")
        else:
            console.print(f"[red]Error listing resources: {e}[/red]")


async def interactive_params(session: ClientSession, tool_name: str) -> dict | None:
    """Interactively prompt for tool parameters with a fancy UI."""
    # Fetch tool schema
    try:
        tools_result = await session.list_tools()
        tool = next((t for t in tools_result.tools if t.name == tool_name), None)
        if not tool:
            console.print(f"[red]Tool not found: {tool_name}[/red]")
            return None
    except Exception as e:
        console.print(f"[red]Error fetching tool info: {e}[/red]")
        return None

    # Check if tool has parameters
    if not tool.inputSchema or "properties" not in tool.inputSchema:
        return {}  # No parameters needed

    properties = tool.inputSchema["properties"]
    required = set(tool.inputSchema.get("required", []))

    if not properties:
        return {}  # No parameters needed

    # Show tool info
    console.print(
        Panel(
            tool.description or "[dim]No description[/dim]",
            title=f"[bold cyan]{tool_name}[/bold cyan]",
            border_style="cyan",
        )
    )

    # Build parameter table for reference
    table = Table(
        title="Parameters", box=box.ROUNDED, border_style="dim", show_header=True
    )
    table.add_column("Name", style="bold")
    table.add_column("Type", style="dim")
    table.add_column("Required", style="yellow", justify="center")
    table.add_column("Description", max_width=40)

    for name, schema in properties.items():
        param_type = schema.get("type", "any")
        if "enum" in schema:
            param_type = "enum"
        required_marker = "[bold red]*[/bold red]" if name in required else ""
        desc = schema.get("description", "")[:40]
        table.add_row(name, str(param_type), required_marker, desc)

    console.print(table)
    console.print()
    console.print(
        "[dim]Enter values for each parameter (press Enter to skip optional ones):[/dim]"
    )
    console.print(
        "[dim]Type [bold]/skip[/bold] to skip all remaining optional parameters[/dim]"
    )
    console.print("[dim]Use Ctrl+C to cancel[/dim]")
    console.print()

    arguments = {}
    skip_optional = False

    try:
        for name, schema in properties.items():
            is_required = name in required

            # Skip optional parameters if user requested
            if skip_optional and not is_required:
                continue
            param_type = schema.get("type", "string")
            description = schema.get("description", "")
            default = schema.get("default")
            enum_values = schema.get("enum")

            # Build the prompt label
            if is_required:
                label = f"[bold red]*[/bold red] [bold cyan]{name}[/bold cyan]"
            else:
                label = f"  [cyan]{name}[/cyan]"

            # Add type hint
            if enum_values:
                type_hint = (
                    f" [dim](options: {', '.join(str(v) for v in enum_values)})[/dim]"
                )
            elif param_type == "boolean":
                type_hint = " [dim](true/false)[/dim]"
            elif param_type == "integer":
                type_hint = " [dim](integer)[/dim]"
            elif param_type == "number":
                type_hint = " [dim](number)[/dim]"
            elif param_type == "array":
                type_hint = " [dim](JSON array)[/dim]"
            elif param_type == "object":
                type_hint = " [dim](JSON object)[/dim]"
            else:
                type_hint = ""

            # Add default hint
            default_hint = ""
            if default is not None:
                default_hint = f" [dim]default: {default}[/dim]"

            # Show description if available
            if description:
                console.print(f"  [dim italic]{description}[/dim italic]")

            # Prompt for value
            prompt_text = f"{label}{type_hint}{default_hint}: "
            console.print(prompt_text, end="")

            # Read input (we use regular input since we're in async context)
            value = await asyncio.get_event_loop().run_in_executor(None, input, "")
            value = value.strip()

            # Handle /skip command
            if value.lower() == "/skip":
                if is_required and default is None:
                    console.print(
                        "[yellow]  ↳ Cannot skip required parameter, please provide a value[/yellow]"
                    )
                    console.print(prompt_text, end="")
                    value = await asyncio.get_event_loop().run_in_executor(
                        None, input, ""
                    )
                    value = value.strip()
                    if not value or value.lower() == "/skip":
                        console.print(
                            f"[red]Cancelled: required parameter '{name}' not provided[/red]"
                        )
                        return None
                    # Fall through to parse the value
                else:
                    skip_optional = True
                    console.print(
                        "[dim]  ↳ Skipping remaining optional parameters[/dim]"
                    )
                    continue

            # Handle empty input
            if not value:
                if is_required and default is None:
                    console.print(
                        "[red]  ↳ Required parameter, please provide a value[/red]"
                    )
                    # Re-prompt
                    console.print(prompt_text, end="")
                    value = await asyncio.get_event_loop().run_in_executor(
                        None, input, ""
                    )
                    value = value.strip()
                    if not value:
                        console.print(
                            f"[red]Cancelled: required parameter '{name}' not provided[/red]"
                        )
                        return None
                elif default is not None:
                    arguments[name] = default
                    console.print(f"  [dim]↳ Using default: {default}[/dim]")
                    continue
                else:
                    # Skip optional parameter
                    continue

            # Parse value based on type
            try:
                if param_type == "boolean":
                    if value.lower() in ("true", "yes", "1", "y"):
                        arguments[name] = True
                    elif value.lower() in ("false", "no", "0", "n"):
                        arguments[name] = False
                    else:
                        console.print(
                            "[yellow]  ↳ Invalid boolean, treating as string[/yellow]"
                        )
                        arguments[name] = value
                elif param_type == "integer":
                    arguments[name] = int(value)
                elif param_type == "number":
                    arguments[name] = float(value)
                elif param_type in ("array", "object"):
                    arguments[name] = json.loads(value)
                else:
                    # String or unknown type
                    arguments[name] = value

                # Validate enum
                if enum_values and arguments[name] not in enum_values:
                    console.print(
                        f"[yellow]  ↳ Warning: '{value}' not in allowed values: {enum_values}[/yellow]"
                    )

            except (ValueError, json.JSONDecodeError) as e:
                console.print(
                    f"[yellow]  ↳ Parse error ({e}), treating as string[/yellow]"
                )
                arguments[name] = value

    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled[/yellow]")
        return None

    console.print()
    return arguments


async def call_tool(session: ClientSession, tool_name: str, args_str: str = ""):
    """Call a tool with optional JSON arguments."""
    try:
        arguments: dict | None = {}
        if args_str.strip():
            try:
                arguments = json.loads(args_str)
            except json.JSONDecodeError as e:
                console.print(f"[red]Invalid JSON arguments: {e}[/red]")
                return
        else:
            # No arguments provided - use interactive mode
            arguments = await interactive_params(session, tool_name)
            if arguments is None:
                return  # Cancelled or error

        console.print(f"[dim]Calling tool: {tool_name}[/dim]")
        if arguments:
            console.print(
                Panel(
                    Syntax(json.dumps(arguments, indent=2), "json", theme="monokai"),
                    title="Arguments",
                    border_style="dim",
                )
            )

        result = await session.call_tool(tool_name, arguments=arguments)

        # Display results
        for i, content in enumerate(result.content):
            if hasattr(content, "text"):
                text_content: str = getattr(content, "text")
                # Try to parse as JSON for pretty printing
                try:
                    parsed = json.loads(text_content)
                    console.print(
                        Panel(
                            Syntax(
                                json.dumps(parsed, indent=2), "json", theme="monokai"
                            ),
                            title=f"Result {i + 1}"
                            if len(result.content) > 1
                            else "Result",
                            border_style="green",
                        )
                    )
                except json.JSONDecodeError:
                    # Plain text output
                    console.print(
                        Panel(
                            text_content,
                            title=f"Result {i + 1}"
                            if len(result.content) > 1
                            else "Result",
                            border_style="green",
                        )
                    )
            elif hasattr(content, "data"):
                mime_type = getattr(content, "mimeType", "unknown")
                data = getattr(content, "data")
                console.print(
                    Panel(
                        f"[dim]Binary data ({mime_type}): {len(data)} bytes[/dim]",
                        title=f"Result {i + 1}"
                        if len(result.content) > 1
                        else "Result",
                        border_style="green",
                    )
                )
            else:
                console.print(Panel(str(content), title="Result", border_style="green"))

        if result.isError:
            console.print("[red]Tool returned an error[/red]")

    except Exception as e:
        console.print(f"[red]Error calling tool: {e}[/red]")


async def get_prompt(session: ClientSession, prompt_name: str, args_str: str = ""):
    """Get a prompt with optional JSON arguments."""
    try:
        arguments = {}
        if args_str.strip():
            try:
                arguments = json.loads(args_str)
            except json.JSONDecodeError as e:
                console.print(f"[red]Invalid JSON arguments: {e}[/red]")
                return

        console.print(f"[dim]Getting prompt: {prompt_name}[/dim]")

        result = await session.get_prompt(prompt_name, arguments=arguments)

        if result.description:
            console.print(f"[cyan]Description:[/cyan] {result.description}")

        for message in result.messages:
            role_color = "green" if message.role == "assistant" else "blue"
            if hasattr(message.content, "text"):
                content_text = getattr(message.content, "text")
            else:
                content_text = str(message.content)

            console.print(
                Panel(
                    Markdown(content_text)
                    if "```" in content_text or "#" in content_text
                    else content_text,
                    title=f"[{role_color}]{message.role.upper()}[/{role_color}]",
                    border_style=role_color,
                )
            )

    except Exception as e:
        console.print(f"[red]Error getting prompt: {e}[/red]")


async def read_resource(session: ClientSession, uri: str):
    """Read a resource by URI."""
    try:
        console.print(f"[dim]Reading resource: {uri}[/dim]")

        result = await session.read_resource(AnyUrl(uri))

        for content in result.contents:
            if hasattr(content, "text"):
                # Try to detect content type and syntax highlight
                text: str = getattr(content, "text")
                mime = getattr(content, "mimeType", "") or ""

                if mime.endswith("/json") or text.strip().startswith("{"):
                    try:
                        parsed = json.loads(text)
                        console.print(
                            Panel(
                                Syntax(
                                    json.dumps(parsed, indent=2),
                                    "json",
                                    theme="monokai",
                                ),
                                title="Resource Content",
                                border_style="yellow",
                            )
                        )
                        continue
                    except json.JSONDecodeError:
                        pass

                console.print(
                    Panel(
                        text,
                        title="Resource Content",
                        border_style="yellow",
                    )
                )
            elif hasattr(content, "blob"):
                blob = getattr(content, "blob")
                console.print(
                    Panel(
                        f"[dim]Binary data: {len(blob)} bytes[/dim]",
                        title="Resource Content",
                        border_style="yellow",
                    )
                )

    except Exception as e:
        console.print(f"[red]Error reading resource: {e}[/red]")


async def show_info(session: ClientSession):
    """Show server info and capabilities."""
    try:
        table = Table(
            title="Server Information", box=box.ROUNDED, border_style="magenta"
        )
        table.add_column("Property", style="bold cyan")
        table.add_column("Value", style="white")

        # Get capabilities from session
        server_info = getattr(session, "_server_info", None)
        if server_info:
            table.add_row("Name", getattr(server_info, "name", "N/A"))
            table.add_row("Version", getattr(server_info, "version", "N/A"))

        # List what's available
        try:
            tools = await session.list_tools()
            table.add_row("Tools", str(len(tools.tools)))
        except Exception:
            table.add_row("Tools", "[dim]Not supported[/dim]")

        try:
            prompts = await session.list_prompts()
            table.add_row("Prompts", str(len(prompts.prompts)))
        except Exception:
            table.add_row("Prompts", "[dim]Not supported[/dim]")

        try:
            resources = await session.list_resources()
            table.add_row("Resources", str(len(resources.resources)))
        except Exception:
            table.add_row("Resources", "[dim]Not supported[/dim]")

        console.print(table)
    except Exception as e:
        console.print(f"[red]Error getting info: {e}[/red]")


async def repl_loop(session: ClientSession):
    """Main REPL loop."""
    print_help()
    console.print()

    # Set up completer with dynamic tool/prompt/resource names
    completer = MCPCompleter()

    # Pre-fetch tool names for completion
    try:
        tools_result = await session.list_tools()
        completer.update_tools([t.name for t in tools_result.tools])
    except Exception:
        pass

    # Pre-fetch prompt names for completion
    try:
        prompts_result = await session.list_prompts()
        completer.update_prompts([p.name for p in prompts_result.prompts])
    except Exception:
        pass

    # Pre-fetch resource URIs for completion
    try:
        resources_result = await session.list_resources()
        completer.update_resources([str(r.uri) for r in resources_result.resources])
    except Exception:
        pass

    # Set up prompt session with history
    history_file = Path.home() / ".mcp_repl_history"
    prompt_session: PromptSession[str] = PromptSession(
        history=FileHistory(str(history_file)),
        completer=completer,
        style=PROMPT_STYLE,
        complete_while_typing=False,  # Only complete on Tab
    )

    while True:
        try:
            command = await prompt_session.prompt_async(
                [("class:prompt", "mcp"), ("", "> ")],
            )
            command = command.strip()

            if not command:
                continue

            parts = command.split(maxsplit=1)
            cmd = parts[0].lower()
            rest = parts[1] if len(parts) > 1 else ""

            if cmd in ("quit", "exit", "q"):
                console.print("[dim]Goodbye![/dim]")
                break

            elif cmd == "help":
                print_help()

            elif cmd == "tools":
                await list_tools(session)

            elif cmd == "tool":
                if not rest:
                    console.print("[red]Usage: tool <tool_name>[/red]")
                    continue
                await show_tool(session, rest.strip())

            elif cmd == "prompts":
                await list_prompts(session)

            elif cmd == "resources":
                await list_resources(session)

            elif cmd == "call":
                if not rest:
                    console.print("[red]Usage: call <tool_name> [json_args][/red]")
                    continue
                tool_parts = rest.split(maxsplit=1)
                tool_name = tool_parts[0]
                args_str = tool_parts[1] if len(tool_parts) > 1 else ""
                await call_tool(session, tool_name, args_str)

            elif cmd == "prompt":
                if not rest:
                    console.print("[red]Usage: prompt <prompt_name> [json_args][/red]")
                    continue
                prompt_parts = rest.split(maxsplit=1)
                prompt_name = prompt_parts[0]
                args_str = prompt_parts[1] if len(prompt_parts) > 1 else ""
                await get_prompt(session, prompt_name, args_str)

            elif cmd == "read":
                if not rest:
                    console.print("[red]Usage: read <resource_uri>[/red]")
                    continue
                await read_resource(session, rest)

            elif cmd == "info":
                await show_info(session)

            else:
                console.print(f"[red]Unknown command: {cmd}[/red]")
                console.print("[dim]Type 'help' for available commands[/dim]")

        except KeyboardInterrupt:
            console.print("\n[dim]Use 'quit' to exit[/dim]")
        except EOFError:
            console.print("\n[dim]Goodbye![/dim]")
            break


async def main():
    parser = argparse.ArgumentParser(
        description="MCP REPL - Interactive Model Context Protocol Tester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Stdio transport (command)
  %(prog)s --command "npx mcp-remote https://mcp.atlassian.com/v1/mcp"
  %(prog)s -c "uvx mcp-server-sqlite --db-path test.db"

  # HTTP transport (URL with optional headers)
  %(prog)s --url https://api.githubcopilot.com/mcp/ --header "Authorization: Bearer TOKEN"
  %(prog)s -u https://mcp.notion.com/mcp

  # Config file
  %(prog)s --config mcp-config.json
  %(prog)s --config mcp-config.json --server github
        """,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "-c",
        "--command",
        help="Command to launch the MCP server (stdio transport)",
    )
    group.add_argument(
        "-u",
        "--url",
        help="URL of the MCP server (HTTP/SSE transport)",
    )
    group.add_argument(
        "--config",
        help="Path to MCP config file (JSON or YAML)",
    )

    parser.add_argument(
        "-H",
        "--header",
        action="append",
        dest="headers",
        metavar="HEADER",
        help="HTTP header in 'Key: Value' format (can be repeated)",
    )
    parser.add_argument(
        "-t",
        "--transport",
        choices=["sse", "http"],
        default="sse",
        help="HTTP transport type: 'sse' (Server-Sent Events) or 'http' (Streamable HTTP)",
    )
    parser.add_argument(
        "-s",
        "--server",
        help="Server name to use from config file (if multiple servers defined)",
    )

    args = parser.parse_args()

    print_banner()
    console.print()

    # Parse configuration
    try:
        if args.command:
            transport = parse_command(args.command)
            console.print("[dim]Transport: stdio[/dim]")
            console.print(f"[dim]Command: {args.command}[/dim]")
        elif args.url:
            transport_type = "streamable_http" if args.transport == "http" else "sse"
            transport = parse_url(args.url, args.headers, transport_type)
            transport_label = (
                "Streamable HTTP" if transport_type == "streamable_http" else "SSE"
            )
            console.print(f"[dim]Transport: {transport_label}[/dim]")
            console.print(f"[dim]URL: {args.url}[/dim]")
            if transport.headers:
                console.print(
                    f"[dim]Headers: {len(transport.headers)} configured[/dim]"
                )
        else:
            transport = load_config(args.config, args.server)
            console.print(f"[dim]Config: {args.config}[/dim]")
            if isinstance(transport, HttpTransport):
                console.print("[dim]Transport: HTTP/SSE[/dim]")
                console.print(f"[dim]URL: {transport.url}[/dim]")
            else:
                console.print("[dim]Transport: stdio[/dim]")
    except Exception as e:
        console.print(f"[red]Configuration error: {e}[/red]")
        sys.exit(1)

    console.print()
    console.print("[yellow]Connecting to MCP server...[/yellow]")
    console.print("[dim](Look for browser popup if using OAuth-based MCP)[/dim]")
    console.print()

    try:
        async with connect_transport(transport) as session:
            console.print("[green]✓ Connected to MCP server[/green]")
            console.print()

            await repl_loop(session)

    except BaseException as e:
        # Handle ExceptionGroup from TaskGroup (Python 3.11+) or regular exceptions
        if hasattr(e, "exceptions"):
            console.print("[red]Failed to connect to MCP server:[/red]")
            exceptions = getattr(e, "exceptions")
            for i, exc in enumerate(exceptions, 1):
                console.print(f"[red]  {i}. {type(exc).__name__}: {exc}[/red]")
        else:
            console.print(
                f"[red]Failed to connect to MCP server: {type(e).__name__}: {e}[/red]"
            )
        sys.exit(1)


def main_sync():
    """Synchronous entry point for console script."""
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
