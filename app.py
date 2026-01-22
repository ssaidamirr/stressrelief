import json
import os
import re
import uuid
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import streamlit as st

# Optional AI (Gemini)
try:
    from google import genai
except Exception:
    genai = None  # app still works without AI

from pydantic import BaseModel, Field, ValidationError


# -----------------------------
# Storage
# -----------------------------
APP_DIR = Path.home() / ".stress_triage"
DATA_PATH = APP_DIR / "tasks.json"


CATEGORIES = ["Money", "Family", "Nova", "School", "Health", "Admin", "Career", "Other"]
STATUSES = ["Open", "Done", "Parked"]


@dataclass
class Task:
    id: str
    title: str
    category: str
    due_date: Optional[str]  # YYYY-MM-DD or None
    importance: int          # 1-5
    consequence: int         # 1-5
    actionability: int       # 1-5
    mental_load: int         # 1-5
    effort: int              # 1-5
    blocked: bool
    next_step_15m: str
    pinned: bool
    status: str
    created_at: str          # ISO datetime

    def due_as_date(self) -> Optional[date]:
        if not self.due_date:
            return None
        try:
            return date.fromisoformat(self.due_date)
        except Exception:
            return None


def ensure_storage() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_PATH.exists():
        DATA_PATH.write_text(json.dumps({"tasks": []}, indent=2), encoding="utf-8")


def load_tasks() -> List[Task]:
    ensure_storage()
    raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    tasks = []
    for t in raw.get("tasks", []):
        tasks.append(Task(**t))
    return tasks


def save_tasks(tasks: List[Task]) -> None:
    ensure_storage()
    payload = {"tasks": [asdict(t) for t in tasks]}
    DATA_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# -----------------------------
# Helpers
# -----------------------------
def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def norm_1_5(v: int) -> float:
    v = max(1, min(5, int(v)))
    return (v - 1) / 4.0


def parse_due_date_quick(text: str) -> Optional[str]:
    """
    Lightweight parser for:
      - "due 2026-01-31"
      - "due Jan 31"
      - "tomorrow", "today"
      - "in 3 day

