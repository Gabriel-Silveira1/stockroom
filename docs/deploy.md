# Deploying to a VPS

The production setup is the development stack with one override: only the Caddy gateway
is reachable, on ports 80 and 443. It gets a TLS certificate automatically, and it asks
for a password on every request that changes state. Reading stays open: the dashboard,
stock, orders and API docs.

## Requirements

- A Linux VPS with 2 GB of RAM (seven containers; RabbitMQ and Postgres are the heaviest).
- Docker Engine with the Compose plugin v2.24 or newer (for `!reset` in overrides).
- A domain or subdomain with an `A` record pointing at the server.
- Ports 80 and 443 open. Port 80 is needed for the certificate challenge.

## First deploy

```sh
git clone https://github.com/Gabriel-Silveira1/stockroom.git
cd stockroom
cp .env.example .env
```

Edit `.env`:

```sh
# the password reviewers use to place orders from the dashboard
docker run --rm caddy:2-alpine caddy hash-password --plaintext 'choose-a-password'
```

Put the output in `DEMO_PASSWORD_HASH`, inside single quotes. Then set `SITE_ADDRESS`
and the two service passwords, and start:

```sh
docker compose -f compose.yaml -f compose.prod.yaml up -d --build
docker compose -f compose.yaml -f compose.prod.yaml ps
```

The seed job loads the catalog once and exits. Open `https://<SITE_ADDRESS>`.

## Checks

```sh
curl -s https://<SITE_ADDRESS>/api/inventory/stock          # 200, open
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  https://<SITE_ADDRESS>/api/orders/webhooks/orders           # 401 without the password
uv run python scripts/load_test.py --base-url https://<SITE_ADDRESS> \
  --user reviewer --password 'choose-a-password'              # from any machine
```

## Updating

```sh
git pull
docker compose -f compose.yaml -f compose.prod.yaml up -d --build
```

Migrations run at service start, under an advisory lock.

## Resetting the demo data

The ledger is append-only by design, so a clean slate means a fresh database:

```sh
docker compose -f compose.yaml -f compose.prod.yaml down
docker volume rm stockroom_pgdata stockroom_rabbitmq
docker compose -f compose.yaml -f compose.prod.yaml up -d
```

Keep the `caddy-data` volume: it holds the TLS certificate, and certificate authorities
rate-limit reissuing.
