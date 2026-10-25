# Recovery — when you lose the scheduler host

Use this guide when the scheduler host is gone or can't be reached: deleted by mistake, its disk
or data centre failed, you've lost the login, or it was compromised. You set up a new host and
it takes over with everything the old one knew: every managed instance, schedule, group, start
order, hook, holiday, budget, wake setting, team, API token and history.

Your managed instances keep running as they are while you do this. Nothing is started or stopped
until the new host's scheduler starts in step 6, and a stopped instance stays safely stopped:
its volumes and IP addresses are kept on your account the whole time.

How long it takes: about 20–30 minutes, most of it creating the host and installing.

---

## Before you start: what you need

You need the **Object Storage bucket settings** in every case, plus one of the two sets below.

**Always:**

- Bucket name, endpoint, access key and secret key (from install, [INSTALL-GUIDE.md](./INSTALL-GUIDE.md)
  steps 4–5). If the access key is lost too, create a new one for the same bucket in Cloud
  Manager (Object Storage → Access Keys) — the backups are in the bucket, not tied to the key.

**Then either A or B:**

| A — the easy way (recommended) | B — without the age copy |
|---|---|
| Your age private key file (`scheduler-age-key.txt`, install step 6) | The API token (install step 2) — or a new one created the same way as the dedicated user |
| | The OAuth client ID and secret (install step 3), if you use the dashboard |

With A, every other setting (API token, OAuth, alerts, passphrase) and the deployment SSH key
come out of the encrypted copy in your bucket. With B, the SSH key and its passphrase are still
recovered automatically (from the bucket and from a private StackScript in your Linode account),
you just type the other settings again.

Lost the API token as well? Sign in to Cloud Manager as the dedicated user (or an account admin),
revoke the old token under Profile → API Tokens, and create a new one with the same scopes
(install step 2).

## Step 1 — Make sure the old host is really stopped

Two scheduler hosts acting on the same instances at the same time would race each other. If the
old host still exists in Cloud Manager, **power it off** (or delete it, if you're sure). If it was
compromised, also revoke its API token and Object Storage access key now and use new ones below.

The tool also protects itself: only one host holds the scheduler's lease in the bucket, and a
second one stands by instead of acting. `recover` takes the lease over deliberately, which is why
the old one has to be off first.

## Step 2 — Create the new host

Create it exactly like the original (install step 7): a Dedicated CPU plan of the same size
(4 GB, or 8/16 GB for Smart Ask), Ubuntu 24.04, the **same region**, and — if the old host was in
a VPC (VPC-only instances, or wake through a NodeBalancer) — **the same VPC**. Give it the same
Cloud Firewall rules.

If the dashboard used a DNS name, point that name's A record at the new host's IP now, so the
HTTPS certificate can be issued during the install.

## Step 3 — Run the recovery

SSH to the new host as root.

**A — with your age key** (copy `scheduler-age-key.txt` to the host first, e.g. with `scp`, into
`/root`):

```
git clone https://github.com/sandipgangdhar/linode-instance-scheduler.git
cd linode-instance-scheduler
sudo ./install.sh recover --env-from-backup --age-identity /root/scheduler-age-key.txt --domain scheduler.example.com
```

It asks only for the bucket name, endpoint, access key and secret key.

**B — without the age copy:**

```
git clone https://github.com/sandipgangdhar/linode-instance-scheduler.git
cd linode-instance-scheduler
sudo ./install.sh recover --domain scheduler.example.com
```

It asks the same questions as the original install. Give it the **same bucket**. Leave the
passphrase question blank — it's fetched from your Linode account.

If GitHub can't be reached, use the copy of the release kept in your own bucket instead of
`git clone`: download `deployment/release/latest.tar.gz` from the bucket (Cloud Manager → Object
Storage → your bucket), unpack it on the host and run its `install.sh recover` the same way.

## Step 4 — What `recover` does

You'll see it work through these, before anything is started:

1. Installs the program, services and dashboard exactly as `install` does, but leaves them stopped.
2. Restores the settings and the deployment SSH key (from the age copy, or from the bucket and
   the restored database).
3. Restores the newest database snapshot from the bucket — usually the continuous one, made
   within about a minute of the last change on the old host — and the instances' trusted SSH host
   keys.
4. Runs `rebuild`: reads your account's resource tags and the bucket's records and adds anything
   newer than the snapshot, including instances onboarded at the last minute.
5. Runs `heal`: compares each instance with your account and fixes what the snapshot couldn't
   know — for example an instance that was started after the snapshot is recognised as running
   (never created a second time), and a half-finished start's leftover blank instance is removed.
6. Takes over the scheduler's lease, starts the services and takes a fresh backup.
7. Lists instances that aren't in the state their schedule wants (when the old host was down for
   more than an hour) and asks whether to start or stop them now.

Want to look before anything starts? Add `--no-start`: it stops after step 5. Check
`sudo linode-instance-scheduler list`, then run `sudo ./install.sh start`.

## Step 5 — Check it

```
sudo ./install.sh status
sudo linode-instance-scheduler doctor
sudo linode-instance-scheduler alerts-check
sudo linode-instance-scheduler list
```

Open the dashboard and check **System backup** (everything green) and **Instances**. Then go
through anything that needs you:

| You see | Do this |
|---|---|
| An instance in **needs manual recovery** | Boot it once from Cloud Manager, then `onboard --name <name> --instance-id <id> --force` (or the dashboard's Onboard page, "force re-onboard") |
| Instances listed as **off their schedule** | Review them on the Instances page (or `schedule-catch-up`); start/stop them, or choose "Leave as is" |
| An instance refusing to start with a **host-key warning** | It was onboarded after the last host-key backup. Confirm it's really your instance, then `reset-host-key --name <name>` |
| An **unfinished migration** | Resume it, or cancel it ("Cancel this migration") — the original boots from its own disk |
| A **manual-start timer** that was counting down | It's reported, not re-armed. Use **Keep running** again if you still need it |
| A **new host address or name** | Update DNS and the OAuth app's callback URL (Cloud Manager → Profile → OAuth Apps), and `LINODE_OAUTH_REDIRECT_URI` in `/opt/linode-instance-scheduler/.env` |

Then delete the old host in Cloud Manager if you only powered it off, and make sure its backups
stopped (it can't upload anything once the new host holds the lease, but a deleted host is
tidier).

## If you have no backup snapshot at all

If the bucket has no snapshot (or no bucket was ever configured), run
`sudo ./install.sh recover --rebuild-only`. `rebuild` then brings everything back from your
account's resource tags and the bucket's records: every instance, its schedule, group membership,
each group's schedule, hooks and start order, and API tokens (with their revocations). Without a
bucket, a stopped instance whose network settings can't be read from its tags comes back as
**needs manual recovery** (see the table above), and API tokens must be created again. The audit
history and dashboard sign-in sessions exist only in the database.

If the deployment SSH key is lost too (no bucket, no snapshot, no age copy), use `--new-ssh-key`:
the new host makes a new key, and you add its public key to each managed instance
(`/root/.ssh/authorized_keys`) before the scheduler can reach them.

## Practise it

You can rehearse a recovery without touching anything: `sudo linode-instance-scheduler
recovery-drill` creates a small throwaway instance, recovers onto it read-only, checks the
database, the key and that every instance would be reachable, and deletes it again. Set
`LIS_RECOVERY_DRILL_DAYS=7` in `.env` to have it run every week; you get an alert if it fails.
