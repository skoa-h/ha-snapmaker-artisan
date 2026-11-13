"""HTTP endpoints used by the drag-and-drop upload panel."""

from __future__ import annotations

from http import HTTPStatus
from ipaddress import ip_address

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.network import is_cloud_connection
from homeassistant.util.network import is_local

from .const import DOMAIN
from .services import async_upload_to_entry

MAX_UPLOAD_BYTES = 512 * 1024 * 1024


def _is_local_request(hass: HomeAssistant, request: web.Request) -> bool:
    """Return True if the request comes from the local network (not via Nabu Casa)."""
    if is_cloud_connection(hass):
        return False
    try:
        return is_local(ip_address(request.remote or ""))
    except ValueError:
        return False


def _json_error(message: str, status: HTTPStatus) -> web.Response:
    return web.json_response({"error": message}, status=status)


class SnapmakerPrintersView(HomeAssistantView):
    """List the configured printers."""

    url = "/api/snapmaker_artisan/printers"
    name = "api:snapmaker_artisan:printers"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        return web.json_response(
            [
                {
                    "entry_id": entry.entry_id,
                    "title": entry.title,
                    "connected": hasattr(entry, "runtime_data")
                    and entry.runtime_data.client.connected,
                }
                for entry in hass.config_entries.async_entries(DOMAIN)
            ]
        )


class SnapmakerUploadView(HomeAssistantView):
    """Receive a G-code file as multipart form data and send it to the printer."""

    url = "/api/snapmaker_artisan/upload"
    name = "api:snapmaker_artisan:upload"

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        user = request.get("hass_user")
        if user is None:
            return _json_error("Authentication required", HTTPStatus.UNAUTHORIZED)
        if not _is_local_request(hass, request):
            return _json_error(
                "Uploads are only allowed from the local network",
                HTTPStatus.FORBIDDEN,
            )

        entry_id: str | None = None
        filename: str | None = None
        data = bytearray()
        reader = await request.multipart()
        while part := await reader.next():
            if part.name == "entry_id":
                entry_id = (await part.text()).strip()
            elif part.name == "file":
                filename = part.filename
                while chunk := await part.read_chunk(1024 * 1024):
                    data += chunk
                    if len(data) > MAX_UPLOAD_BYTES:
                        return _json_error("File too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

        entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
        if entry is None or entry.domain != DOMAIN:
            return _json_error("Unknown printer", HTTPStatus.BAD_REQUEST)
        if not filename:
            return _json_error("No file received", HTTPStatus.BAD_REQUEST)

        try:
            await async_upload_to_entry(entry, filename.replace("\\", "/").split("/")[-1], bytes(data))
        except HomeAssistantError as err:
            return _json_error(str(err), HTTPStatus.BAD_GATEWAY)
        return web.json_response({"result": "ok"})
