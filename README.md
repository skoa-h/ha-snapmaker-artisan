# Snapmaker Artisan for Home Assistant

A custom integration that shows the print progress and status of a
**Snapmaker Artisan** 3D printer in Home Assistant. It works locally over your
network, without the cloud.

The Artisan doesn't offer the HTTP API (port 8080) of the Snapmaker 2.0
A-series. This integration uses **SACP** instead, a binary protocol on TCP port
8888 that Snapmaker Luban also uses. The printer pushes updates, so there is no
polling. The J1 uses the same protocol and will probably work too.

## Entities

| Entity | Description |
| --- | --- |
| Print progress | % complete (current G-code line / total lines) |
| Current line | G-code line being printed (also when the total is unknown) |
| Status | idle, printing, paused, completed, … |
| Print file | File name of the current job |
| Print time elapsed / remaining | Durations; remaining is estimated the way Luban estimates it |
| Print finish time | Estimated wall-clock time the print finishes |
| Nozzle (2) temperature / target | Both nozzles of the dual-extrusion head |
| Bed temperature / target | Heated bed |
| Connection (switch) | Turn off to disconnect Home Assistant from the printer, on to reconnect |

While the printer is off, the entities show as *unavailable*. The integration
reconnects on its own when the printer comes back.

## Using Luban at the same time

The printer probably accepts only one SACP connection at a time. To use Luban, turn off the
**Connection** switch (under the device's *Configuration* section): Home
Assistant disconnects and stops reconnecting, and Luban can connect. Turn the
switch back on when you're done. The setting survives Home Assistant restarts.
You may have to confirm on the touchscreen again when reconnecting.

You can also automate it, e.g. with `switch.turn_off` on
`switch.snapmaker_artisan_connection`.

## Installation

**HACS:** add this repository as a *custom repository* (type: Integration),
install **Snapmaker Artisan** and restart Home Assistant.

**Manual:** copy `custom_components/snapmaker_artisan` into the
`custom_components` folder of your Home Assistant config directory and restart.

## Setup

1. Turn on the printer and check it's on Wi-Fi. You can find its IP address
   on the touchscreen under *Settings → About*.
2. In Home Assistant go to *Settings → Devices & services → Add integration*
   and choose **Snapmaker Artisan**. Printers found on your network are
   suggested.
3. Submit the form, go to the printer and tap **Yes** on the connection
   request from "Home Assistant". You have 2 minutes.

> **Tip:** give the printer a fixed IP address (DHCP reservation in your
> router). Otherwise the integration loses track of it when the address changes.

## Dashboard example

```yaml
type: entities
title: Snapmaker Artisan
entities:
  - entity: sensor.snapmaker_artisan_status
  - entity: sensor.snapmaker_artisan_print_progress
  - entity: sensor.snapmaker_artisan_print_file
  - entity: sensor.snapmaker_artisan_print_time_remaining
  - entity: sensor.snapmaker_artisan_print_finish_time
  - entity: sensor.snapmaker_artisan_nozzle_temperature
  - entity: sensor.snapmaker_artisan_bed_temperature
```

Push notification when the print is done:

```yaml
automation:
  - alias: Snapmaker print finished
    triggers:
      - trigger: state
        entity_id: sensor.snapmaker_artisan_status
        to: completed
    actions:
      - action: notify.mobile_app_your_phone
        data:
          message: "Print {{ states('sensor.snapmaker_artisan_print_file') }} finished"
```

## Known limitations

- **The touchscreen may ask again.** Some firmware versions remember a
  confirmed client and others ask after every power cycle. If a reconnect isn't
  confirmed, the integration retries every 5 minutes and logs a warning.
- **Luban and Home Assistant together:** probably only one can be connected at a time;
  see [Using Luban at the same time](#using-luban-at-the-same-time).
- **Progress depends on the touchscreen.** The percentage needs the total
  line count of the job, which only the touchscreen knows. For prints started
  on the touchscreen it often doesn't answer that request; then progress and
  remaining time are *unknown* (a warning is logged once), while status, file
  name, elapsed time and temperatures still work.
- **Read-only:** no pause, stop or temperature controls.

## Development

```bash
pip install -r requirements_test.txt
pytest
```

The tests run the integration against a simulated printer
(`tests/fake_printer.py`). The packet encoder is also checked against test
vectors from Snapmaker's official `@snapmaker/snapmaker-sacp-sdk`.

## Credits

The protocol details come from [Snapmaker Luban](https://github.com/Snapmaker/Luban),
the [SACP SDK](https://www.npmjs.com/package/@snapmaker/snapmaker-sacp-sdk),
[snapmaker_moonraker](https://github.com/goeland86/snapmaker_moonraker) and
[sm2uploader](https://github.com/macdylan/sm2uploader).
