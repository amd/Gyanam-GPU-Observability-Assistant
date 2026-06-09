# Monitoring Scripts

This directory contains scripts for monitoring Docker volume growth and disk usage for the Gyanam GPU Observability Framework.

## Scripts

### monitor_volumes.sh
Displays current Docker volume sizes and disk usage.

**Usage:**
```bash
./scripts/monitor_volumes.sh
```

**Output:**
- Volume sizes for influxdb-data, grafana-data, and shared-data
- Disk space on Docker partition (color-coded: green <80%, yellow 80-90%, red >90%)
- Total Docker system usage

**Requirements:**
- `jq` (optional, for detailed output)
- `sudo` access (for du command on volume mountpoints)

### log_volume_growth.sh
Tracks volume growth over time by appending measurements to `docker_volume_growth.log`.

**Usage:**
```bash
# Manual run
./scripts/log_volume_growth.sh

# Automatic tracking (add to crontab)
crontab -e
# Add this line to run every hour:
0 * * * * /path/to/gyanam/scripts/log_volume_growth.sh
```

**Output file:** `docker_volume_growth.log` (CSV format)
```
timestamp,volume_name,size_bytes
2026-04-24 10:00:00,gyanam_influxdb-data,1234567890
2026-04-24 10:00:00,gyanam_grafana-data,123456789
2026-04-24 10:00:00,disk_available,98765432100
```

**Features:**
- Automatically retains only last 90 days of data
- CSV format for easy analysis with Excel, Python, etc.

### alert_disk_space.sh
Sends alerts when Docker disk usage exceeds a threshold.

**Usage:**
```bash
# Manual run
./scripts/alert_disk_space.sh

# Automatic monitoring (add to crontab)
crontab -e
# Add this line to check every 15 minutes:
*/15 * * * * /path/to/gyanam/scripts/alert_disk_space.sh
```

**Configuration:**
- Default threshold: 80%
- Override: `export DISK_ALERT_THRESHOLD=90`
- Customize alerting mechanisms in the script (Slack, email, PagerDuty, etc.)

**Alert destinations (edit script to enable):**
- Syslog (enabled by default)
- Slack webhook
- Email
- Custom webhook/API

### run-codeql.sh

Reproducible local CodeQL run (security-extended + code-quality). Builds
the Python database, runs both suites, applies the project's path-based
filter from `.github/codeql/codeql-config.yml`, and writes
`.codeql-results/results.sarif`.

**Usage:**
```bash
./scripts/run-codeql.sh
```

**Prerequisites (one-time):**
```bash
mkdir -p ~/.codeql && cd ~/.codeql
curl -L -o bundle.tar.zst \
  https://github.com/github/codeql-action/releases/latest/download/codeql-bundle-linux64.tar.zst
tar --use-compress-program=unzstd -xf bundle.tar.zst
```

After that, the script self-contains. Output goes to:
- `.codeql-results/results.sarif` — filtered SARIF, committed to repo
- `docs/CODEQL_REPORT.md` — human-readable audit (hand-curated)

### export_influxdb_data.py

Underlying Python script for the `./gyanam.sh influx-export` family.
Standalone usage:
```bash
python3 scripts/export_influxdb_data.py --help
```
See [`docs/DATA_EXPORT_REFERENCE.md`](../docs/DATA_EXPORT_REFERENCE.md)
for the full env-variable + workflow reference.

### test_bmc_alerts.py

Interactive probe to test BMC SSE/webhook capabilities. Not part of the
production pipeline; intentionally uses `verify=False` against
self-signed-cert BMCs (excluded from CodeQL via `paths-ignore`).

## Regenerating diagrams

The architecture and class diagrams under `docs/` are stored as
Mermaid source (`.mmd`) — that's the source of truth. The companion
`.pdf` files are renders and need to be regenerated whenever you edit
the `.mmd`.

The simplest path uses the official Mermaid CLI in a transient
container (no host install required):

```bash
# From the repo root
docker run --rm -v "$(pwd)/docs:/data" minlag/mermaid-cli:latest \
  -i /data/architecture.mmd -o /data/architecture.pdf

docker run --rm -v "$(pwd)/docs:/data" minlag/mermaid-cli:latest \
  -i /data/class-diagram.mmd -o /data/class-diagram.pdf
```

If you'd rather install once:

```bash
npm install -g @mermaid-js/mermaid-cli
mmdc -i docs/architecture.mmd  -o docs/architecture.pdf
mmdc -i docs/class-diagram.mmd -o docs/class-diagram.pdf
```

Commit both the updated `.mmd` and the regenerated `.pdf` together so
GitHub renders the PDF preview for non-Mermaid readers.

## Setup Instructions

### 1. Make scripts executable
```bash
chmod +x scripts/*.sh
```

### 2. Test the monitor script
```bash
./scripts/monitor_volumes.sh
```

### 3. Set up automated monitoring (optional)

**Hourly volume growth tracking + 15-min disk alerts:**
```bash
crontab -e
```

Add these lines (replace /path/to/gyanam with your actual installation path):
```cron
# Track volume growth every hour
0 * * * * /path/to/gyanam/scripts/log_volume_growth.sh

# Check disk space every 15 minutes
*/15 * * * * /path/to/gyanam/scripts/alert_disk_space.sh
```

**Note:** You can also use `$HOME/gyanam/...` if installed in your home directory.

### 4. Configure alerting (optional)

Edit `alert_disk_space.sh` to enable your preferred alerting method:
- Uncomment and configure Slack webhook
- Set up email (requires mailx/sendmail)
- Add custom webhook for your monitoring system

## Integration with gyanam.sh

The monitor functionality is also available via the main management script:

```bash
./gyanam.sh monitor
```

This provides a quick view of volume usage and disk space without needing to call the scripts directly.

## Analyzing Growth Logs

### View recent growth
```bash
tail -20 docker_volume_growth.log
```

### Calculate daily growth (InfluxDB volume)
```bash
# Compare size from 24 hours ago to now
grep "influxdb-data" docker_volume_growth.log | tail -2
```

### Plot with Python (example)
```python
import pandas as pd
import matplotlib.pyplot as plt

df = pd.read_csv('docker_volume_growth.log', comment='#',
                 names=['timestamp', 'volume', 'bytes'])
df['timestamp'] = pd.to_datetime(df['timestamp'])
df['GB'] = df['bytes'] / (1024**3)

for vol in df['volume'].unique():
    data = df[df['volume'] == vol]
    plt.plot(data['timestamp'], data['GB'], label=vol)

plt.legend()
plt.xlabel('Time')
plt.ylabel('Size (GB)')
plt.title('Docker Volume Growth')
plt.xticks(rotation=45)
plt.tight_layout()
plt.savefig('volume_growth.png')
```

## Troubleshooting

### "Permission denied" errors
The scripts need sudo access to read Docker volume mountpoints:
```bash
# Add your user to docker group (logout/login required)
sudo usermod -aG docker $USER

# Or run with sudo
sudo ./scripts/monitor_volumes.sh
```

### jq not installed
Install jq for enhanced output:
```bash
# Ubuntu/Debian
sudo apt-get install jq

# RHEL/CentOS
sudo yum install jq

# macOS
brew install jq
```

Scripts will work without jq but with reduced detail.

### Cron jobs not running
Check cron logs:
```bash
grep CRON /var/log/syslog
```

Ensure full paths are used in crontab entries.
