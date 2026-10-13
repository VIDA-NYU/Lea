"""Deterministic, checkpointable final-verification progress guard."""
import hashlib
import json
from pathlib import Path

from .imports import direct_imports


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def file_hash(path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest() if path else None
    except OSError:
        return None


class StallDetector:
    LIMIT = 3

    def __init__(self, state=None):
        state = state or {}
        self.fingerprint = state.get("fingerprint")
        self.failures = int(state.get("failures", 0))
        self.seen = set(state.get("seen_evidence", []))
        self.evidence_revision = int(state.get("evidence_revision", 0))

    def state(self):
        return {"fingerprint": self.fingerprint, "failures": self.failures,
                "seen_evidence": sorted(self.seen), "evidence_revision": self.evidence_revision}

    def note_tool(self, name, args, result, *, successful=True):
        # These are acknowledgements or already represented by verification/file
        # evidence. A no-op write and an identical recheck cannot buy more turns.
        if (not successful or name in {"update_lea_status", "lean_check", "write_file", "edit_file"}
                or str(result).strip().lower() in {"", "ok", "done", "acknowledged", "ack", "success"}
                or str(result).lower().startswith("error:")):
            return
        signature = digest((name, args, str(result)))
        if signature not in self.seen:
            self.seen.add(signature)
            self.evidence_revision += 1

    def failure(self, proof, source, dependencies, workspace=None):
        imported = {}
        # CLI runs have no registry host. Local import bytes still form evidence.
        pending = [Path(proof.latest_proof_path)] if proof.latest_proof_path else []
        root = Path(workspace) if workspace else None
        while pending and root:
            path = pending.pop()
            try:
                for module in direct_imports(path.read_text()):
                    target = root.joinpath(*module.split(".")).with_suffix(".lean")
                    if module not in imported:
                        imported[module] = file_hash(target)
                        if target.is_file():
                            pending.append(target)
            except OSError:
                pass
        fingerprint = digest({"path": proof.latest_proof_path,
            "bytes": file_hash(proof.latest_proof_path), "passed": proof.latest_check_passed,
            "diagnostics": proof.latest_check_output, "source": source,
            "dependencies": dependencies, "imports": imported, "evidence": self.evidence_revision})
        self.failures = self.failures + 1 if fingerprint == self.fingerprint else 1
        self.fingerprint = fingerprint
        return self.failures >= self.LIMIT

    @staticmethod
    def explanation(proof):
        blocker = proof.latest_check_output or "No proof artifact was produced."
        return ("Paused after three unchanged final verification failures. Partial work is retained.\n\n"
                + str(blocker) + "\n\nNext action: repair the artifact or resolve the recorded blocker, "
                "then Resume for another bounded attempt.")
