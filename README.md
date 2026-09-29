# nanobanana-mcp-docker

Two ways to run [Nano Banana MCP server](https://github.com/ConechoAI/Nano-Banana-MCP):

1. **Local Docker / stdio** (this file) — `docker run` spawned by Claude Code.
2. **AWS Lambda / HTTP** ([lambda/README.md](lambda/README.md)) — hosted remote
   MCP, no per-machine setup. Recommended if you move between machines often.

## What this does

The upstream nanobanana MCP server generates images inside the Docker container. This wrapper adds MCP `instructions` telling the AI agent to automatically copy generated files from the host-mounted output directory to the user's working directory.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes | Google Gemini API key |
| `HOST_OUTPUT_DIR` | No | Host path where `/output` is mounted (default: `c:/tmp`) |
| `IMAGE_OUTPUT_DIR` | No | Container-internal output path (default: `/output`) |
| `LOG_LEVEL` | No | Log level (default: `INFO`) |

## Usage with Claude Code

### .mcp.json

```json
{
  "mcpServers": {
    "nanobanana": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-v", "${HOST_OUTPUT_DIR}:/output",
        "-e", "GEMINI_API_KEY",
        "-e", "LOG_LEVEL=ERROR",
        "-e", "IMAGE_OUTPUT_DIR=/output",
        "-e", "HOST_OUTPUT_DIR",
        "hasshi7965/nanobanana-mcp"
      ],
      "type": "stdio"
    }
  }
}
```

### settings.json

Set `GEMINI_API_KEY` and `HOST_OUTPUT_DIR` in the `env` section:

```json
{
  "env": {
    "GEMINI_API_KEY": "your-gemini-api-key",
    "HOST_OUTPUT_DIR": "c:/tmp"
  }
}
```

## Build (Multi-Arch: amd64 + arm64)

```bash
# One-time setup for buildx (if needed)
docker buildx create --name multiarch --use
docker buildx inspect --bootstrap

# Build and push a multi-arch manifest
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t hasshi7965/nanobanana-mcp:latest \
  --push \
  .
```

## Verify published platforms

```bash
docker buildx imagetools inspect hasshi7965/nanobanana-mcp:latest
```

You should see both `linux/amd64` and `linux/arm64` in the manifest.

## Optional local platform test

```bash
# Run arm64 image explicitly
docker run --rm --platform linux/arm64 -i hasshi7965/nanobanana-mcp:latest

# Run amd64 image explicitly
docker run --rm --platform linux/amd64 -i hasshi7965/nanobanana-mcp:latest
```

## Shortcut script

```bash
# Publish latest
./build-multiarch.sh

# Publish a version tag
./build-multiarch.sh v0.1.0
```
