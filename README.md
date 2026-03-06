# Watson API

Task and context API for [Backyard Brains](https://backyardbrains.com). Exposes projects, tasks, events, and an inbox via REST and [MCP](https://modelcontextprotocol.io).

**Base URL:** https://watson.backyardbrains.com

## Auth

- **REST:** `?secret=<API_SECRET>` query param
- **MCP:** One of:
  - `Authorization: Bearer <API_SECRET>` header
  - `X-API-Key: <API_SECRET>` header  
  - `?key=<API_SECRET>` query param (for ChatGPT connector—connector can't pass headers)
  - `privateKey` in tool arguments (blocked by ChatGPT safety layer)

Set `API_SECRET` or `BACKYARDBRAINS_API_KEY` in the environment.

## Data Source

When `USE_MEMORY_API=true` (default), ontology and inbox come from [memory.backyardbrains.com](https://github.com/BackyardBrains/memory.backyardbrains.com). Otherwise uses local JSON-LD files.

## Quick Start

```bash
pip install -r requirements.txt
export API_SECRET=your_secret
export USE_MEMORY_API=true
export MEMORY_API_URL=https://memory.backyardbrains.com
export MEMORY_API_KEY=your_memory_api_key
uvicorn main:app --host 0.0.0.0 --port 8080
```

## Docker

```bash
docker build -t watson-api .
docker run -p 8080:8080 \
  -e API_SECRET=your_secret \
  -e MEMORY_API_URL=https://memory.backyardbrains.com \
  -e MEMORY_API_KEY=your_key \
  watson-api
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `get_summary` | Daily digest: projects, open tasks, upcoming events |
| `get_projects` | List all projects |
| `get_tasks` | List tasks (filter by project, status) |
| `get_events` | List events in date range |
| `get_links` | Document links for projects |
| `set_task_status` | Update task status |
| `post_inbox` | Append note for Watson to reconcile |

## ChatGPT Integration

Add Watson as an MCP connector with URL:

```
https://watson.backyardbrains.com/mcp?key=<API_SECRET>
```

The key in the URL authenticates requests—ChatGPT cannot pass it in tool arguments due to platform safety restrictions.
