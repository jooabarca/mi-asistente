"""Connection errors shared by all providers."""


class BackendError(Exception):
    """Recoverable problem (network, server hiccup): the app reconnects with backoff."""


class FatalBackendError(BackendError):
    """Retrying won't help (bad key, unknown model, quota): the app stops with this message."""


class ReconnectNow(BackendError):
    """Planned reconnect (profile switch, server rotating the connection): no backoff, no warning."""

    def __init__(self, reason: str, interrupt_playback: bool = False):
        super().__init__(reason)
        self.interrupt_playback = interrupt_playback
