from stockroom.orders.api import OrderWebhook


def test_webhook_model_drops_unknown_fields() -> None:
    body = {
        "id": 1001,
        "line_items": [{"sku": "CORE-001", "quantity": 1}],
        "customer": {"email": "someone@example.com"},
    }

    parsed = OrderWebhook.model_validate(body)

    assert "customer" not in parsed.model_dump()
