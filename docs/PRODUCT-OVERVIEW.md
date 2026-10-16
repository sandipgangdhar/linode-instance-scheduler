# Linode Instance Scheduler

**Stop paying for compute you're not using — without losing your data, your IP, or your network setup.**

## The problem

On Linode, a *stopped* instance bills exactly the same as a *running* one. Powering off a
Linode doesn't reduce your bill at all — RAM and network capacity stay reserved either way.
The only way to actually stop paying for compute is to **delete** the instance outright.

That's a real problem for any workload that doesn't need to run around the clock — dev/test
environments that only matter during business hours, staging servers, demo environments,
batch/CI runners, or any fleet where "always on" is paying for idle time nobody's using.

## What this product does

Linode Instance Scheduler automates a delete-and-recreate cycle that **behaves like stop and
start** from your point of view — while genuinely stopping compute billing during the "off"
period, because the instance is actually deleted, not just powered off.

The trick is what it deliberately keeps untouched. When an instance is "stopped" by this tool,
only the compute resource itself goes away. Everything that makes it *your* machine survives on
its own, independent of the instance's lifecycle:

- **Your data** — the OS disk and any additional data volumes live on Linode Block Storage,
  which is a separate resource from the instance and is never deleted.
- **Your IP address** — the public IP is reserved before the first cycle, so it comes back to
  the exact same address every time, not a new one.
- **Your network identity** — VPC membership, VLAN attachment, tags, and the instance's label
  are all captured and reproduced automatically on every recreate.
- **Your SSH access** — the host key stays stable across cycles, so there's no "your SSH client
  says this might be a security risk" moment every time it comes back.

When you (or a schedule) says "start," the tool recreates the instance from scratch — same
region, same plan, same disks, same IP, same network — and verifies it's genuinely reachable
before reporting success. When it says "stop," the instance is fully deleted, so billing for
compute genuinely stops. Block Storage still bills, but at a small fraction of compute cost.

## Core capabilities

- **Individual schedules** — set exact on/off windows (e.g. weekdays 9am–6pm) per instance, in
  any IANA timezone, with correct daylight-saving-time handling.
- **Schedule groups** — apply one schedule to a whole set of instances at once (e.g. an entire
  dev environment), with individual instances able to override the group schedule when they
  need to.
- **Manual override with auto-revert** — start an instance outside its scheduled hours whenever
  you need to, and it automatically reverts to the schedule after a configurable window (default
  2 hours) unless you explicitly extend it — so a forgotten manual start doesn't quietly run
  (and bill) all weekend.
- **Cost savings tracking** — a configured-savings percentage the moment you set up a schedule,
  and an actual-savings percentage computed from real usage history once the schedule has run —
  shown as a percentage of avoided uptime, not a dollar estimate, since Linode account-specific
  pricing isn't something the tool can see.
- **Self-healing disaster recovery** — the tool's own local database can be lost entirely and
  rebuilt from tags it maintains directly on your Linode resources, with no separate backup
  system required.
- **Three ways to control it** — a command-line tool for scripting and direct operator control,
  a REST API, and a web dashboard, all built on the exact same underlying logic so they never
  disagree with each other.

## How it's deployed

This is **self-hosted, one deployment per customer's own Linode account** — not a shared SaaS
service. You run it against your own account, with your own Linode API token. Nothing about
your infrastructure, credentials, or usage data is ever seen by, or leaves through, any
third-party service. A lightweight local database (SQLite) tracks state; there's no external
database server to stand up or manage.

Two separate credentials are used, for two separate purposes: the Linode API token is the only
thing that ever talks to Linode directly, and it's yours, configured once, on your own
infrastructure. If you also enable the dashboard/API, login uses Linode's own OAuth ("Login
with Linode") — your team signs in with the same account credentials they already use for
Linode's Cloud Manager, and that login only ever answers "who is this," never used to call
Linode's API itself.

SSH access, where the tool needs it (to verify an instance actually came back up), uses a
single keypair generated for the deployment itself — never a password or key stored per
instance, and never anything that outlives the request it was used for if you provide one-time
credentials during setup.

## Getting an existing instance under management

Two paths, depending on where you're starting from:

- **New instance** — provisioned from a prepared base image in seconds, ready to go under
  schedule management immediately.
- **Existing instance** — a one-time migration moves the instance's operating system from local
  disk onto Block Storage (a documented Linode-recommended pattern for exactly this kind of
  workflow), preserving all existing data. This step requires a short, scheduled maintenance
  window, after which the instance is under full schedule management going forward with zero
  further downtime from cycling.

## Where to go deeper

This document is the introduction. For the full technical picture — architecture, every command
and API endpoint, the exact security model, and how disaster recovery actually works under the
hood — see the **Instance Scheduler Definitive Guide**. For day-to-day operation once it's
running — backups, monitoring, upgrades, and troubleshooting — see the **Operations Guide**.
