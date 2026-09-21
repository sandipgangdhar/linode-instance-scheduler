#!/usr/bin/env python3

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import math
import os
import re
import secrets
import shlex
import sqlite3
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Literal, TypeGuard
from zoneinfo import ZoneInfo

import requests.exceptions
from dotenv import load_dotenv
from linode_api4 import Instance, Volume
from linode_api4.errors import ApiError

import linode_engine as engine
import object_storage_backup as osb
from linode_engine import validate_instance_name

REGISTRY_PATH = engine.BASE_DIR / "state" / "instances.db"
MIGRATIONS_PATH = engine.BASE_DIR / "state" / "migrations.json"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class InstanceLockedError(RuntimeError):
    pass


class NotOnboardedError(engine.ConfigError):
    pass


class NeedsManualRecoveryError(engine.ConfigError):
    pass


class GroupNotFoundError(engine.ConfigError):
    pass


class _OnboardRefusal(Exception):
    pass


_INSTANCE_COLUMNS = (
    "name, label, region, network_interface_model, network_config, network_helper_enabled,"
    " vpc_prefix, os_volume_id, data_volumes, reserved_ip, authorized_keys, tags,"
    " instance_attrs, group_id, current_linode_id, current_status, transitioning,"
    " manual_override_expires_at"
)
_INSTANCE_PLACEHOLDERS = ", ".join(["?"] * 18)
_INSTANCE_COLUMN_LIST = [c.strip() for c in _INSTANCE_COLUMNS.split(",")]
_INSTANCE_UPDATE_CLAUSE = ", ".join(
    f"{c} = excluded.{c}" for c in _INSTANCE_COLUMN_LIST if c != "name"
)


def _migrate_stale_schedule_events_schema(conn: sqlite3.Connection) -> None:

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='schedule_events'"
    ).fetchone()
    if row is None or "REFERENCES instances" not in row[0]:
        return
    conn.execute("ALTER TABLE schedule_events RENAME TO schedule_events_pre_fk_fix")
    conn.execute(
        "CREATE TABLE schedule_events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " instance_name TEXT NOT NULL,"
        " action TEXT NOT NULL CHECK (action IN ('create', 'delete')),"
        " triggered_by TEXT NOT NULL CHECK (triggered_by IN ('schedule', 'manual', 'api')),"
        " timestamp TIMESTAMP NOT NULL,"
        " result TEXT NOT NULL,"
        " error_message TEXT"
        ")"
    )
    conn.execute(
        "INSERT INTO schedule_events"
        " (id, instance_name, action, triggered_by, timestamp, result, error_message)"
        " SELECT id, instance_name, action, triggered_by, timestamp, result, error_message"
        " FROM schedule_events_pre_fk_fix"
    )
    conn.execute("DROP TABLE schedule_events_pre_fk_fix")
    conn.commit()


def _migrate_stale_schedules_schema(conn: sqlite3.Connection) -> None:

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='schedules'"
    ).fetchone()
    if row is None or "ON DELETE CASCADE" in row[0]:
        return
    conn.execute("ALTER TABLE schedules RENAME TO schedules_pre_cascade_fix")
    conn.execute(
        "CREATE TABLE schedules ("
        " instance_name TEXT PRIMARY KEY REFERENCES instances(name) ON DELETE CASCADE,"
        " timezone TEXT NOT NULL,"
        " rules TEXT NOT NULL DEFAULT '[]',"
        " enabled INTEGER NOT NULL DEFAULT 1"
        ")"
    )
    conn.execute(
        "INSERT INTO schedules (instance_name, timezone, rules, enabled)"
        " SELECT instance_name, timezone, rules, enabled FROM schedules_pre_cascade_fix"
    )
    conn.execute("DROP TABLE schedules_pre_cascade_fix")
    conn.commit()


def _migrate_stale_schedule_groups_schema(conn: sqlite3.Connection) -> None:

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='schedule_groups'"
    ).fetchone()
    if row is None or "UNIQUE" in row[0]:
        return


    conn.execute(
        "CREATE TABLE schedule_groups_unique_fix_new ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " name TEXT NOT NULL UNIQUE,"
        " timezone TEXT NOT NULL,"
        " rules TEXT NOT NULL DEFAULT '[]',"
        " enabled INTEGER NOT NULL DEFAULT 1"
        ")"
    )
    conn.execute(
        "INSERT INTO schedule_groups_unique_fix_new (id, name, timezone, rules, enabled)"
        " SELECT id, name, timezone, rules, enabled FROM schedule_groups"
    )
    conn.execute("DROP TABLE schedule_groups")
    conn.execute("ALTER TABLE schedule_groups_unique_fix_new RENAME TO schedule_groups")
    conn.commit()


def _migrate_stale_instances_group_id_fk(conn: sqlite3.Connection) -> None:

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='instances'"
    ).fetchone()


    if row is None or "schedule_groups_pre_unique_fix" not in row[0]:
        return
    conn.execute(
        "CREATE TABLE instances_fk_fix_new ("
        " name TEXT PRIMARY KEY,"
        " label TEXT,"
        " region TEXT,"
        " network_interface_model TEXT"
        "     CHECK (network_interface_model IS NULL OR network_interface_model IN"
        "         ('legacy_config', 'linode')),"
        " network_config TEXT,"
        " network_helper_enabled INTEGER,"
        " vpc_prefix INTEGER,"
        " os_volume_id INTEGER,"
        " data_volumes TEXT,"
        " reserved_ip TEXT,"
        " authorized_keys TEXT,"
        " tags TEXT,"
        " instance_attrs TEXT,"
        " group_id INTEGER REFERENCES schedule_groups(id),"
        " current_linode_id INTEGER,"
        " current_status TEXT"
        "     CHECK (current_status IS NULL OR current_status IN"
        "         ('running', 'stopped', 'unreachable', 'needs_manual_recovery')),"
        " transitioning INTEGER NOT NULL DEFAULT 0,"
        " manual_override_expires_at TIMESTAMP"
        ")"
    )
    conn.execute(
        f"INSERT INTO instances_fk_fix_new ({_INSTANCE_COLUMNS})"
        f" SELECT {_INSTANCE_COLUMNS} FROM instances"
    )
    conn.execute("DROP TABLE instances")
    conn.execute("ALTER TABLE instances_fk_fix_new RENAME TO instances")
    conn.commit()


def _migrate_add_manual_override_column(conn: sqlite3.Connection) -> None:

    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='instances'"
    ).fetchone()
    if row is None:
        return
    columns = {r[1] for r in conn.execute("PRAGMA table_info(instances)").fetchall()}
    if "manual_override_expires_at" in columns:
        return
    conn.execute("ALTER TABLE instances ADD COLUMN manual_override_expires_at TIMESTAMP")
    conn.commit()


def _migrate_add_schedule_events_actor_column(conn: sqlite3.Connection) -> None:

    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schedule_events'"
    ).fetchone()
    if row is None:
        return
    columns = {r[1] for r in conn.execute("PRAGMA table_info(schedule_events)").fetchall()}
    if "actor" in columns:
        return
    conn.execute("ALTER TABLE schedule_events ADD COLUMN actor TEXT")
    conn.commit()


def _connect() -> sqlite3.Connection:

    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(REGISTRY_PATH, timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        _migrate_stale_schedule_events_schema(conn)
        _migrate_add_schedule_events_actor_column(conn)
        _migrate_stale_schedules_schema(conn)
        _migrate_stale_schedule_groups_schema(conn)
        _migrate_add_manual_override_column(conn)
        _migrate_stale_instances_group_id_fk(conn)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA_PATH.read_text())
    except sqlite3.DatabaseError as e:
        conn.close()
        if isinstance(e, sqlite3.OperationalError):
            raise
        raise engine.ConfigError(
            f"the local registry database at {REGISTRY_PATH} appears corrupted or unreadable "
            f"({e}). Recovering local records from Linode's own tags needs a working, even if "
            f"empty, local database first -- move the corrupted file aside (e.g. `mv "
            f"{REGISTRY_PATH} {REGISTRY_PATH}.corrupted`) and re-run this command; a fresh, "
            f"empty database will be created automatically, and `rebuild` can then reconstruct "
            f"every managed instance's record from its Linode tags."
        ) from e
    return conn


def _retry_db(fn, attempts=5, delay_s=0.2):

    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            transient = "locked" in str(e).lower() or "busy" in str(e).lower()
            if not transient or attempt == attempts - 1:
                raise
            time.sleep(delay_s)


_NULLABLE_JSON_FIELDS = ("network_config", "authorized_keys", "tags", "instance_attrs")


def _record_from_row(row: tuple) -> dict:

    (
        _name, label, region, network_interface_model, network_config,
        network_helper_enabled, vpc_prefix, os_volume_id, data_volumes, reserved_ip,
        authorized_keys, tags, instance_attrs, group_id, current_linode_id, current_status,
        transitioning, manual_override_expires_at,
    ) = row
    raw = {
        "network_config": network_config,
        "authorized_keys": authorized_keys,
        "tags": tags,
        "instance_attrs": instance_attrs,
    }
    return {
        "label": label,
        "region": region,
        "network_interface_model": network_interface_model,
        **{k: (None if raw[k] is None else json.loads(raw[k])) for k in _NULLABLE_JSON_FIELDS},
        "network_helper_enabled": (
            None if network_helper_enabled is None else bool(network_helper_enabled)
        ),
        "vpc_prefix": vpc_prefix,
        "os_volume_id": os_volume_id,
        "data_volumes": None if data_volumes is None else json.loads(data_volumes),
        "reserved_ip": reserved_ip,
        "group_id": group_id,
        "current_linode_id": current_linode_id,
        "current_status": current_status,
        "transitioning": bool(transitioning),
        "manual_override_expires_at": manual_override_expires_at,
    }


_DEFAULT_RECORD: dict = {
    "label": "",
    "region": "",
    "network_interface_model": "legacy_config",
    "network_helper_enabled": None,
    "vpc_prefix": None,
    "os_volume_id": 0,
    "data_volumes": [],
    "reserved_ip": "",
    "group_id": None,
    "current_linode_id": None,
    "current_status": "stopped",
    "transitioning": False,
    "manual_override_expires_at": None,
}


def _sqlite_safe(value):

    if value is None or isinstance(value, (int, float, str, bytes)):
        return value
    return str(value)


def _row_values(name: str, record: dict) -> tuple:

    merged = {**_DEFAULT_RECORD, **record}
    network_helper_enabled = merged["network_helper_enabled"]
    nullable_json = {
        k: (None if k not in record else json.dumps(record[k], default=str))
        for k in _NULLABLE_JSON_FIELDS
    }
    return (
        name,
        _sqlite_safe(merged["label"]),
        _sqlite_safe(merged["region"]),
        _sqlite_safe(merged["network_interface_model"]),
        nullable_json["network_config"],
        None if network_helper_enabled is None else int(bool(network_helper_enabled)),
        _sqlite_safe(merged["vpc_prefix"]),
        _sqlite_safe(merged["os_volume_id"]),
        json.dumps(merged["data_volumes"], default=str),
        _sqlite_safe(merged["reserved_ip"]),
        nullable_json["authorized_keys"],
        nullable_json["tags"],
        nullable_json["instance_attrs"],
        _sqlite_safe(merged["group_id"]),
        _sqlite_safe(merged["current_linode_id"]),
        _sqlite_safe(merged["current_status"]),
        int(bool(merged["transitioning"])),
        _sqlite_safe(merged["manual_override_expires_at"]),
    )


def load_registry() -> dict:
    def _do():
        conn = _connect()
        try:
            return conn.execute(f"SELECT {_INSTANCE_COLUMNS} FROM instances").fetchall()
        finally:
            conn.close()

    rows = _retry_db(_do)
    return {row[0]: _record_from_row(row) for row in rows}


def save_registry(registry: dict) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute("DELETE FROM instances")
            for name, record in registry.items():
                conn.execute(
                    f"INSERT INTO instances ({_INSTANCE_COLUMNS})"
                    f" VALUES ({_INSTANCE_PLACEHOLDERS})",
                    _row_values(name, record),
                )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def _save_one_record(name: str, record: dict) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute(
                f"INSERT INTO instances ({_INSTANCE_COLUMNS}) VALUES ({_INSTANCE_PLACEHOLDERS}) "
                f"ON CONFLICT(name) DO UPDATE SET {_INSTANCE_UPDATE_CLAUSE}",
                _row_values(name, record),
            )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def _delete_one_record(name: str) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute("DELETE FROM instances WHERE name = ?", (name,))
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


@contextmanager
def _instance_lock(name: str):

    locks_dir = REGISTRY_PATH.parent / "locks"


    reached_open = False
    try:


        locks_dir.mkdir(parents=True, exist_ok=True)
        lock_path = locks_dir / f"{name}.lock"
        reached_open = True
        lock_file = open(lock_path, "w")


    except (OSError, ValueError) as e:


        if reached_open and (isinstance(e, ValueError) or e.errno == errno.ENAMETOOLONG):
            raise engine.ConfigError(f"'{name}' is not a usable instance name: {e}") from e
        raise engine.ConfigError(
            f"could not create the lock file for '{name}' ({e}) -- this is a local filesystem "
            f"problem (permissions, disk space, or similar under {locks_dir}), not necessarily "
            "anything wrong with the name itself."
        ) from e
    with lock_file as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstanceLockedError(
                f"Another start/stop/rebuild/clear-lock process is already operating on "
                f"'{name}' right now. Wait for it to finish and try again."
            ) from None
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


_MIGRATIONS_COLUMNS = "name, phase, checkpoint, updated_at"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def load_migrations() -> dict:
    def _do():
        conn = _connect()
        try:
            return conn.execute("SELECT name, checkpoint FROM migrations").fetchall()
        finally:
            conn.close()

    rows = _retry_db(_do)
    return {name: json.loads(checkpoint) for name, checkpoint in rows}


