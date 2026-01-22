import json
import os
import re
import uuid
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any

import streamlit as st

# Optional Gemini
try:
    from google import genai
except Exception:
    genai = None


# -----------------------------
# Storage (local file). Works on Streamlit Cloud too.
# -----------------------------
APP_DIR = Path(".")  # keep inside app folder for Streamlit Cloud
DATA_PATH = APP_DIR / "tasks.json"

CATEGORIES = ["Money", "Family", "Nova", "School", "Health", "Admin", "Career", "Other"]
BUCKETS = ["Do now", "Stabilize", "Plan next", "Park"]
STATUS = ["Open", "Done"]


@dataclass
class Task:
    id: str
    raw: str
    title: str
    category: str
    due_date: Optional[str]  # YYYY-MM-DD
    urgency: int             # 1-5
    blocked: bool
    next_step: str
    bucket: str
    status: str
    created_at: str


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_storage() -> None:
    if not DATA_PATH.exists():
        DATA_PATH.write_text(json.dumps({"tasks": []}, indent=2), encoding="utf-8")


def load_tasks() -> List[Task]:
    ensure_storage()
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    return [Task(**t) for t in data.get("tasks", [])]


def save_tasks(tasks: List[Task]) -> None:
    DATA_PATH.write_text(json.dumps({"tasks": [asdict(t) for t in tasks]}, indent=2), encoding="utf-8")


# -----------------------------
# Basic parsing (fallback if no Gemini)
# -----------------------------
def parse_due_date(text: str) -> Optional[str]:
    t = text.strip().lower()
    today = date.today()

    if "today" in t:
        return today.isoformat()
    if "tomorrow" in t:
        return (today + timedelta(days=1)).isoformat()

    m = re.search(r"in\s+(\d{1,2})\s+days?", t)
    if m:
        return (today + timedelta(days=int(m.group(1)))).isoformat()

    m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", t)
    if m:
        return m.group(1)

    # "Jan 29" or "due Jan 29"
    month = {
        "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
        "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
        "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10,
        "nov": 11, "november": 11, "dec": 12, "december": 12
    }
    m = re.search(r"\b(?:due\s+)?([a-z]{3,9})\s+(\d{1,2})(?:\b|,)\s*(\d{4})?\b", t)
    if m:
        mon = month.get(m.group(1))
        day = int(m.group(2))
        yr = int(m.group(3)) if m.group(3) else today.year
        if mon:
            try:
                return date(yr, mon, day).isoformat()
            except Exception:
                return None

    return None


def guess_category(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ["rent", "utility", "utilities", "bill", "owe", "payment", "pay", "$", "money", "tuition"]):
        return "Money"
    if any(k in t for k in ["mom", "mother", "brother", "sister", "family", "dad"]):
        return "Family"
    if any(k in t for k in ["nova", "client", "students", "deliverable", "proposal", "follow up", "follow-up", "lead"]):
        return "Nova"
    if any(k in t for k in ["assignment", "exam", "class", "professor", "homework"]):
        return "School"
    if any(k in t for k in ["doctor", "sleep", "health", "dentist", "gym"]):
        return "Health"
    if any(k in t for k in ["dmv", "paperwork", "form", "visa", "opt", "ssn", "admin"]):
        return "Admin"
    return "Other"


def default_urgency(category: str, due: Optional[str], text: str) -> int:
    # Base urgency by category
    base = {"Money": 5, "Nova": 5, "Family": 4, "School": 4, "Health": 3, "Admin": 3, "Career": 3, "Other": 2}.get(category, 2)

    # If due date is soon, bump
    if due:
        try:
            d = date.fromisoformat(due)
            days = (d - date.today()).days
            if days <= 0:
                return 5
            if days <= 3:
                return max(base, 5)
            if days <= 7:
                return max(base, 4)
        except Exception:
            pass

    # If contains "asap" or "urgent"
    if re.search(r"\basap\b|\burgent\b|\bdue\b", text.lower()):
        return min(5, max(base, 4))

    return min(5, max(1, base))


def default_blocked(category: str, text: str) -> bool:
    t = text.lower()
    # money debts often not solvable instantly -> treat as "blocked" unless it's a direct bill with due date
    if category == "Money" and any(k in t for k in ["owe", "pay back", "payback", "need to give", "need to pay"]):
        return True
    # family overwhelm -> not "blocked" but we want "container" actions, which behave like stabilize
    if category == "Family" and any(k in t for k in ["mom", "mother"]):
        return True
    return False


