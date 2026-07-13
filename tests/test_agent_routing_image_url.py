"""图搜路由：alicdn URL 不应被误识别为订单号。"""

from app.services.agent.data_query import extract_lookup_ids
from app.services.agent.routing import resolve_deterministic_route

ALICDN_URL = (
    "https://cbu01.alicdn.com/img/ibank/O1CN017SOkUe2ARem9CRUJL_!!"
    "2219408658200-0-cib.jpg_300x300q90.jpg_.webp"
)

ALLOWED = {
    "product_image_search",
    "order_query",
    "procurement_stats",
    "newton_consult",
}


def test_alicdn_url_not_extracted_as_order_id() -> None:
    assert extract_lookup_ids(ALICDN_URL) == []


def test_alicdn_url_routes_to_image_search() -> None:
    route = resolve_deterministic_route(ALICDN_URL, None, ALLOWED)
    assert route is not None
    assert route["tool"] == "product_image_search"
    assert route["args"]["image_url"] == ALICDN_URL


def test_order_id_outside_url_still_detected() -> None:
    text = "查一下订单 2219408658200 状态"
    assert "2219408658200" in extract_lookup_ids(text)
