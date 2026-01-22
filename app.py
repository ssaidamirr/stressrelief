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
      - "in 3 days"
      - "next week" -> +7 days
    Returns YYYY-MM-DD or None
    """
    t = text.strip().lower()
    today = date.today()

    if "today" in t:
        return today.isoformat()
    if "tomorrow" in t:
        return (today + timedelta(days=1)).isoformat()
    if "next week" in t:
        return (today + timedelta(days=7)).isoformat()

    m = re.search(r"in\s+(\d{1,2})\s+days?", t)
    if m:
        d = int(m.group(1))
        return (today + timedelta(days=d)).isoformat()

    m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", t)
    if m:
        return m.group(1)

    # "due Jan 31" or "Jan 31"
    month_map = {
        "jan": 1, "january": 1,
        "feb": 2, "february": 2,
        "mar": 3, "march": 3,
        "apr": 4, "april": 4,
        "may": 5,
        "jun": 6, "june": 6,
        "jul": 7, "july": 7,
        "aug": 8, "august": 8,
        "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10,
        "nov": 11, "november": 11,
        "dec": 12, "december": 12,
    }
    m = re.search(r"\b(?:due\s+)?([a-z]{3,9})\s+(\d{1,2})(?:\b|,)\s*(\d{4})?\b", t)
    if m:
        mon = month_map.get(m.group(1))
        day = int(m.group(2))
        yr = int(m.group(3)) if m.group(3) else today.year
        if mon:
            try:
                return date(yr, mon, day).isoformat()
            except Exception:
                return None

    return None


def guess_category_basic(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ["rent", "bill", "payment", "bank", "money", "debt", "loan", "tuition"]):
        return "Money"
    if any(k in t for k in ["mom", "mother", "dad", "brother", "sister", "family"]):
        return "Family"
    if any(k in t for k in ["nova", "client", "lead", "sales", "deliverable", "proposal"]):
        return "Nova"
    if any(k in t for k in ["assignment", "exam", "class", "professor", "homework"]):
        return "School"
    if any(k in t for k in ["doctor", "gym", "sleep", "health", "dentist"]):
        return "Health"
    if any(k in t for k in ["dmv", "form", "paperwork", "visa", "opt", "ssn", "admin"]):
        return "Admin"
    return "Other"


def due_score(due: Optional[date]) -> float:
    """
    0..1, higher means more urgent based on due proximity.
    """
    if due is None:
        return 0.2
    today = date.today()
    delta = (due - today).days
    if delta <= 0:
        return 1.0
    if delta == 1:
        return 0.95
    if delta <= 3:
        return 0.9
    if delta <= 7:
        return 0.8
    if delta <= 14:
        return 0.6
    if delta <= 30:
        return 0.4
    return 0.2


def compute_scores(task: Task, weights: Dict[str, float], thresholds: Dict[str, float]) -> Dict[str, float]:
    """
    Returns dict of scores:
      - urgency (0..1)
      - priority (0..1-ish)
    """
    cons = norm_1_5(task.consequence)
    imp = norm_1_5(task.importance)
    stress = norm_1_5(task.mental_load)
    eff = norm_1_5(task.effort)

    dscore = due_score(task.due_as_date())

    urgency = (
        weights["w_due"] * dscore +
        weights["w_consequence"] * cons
    )
    urgency = max(0.0, min(1.0, urgency))

    priority = (
        weights["w_urgency"] * urgency +
        weights["w_importance"] * imp +
        weights["w_stress"] * stress -
        weights["w_effort_penalty"] * eff
    )

    if task.pinned:
        priority += thresholds["pin_boost"]

    # Keep in a reasonable band
    priority = max(0.0, min(1.2, priority))
    return {"urgency": urgency, "priority": priority}


def classify_bucket(task: Task, urgency: float, thresholds: Dict[str, float]) -> str:
    """
    Buckets:
      - Do now: urgent + actionable
      - Stabilize: urgent + not actionable
      - Plan next: important but not urgent
      - Park: low value or noise
    """
    if task.status != "Open":
        return task.status

    act = norm_1_5(task.actionability)
    imp = norm_1_5(task.importance)

    if urgency >= thresholds["urgent_cutoff"]:
        if act >= thresholds["actionable_cutoff"]:
            return "Do now"
        return "Stabilize"

    if imp >= thresholds["important_cutoff"]:
        return "Plan next"

    return "Park"


def template_next_steps(task: Task) -> List[str]:
    """
    Non-AI default suggestions for Stabilize and quick next steps.
    """
    title = task.title.strip()
    cat = task.category

    if cat == "Money":
        return [
            "List the next 14 days: balance, income dates, mandatory bills (10 min).",
            "Send one extension/payment-plan message to whoever is due next (10 min).",
            "Pause one subscription or non-essential spend today (5 min).",
        ]

    if cat == "Family":
        return [
            "Send a boundary message and propose fixed call windows (10 min).",
            "Schedule 2 calls this week, 20 minutes each, and stick to it (5 min).",
            "During calls, limit to 1 topic + end with 1 small next step (prep 3 min).",
        ]

    if cat == "Nova":
        return [
            "Define 'done' for this task in 3 bullets (5 min).",
            "2-minute start: open the doc, write the first ugly version (2 min).",
            "45-minute deep work sprint with phone away (set timer now).",
        ]

    return [
        "Write the smallest next action that fits in 15 minutes.",
        "Start with a 2-minute setup and then do 10 minutes focused work.",
    ]


# -----------------------------
# Gemini (Optional)
# -----------------------------
class TaskDraft(BaseModel):
    title: str = Field(description="Short task title, clear and concrete.")
    category: str = Field(description=f"One of: {', '.join(CATEGORIES)}")
    due_date: Optional[str] = Field(description="YYYY-MM-DD if a due date is implied, else null.")
    importance: int = Field(description="1-5. Goal relevance.")
    consequence: int = Field(description="1-5. Real downside if ignored soon.")
    actionability: int = Field(description="1-5. How easy to take a next step within 15 min.")
    mental_load: int = Field(description="1-5. How much it loops in the head.")
    effort: int = Field(description="1-5. How hard the overall task feels.")
    blocked: bool = Field(description="True if it cannot be solved right now without external dependency.")
    next_step_15m: str = Field(description="A specific next step that fits in 15 minutes or less.")


def get_api_key() -> Optional[str]:
    # Prefer Streamlit secrets, then env
    if "GEMINI_API_KEY" in st.secrets:
        return str(st.secrets["GEMINI_API_KEY"]).strip() or None
    if "GOOGLE_API_KEY" in st.secrets:
        return str(st.secrets["GOOGLE_API_KEY"]).strip() or None
    return (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip() or None


def ai_enabled() -> bool:
    return (genai is not None) and (get_api_key() is not None)


def ai_parse_line(line: str, model: str) -> Optional[TaskDraft]:
    """
    Uses structured output to turn a messy line into a TaskDraft.
    """
    if not ai_enabled():
        return None

    prompt = f"""
