"""A durable call is owned by the current coordinator and is still in flight."""


class WaitingForCall(Exception):
    """Yield a graph node without marking its execution failed or resending."""
