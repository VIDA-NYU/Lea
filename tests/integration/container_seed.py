"""Create deployed-history fixtures inside a disposable production container."""
import hashlib
from pathlib import Path
import sqlite3
import sys
from alembic import command
from app import db, migrations

revision = sys.argv[1]
Path(db.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
command.upgrade(migrations._config(), revision)
source = 'import Lea.Readiness.Helper\nnamespace Lea.Readiness\ntheorem demo : True := helper\nend Lea.Readiness\n'
root = Path('/app/prover/workspace/proofs/Lea/Readiness')
root.mkdir(parents=True, exist_ok=True)
(root / 'Helper.lean').write_text('namespace Lea.Readiness\ntheorem helper : True := by trivial\nend Lea.Readiness\n')
(root / 'Proof.lean').write_text(source)
with sqlite3.connect(db.DB_PATH) as conn:
    conn.execute("insert into projects(id,slug,title,namespace,repo_path,created_at,updated_at) values ('p','readiness','Readiness','Lea.Readiness','proofs/Lea/Readiness','t','t')")
    conn.execute("insert into sessions(id,project_id,title,origin,created_at,updated_at) values ('s','p','Readiness','overleaf','t','t')")
    conn.execute("insert into runs(id,session_id,project_id,status,model,result_kind,created_at,updated_at) values ('r','s','p','done','test/model','proved','t','t')")
    conn.execute("insert into artifact_blobs(id,sha256,content,created_at) values ('b',?,?, 't')", (hashlib.sha256(source.encode()).hexdigest(), source))
    conn.execute("insert into timeline(session_id,run_id,kind,author,path,after_blob_id,check_status,artifact_kind,created_at) values ('s','r','code','agent','Proof.lean','b','ok','proof','t')")
