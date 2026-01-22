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
    ZoneInfo = None

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
    due_date: Optional[str]
    urgency: int
    blocked: bool
    next_step: str
    bucket: str
    status: str
    created_at: str
    snoozed_until: Optional[str] = None  # YYYY-MM-DD, hide until this date (EST)


# -----------------------------
# Time helpers (EST)
# -----------------------------
def tz_now() -> datetime:
    if ZoneInfo is None:
        return datetime.now()
    return datetime.now(ZoneInfo(TZ_NAME))


def today_est() -> date:
    return tz_now().date()


def now_iso() -> str:
    return tz_now().isoformat(timespec="seconds")


# -----------------------------
# Storage helpers
# -----------------------------
def ensure_storage() -> None:
    if not TASKS_PATH.exists():
        TASKS_PATH.write_text(json.dumps({"tasks": []}, indent=2), encoding="utf-8")


def load_tasks() -> List[Task]:
    ensure_storage()
    try:
        data = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {"tasks": []}

    tasks: List[Task] = []
    for t in data.get("tasks", []):
        if not isinstance(t, dict):
            continue
        # Backward compatible defaults
        t.setdefault("status", "Open")
        t.setdefault("snoozed_until", None)
        t.setdefault("blocked", False)
        t.setdefault("due_date", None)
        try:
            tasks.append(Task(**t))
        except TypeError:
            # If schema mismatch, skip the bad record
            continue
    return tasks


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
# Minimal parsing (fallback)
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
    if category == "Money" and any(k in t for k in ["owe", "pay back", "payback", "need to give", "need to pay"]):
        return True
    if category == "Family" and any(k in t for k in ["mom", "mother"]):
        return True
    return False


def next_step_template(category: str, text: str) -> str:
    t = text.lower()
    if category == "Money":
        if any(k in t for k in ["rent", "utilities", "bill", "due"]):
            return "Send one message/call: confirm payment timing or ask for a short extension (10 min)."
        return "Text the person: propose exact date plus partial payment if possible (10 min)."
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


def is_snoozed(t: Task) -> bool:
    if not t.snoozed_until:
        return False
    try:
        return date.fromisoformat(t.snoozed_until) > today_est()
    except Exception:
        return False


# -----------------------------
# Ranking (priority and urgency)
# -----------------------------
def bucket_rank(b: str) -> int:
    return {"Do now": 0, "Stabilize": 1, "Plan next": 2, "Park": 3}.get(b, 9)


def due_sort_key(t: Task) -> str:
    return t.due_date or "9999-12-31"


def compute_ranked(tasks: List[Task]) -> List[Task]:
    open_tasks = [t for t in tasks if t.status == "Open" and not is_snoozed(t)]
    open_tasks.sort(
        key=lambda t: (bucket_rank(t.bucket), -t.urgency, due_sort_key(t), t.created_at)
    )
    return open_tasks


# -----------------------------
# Today plan snapshot (persisted)
# -----------------------------
def generate_today_plan(tasks: List[Task]) -> Dict[str, Any]:
    ranked = compute_ranked(tasks)
    return {
        "date": today_est().isoformat(),
        "ordered_ids": [t.id for t in ranked],
        "generated_at": now_iso(),
    }


def get_or_create_today_plan(tasks: List[Task], force: bool = False) -> Dict[str, Any]:
    plan = load_plan()
    if force or (plan is None) or (plan.get("date") != today_est().isoformat()):
        plan = generate_today_plan(tasks)
        save_plan(plan)
    return plan


def remove_from_plan(task_id: str) -> None:
    plan = load_plan()
    if not plan or "ordered_ids" not in plan:
        return
    plan["ordered_ids"] = [tid for tid in plan["ordered_ids"] if tid != task_id]
    plan["generated_at"] = now_iso()
    save_plan(plan)


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
Return ONLY valid JSON (no markdown), as an array of objects in the SAME ORDER as the input lines.

