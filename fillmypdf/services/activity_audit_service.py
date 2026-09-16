"""
Clinic activity audit log
=========================
Append-only JSONL of mutating API actions (who / when / what resource).
Never stores request bodies, notes, card pixels, or field values.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import settings

_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# More specific patterns first. Group 1, when present, is the resource id.
_RULES: List[Tuple[str, re.Pattern[str], str, str]] = [
    (m, re.compile(p), event, rtype)
    for m, p, event, rtype in (
        ("POST", r"^/api/v1/auth/register/?$", "auth.register", "account"),
        ("POST", r"^/api/v1/auth/login/?$", "auth.login", "account"),
        ("POST", r"^/api/v1/auth/logout/?$", "auth.logout", "account"),
        ("POST", r"^/api/v1/profiles/import/?$", "profile.import", "profile"),
        ("POST", r"^/api/v1/profiles/?$", "profile.create", "profile"),
        ("PATCH", r"^/api/v1/profiles/([^/]+)/?$", "profile.update", "profile"),
        ("DELETE", r"^/api/v1/profiles/([^/]+)/?$", "profile.delete", "profile"),
        ("POST", r"^/api/v1/templates/([^/]+)/fill/?$", "template.fill", "template"),
        ("POST", r"^/api/v1/templates/([^/]+)/batch-csv/?$", "template.batch_csv", "template"),
        ("POST", r"^/api/v1/templates/([^/]+)/batch/?$", "template.batch", "template"),
        ("POST", r"^/api/v1/templates/([^/]+)/sign/?$", "template.sign", "template"),
        ("PUT", r"^/api/v1/templates/([^/]+)/signature-fields/?$", "template.signature_fields", "template"),
        ("PUT", r"^/api/v1/templates/([^/]+)/?$", "template.update", "template"),
        ("DELETE", r"^/api/v1/templates/([^/]+)/?$", "template.delete", "template"),
        ("POST", r"^/api/v1/templates/?$", "template.upload", "template"),
        ("POST", r"^/api/v1/note-paste/extract/?$", "note.paste", "note"),
        ("POST", r"^/api/v1/card-capture/extract/?$", "card.capture", "card"),
        ("POST", r"^/api/v1/extract/?$", "extract.run", "extract"),
        ("POST", r"^/api/v1/jobs/template-batch/?$", "job.template_batch_submit", "job"),
        ("POST", r"^/api/v1/jobs/xlsx-batch/?$", "job.xlsx_batch_submit", "job"),
        ("POST", r"^/api/v1/jobs/batch/?$", "job.batch_submit", "job"),
        ("POST", r"^/api/v1/jobs/extract/?$", "job.extract_submit", "job"),
        ("POST", r"^/api/v1/jobs/([^/]+)/retry-webhook/?$", "job.webhook_retry", "job"),
        ("DELETE", r"^/api/v1/jobs/([^/]+)/?$", "job.cancel", "job"),
        ("POST", r"^/api/v1/batch/fill-json/?$", "batch.fill_json", "batch"),
        ("POST", r"^/api/v1/batch/fill-csv/?$", "batch.fill_csv", "batch"),
        ("POST", r"^/api/v1/batch/fill-xlsx/?$", "batch.fill_xlsx", "batch"),
        ("POST", r"^/api/v1/batch/template-fields/?$", "batch.inspect", "batch"),
        ("POST", r"^/api/v1/pdf/merge/?$", "pdf.merge", "pdf"),
        ("POST", r"^/api/v1/pdf/split/?$", "pdf.split", "pdf"),
        ("POST", r"^/api/v1/pdf/convert-fillable/?$", "pdf.convert_fillable", "pdf"),
        ("POST", r"^/api/v1/pa/fill/report/?$", "pa.fill_report", "pa"),
        ("POST", r"^/api/v1/pa/fill/?$", "pa.fill", "pa"),
        ("POST", r"^/api/v1/mappings/lock-batch/?$", "mapping.lock_batch", "mapping"),
        ("POST", r"^/api/v1/mappings/build/?$", "mapping.build", "mapping"),
        ("POST", r"^/api/v1/mappings/ai-suggest/?$", "mapping.ai_suggest_all", "mapping"),
        ("POST", r"^/api/v1/mappings/([^/]+)/lock/?$", "mapping.lock", "mapping"),
        ("POST", r"^/api/v1/mappings/([^/]+)/unlock/?$", "mapping.unlock", "mapping"),
        ("POST", r"^/api/v1/mappings/([^/]+)/ai-suggest/?$", "mapping.ai_suggest", "mapping"),
        ("POST", r"^/api/v1/mappings/([^/]+)/form-spec/lock/?$", "mapping.form_spec_lock", "mapping"),
        ("POST", r"^/api/v1/mappings/([^/]+)/checklist/ai-suggest/?$", "mapping.checklist_suggest", "mapping"),
        ("PUT", r"^/api/v1/mappings/([^/]+)/checklist/?$", "mapping.checklist_update", "mapping"),
        ("PATCH", r"^/api/v1/mappings/([^/]+)/?$", "mapping.update", "mapping"),
        ("POST", r"^/api/v1/keys/([^/]+)/revoke/?$", "key.revoke", "api_key"),
        ("POST", r"^/api/v1/keys/?$", "key.create", "api_key"),
        ("DELETE", r"^/api/v1/keys/([^/]+)/?$", "key.delete", "api_key"),
        ("POST", r"^/api/v1/signatures/detect-fields/?$", "signature.detect", "signature"),
        ("POST", r"^/api/v1/signatures/apply/?$", "signature.apply", "signature"),
        ("PUT", r"^/api/v1/signatures/saved/?$", "signature.save", "signature"),
        ("DELETE", r"^/api/v1/signatures/saved/?$", "signature.delete_saved", "signature"),
        ("POST", r"^/api/v1/signing-sessions/stage-pdf/?$", "signing.stage_pdf", "signing_session"),
        ("POST", r"^/api/v1/signing-sessions/([^/]+)/sign/?$", "signing.session_sign", "signing_session"),
        ("POST", r"^/api/v1/signing-sessions/([^/]+)/cancel/?$", "signing.session_cancel", "signing_session"),
        ("POST", r"^/api/v1/signing-sessions/?$", "signing.session_create", "signing_session"),
        ("POST", r"^/api/v1/approvals/([^/]+)/decide/?$", "approval.decide", "approval"),
        ("POST", r"^/api/v1/approvals/?$", "approval.create", "approval"),
        ("POST", r"^/api/v1/renewals/?$", "renewal.create", "renewal"),
        ("PATCH", r"^/api/v1/renewals/([^/]+)/?$", "renewal.update", "renewal"),
        ("DELETE", r"^/api/v1/renewals/([^/]+)/?$", "renewal.delete", "renewal"),
        ("POST", r"^/api/v1/account/library/uploads/?$", "library.upload", "library"),
        ("POST", r"^/api/v1/account/library/([^/]+)/?$", "library.add", "library"),
        ("DELETE", r"^/api/v1/account/library/([^/]+)/?$", "library.remove", "library"),
        ("PUT", r"^/api/v1/account/recipes/([^/]+)/?$", "recipe.save", "recipe"),
        ("DELETE", r"^/api/v1/account/recipes/([^/]+)/?$", "recipe.delete", "recipe"),
        ("POST", r"^/api/v1/billing/checkout/?$", "billing.checkout", "billing"),
        ("POST", r"^/api/v1/billing/portal/?$", "billing.portal", "billing"),
    )
]

_SKIP_PREFIXES = (
    "/api/v1/audit",
    "/api/v1/billing/webhook",
    "/api/v1/leads",
)


def should_audit(method: str, path: str) -> bool:
    if method.upper() not in _MUTATING:
        return False
    if not path.startswith("/api/v1/"):
        return False
    return not any(path.startswith(p) for p in _SKIP_PREFIXES)


def classify_event(method: str, path: str) -> Tuple[str, str, Optional[str]]:
    """Return (event, resource_type, resource_id)."""
    method = (method or "POST").upper()
    for m, pat, event, rtype in _RULES:
        if m != method:
            continue
        match = pat.match(path)
        if match:
            rid = match.group(1) if match.lastindex else None
            return event, rtype, rid
    parts = [p for p in path.split("/") if p]
    segment = parts[2] if len(parts) > 2 else "api"
    return f"api.{method.lower()}.{segment}", segment, None


def _client_ip(headers: Dict[str, str], fallback: Optional[str]) -> Optional[str]:
    fwd = (headers.get("x-forwarded-for") or headers.get("X-Forwarded-For") or "").strip()
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return (fallback or "")[:64] or None


class ActivityAuditService:
    """Append-only JSONL under STORAGE_DIR/audit/events.jsonl."""

    @property
    def _dir(self) -> Path:
        p = settings.STORAGE_DIR / "audit"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def _log_path(self) -> Path:
        return self._dir / "events.jsonl"

    def _write_entry(self, entry: Dict[str, Any]) -> None:
        with open(self._log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")

    def record(
        self,
        *,
        event: str,
        method: str,
        path: str,
        status: int,
        actor_id: Optional[str] = None,
        actor_email: Optional[str] = None,
        org_id: Optional[str] = None,
        api_key_id: Optional[str] = None,
        client_ip: Optional[str] = None,
        request_id: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
    ) -> str:
        audit_id = f"act_{uuid.uuid4().hex[:16]}"
        entry: Dict[str, Any] = {
            "audit_id": audit_id,
            "event": event,
            "at": datetime.now(timezone.utc).isoformat(),
            "method": method.upper(),
            "path": path,
            "status": int(status),
            "actor_id": actor_id,
            "actor_email": actor_email,
            "org_id": org_id,
            "api_key_id": api_key_id,
            "client_ip": client_ip,
            "request_id": request_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
        }
        self._write_entry(entry)
        return audit_id

    def record_request(
        self,
        *,
        method: str,
        path: str,
        status: int,
        headers: Optional[Dict[str, str]] = None,
        client_host: Optional[str] = None,
        request_id: Optional[str] = None,
        api_key: Optional[dict] = None,
        user: Optional[dict] = None,
    ) -> Optional[str]:
        if not should_audit(method, path):
            return None
        event, resource_type, resource_id = classify_event(method, path)
        key = api_key or {}
        usr = user or {}
        try:
            return self.record(
                event=event,
                method=method,
                path=path.split("?", 1)[0],
                status=status,
                actor_id=usr.get("id") or key.get("user_id"),
                # Same fallback mapping_review_routes._request_actor() already
                # uses: a bare API key (no logged-in session — the bootstrap
                # admin key flow, automation, etc.) has no email, but its name
                # is still a meaningful "who did this" for the audit trail.
                actor_email=usr.get("email") or key.get("owner") or key.get("name"),
                org_id=usr.get("org_id") or key.get("org_id"),
                api_key_id=key.get("id"),
                client_ip=_client_ip(headers or {}, client_host),
                request_id=request_id,
                resource_type=resource_type,
                resource_id=resource_id,
            )
        except Exception:
            return None

    def list_recent(
        self,
        *,
        limit: int = 100,
        event: Optional[str] = None,
        org_id: Optional[str] = None,
        api_key_id: Optional[str] = None,
        admin: bool = False,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not self._log_path.exists():
            return []
        cap = max(1, min(int(limit or 100), 500))
        lines = self._log_path.read_text(encoding="utf-8").strip().splitlines()
        needle = (event or "").strip()
        want_rtype = (resource_type or "").strip()
        want_rid = (resource_id or "").strip()
        out: List[Dict[str, Any]] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if needle and not str(entry.get("event") or "").startswith(needle):
                continue
            if want_rtype and entry.get("resource_type") != want_rtype:
                continue
            if want_rid and entry.get("resource_id") != want_rid:
                continue
            if not admin:
                if org_id:
                    if entry.get("org_id") != org_id:
                        continue
                elif api_key_id:
                    if entry.get("api_key_id") != api_key_id:
                        continue
                else:
                    continue
            out.append(entry)
            if len(out) >= cap:
                break
        return out

    def lock_and_upload_index(self) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        """One pass over the whole log — used by the Templates list, which
        needs this for every row and must not re-scan the log per row.

        Returns ``(mapping_index, template_index)``:
          mapping_index[fp]  -> {"locked_at", "locked_by", "revision_count"}
              locked_at/by come only from ``mapping.lock`` events (never the
              mapping cache's own actor/updated_at, which any later edit
              overwrites) so "when was this locked" survives later edits.
              revision_count counts every mapping.* event for that fp.
          template_index[template_id] -> {"added_at", "added_by"}
              from the ``template.upload`` event.
        """
        mapping_index: Dict[str, Dict[str, Any]] = {}
        template_index: Dict[str, Dict[str, Any]] = {}
        if not self._log_path.exists():
            return mapping_index, template_index
        # Oldest -> newest (file order, not reversed) so the last write for a
        # given key naturally ends up being the most recent one.
        for line in self._log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = entry.get("resource_type")
            rid = (entry.get("resource_id") or "").strip()
            event = str(entry.get("event") or "")
            if not rid:
                continue
            if rtype == "mapping":
                m = mapping_index.setdefault(
                    rid, {"locked_at": None, "locked_by": None, "revision_count": 0}
                )
                m["revision_count"] += 1
                if event == "mapping.lock":
                    m["locked_at"] = entry.get("at")
                    m["locked_by"] = entry.get("actor_email")
            elif rtype == "template" and event == "template.upload":
                template_index[rid] = {
                    "added_at": entry.get("at"),
                    "added_by": entry.get("actor_email"),
                }
        return mapping_index, template_index
