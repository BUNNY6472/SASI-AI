"""Bounded, read-only evidence tools. Retrieved text is untrusted data."""

from .evidence import collect_evidence, fetch_url

__all__ = ["collect_evidence", "fetch_url"]
