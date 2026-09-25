"""Error types for HTTP fetching."""


class PageGone(RuntimeError):
    """El aviso ya no está (404/410): reintentarlo no sirve, hay que darlo de baja."""
