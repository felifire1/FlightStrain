# Secure API Key Setup

The FlightStrain app uses a secure local file to store your Claude API key instead of environment variables. This keeps credentials out of `.env` files and shell history.

## Setup

### 1. Create the secure directory

```bash
mkdir -p ~/.claude
chmod 700 ~/.claude
```

### 2. Store your API key

Replace `YOUR_API_KEY_HERE` with your actual Claude API key:

```bash
echo "YOUR_API_KEY_HERE" > ~/.claude/api_key
chmod 600 ~/.claude/api_key
```

**File permissions matter:**
- `~/.claude/` should be `700` (readable/writable/executable only by you)
- `~/.claude/api_key` should be `600` (readable/writable only by you)

### 3. Verify it works

Run the agent:
```bash
cd ~/Documents/FlightStrain
.venv/bin/python -m agent.loop "What's the weather in Boston?"
```

## How it works

**Load order:**
1. Tries to read `~/.claude/api_key` (local file, never committed)
2. Falls back to `ANTHROPIC_API_KEY` environment variable
3. Raises error if neither found

## Updating your key

When ASI hands you their token at the door, just overwrite the file:

```bash
echo "ASI_TOKEN_HERE" > ~/.claude/api_key
```

Restart the uvicorn server and it will pick up the new token automatically.

## Why local file instead of .env?

- **Security**: `.env` files sometimes get committed by accident. Local file is outside the repo.
- **Portability**: Multiple clones of the repo can share the same key file without duplication.
- **Shell history**: Environment variables can leak in shell history. File-based loading doesn't.
- **Production-ready**: Mirrors how real apps manage secrets (K8s mounts, EC2 metadata, etc.)
