"""nilscript.sdk — optional Python SDK for the Network Intent Layer (NIL).

The SDK is the only southbound door: it speaks NIL sentences to a conformant
backend. It is imported only when nilscript is installed with the ``[sdk]`` extra
(``pip install nilscript[sdk]``), which pulls in ``httpx`` and ``pydantic``.
"""

from nilscript.sdk.bootstrap import client_for_grant, client_from_env
from nilscript.sdk.client import NilClient
from nilscript.sdk.connect import handshake
from nilscript.sdk.grants import GrantRef, scope_allows
from nilscript.sdk.refusals import RefusalCode
from nilscript.sdk.transport import NilTransport

# Documented public alias (uppercase NIL).
NILClient = NilClient

__all__ = [
    "NilClient",
    "NILClient",
    "NilTransport",
    "GrantRef",
    "scope_allows",
    "RefusalCode",
    "client_from_env",
    "client_for_grant",
    "handshake",
]
