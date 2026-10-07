"""Published example instruments rotate, and copies of them are recognisable."""
from datetime import date

from src import published_examples
from src.observability import attribution_from_params


def test_rotation_is_weekly_and_deterministic():
    monday = date(2026, 10, 5)
    same_week = date(2026, 10, 11)
    next_week = date(2026, 10, 12)

    first = published_examples.example_symbol("vwap", when=monday)
    assert published_examples.example_symbol("vwap", when=same_week) == first
    assert published_examples.example_symbol("vwap", when=next_week) != first
    assert first in published_examples.EXAMPLE_POOLS["vwap"]
    assert published_examples.example_path("fx", when=monday).startswith("/v1/fx/")


def test_every_service_cycles_through_its_whole_pool():
    for service, pool in published_examples.EXAMPLE_POOLS.items():
        seen = {
            published_examples.example_symbol(service, when=date(2026, 1, 5 + 7 * week))
            for week in range(len(pool))
        }
        assert seen == set(pool), service


def test_tag_url_is_idempotent_and_respects_an_existing_source():
    tagged = published_examples.tag_url("https://mcp.blocksize.info/v1/vwap/BTCUSD")
    assert tagged == (
        "https://mcp.blocksize.info/v1/vwap/BTCUSD?selection_source=published_example_path"
    )
    assert published_examples.tag_url(tagged) == tagged
    preview = "https://mcp.blocksize.info/v1/vwap/BTCUSD?selection_source=package_preview&x=1"
    assert published_examples.tag_url(preview) == preview
    with_query = published_examples.tag_url("https://mcp.blocksize.info/v1/batch?reqs=vwap:BTCUSD")
    assert with_query.endswith("reqs=vwap%3ABTCUSD&selection_source=published_example_path")


def test_published_symbols_match_every_spelling_the_docs_use():
    assert published_examples.is_published_example_path("/v1/vwap/BTC-USD")
    assert published_examples.is_published_example_path("/v1/vwap/btc-usd")
    assert published_examples.is_published_example_path("/v1/bidask/AAPLXUSD")
    assert not published_examples.is_published_example_path("/v1/vwap/PYTHUSD")
    assert not published_examples.is_published_example_path("/v1/briefs/market")
    assert published_examples.service_and_symbol("/v1/metal/XAUUSD") == ("metal", "XAUUSD")
    assert published_examples.service_and_symbol("/v1/search") is None


def test_the_tag_survives_attribution_filtering():
    labels = attribution_from_params({"selection_source": "published_example_path"})
    assert labels == {"selection_source": "published_example_path"}
    for value in ("product_preview", "repeat_workflow_recipe"):
        assert attribution_from_params({"selection_source": value}) == {
            "selection_source": value
        }
