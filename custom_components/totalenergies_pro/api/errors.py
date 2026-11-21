"""Exceptions of the TotalEnergies client.

Messages never contain credentials, tokens, e-mail addresses, account, contract, delivery point
or invoice numbers.
"""

from __future__ import annotations


class TotalEnergiesError(Exception):
    """Base class."""


class TotalEnergiesRouteNotAllowed(TotalEnergiesError):
    """The call is not in the read-only allow-list (a bug, never a runtime condition)."""


class TotalEnergiesTemporaryError(TotalEnergiesError):
    """TotalEnergies or the network misbehaved; nothing is known to be wrong with the
    credentials. Try again at the next scheduled fetch — never in a loop."""


class TotalEnergiesBlockedError(TotalEnergiesTemporaryError):
    """DataDome (the anti-bot service in front of the API) answered 403 (CLAUDE.md rule 2).

    The client sends nothing more until `retry_after` (epoch seconds). Never try to bypass it.
    """

    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__("blocked by the TotalEnergies anti-bot protection (DataDome)")


class TotalEnergiesAuthError(TotalEnergiesError):
    """Authentication was refused or cannot be completed without the user.

    Terminal until the user acts (reauth flow): the client makes no further HTTP call.

    reason:
      - "refused": the password was refused (`POST /login` answered 200 without a token, or a
        4xx) — CLAUDE.md rule 1;
      - "refresh_expired": the refresh token was refused and no password is available;
      - "blocked": a previous refusal is pending; nothing was sent.
    """

    def __init__(self, reason: str, status: int | None = None) -> None:
        self.reason = reason
        self.status = status
        detail = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"TotalEnergies authentication failed: {reason}{detail}")
