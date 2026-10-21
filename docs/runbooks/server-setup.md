# Server setup: the Sales Agent on the Oracle free ARM server

Written 2026-10-21 for Step 1 (plan: docs/plans/step1-server.md). Follow it top to bottom. Each step says what to run,
what you should see, and what to do if you don't. Claude runs the server commands in your Terminal panel when you ask
(you approve each one); the browser steps are yours.

**Rules for the whole guide**
- Never paste a key, password or the contents of `.env` into a chat or a terminal command. `.env` is copied as a file.
- Free tiers and console menus change. If a screen looks different, describe it to Claude; don't guess.
- Verified 2026-10-21: Oracle's Always Free idle-reclaim rule and the 20% thresholds (Oracle docs), Healthchecks.io
  free plan of 20 checks (its pricing page), Backblaze B2's first 10 GB free with no card (its pricing page),
  Supabase is Postgres 17.6. Assumed, confirm on the screen: that Pay As You Go accounts are exempt from idle
  reclaim (Oracle's 2023 notice), Oracle's current menu names.

---

## 0. Before you start (laptop)
- [ ] Keys rotated (plan, Part 2 §1). The `.env` on the laptop holds the new ones, and `.\zypher.ps1 preflight`
      and `.\zypher.ps1 status` both work with them.
- [ ] Code pushed to GitHub (`zypher-solutions/zypher-sales-agent`, private). Claude commits and pushes when you ask.
- [ ] A Healthchecks.io account (free, sign up with the company email). Create two checks:
      `sales-agent` (period 5 min, grace 20 min) and `backup` (period 1 day, grace 2 hours). Add the Telegram
      integration in Healthchecks so alerts also reach your phone. Copy each check's ping URL into `.env` yourself:
      `HEALTHCHECKS_URL=` and `HEALTHCHECKS_BACKUP_URL=`.
- [ ] Optional now, recommended: a Backblaze B2 account for off-server backups (section 9).

## 1. Oracle account
1. Sign up at oracle.com/cloud/free. **Home region: India West (Mumbai)** (or India South (Hyderabad)). It can never
   be changed.
2. **Upgrade to Pay As You Go** (Billing, then Upgrade and Manage Payment). Why: Oracle may reclaim Always Free
   servers whose CPU, network and memory all stay under 20% for 7 days, and this agent is nearly idle. Pay As You Go
   accounts are not reclaimed, and Always Free resources stay free.
3. **Set a budget alert first:** Billing, then Budgets, then Create Budget: ₹100 a month, alert at 1%. Any charge at
   all then emails you. Only ever create resources marked "Always Free eligible".

## 2. SSH key (laptop, PowerShell)
An SSH key is a pair of files: the private one stays on your laptop, the public one goes on the server. Logging in
then needs the file, not a password.
```powershell
ssh-keygen -t ed25519 -f $HOME\.ssh\zypher_oracle -C "zypher-oracle"
```
Type a passphrase when asked (you will type it when connecting). Then show the PUBLIC half, to paste into Oracle:
```powershell
Get-Content $HOME\.ssh\zypher_oracle.pub
```
Never share `zypher_oracle` (without `.pub`).

## 3. Create the server
Compute, then Instances, then Create instance:
- Name `zypher-1`. Image: **Canonical Ubuntu 24.04** (not "Minimal" unless only that is offered).
- Shape: Change shape, then Ampere, then **VM.Standard.A1.Flex, 2 OCPU, 12 GB memory**.
- Networking: create a new VCN and public subnet; **assign a public IPv4 address**.
- SSH keys: Paste public key (the line from step 2).
- Boot volume: 50 GB (within the free 200 GB).
- Create. When it is Running, copy its **Public IP address**.

**"Out of capacity"** (common for free ARM servers):
1. Retry at off-peak hours (early morning IST), a few times a day.
2. If the region shows several availability domains (AD-1, AD-2...), try each.
3. Create it at 1 OCPU / 6 GB, and resize later (Edit, then Shape) when capacity frees up.
4. Pay As You Go accounts usually get capacity more easily (another reason for step 1.2).
5. Do not run scripts that retry the API all day: that can get the account flagged.

Add the server to `$HOME\.ssh\config` on the laptop (create the file if missing), with your IP:
```
Host zypher
    HostName <public IP>
    User ubuntu
    IdentityFile ~/.ssh/zypher_oracle
```
Test: `ssh zypher`. Answer `yes` to the fingerprint question once. You should see an `ubuntu@zypher-1:~$` prompt.

## 4. Ubuntu basics (on the server)
```bash
sudo apt update && sudo apt full-upgrade -y
sudo timedatectl set-timezone Asia/Kolkata
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades          # answer Yes: security updates install themselves
```
Let the server reboot itself after security updates at 03:30 IST (this also proves "comes back after a reboot"
every week):
```bash
echo 'Unattended-Upgrade::Automatic-Reboot "true";'          | sudo tee /etc/apt/apt.conf.d/52zypher-reboot
echo 'Unattended-Upgrade::Automatic-Reboot-Time "03:30";'    | sudo tee -a /etc/apt/apt.conf.d/52zypher-reboot
```
SSH with keys only (Oracle's Ubuntu image already disables passwords; this checks it):
```bash
sudo sshd -T | grep -E '^passwordauthentication'   # expect: passwordauthentication no
```
Then `sudo reboot`, wait a minute, `ssh zypher` again.

## 5. Firewall
Two layers on Oracle: the **Security List** (Oracle's firewall in front of the server, set in the web console) and
**iptables** (rules inside Ubuntu, `/etc/iptables/rules.v4`). Both must allow a port for it to be reachable.
- This agent only makes outgoing connections (Supabase, Telegram, mail, Groq). **Only SSH (22) must be open**, and
  both layers allow 22 by default. Change nothing now.
- Ports 80 and 443 are opened in Step 2, together with the Console and Caddy, in both layers. Never open the
  database or any other port.
Check: `sudo iptables -L INPUT -n --line-numbers` lists an ACCEPT for `dpt:22` and a final REJECT.

## 6. Docker Engine (on the server)
From Docker's official guide for Ubuntu (docs.docker.com/engine/install/ubuntu); it detects arm64 by itself:
```bash
sudo apt install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker ubuntu        # run docker without sudo (log out and back in once)
systemctl is-enabled docker           # expect: enabled  (Docker starts on boot, and with it the agent)
```
Log out (`exit`), `ssh zypher` again, then `docker run --rm hello-world` prints "Hello from Docker!".
No Docker account or sign-in is needed.

## 7. Get the code (on the server)
A **deploy key** is an SSH key that can only read this one GitHub repository.
```bash
ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N "" -C "zypher-1 deploy"
cat ~/.ssh/github_deploy.pub
```
GitHub, then the repo, then Settings, then Deploy keys, then Add: paste it, title `zypher-1`, leave "Allow write
access" OFF. Then:
```bash
printf 'Host github.com\n  IdentityFile ~/.ssh/github_deploy\n' >> ~/.ssh/config
sudo mkdir -p /opt/zypher && sudo chown ubuntu:ubuntu /opt/zypher
git clone git@github.com:zypher-solutions/zypher-sales-agent.git /opt/zypher/zypher-sales-agent
```

## 8. `.env` and first start
From the **laptop** (PowerShell, in the project folder). This copies the file; it never prints it:
```powershell
scp .env zypher:/opt/zypher/zypher-sales-agent/.env
```
On the **server**:
```bash
cd /opt/zypher/zypher-sales-agent
chmod 600 .env                 # only the ubuntu user can read it
sed -i 's/\r$//' .env          # Windows line endings off
```
Edit the server's copy for the first day (`nano .env`; Ctrl+O saves, Ctrl+X exits):
- `SENDING_ENABLED=false` for the first 24 hours (approvals work; nothing is emailed).
- `DRAFT_SCHEDULE_ENABLED=true`, `DRAFT_DAILY_LIMIT=10`.
- `AGENT_HOST_LABEL=server`.
- Check `SENDER_LOGO_PATH=assets/logo.png` (a path inside the project).
- No value may contain a `$` (Docker Compose reads `$` as the start of a variable). When rotating a password,
  generate one without `$`. Check without showing values: `grep -c '\$' .env` must print `0`.

**Stop the laptop agent** (Ctrl+C in its window). Then on the server:
```bash
docker compose up -d --build
docker compose ps                        # after ~2 min: sales-agent "Up ... (healthy)", backup "Up"
docker compose logs -f sales-agent       # expect "Everything is running" and a line every minute
```
Close the laptop. A Telegram `/queue` should still answer. Healthchecks shows `sales-agent` green within 5 minutes.

## 9. Off-server backups (Backblaze B2)
The backup service writes `/opt/zypher/zypher-sales-agent/backups/` every night at 02:30 IST and keeps 14 days.
To also keep copies away from Oracle (recommended: if the Oracle account goes, the backups go with it):
1. Backblaze, B2 Cloud Storage: create a **private** bucket, e.g. `zypher-backups-<random>`.
2. Application Keys: Add a key limited to that bucket, read and write. Put the two values into the server's `.env`
   yourself: `RCLONE_CONFIG_B2_ACCOUNT=` (keyID), `RCLONE_CONFIG_B2_KEY=` (applicationKey), and
   `BACKUP_REMOTE=b2:<bucket name>/sales-agent`.
3. `docker compose up -d backup`, then test one now: `docker compose run --rm backup once`. Expect "wrote … .dump"
   and "copied to b2:…", and the `backup` check in Healthchecks turns green.

## 10. Cut-over (after 24 hours with SENDING_ENABLED=false)
- `docker compose ps` healthy, Healthchecks green, Telegram cards arrived from the 08:00 drafting.
- In the server's `.env`: `SENDING_ENABLED=true`. Clear `SEND_ALLOWLIST=` only when you have decided to go live.
- `docker compose up -d` (re-reads `.env`, restarts the agent).

## 11. Day to day
| Task | Command (on the server, in /opt/zypher/zypher-sales-agent) |
|---|---|
| Status | `docker compose ps` |
| Logs | `docker compose logs --tail 100 sales-agent` |
| Code update | `git pull && docker compose up -d --build` (tag first: `git tag deploy-$(date +%F)`) |
| Undo a bad update | `git checkout deploy-<date> && docker compose up -d --build` |
| Change a setting | `nano .env`, then `docker compose up -d` |
| Find leads (paid) | `docker compose exec sales-agent python -m zypher_agent source --query "..." --location "..." --max 20` |

## 12. Restore a backup
Tested 2026-10-21 into a throwaway database: every table's row count matched the live one.
```bash
cd /opt/zypher/zypher-sales-agent
docker compose stop sales-agent                     # nothing writes while restoring
ls backups/                                         # pick a file
docker compose run --rm -T --entrypoint sh backup -c \
  'pg_restore --clean --if-exists --no-owner --no-privileges --dbname "$DATABASE_URL" /backups/zypher-internal-YYYY-MM-DD.dump'
docker compose start sales-agent
```
On the laptop, `.\zypher.ps1 status` shows the restored counts. A message "schema public already exists" is harmless.
To restore from B2 when the server is gone: download the file from the Backblaze web console, then run the same
`pg_restore` from any machine with Docker (`docker run --rm -it -v ${PWD}:/b postgres:17-alpine pg_restore ...`).

## 13. If the server dies (rollback to the laptop)
1. If the server still answers: `docker compose stop sales-agent`. If not, skip this.
2. Laptop: `.\zypher.ps1 start`. If the old server copy's database connection is still open, the laptop copy waits
   a minute or two until it closes, then takes over by itself.
3. Rebuild a server later by repeating this guide (about an hour), then restore nothing: the data is in Supabase.
   Backups are only needed if Supabase itself loses data.

## 14. Done when
- 7 days running on the server without you starting anything (Healthchecks history shows it).
- Telegram cards arrive with the laptop off.
- `sudo reboot` brings it back `healthy` within 3 minutes, with no command from you.
