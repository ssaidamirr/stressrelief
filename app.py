import json
import os
import re
import uuid
from dataclasses import dataclass, asdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

import streamlit as st

# Optional Gemini
try:
    from google import genai
except Exception:
    genai = None

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # py<3.9

TZ_NAME = "America/New_York"

# -----------------------------
# Storage
# -----------------------------
APP_DIR = Path(".")
TASKS_PATH = APP_DIR / "tasks.json"
PLAN_PATH = APP_DIR / "today_plan.json"

CATEGORIES = ["Money", "Family", "Nova", "School", "Health", "Admin", "Career", "Other"]
BUCKETS = ["Do now", "Stabilize", "Plan next", "Park"]


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
    status: str              # Open/Done
    created_at: str          # ISO datetime


def tz_now() -> datetime:
    if ZoneInfo is None:
        return datetime.now()
    return datetime.now(ZoneInfo(TZ_NAME))


def today_est() -> date:
    return tz_now().date()


def now_iso() -> str:
    return tz_now().isoformat(timespec="seconds")


def ensure_storage() -> None:
    if not TASKS_PATH.exists():
        TASKS_PATH.write_text(json.dumps({"tasks": []}, indent=2), encoding="utf-8")


def load_tasks() -> List[Task]:
    ensure_storage()
    data = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
    return [Task(**t) for t in data.get("tasks", [])]


def save_tasks(tasks: List[Task]) -> None:
    TASKS_PATH.write_text(json.dumps({"tasks": [asdict(t) for t in tasks]}, indent=2), encoding="utf-8")


def load_plan() -> Optional[Dict[str, Any]]:
    if not PLAN_PATH.exists():
        return None
    try:
        return json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_plan(plan: Dict[str, Any]) -> None:
    PLAN_PATH.write_text(json.dumps(plan, indent=2), encoding="utf-8")


# -----------------------------
# Parsing + defaults (fallback)
# -----------------------------
def parse_due_date(text: str) -> Optional[str]:
    t = text.strip().lower()
    td = today_est()

    if "today" in t:
        return td.isoformat()
    if "tomorrow" in t:
        return (td + timedelta(days=1)).isoformat()

    m = re.search(r"in\s+(\d{1,2})\s+days?", t)
    if m:
        return (td + timedelta(days=int(m.group(1)))).isoformat()

    m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", t)
    if m:
        return m.group(1)

    month = {
        "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
        "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
        "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12
    }
    m = re.search(r"\b(?:due\s+)?([a-z]{3,9})\s+(\d{1,2})(?:\b|,)\s*(\d{4})?\b", t)
    if m:
        mon = month.get(m.group(1))
        day = int(m.group(2))
        yr = int(m.group(3)) if m.group(3) else td.year
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
    base = {"Money": 5, "Nova": 5, "Family": 4, "School": 4, "Health": 3, "Admin": 3, "Career": 3, "Other": 2}.get(category, 2)

    if due:
        try:
            d = date.fromisoformat(due)
            days = (d - today_est()).days
            if days <= 0:
                return 5
            if days <= 3:
                return max(base, 5)
            if days <= 7:
                return max(base, 4)
        except Exception:
            pass

    if re.search(r"\basap\b|\burgent\b|\bdue\b", text.lower()):
        return min(5, max(base, 4))

    return min(5, max(1, base))


def default_blocked(category: str, text: str) -> bool:
    t = text.lower()
    # Debts and "pay back" often need timing -> stabilize
    if category == "Money" and any(k in t for k in ["owe", "pay back", "payback", "need to give", "need to pay"]):
        return True
    # Mom overwhelm -> stabilize with container action
    if category == "Family" and any(k in t for k in ["mom", "mother"]):
        return True
    return False


