# Hosting JobAgent on a VPS

A practical guide to moving JobAgent from the MacBook to a Linux VPS. Every coupling below is
grounded in the current code. Paths are relative to the repo root.

---

## 1. Laptop / macOS couplings found in the code

| # | Coupling | Where | VPS impact |
|---|----------|-------|------------|
| 1 | **Microsoft Word via AppleScript for DOCX->PDF.** `word_to_pdf()` requires `/Applications/Microsoft Word.app`, stages files in `~/Library/Containers/com.microsoft.Word/Data/jobagent` (`WORD_CONTAINER`), and runs `osascript`. It is used for both the resume and the cover letter. | `jobagent/writing/resume.py:38`, `:98-138`; wired as the default converter in `jobagent/writing/service.py:35` | **Hard blocker.** Every `prepare` fails on Linux. A code change is needed (see 1a). |
| 2 | Doctor reports Word missing as **FAIL** | `jobagent/runtime.py:43-44` | Cosmetic, but it should check for `soffice` instead |
| 3 | **launchd-only scheduling.** `schedule generate` prints a plist (08:00, `run cycle` only). | `jobagent/runtime.py:88-91`, `jobagent/cli.py:133`, `:502-505` | Replace with systemd timers (section 5). No code change needed. |
| 4 | **Claude Code CLI** runs as a subprocess, `claude -p --restricted --permission-prompts none --output-format json --json-schema ...`, authenticated through the laptop's interactive login | `jobagent/writing/cover_letter.py:63-107`; binary from `JOBAGENT_CLAUDE_BIN` | Install `claude` on the VPS and authenticate it headlessly (section 4.7). If `claude` is not on PATH, cover letters are **skipped without an error** (`jobagent/cli.py:385-387`). |
| 5 | **The cover-letter skill is read from the home directory**: `~/.claude/skills/coverLetter/SKILL.md` | `jobagent/writing/cover_letter.py:69-71` (override: `JOBAGENT_COVER_LETTER_SKILL_PATH`) | If the file is missing, `skill` quietly becomes `""` (`:121`). Copy it over. |
| 6 | **Codex CLI does the scoring** (`codex exec --sandbox read-only ...`), authenticated through the subscription in `~/.codex/auth.json` | `jobagent/scoring.py:51-92`; `JOBAGENT_CODEX_BIN` | Install `codex` and log in headlessly. If it is missing, scoring is **skipped** (`jobagent/cli.py:477,485`), so the queue is empty and no packets are prepared. |
| 7 | **Headed Playwright Chromium** with a persistent profile, `headless=False`, blocking on `input()` for human review | `jobagent/apply/filler.py:40` (`data/browser-profile`), `:254`, `:267-289` | Needs a display (Xvfb plus VNC) and an interactive terminal. See section 2. |
| 8 | **Resume fonts**: the master resume uses **Garamond** (163 runs) plus Times New Roman. The cover-letter template uses the theme fonts **Aptos / Aptos Display**. | `candidate/master_resume.docx`, `candidate/cover_letter_template.docx` | None of these fonts ship with Linux. Substitute fonts change line wrapping, so the one-page checks behave differently (`jobagent/writing/service.py:65-71`, `:92-94`, and `LINE_CHARS = 114` in `resume.py:34`, which is tuned for Garamond 10.5pt). |
| 9 | **Everything uses relative paths** (`.env`, `data/`, `out/`, `logs/`, `candidate/`, `config/`) | `jobagent/environment.py:9`, `jobagent/cli.py:35-40`, `jobagent/runtime.py:57`, `jobagent/writing/service.py:33-38` | Every unit must set `WorkingDirectory=` to the repo. Packet paths are stored in SQLite as relative strings (`out/...`), so migrating the DB works if `out/` moves with it. |
| 10 | **Local timezone.** The cover-letter date uses the system's local timezone (`cover_letter.py:173`). The queue date is the **UTC** date (`cli.py:313-316`). `JOBAGENT_TIMEZONE` is in `.env.example` but nothing reads it. | as listed | Set `TZ=America/New_York` on the VPS. A run after 20:00 ET falls on the next UTC day's queue. |
| 11 | **Lock file** `data/jobagent.lock`, which `run cycle` takes and which is PID-based | `jobagent/runtime.py:53-86` | Fine on a single host. Do not rsync it. |
| 12 | Browser profiles `data/browser-profile` (85 MB) and `data/browser-profile-workday` (65 MB, no longer referenced in code) | `jobagent/apply/filler.py:40` | **Don't copy them.** Chromium cookies on macOS are encrypted with a Keychain key and won't decrypt on Linux. Start a fresh profile. |
| 13 | **The repo has no commits and no remote** (`git log` is empty) | n/a | You cannot `git clone` until you push to a **private** remote. `.gitignore` covers most personal files, but **`candidate/previous/` is not ignored**. Add it before the first commit. |
| 14 | Search profiles and settings fall back to the `*.example.yaml` files because `config/search_profiles.yaml` and `config/settings.yaml` don't exist | `jobagent/cli.py:38-40`, `jobagent/scrape/jobspy_source.py:40` | Create real copies on the VPS if you want non-default limits. |

