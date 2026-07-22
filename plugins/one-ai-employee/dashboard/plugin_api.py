"""One AI Employee — cockpit + approvals backend.

Mounted by the dashboard at ``/api/plugins/one-ai-employee/``.

Read-mostly. The single write is a decision comment on the swarm root card.
That is deliberate: Ahmed's approval must be durable, auditable, and visible
from the CLI and the board — not a flag that lives only in this plugin's head.
It rides the same structured-comment convention Kanban Swarm already uses for
its blackboard (``[swarm:blackboard] {json}``).

Nothing here touches GoHighLevel or any external system. Recording an approval
records *intent*; acting on it stays a separate, explicit step.
"""

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from hermes_cli import kanban_db
from hermes_cli import kanban_swarm

log = logging.getLogger(__name__)

router = APIRouter()

# Structured-comment marker for decisions. Mirrors kanban_swarm's
# BLACKBOARD_PREFIX so decisions stay greppable outside this plugin.
DECISION_PREFIX = "[oae:decision] "

# Tag kanban_swarm writes into the root's completion metadata.
SWARM_KIND = "kanban_swarm_v1"

# The synthesizer's merged output; everything else is an input to it.
FINAL_PREFIX = "00-"

# Workflow states a deliverable can be in, from Ahmed's point of view.
ST_RUNNING = "running"
ST_REVIEW = "ready_for_review"
ST_APPROVED = "approved"
ST_CHANGES = "changes_requested"

# Cards this desk creates are stamped with this author so they can be told
# apart from swarm work and from cards Ahmed makes by hand.
DESK_AUTHOR = "approvals-desk"

# Every follow-up job routes here first. Adam is the Chief of Staff and holds
# the delegate_task tool — routing to the right specialist is his job, not
# Ahmed's, and not a guess this plugin should be making from a text box.
ROUTER_PROFILE = "default"

# Ahmed's standing rule, restated on every card this desk creates. Approving
# the PLAN is not approving the BUILD: the agents prepare, then come back here
# for a second explicit yes before anything reaches a live account.
PLAN_ONLY_RULE = (
    "*** HARD RULE - PLAN ONLY ***\n"
    "Do NOT create, edit, publish, or delete anything in GoHighLevel or any "
    "other external system. Ahmed has approved the PLAN, not the build. "
    "Reading a live account to ground your work is fine.\n"
    "Prepare everything so the build is one click away, then complete this "
    "card with: what is ready, and exactly what you need Ahmed to authorise. "
    "He gives a separate, explicit approval before anything goes live."
)

# The same principle as PLAN_ONLY_RULE, worded for work Ahmed starts himself
# rather than work he is reacting to. Nothing the cockpit fires may reach a
# live account or a real person: the team prepares, Ahmed decides.
PREPARE_ONLY_RULE = (
    "*** HARD RULE - PREPARE, DO NOT PUBLISH ***\n"
    "Do NOT create, edit, publish, or delete anything in GoHighLevel or any "
    "other live/external system, and do not contact any real client or lead. "
    "Reading a live account to ground your work is fine.\n"
    "Never invent statistics, guarantees, testimonials, or reviews. If a claim "
    "needs proof, leave a placeholder for Ahmed to supply rather than making "
    "one up.\n"
    "Write your work as files in the shared workspace. When you finish, Ahmed "
    "reviews it on his desk and gives a separate, explicit approval before "
    "anything goes live."
)

# The standing agency team and what each specialist owns on a full-team job.
# This mirrors the split that produced the 30-day playbook. Ahmed supplies the
# goal; who does what is the agency's structure, not a question to put in a
# text box. Anything outside this shape goes to Adam, who can delegate freely.
TEAM_BRIEFS = [
    (
        "marketing",
        "Strategy & offer",
        "You own the strategy. Deliver positioning, the offer, who we target "
        "and why, the funnel, the calendar or sequence, and the numbers we "
        "will judge it by. Diagnose before you prescribe. Write `01-strategy.md`.",
    ),
    (
        "creative",
        "Copy & creative",
        "You own every customer-facing word. Deliver ad copy, short-form video "
        "scripts and hooks, the email and SMS sequences, and landing-page copy. "
        "Write `02-creative.md`.",
    ),
    (
        "ghl",
        "GoHighLevel build spec",
        "You own the GoHighLevel build spec. Deliver the pipeline and stages, "
        "custom fields and tags, workflows, calendar, forms and surveys, "
        "funnels and pages, and the reusable snapshot. Spec it so it is one "
        "click from built. Write `03-ghl-build.md`.",
    ),
]


def _conn(board: Optional[str] = None):
    """Open kanban.db, self-healing the schema like the kanban plugin does."""
    try:
        kanban_db.init_db(board=board)
    except Exception as exc:  # pragma: no cover - matches kanban plugin behaviour
        log.warning("one-ai-employee: init_db failed: %s", exc)
    return kanban_db.connect(board=board)