def next_step_template(category: str, text: str) -> str:
    t = text.lower()
    if category == "Money":
        if any(k in t for k in ["rent", "utilities", "bill", "due"]):
            return "Send one message/call: confirm payment timing or ask for short extension (10 min)."
        return "Text the person: propose exact date + partial payment if possible (10 min)."
    if category == "Family":
        return "Send boundary message: set 2 fixed call times this week, 20 min each (10 min)."
    if category == "Nova":
        return "2-min start: open the doc/list and outline. Then 45-min focused sprint."
    if category == "School":
        return "Write the next 3 micro-steps and do the first 15 minutes now."
    return "Write the smallest 15-min next action and do it now."


def bucket_rule(category: str, urgency: int, blocked: bool) -> str:
    if urgency >= 4:
        return "Stabilize" if blocked else "Do now"
    if urgency == 3:
        return "Plan next"
    return "Park"


# -----------------------------
# Gemini (optional)
# -----------------------------
def get_api_key() -> Optional[str]:
    key = None
    if "GEMINI_API_KEY" in st.secrets:
        key = str(st.secrets["GEMINI_API_KEY"]).strip()
    if not key:
        key = (os.getenv("GEMINI_API_KEY") or "").strip()
    return key or None


def gemini_available() -> bool:
    return genai is not None and get_api_key() is not None


def gemini_triage(lines: List[str], model: str) -> Optional[List[Dict[str, Any]]]:
    if not gemini_available():
        return None

    client = genai.Client(api_key=get_api_key())
    prompt = f"""
Return ONLY valid JSON (no markdown), as an array of objects for each line.

Fields per object:
- title: short concrete title
- category: one of {CATEGORIES}
- due_date: "YYYY-MM-DD" or null
- urgency: integer 1-5 (5 = very urgent)
- blocked: boolean
- next_step: <=15 minute action

Lines:
{json.dumps(lines, ensure_ascii=False)}
""".strip()

    try:
        resp = client.models.generate_content(model=model, contents=prompt)
        text = resp.text.strip()
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1:
            text = text[start:end + 1]
        data = json.loads(text)
        return data if isinstance(data, list) else None
    except Exception:
        return None


# -----------------------------
# Today plan snapshot (persisted)
# -----------------------------
def compute_ranked(tasks: List[Task]) -> List[Task]:
    bucket_order = {"Do now": 0, "Stabilize": 1, "Plan next": 2, "Park": 3}
    open_tasks = [t for t in tasks if t.status == "Open"]
    open_tasks.sort(key=lambda t: (bucket_order.get(t.bucket, 9), -t.urgency, t.created_at))
    return open_tasks


def generate_today_plan(tasks: List[Task]) -> Dict[str, Any]:
    ranked = compute_ranked(tasks)
    # Snapshot: store the ordered ids + minimal display fields
    plan = {
        "date": today_est().isoformat(),
        "ordered_ids": [t.id for t in ranked],
        "generated_at": now_iso(),
    }
    return plan


def get_or_create_today_plan(tasks: List[Task], force: bool = False) -> Dict[str, Any]:
    plan = load_plan()
    if force or (plan is None) or (plan.get("date") != today_est().isoformat()):
        plan = generate_today_plan(tasks)
        save_plan(plan)
    return plan


# -----------------------------
# Calendar scheduling + CSV export
# -----------------------------
def availability_blocks(start_day: date, days_ahead: int = 14) -> List[Tuple[datetime, datetime]]:
    """
    Returns available work blocks in EST for the next days_ahead days, based on:
    Mon-Fri: 19:00-21:00
    Sat: 14:00-17:00
    Sun: 19:00-21:00
    """
    tz = ZoneInfo(TZ_NAME) if ZoneInfo else None
    blocks = []
    for i in range(days_ahead):
        d = start_day + timedelta(days=i)
        dow = d.weekday()  # Mon=0 .. Sun=6

        if dow in [0, 1, 2, 3, 4]:  # Mon-Fri
            s, e = time(19, 0), time(21, 0)
        elif dow == 5:  # Sat
            s, e = time(14, 0), time(17, 0)
        else:  # Sun
            s, e = time(19, 0), time(21, 0)

        start_dt = datetime.combine(d, s)
        end_dt = datetime.combine(d, e)
        if tz:
            start_dt = start_dt.replace(tzinfo=tz)
            end_dt = end_dt.replace(tzinfo=tz)

        # If today, don't schedule in the past
        now_dt = tz_now()
        if start_dt < now_dt < end_dt:
            start_dt = now_dt.replace(second=0, microsecond=0)

        if end_dt > start_dt:
            blocks.append((start_dt, end_dt))
    return blocks


