"""Run once before starting API/worker. Preserves all existing data and backups."""
from app.bootstrap import migrate
if __name__=='__main__':migrate()
