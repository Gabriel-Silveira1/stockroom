from stockroom.shared.settings import ServiceSettings


class OrdersSettings(ServiceSettings):
    # Warehouse allocation is not modelled yet: lines without one ship from here.
    default_warehouse_id: str = "eu-west"
