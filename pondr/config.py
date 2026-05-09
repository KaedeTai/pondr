"""Central config — reads .env, exposes typed settings."""
from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("PONDR_DATA_DIR") or ROOT / "data")
LOG_DIR = DATA_DIR / "logs"
RAW_DIR = DATA_DIR / "raw"
CHROMA_DIR = DATA_DIR / "chroma"

# Load .env from project root (silent if missing)
load_dotenv(ROOT / ".env")

LLM_BASE_URL = os.getenv("PONDR_LLM_BASE_URL", "http://127.0.0.1:9080/v1")
LLM_API_KEY = os.getenv("PONDR_LLM_API_KEY", "local")
LLM_MODEL = os.getenv("PONDR_LLM_MODEL", "gemma-4-31B-it-Q4_K_M.gguf")

DASHBOARD_PORT = int(os.getenv("PONDR_DASHBOARD_PORT", "8090"))
WS_PORT = int(os.getenv("PONDR_WS_PORT", "8765"))

INITIAL_TOPIC = os.getenv("PONDR_INITIAL_TOPIC", "量化交易策略與市場結構研究")

FRED_API_KEY = os.getenv("FRED_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CHANNELS_ENABLED = [c.strip() for c in os.getenv("PONDR_CHANNELS", "ws,stdio").split(",") if c.strip()]
if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and "telegram" not in CHANNELS_ENABLED:
    CHANNELS_ENABLED.append("telegram")

DB_KB = DATA_DIR / "research_kb.db"
DB_TICKS = DATA_DIR / "market_ticks.db"

LLM_LOG_PATH = LOG_DIR / "llm_io.jsonl"
PREFS_MD_PATH = DATA_DIR / "preferences.md"
EVENT_LOG_PATH = LOG_DIR / "events.jsonl"

for d in (DATA_DIR, LOG_DIR, RAW_DIR, CHROMA_DIR):
    d.mkdir(parents=True, exist_ok=True)

BIND_HOST = os.getenv("PONDR_BIND_HOST", "0.0.0.0")
