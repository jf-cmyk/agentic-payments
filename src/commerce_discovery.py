"""Synthetic, model-validated examples for unpaid x402 discovery."""
from src.models import (BidAskData, FXData, MetalData, StatePriceData, VWAPData,
                        VWAP24HrData, VWAP30MinData)

# Fixed historical timestamps deliberately avoid presenting examples as live prices.
STAMP = "2020-01-01T00:00:00Z"
SAMPLES = {
    "vwap": VWAPData(pair="BTC-USD", vwap=100, currency="USD", timestamp=STAMP),
    "bidask": BidAskData(pair="BTC-USD", bid=99, ask=101, spread=2, spread_pct=2,
                          timestamp=STAMP),
    "state": StatePriceData(pair="MSOLUSD", price=100, timestamp=STAMP),
    "vwap30m": VWAP30MinData(ticker="SOL", vwap=100, quote_currency="USD", timestamp=STAMP),
    "vwap24h": VWAP24HrData(pair="BTCUSD", vwap=100, timestamp=STAMP),
    "fx": FXData(pair="EURUSD", base_currency="EUR", quote_currency="USD", mid=1.1,
                 timestamp=STAMP),
    "metal": MetalData(ticker="XAUUSD", name="Gold", price=100, timestamp=STAMP),
}


def output_contract(path):
    """Describe actual model fields; omit fabricated examples for complex products."""
    parts = path.strip("/").split("/")
    model = SAMPLES.get(parts[1]) if len(parts) == 3 and parts[0] == "v1" else None
    if model is None:
        return {"type": "json"}, {"type": "object"}
    sample = model.model_copy(deep=True)
    # Examples must describe the requested resource (including the shared equity route).
    if "pair" in type(sample).model_fields:
        sample.pair = parts[2]
    if "ticker" in type(sample).model_fields and parts[1] == "metal":
        sample.ticker = parts[2]
        sample.name = ""
    normalized = parts[2].upper().replace("-", "").replace("/", "")
    if parts[1] == "fx" and len(normalized) == 6:
        sample.base_currency, sample.quote_currency = normalized[:3], normalized[3:]
    if parts[1] == "vwap30m" and len(normalized) > 3:
        sample.ticker, sample.quote_currency = normalized[:-3], normalized[-3:]
    if parts[1] == "vwap" and len(normalized) > 3:
        sample.currency = normalized[-3:]
    example = {"status": "ok", "data": sample.model_dump(mode="json"),
               "meta": {"provider": "Blocksize Capital", "example_only": True}}
    schema = {"type": "object", "description": "Illustrative response; never live market data.",
              "properties": {"status": {"type": "string"}, "data": type(model).model_json_schema(),
                             "meta": {"type": "object"}},
              "required": ["status", "data", "meta"]}
    return {"type": "json", "example": example}, schema


def query_contract(request, app):
    """Use the same query contract as OpenAPI; do not infer from supplied input."""
    for route in app.routes:
        regex = getattr(route, "path_regex", None)
        if regex and regex.fullmatch(request.url.path):
            operation = app.openapi().get("paths", {}).get(route.path, {}).get("get", {})
            properties, required = {}, []
            for param in operation.get("parameters", []):
                if param.get("in") == "query":
                    properties[param["name"]] = param.get("schema", {})
                    if param.get("required"):
                        required.append(param["name"])
            result = {"type": "object", "properties": properties, "additionalProperties": False}
            if required:
                result["required"] = required
            return result
    return {"type": "object", "properties": {}, "additionalProperties": False}