### 1a. Code change: LibreOffice headless replaces Word

`PreparationService` already takes an injectable `pdf_converter` (`jobagent/writing/service.py:35`). The minimal change is:

1. Add a converter in `jobagent/writing/resume.py`:

```python
def libreoffice_to_pdf(docx: Path, pdf: Path) -> None:
    """Headless LibreOffice export for Linux hosts."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError("LibreOffice (soffice) is required to export PDFs")
    with tempfile.TemporaryDirectory(prefix="jobagent-lo-") as directory:
        # A private profile per call: concurrent or crashed soffice instances never block each other.
        profile = Path(directory, "profile").as_uri()
        result = subprocess.run(
            [soffice, f"-env:UserInstallation={profile}", "--headless", "--norestore",
             "--convert-to", "pdf", "--outdir", directory, str(docx)],
            capture_output=True, text=True, timeout=WORD_TIMEOUT_SECONDS, check=False,
        )
        staged = Path(directory) / f"{docx.stem}.pdf"
        if result.returncode != 0 or not staged.exists() or staged.stat().st_size == 0:
            raise RuntimeError(f"LibreOffice PDF export failed: {result.stderr.strip()[:300]}")
        _write_without_blank_trailing_pages(staged, pdf)


def default_pdf_converter() -> PdfConverter:
    choice = os.getenv("JOBAGENT_PDF_CONVERTER", "word" if sys.platform == "darwin" else "libreoffice")
    return word_to_pdf if choice == "word" else libreoffice_to_pdf
```

2. In `jobagent/cli.py:376` and `:487`, pass `pdf_converter=default_pdf_converter()` to `PreparationService(...)`.
3. In `jobagent/runtime.py:43-44`, check for `shutil.which("soffice")` when not on macOS.
4. Add a unit test that mocks `subprocess.run`, following the existing `pdf_converter` injection pattern in the tests.

**Fidelity caveat:** LibreOffice's layout is close to Word's but not identical. With metric-compatible fonts it is usually the same line for line. Still, compare the first 10 packets by eye against your Word exports. If the resume keeps failing with "does not fit on one page", lower `LINE_CHARS` in `resume.py:34`, for example to 110.

**Fonts** (install on the VPS):
- Times New Roman: `ttf-mscorefonts-installer`, or Liberation Serif, which is metric-compatible.
- **Garamond**: no metric-compatible free clone exists. Either copy your licensed `Garamond*.ttf` from the Mac (`/Library/Fonts` or `~/Library/Fonts`, and check the Office license) into `~/.local/share/fonts/`, or accept EB Garamond (`fonts-ebgaramond`) and re-tune `LINE_CHARS`.
- **Aptos**: an Office cloud font with no Linux package. Copy it from the Mac (it is under `~/Library/Group Containers/UBF8T346G9.Office/FontCache` once Word has downloaded it), or re-save `cover_letter_template.docx` in a font that is available on both machines.
- Check with `fc-list | grep -Ei 'garamond|aptos|times'`.

---

## 2. Recommended split

| Runs unattended on the VPS (systemd timers) | Human in the loop |
|---|---|
| `run cycle`: scrape -> dedupe (in `add_or_get_job`) -> eligibility -> Codex scoring -> queue -> prepare (resume plus Claude cover letter -> PDF) -> Notion sync (`jobagent/pipeline.py:27-70`) | `apply fill`: headed Chromium, `input()` wait, you submit (`jobagent/apply/filler.py:245-289`) |
| `digest send`: Telegram (`cli.py:435-442`) | `status set <id> applied` |
| SQLite backup, failure alerts | Answering captchas and Workday account prompts |

