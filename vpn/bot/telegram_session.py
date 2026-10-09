"""Keep bot API traffic off the primary server's unreachable IPv6 route."""
import socket

from aiogram.client.session.aiohttp import AiohttpSession


class TelegramSession(AiohttpSession):
    def __init__(self):
        super().__init__()
        # aiogram's connector options retain its verified TLS context and limits.
        # Scope this to Telegram; never change the host's network configuration.
        self._connector_init["family"] = socket.AF_INET
        self._connector_init["ttl_dns_cache"] = 60
