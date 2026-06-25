"""Per-format extractors. Each module registers itself with `dispatch.MIME_REGISTRY`
at import time. The dispatch layer lazy-imports them on first use."""
