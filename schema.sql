
CREATE TABLE IF NOT EXISTS schedule_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    timezone TEXT NOT NULL,
    rules TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS instances (
    name TEXT PRIMARY KEY,
    label TEXT,
    region TEXT,
    network_interface_model TEXT
        CHECK (network_interface_model IS NULL OR network_interface_model IN ('legacy_config', 'linode')),
    network_config TEXT,
    network_helper_enabled INTEGER,
    vpc_prefix INTEGER,
    os_volume_id INTEGER,
    data_volumes TEXT,
    reserved_ip TEXT,
    authorized_keys TEXT,
    tags TEXT,
    instance_attrs TEXT,
    group_id INTEGER REFERENCES schedule_groups(id),
    current_linode_id INTEGER,
    current_status TEXT
        CHECK (current_status IS NULL OR current_status IN ('running', 'stopped', 'unreachable', 'needs_manual_recovery')),
    transitioning INTEGER NOT NULL DEFAULT 0,
    manual_override_expires_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS schedules (
    instance_name TEXT PRIMARY KEY REFERENCES instances(name) ON DELETE CASCADE,
    timezone TEXT NOT NULL,
    rules TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS schedule_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_name TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('create', 'delete')),
    triggered_by TEXT NOT NULL CHECK (triggered_by IN ('schedule', 'manual', 'api')),
    timestamp TIMESTAMP NOT NULL,
    result TEXT NOT NULL,
    error_message TEXT,
    actor TEXT
);

CREATE TABLE IF NOT EXISTS locks (
    name TEXT PRIMARY KEY,
    locked_at TIMESTAMP NOT NULL,
    locked_by TEXT
);

CREATE TABLE IF NOT EXISTS migrations (
    name TEXT PRIMARY KEY,
    phase TEXT,
    checkpoint TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS orphaned_migration_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    dest_volume_id INTEGER,
    archived_at TIMESTAMP NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_sessions (
    token TEXT PRIMARY KEY,
    linode_username TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP NOT NULL
);
