"""BPS Redaction Service: the bps_redaction core over HTTP."""

from .app import create_app
from .config import Settings

__all__ = ['create_app', 'Settings']