def default_duration_minutes(task: Task) -> int:
    # Keep it simple and realistic
    if task.bucket == "Do now":
        return 60
    if task.bucket == "Stabilize":
        return 30
    if task.bucket == "Plan next":
        return 30
    return 0


def schedule_into_blocks(tasks_in_order: List[Task], blocks: List[Tuple[datetime, datetime]]) -> List[Dict[str, Any]]:
    """
    Creates event segments inside blocks.
    Splits tasks if needed.
    """
    events = []
    block_i = 0
    cursor = blocks[0][0] if blocks else None

    def advance_block():
        nonlocal block_i, cursor
        block_i += 1
        if block_i >= len(blocks):
            cursor = None
            return
        cursor = blocks[block_i][0]

    for t in tasks_in_order:
        mins = default_duration_minutes(t)
        if mins <= 0:
            continue

        remaining = mins
        part = 1

        while remaining > 0 and cursor is not None:
            block_start, block_end = blocks[block_i]
            if cursor < block_start:
                cursor = block_start

            available = int((block_end - cursor).total_seconds() // 60)
            if available <= 0:
                advance_block()
                continue

            use = min(remaining, available)
            start_dt = cursor
            end_dt = cursor + timedelta(minutes=use)

            title = f"{t.category}: {t.title}"
            if mins > use:
                title = f"{title} (part {part})"

            events.append({
                "Subject": title,
                "Start Date": start_dt.strftime("%m/%d/%Y"),
                "Start Time": start_dt.strftime("%I:%M %p"),
                "End Date": end_dt.strftime("%m/%d/%Y"),
                "End Time": end_dt.strftime("%I:%M %p"),
                "All Day Event": "False",
                "Description": f"Next step: {t.next_step}",
                "Location": "",
                "Private": "True",
            })

            cursor = end_dt
            remaining -= use
            part += 1

            if cursor >= block_end:
                advance_block()

    return events


# -----------------------------
# UI
# -----------------------------
st.set_page_config(page_title="Stress Triage", layout="wide")
st.title("Stress Triage")

tasks = load_tasks()

right = st.sidebar
panic_mode = right.toggle("Panic mode (Top 3 only)", value=True)

use_gemini = right.toggle("Use Gemini", value=True, disabled=not gemini_available())
model = right.selectbox("Model", ["gemini-2.0-flash", "gemini-2.5-flash"], index=0, disabled=not gemini_available())

right.divider()
if right.button("Regenerate Today plan"):
    get_or_create_today_plan(tasks, force=True)
    st.success("Regenerated.")
    st.rerun()

if right.button("Clear all tasks"):
    tasks = []
    save_tasks(tasks)
    if PLAN_PATH.exists():
        PLAN_PATH.unlink()
    st.success("Cleared.")
    st.rerun()

st.caption("Paste everything, one per line. We auto-triage and keep your Today plan saved across restarts.")

dump = st.text_area(
    "Dump list (one per line)",
    height=140,
    placeholder="Example:\n$1200 rent + utilities due Jan 29\nNeed to pay friend $1000 ASAP\nNova: finish 5 student applications\nCall mom (overwhelming)",
)

c1, c2 = st.columns([1, 1])
with c1:
    if st.button("Triage + add", use_container_width=True):
        lines = [ln.strip() for ln in dump.splitlines() if ln.strip()]
        if not lines:
            st.warning("Paste at least one line.")
        else:
            ai = gemini_triage(lines, model=model) if (use_gemini and gemini_available()) else None

            for i, ln in enumerate(lines):
                if ai and i < len(ai) and isinstance(ai[i], dict):
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

                tasks.insert(0, Task(
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
                ))

            save_tasks(tasks)

            # Create/refresh plan only if none exists for today (keeps output stable)
            get_or_create_today_plan(tasks, force=False)

            st.success(f"Added {len(lines)} item(s).")
            st.rerun()

with c2:
    if st.button("Clear input", use_container_width=True):
        st.rerun()

st.divider()

# Apply saved Today plan order
plan = get_or_create_today_plan(tasks, force=False)
id_to_task = {t.id: t for t in tasks}
ordered = [id_to_task[tid] for tid in plan.get("ordered_ids", []) if tid in id_to_task and id_to_task[tid].status == "Open"]

# If plan ids missing (new tasks etc), append remaining open tasks at end
open_ids = {t.id for t in tasks if t.status == "Open"}
planned_ids = {t.id for t in ordered}
remaining = [t for t in compute_ranked(tasks) if t.id in open_ids and t.id not in planned_ids]
ordered.extend(remaining)

def render_task(t: Task):
    left, right = st.columns([6, 1])
    with left:
        due = f" | due {t.due_date}" if t.due_date else ""
        st.write(f"**[{t.bucket}] [{t.category}] {t.title}** (urgency {t.urgency}/5{due})")
        st.write(f"- {t.next_step}")
    with right:
        if st.button("Done", key=f"done_{t.id}"):
            t.status = "Done"
            save_tasks(tasks)
            # keep plan file; it will naturally ignore done items
            st.rerun()

# Output sections
do_now = [t for t in ordered if t.bucket == "Do now"]
stabilize = [t for t in ordered if t.bucket == "Stabilize"]
plan_next = [t for t in ordered if t.bucket == "Plan next"]
park = [t for t in ordered if t.bucket == "Park"]

if panic_mode:
    st.subheader("Today (Top 3)")
    top3 = (do_now + stabilize + plan_next)[:3]
    if not top3:
        st.info("No open items.")
    else:
        for t in top3:
            render_task(t)
else:
    st.subheader("Do now")
    for t in do_now[:10]:
        render_task(t)
    st.subheader("Stabilize")
    for t in stabilize[:10]:
        render_task(t)
    st.subheader("Plan next")
    for t in plan_next[:15]:
        render_task(t)
    with st.expander("Park"):
        for t in park[:25]:
            render_task(t)

st.divider()

# Calendar CSV export
st.subheader("Export calendar CSV (EST work blocks)")

# We schedule only the "actionable" buckets by default
sched_tasks = (do_now + stabilize + plan_next)

blocks = availability_blocks(today_est(), days_ahead=21)
events = schedule_into_blocks(sched_tasks, blocks)

if not events:
    st.write("No events to export yet (need open tasks).")
else:
    csv_cols = ["Subject", "Start Date", "Start Time", "End Date", "End Time", "All Day Event", "Description", "Location", "Private"]
    # build CSV manually to avoid extra deps
    import csv
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=csv_cols)
    w.writeheader()
    for e in events:
        w.writerow(e)
    csv_data = buf.getvalue().encode("utf-8")

    st.download_button(
        "Download Google Calendar CSV",
        data=csv_data,
        file_name=f"stress_triage_calendar_{today_est().isoformat()}.csv",
        mime="text/csv",
        use_container_width=True,
    )

    st.caption("Import in Google Calendar: Settings -> Import & export -> Import (choose this CSV). Make sure your calendar timezone is Eastern.")

with st.expander("Setup: Gemini key"):
    st.code(
        """Streamlit Cloud:
Settings -> Secrets:
GEMINI_API_KEY="YOUR_KEY"

Local:
export GEMINI_API_KEY="YOUR_KEY"
""",
        language="bash",
    )
