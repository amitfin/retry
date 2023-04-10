# Retry

[![HACS Badge](https://img.shields.io/badge/HACS-Default-31A9F4.svg?style=for-the-badge)](https://github.com/hacs/integration)

[![GitHub Release](https://img.shields.io/github/release/amitfin/retry.svg?style=for-the-badge&color=blue)](https://github.com/amitfin/retry/releases) 

![Download](https://img.shields.io/github/downloads/amitfin/retry/total.svg?style=for-the-badge&color=blue) ![Analytics](https://img.shields.io/badge/dynamic/json?style=for-the-badge&color=blue&label=Analytics&suffix=%20Installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$.retry.total)

![Project Maintenance](https://img.shields.io/badge/maintainer-Amit%20Finkelstein-blue.svg?style=for-the-badge)

The integration implements a single custom service `retry.call`. This service warps an inner service call with background retries on failure. It can be useful to mitigate temporary issues of connectivity or invalid device states.

For example, instead of:
```
service: homeassistant.turn_on
target:
  entity_id: light.kitchen
```
The following should be used:
```
service: retry.call
data:
  service: homeassistant.turn_on
target:
  entity_id: light.kitchen
```

The `service` parameter (inside the `data` section) supports templates. It's possible to add any other data parameters needed by the inner service call.

The inner service call will get called again if one of the following happens:
1. The inner service call raised an exception.
2. One of the target entities is unavailable. Note that this is important since HA silently skips unavailable entities ([here](https://github.com/home-assistant/core/blob/580b20b0a83c561986e7571b83df4a4bcb158392/homeassistant/helpers/service.py#L763)).

By default there are 7 retries. It can be changed by passing the optional parameter `retries`:
```
service: retry.call
data:
  service: homeassistant.turn_on
  retries: 10
target:
  entity_id: light.kitchen
```
The `retries` parameter is not passed to the inner service call.

The service implements exponential backoff mechanism. These are the delay times of the first 7 attempts: [0, 1, 2, 4, 8, 16, 32] (each delay is twice than the previous one). The following are the offsets from the initial call [0, 1, 3, 7, 15, 31, 63].

`expected_state` is another _optional_ parameter which can be used to validate the new state of the entities after the inner service call:
```
service: retry.call
data:
  service: homeassistant.turn_on
  expected_state: "on"
target:
  entity_id: light.kitchen
```
If the new state is different than expected, the attempt is considered a failure and the loop of retries continues.  The `expected_state` parameter supports templates, and is not passed to the inner service call.

Notes:
1. The service does not propagate inner service failures (exceptions) since the retries are done in the background. However, the service logs a warning when the inner function fails (on every attempt). It also logs an error when the maximum amount of retries is reached.
2. This service can be used for absolute state changes (like turning on the lights). But it has limitations by nature. For example, it shouldn't be used for sequence of actions, when the order matters.

## Install
HACS is the preferred and easier way to install the component, and can be done by using this My button:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=amitfin&repository=retry&category=integration)

Otherwise, download `retry.zip` from the [latest release](https://github.com/amitfin/retry/releases), extract and copy the content under `custom_components` directory.

Home Assistant restart is required once the integration files were copied (either by HACS or manually).

Adding Retry integration to your Home Assistant instance can be done via the user interface, by using this My button:

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=retry)

It's also possible to add the integration via `configuration.yaml` by adding the single line `retry:`.

## Contributions are welcome!

If you want to contribute to this please read the [Contribution guidelines](CONTRIBUTING.md)
