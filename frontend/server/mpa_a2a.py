"""Compatibility for MPA agent cards behind shared Runtime gateway paths."""

import re
from urllib.parse import urlsplit, urlunsplit


def mpa_a2a_rpc_url(endpoint: str, advertised_url: str) -> str:
    """Restore only the known default RPC path on a same-origin Runtime gateway."""
    try:
        base = urlsplit(endpoint)
        card = urlsplit(advertised_url)
        if (
            base.scheme not in {"http", "https"}
            or base.scheme != card.scheme
            or not base.hostname
            or base.hostname != card.hostname
            or (base.port or (443 if base.scheme == "https" else 80))
            != (card.port or (443 if card.scheme == "https" else 80))
            or any(
                (
                    base.username is not None,
                    card.username is not None,
                    base.query,
                    card.query,
                    base.fragment,
                    card.fragment,
                )
            )
            or not re.fullmatch(r"/runtime/[a-z0-9-]+", base.path.rstrip("/"))
            or card.path != "/a2a/jsonrpc"
        ):
            return advertised_url
        return urlunsplit(
            (base.scheme, base.netloc, base.path.rstrip("/") + card.path, "", "")
        )
    except ValueError:
        return advertised_url
