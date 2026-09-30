# Deploying on one machine

How to run Invoice Collection for a team, on any machine with Docker, behind HTTPS. It is the same Compose file a reviewer runs locally, with [`compose.production.yaml`](../compose.production.yaml) layered on it to add a Caddy proxy. Why one machine is enough, and where it stops, is in [ADR 0017](adr/0017-one-machine-three-roles.md) and [scaling.md](research/scaling.md).

## What has been exercised

| Part | State |
|---|---|
| Building the image, starting the app, the runner and the sample portal with Compose, every service healthy, the runner unreachable from the host, a run over the sample mail | Exercised on every pull request, in CI on a GitHub runner, without the production overlay |
| The `Caddyfile` | Validated by Caddy in CI |
| The public address, HTTPS return addresses, a real Google sign-in over HTTPS, the secure cookie, a run started from the dashboard over the public address | Exercised on the copy the reviewer sees, which is the Compose stack on the author's machine reached through a tunnel (below) |
| Everything else below: a hosted machine, DNS, the certificate, the production overlay started, snapshots, a restore, an update | Not yet exercised on a hosted machine. The commands are standard Docker and Compose, written from the Compose files, and have not been run for this document |

## The copy the reviewer sees

The copy at the public address is not on a hosted machine. Google Cloud, DigitalOcean and Oracle each refused the author's payment method, so the same Compose stack runs on the author's machine and an [ngrok](https://ngrok.com) tunnel gives it a fixed HTTPS address. Nothing in the tool knows the difference: the app takes its public address from `INVOICE_COLLECTOR_PUBLIC_URL` and derives the sign-in return addresses and the secure cookie from it, so the tunnel plays the part Caddy plays in the production overlay.

To do the same:

1. Start the stack as for a local try, with `INVOICE_COLLECTOR_PUBLIC_URL` set to the tunnel's `https://` address.
2. Install ngrok, sign it in (`ngrok config add-authtoken ...`), and claim a fixed domain in its dashboard.
3. Put the tunnel in a config file of its own, so the auth token stays in ngrok's:

   ```yaml
   version: "3"
   endpoints:
     - name: invoice
       url: https://<your domain>.ngrok-free.app
       upstream:
         url: http://localhost:8000
   ```

   and start it with `ngrok start --config <ngrok's config> --config <this file> invoice`.
4. Add `https://<your domain>/auth/callback` and `https://<your domain>/accounts/callback` to the OAuth client.
5. Keep it up: the containers restart on their own (`restart: unless-stopped`); start the tunnel from a task at logon, and stop the machine from sleeping.

What differs from a hosted machine: it is reachable while that machine is up, the address is ngrok's rather than your own, and ngrok's free plan shows a visitor an interstitial page once per browser session. For a team, the procedure below is the one to follow.

## 1. Create the machine

Any provider will do. One small Linux machine with a public address that does not change:

| | Suggested | Why |
|---|---|---|
| System | A current Ubuntu or Debian LTS | Docker supports both |
| Memory | 2 GB or more | An estimate: a run drives a headless Chromium |
| Disk | 20 GB or more | The image is about 1 GB (measured); the ledger and PDFs of a month are a few megabytes |
| Address | A static public IPv4 address | The DNS record points at it |

Turn on the provider's scheduled snapshots of the disk now (see [Backups](#8-backups)).

## 2. Point a subdomain at it

At your DNS provider, add an `A` record for a subdomain, such as `invoices.example.com`, with the machine's address (and an `AAAA` record if it has IPv6). Caddy cannot get a certificate until this resolves to the machine.

## 3. Open only ports 22, 80 and 443

In the provider's firewall, allow inbound TCP 22 (SSH), TCP 80 and TCP 443 (and UDP 443 if you want HTTP/3), and nothing else. Use the provider's firewall rather than one on the machine: ports Docker publishes are not filtered by `ufw`. Compose publishes nothing but the proxy's 80 and 443 in production; the app and the runner are reached only on the Compose network.

## 4. Install Docker

Install Docker Engine and the Compose plugin as [Docker's instructions](https://docs.docker.com/engine/install/) give for your distribution. The production overlay uses the `!reset` tag, which needs Docker Compose 2.24.4 or later: check with `docker compose version`.

## 5. Clone and write `.env`

```bash
git clone https://github.com/itsskofficial/adalatai-assignment.git
cd adalatai-assignment
cp .env.example .env
chmod 600 .env
```

Fill `.env` as the [README](../README.md#3-fill-in-env) describes, with these differences:

| Setting | In production |
|---|---|
| `INVOICE_COLLECTOR_DOMAIN` | The subdomain, such as `invoices.example.com`. Required |
| `INVOICE_COLLECTOR_PUBLIC_URL` | Leave empty: the overlay sets it to `https://` the domain |
| `INVOICE_COLLECTOR_SAMPLE_PORTAL_URL` | Leave empty. The sample portal is for demonstrations |
| `ANTHROPIC_API_KEY`, `JEV_API_KEY` | Set them, or every document is held for review |
| `INVOICE_COLLECTOR_SLACK_WEBHOOK` | Set it, so a failed run is reported |

Make new secrets for this machine; do not reuse the ones from a laptop. `.env` holds every secret, so keep it readable by its owner alone and out of any backup that leaves your control.

## 6. Add the HTTPS return addresses to the OAuth client

In the Google Cloud console, open the web application client (APIs & Services, Credentials) and add, beside the `localhost` ones:

- `https://invoices.example.com/auth/callback`
- `https://invoices.example.com/accounts/callback`

Google may also ask for the domain (`example.com`) under Authorised domains on the consent screen's branding page. Add every person who will sign in, and every mailbox, as a test user while the app is in testing. These console steps have not been checked against a real domain.

## 7. Start it

```bash
docker compose -f compose.yaml -f compose.production.yaml up -d --wait
```

Caddy asks Let's Encrypt for a certificate for the domain and renews it itself. Then open `https://invoices.example.com`, sign in as an address in `INVOICE_COLLECTOR_ALLOWLIST`, and:

1. On **Source accounts**, connect each mailbox and choose the owner account.
2. On **Vendors**, fill the expected vendor list.
3. On **Settings**, turn on the schedule and choose its day, time and time zone.
4. On **People**, add the people who may sign in.

While the OAuth app is in testing, every sign-in ends after seven days. Renew each on the Source accounts screen, which marks those ending within two days. A run that cannot read an account says so in the digest.

`GET https://invoices.example.com/health` answers `{"up": true}` without signing in; point an uptime check at it.

## 8. Backups

Everything the tool keeps is in the Docker volume `data`: the ledger (`ledger.sqlite` and its `-wal` and `-shm` files), the archive, the summaries and the stored sign-ins. The PDFs and summary sheets are also in the owner account's Drive.

**Snapshots.** Schedule the provider's snapshots of the machine's disk, daily, and keep two weeks or more. A snapshot is taken at one instant, and SQLite recovers from its write-ahead log when the file is next opened, as after a power cut.

**Restore from a snapshot.** Create a disk or a machine from the snapshot as your provider allows. If the address changed, update the DNS record. Then start it as in step 7.

**A copy of the volume.** For a copy you can move elsewhere, stop the two services, archive the volume, and start them again. Compose names the volume after the folder, so it is `adalatai-assignment_data` when cloned as above; `docker volume ls` lists it.

```bash
docker compose -f compose.yaml -f compose.production.yaml stop app runner
docker run --rm -v adalatai-assignment_data:/data:ro -v "$PWD/backups":/backup alpine \
  tar czf /backup/data.tgz -C /data .
docker compose -f compose.yaml -f compose.production.yaml start runner app
```

To restore that copy, stop the two services and replace the volume's contents, keeping the files owned by the image's user (uid 10001):

```bash
docker compose -f compose.yaml -f compose.production.yaml stop app runner
docker run --rm -v adalatai-assignment_data:/data -v "$PWD/backups":/backup alpine \
  sh -c "rm -rf /data/* && tar xzf /backup/data.tgz -C /data && chown -R 10001:10001 /data"
docker compose -f compose.yaml -f compose.production.yaml start runner app
```

The copy holds the stored sign-ins, so treat it as a secret.

## 9. Update

Take a snapshot first. Then:

```bash
git pull
docker compose -f compose.yaml -f compose.production.yaml up -d --build --wait
```

A ledger written by an earlier version is brought up to date when it is opened. A run going on when the runner restarts is shown as stopped on the Runs screen; run the month again, which only adds.

## 10. Logs

```bash
docker compose -f compose.yaml -f compose.production.yaml ps
docker compose -f compose.yaml -f compose.production.yaml logs -f runner
docker compose -f compose.yaml -f compose.production.yaml logs --since 1h app proxy
```

`ps` shows each service's health. The runner logs each run and each scheduled run it starts or skips; the app logs a missing or wrong setting, every one at once, before it refuses to start.
