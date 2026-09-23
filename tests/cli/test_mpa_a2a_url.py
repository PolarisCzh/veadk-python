import pytest

from frontend.server.mpa_a2a import mpa_a2a_rpc_url


@pytest.mark.parametrize("suffix", ["", "/"])
def test_shared_runtime_prefix_is_restored(suffix):
    assert (
        mpa_a2a_rpc_url(
            "https://runtime.example/runtime/r-one" + suffix,
            "https://runtime.example:443/a2a/jsonrpc",
        )
        == "https://runtime.example/runtime/r-one/a2a/jsonrpc"
    )


@pytest.mark.parametrize(
    "endpoint,card",
    [
        (
            "https://runtime.example/runtime/r-one",
            "https://@runtime.example/a2a/jsonrpc",
        ),
        ("https://runtime.example", "https://runtime.example/a2a/jsonrpc"),
        ("https://runtime.example/runtime/r-one", "https://other.example/a2a/jsonrpc"),
        ("https://runtime.example/runtime/r-one", "http://runtime.example/a2a/jsonrpc"),
        (
            "https://runtime.example/runtime/r-one",
            "https://runtime.example:8443/a2a/jsonrpc",
        ),
        (
            "https://runtime.example/runtime/r-one",
            "https://runtime.example/runtime/r-one/a2a/jsonrpc",
        ),
        ("https://runtime.example/runtime/r-one", "https://runtime.example/custom"),
        (
            "https://runtime.example/runtime/r-one",
            "https://runtime.example/a2a/jsonrpc?q=1",
        ),
        (
            "https://runtime.example/runtime/r-one",
            "https://runtime.example/a2a/jsonrpc#x",
        ),
        (
            "https://runtime.example/runtime/r-one",
            "https://user@runtime.example/a2a/jsonrpc",
        ),
        ("https://runtime.example/unrelated", "https://runtime.example/a2a/jsonrpc"),
        (
            "https://runtime.example/runtime/%2e%2e",
            "https://runtime.example/a2a/jsonrpc",
        ),
        (
            "https://runtime.example/runtime/r-one",
            "https://runtime.example:bad/a2a/jsonrpc",
        ),
    ],
)
def test_other_card_addresses_are_preserved(endpoint, card):
    assert mpa_a2a_rpc_url(endpoint, card) == card
