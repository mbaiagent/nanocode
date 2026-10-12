import json

AUDIT_FILE = "audit.log"


def write_audit_record(record):
    with open(AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def read_audit_records():
    with open(AUDIT_FILE, encoding="utf-8") as f:
        return [json.loads(line) for line in f]