def _swarm_roots(conn) -> list[tuple[str, dict]]:
    """Every swarm root as ``(task_id, completion_metadata)``, newest first.

    Swarm metadata lives on the *run* (``task_runs.metadata``), not on
    ``tasks.result`` — the root's ``result`` is always NULL. One indexed-ish
    LIKE beats calling ``latest_blackboard`` for every card on the board just
    to discover which three are swarms.
    """
    rows = conn.execute(
        "SELECT task_id, metadata FROM task_runs "
        "WHERE outcome = 'completed' AND metadata LIKE ? "
        "ORDER BY id DESC",
        (f"%{SWARM_KIND}%",),
    ).fetchall()

    seen: set[str] = set()
    out: list[tuple[str, dict]] = []
    for row in rows:
        task_id = row["task_id"]
        if task_id in seen:
            continue
        try:
            meta = json.loads(row["metadata"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        # LIKE can match the string anywhere; confirm it is really the tag.
        if not isinstance(meta, dict) or meta.get("kind") != SWARM_KIND:
            continue
        seen.add(task_id)
        out.append((task_id, meta))
    return out


def _latest_decision(conn, task_id: str) -> Optional[dict]:
    """Last decision on a card, or None if never reviewed.

    Later comments win, so re-approving after a change request is just another
    comment and the history stays intact on the card.
    """
    found = None
    for comment in kanban_db.list_comments(conn, task_id):
        body = comment.body or ""
        if not body.startswith(DECISION_PREFIX):
            continue
        try:
            payload = json.loads(body[len(DECISION_PREFIX):])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            found = payload
    return found


def _workspace_files(workspace: Optional[str]) -> list[dict]:
    """Deliverable markdown sitting in the swarm's shared workspace."""
    if not workspace:
        return []
    root = Path(workspace)
    if not root.is_dir():
        return []
    out = []
    for f in sorted(root.glob("*.md")):
        try:
            out.append({
                "name": f.name,
                "bytes": f.stat().st_size,
                "is_final": f.name.startswith(FINAL_PREFIX),
            })
        except OSError:
            continue
    return out


def _run_summary(conn, task_id: str) -> str:
    """What the agent said when it finished — its report back to Ahmed.

    Lives on the run, not the task, same as the swarm metadata.
    """
    row = conn.execute(
        "SELECT summary FROM task_runs WHERE task_id = ? AND summary IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return (row["summary"] or "") if row else ""


def _handoffs(blackboard: dict) -> list[dict]:
    """Agent handoffs recorded on the blackboard, minus bookkeeping keys."""
    authors = blackboard.get("_authors") or {}
    out = []
    for key, value in blackboard.items():
        if key in ("topology", "_authors"):
            continue
        if isinstance(value, str):
            summary = value
        else:
            summary = json.dumps(value, ensure_ascii=False)
        out.append({
            "key": key,
            "author": authors.get(key, ""),
            "summary": summary[:600],
        })
    return out


@router.get("/team")
def get_team(board: Optional[str] = Query(None)):
    """The roster, plus what each agent is doing right now.

    "Working on" comes from the board rather than the process table: a profile
    with a stopped gateway can still run Kanban work, so process liveness would
    answer the wrong question. ``gateway_running`` is reported separately as
    what it actually means — is this agent reachable on chat.
    """
    try:
        from hermes_cli import profiles as profiles_mod
        profiles = profiles_mod.list_profiles()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to list profiles: {exc}")

    conn = _conn(board=board)
    try:
        tasks = list(kanban_db.list_tasks(conn, include_archived=False))
    finally:
        conn.close()

    running: dict[str, str] = {}
    done_counts: dict[str, int] = {}
    for t in tasks:
        who = t.assignee
        if not who:
            continue
        if t.status == "running" and who not in running:
            running[who] = t.title or ""
        if t.status == "done":
            done_counts[who] = done_counts.get(who, 0) + 1

    return {
        "team": [
            {
                "profile": p.name,
                "is_default": bool(p.is_default),
                "description": p.description or "",
                "gateway_running": bool(p.gateway_running),
                "model": p.model or "",
                "working_on": running.get(p.name),
                "completed": done_counts.get(p.name, 0),
            }
            for p in profiles
        ]
    }


@router.get("/deliverables")
def list_deliverables(board: Optional[str] = Query(None)):
    """Swarm work and where it stands, newest first.

    States: ``running`` (cards still in flight) → ``ready_for_review``
    (finished, Ahmed hasn't decided) → ``approved`` / ``changes_requested``.
    Only ``ready_for_review`` is a decision he can actually make.
    """
    conn = _conn(board=board)
    try:
        items = []
        for root_id, meta in _swarm_roots(conn):
            root = kanban_db.get_task(conn, root_id)
            if root is None:
                continue

            kids = [kanban_db.get_task(conn, cid) for cid in kanban_db.child_ids(conn, root_id)]
            kids = [k for k in kids if k is not None]

            blackboard = kanban_swarm.latest_blackboard(conn, root_id)
            topology = blackboard.get("topology") or {}

            # The synthesizer is the last card in the graph; the swarm is only
            # really finished when it is done, not merely when the workers are.
            tail_ids = [
                *(topology.get("worker_ids") or []),
                topology.get("verifier_id"),
                topology.get("synthesizer_id"),
            ]
            tail = [kanban_db.get_task(conn, tid) for tid in tail_ids if tid]
            tail = [t for t in tail if t is not None]
            graph = tail or kids
            complete = bool(graph) and all(t.status == "done" for t in graph)

            decision = _latest_decision(conn, root_id)
            if not complete:
                state = ST_RUNNING
            elif decision and decision.get("decision") == "approved":
                state = ST_APPROVED
            elif decision and decision.get("decision") == "changes_requested":
                state = ST_CHANGES
            else:
                state = ST_REVIEW

            # What Ahmed's click actually set in motion, and where it got to.
            job = None
            job_id = (decision or {}).get("job_id")
            if job_id:
                job_task = kanban_db.get_task(conn, job_id)
                if job_task is not None:
                    job = {
                        "id": job_task.id,
                        "title": job_task.title,
                        "assignee": job_task.assignee,
                        "status": job_task.status,
                        "summary": _run_summary(conn, job_task.id),
                    }

            files = _workspace_files(root.workspace_path)
            items.append({
                "id": root_id,
                "title": root.title or "",
                "goal": meta.get("goal", ""),
                "created_at": root.created_at,
                "state": state,
                "workspace": root.workspace_path,
                "files": files,
                "final_file": next((f["name"] for f in files if f["is_final"]), None),
                "agents": sorted({t.assignee for t in graph if t.assignee}),
                "progress": {
                    "done": sum(1 for t in graph if t.status == "done"),
                    "total": len(graph),
                },
                "handoffs": _handoffs(blackboard),
                "decision": decision,
                "job": job,
            })
        return {"deliverables": items}
    finally:
        conn.close()


@router.get("/deliverables/{task_id}/doc")
def get_deliverable_doc(
    task_id: str,
    file: Optional[str] = Query(None, description="File name inside the swarm workspace"),
    board: Optional[str] = Query(None),
):
    """Render one deliverable file to HTML for reading in the dashboard.

    ``file`` is resolved inside the card's own workspace and the resolved path
    is re-checked against it, so ``..`` or an absolute path cannot escape.
    """
    conn = _conn(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        if not task.workspace_path:
            raise HTTPException(status_code=404, detail="task has no workspace")
        workspace = task.workspace_path
    finally:
        conn.close()

    root = Path(workspace).resolve()
    files = _workspace_files(workspace)
    name = file or next((f["name"] for f in files if f["is_final"]), None)
    if not name:
        raise HTTPException(status_code=404, detail="no deliverable file found")

    target = (root / name).resolve()
    if not target.is_file() or root not in target.parents:
        raise HTTPException(status_code=404, detail="file not found in workspace")

    text = target.read_text(encoding="utf-8", errors="replace")
    return {
        "file": target.name,
        "bytes": len(text.encode("utf-8")),
        "html": _markdown_to_html(text),
    }


def _job_body(decision: str, *, source: Any, notes: str) -> tuple[str, str]:
    """The card Adam receives. Returns (title, body).

    Written as an instruction to an agent, not a log line: it has to carry the
    decision, where the work lives, what to do next, and the guardrails — the
    agent has no other context when the dispatcher spawns it.
    """
    short = (source.title or "").replace("SWARM:", "").strip()
    where = (
        f"Source deliverable : {short}\n"
        f"Swarm root card    : {source.id}\n"
        f"Shared workspace   : {source.workspace_path or '(none)'}\n"
    )

    if decision == ST_APPROVED:
        title = f"Approved - execute next step: {short}"
        body = (
            "Ahmed reviewed your team's work on the approvals desk and APPROVED it.\n\n"
            + where
            + (f"\nHis note:\n\"{notes}\"\n" if notes else "")
            + "\nYour job:\n"
            "1. Read the final playbook in the shared workspace (the 00-FINAL-*.md file), "
            "especially its final handoff section — the team already wrote who does what next.\n"
            "2. Execute that next step, or delegate it to the right specialist. "
            "You hold delegate_task; routing is your call.\n"
            "3. Complete this card with what is ready and what needs Ahmed's authorisation.\n\n"
            + PLAN_ONLY_RULE
        )
    else:
        title = f"Changes requested: {short}"
        body = (
            "Ahmed reviewed your team's work on the approvals desk and REQUESTED CHANGES.\n\n"
            f"His notes:\n\"{notes}\"\n\n"
            + where
            + "\nYour job:\n"
            "1. Read his notes against the current work in the shared workspace.\n"
            "2. Work out which specialist's output is wrong and delegate the fix "
            "(or fix it yourself if it is yours).\n"
            "3. Update the files in the shared workspace in place — do not start a "
            "parallel copy; the desk reads those exact files.\n"
            "4. Complete this card with what changed, so it can go back to Ahmed.\n\n"
            "The original swarm rules still apply: template-first placeholders "
            "({{client_name}}, {{city}}, {{price_band}}), no invented statistics or "
            "results, and review strategy means earning MORE REAL reviews from real "
            "past clients only.\n\n"
            + PLAN_ONLY_RULE
        )
    return title, body


def _create_followup_job(conn, *, source: Any, decision: str, notes: str) -> Optional[str]:
    """Create the card Ahmed's decision fires, routed to Adam.

    No parents: the card is ``ready`` the moment it exists, so the gateway's
    dispatcher picks it up on its next pass without Ahmed doing anything.

    Idempotent on (source, decision, notes): clicking Approve twice reuses the
    same card, but different change-notes are a genuinely different instruction
    and get their own.
    """
    title, body = _job_body(decision, source=source, notes=notes)
    digest = hashlib.sha1(f"{decision}|{notes}".encode("utf-8")).hexdigest()[:10]
    try:
        return kanban_db.create_task(
            conn,
            title=title,
            body=body,
            assignee=ROUTER_PROFILE,
            created_by=DESK_AUTHOR,
            workspace_kind="dir",
            # Same folder the swarm used, so the agent edits the real files
            # rather than a fresh empty scratch dir.
            workspace_path=source.workspace_path or None,
            idempotency_key=f"oae-{source.id}-{digest}",
        )
    except Exception as exc:
        # A decision that persisted but failed to dispatch is recoverable;
        # losing the decision because dispatch failed is not.
        log.warning("one-ai-employee: follow-up job creation failed: %s", exc)
        return None


class DecisionBody(BaseModel):
    decision: str          # "approved" | "changes_requested"
    notes: str = ""


@router.post("/deliverables/{task_id}/decision")
def record_decision(task_id: str, payload: DecisionBody, board: Optional[str] = Query(None)):
    """Record Ahmed's decision as a durable comment on the swarm root.

    Records intent only — this builds nothing and touches no external system.
    """
    decision = (payload.decision or "").strip()
    if decision not in (ST_APPROVED, ST_CHANGES):
        raise HTTPException(
            status_code=400,
            detail=f"decision must be '{ST_APPROVED}' or '{ST_CHANGES}'",
        )
    notes = (payload.notes or "").strip()
    if decision == ST_CHANGES and not notes:
        # A rejection with no reason is not actionable by the agent that has to
        # redo the work.
        raise HTTPException(
            status_code=400,
            detail="notes are required when requesting changes",
        )

    conn = _conn(board=board)
    try:
        source = kanban_db.get_task(conn, task_id)
        if source is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")

        # Fire the job first so its id can be recorded with the decision — the
        # decision comment is then a complete account of what Ahmed's click did.
        job_id = _create_followup_job(conn, source=source, decision=decision, notes=notes)

        record = {
            "decision": decision,
            "notes": notes,
            "by": "Ahmed",
            "at": int(time.time()),
            "job_id": job_id,
        }
        kanban_db.add_comment(
            conn,
            task_id,
            author="dashboard",
            body=DECISION_PREFIX + json.dumps(record, ensure_ascii=False, sort_keys=True),
        )
        return {"ok": True, "decision": record, "job_id": job_id}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Starting work from the cockpit
# ---------------------------------------------------------------------------


def _slug(text: str, limit: int = 40) -> str:
    """A filesystem-safe stem for a workspace folder."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-") or "job"


def _title_of(request: str, limit: int = 72) -> str:
    """A one-line card title from Ahmed's brief.

    The brief itself always travels in the body or the goal. The `kanban swarm`
    CLI collapses a worker's whole brief into its title; nothing here may.
    """
    line = " ".join(request.split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _job_workspace(request: str, mode: str) -> Path:
    digest = hashlib.sha1(f"{mode}|{request}".encode("utf-8")).hexdigest()[:8]
    return Path(kanban_db.workspaces_root()) / f"oae-{_slug(request)}-{digest}"


def _team_goal(request: str, workspace: Path) -> str:
    """Build the swarm goal.

    ``kanban_swarm._swarm_context`` appends this goal to every worker, the
    verifier and the synthesizer — whose own bodies are hardcoded. The
    synthesizer is never told what to name its output, and this desk recognises
    a deliverable *only* by the ``00-`` prefix, so that convention has to be
    stated here or the work lands and the cockpit shows nothing.
    """
    return (
        f"{request}\n\n"
        f"{PREPARE_ONLY_RULE}\n\n"
        "*** SHARED WORKSPACE ***\n"
        f"Every card in this job shares one folder: {workspace}\n"
        "Write your deliverable there, and read your teammates' files before "
        "you finish so the work fits together instead of contradicting itself.\n"
        "The merged final document is the one Ahmed reads: it MUST be the only "
        f"file whose name starts with `{FINAL_PREFIX}`.\n"
        "Leave client-specific unknowns as `{{placeholder}}` custom values "
        "rather than inventing facts."
    )


class JobBody(BaseModel):
    request: str
    mode: str = "adam"          # "adam" | "team"


@router.post("/jobs")
def create_job(payload: JobBody, board: Optional[str] = Query(None)):
    """Start real work from the cockpit.

    ``adam`` routes a single card to the Chief of Staff, who delegates as he
    sees fit. ``team`` fires the full swarm — the three specialists in
    parallel, a verifier gate, then a synthesizer — the same shape that
    produced the 30-day playbook.
    """
    request = (payload.request or "").strip()
    if not request:
        raise HTTPException(status_code=400, detail="Tell your team what you need.")
    if len(request) > 4000:
        raise HTTPException(
            status_code=400, detail="That brief is too long — keep it under 4000 characters."
        )
    mode = (payload.mode or "adam").strip()
    if mode not in ("adam", "team"):
        raise HTTPException(status_code=400, detail="mode must be 'adam' or 'team'")

    workspace = _job_workspace(request, mode)
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not create workspace: {exc}")

    title = _title_of(request)
    # Same brief + same mode is the same job: double-clicking Send must not put
    # the whole team on it twice.
    digest = hashlib.sha1(f"{mode}|{request}".encode("utf-8")).hexdigest()[:12]
    key = f"oae-job-{digest}"

    conn = _conn(board=board)
    try:
        if mode == "adam":
            task_id = kanban_db.create_task(
                conn,
                title=title,
                body=(
                    f"{request}\n\n{PREPARE_ONLY_RULE}\n\n"
                    f"Shared workspace: {workspace}\n"
                    "Write anything you produce there as markdown. If this needs "
                    "a specialist, delegate it — that call is yours."
                ),
                assignee=ROUTER_PROFILE,
                created_by=DESK_AUTHOR,
                workspace_kind="dir",
                workspace_path=str(workspace),
                idempotency_key=key,
            )
            return {"mode": mode, "root_id": task_id, "workspace": str(workspace)}

        created = kanban_swarm.create_swarm(
            conn,
            goal=_team_goal(request, workspace),
            workers=[
                kanban_swarm.SwarmWorkerSpec(profile=profile, title=f"{owns} — {title}", body=brief)
                for profile, owns, brief in TEAM_BRIEFS
            ],
            verifier_assignee=ROUTER_PROFILE,
            synthesizer_assignee=ROUTER_PROFILE,
            root_title=title,
            verifier_title=f"Verify: {title}",
            synthesizer_title=f"Final deliverable: {title}",
            created_by=DESK_AUTHOR,
            workspace_kind="dir",
            workspace_path=str(workspace),
            idempotency_key=key,
        )
        out = created.as_dict()
        out.update({"mode": mode, "workspace": str(workspace)})
        return out
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("one-ai-employee: job creation failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"could not start the job: {exc}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GoHighLevel Marketplace / Hermes integration endpoints
# ---------------------------------------------------------------------------
# Public URL prefix once mounted by Hermes dashboard:
#   /api/plugins/one-ai-employee/ghl/...
# These routes are deliberately small and self-contained. They let GHL reach
# Hermes without exposing the protected dashboard. Sensitive values are read
# from environment variables only and are never returned.

OAE_APP_ID = "6a595b18cd315b547ffbb455"
OAE_PUBLIC_BASE = "https://os.oneemployee.ai/api/plugins/one-ai-employee/connect"
OAE_REDIRECT_URI = f"{OAE_PUBLIC_BASE}/oauth/callback"
OAE_DB = Path(os.environ.get(
    "OAE_GHL_DB",
    str(Path.home() / "AppData/Local/hermes/profiles/ghl/one_ai_employee_ghl.sqlite3"),
))

GHL_EVENT_CATALOG = {
    "app_lifecycle": [
        "AppInstall", "AppUninstall", "AppUpdate", "AppRefresh", "PlanChange",
        "ExternalAuthConnected", "ExternalAuthDisconnected",
    ],
    "agency_saas_and_locations": [
        "CompanyCreate", "CompanyUpdate", "LocationCreate", "LocationUpdate",
        "LocationDelete", "LocationSettingsUpdate", "SaaSPlanCreate", "SaaSPlanUpdate",
        "SaaSPlanDelete", "SaaSSubscriptionCreate", "SaaSSubscriptionUpdate",
        "SaaSSubscriptionCancel", "SnapshotCreate", "SnapshotUpdate", "SnapshotPush",
    ],
    "crm_contacts": [
        "ContactCreate", "ContactUpdate", "ContactDelete", "ContactTagUpdate",
        "ContactDndUpdate", "ContactTaskCreate", "ContactTaskUpdate", "ContactTaskDelete",
        "ContactNoteCreate", "ContactNoteUpdate", "ContactNoteDelete",
    ],
    "conversations_messages": [
        "InboundMessage", "OutboundMessage", "MessageStatusUpdate", "ConversationCreate",
        "ConversationUpdate", "ConversationDelete", "ConversationUnread", "ConversationRead",
        "AppointmentMessage", "ReviewMessage", "CallStatusUpdate",
    ],
    "opportunities_pipelines": [
        "OpportunityCreate", "OpportunityUpdate", "OpportunityDelete", "OpportunityStatusUpdate",
        "OpportunityStageUpdate", "PipelineCreate", "PipelineUpdate", "PipelineStageCreate",
        "PipelineStageUpdate", "PipelineStageDelete",
    ],
    "calendars_appointments": [
        "AppointmentCreate", "AppointmentUpdate", "AppointmentDelete", "AppointmentStatusUpdate",
        "CalendarCreate", "CalendarUpdate", "CalendarDelete", "CalendarGroupCreate", "CalendarGroupUpdate",
    ],
    "payments_commerce": [
        "InvoiceCreate", "InvoiceUpdate", "InvoiceSent", "InvoicePaid", "InvoiceVoid",
        "OrderCreate", "OrderUpdate", "OrderFulfilled", "TransactionCreate", "TransactionUpdate",
        "PaymentCreate", "PaymentUpdate", "SubscriptionCreate", "SubscriptionUpdate",
        "SubscriptionCancel", "ProductCreate", "ProductUpdate", "ProductDelete",
    ],
    "marketing_content": [
        "FormSubmit", "SurveySubmit", "WorkflowTrigger", "WorkflowAction",
        "EmailEvent", "BlogPostCreate", "BlogPostUpdate", "SocialPostCreate", "SocialPostUpdate",
        "ReviewCreate", "ReviewUpdate", "ReviewRequestSent",
    ],
    "ai_voice": [
        "VoiceAiCallStart", "VoiceAiCallEnd", "VoiceAiTranscriptReady", "ConversationAiMessage",
        "ConversationAiHandoff", "AiAppointmentBooked",
    ],
}

GHL_MODULE_REGISTRY = [
    "oauth", "webhooks", "workflow_actions", "workflow_triggers", "custom_page",
    "custom_js", "snapshots", "agency_saas", "locations", "users", "contacts",
    "conversations", "opportunities", "calendars", "forms", "surveys", "payments",
    "invoices", "products", "blogs", "social_planner", "reputation", "voice_ai",
    "conversation_ai", "conversation_providers", "payment_providers", "custom_menus",
    "mcp_tools", "browser_agent", "agent_jobs", "audit_log", "entitlements",
]


def _oae_env(*names: str) -> str:
    for name in names:
        val = os.environ.get(name)
        if val:
            return val
    return ""


def _oae_db():
    OAE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(OAE_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS oauth_tokens ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, installed_at INTEGER, company_id TEXT, "
        "location_id TEXT, user_id TEXT, token_json TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS webhook_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, received_at INTEGER, event_type TEXT, "
        "company_id TEXT, location_id TEXT, payload_json TEXT NOT NULL)"
    )
    # --- tenant registry (multi-agency routing) ---
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tenants ("
        "tenant_id TEXT PRIMARY KEY,"          # agency_one_ai_employee / agency_<companyId> / location_<locationId>
        "kind TEXT NOT NULL,"                  # 'agency' | 'location'
        "company_id TEXT NOT NULL,"
        "location_id TEXT DEFAULT '',"
        "parent_tenant_id TEXT DEFAULT '',"    # location -> owning agency tenant
        "profile_id TEXT NOT NULL,"            # Hermes profile slug that operates this tenant
        "is_own_agency INTEGER DEFAULT 0,"
        "status TEXT DEFAULT 'active',"        # active | uninstalled
        "created_at INTEGER, updated_at INTEGER)"
    )
    # `oae_app` is part of the uniqueness key: our own agency can install BOTH
    # the agency app and the sub-account app, each yielding a Company grant under
    # the same tenant — they must NOT overwrite each other.
    installs_ddl = (
        "CREATE TABLE IF NOT EXISTS installs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "tenant_id TEXT NOT NULL,"
        "company_id TEXT NOT NULL,"
        "location_id TEXT DEFAULT '',"
        "user_id TEXT DEFAULT '',"
        "user_type TEXT DEFAULT '',"           # Company | Location
        "scopes TEXT DEFAULT '',"
        "token_json TEXT NOT NULL,"            # access+refresh; never returned by any endpoint
        "expires_at INTEGER DEFAULT 0,"
        "status TEXT DEFAULT 'installed',"     # installed | uninstalled | needs_reauth
        "installed_at INTEGER, updated_at INTEGER,"
        "oae_app TEXT DEFAULT 'agency',"       # which Marketplace app granted this (agency|sub)
        "UNIQUE(tenant_id, user_type, oae_app))"
    )
    conn.execute(installs_ddl)
    # Migrate a pre-`oae_app` installs table to the app-aware unique key. SQLite
    # can't alter a UNIQUE constraint in place, so recreate + copy (one-time,
    # idempotent, guarded so a half-migration can't clobber data).
    icols = [r[1] for r in conn.execute("PRAGMA table_info(installs)")]
    has_legacy = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='installs_legacy'").fetchone()
    if "oae_app" not in icols and not has_legacy:
        conn.execute("ALTER TABLE installs RENAME TO installs_legacy")
        conn.execute(installs_ddl)  # recreate with oae_app + new unique key
        for row in conn.execute(
            "SELECT tenant_id,company_id,location_id,user_id,user_type,scopes,"
            "token_json,expires_at,status,installed_at,updated_at FROM installs_legacy").fetchall():
            try:
                app_tag = (json.loads(row[6]) or {}).get("_oae_app") or "agency"
            except Exception:
                app_tag = "agency"
            conn.execute(
                "INSERT OR IGNORE INTO installs(tenant_id,company_id,location_id,user_id,"
                "user_type,scopes,token_json,expires_at,status,installed_at,updated_at,oae_app)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (*row, app_tag))
        conn.execute("DROP TABLE installs_legacy")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tenants_company ON tenants(company_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_installs_company ON installs(company_id)")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(webhook_events)")]
    if "tenant_id" not in cols:
        conn.execute("ALTER TABLE webhook_events ADD COLUMN tenant_id TEXT DEFAULT ''")
    conn.commit()
    return conn


def _json_body(payload: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code)


def _app_credentials(app: str) -> tuple[str, str, str]:
    """Resolve (client_id, client_secret, redirect_uri) for a Marketplace app.

    Two apps share this backend: the Agency app (default) and the Sub-Account
    app. Each is a distinct Marketplace app with its own client_id/secret and
    its own redirect path, so codes from each are exchanged with the right pair.
    """
    if app == "sub":
        return (
            _oae_env("GHL_SUB_CLIENT_ID", "GHL_SUBACCOUNT_CLIENT_ID"),
            _oae_env("GHL_SUB_CLIENT_SECRET", "GHL_SUBACCOUNT_CLIENT_SECRET"),
            _oae_env("GHL_SUB_REDIRECT_URI") or f"{OAE_PUBLIC_BASE}/sub/oauth/callback",
        )
    return (
        _oae_env("GHL_SANDBOX_CLIENT_ID", "GHL_CLIENT_ID", "GHL_MARKETPLACE_CLIENT_ID"),
        _oae_env("GHL_SANDBOX_CLIENT_SECRET", "GHL_CLIENT_SECRET", "GHL_MARKETPLACE_CLIENT_SECRET"),
        _oae_env("GHL_SANDBOX_REDIRECT_URI", "GHL_REDIRECT_URI") or OAE_REDIRECT_URI,
    )


def _token_exchange(code: str, app: str = "agency") -> tuple[bool, dict]:
    client_id, client_secret, redirect_uri = _app_credentials(app)
    if not client_id or not client_secret:
        return False, {"error": "setup_required", "missing": [
            n for n, v in {"client_id": client_id, "client_secret": client_secret}.items() if not v
        ]}
    form = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }
    data = urllib.parse.urlencode(form).encode("utf-8")

    # LeadConnector's Cloudflare edge has blocked Python urllib's default
    # fingerprint from this deployment before (403 / Error 1010). Prefer curl
    # when available because it matches the officially documented cURL flow and
    # passes the edge check from the same container.
    if shutil.which("curl"):
        try:
            args = [
                "curl", "-sS", "-X", "POST", "https://services.leadconnectorhq.com/oauth/token",
                "-H", "Content-Type: application/x-www-form-urlencoded",
                "-H", "Accept: application/json",
            ]
            for key, value in form.items():
                args.extend(["--data-urlencode", f"{key}={value}"])
            proc = subprocess.run(args, capture_output=True, text=True, timeout=25)
            raw = (proc.stdout or "").strip()
            if proc.returncode == 0:
                parsed = json.loads(raw or "{}")
                if parsed.get("access_token"):
                    return True, parsed
                return False, {"error": "token_exchange_failed", "status": parsed.get("status") or parsed.get("statusCode") or "curl_json", "body": raw[:1000]}
            return False, {"error": "token_exchange_failed", "status": "curl_failed", "body": (proc.stderr or raw)[:1000]}
        except Exception as exc:
            # Fall through to urllib so a missing/broken curl is not fatal.
            logging.warning("GHL OAuth curl token exchange failed; falling back to urllib: %s", exc)

    req = urllib.request.Request(
        "https://services.leadconnectorhq.com/oauth/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", "User-Agent": "curl/8.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return True, json.loads(raw or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:1000]
        return False, {"error": "token_exchange_failed", "status": exc.code, "body": body}
    except Exception as exc:
        return False, {"error": "token_exchange_failed", "detail": str(exc)}


def _save_token_legacy(token: dict):
    # Append-only audit trail of every grant received (pre-registry format).
    company_id = str(token.get("companyId") or token.get("company_id") or "")
    location_id = str(token.get("locationId") or token.get("location_id") or "")
    user_id = str(token.get("userId") or token.get("user_id") or "")
    conn = _oae_db()
    try:
        conn.execute(
            "INSERT INTO oauth_tokens(installed_at, company_id, location_id, user_id, token_json) VALUES (?, ?, ?, ?, ?)",
            (int(time.time()), company_id, location_id, user_id, json.dumps(token, separators=(",", ":"))),
        )
        conn.commit()
    finally:
        conn.close()


def _save_token(token: dict, app: str = "agency"):
    # Never log or return token_json. It is stored locally and routed to the
    # owning tenant (our own agency vs. a client agency) by the registry.
    _save_token_legacy(token)
    _register_install(token, app=app)
    # Agency-installed Sub-Account app: GHL returns a Company grant with NO
    # locationId and marks the chosen sub-accounts installed — it does not hand
    # back a location token. Discover the installed sub-accounts and mint a
    # location token for each so every selected client account becomes usable.
    company_id = str(token.get("companyId") or token.get("company_id") or "")
    location_id = str(token.get("locationId") or token.get("location_id") or "")
    user_type = str(token.get("userType") or token.get("user_type") or "")
    if app == "sub" and company_id and not location_id and user_type.lower() != "location":
        try:
            _sync_sub_installed_locations(company_id, token)
        except Exception as exc:  # never let sync break the install/callback
            logging.warning("OAE: sub-app location sync failed for company %s: %s", company_id, exc)


def _payload_type(payload: dict) -> str:
    for key in ("type", "event", "eventType", "webhookType", "messageType"):
        val = payload.get(key)
        if val:
            return str(val)
    return "unknown"


def _store_event(payload: dict):
    event_type = _payload_type(payload)
    company_id = str(payload.get("companyId") or payload.get("company_id") or "")
    location_id = str(payload.get("locationId") or payload.get("location_id") or "")

    # Route by companyId (+locationId). Lifecycle events maintain the registry;
    # everything else is stamped with the tenant that owns it so downstream
    # consumers (profiles, Adam, billing, audit) never have to re-derive it.
    tenant_id = ""
    if company_id or location_id:
        if event_type in ("INSTALL", "AppInstall"):
            tenant_id = _assign_tenant(company_id, location_id, status="active")["tenant_id"]
        elif event_type in ("UNINSTALL", "AppUninstall"):
            _mark_uninstalled(company_id, location_id)
            tenant_id = _route_tenant(company_id, location_id)["tenant_id"]
        else:
            tenant_id = _assign_tenant(company_id, location_id)["tenant_id"]

    conn = _oae_db()
    try:
        conn.execute(
            "INSERT INTO webhook_events(received_at, event_type, company_id, location_id, payload_json, tenant_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (int(time.time()), event_type, company_id, location_id,
             json.dumps(payload, separators=(",", ":")), tenant_id),
        )
        conn.commit()
    finally:
        conn.close()
    return event_type


# GoHighLevel Marketplace webhook signing keys (PUBLIC — safe to embed).
#   Current: Ed25519 signature, base64, in header `X-GHL-Signature`.
#   Legacy:  RSA-SHA256 signature, base64, in header `X-WH-Signature` (being
#            deprecated by GHL; kept as a fallback during the transition).
# Both sign the RAW request body. Keys are overridable via env in case GHL
# rotates them, so we never need a code change to follow a key rotation.
_GHL_ED25519_PUBKEY_PEM = _oae_env("GHL_WEBHOOK_ED25519_PUBLIC_KEY") or (
    "-----BEGIN PUBLIC KEY-----\n"
    "MCowBQYDK2VwAyEAi2HR1srL4o18O8BRa7gVJY7G7bupbN3H9AwJrHCDiOg=\n"
    "-----END PUBLIC KEY-----\n"
)
_GHL_RSA_PUBKEY_PEM = _oae_env("GHL_WEBHOOK_RSA_PUBLIC_KEY") or (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEAokvo/r9tVgcfZ5DysOSC\n"
    "Frm602qYV0MaAiNnX9O8KxMbiyRKWeL9JpCpVpt4XHIcBOK4u3cLSqJGOLaPuXw6\n"
    "dO0t6Q/ZVdAV5Phz+ZtzPL16iCGeK9po6D6JHBpbi989mmzMryUnQJezlYJ3DVfB\n"
    "csedpinheNnyYeFXolrJvcsjDtfAeRx5ByHQmTnSdFUzuAnC9/GepgLT9SM4nCpv\n"
    "uxmZMxrJt5Rw+VUaQ9B8JSvbMPpez4peKaJPZHBbU3OdeCVx5klVXXZQGNHOs8gF\n"
    "3kvoV5rTnXV0IknLBXlcKKAQLZcY/Q9rG6Ifi9c+5vqlvHPCUJFT5XUGG5RKgOKU\n"
    "J062fRtN+rLYZUV+BjafxQauvC8wSWeYja63VSUruvmNj8xkx2zE/Juc+yjLjTXp\n"
    "IocmaiFeAO6fUtNjDeFVkhf5LNb59vECyrHD2SQIrhgXpO4Q3dVNA5rw576PwTzN\n"
    "h/AMfHKIjE4xQA1SZuYJmNnmVZLIZBlQAF9Ntd03rfadZ+yDiOXCCs9FkHibELhC\n"
    "HULgCsnuDJHcrGNd5/Ddm5hxGQ0ASitgHeMZ0kcIOwKDOzOU53lDza6/Y09T7sYJ\n"
    "PQe7z0cvj7aE4B+Ax1ZoZGPzpJlZtGXCsu9aTEGEnKzmsFqwcSsnw3JB31IGKAyk\n"
    "T1hhTiaCeIY/OwwwNUY2yvcCAwEAAQ==\n"
    "-----END PUBLIC KEY-----\n"
)
_WEBHOOK_KEYCACHE: dict = {}


def _load_webhook_pubkey(kind: str):
    """Load + cache a GHL webhook public key. Returns None if unavailable."""
    if kind in _WEBHOOK_KEYCACHE:
        return _WEBHOOK_KEYCACHE[kind]
    key = None
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        pem = _GHL_ED25519_PUBKEY_PEM if kind == "ed25519" else _GHL_RSA_PUBKEY_PEM
        key = load_pem_public_key(pem.encode("utf-8"))
    except Exception as exc:  # missing dep or malformed key -> can't verify
        logging.warning("OAE: webhook %s key unavailable: %s", kind, exc)
    _WEBHOOK_KEYCACHE[kind] = key
    return key


def _verify_ed25519(raw: bytes, sig_b64: str) -> bool:
    key = _load_webhook_pubkey("ed25519")
    if key is None:
        return False
    try:
        key.verify(base64.b64decode(sig_b64), raw)
        return True
    except Exception:
        return False


def _verify_rsa_sha256(raw: bytes, sig_b64: str) -> bool:
    key = _load_webhook_pubkey("rsa")
    if key is None:
        return False
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        key.verify(base64.b64decode(sig_b64), raw, padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


def _verify_webhook_signature(raw: bytes, headers) -> tuple[bool, str]:
    """Verify a GoHighLevel Marketplace webhook.

    Current: Ed25519 over the raw body, base64 in ``X-GHL-Signature``.
    Legacy:  RSA-SHA256 over the raw body, base64 in ``X-WH-Signature``.

    A present signature is ALWAYS verified strictly — an invalid one is
    rejected. If no signature header is present, we accept in setup-mode
    (reported as such) unless GHL_WEBHOOK_REQUIRE_SIGNATURE is set, in which
    case unsigned requests are rejected. An optional shared-secret HMAC path
    remains for non-marketplace/manual testing only.
    """
    ghl_sig = headers.get("x-ghl-signature")
    if ghl_sig:
        ok = _verify_ed25519(raw, ghl_sig)
        return ok, ("verified_ed25519" if ok else "invalid_ed25519")
    wh_sig = headers.get("x-wh-signature")
    if wh_sig:
        ok = _verify_rsa_sha256(raw, wh_sig)
        return ok, ("verified_rsa_legacy" if ok else "invalid_rsa")
    # Optional shared-secret HMAC (manual testing / non-marketplace senders).
    secret = _oae_env("GHL_WEBHOOK_SHARED_SECRET", "GHL_MARKETPLACE_SHARED_SECRET", "WEBHOOK_SECRET")
    if secret:
        received = headers.get("x-lc-signature") or headers.get("x-hub-signature-256") or ""
        if received:
            digest = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
            ok = any(secrets.compare_digest(received, v) for v in (digest, f"sha256={digest}"))
            return ok, ("verified_hmac" if ok else "invalid_hmac")
    require = (_oae_env("GHL_WEBHOOK_REQUIRE_SIGNATURE") or "").strip().lower() in ("1", "true", "yes", "on")
    if require:
        return False, "missing_signature"
    return True, "unsigned_setup_mode"


# ---------------------------------------------------------------------------
# Tenant registry + profile router
#
# Three concepts, kept separate on purpose:
#   admin (Adam/COO profile)  — supervises; never owns tenant tokens
#   agency tenants            — one per GHL company (ours = agency_one_ai_employee)
#   location tenants          — child of an agency, one per sub-account
#
# Every install/token/webhook is routed by companyId (+locationId) to a tenant,
# and each tenant carries the Hermes profile slug that operates it. Our own
# agency is just a regular tenant that happens to match OAE_OWN_COMPANY_ID.
# ---------------------------------------------------------------------------

# Which GHL company is One AI Employee's own agency. Overridable so the real
# production agency can take over from the sandbox without code changes.
OAE_OWN_COMPANY_ID = _oae_env("OAE_OWN_COMPANY_ID") or "fWcUpUDvbv9geCm4YHU7"
OAE_OWN_AGENCY_TENANT = "agency_one_ai_employee"
_LC_BASE = "https://services.leadconnectorhq.com"
_LC_VERSION = "2021-07-28"
_TOKEN_REFRESH_MARGIN = 300  # refresh when <5 min of life remains


def _route_tenant(company_id: str, location_id: str = "") -> dict:
    """Pure routing decision: companyId (+locationId) -> tenant/profile identity."""
    company_id = (company_id or "").strip()
    location_id = (location_id or "").strip()
    if location_id:
        agency = _route_tenant(company_id)
        return {
            "tenant_id": f"location_{location_id}",
            "kind": "location",
            "company_id": company_id,
            "location_id": location_id,
            "parent_tenant_id": agency["tenant_id"] if company_id else "",
            "profile_id": f"location_{location_id}",
            "is_own_agency": agency["is_own_agency"] if company_id else 0,
        }
    is_own = 1 if (company_id and company_id == OAE_OWN_COMPANY_ID) else 0
    tenant_id = OAE_OWN_AGENCY_TENANT if is_own else f"agency_{company_id}"
    return {
        "tenant_id": tenant_id,
        "kind": "agency",
        "company_id": company_id,
        "location_id": "",
        "parent_tenant_id": "",
        "profile_id": tenant_id,
        "is_own_agency": is_own,
    }


def _assign_tenant(company_id: str, location_id: str = "", status: str = "active") -> dict:
    """Upsert the tenant row for a companyId/locationId and return its identity.

    For a location under a known company, the parent agency tenant is created
    too, so a location webhook can never arrive before its agency exists.
    """
    ident = _route_tenant(company_id, location_id)
    if not ident["company_id"] and not ident["location_id"]:
        return ident  # nothing identifiable to register
    now = int(time.time())
    conn = _oae_db()
    try:
        if ident["kind"] == "location" and ident["company_id"]:
            parent = _route_tenant(ident["company_id"])
            conn.execute(
                "INSERT INTO tenants(tenant_id, kind, company_id, location_id, parent_tenant_id,"
                " profile_id, is_own_agency, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(tenant_id) DO UPDATE SET updated_at=excluded.updated_at",
                (parent["tenant_id"], parent["kind"], parent["company_id"], "", "",
                 parent["profile_id"], parent["is_own_agency"], "active", now, now),
            )
        conn.execute(
            "INSERT INTO tenants(tenant_id, kind, company_id, location_id, parent_tenant_id,"
            " profile_id, is_own_agency, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(tenant_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at",
            (ident["tenant_id"], ident["kind"], ident["company_id"], ident["location_id"],
             ident["parent_tenant_id"], ident["profile_id"], ident["is_own_agency"], status, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return ident


def _register_install(token: dict, app: str = "agency") -> dict:
    """Store an OAuth grant under its tenant. Returns the tenant identity.

    `app` records which Marketplace app minted/granted this token ('agency' or
    'sub'), so a later refresh uses the matching client_id/secret pair.
    """
    token.setdefault("_oae_app", app)
    company_id = str(token.get("companyId") or token.get("company_id") or "")
    location_id = str(token.get("locationId") or token.get("location_id") or "")
    user_id = str(token.get("userId") or token.get("user_id") or "")
    user_type = str(token.get("userType") or token.get("user_type") or "")
    scopes = str(token.get("scope") or "")
    expires_at = int(time.time()) + int(token.get("expires_in") or 0)
    ident = _assign_tenant(company_id, location_id, status="active")
    now = int(time.time())
    conn = _oae_db()
    try:
        conn.execute(
            "INSERT INTO installs(tenant_id, company_id, location_id, user_id, user_type,"
            " scopes, token_json, expires_at, status, installed_at, updated_at, oae_app)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(tenant_id, user_type, oae_app) DO UPDATE SET"
            " company_id=excluded.company_id, location_id=excluded.location_id,"
            " user_id=excluded.user_id, scopes=excluded.scopes, token_json=excluded.token_json,"
            " expires_at=excluded.expires_at, status='installed', updated_at=excluded.updated_at",
            (ident["tenant_id"], company_id, location_id, user_id, user_type,
             scopes, json.dumps(token, separators=(",", ":")), expires_at, "installed", now, now, app),
        )
        conn.commit()
    finally:
        conn.close()
    return ident


def _mark_uninstalled(company_id: str, location_id: str = ""):
    ident = _route_tenant(company_id, location_id)
    now = int(time.time())
    conn = _oae_db()
    try:
        conn.execute("UPDATE installs SET status='uninstalled', updated_at=? WHERE tenant_id=?",
                     (now, ident["tenant_id"]))
        conn.execute("UPDATE tenants SET status='uninstalled', updated_at=? WHERE tenant_id=?",
                     (now, ident["tenant_id"]))
        conn.commit()
    finally:
        conn.close()


def _migrate_legacy_tokens():
    """One-time, idempotent: lift rows from the pre-registry oauth_tokens table
    into tenants/installs so the first install (done before the registry
    existed) is routed like every later one."""
    conn = _oae_db()
    try:
        have = conn.execute("SELECT COUNT(*) FROM installs").fetchone()[0]
        legacy = conn.execute(
            "SELECT token_json FROM oauth_tokens ORDER BY id DESC").fetchall()
    finally:
        conn.close()
    if have or not legacy:
        return 0
    migrated = 0
    seen: set[tuple] = set()
    for (tj,) in legacy:  # newest first; keep the freshest grant per tenant
        try:
            token = json.loads(tj)
        except json.JSONDecodeError:
            continue
        key = (str(token.get("companyId") or ""), str(token.get("locationId") or ""),
               str(token.get("userType") or ""))
        if key in seen:
            continue
        seen.add(key)
        _register_install(token)
        migrated += 1
    return migrated


def _lc_request(method: str, path: str, *, token: str = "", query: dict | None = None,
                body: dict | None = None, form: dict | None = None) -> dict:
    """Call the LeadConnector API. curl-first (Cloudflare blocks urllib's
    fingerprint from this container), urllib fallback. Returns
    {ok, status, data} and never raises."""
    qs = urllib.parse.urlencode(query or {}, doseq=True)
    url = _LC_BASE + path + (("?" + qs) if qs else "")
    headers = {"Accept": "application/json", "Version": _LC_VERSION}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data_bytes = None
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data_bytes = urllib.parse.urlencode(form).encode("utf-8")
    elif body is not None:
        headers["Content-Type"] = "application/json"
        data_bytes = json.dumps(body).encode("utf-8")

    if shutil.which("curl"):
        try:
            args = ["curl", "-sS", "-o", "-", "-w", "\n%{http_code}", "-X", method.upper(), url]
            for k, v in headers.items():
                args += ["-H", f"{k}: {v}"]
            if form is not None:
                for k, v in form.items():
                    args += ["--data-urlencode", f"{k}={v}"]
            elif data_bytes is not None:
                args += ["--data-binary", data_bytes.decode("utf-8")]
            proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
            if proc.returncode == 0 and "\n" in (proc.stdout or ""):
                raw, _, code = proc.stdout.rpartition("\n")
                status = int(code.strip() or 0)
                try:
                    parsed = json.loads(raw) if raw.strip() else None
                except json.JSONDecodeError:
                    parsed = raw[:1000]
                return {"ok": 200 <= status < 300, "status": status, "data": parsed}
        except Exception as exc:
            logging.warning("LC curl request failed; falling back to urllib: %s", exc)

    headers["User-Agent"] = "curl/8.0"
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return {"ok": True, "status": resp.status, "data": json.loads(raw) if raw.strip() else None}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")[:1000]
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        return {"ok": False, "status": exc.code, "data": parsed}
    except Exception as exc:
        return {"ok": False, "status": 0, "data": str(exc)[:400]}


def _refresh_install(install: dict) -> tuple[bool, dict]:
    """Exchange the refresh token for a fresh grant and persist it."""
    try:
        token = json.loads(install["token_json"])
    except (KeyError, json.JSONDecodeError):
        return False, {"error": "corrupt_token_record"}
    refresh = token.get("refresh_token")
    if not refresh:
        return False, {"error": "no_refresh_token"}
    app = token.get("_oae_app", "agency")
    client_id, client_secret, _ = _app_credentials(app)
    if not client_id or not client_secret:
        return False, {"error": "setup_required"}
    res = _lc_request("POST", "/oauth/token", form={
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh,
        "user_type": token.get("userType") or "Company",
    })
    if not res["ok"] or not isinstance(res["data"], dict) or not res["data"].get("access_token"):
        conn = _oae_db()
        try:
            conn.execute("UPDATE installs SET status='needs_reauth', updated_at=? WHERE id=?",
                         (int(time.time()), install["id"]))
            conn.commit()
        finally:
            conn.close()
        return False, {"error": "refresh_failed", "status": res["status"]}
    new_token = res["data"]
    # GHL may omit identity fields on refresh; carry them over from the old grant.
    for k in ("companyId", "locationId", "userId", "userType", "scope"):
        new_token.setdefault(k, token.get(k))
    _register_install(new_token, app=app)
    _save_token_legacy(new_token)
    return True, new_token


def _tenant_row(tenant_id: str) -> dict | None:
    conn = _oae_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tenants WHERE tenant_id=?", (tenant_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _agency_mint_token(tenant_id: str) -> tuple[bool, str | dict]:
    """Return a valid agency access token that can mint location tokens.

    /oauth/locationToken requires the `oauth.write` scope. Our own agency may
    hold several Company grants (agency app + sub app) under one tenant, so
    prefer a grant that actually carries oauth.write, refreshing if stale.
    """
    conn = _oae_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM installs WHERE tenant_id=? AND status!='uninstalled'"
            " ORDER BY updated_at DESC", (tenant_id,)).fetchall()]
    finally:
        conn.close()
    # oauth.write-bearing grants first, then the rest as a fallback.
    ordered = ([r for r in rows if "oauth.write" in (r.get("scopes") or "")]
               + [r for r in rows if "oauth.write" not in (r.get("scopes") or "")])
    last_err: dict = {"error": "no_install_for_tenant", "tenant_id": tenant_id}
    for r in ordered:
        stale = bool(r["expires_at"] and r["expires_at"] - int(time.time()) < _TOKEN_REFRESH_MARGIN)
        if stale:
            ok, refreshed = _refresh_install(r)
            if not ok:
                last_err = refreshed
                continue
            return True, refreshed["access_token"]
        try:
            tok = json.loads(r["token_json"])
        except json.JSONDecodeError:
            last_err = {"error": "corrupt_token_record"}
            continue
        if tok.get("access_token"):
            return True, tok["access_token"]
    return False, last_err


def _sync_sub_installed_locations(company_id: str, source_token: dict) -> int:
    """After an agency installs the Sub-Account app, mint a location token for
    every sub-account it was installed on. GHL's agency install returns a
    Company grant + marks locations installed; the location tokens are minted.
    Returns the number of locations minted. Best-effort; never raises."""
    app_id = str(source_token.get("appId") or "")
    if not app_id:
        sub_cid, _, _ = _app_credentials("sub")
        app_id = sub_cid.split("-")[0] if sub_cid else ""
    access = source_token.get("access_token")
    if not access or not app_id:
        return 0
    res = _lc_request("GET", "/oauth/installedLocations", token=access,
                      query={"companyId": company_id, "appId": app_id, "limit": 100})
    if not res["ok"] or not isinstance(res["data"], dict):
        logging.warning("OAE: installedLocations lookup failed for %s (status %s)",
                        company_id, res.get("status"))
        return 0
    minted = 0
    locations = res["data"].get("locations") or []
    for loc in locations:
        lid = loc.get("_id") or loc.get("id") or loc.get("locationId")
        if not lid or loc.get("isInstalled") is False:
            continue
        _assign_tenant(company_id, lid)
        ok, _res = _mint_location_token(company_id, lid)
        if ok:
            minted += 1
    logging.info("OAE: sub-app install sync minted %d/%d location(s) for company %s",
                 minted, len(locations), company_id)
    return minted


def _mint_location_token(company_id: str, location_id: str) -> tuple[bool, dict]:
    """Mint a location-scoped token from the owning agency's Company token.

    POST /oauth/locationToken needs the oauth.write scope on the agency grant.
    The minted token is registered under the location tenant (kind=location,
    user_type=Location), which the router auto-creates as a child of the agency.
    Location tokens carry no refresh_token — when they expire we mint again.
    """
    agency = _route_tenant(company_id)
    ok, tok = _agency_mint_token(agency["tenant_id"])
    if not ok:
        return False, {"error": "agency_token_unavailable", "detail": tok}
    res = _lc_request("POST", "/oauth/locationToken", token=tok,
                      form={"companyId": company_id, "locationId": location_id})
    if not res["ok"] or not isinstance(res["data"], dict) or not res["data"].get("access_token"):
        detail = res["data"] if isinstance(res["data"], dict) else str(res["data"])[:300]
        if res["status"] in (401, 403):
            return False, {"error": "mint_unauthorized", "status": res["status"],
                           "hint": "The agency grant likely lacks the oauth.write scope — "
                                   "add oauth.readonly + oauth.write on the Marketplace Auth "
                                   "page and re-install/re-authorize the app.",
                           "detail": detail}
        return False, {"error": "mint_failed", "status": res["status"], "detail": detail}
    token = res["data"]
    token.setdefault("companyId", company_id)
    token.setdefault("locationId", location_id)
    token.setdefault("userType", "Location")
    _register_install(token)
    _save_token_legacy(token)
    return True, token


def _install_for_tenant(tenant_id: str, prefer_app: str = "") -> dict | None:
    """Most-recent active install for a tenant.

    A tenant can hold grants from BOTH Marketplace apps (e.g. our own agency has
    an agency-app AND a sub-app Company grant). `prefer_app` picks the right one
    for the job — agency-level work wants the 'agency' grant (agency scopes),
    location work wants the richer 'sub' grant — falling back to the freshest
    grant of any app when the preferred one isn't present.
    """
    conn = _oae_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM installs WHERE tenant_id=? AND status!='uninstalled'"
            " ORDER BY updated_at DESC", (tenant_id,)).fetchall()]
    finally:
        conn.close()
    if not rows:
        return None
    if prefer_app:
        for r in rows:
            if (r.get("oae_app") or "") == prefer_app:
                return r
    return rows[0]


def _tenant_access_token(tenant_id: str) -> tuple[bool, str | dict]:
    """Return a currently-valid access token for a tenant.

    Agency tenants refresh via their refresh_token. Location tenants have no
    refresh_token — a stale or missing location grant is re-minted from the
    parent agency's token instead.
    """
    row = _tenant_row(tenant_id)
    # Agency-level work needs the agency-app grant (companies/snapshots/saas);
    # location work prefers the richer direct sub-account grant.
    prefer = "agency" if (row and row.get("kind") == "agency") else "sub"
    install = _install_for_tenant(tenant_id, prefer_app=prefer)
    stale = bool(install and install["expires_at"]
                 and install["expires_at"] - int(time.time()) < _TOKEN_REFRESH_MARGIN)
    if not install or stale:
        if row and row["kind"] == "location" and row["company_id"]:
            ok, minted = _mint_location_token(row["company_id"], row["location_id"])
            if not ok:
                return False, minted
            return True, minted["access_token"]
        if not install:
            return False, {"error": "no_install_for_tenant", "tenant_id": tenant_id}
    if stale:
        ok, refreshed = _refresh_install(install)
        if not ok:
            return False, refreshed
        return True, refreshed["access_token"]
    try:
        token = json.loads(install["token_json"])
    except json.JSONDecodeError:
        return False, {"error": "corrupt_token_record"}
    access = token.get("access_token")
    if not access:
        return False, {"error": "no_access_token"}
    return True, access


def _tenant_api(tenant_id: str, method: str, path: str, *, query: dict | None = None,
                body: dict | None = None) -> dict:
    """Authenticated LeadConnector call on behalf of a tenant."""
    ok, tok = _tenant_access_token(tenant_id)
    if not ok:
        return {"ok": False, "status": 0, "data": tok}
    return _lc_request(method, path, token=tok, query=query, body=body)


@router.get("/connect/health")
@router.get("/ghl/health")
def ghl_health():
    conn = _oae_db()
    try:
        n_tenants = conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
        n_installs = conn.execute(
            "SELECT COUNT(*) FROM installs WHERE status='installed'").fetchone()[0]
    finally:
        conn.close()
    ag_id, ag_secret, _ = _app_credentials("agency")
    sub_id, sub_secret, sub_redirect = _app_credentials("sub")
    return {
        "ok": True,
        "service": "one-ai-employee-ghl",
        "app_id": OAE_APP_ID,
        "public_base": OAE_PUBLIC_BASE,
        "redirect_uri": OAE_REDIRECT_URI,
        "modules": len(GHL_MODULE_REGISTRY),
        "webhook_categories": len(GHL_EVENT_CATALOG),
        "tenants": n_tenants,
        "active_installs": n_installs,
        "apps": {
            "agency": {"configured": bool(ag_id and ag_secret)},
            "sub_account": {"configured": bool(sub_id and sub_secret),
                            "redirect_uri": sub_redirect},
        },
    }


@router.get("/connect/registry")
@router.get("/ghl/registry")
def ghl_registry():
    return {
        "ok": True,
        "app_id": OAE_APP_ID,
        "modules": GHL_MODULE_REGISTRY,
        "webhook_events": GHL_EVENT_CATALOG,
        "urls": {
            "oauth_callback": "https://os.oneemployee.ai/api/plugins/one-ai-employee/connect/oauth/callback",
            "sub_oauth_callback": "https://os.oneemployee.ai/api/plugins/one-ai-employee/connect/sub/oauth/callback",
            "webhook": "https://os.oneemployee.ai/api/plugins/one-ai-employee/connect/webhooks",
            "workflow_action": "https://os.oneemployee.ai/api/plugins/one-ai-employee/connect/webhooks/workflow-action",
            "trigger_subscription": "https://os.oneemployee.ai/api/plugins/one-ai-employee/connect/webhooks/trigger-subscription",
            "custom_page": "https://os.oneemployee.ai/api/plugins/one-ai-employee/connect/dashboard",
            "custom_js": "https://os.oneemployee.ai/api/plugins/one-ai-employee/connect/custom.js",
        },
    }


# ---------------------------------------------------------------------------
# Tenant admin endpoints — deliberately OUTSIDE the public /connect and /ghl
# prefixes, so they sit behind the dashboard's normal auth. They expose
# registry state and run live verification, but never token material.
# ---------------------------------------------------------------------------

def _tenant_public_row(row: dict, install: dict | None) -> dict:
    out = {k: row[k] for k in ("tenant_id", "kind", "company_id", "location_id",
                               "parent_tenant_id", "profile_id", "is_own_agency",
                               "status", "created_at", "updated_at")}
    if install:
        out["install"] = {
            "status": install["status"],
            "user_type": install["user_type"],
            "user_id": install["user_id"],
            "scope_count": len((install["scopes"] or "").split()),
            "expires_at": install["expires_at"],
            "expires_in_s": max(0, install["expires_at"] - int(time.time())) if install["expires_at"] else None,
            "installed_at": install["installed_at"],
            "updated_at": install["updated_at"],
        }
    else:
        out["install"] = None
    return out


@router.get("/tenants")
def tenants_list():
    migrated = _migrate_legacy_tokens()
    conn = _oae_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM tenants ORDER BY is_own_agency DESC, created_at ASC").fetchall()]
    finally:
        conn.close()
    return {
        "ok": True,
        "own_company_id": OAE_OWN_COMPANY_ID,
        "own_agency_tenant": OAE_OWN_AGENCY_TENANT,
        "migrated_from_legacy": migrated,
        "count": len(rows),
        "tenants": [_tenant_public_row(r, _install_for_tenant(r["tenant_id"])) for r in rows],
    }


@router.get("/tenants/{tenant_id}")
def tenants_detail(tenant_id: str):
    _migrate_legacy_tokens()
    conn = _oae_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tenants WHERE tenant_id=?", (tenant_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="unknown tenant")
        children = [dict(r) for r in conn.execute(
            "SELECT * FROM tenants WHERE parent_tenant_id=? ORDER BY created_at", (tenant_id,)).fetchall()]
        events = [
            {"id": r["id"], "received_at": r["received_at"], "event_type": r["event_type"],
             "location_id": r["location_id"]}
            for r in conn.execute(
                "SELECT * FROM webhook_events WHERE tenant_id=? ORDER BY id DESC LIMIT 20", (tenant_id,)).fetchall()
        ]
    finally:
        conn.close()
    detail = _tenant_public_row(dict(row), _install_for_tenant(tenant_id))
    detail["children"] = [_tenant_public_row(c, _install_for_tenant(c["tenant_id"])) for c in children]
    detail["recent_events"] = events
    return {"ok": True, "tenant": detail}


def _verify_agency_tenant(tenant_id: str, company_id: str) -> dict:
    """Phase-2 proof: exercise the stored agency token against real agency APIs.

    Each check reports honestly — a 403 on SaaS endpoints usually means a plan
    gate ($497/mo for the SaaS API), not a broken integration.
    """
    checks: dict[str, dict] = {}

    def run(name: str, method: str, path: str, query: dict | None = None, summarize=None):
        res = _tenant_api(tenant_id, method, path, query=query)
        entry = {"ok": res["ok"], "status": res["status"]}
        if res["ok"] and summarize:
            try:
                entry["summary"] = summarize(res["data"])
            except Exception:
                entry["summary"] = None
        if not res["ok"]:
            data = res["data"]
            entry["error"] = (data.get("message") or data.get("error") if isinstance(data, dict) else str(data))[:200]
        checks[name] = entry

    run("company_details", "GET", f"/companies/{company_id}",
        summarize=lambda d: {"name": (d.get("company") or {}).get("name"),
                             "plan": (d.get("company") or {}).get("subscriptionId") or
                                     (d.get("company") or {}).get("stripeActivePlan")})
    run("installed_locations", "GET", "/oauth/installedLocations",
        query={"companyId": company_id, "appId": OAE_APP_ID, "limit": 20},
        summarize=lambda d: {"count": d.get("count", len(d.get("locations", []) or []))})
    run("locations_search", "GET", "/locations/search",
        query={"companyId": company_id, "limit": 10},
        summarize=lambda d: {"locations": [{"id": l.get("_id") or l.get("id"), "name": l.get("name")}
                                           for l in (d.get("locations") or [])[:10]]})
    run("snapshots", "GET", "/snapshots/", query={"companyId": company_id},
        summarize=lambda d: {"count": len(d.get("snapshots") or [])})
    # Read-only SaaS proof. A 402/403 here usually means the agency plan gate
    # ($497/mo for the SaaS API), not a broken token — report it as-is.
    run("saas_agency_plans", "GET", f"/saas-api/public-api/agency-plans/{company_id}",
        summarize=lambda d: {"plans": len(d if isinstance(d, list) else (d.get("plans") or d.get("data") or []))})
    passed = sum(1 for c in checks.values() if c["ok"])
    return {"tenant_id": tenant_id, "company_id": company_id,
            "passed": passed, "total": len(checks), "checks": checks}


def _verify_location_tenant(tenant_id: str, location_id: str) -> dict:
    """Prove the location token works inside its own location — and only there."""
    checks: dict[str, dict] = {}
    res = _tenant_api(tenant_id, "GET", f"/locations/{location_id}")
    entry = {"ok": res["ok"], "status": res["status"]}
    if res["ok"] and isinstance(res["data"], dict):
        loc = res["data"].get("location") or res["data"]
        entry["summary"] = {"name": loc.get("name"), "id": loc.get("id") or loc.get("_id")}
    elif not res["ok"]:
        entry["error"] = str(res["data"])[:200]
    checks["location_details"] = entry
    passed = sum(1 for c in checks.values() if c["ok"])
    return {"tenant_id": tenant_id, "location_id": location_id,
            "passed": passed, "total": len(checks), "checks": checks}


@router.post("/tenants/{tenant_id}/verify")
def tenants_verify(tenant_id: str):
    _migrate_legacy_tokens()
    row = _tenant_row(tenant_id)
    if not row:
        raise HTTPException(status_code=404, detail="unknown tenant")
    if row["kind"] == "location":
        # Location grants can be minted on demand — no pre-existing install needed.
        return {"ok": True, **_verify_location_tenant(tenant_id, row["location_id"])}
    if not _install_for_tenant(tenant_id):
        raise HTTPException(status_code=409, detail="tenant has no active install/token")
    return {"ok": True, **_verify_agency_tenant(tenant_id, row["company_id"])}


@router.post("/tenants/{tenant_id}/locations/{location_id}/mint")
def tenants_mint_location(tenant_id: str, location_id: str):
    """Mint (or re-mint) a location token from an agency tenant's grant.

    Registers the location as a child tenant and stores the grant under it.
    Returns registry metadata only — never token material.
    """
    _migrate_legacy_tokens()
    row = _tenant_row(tenant_id)
    if not row:
        raise HTTPException(status_code=404, detail="unknown tenant")
    if row["kind"] != "agency":
        raise HTTPException(status_code=400, detail="mint from an agency tenant")
    ident = _assign_tenant(row["company_id"], location_id)
    ok, res = _mint_location_token(row["company_id"], location_id)
    if not ok:
        return _json_body({"ok": False, **res}, status_code=502)
    return {
        "ok": True,
        "tenant": _tenant_public_row(_tenant_row(ident["tenant_id"]),
                                     _install_for_tenant(ident["tenant_id"])),
    }


# ---------------------------------------------------------------------------
# Branded connection pages (One AI Employee)
#
# These are the only pages a customer sees during the GoHighLevel handshake, so
# they carry the brand — not the underlying engine. Fully self-contained: the
# page renders after a GHL redirect (sometimes inside a sandboxed popup with no
# network), so there are NO external fonts, scripts, or assets. Everything —
# type, aurora background, grain, the animated signal orb — is inline CSS.
# ---------------------------------------------------------------------------

_OAE_ICON_CHECK = (
    "<svg viewBox='0 0 48 48' fill='none' aria-hidden='true'>"
    "<path class='draw' d='M14 25 21 32 34 17' stroke='currentColor' stroke-width='3.4' "
    "stroke-linecap='round' stroke-linejoin='round'/></svg>"
)
_OAE_ICON_SIGNAL = (
    "<svg viewBox='0 0 48 48' fill='none' aria-hidden='true'>"
    "<circle cx='24' cy='32' r='2.7' fill='currentColor'/>"
    "<path d='M16 27a11 11 0 0 1 16 0' stroke='currentColor' stroke-width='3' stroke-linecap='round'/>"
    "<path d='M11 21a18 18 0 0 1 26 0' stroke='currentColor' stroke-width='3' stroke-linecap='round' opacity='.5'/>"
    "</svg>"
)
_OAE_ICON_ALERT = (
    "<svg viewBox='0 0 48 48' fill='none' aria-hidden='true'>"
    "<path d='M24 16v11' stroke='currentColor' stroke-width='3.4' stroke-linecap='round'/>"
    "<circle cx='24' cy='33' r='2' fill='currentColor'/>"
    "<path d='M24 9 41 37H7L24 9Z' stroke='currentColor' stroke-width='3' stroke-linejoin='round' opacity='.5'/>"
    "</svg>"
)

_OAE_PAGE_TMPL = """<!doctype html><html lang='en'><head><meta charset='utf-8'>
<title>@@TITLE@@ &middot; One AI Employee</title>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<meta name='robots' content='noindex'>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--accent:@@ACCENT@@;--ink:#eaf0ff;--muted:#8b96b4;--bg:#060912}
html,body{height:100%}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;background:var(--bg);
 color:var(--ink);min-height:100vh;display:grid;place-items:center;padding:28px;position:relative;
 overflow:hidden;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
body::before{content:"";position:fixed;inset:-30%;z-index:0;pointer-events:none;
 background:
  radial-gradient(38% 42% at 20% 16%,color-mix(in srgb,var(--accent) 32%,transparent),transparent 70%),
  radial-gradient(40% 44% at 84% 24%,rgba(76,155,232,.20),transparent 72%),
  radial-gradient(52% 52% at 50% 112%,rgba(167,139,250,.18),transparent 70%);
 filter:blur(6px);animation:drift 22s ease-in-out infinite alternate}
body::after{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.05;
 background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='150' height='150'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
.card{position:relative;z-index:1;width:100%;max-width:452px;padding:46px 42px 32px;text-align:center;
 border-radius:26px;border:1px solid rgba(255,255,255,.09);
 background:linear-gradient(180deg,rgba(255,255,255,.06),rgba(255,255,255,.02));
 backdrop-filter:blur(22px);-webkit-backdrop-filter:blur(22px);
 box-shadow:0 1px 0 rgba(255,255,255,.08) inset,0 40px 90px -34px rgba(0,0,0,.85)}
.card>*{opacity:0;animation:rise .8s cubic-bezier(.2,.7,.2,1) forwards}
.card>*:nth-child(1){animation-delay:.05s}.card>*:nth-child(2){animation-delay:.14s}
.card>*:nth-child(3){animation-delay:.22s}.card>*:nth-child(4){animation-delay:.3s}
.card>*:nth-child(5){animation-delay:.38s}.card>*:nth-child(6){animation-delay:.46s}
.orb{width:104px;height:104px;margin:0 auto 26px;position:relative;display:grid;place-items:center;color:var(--accent)}
.orb i{position:absolute;inset:0;border-radius:50%;border:1px solid color-mix(in srgb,var(--accent) 55%,transparent);
 animation:ping 2.6s cubic-bezier(.2,.7,.2,1) infinite}
.orb i:nth-child(2){animation-delay:.9s}.orb i:nth-child(3){animation-delay:1.8s}
.orb b{position:relative;width:70px;height:70px;border-radius:50%;display:grid;place-items:center;
 background:radial-gradient(circle at 50% 34%,color-mix(in srgb,var(--accent) 28%,transparent),rgba(255,255,255,.03));
 border:1px solid color-mix(in srgb,var(--accent) 42%,transparent);
 box-shadow:0 0 46px -6px color-mix(in srgb,var(--accent) 60%,transparent)}
.orb svg{width:38px;height:38px}
.orb .draw{stroke-dasharray:46;stroke-dashoffset:46;animation:draw .7s .55s cubic-bezier(.6,0,.2,1) forwards}
.eyebrow{font-size:11px;font-weight:600;letter-spacing:.34em;text-transform:uppercase;color:var(--accent);margin-bottom:16px}
h1{font-size:29px;line-height:1.12;font-weight:640;letter-spacing:-.022em;margin-bottom:12px}
.sub{font-size:15px;line-height:1.62;color:var(--muted);max-width:35ch;margin:0 auto}
.meta{display:flex;flex-wrap:wrap;justify-content:center;gap:8px;margin-top:26px}
.chip{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;font-size:11.5px;letter-spacing:.01em;
 color:#c7d0ec;padding:6px 11px;border-radius:999px;border:1px solid rgba(255,255,255,.1);
 background:rgba(255,255,255,.03);display:inline-flex;align-items:center;gap:6px}
.chip::before{content:"";width:5px;height:5px;border-radius:50%;background:var(--accent);box-shadow:0 0 8px var(--accent)}
.detail{margin-top:22px;text-align:left;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;line-height:1.6;
 color:#aeb8d6;background:rgba(0,0,0,.34);border:1px solid rgba(255,255,255,.08);border-radius:12px;
 padding:14px 16px;max-height:210px;overflow:auto;white-space:pre-wrap;word-break:break-word}
.hint{margin-top:22px;font-size:13px;color:#727d9d}
.foot{margin-top:28px;padding-top:20px;border-top:1px solid rgba(255,255,255,.07);
 display:flex;align-items:center;justify-content:center;gap:8px;font-size:12.5px;color:#727d9d}
.foot b{color:#aab4d4;font-weight:600}
.foot .dot{width:5px;height:5px;border-radius:50%;background:var(--accent);box-shadow:0 0 8px var(--accent)}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
@keyframes draw{to{stroke-dashoffset:0}}
@keyframes ping{0%{transform:scale(.66);opacity:.85}80%,100%{transform:scale(1.28);opacity:0}}
@keyframes drift{from{transform:translate3d(-2%,-1%,0) scale(1)}to{transform:translate3d(2%,1%,0) scale(1.06)}}
@media (prefers-reduced-motion:reduce){*{animation:none!important}.orb .draw{stroke-dashoffset:0}}
@media (max-width:520px){.card{padding:38px 24px 26px}h1{font-size:25px}}
</style></head>
<body><main class='card' role='status' aria-live='polite'>
<div class='orb'><i></i><i></i><i></i><b>@@ICON@@</b></div>
<div class='eyebrow'>@@EYEBROW@@</div>
<h1>@@HEADLINE@@</h1>
<p class='sub'>@@SUBCOPY@@</p>
@@EXTRA@@
<div class='foot'><span class='dot'></span><span>One AI Employee &middot; <b>oneemployee.ai</b></span></div>
</main></body></html>"""


def _oae_chips(labels: list[str]) -> str:
    cells = "".join(f"<span class='chip'>{html.escape(x)}</span>" for x in labels)
    return f"<div class='meta'>{cells}</div>"


def _oae_page(*, title: str, eyebrow: str, headline: str, subcopy: str, accent: str,
              icon: str, extra_html: str = "", status_code: int = 200) -> HTMLResponse:
    """Render a branded One AI Employee connection page (self-contained HTML)."""
    doc = (
        _OAE_PAGE_TMPL
        .replace("@@TITLE@@", html.escape(title))
        .replace("@@ACCENT@@", accent)
        .replace("@@EYEBROW@@", html.escape(eyebrow))
        .replace("@@HEADLINE@@", html.escape(headline))
        .replace("@@SUBCOPY@@", html.escape(subcopy))
        .replace("@@ICON@@", icon)
        .replace("@@EXTRA@@", extra_html)
    )
    return HTMLResponse(doc, status_code=status_code)


def _render_oauth_callback(code: str, state: str, error: str, app: str):
    """Shared OAuth callback for both Marketplace apps (agency + sub-account).

    `app` selects the credential pair for the code exchange and tags the stored
    grant so refresh later uses the same app. Routing to the right tenant
    (agency vs. location) is derived from the token's companyId/locationId.
    """
    ready_copy = ("This secure endpoint is online and waiting for GoHighLevel to complete the "
                  "handshake. Start the install from your HighLevel account to connect.")
    if error:
        return _oae_page(
            title="Not connected", accent="#F2765C", icon=_OAE_ICON_ALERT,
            eyebrow="Authorization stopped",
            headline="Authorization didn’t complete",
            subcopy=("GoHighLevel didn’t finish granting access, so nothing was connected. "
                     "You can close this window and start the connection again."),
            extra_html=f"<div class='detail'>{html.escape(str(error))}</div>",
            status_code=400,
        )
    if not code:
        return _oae_page(
            title="Endpoint ready", accent="#4C9BE8", icon=_OAE_ICON_SIGNAL,
            eyebrow="Secure endpoint · live",
            headline=("Sub-account endpoint ready" if app == "sub" else "Connection endpoint ready"),
            subcopy=ready_copy,
            extra_html=_oae_chips(["GoHighLevel OAuth", "TLS secured", "Awaiting handshake"]),
            status_code=200,
        )
    ok, token_or_error = _token_exchange(code, app=app)
    if not ok:
        # Never echo secrets or full token material.
        return _oae_page(
            title="Needs attention", accent="#E0A83D", icon=_OAE_ICON_ALERT,
            eyebrow="One step remaining",
            headline="Almost connected",
            subcopy=("GoHighLevel responded, but the connection couldn’t be finalized. "
                     "The details below can help your developer resolve it."),
            extra_html=f"<div class='detail'>{html.escape(json.dumps(token_or_error, indent=2))}</div>",
            status_code=502,
        )
    _save_token(token_or_error, app=app)
    if app == "sub":
        subcopy = ("This sub-account is now linked to One AI Employee. Your AI account manager can "
                   "work its contacts, conversations, calendars, and pipelines on command.")
        chips = ["Sub-account", "Location linked", "Encrypted & stored"]
    else:
        subcopy = ("Your GoHighLevel workspace is now linked to One AI Employee. Your AI operator can "
                   "start building funnels, running automations, and optimizing on command.")
        chips = ["GoHighLevel", "Workspace linked", "Encrypted & stored"]
    return _oae_page(
        title="Connected", accent="#6EE7B7", icon=_OAE_ICON_CHECK,
        eyebrow="Integration complete",
        headline="You’re connected",
        subcopy=subcopy,
        extra_html=(_oae_chips(chips)
                    + "<p class='hint'>You can safely close this window and return to HighLevel.</p>"),
        status_code=200,
    )


@router.get("/connect/oauth/callback", response_class=HTMLResponse)
@router.get("/ghl/oauth/callback", response_class=HTMLResponse)
def ghl_oauth_callback(code: str = "", state: str = "", error: str = ""):
    return _render_oauth_callback(code, state, error, app="agency")


@router.get("/connect/sub/oauth/callback", response_class=HTMLResponse)
@router.get("/ghl/sub/oauth/callback", response_class=HTMLResponse)
def ghl_sub_oauth_callback(code: str = "", state: str = "", error: str = ""):
    return _render_oauth_callback(code, state, error, app="sub")


@router.post("/connect/webhook")
@router.post("/connect/webhooks")
@router.post("/ghl/webhooks")
async def ghl_webhooks(request: Request):
    raw = await request.body()
    verified, reason = _verify_webhook_signature(raw, request.headers)
    if not verified:
        return _json_body({"ok": False, "error": reason}, status_code=401)
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        payload = {"raw": raw.decode("utf-8", "replace")}
    event_type = _store_event(payload)
    return {"ok": True, "received": True, "event_type": event_type, "signature": reason}


@router.post("/connect/webhooks/workflow-action")
@router.post("/ghl/webhooks/workflow-action")
async def ghl_workflow_action(request: Request):
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        payload = {"raw": raw.decode("utf-8", "replace")}
    payload.setdefault("type", "WorkflowAction")
    event_type = _store_event(payload)
    return {"status": "completed", "message": "One AI Employee received the workflow action.", "event_type": event_type, "data": {}}


@router.post("/connect/webhooks/trigger-subscription")
@router.post("/ghl/webhooks/trigger-subscription")
async def ghl_trigger_subscription(request: Request):
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        payload = {"raw": raw.decode("utf-8", "replace")}
    payload.setdefault("type", "WorkflowTriggerSubscription")
    event_type = _store_event(payload)
    return {"ok": True, "subscribed": True, "event_type": event_type}


_OAE_COCKPIT_HTML = """<!doctype html>
<html lang='en'><head><meta charset='utf-8'>
<title>Command surface · One AI Employee</title>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<meta name='robots' content='noindex'>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--accent:#6EE7B7;--blue:#4C9BE8;--purple:#a78bfa;--ink:#eaf0ff;--muted:#8b96b4;--bg:#060912;--panel:#0d1424;--panel2:#0b1120;--line:#1b2540}
html,body{min-height:100%}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--ink);
 -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;position:relative;overflow-x:hidden;padding:clamp(16px,3vw,34px)}
body::before{content:'';position:fixed;inset:-30%;z-index:0;pointer-events:none;
 background:radial-gradient(36% 40% at 18% 12%,color-mix(in srgb,var(--accent) 26%,transparent),transparent 70%),
  radial-gradient(38% 42% at 86% 20%,rgba(76,155,232,.18),transparent 72%),
  radial-gradient(50% 50% at 50% 116%,rgba(167,139,250,.16),transparent 70%);filter:blur(8px)}
.shell{position:relative;z-index:1;max-width:1080px;margin:0 auto;display:flex;flex-direction:column;gap:clamp(18px,2.4vw,26px)}
.top{display:flex;align-items:center;gap:14px}
.mark{width:38px;height:38px;border-radius:11px;display:grid;place-items:center;
 background:linear-gradient(150deg,rgba(110,231,183,.22),rgba(76,155,232,.14));border:1px solid var(--line)}
.mark svg{width:20px;height:20px}
.brand{font-weight:700;font-size:17px;letter-spacing:.2px;line-height:1.1}
.brand small{display:block;font-weight:500;font-size:11px;color:var(--muted);letter-spacing:.14em;text-transform:uppercase;margin-top:2px}
.pill{margin-left:auto;display:inline-flex;align-items:center;gap:8px;font-size:12.5px;font-weight:600;color:var(--accent);
 background:rgba(110,231,183,.10);border:1px solid rgba(110,231,183,.30);padding:7px 13px;border-radius:999px}
.pill i{width:8px;height:8px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 3px rgba(110,231,183,.16)}
.hero{margin-top:4px}
.eyebrow{font-size:12px;letter-spacing:.2em;text-transform:uppercase;color:var(--accent);font-weight:600;margin-bottom:10px}
.hero h1{font-size:clamp(26px,4vw,40px);line-height:1.05;letter-spacing:-.02em;font-weight:800}
.hero p{margin-top:12px;color:var(--muted);font-size:clamp(14px,1.5vw,16px);max-width:64ch;line-height:1.55}
.sec{background:linear-gradient(180deg,rgba(255,255,255,.02),transparent);border:1px solid var(--line);border-radius:18px;padding:clamp(16px,2vw,22px)}
.sec>h2{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);font-weight:600;margin-bottom:16px;display:flex;align-items:center;gap:10px}
.sec>h2::after{content:'';flex:1;height:1px;background:var(--line)}
.team{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}
.emp{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;display:flex;flex-direction:column;gap:10px}
.emp .r{display:flex;align-items:center;gap:12px}
.emp .av{width:40px;height:40px;border-radius:11px;display:grid;place-items:center;font-weight:800;font-size:15px;color:#04121a;flex:0 0 auto;
 background:linear-gradient(150deg,var(--accent),#48c9a0)}
.emp .av.b{background:linear-gradient(150deg,#7db8f0,var(--blue))}
.emp .av.p{background:linear-gradient(150deg,#c3aef7,var(--purple))}
.emp .av.o{background:linear-gradient(150deg,#f5b784,#e0894a)}
.emp .nm{font-weight:700;font-size:15px}
.emp .ro{font-size:12px;color:var(--accent);font-weight:600;margin-top:2px}
.emp .do{font-size:13px;color:var(--muted);line-height:1.5}
.emp .st{display:inline-flex;align-items:center;gap:7px;font-size:11.5px;color:var(--muted);margin-top:auto}
.emp .st i{width:7px;height:7px;border-radius:50%;background:var(--accent)}
.caps{display:flex;flex-wrap:wrap;gap:9px}
.cap{font-size:13px;color:var(--ink);background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:9px 13px;display:inline-flex;align-items:center;gap:9px}
.cap b{width:6px;height:6px;border-radius:2px;background:var(--accent)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:680px){.two{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}
.card h3{font-size:15px;font-weight:700;margin-bottom:9px;display:flex;align-items:center;gap:9px}
.card p{font-size:13.5px;color:var(--muted);line-height:1.55}
.card p b{color:var(--ink)}
.tag{font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;padding:3px 8px;border-radius:6px;margin-left:auto;
 color:var(--accent);background:rgba(110,231,183,.1);border:1px solid rgba(110,231,183,.28)}
.foot{text-align:center;color:var(--muted);font-size:12px;padding:4px 0 6px;opacity:.85}
</style></head>
<body><div class='shell'>
  <div class='top'>
    <div class='mark'><svg viewBox='0 0 24 24' fill='none' stroke='#6EE7B7' stroke-width='2' stroke-linecap='round'><path d='M4 12a8 8 0 0 1 8-8'/><path d='M4 12a8 8 0 0 0 8 8'/><circle cx='12' cy='12' r='2.4' fill='#6EE7B7' stroke='none'/></svg></div>
    <div class='brand'>One AI Employee<small>Command surface</small></div>
    <span class='pill'><i></i> Connected · online</span>
  </div>
  <div class='hero'>
    <div class='eyebrow'>Linked to this HighLevel account</div>
    <h1>Your AI team is on the clock.</h1>
    <p>Here is who is working inside this account and what they can operate &mdash; on command, with you approving anything that matters.</p>
  </div>
  <div class='sec'>
    <h2>Your team</h2>
    <div class='team'>
      <div class='emp'><div class='r'><div class='av'>A</div><div><div class='nm'>Adam</div><div class='ro'>Chief Operating Officer</div></div></div><div class='do'>Runs the operation, routes the work, and keeps every task on track.</div><div class='st'><i></i> On duty</div></div>
      <div class='emp'><div class='r'><div class='av b'>M</div><div><div class='nm'>Max</div><div class='ro'>HighLevel Solutions Architect</div></div></div><div class='do'>Builds and operates your GoHighLevel &mdash; funnels, automations, sub-accounts, SaaS.</div><div class='st'><i></i> On duty</div></div>
      <div class='emp'><div class='r'><div class='av p'>S</div><div><div class='nm'>Marketing Strategist</div><div class='ro'>Growth &amp; campaigns</div></div></div><div class='do'>Plans campaigns, audiences, and the growth engine behind the account.</div><div class='st'><i></i> On duty</div></div>
      <div class='emp'><div class='r'><div class='av o'>C</div><div><div class='nm'>Creative Director</div><div class='ro'>Copy &amp; content</div></div></div><div class='do'>Turns strategy into copy, creative, and content ready to ship.</div><div class='st'><i></i> On duty</div></div>
    </div>
  </div>
  <div class='sec'>
    <h2>What they operate</h2>
    <div class='caps'>
      <span class='cap'><b></b>Contacts &amp; CRM</span>
      <span class='cap'><b></b>Conversations</span>
      <span class='cap'><b></b>Calendars &amp; booking</span>
      <span class='cap'><b></b>Opportunities &amp; pipelines</span>
      <span class='cap'><b></b>Funnels &amp; sites</span>
      <span class='cap'><b></b>Automations</span>
      <span class='cap'><b></b>Social planner</span>
      <span class='cap'><b></b>Invoices &amp; products</span>
      <span class='cap'><b></b>Campaigns</span>
      <span class='cap'><b></b>Voice AI</span>
      <span class='cap'><b></b>Media &amp; blogs</span>
      <span class='cap'><b></b>Sub-accounts &amp; SaaS</span>
    </div>
  </div>
  <div class='two'>
    <div class='card'><h3>Your connections<span class='tag'>Your keys</span></h3><p>Your team runs on <b>your own</b> AI brain key and connects to your own tools &mdash; Slack, Google, Stripe, your website and more. Connected securely at setup; your keys never leave your account.</p></div>
    <div class='card'><h3>You stay in control<span class='tag'>Approvals</span></h3><p>Nothing important goes live without you. When an employee needs a sign-off it appears here for your approval &mdash; you stay the boss, they do the work.</p></div>
  </div>
  <div class='foot'>Encrypted &middot; your data stays in your account &middot; One AI Employee</div>
</div></body></html>"""


@router.get("/connect/dashboard", response_class=HTMLResponse)
@router.get("/ghl/dashboard", response_class=HTMLResponse)
def ghl_dashboard():
    return HTMLResponse(_OAE_PORTAL_HTML, status_code=200)


@router.get("/connect/custom.js")
@router.get("/ghl/custom.js")
def ghl_custom_js():
    js = """
(function(){
  window.OneAIEmployee = window.OneAIEmployee || {};
  window.OneAIEmployee.version = '2026.07.17';
  window.OneAIEmployee.hermesBase = 'https://os.oneemployee.ai/api/plugins/one-ai-employee/connect';
  window.OneAIEmployee.status = 'marketplace-custom-js-ready';
})();
""".strip() + "\n"
    return Response(content=js, media_type="application/javascript")



# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

_LIST_RE = re.compile(r"^(?:[-*+]|\d+\.)\s+")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _fix_tight_lists(text: str) -> str:
    """Insert a blank line before a list that directly follows a paragraph.

    The agents write "Strategy:" straight above "- item". Python-Markdown reads
    that as a lazy paragraph continuation and dissolves the bullets into prose,
    turning the approval checklist into a run-on sentence. Only indent-0 lists
    after an indent-0 paragraph line, never inside fenced code.
    """
    out: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence and _LIST_RE.match(line) and out:
            prev = out[-1]
            if (prev.strip()
                    and not prev.startswith((" ", "\t", "#", ">", "|"))
                    and not _LIST_RE.match(prev)):
                out.append("")
        out.append(line)
    return "\n".join(out)


# `markdown`'s "extra" passes raw HTML straight through, and this output is
# injected into the dashboard DOM. The text is written by Ahmed's own agents on
# his own machine, but a swarm goal can carry client-supplied wording, so treat
# the agents' output as untrusted and strip the tags/attributes that could
# execute. Not a general-purpose sanitiser — a targeted scrub of the vectors
# that matter for rendered markdown.
_SCRUB_TAGS_RE = re.compile(
    r"<\s*(script|iframe|object|embed|link|meta|base|form)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_SCRUB_VOID_RE = re.compile(
    r"<\s*(script|iframe|object|embed|link|meta|base|form)\b[^>]*/?>",
    re.IGNORECASE,
)
_SCRUB_ON_ATTR_RE = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_SCRUB_JS_URI_RE = re.compile(r"(href|src)\s*=\s*(\"|')?\s*javascript:[^\"'>\s]*", re.IGNORECASE)


def _scrub_html(html: str) -> str:
    html = _SCRUB_TAGS_RE.sub("", html)
    html = _SCRUB_VOID_RE.sub("", html)
    html = _SCRUB_ON_ATTR_RE.sub("", html)
    html = _SCRUB_JS_URI_RE.sub(r"\1='#'", html)
    return html


def _markdown_to_html(text: str) -> str:
    """Render markdown, degrading to escaped plain text if the lib is absent."""
    try:
        import markdown as md_lib
    except ImportError:
        import html as html_lib
        return "<pre>" + html_lib.escape(text) + "</pre>"
    return _scrub_html(md_lib.markdown(_fix_tight_lists(text), extensions=["extra"]))


# ── Per-client (location) GHL overview — read-only (added 2026-07-21) ──
# Aggregates a client's live GHL data (contacts, conversations, upcoming
# appointments) via the tenant's own location token. Every section fails
# soft so one unavailable API never breaks the whole overview.
def _oae_err(res: dict) -> str:
    data = res.get("data")
    if isinstance(data, dict):
        msg = data.get("message") or data.get("error") or data.get("msg")
        if msg:
            return str(msg)[:200]
    return str(data)[:200]


@router.get("/tenants/{tenant_id}/overview")
def tenant_overview(tenant_id: str):
    """Read-only at-a-glance data for a client (location) tenant."""
    _migrate_legacy_tokens()
    row = _tenant_row(tenant_id)
    if not row:
        raise HTTPException(status_code=404, detail="unknown tenant")
    location_id = str(row.get("location_id") or "")
    kind = row.get("kind")
    out = {"ok": True, "tenant_id": tenant_id, "kind": kind,
           "location_id": location_id, "sections": {}}
    if not location_id:
        out["note"] = "Overview is available for client (location) tenants."
        return out

    # Contacts
    try:
        r = _tenant_api(tenant_id, "GET", "/contacts/",
                        query={"locationId": location_id, "limit": 5})
        if r["ok"] and isinstance(r["data"], dict):
            d = r["data"]
            total = (d.get("meta") or {}).get("total")
            recent = []
            for c in (d.get("contacts") or [])[:5]:
                name = (c.get("contactName")
                        or " ".join(x for x in [c.get("firstName"), c.get("lastName")] if x)
                        or c.get("email") or c.get("phone") or "Unnamed")
                recent.append({"id": c.get("id") or c.get("_id"), "name": name,
                               "email": c.get("email"), "phone": c.get("phone")})
            out["sections"]["contacts"] = {"ok": True, "total": total, "recent": recent}
        else:
            out["sections"]["contacts"] = {"ok": False, "status": r.get("status"), "error": _oae_err(r)}
    except Exception as e:
        out["sections"]["contacts"] = {"ok": False, "error": str(e)[:200]}

    # Conversations
    try:
        r = _tenant_api(tenant_id, "GET", "/conversations/search",
                        query={"locationId": location_id, "limit": 5})
        if r["ok"] and isinstance(r["data"], dict):
            d = r["data"]
            total = d.get("total") or (d.get("meta") or {}).get("total")
            recent = []
            for c in (d.get("conversations") or [])[:5]:
                recent.append({"id": c.get("id"),
                               "contactName": c.get("fullName") or c.get("contactName") or c.get("name"),
                               "lastMessageBody": (c.get("lastMessageBody") or "")[:140],
                               "unreadCount": c.get("unreadCount")})
            out["sections"]["conversations"] = {"ok": True, "total": total, "recent": recent}
        else:
            out["sections"]["conversations"] = {"ok": False, "status": r.get("status"), "error": _oae_err(r)}
    except Exception as e:
        out["sections"]["conversations"] = {"ok": False, "error": str(e)[:200]}

    # Upcoming appointments (best-effort; LC events API may require a calendar)
    try:
        import time as _t
        start_ms = int(_t.time() * 1000)
        end_ms = start_ms + 30 * 24 * 3600 * 1000
        r = _tenant_api(tenant_id, "GET", "/calendars/events",
                        query={"locationId": location_id,
                               "startTime": start_ms, "endTime": end_ms})
        if r["ok"] and isinstance(r["data"], dict):
            events = r["data"].get("events") or []
            upcoming = [{"id": e.get("id"), "title": e.get("title"),
                         "startTime": e.get("startTime"),
                         "status": e.get("appointmentStatus") or e.get("status")}
                        for e in events[:5]]
            out["sections"]["appointments"] = {"ok": True, "total": len(events), "upcoming": upcoming}
        else:
            out["sections"]["appointments"] = {"ok": False, "status": r.get("status"),
                                                "error": _oae_err(r),
                                                "note": "GHL calendar events may require selecting a calendar."}
    except Exception as e:
        out["sections"]["appointments"] = {"ok": False, "error": str(e)[:200]}

    return out




# ── GHL sub-account app SSO + client portal (added 2026-07-22) ──────────────
# The sub-account Marketplace app ("Auto by One AI Employees") renders a custom
# page inside each client's HighLevel sub-account. GHL hands that page the
# viewer's context encrypted with THAT app's Shared Secret (SSO key). We decrypt
# it server-side, derive the locationId (never trusting a client-supplied id),
# and return ONLY that client's own scoped data. The secret is read from the
# environment (HERMES_HOME/.env) or a locked file on the bind mount — never
# committed and never returned to the browser.
_SSO_SECRET_FILE = Path("/opt/data/plugins/one-ai-employee/.sso_secret")


def _ghl_sub_shared_secret() -> str:
    val = _oae_env("GHL_SUB_SHARED_SECRET", "GHL_SUBACCOUNT_SHARED_SECRET",
                   "GHL_SUB_SSO_KEY", "GHL_SUBACCOUNT_SSO_KEY")
    if val:
        return val.strip()
    try:
        if _SSO_SECRET_FILE.exists():
            return _SSO_SECRET_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def _evp_bytes_to_key(passphrase: bytes, salt: bytes, key_len: int = 32, iv_len: int = 16):
    """OpenSSL EVP_BytesToKey (MD5) KDF — reproduces CryptoJS.AES default output
    so we can decrypt what GHL's front-end encrypts with a passphrase."""
    d = b""
    prev = b""
    while len(d) < key_len + iv_len:
        prev = hashlib.md5(prev + passphrase + salt).digest()
        d += prev
    return d[:key_len], d[key_len:key_len + iv_len]


def _decrypt_ghl_sso(sso_data: str) -> dict:
    """Decrypt a GHL user-context blob (CryptoJS/OpenSSL 'Salted__'
    AES-256-CBC). The Shared Secret is used as a passphrase (GHL SSO keys are
    UUID strings, not raw AES keys). Raises ValueError on any failure."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    secret = _ghl_sub_shared_secret()
    if not secret:
        raise ValueError("sso_secret_not_configured")
    if not sso_data or not isinstance(sso_data, str):
        raise ValueError("empty_sso_data")
    # When GHL delivers the blob via the ?ssoData={{ssoData}} URL merge field, a
    # '+' in the base64 can arrive decoded as a space — restore it before decode.
    sso_data = sso_data.strip().replace(" ", "+")
    raw = base64.b64decode(sso_data)
    if raw[:8] != b"Salted__":
        raise ValueError("unexpected_sso_format")
    salt = raw[8:16]
    ciphertext = raw[16:]
    key, iv = _evp_bytes_to_key(secret.encode("utf-8"), salt)
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = dec.update(ciphertext) + dec.finalize()
    pad = padded[-1] if padded else 0
    if pad < 1 or pad > 16:
        raise ValueError("bad_padding")
    plaintext = padded[:-pad].decode("utf-8")
    return json.loads(plaintext)


def _client_portal_payload(ctx: dict) -> dict:
    """Assemble a client's white-labeled portal data from a VERIFIED SSO ctx.

    The locationId comes only from the decrypted context, so a client can never
    request another client's data by editing a URL — the encryption is the auth.
    """
    location_id = str(ctx.get("activeLocation") or ctx.get("locationId") or "").strip()
    company_id = str(ctx.get("companyId") or "").strip()
    if not location_id:
        return {"ok": False, "error": "no_location_in_context"}
    _assign_tenant(company_id, location_id)
    tenant_id = f"location_{location_id}"
    name = ""
    try:
        r = _tenant_api(tenant_id, "GET", f"/locations/{location_id}")
        if r["ok"] and isinstance(r["data"], dict):
            loc = r["data"].get("location") or r["data"]
            if isinstance(loc, dict):
                name = loc.get("name") or loc.get("businessName") or ""
    except Exception:
        pass
    overview = tenant_overview(tenant_id)
    viewer_name = ctx.get("userName") or ctx.get("name") or ""
    if not viewer_name:
        viewer_name = " ".join(x for x in [ctx.get("firstName"), ctx.get("lastName")] if x)
    return {
        "ok": True,
        "location_id": location_id,
        "company_id": company_id,
        "business_name": name,
        "viewer": {"name": viewer_name or None,
                   "email": ctx.get("email"), "role": ctx.get("role") or ctx.get("type")},
        "sections": overview.get("sections", {}),
    }


class _SsoBody(BaseModel):
    ssoData: str = ""


@router.post("/connect/portal")
@router.post("/ghl/portal")
def ghl_client_portal(body: _SsoBody):
    """Return the signed-in client's own scoped data, gated by GHL SSO decrypt."""
    try:
        ctx = _decrypt_ghl_sso(body.ssoData)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=200)
    try:
        return JSONResponse(_client_portal_payload(ctx), status_code=200)
    except Exception as e:
        logging.exception("OAE: client portal payload failed")
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=200)


_OAE_PORTAL_HTML = r"""<!doctype html>
<html lang='en'><head><meta charset='utf-8'>
<title>Your AI team · One AI Employee</title>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<meta name='robots' content='noindex'>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--accent:#6EE7B7;--blue:#4C9BE8;--purple:#a78bfa;--ink:#eaf0ff;--muted:#8b96b4;--bg:#060912;--panel:#0d1424;--panel2:#0b1120;--line:#1b2540}
html,body{min-height:100%}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--ink);
 -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;position:relative;overflow-x:hidden;padding:clamp(16px,3vw,34px)}
body::before{content:'';position:fixed;inset:-30%;z-index:0;pointer-events:none;
 background:radial-gradient(36% 40% at 18% 12%,color-mix(in srgb,var(--accent) 26%,transparent),transparent 70%),
  radial-gradient(38% 42% at 86% 20%,rgba(76,155,232,.18),transparent 72%),
  radial-gradient(50% 50% at 50% 116%,rgba(167,139,250,.16),transparent 70%);filter:blur(8px)}
.shell{position:relative;z-index:1;max-width:1080px;margin:0 auto;display:flex;flex-direction:column;gap:clamp(18px,2.4vw,26px)}
.top{display:flex;align-items:center;gap:14px}
.mark{width:38px;height:38px;border-radius:11px;display:grid;place-items:center;
 background:linear-gradient(150deg,rgba(110,231,183,.22),rgba(76,155,232,.14));border:1px solid var(--line)}
.mark svg{width:20px;height:20px}
.brand{font-weight:700;font-size:17px;letter-spacing:.2px;line-height:1.1}
.brand small{display:block;font-weight:500;font-size:11px;color:var(--muted);letter-spacing:.14em;text-transform:uppercase;margin-top:2px}
.pill{margin-left:auto;display:inline-flex;align-items:center;gap:8px;font-size:12.5px;font-weight:600;color:var(--accent);
 background:rgba(110,231,183,.10);border:1px solid rgba(110,231,183,.30);padding:7px 13px;border-radius:999px}
.pill i{width:8px;height:8px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 3px rgba(110,231,183,.16)}
.pill.wait{color:var(--muted);background:rgba(139,150,180,.10);border-color:rgba(139,150,180,.28)}
.pill.wait i{background:var(--muted);box-shadow:0 0 0 3px rgba(139,150,180,.14)}
.hero{margin-top:4px}
.eyebrow{font-size:12px;letter-spacing:.2em;text-transform:uppercase;color:var(--accent);font-weight:600;margin-bottom:10px}
.hero h1{font-size:clamp(24px,4vw,38px);line-height:1.05;letter-spacing:-.02em;font-weight:800}
.hero p{margin-top:12px;color:var(--muted);font-size:clamp(14px,1.5vw,16px);max-width:64ch;line-height:1.55}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
.stat .k{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);font-weight:600}
.stat .v{font-size:30px;font-weight:800;letter-spacing:-.02em;margin-top:8px;font-variant-numeric:tabular-nums}
.stat .s{font-size:12.5px;color:var(--muted);margin-top:4px}
.sec{background:linear-gradient(180deg,rgba(255,255,255,.02),transparent);border:1px solid var(--line);border-radius:18px;padding:clamp(16px,2vw,22px)}
.sec>h2{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);font-weight:600;margin-bottom:16px;display:flex;align-items:center;gap:10px}
.sec>h2::after{content:'';flex:1;height:1px;background:var(--line)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:680px){.two{grid-template-columns:1fr}}
.list{display:flex;flex-direction:column;gap:2px}
.row{display:flex;align-items:center;gap:12px;padding:11px 4px;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}
.row .av{width:34px;height:34px;border-radius:9px;display:grid;place-items:center;font-weight:800;font-size:13px;color:#04121a;flex:0 0 auto;background:linear-gradient(150deg,var(--accent),#48c9a0)}
.row .nm{font-weight:600;font-size:14px}
.row .mt{font-size:12.5px;color:var(--muted);margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:34ch}
.empty{color:var(--muted);font-size:13px;padding:10px 4px}
.team{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}
.emp{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;display:flex;flex-direction:column;gap:10px}
.emp .r{display:flex;align-items:center;gap:12px}
.emp .av{width:40px;height:40px;border-radius:11px;display:grid;place-items:center;font-weight:800;font-size:15px;color:#04121a;flex:0 0 auto;background:linear-gradient(150deg,var(--accent),#48c9a0)}
.emp .av.b{background:linear-gradient(150deg,#7db8f0,var(--blue))}
.emp .av.p{background:linear-gradient(150deg,#c3aef7,var(--purple))}
.emp .av.o{background:linear-gradient(150deg,#f5b784,#e0894a)}
.emp .nm{font-weight:700;font-size:15px}
.emp .ro{font-size:12px;color:var(--accent);font-weight:600;margin-top:2px}
.emp .do{font-size:13px;color:var(--muted);line-height:1.5}
.emp .st{display:inline-flex;align-items:center;gap:7px;font-size:11.5px;color:var(--muted);margin-top:auto}
.emp .st i{width:7px;height:7px;border-radius:50%;background:var(--accent)}
.foot{text-align:center;color:var(--muted);font-size:12px;padding:4px 0 6px;opacity:.85}
.skel{color:var(--muted)}
[hidden]{display:none!important}
</style></head>
<body><div class='shell'>
  <div class='top'>
    <div class='mark'><svg viewBox='0 0 24 24' fill='none' stroke='#6EE7B7' stroke-width='2' stroke-linecap='round'><path d='M4 12a8 8 0 0 1 8-8'/><path d='M4 12a8 8 0 0 0 8 8'/><circle cx='12' cy='12' r='2.4' fill='#6EE7B7' stroke='none'/></svg></div>
    <div class='brand'>One AI Employee<small>Your workspace</small></div>
    <span class='pill wait' id='pill'><i></i> <span id='pilltxt'>Connecting…</span></span>
  </div>
  <div class='hero'>
    <div class='eyebrow' id='eyebrow'>Your account</div>
    <h1 id='headline'>Your AI team is on the clock.</h1>
    <p id='sub'>Here is what your team is seeing and working on inside this account — with you approving anything that matters.</p>
  </div>

  <div class='stats' id='stats' hidden>
    <div class='stat'><div class='k'>Contacts</div><div class='v' id='st-contacts'>—</div><div class='s'>in your CRM</div></div>
    <div class='stat'><div class='k'>Conversations</div><div class='v' id='st-convos'>—</div><div class='s'>across channels</div></div>
    <div class='stat'><div class='k'>Upcoming</div><div class='v' id='st-appts'>—</div><div class='s'>appointments (30 days)</div></div>
  </div>

  <div class='two' id='recents' hidden>
    <div class='sec'><h2>Recent contacts</h2><div class='list' id='list-contacts'></div></div>
    <div class='sec'><h2>Recent conversations</h2><div class='list' id='list-convos'></div></div>
  </div>

  <div class='sec'>
    <h2>Your team</h2>
    <div class='team'>
      <div class='emp'><div class='r'><div class='av'>A</div><div><div class='nm'>Adam</div><div class='ro'>Chief Operating Officer</div></div></div><div class='do'>Runs the operation, routes the work, and keeps every task on track.</div><div class='st'><i></i> On duty</div></div>
      <div class='emp'><div class='r'><div class='av b'>M</div><div><div class='nm'>Max</div><div class='ro'>HighLevel Solutions Architect</div></div></div><div class='do'>Builds and operates your GoHighLevel — funnels, automations, sub-accounts, SaaS.</div><div class='st'><i></i> On duty</div></div>
      <div class='emp'><div class='r'><div class='av p'>S</div><div><div class='nm'>Marketing Strategist</div><div class='ro'>Growth &amp; campaigns</div></div></div><div class='do'>Plans campaigns, audiences, and the growth engine behind the account.</div><div class='st'><i></i> On duty</div></div>
      <div class='emp'><div class='r'><div class='av o'>C</div><div><div class='nm'>Creative Director</div><div class='ro'>Copy &amp; content</div></div></div><div class='do'>Turns strategy into copy, creative, and content ready to ship.</div><div class='st'><i></i> On duty</div></div>
    </div>
  </div>

  <div class='foot'>Encrypted &middot; your data stays in your account &middot; One AI Employee</div>
</div>
<script>
(function(){
  var $=function(id){return document.getElementById(id)};
  function esc(s){s=(s==null?'':String(s));return s.replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
  function initials(n){n=(n||'').trim();if(!n)return '?';var p=n.split(/\s+/);return ((p[0]||'')[0]||'')+((p[1]||'')[0]||'')||n[0]}
  function setPill(txt,ok){var p=$('pill');$('pilltxt').textContent=txt;if(ok){p.classList.remove('wait')}else{p.classList.add('wait')}}

  function renderList(el,items,kind){
    if(!items||!items.length){el.innerHTML="<div class='empty'>Nothing yet — your team will start filling this in.</div>";return}
    el.innerHTML=items.map(function(it){
      var title=kind==='contact'?(it.name||'Unnamed'):(it.contactName||'Conversation');
      var meta=kind==='contact'?(it.email||it.phone||''):(it.lastMessageBody||'');
      return "<div class='row'><div class='av'>"+esc(initials(title))+"</div><div><div class='nm'>"+esc(title)+"</div><div class='mt'>"+esc(meta)+"</div></div></div>";
    }).join('');
  }

  function render(data){
    var name=data.business_name||'';
    if(name){$('eyebrow').textContent='Your account';$('headline').textContent=name+" — your AI team is on the clock.";}
    var s=data.sections||{};
    var c=s.contacts||{},cv=s.conversations||{},ap=s.appointments||{};
    if(c.ok&&c.total!=null)$('st-contacts').textContent=c.total;
    if(cv.ok&&cv.total!=null)$('st-convos').textContent=cv.total;
    if(ap.ok&&ap.total!=null)$('st-appts').textContent=ap.total;
    $('stats').hidden=false;
    renderList($('list-contacts'),c.recent,'contact');
    renderList($('list-convos'),cv.recent,'convo');
    $('recents').hidden=false;
    setPill('Connected · online',true);
  }

  function fallback(reason){
    // Opened outside HighLevel, or context unavailable — show the reassuring
    // team view without any per-client data.
    setPill('Ready',false);
    $('sub').textContent='Open this from inside your HighLevel account to see your live numbers. Your team is on duty either way.';
  }

  function callPortal(ssoData){
    var url=location.pathname.replace(/dashboard$/,'portal');
    fetch(url,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({ssoData:ssoData})})
      .then(function(r){return r.json()})
      .then(function(d){ if(d&&d.ok){render(d)}else{fallback((d&&d.error)||'no_data')} })
      .catch(function(){fallback('network')});
  }

  // Delivery 1: GHL Custom Pages substitute the encrypted blob into the iframe
  // URL via the {{ssoData}} merge field. Prefer it — no round-trip needed.
  try{
    var q=new URLSearchParams(location.search).get('ssoData');
    if(q){callPortal(q);return}
  }catch(e){}

  // Delivery 2: postMessage handshake — ask the parent frame for the context.
  var settled=false;
  function onMsg(e){
    var d=e&&e.data;
    if(d&&d.message==='REQUEST_USER_DATA_RESPONSE'){
      settled=true;window.removeEventListener('message',onMsg);
      if(d.payload){callPortal(d.payload)}else{fallback('empty_payload')}
    }
  }
  window.addEventListener('message',onMsg);
  try{window.parent.postMessage({message:'REQUEST_USER_DATA'},'*')}catch(err){}
  setTimeout(function(){ if(!settled){window.removeEventListener('message',onMsg);fallback('timeout')} },4000);
})();
</script>
</body></html>"""
