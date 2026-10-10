"""Constants for the Snapmaker Artisan integration."""

DOMAIN = "snapmaker_artisan"

CONF_MODEL = "model"
CONF_SERIAL = "serial"

# Option: keep a connection to the printer. Turned off by the user to free
# the printer for Luban, which can't connect while Home Assistant is.
CONF_CONNECTION_ENABLED = "connection_enabled"

# Shown on the printer's touchscreen when Home Assistant asks to connect.
CLIENT_NAME = "Home Assistant"

# How long to wait for the user to accept the connection on the touchscreen.
AUTH_TIMEOUT = 120

# Delays between reconnect attempts (seconds).
RECONNECT_DELAY = 30
RECONNECT_DELAY_AUTH = 300
