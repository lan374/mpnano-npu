"""Jetson image transmission module; public lifecycle API for pipeline integration."""
from .api import Sender, SenderConfig

__all__ = ['Sender', 'SenderConfig']
__version__ = '0.4.0'
