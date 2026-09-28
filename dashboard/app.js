"use strict";

const API = {
  inventory: "/api/inventory",
  orders: "/api/orders",
  fulfillment: "/api/fulfillment",
  carrier: "/api/carrier",
};
const REFRESH_MS = 1500;
const STATUSES = ["pending", "reserved", "picked", "shipped", "cancelled"];

const state = { selected: null, lastWebhook: null, skus: [] };
const $ = (id) => document.getElementById(id);

async function api(method, url, body, headers = {}) {
  const response = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json", ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok && response.status !== 409 && response.status !== 422) {
    throw new Error(`${method} ${url}: ${response.status}`);
  }
  return { status: response.status, payload };
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  node.append(...children.filter((child) => child !== null && child !== undefined));
  return node;
}

function clock(iso) {
  return new Date(iso).toLocaleTimeString([], { hour12: false });
}

function describeLines(lines) {
  return lines.map((line) => `${line.quantity}× ${line.sku}`).join(", ");
}

/* Stock */

function renderStock(levels) {
  const byWarehouse = new Map();
  for (const level of levels) {
    if (!byWarehouse.has(level.warehouse_id)) byWarehouse.set(level.warehouse_id, []);
    byWarehouse.get(level.warehouse_id).push(level);
  }
  const blocks = [...byWarehouse].map(([warehouse, rows]) =>
    el("div", { className: "warehouse" },
      el("h3", { textContent: warehouse }),
      ...rows.map(stockRow)));
  $("stock").replaceChildren(...(blocks.length ? blocks : [el("p", { className: "empty", textContent: "No stock yet." })]));
}

function stockRow(level) {
  const total = Math.max(level.on_hand, 1);
  const numbers = el("span", { className: "numbers" });
  numbers.append(
    el("b", { textContent: level.on_hand }), " on hand · ",
    el("b", { textContent: level.reserved }), " reserved · ",
    el("b", { textContent: level.available }), " available");
  const bar = el("div", { className: "bar", title: `${level.reserved} reserved, ${level.available} available` },
    el("span", { className: "reserved", style: `width:${(100 * level.reserved) / total}%` }),
    el("span", { className: "available", style: `width:${(100 * level.available) / total}%` }));
  return el("div", { className: "stock-row" },
    el("div", { className: "stock-top" },
      el("div", {},
        el("div", { className: "sku", textContent: level.sku }),
        el("div", { className: "sku-name", textContent: level.name })),
      numbers),
    bar);
}

function fillSkuPickers(levels) {
  const keys = levels.map((level) => `${level.sku}|${level.warehouse_id}`);
  if (keys.join() === state.skus.join()) return;
  state.skus = keys;
  for (const id of ["order-sku", "burst-sku"]) {
    const select = $(id);
    const previous = select.value;
    select.replaceChildren(...levels.map((level) =>
      el("option", {
        value: `${level.sku}|${level.warehouse_id}`,
        textContent: `${level.sku} · ${level.warehouse_id} — ${level.name}`,
      })));
    if (keys.includes(previous)) select.value = previous;
    else if (id === "burst-sku" && keys.includes("LTD-001|eu-west")) select.value = "LTD-001|eu-west";
  }
}

/* Orders */

function renderOrders(orders) {
  const template = $("order-item");
  $("orders").replaceChildren(...orders.map((order) => {
    const item = template.content.cloneNode(true);
    const button = item.querySelector(".order");
    button.classList.toggle("selected", order.id === state.selected);
    button.addEventListener("click", () => select(order.id));
    const pill = item.querySelector(".pill");
    pill.textContent = order.status;
    pill.classList.add(order.status);
    item.querySelector(".order-ref").textContent = `#${order.external_id}`;
    item.querySelector(".order-lines").textContent =
      describeLines(order.lines) + (order.cancel_reason ? ` · ${order.cancel_reason}` : "");
    const time = item.querySelector(".order-time");
    time.dateTime = order.created_at;
    time.textContent = clock(order.created_at);
    return item;
  }));
  const counts = Object.fromEntries(STATUSES.map((status) => [status, 0]));
  for (const order of orders) counts[order.status] += 1;
  $("order-counts").replaceChildren(...STATUSES.filter((status) => counts[status])
    .map((status) => el("span", { className: `pill ${status}`, textContent: `${counts[status]} ${status}` })));
}

function select(orderId) {
  state.selected = orderId;
  history.replaceState(null, "", `#order=${orderId}`);
  document.querySelectorAll(".order.selected").forEach((node) => node.classList.remove("selected"));
  refreshDetail();
}

/* Timeline */

async function refreshDetail() {
  if (!state.selected) return;
  const [order, shipment] = await Promise.all([
    api("GET", `${API.orders}/orders/${state.selected}`),
    api("GET", `${API.fulfillment}/shipments/${state.selected}`).catch(() => ({ status: 404 })),
  ]);
  renderDetail(order.payload, shipment.status === 200 ? shipment.payload : null);
}

