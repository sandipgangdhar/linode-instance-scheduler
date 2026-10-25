# Install and configure — step by step

This guide takes you from nothing to a running Linode Instance Scheduler on its own host. Do the
steps in order: steps 1–6 prepare everything the installer asks for, step 7 creates the host,
step 8 installs. Keep a notes file (or your password manager) open while you work — several steps
show a secret only once.

If something goes wrong with the scheduler host later, [RECOVERY-GUIDE.md](./RECOVERY-GUIDE.md)
walks through moving to a new one. The steps below are what makes that recovery quick and
lossless, so don't skip the Object Storage and age steps.

| Step | What you create | Where it's used |
|---|---|---|
| 1 | A dedicated Linode user | Owns the API token, so the tool's changes are easy to tell apart from people's |
| 2 | An API token for that user | Everything the tool does on your account |
| 3 | An OAuth app ("Login with Linode") | Signing in to the web dashboard |
| 4 | An Object Storage bucket | Backups: database snapshots, per-node records, settings |
| 5 | An Object Storage access key | Lets the tool read and write that one bucket |
| 6 | An age key pair | An encrypted copy of all settings that only you can open |
| 7 | The scheduler host (a Linode) | Runs the scheduler, the API and the dashboard |
| 8 | `install.sh` | Installs and configures everything on the host |

---

## Step 1 — Create a dedicated Linode user

Linode records every change under the user who owns the API token. With a user of its own, the
account's event history shows plainly which changes came from the scheduler and which a person
made in Cloud Manager, and the tool's `diagnose` command can tell them apart too.

1. Cloud Manager → **Account** → **Users & Grants** → **Add a User**.
2. Username: for example `instance-scheduler`. Email: an address your team reads.
3. Access: leave the user **unrestricted**. What the tool can actually do is limited by the API
   token's scopes in step 2, not by this setting. (A restricted user also works, but it then
   needs read/write grants on every instance, volume, IP, firewall and NodeBalancer the tool
   manages, and on new ones as you add them — easy to get out of step.)
4. Accept the invitation from that email and sign in once as the new user. Turn on two-factor
   authentication for it.

## Step 2 — Create the API token (as the new user)

Signed in as `instance-scheduler`: **Profile** (top right) → **API Tokens** → **Create a
Personal Access Token**.

- **Label**: `instance-scheduler`.
- **Expiry**: your choice. The tool warns 14 days before the token expires; you then rotate it
  (`rotate-credentials api-token`) without downtime.
- **Scopes** — set exactly these, everything else **No Access**:

| Scope | Access | Why |
|---|---|---|
| Linodes | Read/Write | Create, boot, delete and inspect instances |
| Volumes | Read/Write | Attach, detach, clone, tag volumes |
| IPs | Read/Write | Reserve and move public IP addresses |
| VPCs | Read Only | Read VPC addresses (needed if any instance uses a VPC) |
| Firewalls | Read/Write | Re-attach Cloud Firewalls on every start; wake on demand opens its own port range on the scheduler's firewall |
| NodeBalancers | Read/Write | Nodes behind a NodeBalancer; wake through a NodeBalancer |
| StackScripts | Read/Write | Keeps a recovery copy of the SSH-key passphrase in a private StackScript |
| Events | Read Only | `diagnose` reads Linode's own record of changes |
| Account | Read Only | Startup check; dashboard sign-in checks the person is a user of this account |
| Kubernetes | Read/Write | Only if you schedule LKE node pools; otherwise No Access |
| Object Storage | No Access | The tool uses its own access key from step 5 instead |

Optional: **Account Read/Write** instead of Read Only also lets the tool register its deployment
SSH key in this user's profile. It's not needed; without it the tool notes once that it skipped
that step.

Copy the token now — Cloud Manager shows it once. Store it in your password manager as
"scheduler API token".

## Step 3 — Create the OAuth app (for the dashboard)

Skip this step if you'll use only the command line. The dashboard's "Login with Linode" button
needs an OAuth app in your account.