### Form-fill options

**Option A: keep form-fill on the laptop and pull from the VPS.**
`rsync` `out/` and a snapshot of the DB to the laptop, then run `apply fill` locally.
- Pros: a native browser on a residential IP, which means fewer captchas.
- Cons: **two SQLite copies.** `apply fill` writes `status=prefilled` (`cli.py:428`), and `status set` writes locally, so SQLite stops being the single source of truth (see AGENTS.md). You would need a write-back step. Not recommended.

**Option B (recommended): headed Chromium on the VPS inside Xvfb, viewed over an SSH-tunnelled VNC.**
There is one DB, one `out/`, and no code change. The browser runs on the VPS display `:99`. You watch and submit through macOS's built-in **Screen Sharing** (`vnc://localhost:5901`). noVNC is optional if you'd rather use a browser tab.

```bash
# on the VPS (one-time)
sudo apt install -y xvfb x11vnc openbox tmux
# a start script, run inside tmux so input() has a terminal
cat > ~/bin/fill-session <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
Xvfb :99 -screen 0 1600x1000x24 -nolisten tcp &
sleep 1; DISPLAY=:99 openbox &
x11vnc -display :99 -localhost -rfbport 5901 -forever -shared -rfbauth ~/.vnc/passwd -quiet &
cd ~/jobagent && DISPLAY=:99 exec bash
EOF
chmod +x ~/bin/fill-session; x11vnc -storepasswd ~/.vnc/passwd
```

```bash
# on the Mac
ssh -L 5901:localhost:5901 jobagent@vps -t 'tmux new -A -s fill ~/bin/fill-session'
# then: open vnc://localhost:5901    (Screen Sharing)
# in the tmux shell:
uv run jobagent apply fill --queue --limit 5
```

VNC listens on **localhost only** (`-localhost`), so it is reachable only through the SSH tunnel. Expect more captchas on Workday and Greenhouse from a datacenter IP. You solve them yourself in the VNC window, which is the point of the human-in-the-loop design.

---

## 3. Provider and size

- **Recommended:** Hetzner **CPX11** in Ashburn or Hillsboro (US, 2 vCPU / 2 GB, about $5–6/mo) with a **2 GB swapfile**. If a EU region is acceptable, **CX22** (2 vCPU / 4 GB, about €4–5/mo) is a better fit. The alternative is DigitalOcean Basic 2 GB (about $12/mo). Use **Ubuntu 24.04 LTS**.
- Memory drivers are pandas/jobspy, LibreOffice (about 300 MB per conversion), Chromium (about 500 MB), and the Node-based `claude` and `codex` CLIs. Prefer 4 GB if the price allows. Disk: 40 GB is plenty (the DB is 33 MB today).
- Pick a **US region**. Boards geo-target, and `country_indeed: USA` is set.
- Check current prices on the provider pages; they change.

### Datacenter IPs and job boards

python-jobspy scrapes LinkedIn, Indeed, Glassdoor, Google, and ZipRecruiter anonymously. LinkedIn and Indeed rate-limit or block cloud IP ranges much more often (HTTP 429, empty results). The code states "no proxies and no logins" (`jobagent/scrape/jobspy_source.py:30`). In order of preference:

1. **Measure first.** Run `uv run jobagent run cycle --dry-run` for a few days and compare the `scrape` counts with the laptop.
2. **Be polite:** raise `scrape.delay_seconds` (for example to 15–30), lower `results_wanted`, and spread the three daily cycles apart.
3. **Drop blocked boards** from `scrape.sites` in `config/settings.yaml` and rely on `add-url` for those boards.
4. **Route scraping through a residential IP:** a Tailscale exit node on an always-on home device (Raspberry Pi or router), or a paid residential proxy. jobspy accepts `proxies=[...]`, but `ScrapeSettings` doesn't pass it through, so this is a small code change in `jobspy_source.py`. Because it reverses a documented design choice, treat it as your decision, not a default.

---

## 4. Step-by-step setup

