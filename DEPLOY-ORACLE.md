# Deploying for free on Oracle Cloud

Oracle Cloud's **Always Free** tier includes a virtual server that is free with no time limit.
The whole app runs on that one server with Docker Compose:
- the app container (the same image as on Railway: frontend, API and worker);
- PostgreSQL and Redis;
- a front Caddy server that gets and renews a free HTTPS certificate.

Without a domain of your own, the site address is `<server-ip>.sslip.io`. [sslip.io](https://sslip.io)
is a free service that turns an IP address into a hostname, which is enough for HTTPS.

Files: [deploy/oracle/docker-compose.yml](deploy/oracle/docker-compose.yml),
[deploy/oracle/Caddyfile](deploy/oracle/Caddyfile) and
[deploy/oracle/setup.sh](deploy/oracle/setup.sh).

## 1. Create the account

1. Sign up at https://www.oracle.com/cloud/free/ and choose a **home region** close to your
   users. It cannot be changed later.
2. Oracle asks for a card to verify your identity. Always Free resources are not charged.

## 2. Create the server

1. In the console, open **Compute → Instances → Create instance**.
2. **Image**: Canonical Ubuntu 24.04.
3. **Shape**: Ampere, `VM.Standard.A1.Flex`, with 2 OCPUs and 12 GB of memory. The free
   allowance is up to 4 OCPUs and 24 GB in total.
4. **Networking**: keep "Create new virtual cloud network" and "Assign a public IPv4 address".
5. **SSH keys**: choose "Generate a key pair for me" and **download the private key**. You
   need it to log in.
6. Click **Create** and wait until the instance is **Running**. Note its **public IP address**.

"Out of capacity" means free servers are busy in that availability domain. Pick another
availability domain in the same form, or try again later.

## 3. Open ports 80 and 443

1. On the instance page, open its **subnet**, then the subnet's **Default Security List**.
2. **Add Ingress Rules**: source CIDR `0.0.0.0/0`, IP protocol TCP, destination port `80`.
3. Add a second rule the same way for port `443`.

The setup script opens the same ports in the server's own firewall.

## 4. Install and start the app

From a terminal on your computer (Windows PowerShell works), log in with the downloaded key:

```bash
ssh -i path/to/ssh-key.key ubuntu@YOUR_SERVER_IP
```

On the server, run:

```bash
curl -fsSL https://raw.githubusercontent.com/shreyasvishwa1/ai-software-engineer-codebase/main/deploy/oracle/setup.sh -o setup.sh
bash setup.sh
```

The first run takes about 10 minutes. It ends with `Ready: https://<server-ip>.sslip.io`.
Open that address, register and sign in.

To use your own domain instead, point its DNS `A` record at the server IP and run
`bash setup.sh your.domain.com` before the first start.

## 5. Optional features

Edit the settings file on the server, then restart:

```bash
nano ~/ai-software-engineer-codebase/deploy/oracle/.env
cd ~/ai-software-engineer-codebase/deploy/oracle && sudo docker compose up -d
```

- **AI explanations (free)**: set `GEMINI_API_KEY` to a key from https://aistudio.google.com/apikey.
  On the free tier, Google may use the explained files to improve its products.
- **Password reset emails**: set `SMTP_HOST=smtp.gmail.com`, `SMTP_USERNAME` and `SMTP_FROM`
  to your Gmail address, and `SMTP_PASSWORD` to a Google app password. Reset links use
  `https://<SITE_ADDRESS>` automatically.

`.env` also holds the generated database password and `SECRET_KEY`. Keep it private, and do
not change `POSTGRES_PASSWORD` after the first start.

## Updating

After new commits are pushed to GitHub:

```bash
cd ~/ai-software-engineer-codebase && git pull && cd deploy/oracle && sudo docker compose up -d --build
```

## Useful commands

All of these run from `~/ai-software-engineer-codebase/deploy/oracle`.

| Task | Command |
| --- | --- |
| Status | `sudo docker compose ps` |
| Logs | `sudo docker compose logs -f app` (or `edge` for HTTPS problems) |
| Restart | `sudo docker compose restart` |
| Database backup | `sudo docker compose exec postgres pg_dump -U ai_se ai_software_engineer > backup.sql` |

## Things to know

- Oracle may reclaim Always Free servers that stay almost idle for a week. Regular use avoids
  this, and so does upgrading the account to Pay As You Go: Always Free resources stay free.
- Data lives in Docker volumes on the server. Take a database backup before deleting it.
- Only ports 80 and 443 are public. PostgreSQL, Redis and the app are reachable only inside
  the server.