1. Decide the dashboard's address now. The recommended setup is a DNS name pointing at the
   scheduler host, served over HTTPS by the installer (step 8, `--domain`), for example
   `scheduler.example.com`. You'll create the DNS record once the host exists (step 7).
2. Cloud Manager → **Profile** → **OAuth Apps** → **Add an OAuth App**.
   - **Label**: `Instance Scheduler`.
   - **Callback URL**: `https://scheduler.example.com/oauth/callback` (your name, same path).
   - **Public**: **leave unticked** (the dashboard keeps the secret on the server).
3. Copy the **Client ID** and **Client Secret** (the secret is shown once).

Only users of your Linode account can sign in. You decide what each person may do in the
dashboard under **Users & teams** after installing (viewer, operator, editor or admin, optionally
limited to some groups or nodes).

## Step 4 — Create the Object Storage bucket

Cloud Manager → **Object Storage** → **Create Bucket**.

- **Label**: for example `instance-scheduler-backups-<company>` (labels are unique per region).
- **Region**: any region; a region close to the scheduler host is a good default. Keep it in a
  different data centre from your managed instances if you want backups to survive a whole-region
  problem.
- After creating it, open the bucket's **Details** and copy its **endpoint**, e.g.
  `https://in-maa-1.linodeobjects.com`.

Leave the bucket private. The tool turns on versioning for it (old versions kept 30 days), so an
overwritten or deleted backup can be brought back.

## Step 5 — Create the Object Storage access key

Cloud Manager → **Object Storage** → **Access Keys** → **Create Access Key**.

- **Label**: `instance-scheduler`.
- **Limited access**: on. Give **Read/Write** to the bucket from step 4 only, no access to any
  other bucket.
- Copy the **Access Key** and the **Secret Key** (the secret is shown once).

## Step 6 — Create your age key pair

The scheduler can keep an encrypted copy of all its settings (API token, bucket keys, OAuth
secret, passphrase) and its SSH key in the bucket, encrypted with a public key whose private half
only you hold. With it, recovering onto a new host needs only the bucket settings and your key
file.

