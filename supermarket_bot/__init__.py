"""Read-only market-data bot for the Super Market (Susquehanna Predictions Cup) API."""

__version__ = "0.1.0"

from .client import SuperMarketClient  # noqa: E402
from .config import Settings  # noqa: E402
from .errors import ApiError, NetworkError, SuperMarketError  # noqa: E402

__all__ = ["ApiError", "NetworkError", "Settings", "SuperMarketClient", "SuperMarketError", "__version__"]