You are helping a user triage stress into action.
Convert the input into a single task with realistic 1-5 scores.

Rules:
- Keep title short and actionable.
- category must be one of: {CATEGORIES}
- due_date must be YYYY-MM-DD or null.
- next_step_15m must be very specific, doable in 15 minutes.

Input:
{line}
""".strip()

    try:
        client = genai.Client(api_key=get_api_key())
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_json_schema": TaskDraft.model_json_schema(),
            },
        )
        draft = TaskDraft.model_validate_json(response.text)
        # Normalize category if model returns something close
        if draft.category not in CATEGORIES:
            draft.category = "Other"
        # Clamp ints
        for k in ["importance", "consequence", "actionability", "mental_load", "effort"]:
            v = getattr(draft, k)
            setattr(draft, k, max(1, min(5, int(v))))
        return draft
    except Exception:
        return None


# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="Stress Triage", layout="wide")

st.title("Stress Triage")
st.caption("Dump tasks. Apply your subjective rules consistently. Get a small, clear plan.")

if "tasks" not in st.session_state:
    st.session_state.tasks = load_tasks()

tasks: List[Task] = st.session_state.tasks

with st.sidebar:
    st.header("Ranking rules (your call)")

    st.subheader("Urgency components")
    w_due = st.slider("Weight: due date proximity", 0.0, 1.0, 0.45, 0.05)
    w_cons = st.slider("Weight: consequence severity", 0.0, 1.0, 0.55, 0.05)

    # Normalize so they sum to 1 (avoid weird scaling)
    s = max(1e-9, w_due + w_cons)
    w_due /= s
    w_cons /= s

    st.subheader("Priority blend")
    w_urgency = st.slider("Weight: urgency", 0.0, 1.0, 0.55, 0.05)
    w_importance = st.slider("Weight: importance", 0.0, 1.0, 0.30, 0.05)
    w_stress = st.slider("Weight: mental load", 0.0, 1.0, 0.15, 0.05)

    st.subheader("Penalty and thresholds")
    w_effort_penalty = st.slider("Effort penalty (optional)", 0.0, 0.30, 0.05, 0.01)

    urgent_cutoff = st.slider("Urgent cutoff", 0.0, 1.0, 0.70, 0.05)
    actionable_cutoff = st.slider("Actionable cutoff", 0.0, 1.0, 0.60, 0.05)
    important_cutoff = st.slider("Important cutoff", 0.0, 1.0, 0.60, 0.05)
    pin_boost = st.slider("Pinned boost", 0.0, 0.40, 0.15, 0.05)

    weights = {
        "w_due": w_due,
        "w_consequence": w_cons,
        "w_urgency": w_urgency,
        "w_importance": w_importance,
        "w_stress": w_stress,
        "w_effort_penalty": w_effort_penalty,
    }
    thresholds = {
        "urgent_cutoff": urgent_cutoff,
        "actionable_cutoff": actionable_cutoff,
        "important_cutoff": important_cutoff,
        "pin_boost": pin_boost,
    }

    st.divider()
    st.subheader("Gemini (optional)")

    default_model = "gemini-3-flash-preview"
    model = st.selectbox(
        "Model",
        options=[
            "gemini-3-flash-preview",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
        ],
        index=0,
    )

    st.write("AI status:", "Enabled" if ai_enabled() else "Off (no key or SDK missing)")
    st.caption("Set GEMINI_API_KEY in environment or in .streamlit/secrets.toml")

    panic_mode = st.toggle("Panic mode", value=False)
    st.divider()

    if st.button("Save to disk", use_container_width=True):
        save_tasks(tasks)
        st.success(f"Saved to {DATA_PATH}")

    if st.button("Reload from disk", use_container_width=True):
        st.session_state.tasks = load_tasks()
        st.rerun()


def add_task_from_fields(
    title: str,
    category: str,
    due_date: Optional[str],
    importance: int,
    consequence: int,
    actionability: int,
    mental_load: int,
    effort: int,
    blocked: bool,
    next_step_15m: str,
) -> None:
    t = Task(
        id=str(uuid.uuid4())[:8],
        title=title.strip(),
        category=category,
        due_date=due_date,
        importance=int(importance),
        consequence=int(consequence),
        actionability=int(actionability),
        mental_load=int(mental_load),
        effort=int(effort),
        blocked=bool(blocked),
        next_step_15m=next_step_15m.strip(),
        pinned=False,
        status="Open",
        created_at=now_iso(),
    )
    st.session_state.tasks.insert(0, t)


def render_task_editor(t: Task) -> None:
    cols = st.columns([2.6, 1.2, 1.2, 1.2, 1.2, 1.0])
    with cols[0]:
        t.title = st.text_input("Title", value=t.title, key=f"title_{t.id}")
        t.next_step_15m = st.text_input("15-min next step", value=t.next_step_15m, key=f"ns_{t.id}")

    with cols[1]:
        t.category = st.selectbox("Category", CATEGORIES, index=CATEGORIES.index(t.category), key=f"cat_{t.id}")
        t.status = st.selectbox("Status", STATUSES, index=STATUSES.index(t.status), key=f"st_{t.id}")

    with cols[2]:
        due_str = t.due_date or ""
        due_in = st.text_input("Due (YYYY-MM-DD)", value=due_str, key=f"due_{t.id}")
        due_in = due_in.strip()
        t.due_date = due_in if due_in else None
        t.blocked = st.checkbox("Blocked now", value=t.blocked, key=f"blk_{t.id}")

    with cols[3]:
        t.importance = st.slider("Importance", 1, 5, int(t.importance), key=f"imp_{t.id}")
        t.consequence = st.slider("Consequence", 1, 5, int(t.consequence), key=f"cons_{t.id}")

    with cols[4]:
        t.actionability = st.slider("Actionability", 1, 5, int(t.actionability), key=f"act_{t.id}")
        t.mental_load = st.slider("Mental load", 1, 5, int(t.mental_load), key=f"ml_{t.id}")

    with cols[5]:
        t.effort = st.slider("Effort", 1, 5, int(t.effort), key=f"eff_{t.id}")
        t.pinned = st.checkbox("Pin", value=t.pinned, key=f"pin_{t.id}")


# -----------------------------
# Views
# -----------------------------
tab_labels = ["Quick dump", "Triage", "Today", "Export"]
tabs = st.tabs(tab_labels)

# Quick dump
with tabs[0]:
    if panic_mode:
        st.info("Panic mode is on. Go to the Today tab for the smallest plan.")
    st.subheader("Quick dump")
    st.write("Paste tasks/problems, one per line. Example: `Pay phone bill due Jan 28`")

    use_ai = st.toggle("Use Gemini to clean and score lines (optional)", value=False, disabled=not ai_enabled())

    dump = st.text_area("Tasks (one per line)", height=180, placeholder="One per line...")

    colA, colB = st.columns([1, 1])
    with colA:
        if st.button("Add lines", use_container_width=True):
            lines = [ln.strip() for ln in dump.splitlines() if ln.strip()]
            if not lines:
                st.warning("Nothing to add.")
            else:
                added = 0
                for ln in lines:
                    draft = ai_parse_line(ln, model=model) if use_ai else None
                    if draft:
                        add_task_from_fields(
                            title=draft.title,
                            category=draft.category,
                            due_date=draft.due_date,
                            importance=draft.importance,
                            consequence=draft.consequence,
                            actionability=draft.actionability,
                            mental_load=draft.mental_load,
                            effort=draft.effort,
                            blocked=draft.blocked,
                            next_step_15m=draft.next_step_15m,
                        )
                        added += 1
                    else:
                        # Basic non-AI parse
                        dd = parse_due_date_quick(ln)
                        cat = guess_category_basic(ln)
                        add_task_from_fields(
                            title=ln,
                            category=cat,
                            due_date=dd,
                            importance=3,
                            consequence=3,
                            actionability=3,
                            mental_load=3,
                            effort=3,
                            blocked=False,
                            next_step_15m="Write the smallest next step that takes 15 minutes.",
                        )
                        added += 1

                st.success(f"Added {added} item(s).")
                st.rerun()

    with colB:
        if st.button("Clear input box", use_container_width=True):
            st.rerun()

    st.divider()
    st.subheader("Add one item (manual)")
    with st.form("manual_add", clear_on_submit=True):
        title = st.text_input("Title", placeholder="Example: Call landlord about rent extension")
        category = st.selectbox("Category", CATEGORIES, index=0)
        due = st.text_input("Due date (optional, YYYY-MM-DD)")
        c1, c2, c3, c4, c5 = st.columns(5)
        with c1:
            importance = st.slider("Importance", 1, 5, 3)
        with c2:
            consequence = st.slider("Consequence", 1, 5, 3)
        with c3:
            actionability = st.slider("Actionability", 1, 5, 3)
        with c4:
            mental_load = st.slider("Mental load", 1, 5, 3)
        with c5:
            effort = st.slider("Effort", 1, 5, 3)

        blocked = st.checkbox("Blocked right now")
        next_step = st.text_input("15-min next step", value="Write the smallest next step that takes 15 minutes.")
        submit = st.form_submit_button("Add")

        if submit:
            if not title.strip():
                st.warning("Title is required.")
            else:
                add_task_from_fields(
                    title=title,
                    category=category,
                    due_date=due.strip() or None,
                    importance=importance,
                    consequence=consequence,
                    actionability=actionability,
                    mental_load=mental_load,
                    effort=effort,
                    blocked=blocked,
                    next_step_15m=next_step,
                )
                st.success("Added.")
                st.rerun()


# Triage
with tabs[1]:
    st.subheader("Triage dashboard")

    open_tasks = [t for t in tasks if t.status == "Open"]
    if not open_tasks:
        st.info("No open tasks yet. Add some in Quick dump.")
    else:
        rows = []
        for t in open_tasks:
            scores = compute_scores(t, weights, thresholds)
            bucket = classify_bucket(t, scores["urgency"], thresholds)
            rows.append((t, scores["urgency"], scores["priority"], bucket))

        rows.sort(key=lambda x: x[2], reverse=True)

        # Overview
        bucket_counts = {}
        for _, _, _, b in rows:
            bucket_counts[b] = bucket_counts.get(b, 0) + 1

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Do now", bucket_counts.get("Do now", 0))
        c2.metric("Stabilize", bucket_counts.get("Stabilize", 0))
        c3.metric("Plan next", bucket_counts.get("Plan next", 0))
        c4.metric("Park", bucket_counts.get("Park", 0))

        st.divider()

        for (t, urg, pri, bucket) in rows:
            with st.expander(f"[{bucket}] {t.title}", expanded=False):
                st.write(
                    f"Urgency: **{urg:.2f}**  |  Priority: **{pri:.2f}**  |  Category: **{t.category}**"
                )

                # Explain why it ranks
                dd = t.due_as_date()
                dd_str = dd.isoformat() if dd else "None"
                st.caption(
                    f"Due: {dd_str}. Consequence: {t.consequence}/5. Importance: {t.importance}/5. "
                    f"Actionability: {t.actionability}/5. Mental load: {t.mental_load}/5."
                )

                render_task_editor(t)

                if bucket in ["Stabilize", "Do now"] and not t.next_step_15m.strip():
                    st.write("Suggested defaults:")
                    for s in template_next_steps(t):
                        st.write("-", s)

                if st.button("Delete", key=f"del_{t.id}"):
                    st.session_state.tasks = [x for x in st.session_state.tasks if x.id != t.id]
                    st.rerun()


# Today
with tabs[2]:
    st.subheader("Today plan")

    # Build ranked buckets
    ranked = []
    for t in tasks:
        if t.status != "Open":
            continue
        scores = compute_scores(t, weights, thresholds)
        bucket = classify_bucket(t, scores["urgency"], thresholds)
        ranked.append((t, scores["urgency"], scores["priority"], bucket))

    ranked.sort(key=lambda x: x[2], reverse=True)

    do_now = [x for x in ranked if x[3] == "Do now"]
    stabilize = [x for x in ranked if x[3] == "Stabilize"]
    plan_next = [x for x in ranked if x[3] == "Plan next"]

    if panic_mode:
        st.info("Panic mode: you only see the smallest plan.")
        st.write("### Top 3")
        top3 = (do_now + stabilize + plan_next)[:3]
        if not top3:
            st.write("Nothing open. Add items first.")
        for (t, urg, pri, bucket) in top3:
            st.write(f"**[{bucket}] {t.title}**")
            step = t.next_step_15m.strip() or template_next_steps(t)[0]
            st.write("-", step)
        st.stop()

    st.write("### Top 3 to act on")
    top3 = do_now[:3]
    if not top3:
        st.write("No 'Do now' tasks. That is fine.")
    for (t, urg, pri, bucket) in top3:
        st.write(f"**{t.title}**  (Priority {pri:.2f})")
        st.write("-", t.next_step_15m.strip() or template_next_steps(t)[0])

    st.divider()
    st.write("### Stabilize (urgent but not solvable fast)")
    if not stabilize:
        st.write("None right now.")
    else:
        for (t, urg, pri, bucket) in stabilize[:8]:
            st.write(f"**{t.title}**  (Urgency {urg:.2f})")
            suggestions = template_next_steps(t)
            # Use either the saved next step or a stabilize suggestion
            if t.next_step_15m.strip():
                st.write("-", t.next_step_15m.strip())
            else:
                for s in suggestions[:3]:
                    st.write("-", s)

    st.divider()
    st.write("### Plan next (important, not urgent)")
    if not plan_next:
        st.write("None right now.")
    else:
        for (t, urg, pri, bucket) in plan_next[:8]:
            st.write(f"- {t.title}")


# Export
with tabs[3]:
    st.subheader("Export / Backup")
    st.write(f"Local file path: `{DATA_PATH}`")

    export = {"tasks": [asdict(t) for t in tasks]}
    st.download_button(
        "Download tasks.json",
        data=json.dumps(export, indent=2),
        file_name="tasks.json",
        mime="application/json",
        use_container_width=True,
    )

    st.divider()
    st.subheader("Gemini key setup")
    st.code(
        """# Option A: environment variable (recommended)
export GEMINI_API_KEY="YOUR_KEY_HERE"

# Option B: Streamlit secrets
# Create: .streamlit/secrets.toml
GEMINI_API_KEY="YOUR_KEY_HERE"
""",
        language="bash",
    )