### 4.1 User and SSH hardening
```bash
# as root, first login
adduser --disabled-password --gecos "" jobagent
usermod -aG sudo jobagent
mkdir -p /home/jobagent/.ssh && cp ~/.ssh/authorized_keys /home/jobagent/.ssh/
chown -R jobagent:jobagent /home/jobagent/.ssh && chmod 700 /home/jobagent/.ssh && chmod 600 /home/jobagent/.ssh/authorized_keys
passwd jobagent                                # for sudo only

cat >/etc/ssh/sshd_config.d/10-hardening.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
AllowUsers jobagent
X11Forwarding no
EOF
systemctl reload ssh
timedatectl set-timezone America/New_York       # cover-letter date uses local time
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

### 4.2 Base packages, LibreOffice, and fonts
```bash
sudo apt update && sudo apt -y upgrade
sudo apt install -y git sqlite3 unattended-upgrades ufw fail2ban restic \
  libreoffice-writer-nogui fonts-liberation fonts-ebgaramond fontconfig
echo ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true | sudo debconf-set-selections
sudo apt install -y ttf-mscorefonts-installer
# licensed Garamond / Aptos copied from the Mac (see 1a):
mkdir -p ~/.local/share/fonts && fc-cache -f
```

### 4.3 uv, Node, and the AI CLIs
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh        # installs to ~/.local/bin
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt install -y nodejs
npm config set prefix ~/.local && npm i -g @anthropic-ai/claude-code @openai/codex
```

### 4.4 Code
```bash
# on the Mac, first time only: add candidate/previous/ (and .DS_Store, data/*.lock) to .gitignore,
# verify with `git status --ignored`, commit, then push to a PRIVATE remote.
git clone git@github.com:<you>/jobagent.git ~/jobagent   # or: rsync -a --exclude .venv --exclude data --exclude out --exclude candidate ./ vps:~/jobagent/
cd ~/jobagent
uv sync
uv run playwright install --with-deps chromium
```

### 4.5 Personal data: use scp/rsync, never git
```bash
# from the Mac, in the repo root
rsync -av candidate/ jobagent@vps:~/jobagent/candidate/          # profile/experience/skills/application YAML, master_resume.docx, templates, references/
rsync -av config/*.yaml jobagent@vps:~/jobagent/config/            # only if you created real (non-example) configs
ssh jobagent@vps 'mkdir -p ~/.claude/skills/coverLetter'
scp ~/.claude/skills/coverLetter/SKILL.md jobagent@vps:~/.claude/skills/coverLetter/
ssh jobagent@vps 'chmod -R go-rwx ~/jobagent/candidate ~/.claude'
```

### 4.6 SQLite and generated packets
```bash
# on the Mac: stop any running cycle, then take a consistent snapshot (not a raw cp of a live DB)
sqlite3 data/jobagent.sqlite3 ".backup data/migrate.sqlite3"
rsync -av data/migrate.sqlite3 jobagent@vps:~/jobagent/data/jobagent.sqlite3
rsync -av out/ jobagent@vps:~/jobagent/out/                       # stored packet paths are relative: out/<date>/<Company>/...
ssh jobagent@vps 'cd ~/jobagent && uv run jobagent list-jobs | head'
```
Once the VPS is live, **disable the laptop's launchd job** (`launchctl bootout gui/$(id -u)/com.jobagent.cycle`) so two machines never write separate DBs.

### 4.7 Secrets (`.env`, chmod 600, never committed)
```bash
cd ~/jobagent && cp .env.example .env && chmod 600 .env && $EDITOR .env
```
```ini
JOBAGENT_DATABASE=data/jobagent.sqlite3
JOBAGENT_CLAUDE_BIN=/home/jobagent/.local/bin/claude
JOBAGENT_CODEX_BIN=/home/jobagent/.local/bin/codex
JOBAGENT_PDF_CONVERTER=libreoffice
NOTION_API_KEY=...
NOTION_DATABASE_ID=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
WORKDAY_PASSWORD=...
# headless Claude auth: run `claude setup-token` on the Mac (subscription) and paste it here,
# or use ANTHROPIC_API_KEY (pay-per-token billing) instead
CLAUDE_CODE_OAUTH_TOKEN=...
```
- **Codex:** `codex login --device-auth` on the VPS, or `scp ~/.codex/auth.json vps:~/.codex/` and `chmod 600` it.
- `.env` is loaded from the working directory (`jobagent/environment.py`) and only fills keys the shell hasn't set.
- Verify with `uv run jobagent doctor`, `uv run jobagent notify test`, and `uv run jobagent prepare --job-id <id> --force`, then open the PDFs.

---

## 5. systemd units that replace launchd

