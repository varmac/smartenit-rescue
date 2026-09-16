"""Exceptions raised by the public Smartenit Rescue contracts."""


class RescueError(Exception):
    """Base exception for expected Smartenit Rescue failures."""


class ValidationError(RescueError):
    """Raised when untrusted data violates a closed contract."""


class UnsupportedError(RescueError):
    """Raised when evidence does not support a requested operation."""