def next_step_template(category: str, text: str) -> str:
    t = text.lower()

    if category == "Money":
        if any(k in t for k in ["rent", "utilities", "bill", "due"]):
            return "Send one message/call to confirm payment timing or ask for a short extension (10 min)."
        return "Text the person: propose a date + partial payment if possible (10 min)."

    if category == "Family":
        # container action
        return "Send boundary message: set 2 fixed call times this week, 20 min each (10 min)."

    if category == "Nova":
        return "2-min start: open the doc/list, write the first ugly outline. Then 45-min focused sprint."

    if category == "School":
        return "Write the next 3 micro-steps and do the first 15 minutes immediately."

    return "Write the smallest 15-min next action and do it now."


def bucket_rule(category: str, urgency: int, blocked: bool) -> str:
    # Simple + practical: urgent + actionable -> Do now, urgent + blocked -> Stabilize
    if urgency >= 4:
        return "Stabilize" if blocked else "Do now"
    if urgency == 3:
        return "Plan next"
    return "Park"


# -----------------------------
# Gemini integration (optional)
# -----------------------------
def get_api_key() -> Optional[str]:
    # Streamlit Cloud: use secrets
    key = None
    if "GEMINI_API_KEY" in st.secrets:
        key = str(st.secrets["GEMINI_API_KEY"]).strip()
    if not key:
        key = (os.getenv("GEMINI_API_KEY") or "").strip()
    return key or None


def gemini_available() -> bool:
    return genai is not None and get_api_key() is not None


def gemini_triage(lines: List[str], model: str) -> Optional[List[Dict[str, Any]]]:
    """
    Returns a list of dicts:
    {title, category, due_date, urgency, blocked, next_step}
    """
    if not gemini_available():
        return None

    client = genai.Client(api_key=get_api_key())

    prompt = f"""
You are a stress triage assistant. Turn each line into a structured task.

Return ONLY valid JSON (no markdown), as an array of objects.
Each object must include:
- title: short concrete title
- category: one of {CATEGORIES}
- due_date: "YYYY-MM-DD" or null
- urgency: integer 1-5 (5 = very urgent)
- blocked: boolean (true if not solvable now; then suggest stabilize step)
- next_step: a specific action that fits in <= 15 minutes

Lines:
{json.dumps(lines, ensure_ascii=False)}
""".strip()

    try:
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
        )
        # Try to parse JSON from response text
        text = resp.text.strip()
        # Sometimes models include extra text; extract JSON array if needed
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1:
            text = text[start : end + 1]
        data = json.loads(text)
        if not isinstance(data, list):
            return None
        out = []
        for obj in data:
            if not isinstance(obj, dict):
                continue
            out.append(obj)
        return out
    except Exception:
        return None


# -----------------------------
# Streamlit UI (minimal)
# -----------------------------
st.set_page_config(page_title="Stress Triage", layout="wide")
st.title("Stress Triage")
st.caption("Paste everything. Click triage. Get a clear plan.")

tasks: List[Task] = load_tasks()

col1, col2 = st.columns([2, 1])
with col2:
    panic_mode = st.toggle("Panic mode", value=True)
    st.caption("Panic mode shows only the smallest plan.")
    use_gemini = st.toggle("Use Gemini (auto)", value=True, disabled=not gemini_available())
    model = st.selectbox("Model", ["gemini-2.0-flash", "gemini-2.5-flash"], index=0, disabled=not gemini_available())
    if not gemini_available():
        st.caption("Gemini off: set GEMINI_API_KEY in Streamlit secrets or env.")

