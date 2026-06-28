# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**nanobot** is a lightweight personal AI assistant framework — a multi-channel agentic system (Telegram, WhatsApp) with pluggable tools and skills. Python 3.11+, fully async.

## Build & Install

```bash
pip install -e .          # install in editable mode
nanobot --help            # verify CLI entry point works
```

Build system: Hatchling (PEP 517). No Makefile.

## Test & Lint

```bash
pytest                    # run tests (tests/ dir — currently empty, alpha project)
ruff check .              # lint
ruff format .             # format (line length: 100)
```

Pytest async mode is `auto` (configured in pyproject.toml) — no `@pytest.mark.asyncio` needed.

## Code Style

- Line length: **100 characters** (not the ruff default of 88)
- All I/O must be **async/await** — never use blocking calls (`requests`, `open()`, `time.sleep()`)
- Logging via **loguru**, not stdlib `logging`
- Validation via **Pydantic v2** models

## Architecture

```
MessageBus (async queue)
    ├── Channels (Telegram, WhatsApp) → inbound
    └── AgentLoop → LLMProvider (LiteLLM) → ToolRegistry → outbound
```

Key modules:
- `nanobot/agent/loop.py` — core agentic loop (`AgentLoop`)
- `nanobot/agent/tools/registry.py` — tool registration
- `nanobot/bus/queue.py` — async message bus
- `nanobot/providers/litellm_provider.py` — LLM abstraction (supports 100+ models via LiteLLM)
- `nanobot/session/manager.py` — JSONL-based session persistence
- `nanobot/cli/commands.py` — all CLI commands (Typer)

## Adding Tools

New tools must subclass the base in `nanobot/agent/tools/base.py` and be explicitly registered in `ToolRegistry`. Creating a file alone is not enough.

## Skills System

Each skill lives in `nanobot/skills/<skill-name>/` and contains:
- `SKILL.md` — description loaded and shown to the agent at runtime
- `skill.py` or `*.sh` — implementation

Skills are dynamically loaded by `AgentLoop._register_skills()`.

## Configuration

- Config file: `~/.nanobot/config.json` (created by `nanobot onboard`)
- Workspace: `~/.nanobot/workspace/` (contains memory, sessions, HEARTBEAT.md, SOUL.md)
- Env var overrides: `NANOBOT_*` prefix with `__` for nesting (e.g. `NANOBOT_PROVIDERS__OPENROUTER__API_KEY`)
- Provider priority: OpenRouter > Anthropic > OpenAI

## WhatsApp Bridge

WhatsApp integration requires a separate Node.js 18+ process (`bridge/`) running alongside the Python server. It communicates via WebSocket. Start it independently before running `nanobot gateway`.

## Key Gotchas

- No tests exist yet — write them in `tests/` when adding new functionality
- Max tool iterations default is 20 (prevents infinite loops)
- Shell tool has a 60-second timeout and 10k character output limit
- Session keys use format `channel:chat_id`; history stored as append-only JSONL
- The heartbeat service checks `workspace/HEARTBEAT.md` every 30 minutes for periodic tasks