Fields per object:
- title: short concrete title
- category: one of {CATEGORIES}
- due_date: "YYYY-MM-DD" or null
- urgency: integer 1-5 (5 = very urgent)
- blocked: boolean
- next_step: <= 15 minute action

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
# Calendar scheduling + CSV export (priority based)
# -----------------------------
def availability_blocks(start_day: date, days_ahead: int = 21) -> List[Tuple[datetime, datetime]]:
    """
    Work windows in EST:
    Mon-Fri: 7-9pm
    Sat: 2-5pm
    Sun: 7-9pm
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

        # If today, do not schedule in the past
        now_dt = tz_now()
        if start_dt < now_dt < end_dt:
            start_dt = now_dt.replace(second=0, microsecond=0)

        if end_dt > start_dt:
            blocks.append((start_dt, end_dt))
    return blocks


def default_duration_minutes(task: Task) -> int:
    if task.bucket == "Do now":
        return 60
    if task.bucket == "Stabilize":
        return 30
    if task.bucket == "Plan next":
        return 30
    return 0


def schedule_into_blocks(tasks_in_order: List[Task], blocks: List[Tuple[datetime, datetime]]) -> List[Dict[str, Any]]:
    """
    Schedule tasks in priority order into available time blocks.
    Splits tasks if needed.
    """
    if not blocks:
        return []

    events = []
    block_i = 0
    cursor = blocks[0][0]

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
                "Description": f"Bucket: {t.bucket}. Next step: {t.next_step}",
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
st.caption("Paste tasks, click Triage. Today plan stays saved across restarts. Timezone: EST.")

tasks = load_tasks()

# Sidebar controls
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
    save_tasks([])
    if PLAN_PATH.exists():
        PLAN_PATH.unlink()
    st.success("Cleared.")
    st.rerun()

if not gemini_available():
    right.caption("Gemini off. Set GEMINI_API_KEY in Streamlit secrets or as an env var.")

# Input area
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
                    snoozed_until=None,
                ))

            save_tasks(tasks)
            # Keep Today stable: only create plan if missing for today
            get_or_create_today_plan(tasks, force=False)

            st.success(f"Added {len(lines)} item(s).")
            st.rerun()

with c2:
    if st.button("Clear input", use_container_width=True):
        st.rerun()

st.divider()

# Build display order from saved Today plan, then append any remaining open, non-snoozed tasks
plan = get_or_create_today_plan(tasks, force=False)
id_to_task = {t.id: t for t in tasks}

ordered: List[Task] = []
for tid in plan.get("ordered_ids", []):
    t = id_to_task.get(tid)
    if not t:
        continue
    if t.status != "Open":
        continue
    if is_snoozed(t):
        continue
    ordered.append(t)

planned_ids = {t.id for t in ordered}
remaining = [t for t in compute_ranked(tasks) if t.id not in planned_ids]
ordered.extend(remaining)

# Buckets for display
do_now = [t for t in ordered if t.bucket == "Do now"]
stabilize = [t for t in ordered if t.bucket == "Stabilize"]
plan_next = [t for t in ordered if t.bucket == "Plan next"]
park = [t for t in ordered if t.bucket == "Park"]

# Render function with Done + Delay
def render_task(t: Task):
    left, right = st.columns([6, 2])
    with left:
        due = f" | due {t.due_date}" if t.due_date else ""
        snooze = f" | snoozed until {t.snoozed_until}" if t.snoozed_until else ""
        st.write(f"**[{t.bucket}] [{t.category}] {t.title}** (urgency {t.urgency}/5{due}{snooze})")
        st.write(f"- {t.next_step}")

    with right:
        if st.button("Done", key=f"done_{t.id}"):
            t.status = "Done"
            save_tasks(tasks)
            remove_from_plan(t.id)
            st.rerun()

        if st.button("Delay (tomorrow)", key=f"delay1_{t.id}"):
            t.snoozed_until = (today_est() + timedelta(days=1)).isoformat()
            save_tasks(tasks)
            remove_from_plan(t.id)
            st.rerun()

        if st.button("Delay (3 days)", key=f"delay3_{t.id}"):
            t.snoozed_until = (today_est() + timedelta(days=3)).isoformat()
            save_tasks(tasks)
            remove_from_plan(t.id)
            st.rerun()


# Output
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
    if not do_now:
        st.write("None.")
    else:
        for t in do_now[:12]:
            render_task(t)

    st.subheader("Stabilize")
    if not stabilize:
        st.write("None.")
    else:
        for t in stabilize[:12]:
            render_task(t)

    st.subheader("Plan next")
    if not plan_next:
        st.write("None.")
    else:
        for t in plan_next[:20]:
            render_task(t)

    with st.expander("Park"):
        if not park:
            st.write("None.")
        else:
            for t in park[:30]:
                render_task(t)

# Snoozed tasks view (so you can unsnooze)
snoozed = [t for t in tasks if t.status == "Open" and is_snoozed(t)]
with st.expander("Snoozed"):
    if not snoozed:
        st.write("None.")
    else:
        snoozed.sort(key=lambda t: t.snoozed_until or "9999-12-31")
        for t in snoozed:
            cols = st.columns([6, 2])
            with cols[0]:
                st.write(f"**[{t.category}] {t.title}** (snoozed until {t.snoozed_until})")
            with cols[1]:
                if st.button("Unsnooze", key=f"unsnooze_{t.id}"):
                    t.snoozed_until = None
                    save_tasks(tasks)
                    st.rerun()

st.divider()

# Calendar CSV export, scheduled by priority and urgency
st.subheader("Export calendar CSV (scheduled by priority and urgency)")

# Priority order for scheduling:
# bucket -> urgency desc -> due date -> created_at
sched_tasks = [
    t for t in tasks
    if t.status == "Open"
    and not is_snoozed(t)
    and t.bucket in ["Do now", "Stabilize", "Plan next"]
]
sched_tasks.sort(key=lambda t: (bucket_rank(t.bucket), -t.urgency, due_sort_key(t), t.created_at))

blocks = availability_blocks(today_est(), days_ahead=21)
events = schedule_into_blocks(sched_tasks, blocks)

if not events:
    st.write("No events to export yet (need open tasks).")
else:
    import csv
    import io

    csv_cols = ["Subject", "Start Date", "Start Time", "End Date", "End Time", "All Day Event", "Description", "Location", "Private"]
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
    st.caption("Import in Google Calendar: Settings -> Import & export -> Import. Your calendar timezone should be Eastern.")
