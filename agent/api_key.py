"""Secure API key loader from local file instead of environment variables."""
import os
from pathlib import Path


def get_anthropic_api_key() -> str:
    """Load Claude API key from local file.

    Tries (in order):
    1. ~/.claude/api_key — recommended, secure local file
    2. ANTHROPIC_API_KEY environment variable — fallback for .env

    Returns:
        The API key string.

    Raises:
        FileNotFoundError: If key file doesn't exist and env var not set.
    """
    key_file = Path.home() / ".claude" / "api_key"

    # Try local file first
    if key_file.exists():
        key = key_file.read_text().strip()
        if key:
            return key

    # Fallback to environment variable
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key

    raise FileNotFoundError(
        f"Claude API key not found.\n"
        f"Either:\n"
        f"  1. Create {key_file} with your API key, or\n"
        f"  2. Set ANTHROPIC_API_KEY environment variable"
    )
