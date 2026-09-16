import jsonschema
from starlette.requests import Request

from src.commerce_discovery import SAMPLES, output_contract
from src.resource_server import _x402_bazaar_extension


def request(path, method="GET"):
    return Request({"type": "http", "method": method, "path": path, "headers": [],
                    "query_string": b"", "scheme": "https", "server": ("example.org", 443)})


def test_models_generate_valid_synthetic_discovery():
    for family in SAMPLES:
        path = "/v1/" + family + "/BTCUSD"
        info, schema = output_contract(path)
        jsonschema.validate(info["example"], schema)
        assert info["example"]["meta"]["example_only"] is True
        assert info["example"]["data"]["timestamp"].startswith("2020-01-01")
        bazaar = _x402_bazaar_extension(request(path))
        jsonschema.validate(bazaar["info"], bazaar["schema"])


def test_query_contract_includes_real_optional_inputs():
    bazaar = _x402_bazaar_extension(request("/v1/vwap30m/SOLUSD"))
    query = bazaar["schema"]["properties"]["input"]["properties"]["queryParams"]
    assert query["properties"]["include_trades"]["type"] == "boolean"
    batch = _x402_bazaar_extension(request("/v1/batch"))
    assert batch["info"]["input"]["queryParams"]["reqs"]
    assert "reqs" in batch["schema"]["properties"]["input"]["properties"]["queryParams"]["required"]


def test_complex_product_does_not_advertise_empty_fabricated_response():
    info, _ = output_contract("/v1/briefs/market")
    assert info == {"type": "json"}