def save_migrations(migrations: dict) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute("DELETE FROM migrations")
            for name, state in migrations.items():
                conn.execute(
                    f"INSERT INTO migrations ({_MIGRATIONS_COLUMNS}) VALUES (?, ?, ?, ?)",
                    (
                        name, _sqlite_safe(state.get("phase")),
                        json.dumps(state, default=str), _now(),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def _save_one_migration(name: str, state: dict) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO migrations (name, phase, checkpoint, updated_at)"
                " VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET phase = excluded.phase, "
                "checkpoint = excluded.checkpoint, updated_at = excluded.updated_at",
                (
                    name, _sqlite_safe(state.get("phase")),
                    json.dumps(state, default=str), _now(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def _delete_one_migration(name: str) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute("DELETE FROM migrations WHERE name = ?", (name,))
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def _record_schedule_event(
    name: str, action: Literal["create", "delete"],
    triggered_by: Literal["schedule", "manual", "api"],
    result: Literal["success", "failure"], error_message: str | None = None,
    actor: str | None = None,
) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO schedule_events"
                " (instance_name, action, triggered_by, timestamp, result, error_message, actor)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, action, triggered_by, _now(), result, error_message, actor),
            )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


_START_EVENT_RESULTS: dict[str, Literal["success", "failure"]] = {
    "started": "success",
    "reachability_check_failed": "failure",
    "create_failed": "failure",
}
_STOP_EVENT_RESULTS: dict[str, Literal["success", "failure"]] = {
    "stopped": "success",
    "confirmed_gone_reset_to_stopped": "success",
    "delete_failed": "failure",
    "prepare_failed": "failure",
}


def _record_event(
    name: str, result: StartResult | StopResult, action: Literal["create", "delete"],
    outcome_map: dict[str, Literal["success", "failure"]],
    triggered_by: Literal["schedule", "manual", "api"],
    on_warning: Callable[[str], None] | None, actor: str | None,
) -> None:

    event_result = outcome_map.get(result.outcome)
    if event_result is None:
        return
    try:
        _record_schedule_event(name, action, triggered_by, event_result, result.detail, actor)
    except Exception as e:


        if on_warning is not None:
            on_warning(f"  WARNING: could not record audit event for '{name}': {e}")


def _record_start_event(
    name: str, result: StartResult, triggered_by: Literal["schedule", "manual", "api"],
    on_warning: Callable[[str], None] | None, actor: str | None = None,
) -> None:
    _record_event(name, result, "create", _START_EVENT_RESULTS, triggered_by, on_warning, actor)


def _record_stop_event(
    name: str, result: StopResult, triggered_by: Literal["schedule", "manual", "api"],
    on_warning: Callable[[str], None] | None, actor: str | None = None,
) -> None:
    _record_event(name, result, "delete", _STOP_EVENT_RESULTS, triggered_by, on_warning, actor)


_MAX_HISTORY_LIMIT = 10_000


def get_schedule_events(name: str, limit: int = 50) -> list[dict]:

    if limit < 0:
        raise engine.ConfigError(f"limit must be 0 or greater, got {limit}.")
    if limit > _MAX_HISTORY_LIMIT:
        raise engine.ConfigError(f"limit must be {_MAX_HISTORY_LIMIT} or less, got {limit}.")

    def _do():
        conn = _connect()
        try:
            return conn.execute(
                "SELECT action, triggered_by, timestamp, result, error_message, actor"
                " FROM schedule_events WHERE instance_name = ? ORDER BY timestamp DESC, id DESC"
                " LIMIT ?",
                (name, limit),
            ).fetchall()
        finally:
            conn.close()

    rows = _retry_db(_do)
    return [
        {
            "action": r[0], "triggered_by": r[1], "timestamp": r[2], "result": r[3],
            "error_message": r[4], "actor": r[5],
        }
        for r in rows
    ]


def get_schedule_events_since(name: str, since: datetime) -> list[dict]:

    def _do():
        conn = _connect()
        try:
            return conn.execute(
                "SELECT action, triggered_by, timestamp, result, error_message"
                " FROM schedule_events WHERE instance_name = ? AND timestamp >= ?"
                " ORDER BY timestamp ASC, id ASC",
                (name, since.isoformat()),
            ).fetchall()
        finally:
            conn.close()

    rows = _retry_db(_do)
    return [
        {"action": r[0], "triggered_by": r[1], "timestamp": r[2], "result": r[3], "error_message": r[4]}
        for r in rows
    ]


def event_already_succeeded_today(
    name: str, action: Literal["create", "delete"], tz: str = "UTC",
) -> bool:

    zone = ZoneInfo(tz)
    today = datetime.now(zone).date()
    for event in get_schedule_events(name, limit=200):
        if event["result"] != "success" or event["action"] not in ("create", "delete"):
            continue
        if event["action"] != action:
            return False
        return datetime.fromisoformat(event["timestamp"]).astimezone(zone).date() == today
    return False


def load_orphan_archive() -> dict:

    def _do():
        conn = _connect()
        try:
            return conn.execute(
                "SELECT name, detail FROM orphaned_migration_attempts ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    rows = _retry_db(_do)
    archive: dict = {}
    for name, detail in rows:
        archive.setdefault(name, []).append(json.loads(detail))
    return archive


def save_orphan_archive(archive: dict) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute("DELETE FROM orphaned_migration_attempts")
            now = _now()
            for name, attempts in archive.items():
                for attempt in attempts:
                    conn.execute(
                        "INSERT INTO orphaned_migration_attempts"
                        " (name, dest_volume_id, archived_at, detail) VALUES (?, ?, ?, ?)",
                        (
                            name, _sqlite_safe(attempt.get("dest_volume_id")), now,
                            json.dumps(attempt, default=str),
                        ),
                    )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def _archive_previous_attempts(
    client, name: str, previous_attempts: list,
    on_warning: Callable[[str], None] | None = None,
) -> None:

    if not previous_attempts:
        return


    def _do():
        conn = _connect()
        try:
            existing_ids = {
                row[0] for row in conn.execute(
                    "SELECT dest_volume_id FROM orphaned_migration_attempts WHERE name = ?",
                    (name,),
                ).fetchall()
            }
            new_attempts = [
                a for a in previous_attempts if a.get("dest_volume_id") not in existing_ids
            ]
            now = _now()
            for attempt in new_attempts:
                conn.execute(
                    "INSERT INTO orphaned_migration_attempts"
                    " (name, dest_volume_id, archived_at, detail) VALUES (?, ?, ?, ?)",
                    (
                        name, _sqlite_safe(attempt.get("dest_volume_id")), now,
                        json.dumps(attempt, default=str),
                    ),
                )
            conn.commit()
            return new_attempts
        finally:
            conn.close()

    new_attempts = _retry_db(_do)

    for attempt in new_attempts:
        vol_id = attempt.get("dest_volume_id")
        if vol_id is None:
            continue
        try:
            engine.tag_orphaned_migration_volume(client, vol_id, name)
        except (ApiError, engine.ConfigError, requests.exceptions.RequestException) as e:


            if on_warning is not None:
                on_warning(
                    f"  WARNING: could not tag orphaned volume {vol_id} for disaster "
                    f"recovery ({e}) -- it's still recorded in the local archive."
                )


@dataclass
class MigrateStartResult:

    outcome: Literal["started", "identity_change_declined"]
    instance_id: int | None = None
    dest_volume_id: int | None = None
    dest_volume_size_gb: int | None = None
    local_disk_size_mb: int | None = None


def _append_authorized_key_command(public_key: str) -> str:

    return (
        "(tail -c1 /root/.ssh/authorized_keys 2>/dev/null | read -r _ || "
        "printf '\\n' >> /root/.ssh/authorized_keys); "
        f"echo {shlex.quote(public_key)} >> /root/.ssh/authorized_keys"
    )


def migrate_start_instance(
    client, name: str, instance_id: int, ssh_key: str | None, *,
    force: bool = False,
    ssh_password: str | None = None,
    install_public_key: str | None = None,
    confirm_identity_change: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> MigrateStartResult:

    with _instance_lock(name):
        migrations = load_migrations()
        existing = migrations.get(name)
        if existing and not force:
            raise engine.ConfigError(
                f"a migration for '{name}' is already in progress "
                f"(phase={existing.get('phase')!r}). Use --force to restart it."
            )


        previous_attempts = []
        old_dest_volume_id = None
        if existing:
            previous_attempts = list(existing.get("previous_attempts", [])) + [
                {k: v for k, v in existing.items() if k != "previous_attempts"}
            ]
            old_dest_volume_id = existing.get("dest_volume_id")


            old_instance_id = existing.get("instance_id")
            if old_instance_id is not None and old_instance_id != instance_id:
                if on_progress is not None:


                    on_progress(
                        f"WARNING: --force restarting '{name}' at a DIFFERENT --instance-id "
                        "than the in-progress attempt being replaced:\n"
                        f"  instance id: {old_instance_id} -> {instance_id}\n"
                        "If this is unexpected, double-check --instance-id -- a typo here "
                        f"would abandon the real in-progress migration for '{name}' and start "
                        "a new one against unrelated infrastructure instead."
                    )
                if confirm_identity_change is not None and not confirm_identity_change():
                    return MigrateStartResult(outcome="identity_change_declined")

        try:


            instance = engine.retry_transient(lambda: client.load(Instance, instance_id))
        except (ApiError, requests.exceptions.RequestException) as e:


            raise engine.ConfigError(f"instance {instance_id} not found: {e}") from e

        try:
            engine.check_region_capabilities(client, instance.region.id)
        except engine.ConfigError as e:
            raise engine.ConfigError(
                f"instance {instance_id} is in an unsupported region: {e} This tool requires "
                "Block Storage (the OS must move onto a volume) -- reserved IPs are permanently "
                "locked to the region they were first reserved in, so a migration can't move an "
                "instance to a region that doesn't support it."
            ) from e
        except (ApiError, requests.exceptions.RequestException) as e:


            raise engine.ConfigError(
                f"could not check region capabilities for instance {instance_id}: {e}"
            ) from e

        if instance.status != "running":
            raise engine.ConfigError(
                f"instance {instance_id} is not running (status={instance.status!r}) -- must "
                "be running for pre-flight checks."
            )
        if not instance.ipv4:
            raise engine.ConfigError("instance has no public IPv4 address.")
        host = instance.ipv4[0]

        if on_progress is not None:
            on_progress("Running pre-flight checks (cloud-init version, datasource, networking)...")
        try:
            preflight = engine.check_path_b_preflight(host, ssh_key, password=ssh_password)
        except (engine.ConfigError, ApiError, RuntimeError) as e:
            raise engine.ConfigError(f"pre-flight checks failed: {e}") from e
        if on_progress is not None:
            on_progress(f"  cloud-init: {preflight['cloud_init_version']} "
                        f"({'OK' if preflight['cloud_init_ok'] else 'TOO OLD, need >= 23.3.1'})")
            on_progress(f"  datasource: {preflight['datasource']} "
                        f"({'OK' if preflight['datasource_ok'] else 'NOT akamai'})")
        if not preflight["cloud_init_ok"] or not preflight["datasource_ok"]:


            reasons = []
            if not preflight["cloud_init_ok"]:
                reasons.append(
                    f"cloud-init is {preflight['cloud_init_version']!r} (need >= 23.3.1) -- "
                    f"upgrade cloud-init on this instance, then retry. "
                    f"{engine.CLOUD_INIT_UPGRADE_INSTRUCTIONS}"
                )
            if not preflight["datasource_ok"]:
                reasons.append(
                    f"the datasource is {preflight['datasource']!r}, not 'akamai' -- this is "
                    "not a Linode/Akamai-managed cloud-init datasource, and there's no proven "
                    "fallback for this tool's network fix without it"
                )
            raise engine.ConfigError(
                "this instance does not meet the requirements for automated migration: "
                + "; ".join(reasons) + ". Not supported for now."
            )


        try:
            configs = list(instance.configs)
        except (ApiError, requests.exceptions.RequestException) as e:
            raise engine.ConfigError(f"could not read the instance's boot configs: {e}") from e
        if len(configs) != 1:
            raise engine.ConfigError(
                f"Instance {instance.id} has {len(configs)} boot configs -- can't tell which "
                "one is the real one to migrate from (Linode's API has no such field) and "
                "refuses to guess. Delete or consolidate the extra config(s) manually first "
                "(Cloud Manager), then retry."
            )


        devices = configs[0].devices.dict
        sda = devices.get("sda")
        if not sda:
            raise engine.ConfigError(f"Instance {instance.id} has no device at /dev/sda.")
        if sda.get("filesystem_path"):
            raise engine.ConfigError(
                f"Instance {instance.id}'s /dev/sda is already a Block Storage volume -- "
                "nothing to migrate (Path B is for local-disk instances only)."
            )
        disk_id = sda.get("id")
        if disk_id is None:
            raise engine.ConfigError(f"Instance {instance.id}'s /dev/sda has no disk id.")
        try:
            disks = list(instance.disks)
        except (ApiError, requests.exceptions.RequestException) as e:
            raise engine.ConfigError(f"could not read the instance's disks: {e}") from e
        if not any(d.id == disk_id for d in disks):
            raise engine.ConfigError(f"Could not find disk {disk_id} on instance {instance.id}.")

        if install_public_key is not None:


            if on_progress is not None:
                on_progress(
                    "Installing this deployment's own SSH key so the rest of this migration "
                    "(and onboarding afterward) don't need the one-time credential again..."
                )
            try:
                current_keys = engine.ssh_run(
                    host, ssh_key, "cat /root/.ssh/authorized_keys 2>/dev/null", password=ssh_password,
                )
            except (ApiError, RuntimeError, requests.exceptions.RequestException) as e:
                raise engine.ConfigError(f"could not read authorized_keys: {e}") from e
            if install_public_key not in current_keys:
                try:
                    engine.ssh_run(
                        host, ssh_key, _append_authorized_key_command(install_public_key),
                        password=ssh_password,
                    )
                except (ApiError, RuntimeError, requests.exceptions.RequestException) as e:
                    raise engine.ConfigError(f"could not install this deployment's key: {e}") from e
        if on_progress is not None:
            if preflight["hand_configured_networking"]:


                suspect_lines = "\n".join(
                    f"    - {suspect['path']}: {suspect['reason']}"
                    for suspect in preflight["hand_configured_networking"]
                )
                on_progress(
                    "  WARNING: hand-configured networking found outside Network Helper:\n"
                    f"{suspect_lines}\n"
                    "  This will be overwritten by the network fix once this instance "
                    "is onboarded and recreated. Continuing, since this is "
                    "informational, not a hard block -- review before going further "
                    "if that's a problem."
                )
            else:
                on_progress("  networking: clean (no hand-configured files found)")


            if existing:
                on_progress(
                    f"  NOTE: '{name}' had an incomplete migration attempt "
                    f"(phase={existing.get('phase')!r}"
                    + (f", dest_volume_id={existing['dest_volume_id']!r}"
                       if "dest_volume_id" in existing else "")
                    + "). That volume is NOT deleted automatically -- check Cloud Manager and "
                    "clean it up manually if it's no longer needed. The old attempt's IDs are "
                    f"preserved under '{name}' in the local registry database, not discarded."
                )

            on_progress(f"Starting migration to Block Storage for '{name}' (instance {instance.id})...")

        old_volume_retag_done = False

        def _persist_migration_checkpoint(partial_state: dict) -> None:


            nonlocal old_volume_retag_done
            partial_state["name"] = name
            if previous_attempts:
                partial_state["previous_attempts"] = previous_attempts
            _save_one_migration(name, partial_state)


            if (
                old_dest_volume_id is not None
                and not old_volume_retag_done
                and partial_state.get("dest_volume_id") is not None
            ):


                try:
                    engine.retag_migration_volume_as_orphaned(client, old_dest_volume_id, name)
                    old_volume_retag_done = True
                except (ApiError, engine.ConfigError, requests.exceptions.RequestException) as e:


                    if on_warning is not None:
                        on_warning(f"  WARNING: could not re-tag orphaned volume "
                                   f"{old_dest_volume_id} ({e}) -- will retry on the next "
                                   "checkpoint; still recorded in previous_attempts above.")

        try:
            migration_state = engine.start_path_b_migration(
                client, instance, name=name, persist_fn=_persist_migration_checkpoint
            )
        except (ApiError, RuntimeError, TimeoutError, requests.exceptions.RequestException) as e:
            raise engine.ConfigError(
                f"migration setup failed partway through: {e}. Whatever succeeded so far "
                f"(possibly including a billable destination volume) was saved to the local "
                f"registry database under '{name}' -- check there and in Cloud Manager before "
                "retrying with --force."
            ) from e
        migration_state["name"] = name
        if previous_attempts:
            migration_state["previous_attempts"] = previous_attempts
        if old_dest_volume_id is not None:
            migration_state["replaced_dest_volume_id"] = old_dest_volume_id
        _save_one_migration(name, migration_state)


        if old_dest_volume_id is not None and not old_volume_retag_done:
            try:
                engine.retag_migration_volume_as_orphaned(client, old_dest_volume_id, name)
            except (ApiError, engine.ConfigError, requests.exceptions.RequestException) as e:
                if on_warning is not None:
                    on_warning(f"  WARNING: could not re-tag orphaned volume "
                               f"{old_dest_volume_id} ({e}) -- it's still recorded in "
                               "previous_attempts above.")

        if not migration_state.get("remote_tagged", True) and on_warning is not None:
            on_warning(f"  WARNING: could not remotely tag destination volume "
                       f"{migration_state['dest_volume_id']} as an active migration attempt -- "
                       "it's still recorded locally in the registry database, but won't be "
                       "discoverable by tag if that local state is lost before this migration "
                       "completes.")

        return MigrateStartResult(
            outcome="started",
            instance_id=instance.id,
            dest_volume_id=migration_state["dest_volume_id"],
            dest_volume_size_gb=migration_state["dest_volume_size_gb"],
            local_disk_size_mb=migration_state["local_disk_size_mb"],
        )


def cmd_migrate_start(client, args) -> int:
    def _confirm_different_instance_id() -> bool:
        answer = input(f"Type '{args.name}' to confirm this is intentional: ")
        return answer == args.name

    try:
        result = migrate_start_instance(
            client, args.name, args.instance_id, args.ssh_key,
            force=args.force,
            confirm_identity_change=None if args.yes else _confirm_different_instance_id,
            on_progress=print, on_warning=_print_to_stderr,
        )
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result.outcome == "identity_change_declined":
        print("Aborted -- name didn't match.")
        return 1

    print()
    print("=" * 70)
    print(f"Destination volume created: {result.dest_volume_id} "
          f"({result.dest_volume_size_gb}GB)")
    print(f"Instance {result.instance_id} is now booted into Rescue Mode:")
    print("  /dev/sda = the original local disk (source)")
    print("  /dev/sdb = the new destination volume")
    print()
    print("YOUR TURN -- this is the one manual step in the whole process:")
    print(f"  1. In Cloud Manager, open instance {result.instance_id} and click "
          "\"Launch LISH Console\".")
    print("  2. Log in as root.")


    print("  3. Run `lsblk` FIRST and match devices by size -- do NOT trust /dev/sda/sdb below "
          "blindly:")
    print(f"       source (original disk) should be ~{result.local_disk_size_mb}MB")
    print(f"       destination (new volume) should be ~{result.dest_volume_size_gb}GB")
    print("     If lsblk shows a different device for either size, substitute the correct "
          "device names into the command below instead of running it as printed.")
    print("  4. Once confirmed, run:")
    print()
    print("       dd if=/dev/sda of=/dev/sdb bs=4M status=progress && sync")
    print()
    print("  5. Once it finishes cleanly (no I/O errors), run:")
    print(f"       instance_manager.py migrate-resume --name {args.name}")
    print("=" * 70)
    return 0


@dataclass
class MigrateResumeResult:

    outcome: Literal["resumed", "incomplete"]
    instance_id: int | None = None
    os_volume_id: int | None = None
    reserved_ip: str | None = None
    previous_attempts_count: int = 0
    detail: str | None = None


    fstab_entries_disabled: list[str] = field(default_factory=list)


def migrate_resume_instance(
    client, name: str, ssh_key: str, *,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> MigrateResumeResult:

    with _instance_lock(name):
        migrations = load_migrations()
        migration_state = migrations.get(name)
        if migration_state is None:
            raise engine.ConfigError(
                f"no migration in progress for '{name}'. Run migrate-start first."
            )


        if migration_state.get("phase") != "awaiting_manual_dd":
            raise engine.ConfigError(
                f"'{name}' is not ready for migrate-resume -- its checkpoint is at phase "
                f"{migration_state.get('phase')!r}, not the expected 'awaiting_manual_dd'. "
                "This means the manual `dd` + `sync` step (or the rescue-mode request itself) "
                "was never confirmed to complete -- resuming now could build a boot config from "
                "an empty or partially-copied destination volume. If dd + sync is genuinely "
                "done and this is just stale bookkeeping, or you want to abandon this attempt "
                f"and start over, re-run `migrate-start --name {name} --instance-id <id> "
                "--force` instead."
            )

        if on_progress is not None:
            on_progress(f"Resuming migration for '{name}'...")

        def _persist_resume_checkpoint(partial_state: dict) -> None:


            _save_one_migration(name, partial_state)

        try:
            result = engine.resume_path_b_migration(
                client, migration_state, ssh_key_path=ssh_key,
                persist_fn=_persist_resume_checkpoint, on_progress=on_progress,
            )
        except (
            ApiError, RuntimeError, TimeoutError, engine.ConfigError,
            requests.exceptions.RequestException,
        ) as e:
            raise engine.ConfigError(
                f"resuming the migration failed: {e}. The migration checkpoint for '{name}' "
                "was left in place -- fix the underlying issue and re-run migrate-resume."
            ) from e


        previous_attempts = migration_state.get("previous_attempts") or []
        try:
            _archive_previous_attempts(client, name, previous_attempts, on_warning=on_warning)


            _delete_one_migration(name)
        except Exception as e:


            return MigrateResumeResult(
                outcome="incomplete",
                instance_id=result["instance_id"],
                os_volume_id=result["os_volume_id"],
                reserved_ip=result["reserved_ip"],
                detail=str(e),
                fstab_entries_disabled=result.get("fstab_entries_disabled", []),
            )

        return MigrateResumeResult(
            outcome="resumed",
            instance_id=result["instance_id"],
            os_volume_id=result["os_volume_id"],
            reserved_ip=result["reserved_ip"],
            previous_attempts_count=len(previous_attempts),
            fstab_entries_disabled=result.get("fstab_entries_disabled", []),
        )


def cmd_migrate_resume(client, args) -> int:
    try:
        result = migrate_resume_instance(
            client, args.name, args.ssh_key, on_progress=print, on_warning=_print_to_stderr,
        )
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1

    if result.outcome == "incomplete":
        print(
            f"Configuration error: the migration for '{args.name}' completed successfully "
            f"(instance {result.instance_id}, now booting from volume "
            f"{result.os_volume_id}, reserved ip {result.reserved_ip}), but recording "
            f"that locally failed: {result.detail}. Do NOT re-run migrate-start -- the migration "
            "itself is done. Fix the underlying issue (disk space, or a corrupted registry "
            "database under state/), then finish onboarding directly: "
            f"instance_manager.py onboard --name {args.name} --instance-id {result.instance_id}",
            file=sys.stderr,
        )
        return 1

    if result.previous_attempts_count:
        print()
        print(f"NOTE: {result.previous_attempts_count} earlier incomplete migration attempt(s) "
              f"for '{args.name}' were replaced by --force before this one. Their resource IDs "
              f"(most importantly any dest_volume_id) are preserved locally under '{args.name}' "
              f"-- those volumes were NOT deleted automatically and are likely still billing. "
              f"Run `migrate-orphans --name {args.name}` to see them, and `--cleanup` to delete "
              "them once you're sure they're no longer needed.")
    print()
    print("=" * 70)
    print(f"Migration complete for '{args.name}':")
    print(f"  instance: {result.instance_id}")
    print(f"  now booting from volume: {result.os_volume_id}")
    print(f"  reserved ip: {result.reserved_ip}")
    if result.fstab_entries_disabled:
        print()
        print(f"  NOTE: {len(result.fstab_entries_disabled)} stale /etc/fstab entr"
              f"{'y' if len(result.fstab_entries_disabled) == 1 else 'ies'} disabled (device no "
              "longer present after migration -- would otherwise stall every future boot for "
              "systemd's ~90s device-wait timeout). Backed up before editing:")
        for line in result.fstab_entries_disabled:
            print(f"    {line}")
    print()
    print(f"Ready to onboard: instance_manager.py onboard --name {args.name} "
          f"--instance-id {result.instance_id}")
    print("=" * 70)
    return 0


def _migration_conflict_reason_text(c: dict) -> str:

    if c["reason"] == "multiple_names":
        return f"tagged for more than one name at once: {c['names']!r}"
    if c["reason"] == "multiple_roles":
        return f"carries both the active and orphaned migration role tags at once (names: {c['names']!r})"
    if c["reason"] == "managed_role_conflict":
        return (f"carries a migration role tag alongside a managed resource role {c['roles']!r} "
                f"(names: {c['names']!r}) -- this may be a live OS/data volume, not an abandoned attempt")
    return "carries a migration role tag but no scheduler name tag at all"


@dataclass
class MarkOrphanedResult:

    outcome: Literal["marked", "aborted_by_user"]
    volume_id: int | None = None


def mark_migration_volume_orphaned(
    client, name: str | None, volume_id: int | None, *,
    confirm: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> MarkOrphanedResult:

    if name is None or volume_id is None:
        raise engine.ConfigError("--mark-orphaned requires both --name and --volume-id.")

    with _instance_lock(name):


        migrations = load_migrations()
        for local_name, local_state in migrations.items():
            if local_state.get("dest_volume_id") == volume_id:
                raise engine.ConfigError(
                    f"volume {volume_id} is the CURRENT destination of a locally-known, "
                    f"resumable migration ('{local_name}', phase={local_state.get('phase')!r}) "
                    "-- refusing to mark it orphaned. This command is only for a volume whose "
                    f"local migration state is already gone. Resume the migration "
                    f"(`migrate-resume --name {local_name}`) or explicitly replace it "
                    f"(`migrate-start --name {local_name} --force`) instead."
                )

        try:
            tagged, conflicts = engine.find_migration_volumes_by_tag(client)
        except (ApiError, requests.exceptions.RequestException) as e:
            raise engine.ConfigError(f"could not scan remote tags: {e}") from e


        own_conflict = next((c for c in conflicts if c["id"] == volume_id), None)
        if own_conflict is not None:
            raise engine.ConfigError(
                f"volume {volume_id} has contradictory tags ({own_conflict['reason']}, "
                f"names={own_conflict['names']!r}) -- refusing to mark it orphaned until the "
                "tags are corrected by hand (Cloud Manager). Marking it now would guess at an "
                "ambiguous state instead of resolving it."
            )

        entry = tagged.get(name, {"active_migration_volume_ids": []})
        if volume_id not in entry["active_migration_volume_ids"]:
            raise engine.ConfigError(
                f"volume {volume_id} is not tagged as an active migration attempt for '{name}' "
                f"-- nothing to mark orphaned. Active volumes for '{name}': "
                f"{entry['active_migration_volume_ids']}."
            )

        if on_progress is not None:
            on_progress(f"This will mark volume {volume_id} (active migration attempt for "
                        f"'{name}') as orphaned. The volume itself is NOT deleted -- it becomes "
                        "eligible for `migrate-orphans --cleanup` afterward.")
        if confirm is not None and not confirm():
            return MarkOrphanedResult(outcome="aborted_by_user")

        try:
            engine.retag_migration_volume_as_orphaned(client, volume_id, name)
        except (ApiError, engine.ConfigError, requests.exceptions.RequestException) as e:
            raise engine.ConfigError(f"failed to retag volume {volume_id}: {e}") from e

        return MarkOrphanedResult(outcome="marked", volume_id=volume_id)


def _cmd_migrate_orphans_mark_orphaned(client, args) -> int:
    def _confirm() -> bool:
        answer = input(f"Type '{args.name}' to confirm: ")
        return answer == args.name

    try:
        result = mark_migration_volume_orphaned(
            client, args.name, args.volume_id,
            confirm=None if args.yes else _confirm,
            on_progress=print,
        )
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result.outcome == "aborted_by_user":
        print("Aborted -- name didn't match.")
        return 1
    print(f"Volume {result.volume_id} marked orphaned for '{args.name}'.")
    return 0


def _remove_resolved_orphan_attempts(name: str, resolved_volume_ids: set[int]) -> None:

    if not resolved_volume_ids:
        return

    def _do():
        conn = _connect()
        try:
            placeholders = ", ".join("?" * len(resolved_volume_ids))
            conn.execute(
                f"DELETE FROM orphaned_migration_attempts"
                f" WHERE name = ? AND dest_volume_id IN ({placeholders})",
                (name, *resolved_volume_ids),
            )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


@dataclass
class MigrateOrphansResult:

    outcome: Literal["listed", "nothing_found", "aborted_by_user", "cleanup_complete", "cleanup_partial"]
    ok: bool


def list_or_cleanup_migration_orphans(
    client, name: str | None, *,
    volume_id: int | None = None, cleanup: bool = False,
    confirm: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> MigrateOrphansResult:

    if cleanup and name is None:
        raise engine.ConfigError("--cleanup requires --name.")

    if cleanup:
        assert name is not None
        with _instance_lock(name):
            return _list_or_cleanup_migration_orphans_impl(
                client, name, volume_id=volume_id, cleanup=cleanup,
                confirm=confirm, on_progress=on_progress, on_warning=on_warning,
            )
    return _list_or_cleanup_migration_orphans_impl(
        client, name, volume_id=volume_id, cleanup=cleanup,
        confirm=confirm, on_progress=on_progress, on_warning=on_warning,
    )


def _list_or_cleanup_migration_orphans_impl(
    client, name: str | None, *, volume_id: int | None, cleanup: bool,
    confirm: Callable[[], bool] | None,
    on_progress: Callable[[str], None] | None,
    on_warning: Callable[[str], None] | None,
) -> MigrateOrphansResult:
    archive = load_orphan_archive()
    if name is not None:
        archive = {name: archive[name]} if name in archive else {}


    scan_failed = False
    try:
        tagged, conflicts = engine.find_migration_volumes_by_tag(client)
    except (ApiError, requests.exceptions.RequestException) as e:
        scan_failed = True
        if cleanup:
            raise engine.ConfigError(
                f"could not scan remote tags for migration volumes ({e}) -- refusing to "
                "proceed with --cleanup. The remote conflict check cannot be skipped at a "
                "permanent-delete boundary: an unknown scan result must never be treated as "
                "proof no conflicts exist."
            ) from e
        if on_warning is not None:
            on_warning(f"  WARNING: could not scan remote tags for migration volumes ({e}) -- "
                       "showing local archive only. This listing is INCOMPLETE -- it cannot "
                       "confirm no remote conflicts exist.")
        tagged, conflicts = {}, []
    if name is not None:
        tagged = {name: tagged[name]} if name in tagged else {}


    local_orphan_ids = {
        n: {a["dest_volume_id"] for a in attempts if a.get("dest_volume_id") is not None}
        for n, attempts in archive.items()
    }


    remote_only: dict[str, list[int]] = {}
    for n, entry in tagged.items():
        extra = sorted(set(entry["orphaned_migration_volume_ids"]) - local_orphan_ids.get(n, set()))
        if extra:
            remote_only[n] = extra

    if not cleanup:
        if not scan_failed and not archive and not remote_only and not conflicts and not any(
            entry["active_migration_volume_ids"] for entry in tagged.values()
        ):
            scope = f" for '{name}'" if name else ""
            if on_progress is not None:
                on_progress(f"No orphaned migration attempts recorded{scope}.")
            return MigrateOrphansResult(outcome="nothing_found", ok=True)
        if scan_failed and not archive and on_progress is not None:
            scope = f" for '{name}'" if name else ""
            on_progress(f"No orphaned migration attempts recorded in the local archive{scope} -- "
                        "but the remote scan failed, so this is NOT a confirmed complete picture.")
        if on_progress is not None:
            for n, attempts in archive.items():
                on_progress(f"'{n}':")
                for attempt in attempts:
                    on_progress(f"  dest_volume_id={attempt.get('dest_volume_id')}  "
                                f"phase={attempt.get('phase')!r}")
            for n, vol_ids in remote_only.items():
                on_progress(f"'{n}' (found via remote tags only -- not in local archive):")
                for vol_id in vol_ids:
                    on_progress(f"  dest_volume_id={vol_id}  phase=unknown (no local record)")
            for n, entry in tagged.items():
                if entry["active_migration_volume_ids"]:
                    on_progress(f"'{n}': also has active (not orphaned) migration volume(s) "
                                f"{entry['active_migration_volume_ids']} -- not eligible for "
                                "cleanup here.")
            if conflicts:
                on_progress(f"WARNING: {len(conflicts)} migration volume(s) have contradictory "
                            "tags and were excluded above entirely -- they are NOT eligible for "
                            "automated cleanup or retagging until resolved by hand:")
                for c in conflicts:
                    on_progress(f"  volume {c['id']}: {_migration_conflict_reason_text(c)}")
                on_progress("  Review tags directly in Cloud Manager and correct them by hand "
                            "-- do not delete the volume based on a guess.")
            on_progress("")
            on_progress("Run with --cleanup --name <name> to delete these volumes (optionally "
                        "--volume-id to target just one).")


        if name is not None:
            relevant_conflicts = [c for c in conflicts if name in c.get("names", [])]
        else:
            relevant_conflicts = conflicts
        return MigrateOrphansResult(outcome="listed", ok=not (relevant_conflicts or scan_failed))

    assert name is not None
    attempts = archive.get(name, [])
    if volume_id is not None:
        attempts = [a for a in attempts if a.get("dest_volume_id") == volume_id]
    volume_id_set = {a["dest_volume_id"] for a in attempts if a.get("dest_volume_id") is not None}
    remote_extra = set(remote_only.get(name, []))
    if volume_id is not None:
        remote_extra = {v for v in remote_extra if v == volume_id}
    volume_ids = sorted(volume_id_set | remote_extra)


    conflicted_ids = {c["id"] for c in conflicts}
    conflicted_overlap = sorted(set(volume_ids) & conflicted_ids)
    if conflicted_overlap:
        lines = [
            f"refusing to delete -- {len(conflicted_overlap)} of the requested volume(s) for "
            f"'{name}' have contradictory remote tags and are NOT eligible for automated "
            f"cleanup: {conflicted_overlap}. Full candidate set was {volume_ids} -- nothing was "
            "deleted, and the local archive is untouched."
        ]
        for c in conflicts:
            if c["id"] in conflicted_overlap:
                lines.append(f"  volume {c['id']}: {c['reason']} (names: {c['names']!r})")
        lines.append("  Review tags directly in Cloud Manager and correct them by hand, then "
                     "re-run cleanup.")
        raise engine.ConfigError("\n".join(lines))

    if not volume_ids:
        if volume_id is not None and volume_id in conflicted_ids:


            conflict = next(c for c in conflicts if c["id"] == volume_id)
            raise engine.ConfigError(
                f"volume {volume_id} is tag-conflicted, not simply orphaned, for '{name}': "
                f"{_migration_conflict_reason_text(conflict)}. Not eligible for automated "
                "cleanup -- review tags directly in Cloud Manager and correct them by hand."
            )


        if volume_id is None:
            name_conflicts = [c for c in conflicts if name in c.get("names", [])]
            if name_conflicts:
                lines = [
                    f"'{name}' has no plain orphaned volumes to clean up, but "
                    f"{len(name_conflicts)} volume(s) with contradictory remote tags naming it "
                    "were found -- not eligible for automated cleanup:"
                ]
                for c in name_conflicts:
                    lines.append(f"  volume {c['id']}: {_migration_conflict_reason_text(c)}")
                lines.append("  Review tags directly in Cloud Manager and correct them by hand.")
                raise engine.ConfigError("\n".join(lines))
        if on_progress is not None:
            on_progress(f"No matching orphaned attempts for '{name}'.")
        return MigrateOrphansResult(outcome="nothing_found", ok=True)

    if on_progress is not None:
        on_progress(f"This will PERMANENTLY DELETE {len(volume_ids)} orphaned volume(s) for "
                    f"'{name}': {volume_ids}")
    if confirm is not None and not confirm():
        return MigrateOrphansResult(outcome="aborted_by_user", ok=False)


    verification_problems: list[str] = []
    verified_volumes: dict[int, Volume] = {}
    already_gone: set[int] = set()
    for vol_id in volume_ids:
        try:
            problems, vol = engine.verify_migration_volume_orphaned_and_unmanaged(
                client, vol_id, name
            )
        except ApiError as e:
            if e.status == 404:
                already_gone.add(vol_id)
                continue
            verification_problems.append(f"volume {vol_id}: could not verify current tags ({e})")
            continue
        except requests.exceptions.RequestException as e:


            verification_problems.append(f"volume {vol_id}: could not verify current tags ({e})")
            continue
        if problems:
            verification_problems.append(f"volume {vol_id}: {'; '.join(problems)}")
        else:
            verified_volumes[vol_id] = vol
    if verification_problems:
        lines = [
            "refusing to delete -- pre-delete verification found "
            f"{len(verification_problems)} volume(s) whose current tags no longer confirm "
            "they're a genuinely safe, unmanaged orphan (state may have changed since the "
            "scan above, or the volume is actually still managed):",
        ]
        lines.extend(f"  {msg}" for msg in verification_problems)
        lines.append("  Nothing was deleted, and the local archive is untouched. Re-run "
                     "`migrate-orphans` to see current state.")
        raise engine.ConfigError("\n".join(lines))

    resolved: set[int] = set(already_gone)
    if on_progress is not None:
        for vol_id in already_gone:
            on_progress(f"  volume {vol_id}: already gone")
    for vol_id in verified_volumes:


        try:
            problems, fresh_vol = engine.verify_migration_volume_orphaned_and_unmanaged(
                client, vol_id, name
            )
        except ApiError as e:
            if e.status == 404:
                if on_progress is not None:
                    on_progress(f"  volume {vol_id}: already gone")
                resolved.add(vol_id)
            elif on_warning is not None:
                on_warning(f"  FAILED to re-verify volume {vol_id} immediately before delete: "
                           f"{e} -- not deleted, left in the archive.")
            continue
        except requests.exceptions.RequestException as e:


            if on_warning is not None:
                on_warning(f"  FAILED to re-verify volume {vol_id} immediately before delete: "
                           f"{e} -- not deleted, left in the archive.")
            continue
        if problems:
            if on_warning is not None:
                on_warning(f"  volume {vol_id}: tags changed since the batch preflight above "
                           f"({'; '.join(problems)}) -- NOT deleted, left in the archive.")
            continue
        try:


            engine.retry_transient(fresh_vol.delete)
            if on_progress is not None:
                on_progress(f"  deleted volume {vol_id}")
            resolved.add(vol_id)
        except ApiError as e:
            if e.status == 404:
                if on_progress is not None:
                    on_progress(f"  volume {vol_id}: already gone")
                resolved.add(vol_id)
            elif on_warning is not None:
                on_warning(f"  FAILED to delete volume {vol_id}: {e}")
        except Exception as e:
            if on_warning is not None:
                on_warning(f"  FAILED to delete volume {vol_id}: {e}")

    if resolved:
        _remove_resolved_orphan_attempts(name, resolved)
    remaining = len(volume_ids) - len(resolved)
    if remaining:
        if on_warning is not None:
            on_warning(f"{remaining} volume(s) could not be confirmed deleted -- left in the "
                       "archive, safe to re-run --cleanup.")
        return MigrateOrphansResult(outcome="cleanup_partial", ok=False)
    if on_progress is not None:
        on_progress(f"All matching orphaned attempts for '{name}' resolved.")
    return MigrateOrphansResult(outcome="cleanup_complete", ok=True)


def cmd_migrate_orphans(client, args) -> int:

    if args.mark_orphaned:
        return _cmd_migrate_orphans_mark_orphaned(client, args)
    return _cmd_migrate_orphans_list_or_cleanup(client, args)


def _cmd_migrate_orphans_list_or_cleanup(client, args) -> int:
    def _confirm() -> bool:
        answer = input(f"Type '{args.name}' to confirm: ")
        return answer == args.name

    try:
        result = list_or_cleanup_migration_orphans(
            client, args.name, volume_id=args.volume_id, cleanup=args.cleanup,
            confirm=None if args.yes else _confirm,
            on_progress=print, on_warning=_print_to_stderr,
        )
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result.outcome == "aborted_by_user":
        print("Aborted -- name didn't match.")
        return 1
    return 0 if result.ok else 1


def find_vpc_id_for_subnet(client, subnet_id: int) -> int | None:

    for vpc in client.vpcs():
        for subnet in vpc.subnets:
            if subnet.id == subnet_id:
                return vpc.id
    return None


def _derive_vpc_prefix(
    client, network_config: list, network_interface_model: str, *,
    vpc_id: int | None, cannot_determine: Callable[[str], Exception],
) -> int | None:

    for iface in network_config:
        if network_interface_model == engine.INTERFACE_MODEL_LEGACY:
            if iface.get("purpose") != "vpc":
                continue
            subnet_id = iface.get("subnet_id")
            resolved_vpc_id = vpc_id
            if resolved_vpc_id is None:
                resolved_vpc_id = find_vpc_id_for_subnet(client, subnet_id)
            if resolved_vpc_id is None:
                raise cannot_determine(
                    "has a VPC interface but its vpc_id couldn't be determined automatically "
                    "(legacy_config model doesn't capture it) and no VPC was found containing "
                    "that subnet."
                )
        else:
            if not iface.get("vpc"):
                continue
            subnet_id = iface["vpc"].get("subnet_id")
            resolved_vpc_id = iface["vpc"].get("vpc_id") or vpc_id
            if resolved_vpc_id is None:
                raise cannot_determine(
                    "has a VPC interface but its vpc_id couldn't be determined (missing from "
                    "the captured interface, unexpectedly)."
                )
        return engine.get_vpc_subnet_prefix(client, resolved_vpc_id, subnet_id)
    return None


@dataclass
class OnboardResult:

    outcome: Literal["onboarded", "identity_change_declined"]
    record: dict | None = None


def onboard_instance(
    client, name: str, instance_id: int, ssh_key: str | None, *,
    vpc_id: int | None = None, force: bool = False,
    ssh_password: str | None = None,
    install_public_key: str | None = None,
    confirm_identity_change: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> OnboardResult:

    with _instance_lock(name):


        registry = load_registry()
        if name in registry and not force:
            raise engine.AlreadyOnboardedError(name)

        try:


            instance = engine.retry_transient(lambda: client.load(Instance, instance_id))
        except (ApiError, requests.exceptions.RequestException) as e:


            raise engine.ConfigError(f"instance {instance_id} not found: {e}") from e

        try:
            engine.check_region_capabilities(client, instance.region.id)
        except engine.ConfigError as e:
            raise engine.ConfigError(
                f"instance {instance_id} is in an unsupported region: {e} This tool requires "
                "Block Storage (the OS lives on a volume, never local disk) -- reserved IPs are "
                "permanently locked to the region they were first reserved in, so onboarding "
                "can't move an instance to a region that doesn't support it."
            ) from e
        except (ApiError, requests.exceptions.RequestException) as e:


            raise engine.ConfigError(
                f"could not check region capabilities for instance {instance_id}: {e}"
            ) from e

        if instance.status != "running":
            raise engine.ConfigError(
                f"instance {instance_id} is not running (status={instance.status!r}) -- must "
                "be running to capture its network config and data volumes over SSH."
            )


        try:
            configs = list(instance.configs)
        except (ApiError, requests.exceptions.RequestException) as e:
            raise engine.ConfigError(f"could not read instance {instance_id}'s boot configs: {e}") from e
        if not configs:
            raise engine.ConfigError("instance has no boot config.")
        if len(configs) != 1:
            raise engine.ConfigError(
                f"instance {instance_id} has {len(configs)} boot configs -- this tool can't "
                "tell which one is actually active (Linode's API has no such field) and "
                "refuses to guess, since onboarding the wrong one would silently capture the "
                "wrong os_volume_id. Delete or consolidate the extra config(s) manually first "
                "(Cloud Manager), then retry."
            )
        devices = configs[0].devices.dict
        sda = devices.get("sda")


        if not sda or not sda.get("filesystem_path"):


            raise engine.NotMigratedError(instance_id)
        os_volume_id = sda["id"]

        if not instance.ipv4:
            raise engine.ConfigError("instance has no public IPv4 address.")
        reserved_ip = instance.ipv4[0]
        try:
            ip_info = engine.get_ip_details(client, reserved_ip)
        except (ApiError, requests.exceptions.RequestException) as e:


            raise engine.ConfigError(
                f"could not check whether {reserved_ip} is reserved: {e}"
            ) from e
        if not ip_info.get("reserved"):
            raise engine.IPNotReservedError(reserved_ip)


        old_record = registry.get(name)
        identity_changed = False
        if old_record is not None:
            identity_changed = (
                old_record.get("current_linode_id") != instance.id
                or old_record.get("os_volume_id") != os_volume_id
                or old_record.get("reserved_ip") != reserved_ip
                or old_record.get("region") != instance.region.id
            )
            if identity_changed:
                if on_progress is not None:


                    on_progress(
                        f"WARNING: re-onboarding '{name}' at a DIFFERENT resource identity "
                        "than what's currently on record:\n"
                        f"  instance id:  {old_record.get('current_linode_id')} -> {instance.id}\n"
                        f"  os_volume_id: {old_record.get('os_volume_id')} -> {os_volume_id}\n"
                        f"  reserved_ip:  {old_record.get('reserved_ip')} -> {reserved_ip}\n"
                        f"  region:       {old_record.get('region')} -> {instance.region.id}\n"
                        "If this is unexpected, double-check --instance-id -- a typo here "
                        f"would silently redirect '{name}' to unrelated infrastructure from "
                        "now on."
                    )
                if confirm_identity_change is not None and not confirm_identity_change():
                    return OnboardResult(outcome="identity_change_declined")


        try:


            captured = engine.capture_network_config(instance, configs=configs)

            vpc_prefix = _derive_vpc_prefix(
                client, captured["network_config"], captured["network_interface_model"],
                vpc_id=vpc_id,
                cannot_determine=lambda msg: _OnboardRefusal(
                    f"this instance {msg} Pass --vpc-id explicitly."
                ),
            )

            if on_progress is not None:
                on_progress("Capturing authorized_keys over SSH and data volumes over the API...")


            authorized_keys_raw = engine.ssh_run(
                reserved_ip, ssh_key, "cat /root/.ssh/authorized_keys 2>/dev/null",
                trust_new=True, password=ssh_password,
            )
            authorized_keys = [
                line.strip() for line in authorized_keys_raw.splitlines() if line.strip()
            ]


            needs_key_install = install_public_key is not None and install_public_key not in authorized_keys
            if needs_key_install:
                assert install_public_key is not None
                authorized_keys.append(install_public_key)
            if not authorized_keys:
                raise _OnboardRefusal(
                    "could not read any authorized_keys from the instance -- refusing to "
                    "onboard without knowing how future recreates will grant SSH access."
                )

            data_volumes = engine.capture_data_volumes(instance, os_volume_id, configs=configs)
            instance_attrs = engine.capture_instance_attributes(instance)
        except _OnboardRefusal as e:


            raise engine.ConfigError(str(e)) from e
        except (ApiError, RuntimeError, engine.ConfigError, requests.exceptions.RequestException) as e:


            raise engine.ConfigError(
                f"failed to capture '{name}' from instance {instance_id}: {e}"
            ) from e

        record = {
            "name": name,
            "region": instance.region.id,
            "os_volume_id": os_volume_id,
            "reserved_ip": reserved_ip,


            "group_id": registry.get(name, {}).get("group_id"),
            "network_interface_model": captured["network_interface_model"],
            "network_config": captured["network_config"],
            "network_helper_enabled": captured["network_helper_enabled"],
            "vpc_prefix": vpc_prefix,
            "data_volumes": data_volumes,
            "authorized_keys": authorized_keys,


            "label": instance.label,
            "tags": list(instance.tags),


            "instance_attrs": instance_attrs,
            "current_linode_id": instance.id,
            "current_status": "running",
            "transitioning": False,
        }
        tag_kwargs = {
            "os_volume_id": os_volume_id,
            "data_volume_ids": [dv["volume_id"] for dv in data_volumes],
            "reserved_ip": reserved_ip,
        }
        if on_progress is not None:
            on_progress("Tagging resources for disaster recovery...")
        try:
            engine.tag_managed_resources(client, name, **tag_kwargs)
        except engine.ResourceOwnershipConflict as e:


            raise engine.ConfigError(
                f"{e}\nOnboarding record was NOT saved. This name's disaster-recovery tags "
                "were never written, so `rebuild` cannot find this node if local state is lost."
            ) from e
        except engine.TagVerificationError as e:


            if on_warning is not None:
                on_warning(
                    f"  WARNING: could not tag resources for disaster recovery ({e}) -- "
                    "onboarding still succeeded, but `rebuild` won't find this node if local "
                    "state is lost. Safe to ignore or retry later."
                )
        except (ApiError, requests.exceptions.RequestException) as e:


            if on_warning is not None:
                on_warning(
                    f"  WARNING: could not tag resources for disaster recovery ({e}) -- "
                    "onboarding still succeeded, but `rebuild` won't find this node if local "
                    "state is lost. Safe to ignore or retry later."
                )

        if needs_key_install:
            assert install_public_key is not None


            if on_progress is not None:
                on_progress(
                    "Installing this deployment's own SSH key so future automated "
                    "operations don't need the one-time credential again..."
                )
            try:
                engine.ssh_run(
                    reserved_ip, ssh_key, _append_authorized_key_command(install_public_key),
                    trust_new=True, password=ssh_password,
                )
            except (ApiError, RuntimeError, requests.exceptions.RequestException) as e:
                if on_warning is not None:
                    on_warning(
                        f"  WARNING: could not install this deployment's own SSH key ({e}) -- "
                        "onboarding still succeeded, but a future automated start/stop will need "
                        "the one-time credential again unless this is retried (e.g. `onboard "
                        "--force`) or the key is added manually."
                    )

        _save_one_record(name, record)


        if record.get("group_id") is not None:
            try:
                _sync_group_membership_tags_locked(
                    client, name, record, _group_row_by_id(record["group_id"]),
                )
            except Exception as e:

                if on_warning is not None:
                    on_warning(
                        f"  WARNING: could not sync group membership tags for '{name}' onto its "
                        f"reserved IP ({e}) -- group membership itself is preserved locally and "
                        "fully in effect, but won't be recoverable via `rebuild` if the local "
                        "database is lost before this is retried (safe to retry: re-run "
                        "`group-add` for this instance)."
                    )


        existing_schedule = get_instance_schedule(name)
        if existing_schedule is not None:
            try:
                _sync_schedule_tags_locked(client, name, record, existing_schedule)
            except Exception as e:

                if on_warning is not None:
                    on_warning(
                        f"  WARNING: could not sync schedule tags for '{name}' onto its reserved "
                        f"IP ({e}) -- the schedule itself is preserved locally and fully in "
                        "effect, but won't be recoverable via `rebuild` if the local database is "
                        "lost before this is retried (safe to retry: re-run `schedule-set` for "
                        "this instance)."
                    )


        try:
            matched_key_ids, _unmatched_keys = _classify_authorized_keys(
                client, record["authorized_keys"],
            )
            _sync_extra_recovery_tags_locked(client, name, record, matched_key_ids)
        except Exception as e:

            if on_warning is not None:
                on_warning(
                    f"  WARNING: could not sync network-config/SSH-key recovery tags for "
                    f"'{name}' onto its reserved IP ({e}) -- onboarding still succeeded and "
                    "'{name}' is fully correct locally; this only affects recovery after a "
                    "total local database loss (safe to retry: `onboard --force`)."
                )
        osb.sync_object_storage_backup(name, record, on_warning=on_warning)


        try:
            fresh_os_volume = client.load(Volume, os_volume_id)
            other_names = [n for n in engine.names_from_tags(fresh_os_volume.tags) if n != name]
            if other_names and on_warning is not None:
                on_warning(
                    f"  WARNING: possible concurrent-onboard race detected -- immediately "
                    f"after tagging, OS volume {os_volume_id}'s tags also show {other_names!r} "
                    "as an owner. This usually means another onboard for a different name "
                    "targeted the same resource at nearly the same time. Verify manually in "
                    "Cloud Manager which name should actually own this volume before trusting "
                    "either name's disaster recovery."
                )
        except Exception as e:


            if on_warning is not None:
                on_warning(
                    f"  WARNING: could not verify OS volume {os_volume_id}'s tags for a "
                    f"possible concurrent-onboard race ({e}) -- onboarding still succeeded. "
                    "Safe to ignore."
                )

        old_reserved_ip = old_record.get("reserved_ip") if old_record is not None else None
        if identity_changed and old_reserved_ip and old_reserved_ip != reserved_ip:


            try:
                engine.reset_known_host(old_reserved_ip)
            except Exception as e:
                if on_warning is not None:
                    on_warning(
                        f"  WARNING: could not clear the known-hosts entry for the old "
                        f"address {old_reserved_ip} ({e}) -- if it's ever reused by a "
                        f"different node, its first onboarding attempt may need "
                        f"`reset-host-key --ip {old_reserved_ip}` first."
                    )

        return OnboardResult(outcome="onboarded", record=record)


def cmd_onboard(client, args) -> int:
    def _confirm_identity_change() -> bool:
        answer = input(f"Type '{args.name}' to confirm this is intentional: ")
        return answer == args.name

    try:
        result = onboard_instance(
            client, args.name, args.instance_id, args.ssh_key,
            vpc_id=args.vpc_id, force=args.force,
            confirm_identity_change=None if args.yes else _confirm_identity_change,
            on_progress=print, on_warning=_print_to_stderr,
        )
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result.outcome == "identity_change_declined":
        print("Aborted -- name didn't match.")
        return 1
    assert result.record is not None
    record = result.record
    instance_attrs = record["instance_attrs"]
    print(f"Onboarded '{args.name}':")
    print(f"  region: {record['region']}")
    print(f"  os_volume_id: {record['os_volume_id']}")
    print(f"  reserved_ip: {record['reserved_ip']}")
    print(f"  network_interface_model: {record['network_interface_model']}")
    print(f"  data_volumes: {len(record['data_volumes'])}")
    print(f"  label: {record['label']}")
    print(f"  tags: {record['tags']}")
    print(f"  plan_type: {instance_attrs['plan_type']}")
    print(f"  firewall_ids: {instance_attrs['firewall_ids']}")
    print(f"  placement_group_id: {instance_attrs['placement_group_id']}")
    print(f"  maintenance_policy: {instance_attrs['maintenance_policy']}")
    print(f"  watchdog_enabled: {instance_attrs['watchdog_enabled']}")
    print(f"  currently: running as instance {record['current_linode_id']} "
          "(not touched by onboarding)")
    print(f"Use `stop --name {args.name}` when ready to stop billing, "
          f"`start --name {args.name}` to bring it back.")
    return 0


def _on_retry(attempt, delay, exc):
    print(f"  failed to start — retrying in {delay}s (attempt {attempt}): {exc}")


def _raise_if_needs_manual_recovery(record, name: str) -> None:

    if record.get("current_status") != "needs_manual_recovery":
        return
    raise NeedsManualRecoveryError(
        f"'{name}' was only partially recovered by `rebuild` -- it was stopped when local "
        "state was lost, so its network config and SSH access couldn't be recovered (only "
        "its resource IDs could). Boot it once manually via Cloud Manager from its "
        f"os_volume_id ({record.get('os_volume_id')}), then run `onboard` again to fully "
        "restore management before using `start`/`stop`."
    )


@dataclass
class StartResult:

    outcome: Literal[
        "started", "already_running", "confirmed_gone_reset_to_stopped",
        "found_offline_marked_unreachable", "transitioning",
        "reachability_check_failed", "create_failed",
    ]
    instance_id: int | None = None
    reserved_ip: str | None = None
    live_status: str | None = None
    security_warning: bool = False
    detail: str | None = None
    manual_override_expires_at: str | None = None


def start_instance(
    client, name: str, ssh_key: str, *,
    triggered_by: Literal["schedule", "manual", "api"] = "manual",
    override_window_hours: float | None = None,
    actor: str | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
    on_created: Callable[[int], None] | None = None,
    on_retry: Callable[[int, int, Exception], None] | None = None,
) -> StartResult:

    _validate_override_window_hours(override_window_hours)
    result = _start_instance_locked(
        client, name, ssh_key, triggered_by=triggered_by, override_window_hours=override_window_hours,
        on_progress=on_progress, on_created=on_created, on_retry=on_retry,
    )
    _record_start_event(name, result, triggered_by, on_warning, actor)
    return result


def _compute_manual_override_expiry(
    name: str, record: dict, triggered_by: Literal["schedule", "manual", "api"],
    override_window_hours: float | None,
) -> str | None:

    if triggered_by == "schedule":
        return None
    schedule, _via_group = resolve_effective_schedule(name, record)


    if not schedule_is_active(schedule):
        return None
    if is_within_scheduled_on_window(schedule, datetime.now(UTC)):
        return None
    hours = (
        override_window_hours if override_window_hours is not None
        else DEFAULT_MANUAL_OVERRIDE_WINDOW_HOURS
    )
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


def _start_instance_locked(
    client, name: str, ssh_key: str, *,
    triggered_by: Literal["schedule", "manual", "api"],
    override_window_hours: float | None,
    on_progress: Callable[[str], None] | None,
    on_created: Callable[[int], None] | None,
    on_retry: Callable[[int, int, Exception], None] | None,
) -> StartResult:
    with _instance_lock(name):


        registry = load_registry()
        record = registry.get(name)
        if record is None:
            raise NotOnboardedError(f"'{name}' is not onboarded. Run `onboard` first.")

        def _confirmed_gone_reset_to_stopped(rec: dict, instance_id: int | None) -> StartResult:


            rec["current_status"] = "stopped"
            rec["current_linode_id"] = None
            rec["manual_override_expires_at"] = None
            _save_one_record(name, rec)
            return StartResult(outcome="confirmed_gone_reset_to_stopped", instance_id=instance_id)

        if record["current_status"] == "running":


            def _already_running(rec: dict) -> StartResult:


                return StartResult(
                    outcome="already_running",
                    instance_id=rec["current_linode_id"], reserved_ip=rec["reserved_ip"],
                )


            try:
                instance = engine.retry_transient(
                    lambda: client.load(Instance, record["current_linode_id"])
                )
            except ApiError as e:
                if e.status == 404:
                    return _confirmed_gone_reset_to_stopped(record, record["current_linode_id"])
                return _already_running(record)
            except requests.exceptions.RequestException:
                return _already_running(record)
            if instance.status == "offline":


                record["current_status"] = "unreachable"


                record["manual_override_expires_at"] = None
                _save_one_record(name, record)
                return StartResult(
                    outcome="found_offline_marked_unreachable",
                    instance_id=record["current_linode_id"],
                )
            if instance.status != "running":


                return StartResult(
                    outcome="transitioning", instance_id=record["current_linode_id"],
                    live_status=instance.status,
                )
            return _already_running(record)
        _raise_if_needs_manual_recovery(record, name)

        if record["current_status"] == "unreachable":


            instance_id = record["current_linode_id"]
            if on_progress is not None:
                on_progress(
                    f"'{name}' has an unreachable instance ({instance_id}) from a previous "
                    "start attempt -- retrying the reachability check instead of creating a "
                    "new one..."
                )
            try:
                instance = engine.retry_transient(lambda: client.load(Instance, instance_id))
                if instance.status not in ("running", "booting"):


                    instance = engine.poll_until_status(
                        lambda: client.load(Instance, instance_id),
                        ("offline", "running", "booting"), timeout_s=120,
                    )
                    if instance.status == "offline":


                        engine.retry_transient_or_already_done(
                            instance.boot,
                            lambda: client.load(Instance, instance_id).status
                            in ("running", "booting"),
                        )
                engine.poll_until_status(
                    lambda: client.load(Instance, instance_id), ("running",), timeout_s=180
                )
                if on_progress is not None:
                    on_progress("  running -- verifying real network reachability...")
                engine.ssh_run(record["reserved_ip"], ssh_key, "echo ok", retries=12, retry_delay_s=10)
            except ApiError as e:
                if e.status == 404:


                    return _confirmed_gone_reset_to_stopped(record, instance_id)
                return StartResult(
                    outcome="reachability_check_failed", instance_id=instance_id,
                    security_warning=_is_host_key_mismatch(e), detail=str(e),
                )
            except requests.exceptions.RequestException as e:


                return StartResult(
                    outcome="reachability_check_failed", instance_id=instance_id,
                    security_warning=_is_host_key_mismatch(e), detail=str(e),
                )
            except (RuntimeError, TimeoutError) as e:
                return StartResult(
                    outcome="reachability_check_failed", instance_id=instance_id,
                    security_warning=_is_host_key_mismatch(e), detail=str(e),
                )
            record["current_status"] = "running"
            record["manual_override_expires_at"] = _compute_manual_override_expiry(
                name, record, triggered_by, override_window_hours,
            )
            _save_one_record(name, record)
            return StartResult(
                outcome="started", instance_id=instance_id, reserved_ip=record["reserved_ip"],
                manual_override_expires_at=record["manual_override_expires_at"],
            )


        if on_progress is not None:
            on_progress(f"Starting '{name}'...")
        try:


            golden_volume = engine.retry_transient(
                lambda: client.load(Volume, record["os_volume_id"])
            )
            data_volume_devices = engine.build_data_volume_devices(record["data_volumes"])
            with engine.transitioning(record, persist_fn=lambda r: _save_one_record(name, r)) as record:
                instance, _config = engine.create_and_boot_instance_with_retry(
                    client,
                    region=record["region"],
                    label=record["label"],
                    tags=record["tags"],
                    golden_volume=golden_volume,
                    reserved_ip=record["reserved_ip"],
                    captured_network={
                        "network_interface_model": record["network_interface_model"],
                        "network_config": record["network_config"],
                        "network_helper_enabled": record["network_helper_enabled"],
                    },


                    authorized_keys=None,
                    root_pass=secrets.token_urlsafe(24),
                    preserve_host_keys=True,
                    vpc_prefix=record["vpc_prefix"],
                    data_volume_devices=data_volume_devices,
                    instance_attrs=record.get("instance_attrs"),
                    on_retry=on_retry,
                    on_created=on_created,
                )
                record["current_linode_id"] = instance.id


                record["current_status"] = "unreachable"
        except (ApiError, RuntimeError, requests.exceptions.RequestException) as e:
            return StartResult(outcome="create_failed", detail=str(e))


        if on_progress is not None:
            on_progress(f"  instance: {instance.id}")

        try:
            engine.poll_until_status(
                lambda: client.load(Instance, instance.id), ("running",), timeout_s=180
            )
            if on_progress is not None:
                on_progress("  running -- verifying real network reachability...")


            engine.ssh_run(record["reserved_ip"], ssh_key, "echo ok", retries=12, retry_delay_s=10)
        except (
            ApiError, RuntimeError, TimeoutError, requests.exceptions.RequestException,
        ) as e:


            return StartResult(
                outcome="reachability_check_failed", instance_id=instance.id,
                security_warning=_is_host_key_mismatch(e), detail=str(e),
            )
        record["current_status"] = "running"
        record["manual_override_expires_at"] = _compute_manual_override_expiry(
            name, record, triggered_by, override_window_hours,
        )
        _save_one_record(name, record)
        return StartResult(
            outcome="started", instance_id=instance.id, reserved_ip=record["reserved_ip"],
            manual_override_expires_at=record["manual_override_expires_at"],
        )


def _is_host_key_mismatch(e: Exception) -> bool:
    return "host key" in str(e).lower() or "REMOTE HOST IDENTIFICATION" in str(e)


def _print_to_stderr(message: str) -> None:

    print(message, file=sys.stderr)


def cmd_start(client, args) -> int:
    try:
        result = start_instance(
            client, args.name, args.ssh_key,
            override_window_hours=args.override_window_hours,
            on_progress=print, on_warning=_print_to_stderr,
            on_created=lambda i: print(f"  instance created: {i} (booting...)"),
            on_retry=_on_retry,
        )
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    return _print_start_result(args.name, result)


def _print_start_result(name: str, result: StartResult) -> int:

    if result.outcome == "already_running":
        print(f"'{name}' is already running (instance {result.instance_id}, "
              f"IP {result.reserved_ip}).")
        return 0
    if result.outcome == "confirmed_gone_reset_to_stopped":
        print(
            f"Configuration error: instance {result.instance_id} no longer exists (confirmed "
            f"404) -- '{name}' has been reset to 'stopped'. Its OS volume, data volume(s), and "
            "reserved IP are unaffected. Run `start` again to recreate it.", file=sys.stderr,
        )
        return 1
    if result.outcome == "found_offline_marked_unreachable":
        print(f"'{name}' instance {result.instance_id} exists but is offline -- likely powered "
              "off out-of-band. Marking it for recovery...")
        print(
            f"Configuration error: '{name}' was found powered off, not deleted -- run "
            f"`start --name {name}` again to boot it back up and verify reachability.",
            file=sys.stderr,
        )
        return 1
    if result.outcome == "transitioning":
        print(f"'{name}' instance {result.instance_id} is currently transitioning "
              f"(status: {result.live_status}), not powered off -- try `start --name {name}` "
              "again shortly once it settles.", file=sys.stderr)
        return 1
    if result.outcome == "reachability_check_failed":
        _print_start_reachability_failure(
            name, result.instance_id, result.detail, result.security_warning,
        )
        return 1
    if result.outcome == "create_failed":
        print(f"Configuration error: failed to start '{name}': {result.detail}", file=sys.stderr)
        return 1

    print(f"'{name}' is up at {result.reserved_ip}.")
    if result.manual_override_expires_at is not None:
        print(f"  manually started outside its scheduled hours -- auto-stops at "
              f"{_format_override_expiry(result.manual_override_expires_at)} unless extended "
              f"(`extend --name {name}`).")
    return 0


def _format_override_expiry(iso_timestamp: str) -> str:

    return datetime.fromisoformat(iso_timestamp).strftime("%Y-%m-%d %H:%M UTC")


def _print_start_reachability_failure(
    name: str, instance_id: int | None, detail: str | None, security_warning: bool,
) -> None:

    if security_warning:
        print(
            f"SECURITY WARNING: '{name}' has instance {instance_id} but it presented a "
            f"DIFFERENT SSH host key than the one on record: {detail}. This should never happen "
            "after a steady-state recreate -- do not proceed. If this key change is genuinely "
            "expected (e.g. you intentionally replaced the disk), confirm out-of-band first, "
            f"then run `reset-host-key --name {name}` before retrying.",
            file=sys.stderr,
        )
    else:
        print(
            f"Configuration error: '{name}' has instance {instance_id} but it could not be "
            f"confirmed reachable: {detail}. Check Cloud Manager directly -- it may still be "
            f"booting; `status --name {name}` shows it as 'unreachable' (a real instance "
            f"exists, but its health check hasn't passed) until a retried `start --name {name}` "
            "succeeds.",
            file=sys.stderr,
        )


@dataclass
class StopResult:

    outcome: Literal[
        "already_stopped", "confirmed_gone_reset_to_stopped", "aborted_by_user",
        "prepare_failed", "delete_failed", "stopped",
    ]
    instance_id: int | None = None
    detail: str | None = None


def stop_instance(
    client, name: str, ssh_key: str, *,
    skip_precapture: bool = False, force: bool = False,
    triggered_by: Literal["schedule", "manual", "api"] = "manual",
    actor: str | None = None,
    confirm: Callable[[dict], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> StopResult:

    result = _stop_instance_locked(
        client, name, ssh_key,
        skip_precapture=skip_precapture, force=force,
        confirm=confirm, on_progress=on_progress, on_warning=on_warning,
    )
    _record_stop_event(name, result, triggered_by, on_warning, actor)
    return result


def _stop_instance_locked(
    client, name: str, ssh_key: str, *,
    skip_precapture: bool,
    force: bool,
    confirm: Callable[[dict], bool] | None,
    on_progress: Callable[[str], None] | None,
    on_warning: Callable[[str], None] | None,
) -> StopResult:
    with _instance_lock(name):
        registry = load_registry()
        record = registry.get(name)
        if record is None:
            raise NotOnboardedError(f"'{name}' is not onboarded.")
        if record["current_status"] == "stopped":
            return StopResult(outcome="already_stopped")
        _raise_if_needs_manual_recovery(record, name)

        def _reset_to_stopped_confirmed_gone(rec: dict) -> StopResult:


            old_instance_id = rec["current_linode_id"]
            rec["current_status"] = "stopped"
            rec["current_linode_id"] = None
            rec["manual_override_expires_at"] = None
            _save_one_record(name, rec)
            return StopResult(outcome="confirmed_gone_reset_to_stopped", instance_id=old_instance_id)

        if record["current_status"] in ("unreachable", "running"):


            try:


                engine.retry_transient(lambda: client.load(Instance, record["current_linode_id"]))
            except ApiError as e:
                if e.status == 404:
                    return _reset_to_stopped_confirmed_gone(record)
            except requests.exceptions.RequestException:
                pass

        if confirm is not None and not confirm(record):
            return StopResult(outcome="aborted_by_user")

        try:


            try:


                instance = engine.retry_transient(
                    lambda: client.load(Instance, record["current_linode_id"])
                )
            except ApiError as e:
                if e.status != 404:
                    raise
                return _reset_to_stopped_confirmed_gone(record)


            golden_volume = engine.retry_transient(
                lambda: client.load(Volume, record["os_volume_id"])
            )

            if on_progress is not None:
                on_progress(
                    f"Re-capturing network config, data volumes, authorized_keys, label, tags, "
                    f"and instance attributes before stopping '{name}' (an out-of-band change "
                    "since the last start/stop shouldn't be silently reverted)..."
                )


            configs = list(instance.configs)
            fresh_network = engine.capture_network_config(instance, configs=configs)


            fresh_data_volumes = engine.capture_data_volumes(
                instance, record["os_volume_id"], configs=configs,
            )
            if skip_precapture:


                if on_warning is not None:
                    on_warning(
                        "  WARNING: --skip-precapture given -- using the last-known "
                        "authorized_keys from the registry instead of a fresh SSH capture. An "
                        "SSH key added/revoked out of band since the last start/stop cycle "
                        "would be silently missed."
                    )
                fresh_authorized_keys = record["authorized_keys"]
            else:


                authorized_keys_raw = engine.ssh_run(
                    record["reserved_ip"], ssh_key, "cat /root/.ssh/authorized_keys 2>/dev/null",
                )
                fresh_authorized_keys = [
                    line.strip() for line in authorized_keys_raw.splitlines() if line.strip()
                ]
                if not fresh_authorized_keys:
                    raise RuntimeError(
                        "could not read any authorized_keys from the instance -- refusing to "
                        "proceed without knowing how the next `start` would grant SSH access "
                        "(use --skip-precapture to keep the last-known list instead, if that's "
                        "genuinely intended)"
                    )


            data_volumes_now = []
            still_recorded = []
            for dv in fresh_data_volumes:
                try:


                    data_volumes_now.append(
                        engine.retry_transient(lambda dv=dv: client.load(Volume, dv["volume_id"]))
                    )
                    still_recorded.append(dv)
                except ApiError as e:
                    if e.status != 404:
                        raise
                    if on_warning is not None:
                        on_warning(
                            f"  WARNING: data volume {dv['volume_id']} no longer exists "
                            "(detached or deleted out of band since the last cycle) -- "
                            f"dropping it from the recorded data volume list for '{name}'."
                        )
            fresh_data_volumes = still_recorded
            fresh_label = instance.label
            fresh_tags = list(instance.tags)
            fresh_instance_attrs = engine.capture_instance_attributes(instance)
        except (ApiError, RuntimeError, requests.exceptions.RequestException) as e:
            return StopResult(outcome="prepare_failed", detail=f"{e}. Nothing was deleted.")

        try:
            engine.tag_managed_resources(
                client, name, os_volume_id=record["os_volume_id"],
                data_volume_ids=[dv["volume_id"] for dv in fresh_data_volumes],
                reserved_ip=record["reserved_ip"],
            )
        except (engine.ResourceOwnershipConflict, engine.TagVerificationError) as e:


            raise engine.ConfigError(
                f"could not refresh disaster-recovery tags for '{name}' ({e}). Refusing to "
                "stop -- this would delete the live instance while the account-side recovery "
                "mapping is provably wrong. Resolve the conflict (or re-onboard) before "
                "retrying."
            ) from e
        except (ApiError, requests.exceptions.RequestException) as e:


            if not force:
                raise engine.ConfigError(
                    f"could not refresh disaster-recovery tags for '{name}' ({e}). Refusing to "
                    "stop -- pass --force to proceed anyway if this is a known transient API "
                    "issue."
                ) from e
            if on_warning is not None:
                on_warning(
                    f"  WARNING: could not refresh disaster-recovery tags ({e}) -- --force "
                    "given, continuing with the stop anyway."
                )

        if on_progress is not None:
            on_progress(f"Stopping '{name}' (confirmed detach before delete)...")
        try:
            with engine.transitioning(record, persist_fn=lambda r: _save_one_record(name, r)) as record:
                engine.delete_instance_and_detach_volumes(
                    client, instance, [golden_volume, *data_volumes_now]
                )
                record["network_config"] = fresh_network["network_config"]
                record["network_helper_enabled"] = fresh_network["network_helper_enabled"]
                record["data_volumes"] = fresh_data_volumes
                record["authorized_keys"] = fresh_authorized_keys
                record["label"] = fresh_label
                record["tags"] = fresh_tags
                record["instance_attrs"] = fresh_instance_attrs
                record["current_linode_id"] = None
                record["current_status"] = "stopped"
                record["manual_override_expires_at"] = None
        except (ApiError, RuntimeError, requests.exceptions.RequestException) as e:


            return StopResult(
                outcome="delete_failed",
                detail=f"{e}. Check Cloud Manager directly -- it may be partially deleted; "
                f"`status --name {name}` reflects whatever was captured before the failure.",
            )


        try:
            matched_key_ids, _unmatched_keys = _classify_authorized_keys(client, fresh_authorized_keys)
            _sync_extra_recovery_tags_locked(client, name, record, matched_key_ids)
        except Exception as e:
            if on_warning is not None:
                on_warning(
                    f"  WARNING: could not sync network-config/SSH-key recovery tags for "
                    f"'{name}' onto its reserved IP ({e}) -- '{name}' is fully stopped and "
                    "correct locally; this only affects recovery after a total local database "
                    "loss (safe to retry: just run `stop`/`start` again)."
                )
        osb.sync_object_storage_backup(name, record, on_warning=on_warning)

        return StopResult(outcome="stopped")


def cmd_stop(client, args) -> int:
    def _confirm(record: dict) -> bool:
        answer = input(f"Stop '{args.name}' (instance {record['current_linode_id']})? [y/N] ")
        return answer.strip().lower() == "y"

    try:
        result = stop_instance(
            client, args.name, args.ssh_key,
            skip_precapture=args.skip_precapture, force=args.force,
            confirm=None if args.yes else _confirm, on_progress=print,
            on_warning=_print_to_stderr,
        )
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    return _print_stop_result(args.name, result)


def _print_stop_result(name: str, result: StopResult) -> int:

    if result.outcome == "already_stopped":
        print(f"'{name}' is already stopped.")
        return 0
    if result.outcome == "confirmed_gone_reset_to_stopped":
        print(
            f"'{name}': instance {result.instance_id} no longer exists (confirmed 404, "
            "presumably deleted out-of-band) -- reset to 'stopped'. Its OS volume, data "
            "volume(s), and reserved IP are unaffected."
        )
        return 0
    if result.outcome == "aborted_by_user":
        print("Aborted.")
        return 1
    if result.outcome == "prepare_failed":
        print(f"Configuration error: failed to prepare '{name}' for stopping: {result.detail}",
              file=sys.stderr)
        return 1
    if result.outcome == "delete_failed":
        print(f"Configuration error: failed to stop '{name}': {result.detail}", file=sys.stderr)
        return 1

    print(f"'{name}' stopped; OS volume, data volume(s), and reserved IP all survive.")
    return 0


def extend_manual_override(name: str, hours: float | None = None) -> str:

    _validate_override_window_hours(hours)
    with _instance_lock(name):
        registry = load_registry()
        record = registry.get(name)
        if record is None:
            raise NotOnboardedError(f"'{name}' is not onboarded.")
        if record.get("current_status") != "running":
            raise engine.ConfigError(f"'{name}' is not currently running -- nothing to extend.")
        if not record.get("manual_override_expires_at"):
            raise engine.ConfigError(
                f"'{name}' has no active manual-override timer to extend -- it was either "
                "started within its schedule's own on-window, has no schedule at all, or is "
                "already following its schedule normally."
            )
        window_hours = hours if hours is not None else DEFAULT_MANUAL_OVERRIDE_WINDOW_HOURS
        new_expiry = (datetime.now(UTC) + timedelta(hours=window_hours)).isoformat()
        record["manual_override_expires_at"] = new_expiry
        _save_one_record(name, record)
        return new_expiry


def cmd_extend(args) -> int:
    try:
        new_expiry = extend_manual_override(args.name, args.hours)
    except NotOnboardedError as e:
        print(f"{e}", file=sys.stderr)
        return 1


    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"'{args.name}' extended -- now auto-stops at {_format_override_expiry(new_expiry)}.")
    return 0


@dataclass
class OffboardResult:

    outcome: Literal["aborted_by_user", "incomplete", "offboarded"]
    detail: str | None = None


def offboard_instance(
    client, name: str, *,
    delete_volumes: bool = False,
    confirm: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> OffboardResult:

    with _instance_lock(name):
        registry = load_registry()
        record = registry.get(name)
        if record is None:
            raise NotOnboardedError(f"'{name}' is not onboarded.")
        if record["current_status"] not in ("stopped", "needs_manual_recovery"):
            raise engine.ConfigError(
                f"'{name}' is currently {record['current_status']!r} -- stop it first "
                f"(`stop --name {name}`) before offboarding."
            )

        reserved_ip = record.get("reserved_ip")
        os_volume_id = record.get("os_volume_id")
        data_volume_ids = [dv["volume_id"] for dv in record.get("data_volumes", [])]

        if on_progress is not None:
            on_progress(f"Offboarding '{name}' will permanently:")
            if reserved_ip:
                on_progress(
                    f"  - release reserved IP {reserved_ip} back to Linode's pool (never reused "
                    "-- a new address would be assigned if this node is ever re-onboarded)"
                )
            else:
                on_progress("  - no reserved IP on record for this node -- nothing to release")
            if delete_volumes:
                on_progress(
                    f"  - PERMANENTLY DELETE the OS volume ({os_volume_id}) and "
                    f"{len(data_volume_ids)} data volume(s) {data_volume_ids} -- this destroys "
                    "real data and cannot be undone"
                )
            else:
                on_progress(
                    f"  - leave the OS volume ({os_volume_id}) and {len(data_volume_ids)} data "
                    "volume(s) intact, untagged so `rebuild` won't resurrect this node "
                    "(pass --delete-volumes to also permanently remove them)"
                )
            on_progress(f"  - remove '{name}' from the local registry")

        if confirm is not None and not confirm():
            return OffboardResult(outcome="aborted_by_user")


        if not delete_volumes:
            try:
                engine.untag_managed_resources(
                    client, name, os_volume_id=os_volume_id, data_volume_ids=data_volume_ids,
                )
            except (ApiError, requests.exceptions.RequestException, engine.ConfigError) as e:


                return OffboardResult(
                    outcome="incomplete",
                    detail=f"Could not remove disaster-recovery tags ({e}). Offboard is "
                    f"INCOMPLETE -- nothing else was changed. Re-run `offboard --name {name}` "
                    "to retry; already-completed steps are safe to repeat.",
                )


        deleted_so_far: list[int | str] = []

        def _incomplete_note() -> str:
            if not deleted_so_far:
                return " -- nothing was deleted"
            return f" -- {deleted_so_far} already permanently deleted above before this step"

        if delete_volumes:


            candidates = [(os_volume_id, engine.REGISTRY_ROLE_TAG_OS)] if os_volume_id else []
            candidates += [(vid, engine.REGISTRY_ROLE_TAG_DATA) for vid in data_volume_ids]

            for vid, role_tag in candidates:
                try:
                    problems, vol = engine.verify_managed_volume_owned_by_name(
                        client, vid, name, role_tag
                    )
                except ApiError as e:
                    if e.status == 404:
                        if on_progress is not None:
                            on_progress(f"  volume {vid}: already gone")


                        deleted_so_far.append(vid)
                        continue
                    return OffboardResult(
                        outcome="incomplete",
                        detail=f"Could not verify volume {vid} before deleting it ({e}). "
                        f"Offboard is INCOMPLETE{_incomplete_note()}. Re-run `offboard --name "
                        f"{name} --delete-volumes` to retry.",
                    )
                except requests.exceptions.RequestException as e:


                    return OffboardResult(
                        outcome="incomplete",
                        detail=f"Could not verify volume {vid} before deleting it ({e}). "
                        f"Offboard is INCOMPLETE{_incomplete_note()}. Re-run `offboard --name "
                        f"{name} --delete-volumes` to retry.",
                    )
                if problems:
                    return OffboardResult(
                        outcome="incomplete",
                        detail=f"Refusing to delete volume {vid} -- its current tags don't "
                        f"confirm it still belongs to '{name}': {'; '.join(problems)}. Offboard "
                        f"is INCOMPLETE{_incomplete_note()}. Review tags directly in Cloud "
                        "Manager before retrying.",
                    )
                try:


                    engine.retry_transient(vol.delete)
                    if on_progress is not None:
                        on_progress(f"  deleted volume {vid}")
                    deleted_so_far.append(vid)
                except ApiError as e:
                    if e.status != 404:
                        return OffboardResult(
                            outcome="incomplete",
                            detail=f"Failed to delete volume {vid} ({e}). Offboard is "
                            f"INCOMPLETE{_incomplete_note()}. Re-run `offboard --name {name} "
                            "--delete-volumes` to retry; already-deleted volumes are safe to "
                            "skip.",
                        )
                    if on_progress is not None:
                        on_progress(f"  volume {vid}: already gone")
                    deleted_so_far.append(vid)
                except requests.exceptions.RequestException as e:


                    return OffboardResult(
                        outcome="incomplete",
                        detail=f"Failed to delete volume {vid} ({e}). Offboard is "
                        f"INCOMPLETE{_incomplete_note()}. Re-run `offboard --name {name} "
                        "--delete-volumes` to retry; already-deleted volumes are safe to skip.",
                    )

        if reserved_ip:


            ip = None
            try:
                problems, ip = engine.verify_managed_ip_owned_by_name(client, reserved_ip, name)
            except ApiError as e:
                if e.status != 404:
                    return OffboardResult(
                        outcome="incomplete",
                        detail=f"Could not verify reserved IP {reserved_ip} before releasing it "
                        f"({e}). Offboard is INCOMPLETE -- the reserved IP was NOT released"
                        f"{_incomplete_note()}. Re-run `offboard --name {name}` to retry.",
                    )
                if on_progress is not None:
                    on_progress(f"  reserved IP {reserved_ip}: already gone")


                deleted_so_far.append(reserved_ip)
            except requests.exceptions.RequestException as e:
                return OffboardResult(
                    outcome="incomplete",
                    detail=f"Could not verify reserved IP {reserved_ip} before releasing it "
                    f"({e}). Offboard is INCOMPLETE -- the reserved IP was NOT released"
                    f"{_incomplete_note()}. Re-run `offboard --name {name}` to retry.",
                )

            if ip is not None:
                if problems:
                    return OffboardResult(
                        outcome="incomplete",
                        detail=f"Refusing to release reserved IP {reserved_ip} -- its current "
                        f"tags don't confirm it still belongs to '{name}': "
                        f"{'; '.join(problems)}. Offboard is INCOMPLETE -- the reserved IP was "
                        f"NOT released{_incomplete_note()}. Review tags directly in Cloud "
                        "Manager before retrying.",
                    )
                try:


                    engine.retry_transient(ip.delete)
                    if on_progress is not None:
                        on_progress(f"  released reserved IP {reserved_ip}")
                except ApiError as e:
                    if e.status != 404:


                        return OffboardResult(
                            outcome="incomplete",
                            detail=f"Failed to release reserved IP {reserved_ip} ({e}). "
                            f"Offboard is INCOMPLETE -- the reserved IP was NOT released"
                            f"{_incomplete_note()}. Re-run `offboard --name {name}` to retry.",
                        )
                    if on_progress is not None:
                        on_progress(f"  reserved IP {reserved_ip}: already gone")


                    deleted_so_far.append(reserved_ip)
                except requests.exceptions.RequestException as e:


                    return OffboardResult(
                        outcome="incomplete",
                        detail=f"Failed to release reserved IP {reserved_ip} ({e}). Offboard is "
                        f"INCOMPLETE -- the reserved IP was NOT released{_incomplete_note()}. "
                        f"Re-run `offboard --name {name}` to retry.",
                    )


            try:
                engine.reset_known_host(reserved_ip)
            except Exception as e:
                if on_warning is not None:
                    on_warning(
                        f"  WARNING: could not clear the known-hosts entry for {reserved_ip} "
                        f"({e}) -- if this address is ever reused by a different node, its "
                        f"first onboarding attempt may need `reset-host-key --ip {reserved_ip}` "
                        "first."
                    )

        _delete_one_record(name)
        return OffboardResult(outcome="offboarded")


def cmd_offboard(client, args) -> int:
    def _confirm() -> bool:
        answer = input(f"Type '{args.name}' to confirm: ")
        return answer == args.name

    try:
        result = offboard_instance(
            client, args.name, delete_volumes=args.delete_volumes,
            confirm=None if args.yes else _confirm,
            on_progress=print, on_warning=_print_to_stderr,
        )
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result.outcome == "aborted_by_user":
        print("Aborted -- name didn't match.")
        return 1
    if result.outcome == "incomplete":
        print(f"  {result.detail}", file=sys.stderr)
        return 1
    print(f"'{args.name}' fully offboarded.")
    return 0


VALID_SCHEDULE_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


_SCHEDULE_TAG_PREFIX = "sched-"
_GROUP_SCHEDULE_TAG_PREFIX = "grp-"


_SCHEDULE_DAY_BITS = {day: 1 << i for i, day in enumerate(VALID_SCHEDULE_DAYS)}


def _is_schedule_tag(tag: str, *, prefix: str = _SCHEDULE_TAG_PREFIX) -> bool:
    return tag.startswith((f"{prefix}tz:", f"{prefix}en:", f"{prefix}r"))


def _encode_schedule_as_tags(schedule: dict, *, prefix: str = _SCHEDULE_TAG_PREFIX) -> list[str]:

    tags = [
        f"{prefix}tz:{schedule['timezone']}",
        f"{prefix}en:{1 if schedule.get('enabled', True) else 0}",
    ]
    for i, rule in enumerate(schedule["rules"]):


        bitmask = sum(_SCHEDULE_DAY_BITS[d] for d in set(rule["days_of_week"]))
        start = rule["start_time"].replace(":", "")
        stop = rule["stop_time"].replace(":", "")
        tags.append(f"{prefix}r{i}:{bitmask}-{start}-{stop}")
    return tags


def _decode_schedule_from_tags(
    tags: list[str] | None, *, prefix: str = _SCHEDULE_TAG_PREFIX
) -> dict | None:

    tz_prefix, en_prefix, rule_prefix = f"{prefix}tz:", f"{prefix}en:", f"{prefix}r"
    timezone = None
    enabled = True
    rules_by_index: dict[int, dict] = {}
    for tag in tags or []:
        if tag.startswith(tz_prefix):
            timezone = tag[len(tz_prefix):]
        elif tag.startswith(en_prefix):
            enabled = tag[len(en_prefix):] == "1"
        elif tag.startswith(rule_prefix) and ":" in tag:
            rule_tag_prefix, _, rest = tag.partition(":")
            try:
                index = int(rule_tag_prefix[len(rule_prefix):])
                bitmask_str, start, stop = rest.split("-")
                bitmask = int(bitmask_str)
            except ValueError:
                continue
            days = [d for d in VALID_SCHEDULE_DAYS if bitmask & _SCHEDULE_DAY_BITS[d]]
            if not days or len(start) != 4 or len(stop) != 4:
                continue
            rules_by_index[index] = {
                "days_of_week": days,
                "start_time": f"{start[:2]}:{start[2:]}",
                "stop_time": f"{stop[:2]}:{stop[2:]}",
            }
    if timezone is None or not rules_by_index:
        return None
    rules = [rules_by_index[i] for i in sorted(rules_by_index)]
    return {"timezone": timezone, "rules": rules, "enabled": enabled}


def _verify_reserved_ip_tag_write(client, reserved_ip: str, expected_tags: list[str]) -> None:

    fresh = engine.retry_transient(lambda: client.load(engine.ReservedIPAddress, reserved_ip))
    if set(fresh.tags or []) != set(expected_tags):
        raise engine.TagVerificationError(
            f"tag write for reserved IP {reserved_ip} did not take effect as expected -- a "
            "fresh read shows different tags than what was just saved."
        )


def _sync_schedule_tags_locked(client, name: str, record: dict, schedule: dict | None) -> None:


    if get_instance_schedule(name) != schedule:
        return
    ip = engine.retry_transient(
        lambda: client.load(engine.ReservedIPAddress, record["reserved_ip"])
    )
    kept = [t for t in (ip.tags or []) if not _is_schedule_tag(t)]
    new_tags = kept + (_encode_schedule_as_tags(schedule) if schedule else [])
    if new_tags != (ip.tags or []):
        ip.tags = new_tags
        ip.save()
        _verify_reserved_ip_tag_write(client, record["reserved_ip"], new_tags)


def _sync_schedule_tags(
    client, name: str, schedule: dict | None, *, on_warning: Callable[[str], None] | None = None
) -> None:

    try:


        with _instance_lock(name):


            registry = load_registry()
            record = registry.get(name)
            if record is None or not record.get("reserved_ip"):
                return
            _sync_schedule_tags_locked(client, name, record, schedule)
    except Exception as e:
        if on_warning is not None:
            on_warning(
                f"  WARNING: could not sync schedule tags for '{name}' onto its reserved IP "
                f"({e}) -- the schedule itself was saved locally and is fully in effect, but "
                "won't be recoverable via `rebuild` if the local database is lost before this "
                "is retried (safe to retry: just run schedule-set again)."
            )


def _validate_schedule_rules(rules: list) -> None:

    if not rules or not isinstance(rules, list):
        raise engine.ConfigError("a schedule needs at least one rule.")
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise engine.ConfigError(f"rule {i}: must be an object with days_of_week/start_time/stop_time.")
        days = rule.get("days_of_week")
        if not days or not isinstance(days, list):
            raise engine.ConfigError(f"rule {i}: days_of_week must be a non-empty list.")
        for day in days:
            if day not in VALID_SCHEDULE_DAYS:
                raise engine.ConfigError(
                    f"rule {i}: invalid day {day!r} -- must be one of {VALID_SCHEDULE_DAYS}."
                )
        if len(set(days)) != len(days):


            raise engine.ConfigError(f"rule {i}: days_of_week has a repeated day -- {days!r}.")
        start_time = rule.get("start_time")
        stop_time = rule.get("stop_time")
        for label, value in (("start_time", start_time), ("stop_time", stop_time)):
            if not isinstance(value, str) or not _TIME_RE.fullmatch(value):
                raise engine.ConfigError(f"rule {i}: {label} must be HH:MM 24-hour, got {value!r}.")
        assert isinstance(start_time, str) and isinstance(stop_time, str)
        if start_time >= stop_time:
            raise engine.ConfigError(
                f"rule {i}: start_time ({start_time}) must be before stop_time ({stop_time}) -- "
                "overnight schedules (stop_time on the next day) are not supported yet."
            )


def _validate_schedule_timezone(timezone: str) -> None:

    try:
        ZoneInfo(timezone)
    except Exception as e:

        raise engine.ConfigError(f"invalid timezone {timezone!r}: {e}") from e


def _save_schedule_row(name: str, timezone: str, rules: list, enabled: bool) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO schedules (instance_name, timezone, rules, enabled) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(instance_name) DO UPDATE SET timezone = excluded.timezone,"
                " rules = excluded.rules, enabled = excluded.enabled",
                (name, timezone, json.dumps(rules), 1 if enabled else 0),
            )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def save_instance_schedule(
    client, name: str, timezone: str, rules: list, enabled: bool = True,
    on_warning: Callable[[str], None] | None = None,
) -> None:

    _validate_schedule_rules(rules)
    _validate_schedule_timezone(timezone)
    with _instance_lock(name):
        if name not in load_registry():
            raise NotOnboardedError(f"'{name}' is not onboarded. Run `onboard` first.")
        _save_schedule_row(name, timezone, rules, enabled)
    _sync_schedule_tags(
        client, name, {"timezone": timezone, "rules": rules, "enabled": enabled},
        on_warning=on_warning,
    )


def get_instance_schedule(name: str) -> dict | None:

    def _do():
        conn = _connect()
        try:
            return conn.execute(
                "SELECT timezone, rules, enabled FROM schedules WHERE instance_name = ?", (name,)
            ).fetchone()
        finally:
            conn.close()

    row = _retry_db(_do)
    if row is None:
        return None
    return {"timezone": row[0], "rules": json.loads(row[1]), "enabled": bool(row[2])}


def _clear_schedule_row(name: str) -> bool:

    def _do():
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM schedules WHERE instance_name = ?", (name,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    return _retry_db(_do)


def clear_instance_schedule(
    client, name: str, on_warning: Callable[[str], None] | None = None
) -> bool:

    with _instance_lock(name):
        cleared = _clear_schedule_row(name)
    if cleared:
        _sync_schedule_tags(client, name, None, on_warning=on_warning)
    return cleared


def _parse_schedule_rules_from_cli_args(args) -> list | None:

    if args.rules_json is not None:
        if args.days is not None or args.start_time is not None or args.stop_time is not None:
            print(
                "Configuration error: --rules-json can't be combined with --days/--start-time/"
                "--stop-time -- use one or the other.",
                file=sys.stderr,
            )
            return None
        try:
            parsed = json.loads(args.rules_json)
        except json.JSONDecodeError as e:
            print(f"Configuration error: --rules-json is not valid JSON: {e}", file=sys.stderr)
            return None
        if parsed is None:


            print(
                "Configuration error: --rules-json must be a JSON array of rule objects, not "
                "null.",
                file=sys.stderr,
            )
            return None
        return parsed
    if args.days is None or args.start_time is None or args.stop_time is None:
        print(
            "Configuration error: pass --days/--start-time/--stop-time together (one rule), "
            "or --rules-json (one or more rules).",
            file=sys.stderr,
        )
        return None
    return [{
        "days_of_week": [d.strip() for d in args.days.split(",") if d.strip()],
        "start_time": args.start_time, "stop_time": args.stop_time,
    }]


def cmd_schedule_set(client, args) -> int:
    rules = _parse_schedule_rules_from_cli_args(args)
    if rules is None:
        return 1

    try:
        save_instance_schedule(
            client, args.name, args.timezone, rules, enabled=not args.disabled,
            on_warning=_print_to_stderr,
        )
    except NotOnboardedError as e:
        print(f"{e}", file=sys.stderr)
        return 1


    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"Schedule set for '{args.name}': {len(rules)} rule(s), "
          f"{'enabled' if not args.disabled else 'disabled'}.")
    return 0


def cmd_schedule_show(args) -> int:
    schedule = get_instance_schedule(args.name)
    if schedule is None:
        print(f"No schedule set for '{args.name}'.")
        return 0
    print(json.dumps(schedule, indent=2))
    return 0


def cmd_schedule_clear(client, args) -> int:
    try:
        cleared = clear_instance_schedule(client, args.name, on_warning=_print_to_stderr)
    except InstanceLockedError as e:


        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if not cleared:
        print(f"'{args.name}' has no schedule to clear.")
        return 0
    print(f"Schedule cleared for '{args.name}'.")
    return 0


MAX_GROUP_NAME_LENGTH = 41


_GROUP_NAME_TAG_PREFIX = "grp-name:"


def _group_id_for_name(conn: sqlite3.Connection, group_name: str) -> int:

    row = conn.execute("SELECT id FROM schedule_groups WHERE name = ?", (group_name,)).fetchone()
    if row is None:
        raise GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
    return row[0]


def _group_row_by_id(group_id: int) -> dict | None:

    def _do():
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT name, timezone, rules, enabled FROM schedule_groups WHERE id = ?",
                (group_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "id": group_id, "name": row[0], "timezone": row[1], "rules": json.loads(row[2]),
                "enabled": bool(row[3]),
            }
        finally:
            conn.close()

    return _retry_db(_do)


def create_schedule_group(group_name: str, timezone: str) -> int:

    if not group_name or not group_name.strip():
        raise engine.ConfigError("a group name is required.")
    if len(group_name) > MAX_GROUP_NAME_LENGTH:
        raise engine.ConfigError(
            f"group name {group_name!r} is {len(group_name)} characters -- must be "
            f"{MAX_GROUP_NAME_LENGTH} or fewer, so its disaster-recovery tag "
            f"('{_GROUP_NAME_TAG_PREFIX}{group_name}') fits Linode's 50-character tag limit."
        )
    _validate_schedule_timezone(timezone)

    def _do():
        conn = _connect()
        try:
            try:
                cur = conn.execute(
                    "INSERT INTO schedule_groups (name, timezone, rules, enabled)"
                    " VALUES (?, ?, '[]', 1)",
                    (group_name, timezone),
                )
                conn.commit()
                return cur.lastrowid
            except sqlite3.IntegrityError as e:
                raise engine.ConfigError(f"a group named '{group_name}' already exists.") from e
        finally:
            conn.close()

    return _retry_db(_do)


def get_schedule_group(group_name: str) -> dict | None:

    def _do():
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id, name, timezone, rules, enabled FROM schedule_groups WHERE name = ?",
                (group_name,),
            ).fetchone()
            if row is None:
                return None
            members = [
                r[0] for r in conn.execute(
                    "SELECT name FROM instances WHERE group_id = ? ORDER BY name", (row[0],)
                ).fetchall()
            ]
            return {
                "id": row[0], "name": row[1], "timezone": row[2], "rules": json.loads(row[3]),
                "enabled": bool(row[4]), "members": members,
            }
        finally:
            conn.close()

    return _retry_db(_do)


def list_schedule_groups() -> list[dict]:

    def _do():
        conn = _connect()
        try:
            groups = conn.execute(
                "SELECT id, name, timezone, rules, enabled FROM schedule_groups ORDER BY name"
            ).fetchall()
            counts = dict(conn.execute(
                "SELECT group_id, COUNT(*) FROM instances"
                " WHERE group_id IS NOT NULL GROUP BY group_id"
            ).fetchall())
            return [
                {
                    "id": gid, "name": name, "timezone": tz, "rules": json.loads(rules),
                    "enabled": bool(enabled), "member_count": counts.get(gid, 0),
                }
                for gid, name, tz, rules, enabled in groups
            ]
        finally:
            conn.close()

    return _retry_db(_do)


def _sync_group_membership_tags_locked(client, name: str, record: dict, group: dict | None) -> None:


    if group is not None:
        if record.get("group_id") != group.get("id"):
            return
    elif record.get("group_id") is not None:
        return
    ip = engine.retry_transient(
        lambda ip_addr=record["reserved_ip"]: client.load(engine.ReservedIPAddress, ip_addr)
    )
    kept = [
        t for t in (ip.tags or [])
        if not t.startswith(_GROUP_NAME_TAG_PREFIX)
        and not _is_schedule_tag(t, prefix=_GROUP_SCHEDULE_TAG_PREFIX)
    ]
    new_tags = kept
    if group is not None:
        new_tags = new_tags + [f"{_GROUP_NAME_TAG_PREFIX}{group['name']}"] + \
            _encode_schedule_as_tags(group, prefix=_GROUP_SCHEDULE_TAG_PREFIX)
    if new_tags != (ip.tags or []):
        ip.tags = new_tags
        ip.save()
        _verify_reserved_ip_tag_write(client, record["reserved_ip"], new_tags)


def _sync_group_membership_tags(
    client, name: str, group: dict | None, *, on_warning: Callable[[str], None] | None = None
) -> None:

    try:


        with _instance_lock(name):


            registry = load_registry()
            record = registry.get(name)
            if record is None or not record.get("reserved_ip"):
                return
            _sync_group_membership_tags_locked(client, name, record, group)
    except Exception as e:
        if on_warning is not None:
            on_warning(
                f"  WARNING: could not sync group membership tags for '{name}' onto its "
                f"reserved IP ({e}) -- group membership itself is saved locally and fully in "
                "effect, but won't be recoverable via `rebuild` if the local database is lost "
                "before this is retried (safe to retry: re-run group-add for this instance)."
            )


_SIMPLE_NETWORK_CONFIG_TAG_PREFIX = "net-"
_SSH_KEY_TAG_PREFIX = "sshkeys"


_MAX_TAG_LENGTH = 50


def _is_extra_recovery_tag(tag: str) -> bool:

    if tag.startswith(_SIMPLE_NETWORK_CONFIG_TAG_PREFIX):
        return True
    if tag.startswith(_SSH_KEY_TAG_PREFIX):
        suffix = tag.partition(":")[0][len(_SSH_KEY_TAG_PREFIX):]
        return suffix.isdigit()
    return False


def _encode_simple_network_config_as_tags(
    network_interface_model: str | None, network_config: list | None, network_helper_enabled: bool | None
) -> list[str]:

    if network_interface_model != "legacy_config" or network_helper_enabled is None:
        return []
    if not isinstance(network_config, list) or len(network_config) != 1:
        return []
    iface = network_config[0]
    if not isinstance(iface, dict) or set(iface.keys()) - {"purpose", "primary"}:
        return []
    if iface.get("purpose") != "public":
        return []
    primary = 1 if iface.get("primary") else 0
    return [
        f"{_SIMPLE_NETWORK_CONFIG_TAG_PREFIX}if:pub-{primary}",
        f"{_SIMPLE_NETWORK_CONFIG_TAG_PREFIX}nh:{1 if network_helper_enabled else 0}",
    ]


def _decode_simple_network_config_from_tags(tags: list[str] | None) -> dict | None:

    if_prefix, nh_prefix = f"{_SIMPLE_NETWORK_CONFIG_TAG_PREFIX}if:", f"{_SIMPLE_NETWORK_CONFIG_TAG_PREFIX}nh:"
    primary = None
    network_helper_enabled = None
    for tag in tags or []:
        if tag.startswith(if_prefix):
            value = tag[len(if_prefix):]
            if value == "pub-0":
                primary = False
            elif value == "pub-1":
                primary = True
        elif tag.startswith(nh_prefix):
            network_helper_enabled = tag[len(nh_prefix):] == "1"
    if primary is None or network_helper_enabled is None:
        return None
    return {
        "network_interface_model": "legacy_config",
        "network_config": [{"purpose": "public", "primary": primary}],
        "network_helper_enabled": network_helper_enabled,
    }


def _classify_authorized_keys(client, raw_keys: list[str]) -> tuple[list[int], list[str]]:

    if not raw_keys:
        return [], []
    try:
        registered = {
            k.ssh_key: k.id
            for k in engine.retry_transient(lambda: list(client.profile.ssh_keys()))
        }
    except Exception:
        return [], list(raw_keys)
    matched_ids: list[int] = []
    unmatched: list[str] = []
    for key in raw_keys:
        key_id = registered.get(key)
        if key_id is not None:
            matched_ids.append(key_id)
        else:
            unmatched.append(key)
    return matched_ids, unmatched


def _encode_ssh_key_ids_as_tags(key_ids: list[int]) -> list[str]:

    tags: list[str] = []
    current: list[str] = []
    index = 0
    for key_id in key_ids:
        candidate = [*current, str(key_id)]
        candidate_tag = f"{_SSH_KEY_TAG_PREFIX}{index}:{'-'.join(candidate)}"
        if len(candidate_tag) > _MAX_TAG_LENGTH and current:
            tags.append(f"{_SSH_KEY_TAG_PREFIX}{index}:{'-'.join(current)}")
            index += 1
            current = [str(key_id)]
        else:
            current = candidate
    if current:
        tags.append(f"{_SSH_KEY_TAG_PREFIX}{index}:{'-'.join(current)}")
    return tags


def _decode_ssh_key_ids_from_tags(tags: list[str] | None) -> list[int]:

    ids: list[int] = []
    for tag in tags or []:
        prefix_part, sep, rest = tag.partition(":")
        if not sep or not prefix_part.startswith(_SSH_KEY_TAG_PREFIX):
            continue
        if not prefix_part[len(_SSH_KEY_TAG_PREFIX):].isdigit():
            continue
        for id_str in rest.split("-"):
            if id_str.isdigit():
                ids.append(int(id_str))
    return ids


def _resolve_ssh_key_ids_to_content(client, key_ids: list[int]) -> list[str]:

    if not key_ids:
        return []
    try:
        registered = {
            k.id: k.ssh_key
            for k in engine.retry_transient(lambda: list(client.profile.ssh_keys()))
        }
    except Exception:
        return []
    return [registered[key_id] for key_id in key_ids if key_id in registered]


def _sync_extra_recovery_tags_locked(client, name: str, record: dict, ssh_key_ids: list[int]) -> None:

    ip = engine.retry_transient(
        lambda: client.load(engine.ReservedIPAddress, record["reserved_ip"])
    )
    kept = [t for t in (ip.tags or []) if not _is_extra_recovery_tag(t)]
    new_extra = (
        _encode_simple_network_config_as_tags(
            record.get("network_interface_model"), record.get("network_config"),
            record.get("network_helper_enabled"),
        )
        + _encode_ssh_key_ids_as_tags(ssh_key_ids)
    )
    new_tags = kept + new_extra
    if new_tags != (ip.tags or []):
        ip.tags = new_tags
        ip.save()
        _verify_reserved_ip_tag_write(client, record["reserved_ip"], new_tags)


@dataclass
class BackupResult:

    object_storage_configured: bool = False
    instances_synced: list[str] = field(default_factory=list)
    instances_failed: list[str] = field(default_factory=list)
    object_storage_snapshot_key: str | None = None
    object_storage_snapshot_error: str | None = None
    local_snapshot_path: str | None = None
    local_snapshot_error: str | None = None

    @property
    def ok(self) -> bool:

        return not (
            self.instances_failed
            or self.object_storage_snapshot_error
            or self.local_snapshot_error
        )


def backup_full_system(
    *, local_dir: Path | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> BackupResult:

    result = BackupResult(object_storage_configured=osb.is_configured())

    if result.object_storage_configured:
        registry = load_registry()
        if on_progress is not None:
            on_progress(f"Re-syncing {len(registry)} instance record(s) to Object Storage...")
        for name, record in registry.items():
            try:
                osb.upload_instance_backup(name, record)
                result.instances_synced.append(name)
            except Exception as e:
                result.instances_failed.append(name)
                if on_warning is not None:
                    on_warning(f"  WARNING: could not back up '{name}' to Object Storage ({e})")

        if on_progress is not None:
            on_progress("Uploading a full database snapshot to Object Storage...")
        try:
            result.object_storage_snapshot_key = osb.upload_database_snapshot(REGISTRY_PATH)
        except Exception as e:
            result.object_storage_snapshot_error = str(e)
            if on_warning is not None:
                on_warning(f"  WARNING: could not upload full database snapshot ({e})")
    elif on_warning is not None:
        on_warning(
            "  WARNING: Object Storage is not configured (LINODE_OBJ_STORAGE_* unset) -- "
            "skipping the per-instance Object Storage re-sync and snapshot upload. See "
            "docs/OPERATIONS.md to set this up."
        )

    if local_dir is not None:
        local_dir.mkdir(parents=True, exist_ok=True)
        local_path = local_dir / f"instances-{int(time.time())}.db"
        if on_progress is not None:
            on_progress(f"Writing a full database snapshot to {local_path}...")
        try:
            osb.snapshot_database(REGISTRY_PATH, local_path)
            result.local_snapshot_path = str(local_path)
        except Exception as e:
            result.local_snapshot_error = str(e)
            if on_warning is not None:
                on_warning(f"  WARNING: could not write local database snapshot ({e})")

    return result


def cmd_backup(args) -> int:
    if not osb.is_configured() and not args.backup_dir:
        print(
            "Configuration error: nothing to back up to -- Object Storage isn't configured "
            "(LINODE_OBJ_STORAGE_* unset in .env) and no --backup-dir was given. Configure "
            "Object Storage (see docs/OPERATIONS.md) or pass --backup-dir.",
            file=sys.stderr,
        )
        return 1

    local_dir = Path(args.backup_dir) if args.backup_dir else None
    result = backup_full_system(
        local_dir=local_dir, on_progress=print, on_warning=_print_to_stderr,
    )

    if result.object_storage_configured:
        summary = f"Object Storage: {len(result.instances_synced)} instance record(s) re-synced"
        if result.instances_failed:
            summary += f", {len(result.instances_failed)} failed"
        print(summary + ".")
        if result.object_storage_snapshot_key:
            print(
                f"Object Storage: full database snapshot uploaded as "
                f"'{result.object_storage_snapshot_key}'."
            )
    if result.local_snapshot_path:
        print(f"Local snapshot written to {result.local_snapshot_path}.")

    return 0 if result.ok else 1


def _set_group_schedule_row(group_name: str, timezone: str, rules: list, enabled: bool) -> None:

    def _do():
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE schedule_groups SET timezone = ?, rules = ?, enabled = ? WHERE name = ?",
                (timezone, json.dumps(rules), 1 if enabled else 0, group_name),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    if _retry_db(_do) == 0:
        raise GroupNotFoundError(f"no schedule group named '{group_name}' exists.")


def set_group_schedule(
    client, group_name: str, timezone: str, rules: list, enabled: bool = True,
    on_warning: Callable[[str], None] | None = None,
) -> None:

    _validate_schedule_rules(rules)
    _validate_schedule_timezone(timezone)
    _set_group_schedule_row(group_name, timezone, rules, enabled)

    group = get_schedule_group(group_name)
    if group is None:


        raise GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
    for member in group["members"]:
        _sync_group_membership_tags(client, member, group, on_warning=on_warning)


def delete_schedule_group(group_name: str) -> None:

    def _do():
        conn = _connect()
        try:
            group_id = _group_id_for_name(conn, group_name)
            members = [r[0] for r in conn.execute(
                "SELECT name FROM instances WHERE group_id = ?", (group_id,)
            ).fetchall()]
            if members:
                raise engine.ConfigError(
                    f"group '{group_name}' still has {len(members)} member(s): "
                    f"{', '.join(members)} -- run `group-remove` for each one first."
                )
            try:
                conn.execute("DELETE FROM schedule_groups WHERE id = ?", (group_id,))
                conn.commit()
            except sqlite3.IntegrityError as e:


                raise engine.ConfigError(
                    f"group '{group_name}' gained a new member while being deleted (a rare race "
                    "with a concurrent `group-add`) -- re-run `group-delete` once that's "
                    "resolved."
                ) from e
        finally:
            conn.close()

    _retry_db(_do)


def assign_instance_to_group(
    client, name: str, group_name: str, on_warning: Callable[[str], None] | None = None
) -> int:

    with _instance_lock(name):
        registry = load_registry()
        if name not in registry:
            raise NotOnboardedError(f"'{name}' is not onboarded. Run `onboard` first.")

        def _do():
            conn = _connect()
            try:
                group_id = _group_id_for_name(conn, group_name)
                conn.execute("UPDATE instances SET group_id = ? WHERE name = ?", (group_id, name))
                conn.commit()
            finally:
                conn.close()

        _retry_db(_do)
    group = get_schedule_group(group_name)
    if group is None:


        raise GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
    _sync_group_membership_tags(client, name, group, on_warning=on_warning)
    return group["id"]


def remove_instance_from_group(
    client, name: str, *, copy_group_rules_as_individual: bool,
    on_warning: Callable[[str], None] | None = None,
) -> str:

    with _instance_lock(name):
        registry = load_registry()
        if name not in registry:
            raise NotOnboardedError(f"'{name}' is not onboarded. Run `onboard` first.")
        record = registry[name]
        if record.get("group_id") is None:
            raise engine.ConfigError(f"'{name}' is not currently in any group.")

        existing_schedule = get_instance_schedule(name)


        if schedule_is_active(existing_schedule):
            outcome = "removed_had_individual_schedule"
        elif copy_group_rules_as_individual:
            outcome = "removed_copied_schedule"
        else:
            outcome = "removed_kept_manual"

        group_id = record["group_id"]


        group = _group_row_by_id(group_id) if outcome == "removed_copied_schedule" else None
        if outcome == "removed_copied_schedule" and group is not None:
            _validate_schedule_rules(group["rules"])
            _validate_schedule_timezone(group["timezone"])

        def _do():
            conn = _connect()
            try:
                conn.execute("UPDATE instances SET group_id = NULL WHERE name = ?", (name,))
                conn.commit()
            finally:
                conn.close()

        _retry_db(_do)
    _sync_group_membership_tags(client, name, None, on_warning=on_warning)

    if outcome == "removed_copied_schedule":
        if group is None:


            if on_warning is not None:
                on_warning(
                    f"  WARNING: '{name}'s group was deleted concurrently -- nothing to copy, "
                    "left as manual-only."
                )
            return "removed_kept_manual"
        save_instance_schedule(
            client, name, group["timezone"], group["rules"],


            enabled=group.get("enabled", True), on_warning=on_warning,
        )

    return outcome


def cmd_group_create(args) -> int:
    try:
        group_id = create_schedule_group(args.name, args.timezone)
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"Created group '{args.name}' (id {group_id}), timezone {args.timezone}, no rules yet "
          f"-- use group-schedule-set to give it one.")
    return 0


def cmd_group_schedule_set(client, args) -> int:
    rules = _parse_schedule_rules_from_cli_args(args)
    if rules is None:
        return 1

    try:
        set_group_schedule(
            client, args.group_name, args.timezone, rules,
            enabled=not args.disabled, on_warning=_print_to_stderr,
        )
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"Schedule set for group '{args.group_name}': {len(rules)} rule(s), "
          f"{'enabled' if not args.disabled else 'disabled'}.")
    return 0


def cmd_group_show(args) -> int:
    group = get_schedule_group(args.group_name)
    if group is None:
        print(f"Configuration error: no schedule group named '{args.group_name}' exists.", file=sys.stderr)
        return 1
    print(json.dumps(group, indent=2))
    return 0


def cmd_group_list(args) -> int:
    groups = list_schedule_groups()
    if not groups:
        print("No schedule groups exist yet -- create one with group-create.")
        return 0
    for g in groups:
        state = "enabled" if g["enabled"] else "disabled"
        print(f"{g['name']} (id {g['id']}, {g['timezone']}, {len(g['rules'])} rule(s), {state}, "
              f"{g['member_count']} member(s))")
    return 0


def cmd_group_delete(args) -> int:
    try:
        delete_schedule_group(args.group_name)
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"Group '{args.group_name}' deleted.")
    return 0


def cmd_group_add(client, args) -> int:
    try:
        assign_instance_to_group(client, args.name, args.group_name, on_warning=_print_to_stderr)
    except NotOnboardedError as e:
        print(f"{e}", file=sys.stderr)
        return 1


    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"'{args.name}' added to group '{args.group_name}'.")
    return 0


def cmd_group_remove(client, args) -> int:
    try:
        registry = load_registry()
        if args.name not in registry:
            raise NotOnboardedError(f"'{args.name}' is not onboarded. Run `onboard` first.")
        record = registry[args.name]
        if record.get("group_id") is None:
            raise engine.ConfigError(f"'{args.name}' is not currently in any group.")

        copy_rules = args.copy_schedule
        if (
            not args.copy_schedule and not args.keep_manual


            and not schedule_is_active(get_instance_schedule(args.name)) and not args.yes
        ):
            group = _group_row_by_id(record["group_id"])
            group_label = group["name"] if group else "its group"
            answer = input(
                f"'{args.name}' has no individual schedule of its own and is governed entirely "
                f"by {group_label!r}'s schedule. Removing it from the group would leave it with "
                f"NO schedule at all unless you choose otherwise now.\n"
                f"Copy {group_label!r}'s current rules into a new individual schedule for "
                f"'{args.name}'? [y/N] "
            ).strip().lower()
            copy_rules = answer == "y"

        outcome = remove_instance_from_group(
            client, args.name, copy_group_rules_as_individual=copy_rules,
            on_warning=_print_to_stderr,
        )
    except NotOnboardedError as e:
        print(f"{e}", file=sys.stderr)
        return 1


    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1

    if outcome == "removed_had_individual_schedule":
        print(f"'{args.name}' removed from its group -- unaffected, it already has its own "
              f"individual schedule.")
    elif outcome == "removed_copied_schedule":
        print(f"'{args.name}' removed from its group -- the group's rules were copied into a "
              f"new individual schedule for it.")
    else:
        print(f"'{args.name}' removed from its group -- now manual-only (no schedule).")
    return 0


DEFAULT_POLL_WINDOW_SECONDS = 300


MIN_POLL_SLEEP_SECONDS = 2.0


DEFAULT_MANUAL_OVERRIDE_WINDOW_HOURS = 2.0


MAX_MANUAL_OVERRIDE_WINDOW_HOURS = 24 * 365 * 10


def _validate_override_window_hours(hours: float | None) -> None:

    if hours is None:
        return
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours):
        raise engine.ConfigError(f"override window hours must be a positive number, got {hours!r}.")
    if not (0 < hours <= MAX_MANUAL_OVERRIDE_WINDOW_HOURS):
        raise engine.ConfigError(
            f"override window hours must be greater than 0 and at most "
            f"{MAX_MANUAL_OVERRIDE_WINDOW_HOURS} (got {hours}) -- use a schedule instead for "
            "anything longer than that."
        )


def _local_time_to_utc(zone: ZoneInfo, day: date, time_str: str) -> datetime:

    hour, minute = (int(p) for p in time_str.split(":"))
    return datetime.combine(day, dtime(hour, minute), tzinfo=zone).astimezone(UTC)


def resolve_due_action(
    schedule: dict, now: datetime, window_seconds: int = DEFAULT_POLL_WINDOW_SECONDS,
) -> Literal["create", "delete", None]:

    if not schedule.get("enabled", True):
        return None
    zone = ZoneInfo(schedule["timezone"])
    local_now = now.astimezone(zone)
    weekday = VALID_SCHEDULE_DAYS[local_now.weekday()]
    today = local_now.date()


    matched: list[Literal["create", "delete"]] = []
    for rule in schedule.get("rules", []):
        if weekday not in rule["days_of_week"]:
            continue
        candidates: list[tuple[str, Literal["create", "delete"]]] = [
            (rule["start_time"], "create"), (rule["stop_time"], "delete"),
        ]
        for time_str, action in candidates:


            window_start_utc = _local_time_to_utc(zone, today, time_str)
            window_end_utc = window_start_utc + timedelta(seconds=window_seconds)
            if window_start_utc <= now.astimezone(UTC) < window_end_utc:
                matched.append(action)


    if "delete" in matched:
        return "delete"
    if "create" in matched:
        return "create"
    return None


def schedule_is_active(schedule: dict | None) -> TypeGuard[dict]:

    return schedule is not None and schedule.get("enabled", True)


def is_within_scheduled_on_window(schedule: dict, now: datetime) -> bool:

    if not schedule.get("enabled", True):
        return False
    zone = ZoneInfo(schedule["timezone"])
    local_now = now.astimezone(zone)
    weekday = VALID_SCHEDULE_DAYS[local_now.weekday()]
    today = local_now.date()
    now_utc = now.astimezone(UTC)
    for rule in schedule.get("rules", []):
        if weekday not in rule["days_of_week"]:
            continue
        start_utc = _local_time_to_utc(zone, today, rule["start_time"])
        stop_utc = _local_time_to_utc(zone, today, rule["stop_time"])
        if start_utc <= now_utc < stop_utc:
            return True
    return False


def resolve_effective_schedule(name: str, record: dict) -> tuple[dict | None, str | None]:

    schedule = get_instance_schedule(name)
    if schedule_is_active(schedule):
        return schedule, None
    if record.get("group_id") is not None:
        group = _group_row_by_id(record["group_id"])
        if group is not None and group["rules"]:
            return (
                {
                    "timezone": group["timezone"], "rules": group["rules"],
                    "enabled": group["enabled"],
                },
                group["name"],
            )
    if schedule is not None:
        return schedule, None
    return None, None


def compute_scheduled_savings_percent(schedule: dict) -> float | None:

    rules = schedule.get("rules") or []
    if not rules:
        return None
    total_hours_per_week = 0.0
    for rule in rules:
        start_h, start_m = (int(p) for p in rule["start_time"].split(":"))
        stop_h, stop_m = (int(p) for p in rule["stop_time"].split(":"))
        hours_per_occurrence = (stop_h * 60 + stop_m - (start_h * 60 + start_m)) / 60
        total_hours_per_week += hours_per_occurrence * len(rule["days_of_week"])
    return round(max(0.0, (1 - total_hours_per_week / 168) * 100), 1)


def compute_actual_savings_percent(
    events: list[dict], window_start: datetime, window_end: datetime,
) -> float | None:

    successes = [e for e in events if e["result"] == "success"]
    if not successes:
        return None
    uptime_seconds = 0.0
    up_since: datetime | None = None
    seen_any_event_in_window = False
    for event in successes:
        ts = datetime.fromisoformat(event["timestamp"])
        if ts < window_start or ts >= window_end:
            continue
        if event["action"] == "create":
            if up_since is None:
                up_since = ts


            seen_any_event_in_window = True
        elif event["action"] == "delete":
            if up_since is not None:
                uptime_seconds += (ts - up_since).total_seconds()
                up_since = None
            elif not seen_any_event_in_window:
                uptime_seconds += (ts - window_start).total_seconds()


            seen_any_event_in_window = True
    if up_since is not None:
        uptime_seconds += (window_end - up_since).total_seconds()
    window_seconds = (window_end - window_start).total_seconds()
    if window_seconds <= 0:
        return None
    return round(max(0.0, (1 - uptime_seconds / window_seconds) * 100), 1)


@dataclass
class PollTickInstanceResult:

    name: str
    outcome: Literal[
        "no_schedule", "disabled", "not_due", "already_transitioning", "already_fired_today",
        "fired_success", "fired_noop", "fired_failure", "error", "auto_revert_window_reopened",
    ]
    action: Literal["create", "delete"] | None = None
    detail: str | None = None
    via_group: str | None = None


    via_auto_revert: bool = False


@dataclass
class PollTickResult:

    results: list[PollTickInstanceResult] = field(default_factory=list)

    @property
    def fired_count(self) -> int:
        return sum(1 for r in self.results if r.outcome == "fired_success")

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if r.outcome in ("fired_failure", "error"))


_POLL_NOOP_START_OUTCOMES = frozenset({"already_running"})
_POLL_NOOP_STOP_OUTCOMES = frozenset({"already_stopped", "aborted_by_user"})


def poll_tick(
    client, ssh_key: str, *,
    window_seconds: int = DEFAULT_POLL_WINDOW_SECONDS,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> PollTickResult:

    results: list[PollTickInstanceResult] = []
    registry = load_registry()
    for name, record in registry.items():
        action: Literal["create", "delete"] | None = None
        via_group: str | None = None


        via_auto_revert = False
        try:
            expires_at_raw = record.get("manual_override_expires_at")
            is_due_for_revert = False
            if record.get("current_status") == "running" and expires_at_raw:


                action = "delete"
                via_auto_revert = True
                is_due_for_revert = datetime.now(UTC) >= datetime.fromisoformat(expires_at_raw)
                if not is_due_for_revert:
                    action = None
                    via_auto_revert = False
            if is_due_for_revert:
                if record.get("transitioning"):


                    results.append(PollTickInstanceResult(
                        name, "already_transitioning", action="delete", via_auto_revert=True,
                    ))
                    continue


                effective_schedule, _via_group_for_revert = resolve_effective_schedule(name, record)
                if schedule_is_active(effective_schedule) and is_within_scheduled_on_window(
                    effective_schedule, datetime.now(UTC),
                ):
                    timer_cleared = False
                    with _instance_lock(name):
                        fresh = load_registry().get(name)
                        if fresh is not None and fresh.get("manual_override_expires_at") == expires_at_raw:
                            fresh["manual_override_expires_at"] = None
                            _save_one_record(name, fresh)
                            timer_cleared = True


                    if timer_cleared:
                        if on_progress is not None:
                            on_progress(
                                f"'{name}': manual override expiry reached, but the instance is now "
                                "inside its own scheduled on-window -- clearing the timer instead of "
                                "stopping."
                            )
                        results.append(PollTickInstanceResult(
                            name, "auto_revert_window_reopened", via_auto_revert=True,
                        ))
                    else:


                        results.append(PollTickInstanceResult(
                            name, "not_due", via_auto_revert=True,
                            detail="manual override expiry was concurrently changed; skipped "
                            "this tick.",
                        ))
                    continue


                with _instance_lock(name):
                    fresh_for_revert = load_registry().get(name)
                    still_due = (
                        fresh_for_revert is not None
                        and fresh_for_revert.get("manual_override_expires_at") == expires_at_raw
                    )
                if not still_due:
                    results.append(PollTickInstanceResult(
                        name, "not_due", via_auto_revert=True,
                        detail="manual override expiry was concurrently changed; skipped "
                        "this tick.",
                    ))
                    continue
                if on_progress is not None:
                    on_progress(f"'{name}': manual override expired, auto-reverting (stop)...")
                revert_result = stop_instance(
                    client, name, ssh_key, triggered_by="schedule",
                    on_progress=on_progress, on_warning=on_warning,
                )
                revert_event_result = _STOP_EVENT_RESULTS.get(revert_result.outcome)
                if revert_event_result == "success":
                    results.append(PollTickInstanceResult(
                        name, "fired_success", action="delete", via_auto_revert=True,
                    ))
                else:
                    results.append(PollTickInstanceResult(
                        name, "fired_failure", action="delete",
                        detail=f"outcome={revert_result.outcome}", via_auto_revert=True,
                    ))
                continue

            schedule, via_group = resolve_effective_schedule(name, record)
            if schedule is None:
                results.append(PollTickInstanceResult(name, "no_schedule"))
                continue
            if not schedule.get("enabled", True):
                results.append(PollTickInstanceResult(name, "disabled", via_group=via_group))
                continue
            action = resolve_due_action(schedule, datetime.now(UTC), window_seconds)
            if action is None:
                results.append(PollTickInstanceResult(name, "not_due", via_group=via_group))
                continue
            if record.get("transitioning"):


                results.append(PollTickInstanceResult(
                    name, "already_transitioning", action=action, via_group=via_group,
                ))
                continue
            if event_already_succeeded_today(name, action, schedule["timezone"]):
                results.append(PollTickInstanceResult(
                    name, "already_fired_today", action=action, via_group=via_group,
                ))
                continue

            if on_progress is not None:
                via = f" via group '{via_group}'" if via_group else ""
                on_progress(f"'{name}': {action} due{via}, firing (triggered_by=schedule)...")
            op_result: StartResult | StopResult
            if action == "create":
                op_result = start_instance(
                    client, name, ssh_key, triggered_by="schedule",
                    on_progress=on_progress, on_warning=on_warning,
                )
                event_result = _START_EVENT_RESULTS.get(op_result.outcome)
                is_noop = op_result.outcome in _POLL_NOOP_START_OUTCOMES
            else:
                op_result = stop_instance(
                    client, name, ssh_key, triggered_by="schedule",
                    on_progress=on_progress, on_warning=on_warning,
                )
                event_result = _STOP_EVENT_RESULTS.get(op_result.outcome)
                is_noop = op_result.outcome in _POLL_NOOP_STOP_OUTCOMES


            if event_result == "success":
                results.append(PollTickInstanceResult(
                    name, "fired_success", action=action, via_group=via_group,
                ))
            elif is_noop:
                results.append(PollTickInstanceResult(
                    name, "fired_noop", action=action, detail=f"outcome={op_result.outcome}",
                    via_group=via_group,
                ))
            else:
                results.append(PollTickInstanceResult(
                    name, "fired_failure", action=action, detail=f"outcome={op_result.outcome}",
                    via_group=via_group,
                ))
        except Exception as e:


            results.append(PollTickInstanceResult(
                name, "error", action=action, detail=str(e), via_group=via_group,
                via_auto_revert=via_auto_revert,
            ))
    return PollTickResult(results=results)


def cmd_poll(client, args) -> int:
    def _report_tick(tick: PollTickResult) -> None:
        for r in tick.results:
            if r.outcome in (
                "fired_success", "fired_failure", "error", "auto_revert_window_reopened",
            ):
                detail = f" ({r.detail})" if r.detail else ""
                via = f" via_group={r.via_group}" if r.via_group else ""
                revert = " auto_revert=true" if r.via_auto_revert else ""
                print(f"  {r.name}: {r.outcome} action={r.action}{via}{revert}{detail}")
        if tick.results:
            print(f"Tick complete: {tick.fired_count} fired, {tick.failed_count} failed, "
                  f"{len(tick.results)} instance(s) checked.")

    if args.once:
        tick = poll_tick(
            client, args.ssh_key, window_seconds=args.window_seconds,
            on_progress=print, on_warning=_print_to_stderr,
        )
        _report_tick(tick)
        return 1 if tick.failed_count else 0


    if args.window_seconds < args.interval_seconds:
        print(
            f"Configuration error: --window-seconds ({args.window_seconds}) is smaller than "
            f"--interval-seconds ({args.interval_seconds}) -- a real gap would exist between "
            "ticks where a scheduled action could be silently skipped for an entire day. Raise "
            "--window-seconds to at least --interval-seconds, or lower --interval-seconds.",
            file=sys.stderr,
        )
        return 1

    print(f"Polling every {args.interval_seconds}s (Ctrl-C to stop)...")
    try:
        while True:
            tick_started = time.monotonic()
            tick = poll_tick(
                client, args.ssh_key, window_seconds=args.window_seconds,
                on_progress=print, on_warning=_print_to_stderr,
            )
            _report_tick(tick)
            elapsed = time.monotonic() - tick_started


            if elapsed > args.interval_seconds:
                print(
                    f"WARNING: this tick took {elapsed:.1f}s, longer than the "
                    f"{args.interval_seconds}s poll interval -- a schedule's match window may "
                    "have been skipped entirely this cycle.", file=sys.stderr,
                )


            time.sleep(max(MIN_POLL_SLEEP_SECONDS, args.interval_seconds - elapsed))
    except KeyboardInterrupt:
        print("Stopped.")
        return 0


def cmd_serve_api(args) -> int:

    import api_server

    try:
        api_server.run_server(args.host, args.port)
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    return 0


def list_instances() -> dict:

    return load_registry()


def cmd_list(args) -> int:
    registry = list_instances()
    if not registry:
        print("No instances onboarded yet.")
        return 0
    for name, record in registry.items():
        lock = " [LOCKED]" if record.get("transitioning") else ""
        override = ""
        if record.get("manual_override_expires_at"):
            override = f"  [override expires {_format_override_expiry(record['manual_override_expires_at'])}]"
        print(f"{name}: {record['current_status']}{lock}  "
              f"ip={record['reserved_ip']}  region={record['region']}  "
              f"linode_id={record['current_linode_id']}{override}")
    return 0


def get_instance_status(name: str) -> dict:

    registry = load_registry()
    record = registry.get(name)
    if record is None:
        raise NotOnboardedError(f"'{name}' is not onboarded.")
    return record


def cmd_status(args) -> int:
    try:
        record = get_instance_status(args.name)
    except NotOnboardedError as e:


        print(f"{e}", file=sys.stderr)
        return 1
    if record.get("manual_override_expires_at"):


        print(f"'{args.name}': manually started outside its scheduled hours, auto-stops at "
              f"{_format_override_expiry(record['manual_override_expires_at'])} unless extended "
              f"(`extend --name {args.name}`).")
    print(json.dumps(record, indent=2, default=str))
    return 0


def cmd_history(args) -> int:

    events = get_schedule_events(args.name, limit=args.limit)
    if not events:
        print(f"No events recorded for '{args.name}'.")
        return 0
    for event in events:
        line = (
            f"{event['timestamp']}  {event['action']:6}  {event['result']:7}  "
            f"triggered_by={event['triggered_by']}"
        )
        if event.get("actor"):
            line += f"  actor={event['actor']}"
        if event["error_message"]:
            line += f"  ({event['error_message']})"
        print(line)
    return 0


@dataclass
class ClearLockResult:

    outcome: Literal["not_locked", "aborted_by_user", "cleared"]


def clear_instance_lock(
    name: str, *,
    confirm: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> ClearLockResult:

    with _instance_lock(name):
        registry = load_registry()
        record = registry.get(name)
        if record is None:
            raise NotOnboardedError(f"'{name}' is not onboarded.")
        if not record.get("transitioning"):
            return ClearLockResult(outcome="not_locked")
        if on_progress is not None:
            on_progress(
                "WARNING: this forcibly clears the transitioning lock without confirming the "
                "operation it was protecting actually finished. Only do this if you've "
                f"independently confirmed (e.g. via Cloud Manager) that no create/delete is "
                f"genuinely still in flight for '{name}'."
            )
        if confirm is not None and not confirm():
            return ClearLockResult(outcome="aborted_by_user")


        engine.release_transition_lock(record, persist_fn=lambda r: _save_one_record(name, r))
        return ClearLockResult(outcome="cleared")


def cmd_clear_lock(args) -> int:
    def _confirm() -> bool:
        answer = input(f"Clear the lock on '{args.name}' anyway? [y/N] ")
        return answer.strip().lower() == "y"

    try:
        result = clear_instance_lock(
            args.name, confirm=None if args.yes else _confirm, on_progress=print,
        )
    except InstanceLockedError as e:


        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except NotOnboardedError as e:
        print(f"{e}", file=sys.stderr)
        return 1
    if result.outcome == "not_locked":
        print(f"'{args.name}' is not locked -- nothing to do.")
        return 0
    if result.outcome == "aborted_by_user":
        print("Aborted.")
        return 1
    print("Lock cleared.")
    return 0


@dataclass
class DeregisterResult:

    outcome: Literal["not_onboarded", "aborted_by_user", "deregistered"]


def deregister_instance(
    name: str, *,
    confirm: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> DeregisterResult:

    with _instance_lock(name):
        registry = load_registry()
        if name not in registry:
            return DeregisterResult(outcome="not_onboarded")
        if on_progress is not None:
            on_progress(
                f"This removes '{name}' from this tool's own tracking only -- nothing on "
                "Linode changes (the instance, its volumes, its reserved IP, and its tags are "
                "all left exactly as they are). Use this when the local record itself is wrong "
                "or unsafe, not as a way to decommission the instance (use offboard for that)."
            )
        if confirm is not None and not confirm():
            return DeregisterResult(outcome="aborted_by_user")
        _delete_one_record(name)
        return DeregisterResult(outcome="deregistered")


def cmd_deregister(args) -> int:
    def _confirm() -> bool:
        answer = input(f"Remove '{args.name}' from this tool's tracking? [y/N] ")
        return answer.strip().lower() == "y"


    try:
        result = deregister_instance(
            args.name, confirm=None if args.yes else _confirm, on_progress=print,
        )
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result.outcome == "not_onboarded":
        print(f"'{args.name}' is not onboarded -- nothing to do.")
        return 0
    if result.outcome == "aborted_by_user":
        print("Aborted.")
        return 1
    print(f"'{args.name}' removed from tracking. The Linode instance itself was not touched.")
    return 0


@dataclass
class ResetHostKeyResult:

    outcome: Literal["aborted_by_user", "reset"]
    confirm_label: str | None = None
    reserved_ip: str | None = None


def reset_host_key(
    name: str | None, ip: str | None, ssh_key: str, *,
    confirm: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> ResetHostKeyResult:

    if bool(name) == bool(ip):
        raise engine.ConfigError("pass exactly one of --name or --ip.")

    if name:
        with _instance_lock(name):
            registry = load_registry()
            record = registry.get(name)
            if record is None:
                raise engine.ConfigError(
                    f"'{name}' is not onboarded. Use --ip instead for a node that isn't "
                    "onboarded yet."
                )
            reserved_ip = record.get("reserved_ip")
            if not reserved_ip:
                raise engine.ConfigError(f"'{name}' has no reserved_ip on record.")
            return _reset_host_key_confirm_and_apply(
                confirm_label=name, reserved_ip=reserved_ip, ssh_key=ssh_key,
                confirm=confirm, on_progress=on_progress,
            )
    assert ip is not None
    return _reset_host_key_confirm_and_apply(
        confirm_label=ip, reserved_ip=ip, ssh_key=ssh_key,
        confirm=confirm, on_progress=on_progress,
    )


def _reset_host_key_confirm_and_apply(
    *, confirm_label: str, reserved_ip: str, ssh_key: str,
    confirm: Callable[[], bool] | None,
    on_progress: Callable[[str], None] | None,
) -> ResetHostKeyResult:
    if on_progress is not None:
        on_progress(f"This will forget the SSH host key currently trusted for '{confirm_label}' "
                    f"({reserved_ip}) and trust whatever key it presents next.")
        on_progress("Only do this after independently confirming the new key is legitimate "
                    "(e.g. via Cloud Manager's Lish console) -- this is exactly the check that "
                    "would otherwise catch a MITM or a wrong-disk mistake, so don't bypass it "
                    "casually.")
    if confirm is not None and not confirm():
        return ResetHostKeyResult(outcome="aborted_by_user")


    with _instance_lock(f"known-host-{reserved_ip}"):


        had_backup = engine.read_known_host_entry(reserved_ip) is not None
        try:
            engine.reset_and_reestablish_trust(reserved_ip, ssh_key)
        except (ApiError, RuntimeError) as e:
            raise engine.ConfigError(
                f"could not reach '{confirm_label}' to establish new trust: {e}."
                + (" The previously-trusted key was restored -- nothing was left in a worse "
                   "state than before this command ran." if had_backup else " There was no "
                   "previous entry to restore -- nothing is trusted for this address yet.")
            ) from e
        return ResetHostKeyResult(outcome="reset", confirm_label=confirm_label, reserved_ip=reserved_ip)


def cmd_reset_host_key(args) -> int:
    def _confirm() -> bool:
        label = args.name if args.name else args.ip
        answer = input(f"Type '{label}' to confirm: ")
        return answer == label

    try:
        result = reset_host_key(
            args.name, args.ip, args.ssh_key,
            confirm=None if args.yes else _confirm,
            on_progress=print,
        )
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result.outcome == "aborted_by_user":
        print("Aborted -- name didn't match.")
        return 1
    print(f"New host key for '{result.confirm_label}' ({result.reserved_ip}) is now trusted.")
    return 0


@dataclass
class RebuildResult:

    nothing_found: bool = False
    scanned_count: int = 0
    recovered: list[str] = field(default_factory=list)
    partial: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    incomplete: list[tuple[str, dict]] = field(default_factory=list)
    ambiguous: list[tuple[str, dict]] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:

        return not (self.failed or self.incomplete or self.ambiguous or self.invalid or self.conflicts)


def rebuild_instances(
    client, ssh_key: str, *,
    vpc_id: int | None = None, force: bool = False,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> RebuildResult:

    registry = load_registry()
    try:
        found, conflicts = engine.find_managed_resources_by_tag(client)
    except (ApiError, requests.exceptions.RequestException) as e:


        raise engine.ConfigError(f"could not scan tagged resources: {e}") from e
    if not found and not conflicts:
        return RebuildResult(nothing_found=True)

    recovered, partial, skipped, incomplete, failed, invalid = [], [], [], [], [], []
    ambiguous: list[tuple[str, dict]] = []

    for name, resources in found.items():


        try:
            engine.validate_instance_name(name)
        except argparse.ArgumentTypeError as e:
            if on_warning is not None:
                on_warning(f"  WARNING: skipping a tag-derived name that fails validation: {e}")
            invalid.append(name)
            continue

        if name in registry and not force:
            skipped.append(name)
            continue


        if len(resources.get("os_volume_id_candidates", [])) > 1 or \
                len(resources.get("reserved_ip_candidates", [])) > 1 or \
                len(resources.get("region_candidates", [])) > 1:
            ambiguous.append((name, resources))
            continue
        if resources.get("os_volume_id") is None or resources.get("reserved_ip") is None:
            incomplete.append((name, resources))
            continue


        try:
            with _instance_lock(name):


                fresh_registry = load_registry()
                if name in fresh_registry and not force:
                    if on_warning is not None:
                        on_warning(
                            f"  WARNING: '{name}' was onboarded by a concurrent process while "
                            "this scan was running -- skipping to avoid overwriting it (re-run "
                            "with --force if you actually want rebuild's reconstruction to "
                            "win)."
                        )
                    skipped.append(name)
                    continue
                record = _rebuild_one_record(
                    client, name, resources, ssh_key=ssh_key, vpc_id=vpc_id
                )


                existing = fresh_registry.get(name)
                if existing is not None and record.get("group_id") is None:
                    record["group_id"] = existing.get("group_id")
                _save_one_record(name, record)

                if record.get("current_status") == "running" and on_warning is not None:


                    on_warning(
                        f"  WARNING: '{name}' recovered as running, but any active manual-"
                        "override auto-revert timer it had could not be recovered (this isn't "
                        "tracked in Linode's own tags) -- if it was manually started outside its "
                        "schedule's on-window, run `extend`/`stop` for it manually as needed."
                    )


                try:
                    ip = engine.retry_transient(
                        lambda ip_addr=resources["reserved_ip"]: client.load(
                            engine.ReservedIPAddress, ip_addr
                        )
                    )
                except Exception as e:
                    ip = None
                    if on_warning is not None:
                        on_warning(
                            f"  WARNING: '{name}' recovered, but its reserved IP's tags could "
                            f"not be read to check for a schedule or group membership ({e}) -- "
                            "re-run schedule-set/group-add for it manually if it had either."
                        )

                if ip is not None:
                    try:
                        decoded = _decode_schedule_from_tags(ip.tags)
                        if decoded is not None:


                            _validate_schedule_timezone(decoded["timezone"])
                            _validate_schedule_rules(decoded["rules"])
                            _save_schedule_row(
                                name, decoded["timezone"], decoded["rules"], decoded["enabled"]
                            )
                            if on_progress is not None:
                                on_progress(f"  restored schedule for '{name}' from tags.")
                    except Exception as e:

                        if on_warning is not None:
                            on_warning(
                                f"  WARNING: '{name}' recovered, but its individual schedule "
                                f"tags could not be restored: {e} -- re-run schedule-set for it "
                                "manually if it had one."
                            )

                    try:
                        group_name = next(
                            (
                                t[len(_GROUP_NAME_TAG_PREFIX):] for t in (ip.tags or [])
                                if t.startswith(_GROUP_NAME_TAG_PREFIX)
                            ),
                            None,
                        )
                        if group_name is not None:
                            existing_group = get_schedule_group(group_name)
                            if existing_group is not None:
                                group_id = existing_group["id"]
                            else:


                                snapshot = _decode_schedule_from_tags(
                                    ip.tags, prefix=_GROUP_SCHEDULE_TAG_PREFIX
                                )
                                if snapshot is None:
                                    raise engine.ConfigError(
                                        f"'{name}' is tagged for group '{group_name}', but no "
                                        "group-schedule snapshot was found in its tags to "
                                        "recreate the group from."
                                    )


                                _validate_schedule_rules(snapshot["rules"])
                                group_id = create_schedule_group(group_name, snapshot["timezone"])
                                _set_group_schedule_row(
                                    group_name, snapshot["timezone"], snapshot["rules"],
                                    snapshot["enabled"],
                                )
                                if on_progress is not None:
                                    on_progress(
                                        f"  recreated group '{group_name}' from '{name}''s tags."
                                    )

                            def _assign_group(group_id=group_id, name=name):
                                conn = _connect()
                                try:
                                    conn.execute(
                                        "UPDATE instances SET group_id = ? WHERE name = ?",
                                        (group_id, name),
                                    )
                                    conn.commit()
                                finally:
                                    conn.close()

                            _retry_db(_assign_group)
                            if on_progress is not None:
                                on_progress(
                                    f"  restored '{name}''s membership in group "
                                    f"'{group_name}' from tags."
                                )
                    except Exception as e:
                        if on_warning is not None:
                            on_warning(
                                f"  WARNING: '{name}' recovered, but its group membership tags "
                                f"could not be restored: {e} -- re-run group-add for it "
                                "manually if it belonged to a group."
                            )
        except InstanceLockedError as e:
            if on_warning is not None:
                on_warning(f"  WARNING: {e} -- skipping '{name}' this pass, safe to re-run "
                           "`rebuild` once it's finished.")
            failed.append(name)
            continue
        except (ApiError, RuntimeError, engine.ConfigError, requests.exceptions.RequestException) as e:


            if on_warning is not None:
                on_warning(f"  WARNING: failed to rebuild '{name}': {e} -- skipping, "
                           "other names are unaffected.")
            failed.append(name)
            continue

        registry[name] = record


        if record["current_status"] in ("running", "stopped"):
            recovered.append(name)
        else:
            partial.append(name)

    if on_progress is not None:
        on_progress(f"Scanned tags: {len(found)} name(s) found.")
        if recovered:
            on_progress(f"  fully recovered: {', '.join(recovered)}")
        if partial:
            on_progress(
                f"  partially recovered (was stopped, needs manual recovery): "
                f"{', '.join(partial)}"
            )
            on_progress("    -- boot each of these manually once via Cloud Manager from its "
                        "os_volume_id, using its network_config from Cloud Manager's own UI, "
                        "then re-run `onboard` to fully restore management.")
        if skipped:
            on_progress(f"  skipped (already in local registry, use --force to overwrite): "
                        f"{', '.join(skipped)}")
        if failed:
            on_progress(f"  FAILED to rebuild (see warnings above, safe to re-run `rebuild` "
                        f"later for just these): {', '.join(failed)}")
        if incomplete:
            for name, resources in incomplete:
                on_progress(
                    f"  WARNING: '{name}' has incomplete tags (found: {resources}) -- skipped."
                )
        if ambiguous:
            for name, resources in ambiguous:
                on_progress(
                    f"  WARNING: '{name}' has AMBIGUOUS tags -- multiple candidates found "
                    f"(os_volume_id_candidates={resources.get('os_volume_id_candidates')}, "
                    f"reserved_ip_candidates={resources.get('reserved_ip_candidates')}, "
                    f"region_candidates={resources.get('region_candidates')}) -- skipped, "
                    "not guessed. This usually means a stale tag was left on an old resource "
                    "(e.g. before a forced re-onboard, or an out-of-band change), or the "
                    "resources for this name genuinely span more than one region (should never "
                    "happen -- investigate). Review tags directly in Cloud Manager and remove "
                    "the stale one, then re-run rebuild."
                )
        if invalid:
            on_progress(
                f"  WARNING: skipped {len(invalid)} tag-derived name(s) that failed validation "
                f"(see warnings above): {invalid!r}. Check the raw tags on your Linode "
                "resources (Cloud Manager) for how these got there."
            )
        if conflicts:
            on_progress(
                f"  WARNING: {len(conflicts)} resource(s) are ambiguously tagged and were "
                "excluded from every name's candidate set entirely (not just left ambiguous "
                "for one name):"
            )
            for c in conflicts:
                if c["reason"] == "multiple_names":
                    on_progress(f"    {c['kind']} {c['id']}: tagged for more than one name at "
                                f"once: {c['names']!r}")
                else:
                    on_progress(f"    {c['kind']} {c['id']}: carries both the OS and data role "
                                f"tags at once (names: {c['names']!r})")
            on_progress("    Review tags directly in Cloud Manager and remove whichever is "
                        "stale, then re-run rebuild.")

    return RebuildResult(
        scanned_count=len(found), recovered=recovered, partial=partial, skipped=skipped,
        failed=failed, incomplete=incomplete, ambiguous=ambiguous, invalid=invalid,
        conflicts=conflicts,
    )


def cmd_rebuild(client, args) -> int:
    try:
        result = rebuild_instances(
            client, args.ssh_key, vpc_id=args.vpc_id, force=args.force,
            on_progress=print, on_warning=_print_to_stderr,
        )
    except engine.ConfigError as e:


        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result.nothing_found:
        print("No tagged resources found on this account -- nothing to rebuild.")
        return 0
    return 0 if result.ok else 1


def _rebuild_one_record(
    client, name: str, resources: dict, *, ssh_key: str, vpc_id: int | None = None
) -> dict:


    ip_info = engine.retry_transient(
        lambda: engine.get_ip_details(client, resources["reserved_ip"])
    )
    linode_id = ip_info.get("linode_id")

    if linode_id:
        instance = engine.retry_transient(lambda: client.load(Instance, linode_id))


        configs = list(instance.configs)
        captured_network = engine.capture_network_config(instance, configs=configs)


        live_sda = configs[0].devices.dict.get("sda")


        if not live_sda or not live_sda.get("filesystem_path"):
            raise engine.ConfigError(
                f"'{name}': live instance {linode_id}'s /dev/sda is not a Block Storage "
                "volume -- can't establish os_volume_id from the live boot config."
            )
        os_volume_id = live_sda["id"]
        if os_volume_id != resources["os_volume_id"]:
            raise engine.ConfigError(
                f"'{name}': the OS volume tagged for this name ({resources['os_volume_id']}) "
                f"doesn't match what's actually attached at /dev/sda on the live instance "
                f"({os_volume_id}) -- most likely an out-of-band boot-volume swap. Refusing "
                "to guess which one is right; reconcile manually (Cloud Manager: which "
                "volume should carry this name's disaster-recovery tags) before re-running "
                "rebuild."
            )


        vpc_prefix = _derive_vpc_prefix(
            client, captured_network["network_config"],
            captured_network["network_interface_model"], vpc_id=vpc_id,
            cannot_determine=lambda msg: engine.ConfigError(
                f"'{name}' {msg} Re-run rebuild with --vpc-id."
            ),
        )

        data_volumes = engine.capture_data_volumes(instance, os_volume_id, configs=configs)


        authorized_keys_raw = engine.ssh_run(
            resources["reserved_ip"], ssh_key,
            "cat /root/.ssh/authorized_keys 2>/dev/null",
            trust_new=True,
        )
        authorized_keys = [
            line.strip() for line in authorized_keys_raw.splitlines() if line.strip()
        ]
        if not authorized_keys:


            raise engine.ConfigError(
                f"'{name}' -- could not read any authorized_keys from the instance -- "
                "refusing to recover a record without knowing how the next start would "
                "grant SSH access."
            )
        instance_attrs = engine.capture_instance_attributes(instance)


        engine.tag_managed_resources(
            client, name, os_volume_id=os_volume_id,
            data_volume_ids=[dv["volume_id"] for dv in data_volumes],
            reserved_ip=resources["reserved_ip"],
        )
        return {
            "name": name,
            "region": resources["region"],
            "os_volume_id": os_volume_id,
            "reserved_ip": resources["reserved_ip"],
            "network_interface_model": captured_network["network_interface_model"],
            "network_config": captured_network["network_config"],
            "network_helper_enabled": captured_network["network_helper_enabled"],
            "vpc_prefix": vpc_prefix,
            "data_volumes": data_volumes,
            "authorized_keys": authorized_keys,
            "label": instance.label,
            "tags": list(instance.tags),
            "instance_attrs": instance_attrs,
            "current_linode_id": linode_id,
            "current_status": "running",
            "transitioning": False,
        }


    slot_names = engine.DATA_VOLUME_DEVICE_SLOTS
    data_volume_ids = resources["data_volume_ids"]
    if len(data_volume_ids) > len(slot_names):
        raise engine.ConfigError(
            f"'{name}' has {len(data_volume_ids)} data volumes, more than the "
            f"{len(slot_names)} non-OS device slots a boot config supports -- refusing "
            "to silently drop any of them from the reconstructed record."
        )
    record = {
        "name": name,
        "region": resources["region"],
        "os_volume_id": resources["os_volume_id"],
        "reserved_ip": resources["reserved_ip"],
        "data_volumes": [
            {"volume_id": vid, "device_slot": slot}


            for vid, slot in zip(resources["data_volume_ids"], slot_names)
        ],
        "current_linode_id": None,
        "current_status": "needs_manual_recovery",
        "transitioning": False,
    }


    try:
        ip = engine.retry_transient(
            lambda: client.load(engine.ReservedIPAddress, resources["reserved_ip"])
        )


        ip_tags = ip.tags if isinstance(ip.tags, list) else []
    except Exception:
        ip_tags = []

    simple_network = _decode_simple_network_config_from_tags(ip_tags)
    if simple_network is not None:
        record.update(simple_network)

    tag_ssh_key_ids = _decode_ssh_key_ids_from_tags(ip_tags)
    tag_authorized_keys = (
        _resolve_ssh_key_ids_to_content(client, tag_ssh_key_ids) if tag_ssh_key_ids else []
    )

    backup = osb.download_instance_backup(name)

    if simple_network is None and backup is not None:
        record["network_interface_model"] = backup.get("network_interface_model")
        record["network_config"] = backup.get("network_config")
        record["network_helper_enabled"] = backup.get("network_helper_enabled")

    merged_keys = list(tag_authorized_keys)
    if backup is not None:


        for key in backup.get("authorized_keys") or []:
            if key not in merged_keys:
                merged_keys.append(key)
    if merged_keys:
        record["authorized_keys"] = merged_keys

    if backup is not None:
        record["vpc_prefix"] = backup.get("vpc_prefix")
        record["label"] = backup.get("label")
        record["tags"] = backup.get("tags")
        record["instance_attrs"] = backup.get("instance_attrs")


    has_network = (
        record.get("network_interface_model") is not None
        and record.get("network_config") is not None
        and (
            record.get("network_interface_model") != "legacy_config"
            or record.get("network_helper_enabled") is not None
        )
    )
    if has_network and record.get("authorized_keys"):
        record["current_status"] = "stopped"

    return record


DEFAULT_SESSION_LIFETIME_HOURS = 24.0


def create_api_session(
    linode_username: str, hours: float = DEFAULT_SESSION_LIFETIME_HOURS
) -> dict:

    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    expires_at = (now + timedelta(hours=hours)).isoformat()

    def _do():
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO api_sessions (token, linode_username, created_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (token, linode_username, now.isoformat(), expires_at),
            )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)
    return {"token": token, "expires_at": expires_at}


def get_api_session(token: str) -> str | None:

    def _do():
        conn = _connect()
        try:
            return conn.execute(
                "SELECT linode_username, expires_at FROM api_sessions WHERE token = ?", (token,)
            ).fetchone()
        finally:
            conn.close()

    row = _retry_db(_do)
    if row is None:
        return None
    linode_username, expires_at = row
    if datetime.now(UTC) >= datetime.fromisoformat(expires_at):
        return None
    return linode_username


def delete_api_session(token: str) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute("DELETE FROM api_sessions WHERE token = ?", (token,))
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def build_arg_parser() -> argparse.ArgumentParser:


    load_dotenv(engine.BASE_DIR / ".env")
    default_ssh_key = os.environ.get(
        "LINODE_SSH_KEY_PATH", str(Path.home() / ".ssh" / "linode_spike_key")
    )

    parser = argparse.ArgumentParser(
        prog="instance_manager.py",
        description="Onboard existing Linode instances and start/stop them by name.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate_start_parser = subparsers.add_parser(
        "migrate-start",
        help="Path B: start migrating a local-disk instance onto Block Storage.",
    )
    migrate_start_parser.add_argument("--name", required=True, type=validate_instance_name, help="A short name to track this migration by.")
    migrate_start_parser.add_argument("--instance-id", type=int, required=True)
    migrate_start_parser.add_argument("--ssh-key", default=default_ssh_key)
    migrate_start_parser.add_argument("--force", action="store_true", help="Restart an in-progress migration.")
    migrate_start_parser.add_argument(
        "--yes", action="store_true",
        help="Skip the confirmation prompt shown when --force targets a DIFFERENT "
        "--instance-id than the in-progress attempt being restarted.",
    )

    migrate_resume_parser = subparsers.add_parser(
        "migrate-resume",
        help="Path B: finish a migration after the manual dd step is done.",
    )
    migrate_resume_parser.add_argument("--name", required=True, type=validate_instance_name)
    migrate_resume_parser.add_argument("--ssh-key", default=default_ssh_key)

    migrate_orphans_parser = subparsers.add_parser(
        "migrate-orphans",
        help="List or clean up destination volumes orphaned by --force-replaced migration "
        "attempts.",
    )
    migrate_orphans_parser.add_argument(
        "--name", type=validate_instance_name, help="Limit to one name. Required with --cleanup.",
    )
    migrate_orphans_parser.add_argument(
        "--volume-id", type=int, default=None,
        help="With --cleanup, delete only this one volume instead of every orphaned volume for "
        "--name. Required (identifies exactly one volume) with --mark-orphaned.",
    )


    cleanup_group = migrate_orphans_parser.add_mutually_exclusive_group()
    cleanup_group.add_argument(
        "--cleanup", action="store_true",
        help="Delete the matching orphaned volume(s) (requires --name) instead of just listing "
        "them. Idempotent -- a 404 counts as already resolved.",
    )
    cleanup_group.add_argument(
        "--mark-orphaned", action="store_true",
        help="Retag a remote-only ACTIVE migration volume (found via tag, no local "
        "record) as orphaned, for a volume the operator has confirmed by "
        "hand (Cloud Manager / direct knowledge) is dead and unresumable. Requires --name "
        "and --volume-id, and typing --name to confirm unless --yes. Does not delete "
        "anything -- once marked, the volume is eligible for the existing --cleanup path "
        "-- run this as a separate, later "
        "invocation, not combined with --cleanup in the same one.",
    )
    migrate_orphans_parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt.")

    onboard_parser = subparsers.add_parser(
        "onboard", help="Register an existing, already volume-based instance."
    )
    onboard_parser.add_argument("--name", required=True, type=validate_instance_name, help="A short name to refer to this instance by.")
    onboard_parser.add_argument("--instance-id", type=int, required=True, help="The live Linode instance ID.")
    onboard_parser.add_argument(
        "--vpc-id", type=int, default=None,
        help="Required only if the instance has a VPC interface under the legacy_config model "
        "(that model doesn't expose vpc_id directly) and no matching VPC can be found "
        "automatically. Also used as a fallback for the newer 'linode' interface model if "
        "its own captured vpc_id is ever missing (not expected in practice).",
    )
    onboard_parser.add_argument("--ssh-key", default=default_ssh_key)
    onboard_parser.add_argument("--force", action="store_true", help="Re-onboard even if already registered.")
    onboard_parser.add_argument(
        "--yes", action="store_true",
        help="Skip the confirmation prompt shown when --force changes the underlying resource "
        "identity (a different instance/volume/IP than what's currently on record).",
    )

    start_parser = subparsers.add_parser("start", help="Start (create) an onboarded instance.")
    start_parser.add_argument("--name", required=True, type=validate_instance_name)
    start_parser.add_argument("--ssh-key", default=default_ssh_key)
    start_parser.add_argument(
        "--override-window-hours", type=float, default=None,
        help="Only relevant for a manual start outside a configured schedule's own on-window: "
        f"how many hours before it auto-stops (default {DEFAULT_MANUAL_OVERRIDE_WINDOW_HOURS} "
        "-- see `extend` to push an already-running override further out).",
    )

    stop_parser = subparsers.add_parser("stop", help="Stop (delete) an onboarded instance.")
    stop_parser.add_argument("--name", required=True, type=validate_instance_name)
    stop_parser.add_argument("--ssh-key", default=default_ssh_key)
    stop_parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt.")
    stop_parser.add_argument(
        "--force", action="store_true",
        help="Proceed with the delete even if the disaster-recovery tag refresh fails with a "
        "transient API error. Never bypasses a ResourceOwnershipConflict or "
        "TagVerificationError -- those mean the recovery mapping is provably wrong or "
        "contested, not just temporarily unreachable.",
    )
    stop_parser.add_argument(
        "--skip-precapture", action="store_true",


        help="Skip the SSH-based data-volume AND authorized_keys re-capture, using the "
        "last-known data_volumes/authorized_keys from the registry instead. Only use this "
        "when the node is confirmed SSH-unreachable (a guest firewall rule, crashed sshd, "
        "wedged network stack) -- without it, an unreachable node can never be stopped "
        "through this tool at all, even though it keeps billing. A data volume attached, or "
        "an SSH key added/revoked, out of band since the last start/stop cycle would be "
        "silently missed.",
    )

    offboard_parser = subparsers.add_parser(
        "offboard",
        help="Permanently decommission a stopped instance: release its reserved IP, "
        "remove it from the registry, and optionally delete its volumes.",
    )
    offboard_parser.add_argument("--name", required=True, type=validate_instance_name)
    offboard_parser.add_argument(
        "--delete-volumes", action="store_true",
        help="Also permanently delete the OS and data volume(s), not just release the IP. "
        "Destroys real data -- omit to keep the volumes (untagged, so `rebuild` won't find them).",
    )
    offboard_parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt.")

    poll_parser = subparsers.add_parser(
        "poll",
        help="Run the scheduler: check every onboarded instance's individual schedule and "
        "start/stop it if due.",
    )
    poll_parser.add_argument(
        "--interval-seconds", type=int, default=300,
        help="Seconds between ticks when running continuously (default 300, on this project's own recommended cadence of every 1-5 minutes). Ignored with --once.",
    )
    poll_parser.add_argument(
        "--once", action="store_true",
        help="Run exactly one tick and exit, instead of looping forever. Useful for testing, or "
        "for running this from cron instead of a supervisor.",
    )
    poll_parser.add_argument(
        "--window-seconds", type=int, default=DEFAULT_POLL_WINDOW_SECONDS,
        help=f"How long after a rule's start_time/stop_time the poller still considers it due "
        f"(default {DEFAULT_POLL_WINDOW_SECONDS}, tolerating ordinary poll-tick drift).",
    )
    poll_parser.add_argument("--ssh-key", default=default_ssh_key)

    serve_api_parser = subparsers.add_parser(
        "serve-api",
        help="Run the customer-facing REST API server. Binds to localhost "
        "by default -- pass --host to expose it further, e.g. behind a reverse proxy.",
    )
    serve_api_parser.add_argument("--host", default="127.0.0.1")
    serve_api_parser.add_argument("--port", type=int, default=8000)

    subparsers.add_parser("list", help="List all onboarded instances and their current status.")

    status_parser = subparsers.add_parser("status", help="Show one onboarded instance's full record.")
    status_parser.add_argument("--name", required=True, type=validate_instance_name)

    history_parser = subparsers.add_parser(
        "history",
        help="Show recent create/delete events for one instance (the audit trail).",
    )
    history_parser.add_argument("--name", required=True, type=validate_instance_name)
    history_parser.add_argument(
        "--limit", type=int, default=50,
        help="Maximum number of most-recent events to show (default 50).",
    )

    schedule_set_parser = subparsers.add_parser(
        "schedule-set",
        help="Create or replace one instance's individual start/stop schedule.",
    )
    schedule_set_parser.add_argument("--name", required=True, type=validate_instance_name)
    schedule_set_parser.add_argument(
        "--timezone", required=True,
        help="IANA timezone name (e.g. Asia/Kolkata, US/Pacific) -- this schedule's own "
        "day/time rules are resolved in this timezone, not UTC.",
    )
    schedule_set_parser.add_argument(
        "--days", help="Comma-separated days for a single rule, e.g. mon,tue,wed,thu,fri. "
        "Pass with --start-time/--stop-time; mutually exclusive with --rules-json.",
    )
    schedule_set_parser.add_argument(
        "--start-time", help="HH:MM 24-hour, e.g. 09:00. Pass with --days/--stop-time.",
    )
    schedule_set_parser.add_argument(
        "--stop-time", help="HH:MM 24-hour, e.g. 18:00. Pass with --days/--start-time.",
    )
    schedule_set_parser.add_argument(
        "--rules-json",
        help="A full JSON array of {days_of_week, start_time, stop_time} rules, for more than "
        "one rule in a single call. Mutually exclusive with --days/--start-time/--stop-time.",
    )
    schedule_set_parser.add_argument(
        "--disabled", action="store_true",
        help="Create the schedule disabled (the poller ignores it until re-enabled).",
    )

    schedule_show_parser = subparsers.add_parser(
        "schedule-show", help="Show one instance's individual schedule, if any.",
    )
    schedule_show_parser.add_argument("--name", required=True, type=validate_instance_name)

    schedule_clear_parser = subparsers.add_parser(
        "schedule-clear", help="Remove one instance's individual schedule.",
    )
    schedule_clear_parser.add_argument("--name", required=True, type=validate_instance_name)

    extend_parser = subparsers.add_parser(
        "extend",
        help="Push a currently-active manual-override auto-revert timer further out. Refuses if the instance isn't running or has no active override to extend.",
    )
    extend_parser.add_argument("--name", required=True, type=validate_instance_name)
    extend_parser.add_argument(
        "--hours", type=float, default=None,
        help=f"How many hours from now to push the auto-stop out to (default "
        f"{DEFAULT_MANUAL_OVERRIDE_WINDOW_HOURS}).",
    )

    group_create_parser = subparsers.add_parser(
        "group-create",
        help="Create a new, empty schedule group -- give it a schedule "
        "afterward with group-schedule-set.",
    )
    group_create_parser.add_argument("--name", required=True, help="The group's display name.")
    group_create_parser.add_argument(
        "--timezone", required=True,
        help="IANA timezone name (e.g. Asia/Kolkata, US/Pacific) -- the group's own day/time "
        "rules are resolved in this timezone, not UTC.",
    )

    group_schedule_set_parser = subparsers.add_parser(
        "group-schedule-set",
        help="Create or replace a schedule group's start/stop schedule.",
    )
    group_schedule_set_parser.add_argument("--group-name", required=True)
    group_schedule_set_parser.add_argument(
        "--timezone", required=True,
        help="IANA timezone name (e.g. Asia/Kolkata, US/Pacific) -- this group's own day/time "
        "rules are resolved in this timezone, not UTC. Required on every call, the same as "
        "group-create's own --timezone -- this is the only way to change a group's timezone "
        "after creation.",
    )
    group_schedule_set_parser.add_argument(
        "--days", help="Comma-separated days for a single rule, e.g. mon,tue,wed,thu,fri. "
        "Pass with --start-time/--stop-time; mutually exclusive with --rules-json.",
    )
    group_schedule_set_parser.add_argument(
        "--start-time", help="HH:MM 24-hour, e.g. 09:00. Pass with --days/--stop-time.",
    )
    group_schedule_set_parser.add_argument(
        "--stop-time", help="HH:MM 24-hour, e.g. 18:00. Pass with --days/--start-time.",
    )
    group_schedule_set_parser.add_argument(
        "--rules-json",
        help="A full JSON array of {days_of_week, start_time, stop_time} rules, for more than "
        "one rule in a single call. Mutually exclusive with --days/--start-time/--stop-time.",
    )
    group_schedule_set_parser.add_argument(
        "--disabled", action="store_true",
        help="Set the schedule disabled (the poller ignores it, for every member, until "
        "re-enabled).",
    )

    group_show_parser = subparsers.add_parser(
        "group-show", help="Show one schedule group's schedule and current members.",
    )
    group_show_parser.add_argument("--group-name", required=True)

    subparsers.add_parser("group-list", help="List every schedule group.")

    group_delete_parser = subparsers.add_parser(
        "group-delete",
        help="Permanently delete a schedule group. Refuses if it still has members -- "
        "group-remove each one first.",
    )
    group_delete_parser.add_argument("--group-name", required=True)

    group_add_parser = subparsers.add_parser(
        "group-add", help="Add (or move) an onboarded instance into a schedule group.",
    )
    group_add_parser.add_argument("--name", required=True, type=validate_instance_name)
    group_add_parser.add_argument("--group-name", required=True)

    group_remove_parser = subparsers.add_parser(
        "group-remove",
        help="Remove an instance from its current schedule group. If it has no individual "
        "schedule of its own, prompts whether to copy the group's rules into a new one for it "
        "(or pass --keep-manual/--copy-schedule to decide non-interactively).",
    )
    group_remove_parser.add_argument("--name", required=True, type=validate_instance_name)
    group_remove_exclusive = group_remove_parser.add_mutually_exclusive_group()
    group_remove_exclusive.add_argument(
        "--keep-manual", action="store_true",
        help="If it has no individual schedule, leave it manual-only -- don't copy the group's "
        "rules. Skips the interactive prompt.",
    )
    group_remove_exclusive.add_argument(
        "--copy-schedule", action="store_true",
        help="If it has no individual schedule, copy the group's current rules into a new "
        "individual schedule for it. Skips the interactive prompt.",
    )
    group_remove_parser.add_argument(
        "--yes", action="store_true",
        help="Non-interactive: if neither --keep-manual nor --copy-schedule is given and a "
        "prompt would otherwise be shown, default to keeping it manual-only rather than "
        "prompting.",
    )

    clear_lock_parser = subparsers.add_parser(
        "clear-lock", help="Forcibly clear a stuck transitioning lock for one instance."
    )
    clear_lock_parser.add_argument("--name", required=True, type=validate_instance_name)
    clear_lock_parser.add_argument("--yes", action="store_true")

    deregister_parser = subparsers.add_parser(
        "deregister",
        help="Remove an instance from this tool's own tracking only -- the Linode instance, "
        "its volumes, its reserved IP, and its tags are never touched. Use this when the local "
        "record itself is wrong or unsafe (not to decommission the instance -- use offboard "
        "for that).",
    )
    deregister_parser.add_argument("--name", required=True, type=validate_instance_name)
    deregister_parser.add_argument("--yes", action="store_true")

    reset_host_key_parser = subparsers.add_parser(
        "reset-host-key",
        help="Forget a node's trusted SSH host key and trust whatever it presents next. "
        "Only use after independently confirming a key change is legitimate.",
    )
    reset_host_key_parser.add_argument(
        "--name", type=validate_instance_name,
        help="An already-onboarded node's name. Mutually exclusive with --ip.",
    )
    reset_host_key_parser.add_argument(
        "--ip", type=engine.validate_ip_address,
        help="Reset trust by IP address directly, for a node that isn't onboarded yet -- e.g. "
        "a newly reused/reissued address with a stale entry from a previous node. Mutually "
        "exclusive with --name.",
    )
    reset_host_key_parser.add_argument("--ssh-key", default=default_ssh_key)
    reset_host_key_parser.add_argument("--yes", action="store_true")

    rebuild_parser = subparsers.add_parser(
        "rebuild",
        help="Disaster recovery: reconstruct the registry from tags on Linode's "
        "own account state, in case the local instances.json is lost.",
    )
    rebuild_parser.add_argument("--ssh-key", default=default_ssh_key)
    rebuild_parser.add_argument(
        "--vpc-id", type=int, default=None,
        help="Required only if a recovered instance has a VPC interface under the "
        "legacy_config model (that model doesn't expose vpc_id directly) and no VPC "
        "containing its subnet can be found automatically. Also used as a fallback for the "
        "newer 'linode' interface model if its own captured vpc_id is ever missing (not "
        "expected in practice). Applies to every recovered name this run.",
    )
    rebuild_parser.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing local entry with the rebuilt one, if one already exists.",
    )

    backup_parser = subparsers.add_parser(
        "backup",
        help="On-demand, whole-system backup -- re-syncs every instance's Object Storage "
        "record and takes a full database snapshot. Meant to be run on a schedule (cron/"
        "systemd timer), not just invoked manually.",
    )
    backup_parser.add_argument(
        "--backup-dir", default=None,
        help="Also write a full local database snapshot into this directory, independent of "
        "Object Storage. Give this, configure Object Storage (LINODE_OBJ_STORAGE_* in .env), "
        "or both -- refuses if neither is available, since there'd be nothing to actually do.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        return _dispatch(args)
    except Exception as e:
        print(f"Configuration error: unexpected internal error: {e}", file=sys.stderr)
        return 1


def _dispatch(args) -> int:

    try:
        return _route(args)
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1


def _route(args) -> int:
    if args.command == "list":
        return cmd_list(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "history":
        return cmd_history(args)
    if args.command == "schedule-show":
        return cmd_schedule_show(args)
    if args.command == "extend":
        return cmd_extend(args)
    if args.command == "serve-api":
        return cmd_serve_api(args)
    if args.command == "group-create":
        return cmd_group_create(args)
    if args.command == "group-show":
        return cmd_group_show(args)
    if args.command == "group-list":
        return cmd_group_list(args)
    if args.command == "group-delete":
        return cmd_group_delete(args)
    if args.command == "clear-lock":
        return cmd_clear_lock(args)
    if args.command == "deregister":
        return cmd_deregister(args)
    if args.command == "reset-host-key":
        return cmd_reset_host_key(args)
    if args.command == "backup":
        return cmd_backup(args)

    try:
        token = engine.load_token()
        client = engine.build_client(token)
        engine.auth_check(client)
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except (ApiError, requests.exceptions.RequestException) as e:


        print(f"Configuration error: could not verify API access: {e}", file=sys.stderr)
        return 1

    if args.command == "migrate-start":
        return cmd_migrate_start(client, args)
    if args.command == "migrate-resume":
        return cmd_migrate_resume(client, args)
    if args.command == "migrate-orphans":
        return cmd_migrate_orphans(client, args)
    if args.command == "onboard":
        return cmd_onboard(client, args)
    if args.command == "start":
        return cmd_start(client, args)
    if args.command == "stop":
        return cmd_stop(client, args)
    if args.command == "offboard":
        return cmd_offboard(client, args)
    if args.command == "poll":
        return cmd_poll(client, args)
    if args.command == "rebuild":
        return cmd_rebuild(client, args)
    if args.command == "schedule-set":
        return cmd_schedule_set(client, args)
    if args.command == "schedule-clear":
        return cmd_schedule_clear(client, args)
    if args.command == "group-schedule-set":
        return cmd_group_schedule_set(client, args)
    if args.command == "group-add":
        return cmd_group_add(client, args)
    if args.command == "group-remove":
        return cmd_group_remove(client, args)

    return 1


if __name__ == "__main__":
    sys.exit(main())
