import os
import json
import re
import hashlib
import queue
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

from services.google_service import (
    create_gmail_draft,
    send_gmail_draft,
    update_gmail_draft,
    get_gmail_threads,
    get_gmail_thread,
    get_gmail_message,
    get_gmail_draft,
    delete_gmail_draft
)
from services.auto_send_policy import AutoSendPolicy
from services.privacy_gate import PrivacyGate
from services.agent import AgentSession, AgentLoop, PolicyEngine, ToolRegistry, Verifier
from services.agent.models.lmstudio import LMStudioModel, ModelTimeout, ModelUnavailable
from services.work_state_store import work_state_store
from services.secure_storage import DataEncryptionError, read_encrypted_json, write_encrypted_json

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
WORKSPACES_DIR = BASE_DIR / "workspaces"
JOBS_FILE = DATA_DIR / "work_jobs.json"
SETTINGS_FILE = DATA_DIR / "work_settings.json"

DEFAULT_SETTINGS = {
    "auto_prep": True,
    "create_gmail_drafts": True,
    "auto_send_mode": "safe_replies",  # "off", "prepare", "safe_replies", "full_prepare"
    "countdown_seconds": 20,
    "trusted_senders": []
}

def _now():
    return datetime.now(timezone.utc).isoformat()

def _clean(val, limit=2000):
    return re.sub(r"\s+", " ", str(val or "")).strip()[:limit]


def _substantive_reply(body):
    content = re.sub(r'(?im)^\s*(hi|hello|dear)\b[^\n]*[,!]?\s*$', '', str(body or ''))
    content = re.sub(r'(?ims)\b(best regards|best|regards|sincerely|thanks)[,\s]*\n?\s*priyam\s*$', '', content)
    return len(re.sub(r'\W+', '', content)) >= 24


def _fallback_reply(sender, subject, deadline=''):
    name = str(sender or '').split('<')[0].strip() or 'there'
    timing = f' I have noted the deadline as {deadline}.' if deadline else ''
    return (
        f"Hi {name},\n\nThank you for the reminder about {subject or 'this request'}."
        f"{timing} I am reviewing the requirements and will complete the requested action on time.\n\n"
        "Best regards,\nPriyam"
    )


def _strict_newer_outbound(messages, source_msg_id):
    # Return (source_message, newer_outbound, reason).
    # Never scan the whole thread when the exact source cannot be identified.
    source_msg_id = str(source_msg_id or "").strip()
    if not source_msg_id:
        return None, None, "missing_source_id"

    source_index = -1
    source_message = None
    for idx, message in enumerate(messages or []):
        current_id = str(message.get("id") or message.get("gmail_id") or "").strip()
        if current_id == source_msg_id:
            source_index = idx
            source_message = message
            break

    if source_index < 0 or source_message is None:
        return None, None, "source_not_found"

    if source_message.get("direction") == "outbound" or source_message.get("is_sent_by_me"):
        return source_message, None, "source_outbound"

    for message in (messages or [])[source_index + 1:]:
        current_id = str(message.get("id") or message.get("gmail_id") or "").strip()
        if not current_id or current_id == source_msg_id:
            continue
        if message.get("direction") == "outbound" or message.get("is_sent_by_me"):
            return source_message, message, "ok"

    return source_message, None, "ok"


def _remove_draft_artifacts(job):
    job["artifacts"] = [
        artifact for artifact in (job.get("artifacts") or [])
        if artifact.get("type") != "email_draft"
    ]


def _append_step_once(job, step_type, label, status="done", **extra):
    steps = job.setdefault("steps", [])
    if steps and steps[-1].get("type") == step_type and steps[-1].get("label") == label:
        return
    record = {
        "type": step_type,
        "label": label,
        "status": status,
        "at": _now(),
    }
    record.update(extra)
    steps.append(record)

def clean_job_title(title: str, subject: str = "") -> str:
    """Transform messy or classified titles into clean, concise human-readable titles."""
    raw = title or subject or "Work Task"
    # Remove synthetic prefixes like "Assignment: ", "Task: ", "Re: ", "Fwd: "
    cleaned = re.sub(r"^(?:assignment|task|follow-?up|re|fwd|todo)[\s:]+", "", raw, flags=re.I).strip()
    if not cleaned or cleaned.lower() == "no subject":
        cleaned = subject if (subject and subject.lower() != "no subject") else "DA Submission"
    cleaned = re.sub(r"^(?:assignment|task|follow-?up|re|fwd|todo)[\s:]+", "", cleaned, flags=re.I).strip()
    if not cleaned:
        cleaned = "DA Submission"
    
    words = cleaned.split()
    fixed_words = []
    acronyms = {"da", "os", "ai", "stt", "tts", "api", "db", "ui", "ux", "ml", "llm", "cse", "ece", "vit"}
    for w in words:
        low = re.sub(r"[^\w]", "", w.lower())
        if low in acronyms:
            # Preserve punctuation around acronym
            fixed_words.append(re.sub(r"[a-zA-Z]+", low.upper(), w))
        elif w.isupper() and len(w) > 1:
            fixed_words.append(w)
        else:
            fixed_words.append(w.capitalize())
    return " ".join(fixed_words)