with col1:
    st.subheader("Dump list")
    dump = st.text_area(
        "One per line",
        height=160,
        placeholder="Example:\n$1200 rent + utilities due Jan 29\nNeed to pay friend $1000 ASAP\nNova: finish 5 students applications\nCall mom (overwhelming)",
    )
    cA, cB, cC = st.columns([1, 1, 1])
    with cA:
        triage_btn = st.button("Triage + add", use_container_width=True)
    with cB:
        clear_btn = st.button("Clear input", use_container_width=True)
    with cC:
        reset_btn = st.button("Clear all saved tasks", use_container_width=True)

    if clear_btn:
        st.rerun()

    if reset_btn:
        save_tasks([])
        st.success("Cleared.")
        st.rerun()

    if triage_btn:
        lines = [ln.strip() for ln in dump.splitlines() if ln.strip()]
        if not lines:
            st.warning("Paste at least one line.")
        else:
            ai = gemini_triage(lines, model=model) if (use_gemini and gemini_available()) else None

            for i, ln in enumerate(lines):
                if ai and i < len(ai):
                    obj = ai[i]
                    title = str(obj.get("title") or ln).strip()
                    category = str(obj.get("category") or guess_category(ln)).strip()
                    if category not in CATEGORIES:
                        category = guess_category(ln)
                    due = obj.get("due_date")
                    due = str(due).strip() if isinstance(due, str) and due.strip() else None
                    urgency = int(obj.get("urgency") or 3)
                    urgency = max(1, min(5, urgency))
                    blocked = bool(obj.get("blocked")) if obj.get("blocked") is not None else default_blocked(category, ln)
                    next_step = str(obj.get("next_step") or next_step_template(category, ln)).strip()
                else:
                    category = guess_category(ln)
                    due = parse_due_date(ln)
                    urgency = default_urgency(category, due, ln)
                    blocked = default_blocked(category, ln)
                    title = ln.strip()
                    next_step = next_step_template(category, ln)

                bucket = bucket_rule(category, urgency, blocked)

                tasks.insert(
                    0,
                    Task(
                        id=str(uuid.uuid4())[:8],
                        raw=ln,
                        title=title,
                        category=category,
                        due_date=due,
                        urgency=urgency,
                        blocked=blocked,
                        next_step=next_step,
                        bucket=bucket,
                        status="Open",
                        created_at=now_iso(),
                    ),
                )

            save_tasks(tasks)
            st.success(f"Added {len(lines)} item(s). Go below for your plan.")
            st.rerun()


# -----------------------------
# Output
# -----------------------------
open_tasks = [t for t in tasks if t.status == "Open"]

# Sort: bucket first, urgency desc, created_at desc
bucket_order = {"Do now": 0, "Stabilize": 1, "Plan next": 2, "Park": 3}
open_tasks.sort(key=lambda t: (bucket_order.get(t.bucket, 9), -t.urgency, t.created_at), reverse=False)

do_now = [t for t in open_tasks if t.bucket == "Do now"]
stabilize = [t for t in open_tasks if t.bucket == "Stabilize"]
plan_next = [t for t in open_tasks if t.bucket == "Plan next"]
park = [t for t in open_tasks if t.bucket == "Park"]


def render_task_row(t: Task):
    left, right = st.columns([6, 1])
    with left:
        due = f" | due {t.due_date}" if t.due_date else ""
        st.write(f"**[{t.category}] {t.title}**  (urgency {t.urgency}/5{due})")
        st.write(f"- {t.next_step}")
    with right:
        if st.button("Done", key=f"done_{t.id}"):
            t.status = "Done"
            save_tasks(tasks)
            st.rerun()


st.divider()

if panic_mode:
    st.subheader("Today (Top 3 only)")
    top = (do_now + stabilize + plan_next)[:3]
    if not top:
        st.info("No open items. Add lines above.")
    else:
        for t in top:
            render_task_row(t)
else:
    st.subheader("Do now")
    if not do_now:
        st.write("Nothing urgent + actionable right now.")
    else:
        for t in do_now[:6]:
            render_task_row(t)

    st.subheader("Stabilize (urgent but not solvable fast)")
    if not stabilize:
        st.write("None.")
    else:
        for t in stabilize[:8]:
            render_task_row(t)

    st.subheader("Plan next")
    if not plan_next:
        st.write("None.")
    else:
        for t in plan_next[:10]:
            st.write(f"- **[{t.category}] {t.title}**")

    with st.expander("Parked (low priority noise)"):
        if not park:
            st.write("None.")
        else:
            for t in park[:20]:
                st.write(f"- **[{t.category}] {t.title}**")


with st.expander("Advanced: Export + Gemini key setup"):
    st.write(f"Saved file: `{DATA_PATH.resolve()}`")
    st.download_button(
        "Download tasks.json",
        data=json.dumps({"tasks": [asdict(t) for t in tasks]}, indent=2),
        file_name="tasks.json",
        mime="application/json",
        use_container_width=True,
    )
    st.code(
        """# Streamlit Cloud (recommended):
# Settings -> Secrets
# Add:
# GEMINI_API_KEY="YOUR_KEY"

# Local:
export GEMINI_API_KEY="YOUR_KEY"
""",
        language="bash",
    )