The launchd plist ran `uv run jobagent run cycle` daily at 08:00 (`cli.py:504`). `config/settings.example.yaml` describes cycles at 08:00, 12:00, and 16:00 plus a digest at 20:00 ET. The units below implement that. `run cycle` already takes `data/jobagent.lock`, so overlapping runs fail safely.

`/etc/systemd/system/jobagent-cycle.service`
```ini
[Unit]
Description=JobAgent daily cycle (scrape, eligibility, score, queue, prepare, Notion)
Wants=network-online.target
After=network-online.target
OnFailure=jobagent-alert@%n.service

[Service]
Type=oneshot
User=jobagent
WorkingDirectory=/home/jobagent/jobagent
Environment=PATH=/home/jobagent/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=TZ=America/New_York
ExecStart=/home/jobagent/.local/bin/uv run --frozen jobagent run cycle
TimeoutStartSec=3h
Nice=10
# hardening: the job only needs its repo and home (claude/codex auth, uv cache)
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=tmpfs
BindPaths=/home/jobagent
ReadWritePaths=/home/jobagent
```

`/etc/systemd/system/jobagent-cycle.timer`
```ini
[Unit]
Description=JobAgent discovery cycles

[Timer]
OnCalendar=*-*-* 08,12,16:00:00 America/New_York
RandomizedDelaySec=10min
Persistent=true

[Install]
WantedBy=timers.target
```

`/etc/systemd/system/jobagent-digest.service` and `.timer`
```ini
[Unit]
Description=JobAgent Telegram digest
After=network-online.target
OnFailure=jobagent-alert@%n.service

[Service]
Type=oneshot
User=jobagent
WorkingDirectory=/home/jobagent/jobagent
Environment=PATH=/home/jobagent/.local/bin:/usr/bin:/bin
ExecStart=/home/jobagent/.local/bin/uv run --frozen jobagent digest send
```
```ini
[Timer]
OnCalendar=*-*-* 20:00:00 America/New_York
Persistent=true
[Install]
WantedBy=timers.target
```

`/etc/systemd/system/jobagent-alert@.service` sends a Telegram message when any unit fails. It reuses `.env`, so no new secrets are involved.
```ini
[Unit]
Description=JobAgent failure alert for %i

[Service]
Type=oneshot
User=jobagent
EnvironmentFile=/home/jobagent/jobagent/.env
ExecStart=/bin/sh -c 'curl -fsS -m 20 "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
  --data-urlencode chat_id="$TELEGRAM_CHAT_ID" \
  --data-urlencode text="JobAgent FAILED: %i on $(hostname). journalctl -u %i -n 50"'
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now jobagent-cycle.timer jobagent-digest.timer jobagent-backup.timer
systemctl list-timers 'jobagent*'
sudo systemctl start jobagent-cycle.service && journalctl -u jobagent-cycle -f     # first manual run
```
`run cycle` exits 1 when any step recorded an error (`cli.py:499`). Partial runs therefore also trigger the alert, which is what you want.

---

## 6. Backups, logs, and monitoring

**SQLite backup:** a consistent online snapshot, then an encrypted offsite copy.

`/home/jobagent/bin/backup.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail
cd /home/jobagent/jobagent
mkdir -p backups
sqlite3 data/jobagent.sqlite3 ".backup backups/jobagent-$(date -u +%F).sqlite3"
sqlite3 "backups/jobagent-$(date -u +%F).sqlite3" "PRAGMA integrity_check;" | grep -qx ok
find backups -name 'jobagent-*.sqlite3' -mtime +14 -delete
# offsite: restic to Backblaze B2 / S3 (creds in /home/jobagent/.restic.env, chmod 600)
set -a; . /home/jobagent/.restic.env; set +a
restic backup backups/ out/ candidate/ config/ --tag jobagent --exclude 'data/browser-profile*'
restic forget --keep-daily 14 --keep-weekly 8 --keep-monthly 6 --prune
```
Set it up with `restic init` once. Add a `jobagent-backup.service` (Type=oneshot, `ExecStart=/home/jobagent/bin/backup.sh`, `OnFailure=jobagent-alert@%n.service`) and a `.timer` with `OnCalendar=*-*-* 03:30:00 America/New_York`. `candidate/` is included because it is your personal truth data and restic encrypts it client-side. Test a restore once with `restic restore latest --target /tmp/r`.

