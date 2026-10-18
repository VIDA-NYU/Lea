"""SQL heartbeat probe; never imports a worker or touches a workspace."""
from app.process_state import accepting_work
raise SystemExit(0 if accepting_work() else 1)
