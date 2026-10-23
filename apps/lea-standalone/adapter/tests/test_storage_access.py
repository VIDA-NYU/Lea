"""Restricted production API can use service-owned DB/intake, without broad ACLs."""
from pathlib import Path
import stat
import pytest
from app import bootstrap,db


@pytest.fixture
def storage(tmp_path,monkeypatch):
    root=tmp_path/'data';root.mkdir();database=root/'db.sqlite';database.write_bytes(b'preserved database bytes')
    monkeypatch.setattr(db,'DB_PATH',database);monkeypatch.setenv('LEA_REQUIRE_SHARED_OWNERSHIP','1')
    monkeypatch.setattr(bootstrap.os.path,'ismount',lambda p:Path(p)==root)
    changed=[];monkeypatch.setattr(bootstrap.os,'chown',lambda path,*args,**kw:changed.append(Path(path)))
    return root,database,changed


def test_dedicated_storage_is_private_and_preserves_payloads(storage):
    root,database,changed=storage
    intake=root/'credential-intake';intake.mkdir();ticket=intake/'ticket';ticket.write_bytes(b'private input')
    unrelated=root/'backups';unrelated.mkdir();backup=unrelated/'old.sqlite';backup.write_bytes(b'old backup')
    bootstrap.prepare_storage_access()
    assert database.read_bytes()==b'preserved database bytes' and ticket.read_bytes()==b'private input'
    assert stat.S_IMODE(root.stat().st_mode)==0o700 and stat.S_IMODE(database.stat().st_mode)==0o600
    assert stat.S_IMODE(ticket.stat().st_mode)==0o600 and (root/'http-intake').is_dir()
    assert database in changed and ticket in changed and unrelated not in changed and backup not in changed


def test_storage_outside_a_dedicated_mount_is_rejected(storage,monkeypatch):
    root,database,changed=storage;monkeypatch.setattr(bootstrap.os.path,'ismount',lambda _:False)
    with pytest.raises(RuntimeError,match='dedicated shared mount'):bootstrap.prepare_storage_access()
    assert not changed and database.read_bytes()==b'preserved database bytes'


def test_intake_symlink_is_rejected_before_any_permissions_change(storage,tmp_path):
    root,database,changed=storage
    outside=tmp_path/'outside';outside.write_text('untouched');intake=root/'http-intake';intake.mkdir()
    (intake/'link').symlink_to(outside)
    with pytest.raises(RuntimeError,match='unexpected symlink'):bootstrap.prepare_storage_access()
    assert not changed and outside.read_text()=='untouched'


def test_large_intake_requires_bounded_offline_cleanup(storage,monkeypatch):
    root,database,changed=storage;intake=root/'http-intake';intake.mkdir()
    for name in ('one','two','three'):(intake/name).touch()
    monkeypatch.setattr(bootstrap,'MAX_INTAKE_ENTRIES',3)
    with pytest.raises(RuntimeError,match='offline cleanup'):bootstrap.prepare_storage_access()
    assert not changed


def test_local_development_does_not_transfer_storage_ownership(storage,monkeypatch):
    root,database,changed=storage;monkeypatch.delenv('LEA_REQUIRE_SHARED_OWNERSHIP')
    bootstrap.prepare_storage_access();assert not changed
