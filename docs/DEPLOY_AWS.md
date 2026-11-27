# Deploying to AWS EC2

One small instance running Docker Compose: PostgreSQL, the one-shot pipeline job, the Streamlit dashboard, the
internal mock API and Caddy (automatic HTTPS). Only ports 80/443 are public. Nothing here creates other AWS resources.

## 1. Launch the instance

- **AMI:** Amazon Linux 2023 or Ubuntu 24.04 (arm64 or x86_64).
- **Size:** a 2 GB instance (e.g. `t4g.small`) is enough for this dataset. Check current EC2 pricing in your region.
- **Storage:** 16 GB gp3.
- **Security group:** inbound 80 and 443 from anywhere; 22 from **your IP only**. No other ports.
- **Elastic IP:** allocate and associate one, so the address survives restarts (a public link must not change).

## 2. Point a domain at it

Create an `A` record (e.g. `recon.yourname.dev`) for the Elastic IP. Caddy obtains a Let's Encrypt certificate
automatically once DNS resolves. Without a domain you can start with `DOMAIN=:80` (plain HTTP) to smoke-test.

## 3. Install Docker and start

```bash
# Amazon Linux 2023
sudo dnf install -y docker git && sudo systemctl enable --now docker && sudo usermod -aG docker $USER
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m) \
     -o /usr/local/lib/docker/cli-plugins/docker-compose && sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
# log out and back in, then:
git clone https://github.com/jjeong4335/finrecon && cd finrecon
cp .env.example .env && nano .env      # POSTGRES_PASSWORD, DASHBOARD_DB_PASSWORD, DOMAIN, GITHUB_URL
docker compose up -d --build
docker compose logs pipeline            # should end with the scenario-validation line
```

Open `https://<your-domain>`.

## 4. Secrets

`.env` lives only on the instance (`chmod 600 .env`). The public dashboard runs in offline/snapshot mode and needs no
QuickBooks or Stripe credentials. If you later run live ingestion on the server, store the QBO refresh token and the
Stripe test key in AWS Systems Manager Parameter Store (SecureString) and load them at start-up instead of `.env`.

## 5. Operate

| Task | Command |
|---|---|
| Status / logs | `docker compose ps` · `make logs` |
| Rebuild demo data | `make rebuild-data` |
| Serve a captured sandbox snapshot | set `FIXTURE_DIR=fixtures/live_snapshot` in `.env`, then `make rebuild-data` |
| Backup | `make backup` (gzip `pg_dump` in the current folder) |
| Stop (keeps data) | `docker compose stop` |
| Update code | `git pull && docker compose up -d --build` |

Add an uptime check (any free HTTP monitor) on the public URL so you know before a reviewer does.

## 6. Teardown

`docker compose down -v` (removes containers and volumes), then terminate the instance and **release the Elastic IP**
(an unattached Elastic IP is billed).

## Optional improvements (not required for the demo)

Scheduled ingestion with EventBridge + a container task, raw archive synced to S3 with a lifecycle rule, Parameter
Store for all secrets, CloudWatch log shipping.
