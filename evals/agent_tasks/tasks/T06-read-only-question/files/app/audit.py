from app.logging_utils import log_event
from storage.audit_store import write_audit_record


def audit(action, target):
    log_event(f"{action} {target}")
    write_audit_record({"action": action, "target": target})