function renderDetail(order, shipment) {
  const start = new Date(order.history[0]?.at ?? order.created_at);
  const facts = el("dl", {},
    el("dt", { textContent: "Order" }), el("dd", { textContent: `#${order.external_id}` }),
    el("dt", { textContent: "Status" }), el("dd", {}, el("span", { className: `pill ${order.status}`, textContent: order.status })),
    el("dt", { textContent: "Items" }), el("dd", { textContent: describeLines(order.lines) }),
    order.reservation_id ? el("dt", { textContent: "Reservation" }) : null,
    order.reservation_id ? el("dd", { textContent: order.reservation_id }) : null,
    order.tracking_number ? el("dt", { textContent: "Tracking" }) : null,
    order.tracking_number ? el("dd", { textContent: order.tracking_number }) : null,
    order.cancel_reason ? el("dt", { textContent: "Cancelled" }) : null,
    order.cancel_reason ? el("dd", { className: "error", textContent: order.cancel_reason }) : null);

  const timeline = el("ol", { className: "timeline" }, ...order.history.map((entry) => {
    const offset = ((new Date(entry.at) - start) / 1000).toFixed(2);
    const item = el("li", {},
      el("div", {}, el("span", { className: `pill ${entry.status}`, textContent: entry.status }),
        " ", el("span", { className: "when", textContent: `${clock(entry.at)} · +${offset}s` })),
      el("div", { className: "cause", textContent: `caused by ${entry.cause}${entry.reason ? ` (${entry.reason})` : ""}` }));
    item.style.setProperty("--dot", `var(--${entry.status})`);
    return item;
  }));

  const parts = [facts, el("h3", { textContent: "History" }), timeline];
  if (shipment) {
    parts.push(el("div", { className: "shipment" },
      el("h3", { textContent: "Shipment" }),
      el("dl", {},
        el("dt", { textContent: "Status" }), el("dd", {}, el("span", { className: `pill ${shipment.status}`, textContent: shipment.status })),
        el("dt", { textContent: "Carrier attempts" }), el("dd", { textContent: shipment.attempts }),
        shipment.status === "picked" ? el("dt", { textContent: "Next attempt" }) : null,
        shipment.status === "picked" ? el("dd", { textContent: clock(shipment.next_attempt_at) }) : null,
        shipment.last_error ? el("dt", { textContent: "Last error" }) : null,
        shipment.last_error ? el("dd", { className: "error", textContent: shipment.last_error }) : null)));
  }
  $("detail").replaceChildren(...parts);
}

/* Refresh loop */

async function refresh() {
  try {
    const [stock, orders] = await Promise.all([
      api("GET", `${API.inventory}/stock`),
      api("GET", `${API.orders}/orders?limit=40`),
    ]);
    renderStock(stock.payload);
    fillSkuPickers(stock.payload);
    renderOrders(orders.payload);
    await refreshDetail();
    $("live").classList.remove("stale");
  } catch (error) {
    $("live").classList.add("stale");
    console.error(error);
  }
}

/* Simulate */

function status(text) {
  $("sim-status").textContent = text;
}

function webhook(sku, warehouse, quantity) {
  const id = `${Date.now()}${Math.floor(Math.random() * 1000)}`;
  return {
    key: `dash-${id}`,
    body: { id, line_items: [{ sku, warehouse_id: warehouse, quantity }] },
  };
}

async function send(hook) {
  return api("POST", `${API.orders}/webhooks/orders`, hook.body, { "Idempotency-Key": hook.key });
}

$("order-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const [sku, warehouse] = $("order-sku").value.split("|");
  const hook = webhook(sku, warehouse, Number($("order-qty").value));
  const { payload } = await send(hook);
  state.lastWebhook = hook;
  $("replay").disabled = false;
  select(payload.id);
  status(`Order #${payload.external_id} placed.`);
  refresh();
});

$("replay").addEventListener("click", async () => {
  const results = [];
  for (let i = 0; i < 3; i += 1) results.push(await send(state.lastWebhook));
  const ids = new Set(results.map((result) => result.payload.id));
  status(`Same webhook sent 3 more times → ${ids.size} order (${results.map((r) => r.status).join(", ")}).`);
  refresh();
});

$("burst").addEventListener("click", async () => {
  const [sku, warehouse] = $("burst-sku").value.split("|");
  const count = Number($("burst-count").value);
  $("burst").disabled = true;
  status(`Sending ${count} concurrent orders for ${sku}…`);
  const started = performance.now();
  await Promise.all(Array.from({ length: count }, () => send(webhook(sku, warehouse, 1))));
  status(`${count} orders accepted in ${((performance.now() - started) / 1000).toFixed(1)} s. Watch them settle.`);
  $("burst").disabled = false;
  refresh();
});

$("rate").addEventListener("input", () => {
  $("rate-out").textContent = `${$("rate").value}%`;
});

$("carrier-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const current = await api("GET", `${API.carrier}/admin/behaviour`);
  const rate = Number($("rate").value) / 100;
  await api("PUT", `${API.carrier}/admin/behaviour`, { ...current.payload, failure_rate: rate });
  status(`Carrier now fails ${Math.round(rate * 100)}% of bookings.`);
});

async function loadCarrier() {
  const { payload } = await api("GET", `${API.carrier}/admin/behaviour`).catch(() => ({ payload: null }));
  if (!payload) return;
  $("rate").value = Math.round(payload.failure_rate * 100);
  $("rate-out").textContent = `${$("rate").value}%`;
}

const linked = new URLSearchParams(location.hash.slice(1)).get("order");
if (linked) state.selected = linked;

loadCarrier();
refresh();
setInterval(refresh, REFRESH_MS);
