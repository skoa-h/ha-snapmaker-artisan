"""Config flow for the Snapmaker Artisan integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST

from .const import AUTH_TIMEOUT, CLIENT_NAME, CONF_MODEL, CONF_SERIAL, DOMAIN
from .sacp import (
    DiscoveredPrinter,
    SacpAuthError,
    SacpConnectionError,
    SnapmakerClient,
    discover,
)

_LOGGER = logging.getLogger(__name__)

# Seconds async_setup_entry has to claim the flow's connection.
HANDOVER_TIMEOUT = 60


class SnapmakerArtisanConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the printer address, then wait for touchscreen confirmation."""

    VERSION = 1

    def __init__(self) -> None:
        self._host: str | None = None
        self._discovered: dict[str, DiscoveredPrinter] | None = None
        self._client: SnapmakerClient | None = None
        self._connect_task: asyncio.Task | None = None
        self._error: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = user_input[CONF_HOST].strip()
            await self.async_set_unique_id(self._host)
            self._abort_if_unique_id_configured()
            return await self.async_step_authorize()

        if self._discovered is None:
            self._discovered = {p.host: p for p in await discover()}
        if self._error:
            errors["base"] = self._error
            self._error = None

        default_host = self._host or next(iter(self._discovered), "")
        found = ", ".join(f"{p.model or p.name} ({p.host})" for p in self._discovered.values())
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_HOST, default=default_host): str}),
            errors=errors,
            description_placeholders={"found": found or "-"},
        )

    async def async_step_authorize(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Connect; the user has to tap "Yes" on the printer's touchscreen."""
        assert self._host
        if self._connect_task is None:
            self._client = SnapmakerClient(self._host, client_name=CLIENT_NAME)
            self._connect_task = self.hass.async_create_task(
                self._client.connect(auth_timeout=AUTH_TIMEOUT)
            )
        if not self._connect_task.done():
            return self.async_show_progress(
                step_id="authorize",
                progress_action="confirm_on_touchscreen",
                progress_task=self._connect_task,
                description_placeholders={"host": self._host},
            )

        task, self._connect_task = self._connect_task, None
        try:
            task.result()
        except SacpAuthError:
            self._error = "not_confirmed"
        except SacpConnectionError:
            self._error = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error connecting to %s", self._host)
            self._error = "unknown"
        if self._error:
            return self.async_show_progress_done(next_step_id="user")
        return self.async_show_progress_done(next_step_id="finish")

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._host and self._client
        # Hand the authorised connection over to async_setup_entry.
        handover = self.hass.data.setdefault(DOMAIN, {})
        client, host = self._client, self._host
        self._client = None

        def _drop_unclaimed() -> None:
            if host in handover and handover[host][0] is client:
                del handover[host]
                self.hass.async_create_task(client.disconnect())

        # async_setup_entry claims it and cancels the timer.
        handover[host] = (client, self.hass.loop.call_later(HANDOVER_TIMEOUT, _drop_unclaimed))
        printer = (self._discovered or {}).get(self._host)
        model = printer.model if printer and printer.model else "Snapmaker Artisan"
        return self.async_create_entry(
            title=model,
            data={
                CONF_HOST: self._host,
                CONF_MODEL: model,
                CONF_SERIAL: printer.name if printer else None,
            },
        )

    def async_remove(self) -> None:
        """Flow aborted: don't leave a dangling connection behind."""
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        if self._client:
            self.hass.async_create_task(self._client.disconnect())
