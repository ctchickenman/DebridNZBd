"""Generate SABnzbd-compatible nzo_id strings."""

import secrets


def generate_nzo_id() -> str:
    """Generate a unique nzo_id in SABnzbd format: SABnzbd_nzo_<10 hex chars>."""
    return f"SABnzbd_nzo_{secrets.token_hex(5)}"


def generate_nzf_id() -> str:
    """Generate a unique nzf_id for files within a job."""
    return f"SABnzbd_nzf_{secrets.token_hex(5)}"