class WorkAgentService:
    def __init__(self):
        self.base_url = os.getenv("LM_STUDIO_BASE_URL", "http://127.0.0.1:2806/v1").rstrip("/")
        self.model = os.getenv("LM_STUDIO_MODEL", "")
        self.timeout = int(os.getenv("LM_STUDIO_TIMEOUT_SECONDS", "30"))
        self._lock = threading.RLock()
        self._queue = queue.Queue()
        self._running_worker = None
        self._active_jobs = set()
        self._start_queue_worker()

    def _start_queue_worker(self):
        def worker_loop():
            while True:
                job_id, user_id = self._queue.get()
                try:
                    self._execute_job(job_id, user_id)
                except Exception as e:
                    print(f"[WorkAgent] Worker error for {job_id}: {e}")
                finally:
                    self._queue.task_done()
                    with self._lock:
                        self._active_jobs.discard(job_id)

        t = threading.Thread(target=worker_loop, daemon=True, name="WorkAgent-Worker")
        t.start()
        self._running_worker = t

    def get_settings(self) -> dict:
        try:
            data = read_encrypted_json(SETTINGS_FILE, 'work_settings', default={})
            merged = dict(DEFAULT_SETTINGS)
            merged.update(data if isinstance(data, dict) else {})
            return merged
        except DataEncryptionError:
            raise
        except Exception:
            return dict(DEFAULT_SETTINGS)

    def update_settings(self, new_settings: dict) -> dict:
        current = self.get_settings()
        for k, v in (new_settings or {}).items():
            if k in DEFAULT_SETTINGS:
                current[k] = v
        write_encrypted_json(SETTINGS_FILE, current, 'work_settings')
        return current

    def _read_jobs(self):
        data = work_state_store.read_all()
        settings = self.get_settings()
        for j in data.values():
            if "clean_title" not in j:
                j["clean_title"] = clean_job_title(j.get("title", ""), (j.get("source") or {}).get("subject", ""))
            if "policy_verdict" not in j and j.get("output", {}).get("suggested_reply"):
                j["policy_verdict"] = AutoSendPolicy.evaluate(
                    j["output"]["suggested_reply"], artifacts=j.get("artifacts", []),
                    source=j.get("source", {}), settings=settings
                )
        return data

    def _write_jobs(self, jobs):
        work_state_store.write_all(jobs)

    def _job_id(self, user_id, message_id):
        digest = hashlib.sha256(f"{user_id}:{message_id}".encode("utf-8")).hexdigest()[:12]
        return f"work_{digest}"

    def reconcile_jobs_with_gmail(self, user_id=None):
        """
        Inspect unresolved WorkJobs tied to Gmail threads.
        If the authenticated user sent a newer outbound message on the thread,
        transition the job to 'resolved_external', cancel any auto-send countdown,
        and safely delete Mailmate's own stale draft if one exists.
        """
        reconciled = []
        with self._lock:
            jobs = self._read_jobs()
            unresolved_statuses = {
                "queued", "reading_context", "planning", "researching",
                "generating", "drafting_reply", "creating_files",
                "verifying", "preparing", "working", "waiting_approval",
                "auto_send_countdown", "needs_input",
                "resolved_external"
            }
            changed = False
            for jid, job in list(jobs.items()):
                if user_id and job.get("user_id") != user_id:
                    continue
                if job.get("status") not in unresolved_statuses:
                    continue

                source = job.get("source") or {}
                thread_id = source.get("thread_id")
                source_msg_id = source.get("message_id")
                if not thread_id:
                    continue

                try:
                    thread_data = get_gmail_thread(thread_id)
                except Exception as e:
                    print(f"[WorkAgent] Thread check failed ({thread_id}): {e}")
                    continue

                if not thread_data or not thread_data.get("messages"):
                    continue

                messages = thread_data["messages"]
                source_message, outbound, match_reason = _strict_newer_outbound(
                    messages,
                    source_msg_id
                )

                if match_reason == "source_outbound":
                    job["status"] = "ignored_outbound"
                    job["resolution"] = "source_message_was_outbound"
                    job["updated_at"] = _now()
                    if "countdown" in job:
                        job["countdown"]["cancelled"] = True

                    draft_id = (job.get("output") or {}).get("gmail_draft_id")
                    if draft_id:
                        try:
                            delete_gmail_draft(draft_id)
                        except Exception as d_err:
                            print(f"[WorkAgent] Outbound draft cleanup notice: {d_err}")
                    job.setdefault("output", {})["gmail_draft_id"] = None
                    _remove_draft_artifacts(job)
                    _append_step_once(
                        job,
                        "ignored_outbound",
                        "Ignored sent mail — Work only acts on inbound requests"
                    )
                    changed = True
                    continue

                if match_reason != "ok":
                    if job.get("status") == "resolved_external":
                        job["status"] = "waiting_approval"
                        job["updated_at"] = _now()
                        job.pop("resolved_at", None)
                        job.pop("resolved_message_id", None)
                        job.pop("resolution", None)
                        job.setdefault("output", {})["gmail_draft_id"] = None
                        _remove_draft_artifacts(job)
                        _append_step_once(
                            job,
                            "reconciliation_corrected",
                            "Previous Gmail resolution could not be verified — returned to review"
                        )
                        changed = True
                    continue

                if outbound:
                    print(
                        f"[WorkAgent] Verified newer outbound Gmail reply on thread "
                        f"{thread_id} (msg {outbound.get('id')}). Resolving job {jid}."
                    )
                    job["status"] = "resolved_external"
                    job["resolved_at"] = outbound.get("date") or outbound.get("timestamp") or _now()
                    job["resolved_message_id"] = outbound.get("id")
                    job["resolution"] = "manual_gmail_reply"
                    job["updated_at"] = _now()
                    changed = True

                    if "countdown" in job:
                        job["countdown"]["cancelled"] = True

                    draft_id = (job.get("output") or {}).get("gmail_draft_id")
                    if draft_id:
                        try:
                            existing_draft = get_gmail_draft(draft_id)
                            if existing_draft:
                                delete_gmail_draft(draft_id)
                                job.setdefault("output", {})["draft_stale"] = True
                        except Exception as d_err:
                            print(f"[WorkAgent] Draft cleanup notice: {d_err}")

                    job.setdefault("output", {})["gmail_draft_id"] = None
                    _remove_draft_artifacts(job)
                    _append_step_once(
                        job,
                        "resolved_external",
                        "Verified newer reply sent manually in Gmail",
                        resolved_message_id=outbound.get("id")
                    )
                    reconciled.append(job)

                elif job.get("status") == "resolved_external":
                    job["status"] = "waiting_approval"
                    job["updated_at"] = _now()
                    job.pop("resolved_at", None)
                    job.pop("resolved_message_id", None)
                    job.pop("resolution", None)
                    job.setdefault("output", {})["gmail_draft_id"] = None
                    _remove_draft_artifacts(job)
                    _append_step_once(
                        job,
                        "reconciliation_corrected",
                        "No newer Gmail reply found — draft returned to review"
                    )
                    changed = True

            if changed:
                self._write_jobs(jobs)

        return reconciled

    def list_jobs(self, user_id, reconcile=True):
        work_state_store.hydrate(user_id)
        if reconcile and user_id:
            try:
                self.reconcile_jobs_with_gmail(user_id)
            except Exception as e:
                print(f"[WorkAgent] Reconciliation notice in list_jobs: {e}")

        with self._lock:
            jobs = self._read_jobs()
            user_jobs = [j for j in jobs.values() if j.get("user_id") == user_id]
            user_jobs.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
            return user_jobs

    def get_active_jobs(self, user_id):
        active_statuses = {
            "queued", "reading_context", "planning", "researching",
            "generating", "drafting_reply", "creating_files",
            "verifying", "preparing", "working"
        }
        return [j for j in self.list_jobs(user_id, reconcile=False) if j.get("status") in active_statuses]

    def get_waiting_approval_jobs(self, user_id):
        return [j for j in self.list_jobs(user_id, reconcile=False) if j.get("status") in {"waiting_approval", "auto_send_countdown"}]

    def get_needs_input_jobs(self, user_id):
        return [j for j in self.list_jobs(user_id, reconcile=False) if j.get("status") == "needs_input"]

    def get_completed_jobs(self, user_id):
        completed_statuses = {"sent", "approved_sent", "resolved_external", "ignored_outbound", "completed", "cancelled", "failed"}
        return [j for j in self.list_jobs(user_id, reconcile=False) if j.get("status") in completed_statuses]

    def resolved_source_message_ids(self, user_id):
        """Return Gmail source IDs whose requested reply/action is verifiably resolved."""
        resolved_statuses = {"sent", "approved_sent", "resolved_external", "ignored_outbound"}
        return {
            str((job.get("source") or {}).get("message_id") or "")
            for job in self.list_jobs(user_id, reconcile=False)
            if job.get("status") in resolved_statuses
            and (job.get("source") or {}).get("message_id")
        }

    def create_automation_run(self, user_id, automation):
        automation_id = str((automation or {}).get("id") or "automation")
        stamp = _now()
        digest = hashlib.sha256(f"{automation_id}:{stamp}".encode("utf-8")).hexdigest()[:12]
        job_id = f"automation_{digest}"
        job = {
            "id": job_id,
            "type": "automation_run",
            "automation_id": automation_id,
            "user_id": user_id,
            "title": str((automation or {}).get("name") or "Scheduled Kyle run"),
            "clean_title": str((automation or {}).get("name") or "Scheduled Kyle run"),
            "status": "working",
            "created_at": stamp,
            "updated_at": stamp,
            "source": {
                "kind": "automation",
                "automation_id": automation_id,
                "schedule": (automation or {}).get("schedule") or {},
            },
            "goal": ((automation or {}).get("action") or {}).get("goal") or "Check the workspace and prepare a summary.",
            "steps": [{"type": "automation_start", "label": "Started scheduled Kyle run", "status": "done", "at": stamp}],
            "artifacts": [],
            "output": {},
        }
        with self._lock:
            jobs = self._read_jobs()
            jobs[job_id] = job
            self._write_jobs(jobs)
        return dict(job)

    def add_automation_run_step(self, job_id, user_id, label, status="done", detail=None):
        with self._lock:
            jobs = self._read_jobs()
            job = jobs.get(str(job_id))
            if not job or job.get("user_id") != user_id or job.get("type") != "automation_run":
                return None
            step = {"type": "automation_action", "label": _clean(label, 240), "status": status, "at": _now()}
            if detail:
                step["detail"] = _clean(detail, 500)
            job.setdefault("steps", []).append(step)
            job["updated_at"] = _now()
            self._write_jobs(jobs)
            return dict(job)

    def finish_automation_run(self, job_id, user_id, summary, checklist=None, error=None):
        with self._lock:
            jobs = self._read_jobs()
            job = jobs.get(str(job_id))
            if not job or job.get("user_id") != user_id or job.get("type") != "automation_run":
                return None
            job["status"] = "failed" if error else "completed"
            job["updated_at"] = _now()
            job["output"] = {
                "summary": _clean(summary, 2000),
                "checklist": [_clean(item, 240) for item in (checklist or [])[:12]],
            }
            if error:
                job["output"]["error"] = _clean(error, 800)
            _append_step_once(
                job,
                "automation_complete" if not error else "automation_failed",
                "Work summary ready" if not error else "Scheduled run could not finish",
                status="done" if not error else "error",
            )
            self._write_jobs(jobs)
            return dict(job)

    def get_job(self, job_id, user_id):
        work_state_store.hydrate(user_id)
        with self._lock:
            jobs = self._read_jobs()
            job = jobs.get(str(job_id))
            if job and job.get("user_id") == user_id:
                return job
            return None

    def _ensure_source_metadata(self, job_id, user_id, job=None):
        """Restore action metadata from Gmail for jobs hydrated on another device."""
        current = job or self.get_job(job_id, user_id)
        source = (current or {}).get("source") or {}
        if source.get("sender") and source.get("subject") and source.get("thread_id"):
            return current

        message_id = source.get("message_id")
        if not message_id:
            return current

        try:
            message = get_gmail_message(message_id)
        except Exception as exc:
            print(f"[WorkAgent] Gmail metadata refresh notice ({message_id}): {exc}")
            return current

        labels = [str(label) for label in (message.get("labels") or [])]
        metadata = {
            "thread_id": message.get("thread_id"),
            "rfc_message_id": message.get("rfc_message_id"),
            "sender": message.get("sender"),
            "subject": message.get("subject"),
            "direction": "outbound" if "SENT" in {label.upper() for label in labels} else "inbound",
            "labels": labels,
        }
        with self._lock:
            jobs = self._read_jobs()
            target = jobs.get(str(job_id))
            if not target or target.get("user_id") != user_id:
                return current
            target_source = target.setdefault("source", {})
            for key, value in metadata.items():
                if value:
                    target_source[key] = value
            target["updated_at"] = _now()
            self._write_jobs(jobs)
            return dict(target)

    def sync_and_enqueue(self, user_id, overview_data):
        """Analyze overview tasks, create WorkJob records, and enqueue actionable jobs."""
        if not user_id or not overview_data:
            return []

        settings = self.get_settings()
        if not settings.get("auto_prep", True):
            return []

        needs_attention = overview_data.get("needs_attention") or []
        emails = overview_data.get("emails") or []
        # The dashboard can emit attention references as source_message_id, message_id,
        # or email_id depending on which analysis/cache path produced the item. Build
        # one canonical lookup so Work does not silently drop otherwise Work-ready mail.
        email_map = {}
        for email in emails:
            for candidate_id in (email.get("id"), email.get("gmail_id"), email.get("message_id")):
                if candidate_id:
                    email_map[str(candidate_id)] = email

        new_jobs = []
        with self._lock:
            jobs = self._read_jobs()
            for item in needs_attention:
                msg_id = str(
                    item.get("source_message_id")
                    or item.get("message_id")
                    or item.get("email_id")
                    or ""
                )
                if not msg_id:
                    continue
                email = email_map.get(msg_id)
                if not email:
                    continue

                direction = str(email.get("direction") or "").strip().lower()
                labels = {str(label).upper() for label in (email.get("labels") or [])}
                if direction == "outbound" or "SENT" in labels:
                    continue

                thread_id = email.get("thread_id") or msg_id
                subject = email.get("subject") or item.get("description") or "Action Item"
                sender = email.get("sender") or email.get("from") or "Unknown sender"
                snippet = email.get("snippet") or item.get("description") or ""

                # Privacy Gate: Verify that item is safe and actionable for the Work Plane
                gate = PrivacyGate.evaluate(email if email else {"subject": subject, "snippet": snippet, "sender": sender})
                if gate.get("work_agent_allowed") is not True:
                    continue
                context_scores = email.get('context_scores') or {}
                if context_scores and context_scores.get('work_allowed') is not True:
                    continue

                job_id = self._job_id(user_id, msg_id)

                if job_id not in jobs:
                    is_academic = bool(re.search(r"\b(DA|assignment|submission|exam|quiz|synopsis|project)\b", f"{subject} {snippet}", re.I))
                    display_title = clean_job_title(subject, subject)

                    job = {
                        "id": job_id,
                        "user_id": user_id,
                        "title": display_title,
                        "clean_title": display_title,
                        "kind": "assignment-prep" if is_academic else "email-reply",
                        "status": "queued",
                        "current_step": "Queued for local AI",
                        "activity_label": "Queued for local AI",
                        "source": {
                            "type": "gmail",
                            "message_id": msg_id,
                            "rfc_message_id": email.get("rfc_message_id") or "",
                            "thread_id": thread_id,
                            "sender": sender,
                            "subject": subject,
                            "snippet": snippet,
                            "deadline": item.get("deadline") or "",
                            "direction": direction,
                            "labels": sorted(labels)
                        },
                        "steps": [
                            {"type": "read_thread", "label": f"Read email from {sender.split('<')[0].strip()}", "status": "done"},
                            {"type": "extract_requirements", "label": "Identified action & deadline", "status": "done"},
                            {"type": "prepare_reply", "label": "Prepare draft reply", "status": "queued"},
                            {"type": "create_files", "label": "Generate checklist / artifacts", "status": "queued"},
                            {"type": "human_approval", "label": "Waiting for review", "status": "pending"}
                        ],
                        "output": {
                            "summary": item.get("description") or snippet,
                            "checklist": [],
                            "suggested_reply": "",
                            "gmail_draft_id": None,
                            "notes": []
                        },
                        "policy_verdict": None,
                        "artifacts": [],
                        "created_at": _now(),
                        "updated_at": _now()
                    }
                    jobs[job_id] = job
                    new_jobs.append(job_id)
                    self._queue.put((job_id, user_id))

            if new_jobs:
                self._write_jobs(jobs)

        return new_jobs

    def run_job(self, job_id, user_id):
        job = self.get_job(job_id, user_id)
        if not job:
            return None
        with self._lock:
            jobs = self._read_jobs()
            if job_id in jobs:
                jobs[job_id]["status"] = "queued"
                jobs[job_id]["updated_at"] = _now()
                self._write_jobs(jobs)
                self._queue.put((job_id, user_id))
                return jobs[job_id]
        return None

    def cancel_job(self, job_id, user_id):
        with self._lock:
            jobs = self._read_jobs()
            if job_id in jobs and jobs[job_id].get("user_id") == user_id:
                jobs[job_id]["status"] = "cancelled"
                jobs[job_id]["updated_at"] = _now()
                if "countdown" in jobs[job_id]:
                    jobs[job_id]["countdown"]["cancelled"] = True
                self._write_jobs(jobs)
                return jobs[job_id]
        return None

    def cancel_countdown(self, job_id, user_id):
        """Immediately halts the 20-second auto-send timer and holds for human approval."""
        with self._lock:
            jobs = self._read_jobs()
            job = jobs.get(str(job_id))
            if not job or job.get("user_id") != user_id:
                return None
            if job.get("status") == "auto_send_countdown":
                job["status"] = "waiting_approval"
                job["updated_at"] = _now()
                if "countdown" in job:
                    job["countdown"]["cancelled"] = True
                for step in job.get("steps", []):
                    if step.get("type") == "countdown":
                        step["status"] = "cancelled"
                        step["label"] = "Auto-send cancelled by user"
                job["steps"].append({
                    "type": "human_approval",
                    "label": "Waiting for manual human approval",
                    "status": "pending",
                    "at": _now()
                })
                self._write_jobs(jobs)
                return job
            return job

    def save_draft(self, job_id, user_id, edited_reply):
        """Save manual edits to the draft without sending. Aborts any active auto-send countdown."""
        self._ensure_source_metadata(job_id, user_id)
        with self._lock:
            jobs = self._read_jobs()
            job = jobs.get(str(job_id))
            if not job or job.get("user_id") != user_id:
                return None

            # If job was in auto-send countdown, an edit halts auto-send
            if job.get("status") == "auto_send_countdown":
                job["status"] = "waiting_approval"
                if "countdown" in job:
                    job["countdown"]["cancelled"] = True

            job["updated_at"] = _now()
            if "reply_draft" not in job:
                job["reply_draft"] = {}
            job["reply_draft"]["body"] = edited_reply
            if "output" not in job:
                job["output"] = {}
            job["output"]["suggested_reply"] = edited_reply

            # Re-evaluate policy verdict for the edited text
            settings = self.get_settings()
            job["policy_verdict"] = AutoSendPolicy.evaluate(
                edited_reply,
                artifacts=job.get("artifacts", []),
                source=job.get("source", {}),
                settings=settings
            )

            # Update real Gmail draft if one exists
            draft_id = job.get("output", {}).get("gmail_draft_id")
            source = job.get("source") or {}
            sender = source.get("sender") or ""
            match = re.search(r"[\w\.-]+@[\w\.-]+", sender)
            to_email = match.group(0) if match else sender
            subj = "Re: " + re.sub(r"^(Re:\s*)+", "", source.get("subject") or "", flags=re.I)
            if draft_id:
                try:
                    update_gmail_draft(draft_id, to_email, subj, edited_reply, thread_id=source.get("thread_id"))
                except Exception as e:
                    print(f"[WorkAgent] Failed to update draft on save: {e}")
            else:
                try:
                    draft_res = create_gmail_draft(to_email, subj, edited_reply, thread_id=source.get("thread_id"))
                    job["output"]["gmail_draft_id"] = draft_res.get("id")
                except Exception as e:
                    print(f"[WorkAgent] Failed to create draft on save: {e}")

            self._write_jobs(jobs)
            return job

    def approve_job(self, job_id, user_id, edited_reply=None, auto_sent=False):
        """Human approval boundary: Execute external action (send the Gmail draft)."""
        job = self.get_job(job_id, user_id)
        if not job:
            raise ValueError("Job not found")
        job = self._ensure_source_metadata(job_id, user_id, job) or job

        # Reconcile with Gmail thread before executing send
        source = job.get("source") or {}
        thread_id = source.get("thread_id")
        source_msg_id = source.get("message_id")
        if thread_id and not auto_sent:
            try:
                thread_data = get_gmail_thread(thread_id)
                if thread_data and thread_data.get("messages"):
                    msgs = thread_data["messages"]
                    source_message, outbound, match_reason = _strict_newer_outbound(
                        msgs,
                        source_msg_id
                    )
                    if match_reason == "source_outbound":
                        raise ValueError(
                            "Refusing to reply: this Work job was created from an outbound Gmail message."
                        )
                    if outbound:
                        print(
                            f"[WorkAgent] Aborting send for job {job_id}: verified newer "
                            f"outbound reply {outbound.get('id')} already exists."
                        )
                        self.reconcile_jobs_with_gmail(user_id)
                        return self.get_job(job_id, user_id) or job
                    if match_reason != "ok":
                        print(
                            f"[WorkAgent] Could not verify source message before explicit "
                            f"approval ({match_reason}); not inferring any previous reply."
                        )
            except Exception as e:
                print(f"[WorkAgent] approve_job reconciliation check notice: {e}")

        with self._lock:
            jobs = self._read_jobs()
            if job_id in jobs:
                jobs[job_id]["status"] = "sending"
                self._write_jobs(jobs)

        source = job.get("source") or {}
        sender = source.get("sender") or ""
        match = re.search(r"[\w\.-]+@[\w\.-]+", sender)
        to_email = match.group(0) if match else sender
        if not to_email:
            raise ValueError("Cannot send this Work item because its Gmail sender could not be resolved.")
        subj = "Re: " + re.sub(r"^(Re:\s*)+", "", source.get("subject") or "", flags=re.I)
        body = edited_reply or (job.get("reply_draft") or {}).get("body") or (job.get("output") or {}).get("suggested_reply") or "Thank you, I am working on this."

        try:
            draft_id = (job.get("output") or {}).get("gmail_draft_id")
            if not draft_id:
                # If no draft ID exists yet, create one now
                draft_res = create_gmail_draft(to_email, subj, body, thread_id=source.get("thread_id"))
                draft_id = draft_res.get("id")
            elif edited_reply:
                # Update existing draft with user's edited content before sending
                try:
                    update_gmail_draft(draft_id, to_email, subj, body, thread_id=source.get("thread_id"))
                except Exception as e:
                    print(f"[WorkAgent] Notice: draft update warning: {e}")

            # Now send the exact reviewed draft
            send_res = send_gmail_draft(draft_id)
            sent_message_id = str((send_res or {}).get("id") or "").strip()
            sent_thread_id = str(
                (send_res or {}).get("thread_id")
                or source.get("thread_id")
                or ""
            ).strip()
            if not sent_message_id:
                raise RuntimeError("Gmail send returned no sent message ID.")

            send_verified = False
            if sent_thread_id:
                for verify_attempt in range(4):
                    try:
                        sent_thread = get_gmail_thread(sent_thread_id)
                        exact_sent = next(
                            (
                                message for message in (sent_thread or {}).get("messages", [])
                                if str(message.get("id") or message.get("gmail_id") or "") == sent_message_id
                                and (
                                    message.get("direction") == "outbound"
                                    or message.get("is_sent_by_me")
                                )
                            ),
                            None,
                        )
                        if exact_sent:
                            send_verified = True
                            break
                    except Exception as verify_err:
                        print(f"[WorkAgent] Gmail send verification notice: {verify_err}")
                    time.sleep(0.35 * (verify_attempt + 1))
        except Exception as exc:
            with self._lock:
                jobs = self._read_jobs()
                if job_id in jobs:
                    jobs[job_id]["status"] = "waiting_approval"
                    jobs[job_id]["updated_at"] = _now()
                    self._write_jobs(jobs)
            raise exc

        with self._lock:

            jobs = self._read_jobs()
            if job_id in jobs:
                jobs[job_id]["status"] = "sent"
                jobs[job_id]["updated_at"] = _now()
                if edited_reply:
                    if "reply_draft" in jobs[job_id]:
                        jobs[job_id]["reply_draft"]["body"] = edited_reply
                    if "output" in jobs[job_id]:
                        jobs[job_id]["output"]["suggested_reply"] = edited_reply
                jobs[job_id]["output"]["gmail_draft_id"] = draft_id
                
                # Update steps
                for step in jobs[job_id].get("steps", []):
                    if step.get("type") in ["human_approval", "countdown"]:
                        step["status"] = "done"
                        step["label"] = "Sent automatically (Safe acknowledgement rule)" if auto_sent else "Approved & sent via Gmail"
                
                jobs[job_id]["steps"].append({
                    "type": "email_sent",
                    "label": (
                        f"Sent via Gmail and verified for {job.get('source', {}).get('sender')}"
                        if send_verified
                        else f"Sent via Gmail API to {job.get('source', {}).get('sender')} · verification pending"
                    ),
                    "status": "done",
                    "auto_sent": auto_sent,
                    "gmail_message_id": sent_message_id,
                    "verified": send_verified,
                    "at": _now()
                })
                self._write_jobs(jobs)
                return jobs[job_id]

    def _on_session_step(self, job_id, session, step_record):
        """Immediately persists each agent step as it executes."""
        with self._lock:
            jobs = self._read_jobs()
            job = jobs.get(job_id)
            if not job:
                return
            job["status"] = "working"
            job["steps"] = list(session.steps)
            job["artifacts"] = list(session.artifacts)
            if step_record:
                labels = {
                    'work.create_checklist': 'Understood request',
                    'mail.prepare_reply': 'Drafted reply',
                    'file.create_markdown': 'Created file',
                    'file.create_docx': 'Created document',
                    'file.create_pdf': 'Created PDF',
                    'work.finish': 'Ready for review',
                }
                activity = labels.get(step_record.get('action'), 'Preparing work')
                job["current_step"] = activity
                job["activity_label"] = activity
                job["step_index"] = session.step_count
                job["step_count"] = session.step_count
            job["checkpoint"] = session.to_dict()
            job["updated_at"] = _now()
            self._write_jobs(jobs)

    def _set_job_activity(self, job_id, status, label):
        with self._lock:
            jobs = self._read_jobs()
            job = jobs.get(job_id)
            if not job:
                return
            job['status'] = status
            job['current_step'] = label
            job['activity_label'] = label
            job['updated_at'] = _now()
            self._write_jobs(jobs)

    @staticmethod
    def _complex_work_required(source):
        text = f"{source.get('subject', '')} {source.get('snippet', '')}".lower()
        return bool(re.search(
            r'\b(research|compare sources|investigate|slides?|presentation|spreadsheet|codebase|debug|pdf attachment|ambiguous)\b',
            text,
        ))

    def _run_fast_work_path(self, session, source):
        """Prepare common email work with one model call and deterministic execution."""
        self._set_job_activity(session.job_id, 'planning', 'Queued for local AI')
        try:
            plan = LMStudioModel(timeout=min(self.timeout, 25)).work_plan(source)
        except (ModelUnavailable, ModelTimeout):
            if session.routing == 'LOCAL_ONLY':
                session.status = 'waiting_local_model'
                session.summary = 'Kyle paused because local compute is temporarily unavailable. Progress is saved.'
                return session
            return None
        except Exception as exc:
            print(f'[WorkAgent] Fast WorkPlan notice ({session.job_id}): {exc}')
            return None

        self._set_job_activity(session.job_id, 'drafting_reply', 'Drafting reply')
        registry = ToolRegistry()
        checklist = [_clean(item, 240) for item in (plan.get('checklist') or plan.get('requirements') or [])[:6] if _clean(item, 240)]
        if not checklist:
            checklist = ['Review the request', 'Prepare the requested response', 'Verify details before sending']
        session.set_checklist(checklist)
        session.record_step('', 'work.create_checklist', {'items': checklist}, {'allowed': True}, {
            'ok': True, 'summary': 'Requirements organized',
        })

        reply = plan.get('reply') if isinstance(plan.get('reply'), dict) else {}
        sender_match = re.search(r'[\w.+-]+@[\w.-]+', str(source.get('sender') or ''))
        to_email = sender_match.group(0) if sender_match else ''
        clean_subject = re.sub(r'^(Re:\s*)+', '', source.get('subject') or 'Update', flags=re.I)
        subject = str(reply.get('subject') or f'Re: {clean_subject}')[:180]
        body = str(reply.get('body') or '').strip()
        if not _substantive_reply(body):
            body = _fallback_reply(source.get('sender'), source.get('subject'), source.get('deadline'))
        session.set_reply(body, subject=subject, to=to_email)
        session.record_step('', 'mail.prepare_reply', {'subject': subject, 'to': to_email}, {'allowed': True}, {
            'ok': True, 'summary': 'Reply drafted',
        })

        artifacts = list(plan.get('artifacts') or [])[:2]
        combined = f"{source.get('subject', '')} {source.get('snippet', '')}".lower()
        if not artifacts and re.search(r'\b(assignment|submission|report|project|da)\b', combined):
            artifacts = [{
                'type': 'markdown',
                'filename': 'work_checklist.md',
                'spec': {
                    'title': source.get('subject') or 'Work checklist',
                    'sections': [{'heading': 'Action items', 'content': '\n'.join(f'- [ ] {item}' for item in checklist)}],
                },
            }]
        for artifact in artifacts:
            kind = str(artifact.get('type') or 'markdown').lower()
            if kind not in {'markdown', 'docx', 'pdf'}:
                continue
            filename = str(artifact.get('filename') or f'work_plan.{"md" if kind == "markdown" else kind}')
            args = {'filename': filename, 'spec': artifact.get('spec') or {'content': '\n'.join(checklist)}}
            action = f'file.create_{kind}'
            observation = registry.execute(action, args, session)
            session.record_step('', action, {'filename': filename}, {'allowed': True}, observation, 'done' if observation.get('ok') else 'error')

        session.summary = _clean(plan.get('summary') or source.get('snippet') or 'Work prepared.', 1200)
        self._set_job_activity(session.job_id, 'verifying', 'Verifying result')
        session.status = 'finished'
        session.finish_reason = 'fast_work_plan'
        Verifier().finalize(session)
        return session

    def _execute_job(self, job_id, user_id):
        with self._lock:
            jobs = self._read_jobs()
            job = jobs.get(job_id)
            if not job or job.get("status") == "cancelled":
                return
            job["status"] = "preparing"
            job["current_step"] = "Reading request"
            job["activity_label"] = "Reading request"
            job["updated_at"] = _now()
            self._write_jobs(jobs)

        source = job.get("source") or {}
        subject = source.get("subject") or ""
        snippet = source.get("snippet") or ""
        sender = source.get("sender") or ""
        deadline = source.get("deadline") or ""

        # Step 1: Privacy Gate & Routing
        gate = PrivacyGate.evaluate(source if source else {"subject": subject, "snippet": snippet, "sender": sender})
        routing = gate.get("routing", "LOCAL_ONLY")
        if routing == "BLOCK":
            with self._lock:
                jobs = self._read_jobs()
                if job_id in jobs:
                    jobs[job_id]["status"] = "blocked"
                    jobs[job_id]["summary"] = "Job blocked: content classified as sensitive by Privacy Gate."
                    jobs[job_id]["routing"] = routing
                    jobs[job_id]["updated_at"] = _now()
                    self._write_jobs(jobs)
            return

        settings = self.get_settings()
        autonomy_level = settings.get("auto_send_mode", "safe_replies")

        # Step 2: Initialize Bounded Agent Session with real-time on_step callback
        session = AgentSession(
            job_id=job_id,
            user_id=user_id,
            goal=f"Analyze email, extract requirements, generate checklist, prepare workspace files, and draft response for: {subject}",
            source_email=source,
            routing=routing,
            autonomy_level=autonomy_level,
            max_steps=10,
            max_tool_failures=3,
            max_research_calls=4,
            max_generated_files=5,
            on_step=lambda s, step: self._on_session_step(job_id, s, step)
        )

        # Restore the last successfully persisted agent checkpoint.
        # This lets a paused Tailscale/LM Studio job continue instead of replaying
        # already-completed research/file/tool actions.
        checkpoint = job.get("checkpoint") or {}
        if checkpoint:
            try:
                session.steps = list(checkpoint.get("steps") or [])
                session.artifacts = list(checkpoint.get("artifacts") or [])
                session.notes = list(checkpoint.get("notes") or [])
                session.checklist = list(checkpoint.get("checklist") or [])
                session.reply_draft = dict(checkpoint.get("reply_draft") or {})
                session.summary = str(checkpoint.get("summary") or "")
                counters = checkpoint.get("counters") or {}
                session.step_count = min(int(checkpoint.get("step_count") or len(session.steps)), session.max_steps - 1)
                session.tool_failures = int(counters.get("tool_failures") or 0)
                session.research_calls = int(counters.get("research_calls") or 0)
                session.generated_files = int(counters.get("generated_files") or 0)
                session.status = "running"
                session.finish_reason = None
            except Exception as checkpoint_exc:
                print(f"[WorkAgent] Checkpoint restore notice: {checkpoint_exc}")

        # Step 3: Common email work uses one structured plan. Complex work keeps the bounded loop.
        final_session = None
        if not checkpoint and not self._complex_work_required(source):
            final_session = self._run_fast_work_path(session, source)
        if final_session is None:
            loop = AgentLoop(session)
            final_session = loop.run()

        # Step 4: Handle needs_input (e.g. required specific deliverable like OS PDF is missing)
        if final_session.status == "needs_input":
            with self._lock:
                jobs = self._read_jobs()
                if job_id in jobs:
                    j = jobs[job_id]
                    j["status"] = "needs_input"
                    j["missing_deliverable"] = final_session.missing_deliverable
                    j["summary"] = final_session.summary or f"Needs input — required {final_session.missing_deliverable or 'file'} is missing."
                    j["updated_at"] = _now()
                    j["steps"] = final_session.steps
                    j["artifacts"] = final_session.artifacts
                    j["output"] = {
                        "summary": j["summary"],
                        "checklist": final_session.checklist,
                        "suggested_reply": final_session.reply_draft.get("body") or "",
                        "gmail_draft_id": None,
                        "notes": final_session.notes
                    }
                    self._write_jobs(jobs)
            return

        # Step 4.5: Handle model availability / routing pause
        if final_session.status == "waiting_local_model":
            with self._lock:
                jobs = self._read_jobs()
                if job_id in jobs:
                    jobs[job_id]["status"] = "waiting_local_model"
                    jobs[job_id]["summary"] = "Kyle paused because local compute is temporarily unavailable. Progress is saved."
                    jobs[job_id]["routing"] = routing
                    jobs[job_id]["steps"] = final_session.steps
                    jobs[job_id]["artifacts"] = final_session.artifacts
                    jobs[job_id]["checkpoint"] = final_session.to_dict()
                    jobs[job_id]["pause_reason"] = "compute_unavailable"
                    jobs[job_id]["paused_at"] = _now()
                    jobs[job_id]["current_step"] = "Waiting for local AI · progress saved"
                    jobs[job_id]["updated_at"] = _now()
                    self._write_jobs(jobs)
            return

        # Step 5: Check/Sync Gmail Draft if requested
        draft_id = None
        for art in final_session.artifacts:
            if art.get("type") == "email_draft":
                draft_id = art.get("draft_id")
                break

        suggested_reply = final_session.reply_draft.get("body") or ""
        if not _substantive_reply(suggested_reply):
            suggested_reply = _fallback_reply(sender, subject, deadline)
            reply_subject = "Re: " + re.sub(r"^(Re:\s*)+", "", subject, flags=re.I)
            final_session.set_reply(suggested_reply, subject=reply_subject)

        if not draft_id and settings.get("create_gmail_drafts", True):
            try:
                match = re.search(r"[\w\.-]+@[\w\.-]+", sender)
                to_email = match.group(0) if match else sender
                reply_subject = "Re: " + re.sub(r"^(Re:\s*)+", "", subject, flags=re.I)
                draft_res = create_gmail_draft(
                    to=to_email,
                    subject=reply_subject,
                    body=suggested_reply,
                    thread_id=source.get("thread_id"),
                    in_reply_to=source.get("rfc_message_id") or None,
                )
                draft_id = draft_res.get("id")
                final_session.add_artifact({
                    "type": "email_draft",
                    "name": f"Gmail Draft ({reply_subject})",
                    "draft_id": draft_id
                })
            except Exception as draft_err:
                print(f"[WorkAgent] Notice: Gmail draft creation skipped: {draft_err}")

        # Step 6: Evaluate with AutoSendPolicy
        policy_verdict = AutoSendPolicy.evaluate(
            suggested_reply,
            artifacts=final_session.artifacts,
            source=source,
            settings=settings
        )

        # Step 7: Finalize Job State
        with self._lock:
            jobs = self._read_jobs()
            if job_id in jobs:
                j = jobs[job_id]
                j["clean_title"] = clean_job_title(j.get("title", ""), subject)
                j["updated_at"] = _now()
                j["routing"] = routing
                j["output"] = {
                    "summary": final_session.summary or snippet,
                    "checklist": final_session.checklist,
                    "suggested_reply": suggested_reply,
                    "gmail_draft_id": draft_id,
                    "notes": final_session.notes
                }
                j["artifacts"] = final_session.artifacts
                j["policy_verdict"] = policy_verdict
                j["verification"] = final_session.verification_report

                # Merge agent loop steps with final review/dispatch steps
                steps = list(final_session.steps)
                if not steps:
                    steps = [
                        {"type": "read_thread", "label": f"Read thread from {sender.split('<')[0].strip()}", "status": "done"},
                        {"type": "extract_deadline", "label": f"Found deadline ({deadline or 'urgent'})", "status": "done"},
                        {"type": "prepare_reply", "label": "Prepared response draft", "status": "done"},
                        {"type": "create_files", "label": f"Created {len(final_session.artifacts)} workspace artifacts", "status": "done"},
                        {"type": "create_gmail_draft", "label": "Synced draft to Gmail", "status": "done" if draft_id else "skipped"}
                    ]

                if draft_id and not any(step.get("type") == "create_gmail_draft" for step in steps):
                    steps.append({
                        "type": "create_gmail_draft",
                        "label": "Synced reply draft to Gmail",
                        "status": "done"
                    })
                elif (
                    not draft_id
                    and settings.get("create_gmail_drafts", True)
                    and not any(step.get("type") == "create_gmail_draft" for step in steps)
                ):
                    steps.append({
                        "type": "create_gmail_draft",
                        "label": "Gmail draft could not be staged",
                        "status": "skipped"
                    })

                if policy_verdict.get("auto_send_allowed") is True:
                    # Safe reply -> start 20s cancelable countdown
                    countdown_sec = int(settings.get("countdown_seconds", 20))
                    started_at = _now()
                    send_at = (datetime.now(timezone.utc) + timedelta(seconds=countdown_sec)).isoformat()
                    j["status"] = "auto_send_countdown"
                    j["countdown"] = {
                        "duration": countdown_sec,
                        "started_at": started_at,
                        "auto_send_at": send_at,
                        "cancelled": False
                    }
                    steps.append({
                        "type": "countdown",
                        "label": f"Policy approved: sending in {countdown_sec}s",
                        "status": "active"
                    })
                    j["steps"] = steps
                    self._write_jobs(jobs)
                    self._start_countdown_timer(job_id, user_id, send_at, countdown_sec)
                else:
                    # High / medium risk -> hold for human approval
                    j["status"] = "waiting_approval"
                    steps.append({
                        "type": "human_approval",
                        "label": "Waiting for human approval",
                        "status": "pending"
                    })
                    j["steps"] = steps
                    self._write_jobs(jobs)

    def _start_countdown_timer(self, job_id, user_id, expected_send_at, seconds=20):
        """Launches a background timer that dispatches the draft if not cancelled, with pre-send reconciliation check."""
        def timer_loop():
            time.sleep(seconds)
            with self._lock:
                jobs = self._read_jobs()
                job = jobs.get(str(job_id))
                if not job or job.get("status") != "auto_send_countdown":
                    return
                # Verify countdown timestamp was not aborted
                c = job.get("countdown") or {}
                if c.get("cancelled") or c.get("auto_send_at") != expected_send_at:
                    return

            # Pre-send reconciliation invariant: verify user hasn't replied manually in Gmail
            source = job.get("source") or {}
            thread_id = source.get("thread_id")
            if thread_id:
                try:
                    thread_data = get_gmail_thread(thread_id)
                    if thread_data and thread_data.get("messages"):
                        source_msg_id = source.get("message_id")
                        msgs = thread_data["messages"]
                        source_message, outbound, match_reason = _strict_newer_outbound(
                            msgs,
                            source_msg_id
                        )

                        if match_reason == "source_outbound":
                            with self._lock:
                                jobs = self._read_jobs()
                                current = jobs.get(str(job_id))
                                if current:
                                    current["status"] = "ignored_outbound"
                                    current["resolution"] = "source_message_was_outbound"
                                    current["updated_at"] = _now()
                                    if "countdown" in current:
                                        current["countdown"]["cancelled"] = True
                                    _append_step_once(
                                        current,
                                        "ignored_outbound",
                                        "Auto-send cancelled — source message was sent by you"
                                    )
                                    self._write_jobs(jobs)
                            return

                        if match_reason != "ok":
                            with self._lock:
                                jobs = self._read_jobs()
                                current = jobs.get(str(job_id))
                                if current:
                                    current["status"] = "waiting_approval"
                                    current["updated_at"] = _now()
                                    if "countdown" in current:
                                        current["countdown"]["cancelled"] = True
                                    _append_step_once(
                                        current,
                                        "human_approval",
                                        "Auto-send paused — Gmail source could not be verified",
                                        status="pending"
                                    )
                                    self._write_jobs(jobs)
                            return

                        if outbound:
                            print(
                                f"[WorkAgent] Auto-send aborted: verified newer manual "
                                f"reply {outbound.get('id')} exists on thread {thread_id}."
                            )
                            self.reconcile_jobs_with_gmail(user_id)
                            return
                except Exception as check_err:
                    print(f"[WorkAgent] Pre-send thread check error: {check_err}")

            print(f"[WorkAgent] Countdown expired. Auto-sending safe reply for job {job_id}...")
            try:
                self.approve_job(job_id, user_id, auto_sent=True)
            except Exception as e:
                print(f"[WorkAgent] Auto-send execution failed for {job_id}: {e}")

        t = threading.Thread(target=timer_loop, daemon=True, name=f"AutoSendTimer-{job_id}")
        t.start()

    def _call_llm(self, subject, snippet, sender, deadline, kind):
        """Call LM Studio if available, otherwise fallback to Gemini."""
        prompt = (
            f"You are Mailmate's autonomous preparatory work agent.\n"
            f"Analyze this inbound email and prepare actionable output.\n"
            f"Subject: {subject}\n"
            f"From: {sender}\n"
            f"Deadline: {deadline}\n"
            f"Snippet: {snippet}\n\n"
            f"Return JSON strictly matching this schema:\n"
            f'{{\n'
            f'  "summary": "Brief summary of the requested task",\n'
            f'  "checklist": ["Step 1", "Step 2", "Step 3"],\n'
            f'  "suggested_reply": "Natural, polite email reply acknowledging the request. Sign off as Priyam.",\n'
            f'  "notes": ["Important detail 1", "Important detail 2"]\n'
            f'}}'
        )

        # 1. Try LM Studio
        try:
            headers = {"Content-Type": "application/json"}
            payload = {
                "model": self.model or "local-model",
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": "You are a helpful email productivity agent. Always respond with valid JSON only."},
                    {"role": "user", "content": prompt}
                ]
            }
            res = requests.post(f"{self.base_url}/chat/completions", headers=headers, json=payload, timeout=self.timeout)
            if res.ok:
                content = res.json()["choices"][0]["message"]["content"]
                clean_json = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.I)
                return json.loads(clean_json)
        except Exception:
            pass

        # 2. Try Gemini Fallback
        try:
            import google.generativeai as genai
            genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
            m = genai.GenerativeModel("gemini-3.5-flash-lite", generation_config={"response_mime_type": "application/json"})
            res = m.generate_content(prompt)
            return json.loads(res.text)
        except Exception:
            # Fallback default template
            return {
                "summary": f"Task: {subject}",
                "checklist": [
                    f"Review {subject} details",
                    f"Complete task before {deadline or 'due date'}",
                    "Submit deliverables"
                ],
                "suggested_reply": (
                    f"Hi {sender.split('<')[0].strip()},\n\n"
                    f"I received your email regarding '{subject}'. I am reviewing the details now and will have this completed within the requested deadline ({deadline or 'asap'}).\n\n"
                    f"Best regards,\nPriyam"
                ),
                "notes": [f"Deadline noted: {deadline}"]
            }

work_agent_service = WorkAgentService()
