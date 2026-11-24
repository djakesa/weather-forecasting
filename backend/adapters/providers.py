"""Translate provider transport errors at the application boundary."""
import requests

from backend.application.ports import SourceUnavailable


def provider(callback, *args, **kwargs):
    try:
        return callback(*args, **kwargs)
    except requests.RequestException as exc:
        raise SourceUnavailable(str(exc)) from exc