**Logs:** the jobs log to stdout, which goes to journald. Cap its size:
```bash
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=300M\nMaxRetentionSec=1month\n' | sudo tee /etc/systemd/journald.conf.d/jobagent.conf
sudo systemctl restart systemd-journald
```
`logs/fill_reports/*.json` (`cli.py:35`) are small. Prune them with a `find logs -mtime +60 -delete` line in `backup.sh` if needed.

**Monitoring:**
- The `OnFailure=` alerts above cover failed runs.
- The nightly digest is also a heartbeat: if it doesn't arrive, the box is down.
- Optionally add a free dead-man switch (healthchecks.io): add `ExecStartPost=curl -fsS -m 10 https://hc-ping.com/<uuid>` to the cycle service.
- Watch for **silent degradation**. The cycle "succeeds" when `claude` or `codex` is missing, and cover letters or scoring are just skipped. Check `uv run jobagent doctor` output and `cover_letter_error` in packet metadata after any CLI update.

---

## 7. Security

```bash
sudo ufw default deny incoming && sudo ufw default allow outgoing
sudo ufw limit OpenSSH && sudo ufw enable            # no other inbound ports: VNC is localhost-only via the SSH tunnel
sudo systemctl enable --now fail2ban                 # the default sshd jail is enough
sudo dpkg-reconfigure -plow unattended-upgrades
```
- **No public ports** except 22. Notion and Telegram use outbound HTTPS only, and nothing needs a webhook.
- **Secrets:** `.env`, `~/.codex/auth.json`, `~/.claude/` credentials, and `.restic.env` are all `chmod 600` and owned by `jobagent`. They are never in git, never in backups of the repo tree, and never pasted into prompts.
- **AI-safety rules from AGENTS.md still apply on the VPS.** Claude runs `--restricted` with no tools, inside a temp directory (`cover_letter.py:84-96`). Codex runs `--sandbox read-only` in an empty temp workspace (`scoring.py:73-82`). Keep both of those. Never run either CLI with bypass or "yolo" flags on the server. The server holds your Notion, Telegram, and Workday secrets, and job descriptions are untrusted input.
- Everything runs as the unprivileged `jobagent` user, and the systemd hardening options limit what it can write.
- **Personal data** (`candidate/`, the DB, `out/`, browser profile) lives only on an encrypted-at-rest provider volume and in the restic repo. Enable full-disk encryption if the provider offers it.
- The browser profile holds ATS session cookies. Keep it out of backups (it is excluded above) and delete it when you retire the box.

---

## 8. Cost and the code-change checklist

**Estimated monthly cost**

| Item | Cost |
|---|---|
| Hetzner CPX11 (US) / CX22 (EU) | about $5–6 |
| Backups (provider snapshots at 20%, or B2 at about 1 GB) | about $1 |
| Claude (subscription via `setup-token`) and Codex (ChatGPT subscription) | $0 extra (existing plans) |
| Residential proxy (only if boards block you) | $0, or $5–15+ |
| **Total** | **about $6–8/mo** (DigitalOcean 2 GB: about $13–15) |

**Minimal code changes required** (done Oct 8, 2026: the converter is chosen automatically in `PreparationService`; ready-made units and scripts are in `deploy/`; fills wait for the window to close when no terminal is attached)
- [x] **Required:** add `libreoffice_to_pdf()` and `default_pdf_converter()` to `jobagent/writing/resume.py`, and pass it in at `jobagent/cli.py:376` and `:487` (section 1a), with a mocked unit test.
- [x] **Required:** have `doctor` check for `soffice` instead of Word on Linux (`jobagent/runtime.py:43-44`).
- [x] **Required before the first commit:** add `candidate/previous/`, `.DS_Store`, and `data/*.lock` to `.gitignore`.
- [x] Recommended: make `schedule generate` emit systemd units on Linux, or document section 5 (`jobagent/runtime.py:88`, `cli.py:502`).
- [ ] Recommended: re-tune `LINE_CHARS` (`resume.py:34`) after comparing LibreOffice output, or install the licensed Garamond and Aptos.
- [ ] Optional: pass `proxies` through `ScrapeSettings` (`jobagent/scrape/jobspy_source.py:28-36`) if datacenter blocking makes that necessary.
- [ ] Optional: read `JOBAGENT_TIMEZONE` for the queue date (`cli.py:313-316` uses the UTC date today).
- [ ] Afterwards: run `uv run ruff check .` and `uv run pytest`, then do a manual `prepare --job-id` on the VPS and compare the PDF by eye.
