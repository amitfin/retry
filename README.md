# Retry

[![GitHub Release](https://img.shields.io/github/release/amitfin/retry.svg?style=for-the-badge&color=blue)](https://github.com/amitfin/retry/releases) ![Analytics](https://img.shields.io/badge/dynamic/json?style=for-the-badge&color=blue&label=Analytics&suffix=%20Installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$.retry.total)

![Project Maintenance](https://img.shields.io/badge/maintainer-Amit%20Finkelstein-blue.svg?style=for-the-badge)

The integration implements a single custom service `retry.call`. This service warps an inner service call with background retries on failure. It can be useful and mitigate intermediate issues of connectivity or invalid device state.

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

It's possible to add any other data parameters needed by the inner service call. 

The inner service call will get called again if one of the following happens:
1. The inner service call raised an exception.
2. One of the target entitles is unavailable.

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

The service implements exponential backoff mechanism. This is the delay times of the first 7 attempts: [0, 1, 2, 4, 8, 16, 32] (each delay is twice than the previous one). The following are the offsets from the initial call [0, 1, 3, 7, 15, 31, 63].

Notes:
1. This service never fails (i.e. doesn't raise exceptions), since the retries are done in the background. However, the service logs a warning when the inner function fails (on every attempt). It also logs an error when the maximum amount of retries is reached.
2. This service can be used for absolute state changes (like turning on the lights). But it has limitations by nature. For example, it shouldn't be used for sequence of actions, when the order matters.

## Install
[HACS custom repositories](https://hacs.xyz/docs/faq/custom_repositories/) is the preferred and easier way to install the component.

Otherwise, download `retry.zip` from the [latest release](https://github.com/amitfin/retry/releases), extract and copy the content under `custom_components`.

Home Assistant restart is required once the integration is installed, and then the following line should be added to `configuration.yaml` (which requires another restart):
```
retry:
```

## Contributions are welcome!

If you want to contribute to this please read the [Contribution guidelines](CONTRIBUTING.md)
