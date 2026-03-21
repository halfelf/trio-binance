from importlib.metadata import version

__version__ = version("trio-binance")

from trio_binance.client import AsyncClient
