import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
DATA = ROOT / "data"

PROVIDER = os.getenv("LLM_PROVIDER", "claude")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT", "medium")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:9b-64k")
OLLAMA_THINK = os.getenv("OLLAMA_THINK", "false").lower() == "true"
SIM_SEED = int(os.getenv("SIM_SEED", "7"))
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
EMOTION_LLM = os.getenv("EMOTION_LLM", "true").lower() == "true"