On your own computer (not the scheduler host), install [age](https://age-encryption.org) and run:

```
age-keygen -o scheduler-age-key.txt
```

It prints the public key, a line starting `age1…`. Keep `scheduler-age-key.txt` in your password
manager or a vault — it's what opens the copy during a recovery. The installer asks for the public
`age1…` line only.

(No age installed? After installing, `sudo linode-instance-scheduler env-backup-keygen` makes a
pair too — then move the private file off the host.)

## Step 7 — Create the scheduler host

Create one Linode for the scheduler. We recommend a **Dedicated CPU** plan rather than a shared
one: the scheduler's starts, stops and checks then never slow down when neighbours on the same
hardware are busy, and Smart Ask answers noticeably faster.

Choose the size by which Ask features you want:

| You want | Plan (Dedicated CPU) | What you get |
|---|---|---|
| The scheduler, dashboard and **Ask** (instant answers) | **Dedicated 4 GB** (2 CPUs) | Everything except Smart Ask |
| …plus **Smart Ask** (a model on the host picks the best answer when Ask isn't sure) | **Dedicated 8 GB** (4 CPUs) | Smart Ask with Qwen3 4B |
| …plus Smart Ask with the **better** model (recommended for Smart Ask) | **Dedicated 16 GB** (8 CPUs) or larger | Smart Ask with Qwen 2.5 7B |

The installer picks the Smart Ask model from the host's memory by itself; on a 4 GB host Smart
Ask stays off. Smart Ask downloads 3–5 GB once; every plan above has the disk for it.

Other settings:

- **Image**: Ubuntu 24.04 LTS (Debian 12+, Rocky/Alma 9+ also work).
- **Region**: the region of your managed instances. Any region works for instances with a public
  IP, but if any managed instance has **only** VPC or VLAN interfaces (no public IP), or you'll use
  **wake through a NodeBalancer**, the scheduler host must be in the same region and in that VPC
  (add a VPC interface when creating it) — the tool reaches those instances at their private
  address.
- **Root password / SSH key**: your own admin key.
- **Cloud Firewall**: inbound TCP 22 from your admin addresses only; TCP 80 and 443 from wherever
  people open the dashboard (needed for the HTTPS certificate). Everything outbound allowed.
  Don't open the API port (8000); the dashboard reaches it through Caddy on 443.
- **Backups**: optional — everything the scheduler needs is already backed up to Object Storage.

When it's running, create the DNS record from step 3 (an **A** record for
`scheduler.example.com` pointing at the host's public IPv4) and wait until it resolves.

## Step 8 — Install

SSH to the host as root, then:

```
git clone https://github.com/sandipgangdhar/linode-instance-scheduler.git
cd linode-instance-scheduler
sudo ./install.sh install --domain scheduler.example.com --env-backup-recipient age1yourpublickey...
```

The installer downloads the latest release and checks its checksum, then asks, in order:

| Question | Answer with |
|---|---|
| Linode API token | Step 2 |
| Object Storage bucket, endpoint, access key, secret key | Steps 4 and 5 |
| "Login with Linode": client ID, secret, callback URL | Step 3 (the callback URL defaults to `https://<domain>/oauth/callback`) |
| Your age public key | Step 6 (skipped when passed with `--env-backup-recipient`) |

Nothing is saved until every question is answered, so an interrupted run leaves nothing
half-written. The installer then creates a service user, the deployment SSH key, the scheduler and
API services, an hourly backup timer, Smart Ask (when the host has the memory), and the HTTPS
dashboard at `https://scheduler.example.com`.

At the end it shows two things — keep both:

1. **The deployment SSH public key.** Every instance you'll manage must trust it: add it to
   `/root/.ssh/authorized_keys` on each instance (or to the Linode profile's SSH keys before
   creating new instances). The dashboard's onboarding and migration screens can also install it
   for you with a one-time root password.
2. **The SSH-key passphrase.** It's already stored safely (encrypted copies in the bucket and in a
   private StackScript in your account), but keep your own copy in the password manager too.

Useful options: `--no-smart-ask` (skip Smart Ask), `--no-api` (CLI and scheduler only),
`--heartbeat-url <url>` (an uptime monitor pinged every minute, so you hear about it if the whole
host goes away), `--env-file <file> --yes` (non-interactive). `sudo ./install.sh --help` lists
them all.

## Step 9 — Check it

```
sudo ./install.sh status                         # services, latest backups, managed instances
sudo linode-instance-scheduler token-check       # the API token works
sudo linode-instance-scheduler doctor            # everything consistent
sudo linode-instance-scheduler alerts-check      # backups, key copies, settings copy all in place
```

Open `https://scheduler.example.com`, sign in with Linode, and look at **System backup**: every
item should be green. Then:

- **Users & teams**: give your colleagues roles. Unrestricted users of your Linode account are
  admins; everyone else waits for access until you add them to a team.
- **Alerts**: set a webhook (Slack/Teams) or email so you hear about failed backups or a stopped
  scheduler.
- Onboard your first instance: see the README (onboarding and migration methods), or the
  dashboard's **Onboard** page.

Upgrading later is the same command: re-run `sudo ./install.sh install` in an updated clone (or
with `--version vX.Y.Z`). It keeps your settings, key and database, takes a snapshot first and
restarts the services.

## What to keep safe, in one place

| Item | Needed for |
|---|---|
| API token (step 2) | Recovery (unless you use the age copy) |
| Bucket name, endpoint, access key, secret key (steps 4–5) | **Every** recovery |
| age private key file (step 6) | Recovery with `--env-from-backup` (the easiest one) |
| OAuth client ID and secret (step 3) | Recovery (unless you use the age copy) |
| SSH-key passphrase (shown at install) | Last-resort recovery of the SSH key |
