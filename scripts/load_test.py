"""Fire many concurrent orders for one unit each at an item with a fixed number of units.

Checks, against the running stack, that exactly as many orders get stock as there are
units, that availability never goes negative, and that every order settles. Exits 1 if
any check fails.

    uv run python scripts/load_test.py                     # 200 orders, 50 units
    uv run python scripts/load_test.py --orders 500 --units 20
"""

import argparse
import asyncio
import sys
import time
import uuid
from collections import Counter
from typing import Any

import httpx

FINAL = {"shipped", "cancelled"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8100")
    parser.add_argument("--orders", type=int, default=200)
    parser.add_argument("--units", type=int, default=50)
    parser.add_argument("--sku", default="LTD-001")
    parser.add_argument("--warehouse", default="eu-west")
    parser.add_argument("--user", help="basic auth user, when writes are protected")
    parser.add_argument("--password", help="basic auth password")
    parser.add_argument("--timeout", type=float, default=120, help="seconds to wait to settle")
    return parser.parse_args()


async def available(client: httpx.AsyncClient, sku: str, warehouse: str) -> tuple[int, int, int]:
    response = await client.get("/api/inventory/stock", params={"warehouse_id": warehouse})
    response.raise_for_status()
    for level in response.json():
        if level["sku"] == sku:
            return level["on_hand"], level["reserved"], level["available"]
    raise SystemExit(f"{sku} is not stocked in {warehouse}")


async def top_up(client: httpx.AsyncClient, args: argparse.Namespace) -> None:
    """Make exactly `units` available. The ledger only grows, so extra units can't be
    removed here; in that case pick another item or a fresh stack."""
    _, reserved, free = await available(client, args.sku, args.warehouse)
    if reserved:
        raise SystemExit(f"{args.sku} has {reserved} units on hold; wait for them to settle")
    if free > args.units:
        raise SystemExit(f"{args.sku} already has {free} units available, more than {args.units}")
    if free < args.units:
        response = await client.post(
            "/api/inventory/receipts",
            json={
                "sku": args.sku,
                "warehouse_id": args.warehouse,
                "quantity": args.units - free,
                "reference": "load-test",
            },
        )
        response.raise_for_status()


async def place(client: httpx.AsyncClient, args: argparse.Namespace, run: str, n: int) -> str:
    body = {
        "id": f"load-{run}-{n}",
        "line_items": [{"sku": args.sku, "warehouse_id": args.warehouse, "quantity": 1}],
    }
    response = await client.post(
        "/api/orders/webhooks/orders", json=body, headers={"Idempotency-Key": f"{run}-{n}"}
    )
    response.raise_for_status()
    order_id: str = response.json()["id"]
    return order_id


async def order(client: httpx.AsyncClient, order_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/orders/orders/{order_id}")
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return body


async def settle(
    client: httpx.AsyncClient, args: argparse.Namespace, order_ids: list[str]
) -> tuple[list[dict[str, Any]], int]:
    """Poll until every order is shipped or cancelled; track the lowest availability seen."""
    lowest = args.units
    deadline = time.monotonic() + args.timeout
    while True:
        lowest = min(lowest, (await available(client, args.sku, args.warehouse))[2])
        orders = await asyncio.gather(*(order(client, order_id) for order_id in order_ids))
        pending = sum(o["status"] not in FINAL for o in orders)
        print(f"\r  settling: {pending:4d} orders still moving", end="", flush=True)
        if not pending or time.monotonic() > deadline:
            print()
            return orders, lowest
        await asyncio.sleep(1)


def check(name: str, ok: bool, detail: str) -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    return ok


async def main() -> int:
    args = parse_args()
    auth = httpx.BasicAuth(args.user, args.password) if args.user and args.password else None
    limits = httpx.Limits(max_connections=args.orders, max_keepalive_connections=50)
    async with httpx.AsyncClient(
        base_url=args.base_url, auth=auth, limits=limits, timeout=30
    ) as client:
        await top_up(client, args)
        before = await available(client, args.sku, args.warehouse)
        print(f"{args.orders} concurrent orders for 1 x {args.sku} ({args.units} units available)")

        run = uuid.uuid4().hex[:8]
        started = time.monotonic()
        order_ids = await asyncio.gather(*(place(client, args, run, n) for n in range(args.orders)))
        print(f"  {len(order_ids)} webhooks accepted in {time.monotonic() - started:.2f} s")

        orders, lowest = await settle(client, args, order_ids)
        after = await available(client, args.sku, args.warehouse)

    statuses = Counter(o["status"] for o in orders)
    reasons = Counter(o["cancel_reason"] for o in orders if o["status"] == "cancelled")
    got_stock = sum(o["reservation_id"] is not None for o in orders)
    shipped = statuses["shipped"]
    print(f"  statuses: {dict(statuses)}  cancel reasons: {dict(reasons)}")
    print(f"  stock (on hand, reserved, available): before {before}, after {after}")

    expected = min(args.units, args.orders)
    results = [
        check("orders that got stock", got_stock == expected, f"{got_stock}, expected {expected}"),
        check("lowest availability seen", lowest >= 0, str(lowest)),
        check("every order settled", len(order_ids) == sum(statuses[s] for s in FINAL), "yes"),
        check(
            "stock left the ledger once per shipment",
            after[0] == before[0] - shipped and after[1] == 0,
            f"on hand {before[0]} -> {after[0]} for {shipped} shipped",
        ),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
