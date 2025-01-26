# Retry

[![HACS Badge](https://img.shields.io/badge/HACS-Default-31A9F4.svg?style=for-the-badge)](https://github.com/hacs/integration)

[![GitHub Release](https://img.shields.io/github/release/amitfin/retry.svg?style=for-the-badge&color=blue)](https://github.com/amitfin/retry/releases)

![Download](https://img.shields.io/github/downloads/amitfin/retry/total.svg?style=for-the-badge&color=blue) ![Analytics](https://img.shields.io/badge/dynamic/json?style=for-the-badge&color=blue&label=Analytics&suffix=%20Installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$.retry.total)

![Project Maintenance](https://img.shields.io/badge/maintainer-Amit%20Finkelstein-blue.svg?style=for-the-badge)

Smart homes include a network of devices. A case of a failed command can happen due to temporary connectivity issues or invalid device states. The cost of such a failure can be high, especially for background automation. For example, failing to shutdown an irrigation system which should run for 20 minutes can have severe consequences.

The integration increases the automation reliability by implementing 2 custom actions - `retry.actions` and `retry.action`.
`retry.actions` is the recommended and UI friendly action which should be used. `retry.action` is the engine behind the scenes. It's being used by `retry.actions` but can also be used directly by advanced users.

## `retry.actions`

Here is a short demo of using `retry.actions` in the automation rule editor:

https://github.com/user-attachments/assets/69b4db6b-80c6-4527-b088-e10b68e0f18c

`retry.actions` wraps any action inside the sequence of actions with `retry.action`. `retry.action` performs the original action with a background retry logic on failures. A complex sequence of actions with a nested structure and conditions is supported. `retry.actions` traverses through the actions and wraps any action step. There is no impact or changes to the rest of the steps. The detailed behavior and the list of optional parameters of `retry.action` is explained in the section below. All features and parameters of `retry.action` are also supported by `retry.actions`, so there is no reason to use a YAML configuration. A straightforward UI usage as demonstrated above should be the way to go.

Note: `retry.actions` and `retry.action` are not suitable for the following scenarios:

1. For a relative state change: for example, `homeassistant.toggle` and `fan.increase_speed` are relative actions while `light.turn_on` is an absolute action. The reason is that a relative action might change the state and only then a failure occurs. Performing it again might have an unintentional result.
2. If any [action response data](https://www.home-assistant.io/docs/scripts/service-calls/#use-templates-to-handle-response-data) is needed: the actions are running in the background and therefore it's not possible to propagate responses.
3. The order of performing the actions successfully is not guaranteed since retries are running in the background. It's possible however to use a synchronization statement which can be placed between 2 retry actions. For example:
```
wait_for_trigger:
 - trigger: state
    entity_id: domain.state_1
    to: "on"
timeout: 60
continue_on_timeout: false
```

## `retry.action`

(`retry.call` is the previous and obsolete name but can still be used.)

This action wraps an inner action with background retries on failures. It can be useful to mitigate temporary issues of connectivity or invalid device states.

For example, instead of:

```
action: homeassistant.turn_on
target:
  entity_id: light.kitchen
```

The following should be used:

```
action: retry.action
data:
  action: homeassistant.turn_on
target:
  entity_id: light.kitchen
```

It's possible to add additional parameters to the `data` section. The extra parameters will be passed to the inner action.

The inner action will get performed again if one of the following happens:

1. The inner action raises an exception.
2. The target entity is unavailable. Note that this is important since HA silently skips unavailable entities ([here](https://github.com/home-assistant/core/blob/580b20b0a83c561986e7571b83df4a4bcb158392/homeassistant/helpers/service.py#L763)).

Here is the list of parameters to control the behavior of `retry.action ` (directly or via `retry.actions`). These parameters are not passed to the inner action and are consumed only by the `retry.action` itself.

#### `action` parameter (mandatory)

The `action` parameter is the only mandatory parameter. It contains the name of the inner action. It supports templates.

(`service` is the previous and obsolete parameter name but can still be used. `action` and `service` are mutually exclusive. One of them must be provided.)

#### `retries` parameter (optional)

Controls the amount of retries. The default value is 7. For example:

```
action: retry.action
data:
  action: homeassistant.turn_on
  retries: 10
target:
  entity_id: light.kitchen
```

#### `backoff` parameter (optional)

The amount of seconds to wait between attempts. It's expressed in a special template format with square brackets `"[[ ... ]]"` instead of curly brackets `"{{ ... }}"`. This is needed to prevent from rendering the expression in advance (relevant core's code is [1](https://github.com/home-assistant/core/blob/c5453835c258a7625c93de826103b355ecdaa445/homeassistant/helpers/config_validation.py#L1463), [2](https://github.com/home-assistant/core/blob/c5453835c258a7625c93de826103b355ecdaa445/homeassistant/helpers/config_validation.py#L782), [3](https://github.com/home-assistant/core/blob/c5453835c258a7625c93de826103b355ecdaa445/homeassistant/helpers/script.py#L734), [4](https://github.com/home-assistant/core/blob/c5453835c258a7625c93de826103b355ecdaa445/homeassistant/helpers/service.py#L414)). `attempt` is provided as a variable, holding a zero-based counter - it's zero for 1st time the expression is evaluated, and increasing by one for subsequence evaluations. Note that there is no delay for the initial attempt, so the list of delays always starts with a zero.

The default value is `"[[ 2 ** attempt ]]"` which is an exponential backoff. These are the delay times of the first 7 attempts: [0, 1, 2, 4, 8, 16, 32] (each delay is twice than the previous one). The following are the attempt offsets from the beginning: [0, 1, 3, 7, 15, 31, 63].

Linear backoff is a different strategy which can be expressed as a simple non-template string (without brackets). For example, these are the delay times of the first 7 attempts when using `"10"`: [0, 10, 10, 10, 10, 10, 10]. The following are the attempt offsets from the beginning: [0, 10, 20, 30, 40, 50, 60].

Another example is `"[[ 10 * 2 ** attempt ]]"` which is a slower exponential backoff. These are the delay times of the first 7 attempts: [0, 10, 20, 40, 80, 160, 320]. The following are the attempt offsets from the beginning: [0, 10, 30, 70, 150, 310, 630].

```
action: retry.action
data:
  action: homeassistant.turn_on
  backoff: "[[ 10 * 2 ** attempt ]]"
target:
  entity_id: light.kitchen
```

#### `expected_state` parameter (optional)

Validation of the entity's state after the inner action. For example:

```
action: retry.action
data:
  action: homeassistant.turn_on
  expected_state: "on"
target:
  entity_id: light.kitchen
```

If the new state is different than expected, the attempt is considered a failure and the loop of retries continues. The `expected_state` parameter can be a list and it supports templates.

#### `validation` parameter (optional)

A boolean expression of a special template format with square brackets `"[[ ... ]]"` instead of curly brackets `"{{ ... }}"`. This is needed to prevent from rendering the expression in advance. `entity_id` is provided as a variable. For example:

```
action: retry.action
data:
  action: light.turn_on
  brightness: 70
  validation: "[[ state_attr(entity_id, 'brightness') == 70 ]]"
target:
  entity_id: light.kitchen
```

The boolean expression is rendered after each inner action attempt. If the value is False, the attempt is considered a failure and the loop of retries continues.

Note: `validation: "[[ states(entity_id) == 'on' ]]"` has an identical logic and impact as setting `expected_state: "on"`. Therefore, `expected_state` is preferable from simplicity reasons.

#### `state_delay` parameter (optional)

Controls the amount of seconds to wait before the initial check of `expected_state` and `validation` (has no impact if both are absent). The default value is zero (no delay). This option can be used if the new state is being updated immediately but later on getting reverted to the previous state since the operation failed on the remote device. It's not the common behavior as integrations should update the state only when getting a new state from the device. Here is a configuration example:

```
action: retry.action
data:
  action: light.turn_off
  state_delay: 2
  expected_state: "off"
target:
  entity_id: light.kitchen
```

#### `state_grace` parameter (optional)

Controls the amount of seconds to wait before the final check of `expected_state` and `validation` (has no impact if both are absent). The default value is 0.2 seconds. The 2nd (final) check is done if the initial check fails. The action attempt is considered a failure only if the 2nd check fails. Here is a configuration example:

```
action: retry.action
data:
  action: light.turn_off
  transition: 5
  state_grace: 5.1
  expected_state: "off"
target:
  entity_id: light.kitchen
```

#### `on_error` parameter (optional)

A sequence of actions to perform if all retries fail.

Here is an automation rule example with a self remediation logic:

```
alias: Kitchen Evening Lights
mode: parallel
trigger:
  - platform: sun
    event: sunset
action:
  - action: retry.action
    data:
      action: light.turn_on
      entity_id: light.kitchen_light
      retries: 2
      on_error:
        - action: homeassistant.reload_config_entry
          data:
            entry_id: "{{ config_entry_id(entity_id) }}"
        - delay:
            seconds: 20
        - action: automation.trigger
          target:
            entity_id: automation.kitchen_evening_lights
```

(This example can be configured in UI mode by using `retry.actions`. YAML is not needed.)

`entity_id` is provided as a variable and can be used by `on_error` templates.

Note that each entity is running individually when the inner action has a list of entities. In such a case `on_error` can get performed multiple times, once per each failed entity. Similarly, `retry.actions` has a sequence of actions which might include multiple actions. This can also cause `on_error` to get performed multiple times, once per each failed inner action.

#### `repair` parameter (optional)

A boolean parameter controlling whether to issue repair tickets on failure. The system default is used when the parameter is not provided. The system default can be configured via the [integration's configuration dialog](https://my.home-assistant.io/redirect/integration/?domain=retry).

#### `retry_id` parameter (optional)

An action cancels a previous running action with the same retry ID. This parameter can be used to set the retry ID explicitly but it should be rarely used, if at all. The default value of `retry_id` is the `entity_id` of the inner action. For an inner action with no `entity_id`, the default value of `retry_id` is the action name (e.g. `homeassistant.reload_all`).

An example of the cancellation scenario might be when turning off a light while the turn on retry loop of the same light is still running due to failures or light's transition time. The turn on retry loop will be getting canceled by the turn off action since both share the same `retry_id` by default (the entity ID).

Note that each entity is running individually when the inner action has a list of entities. Therefore, they have a different default `retry_id`. However, an explicit `retry_id` is shared for all entities of the same action. Nevertheless, retry loops created by the same action (`retry.action ` or `retry.actions`) are not canceling each other even when they share the same `retry_id`.

It's possible to disable the cancellation logic by setting `retry_id` to an empty string (`retry_id: ""`) or null (`retry_id: null`). In such a case, the action doesn't cancel any other running action and will not be canceled by any other future action. Note that it's not possible to set `retry_id` to an empty string or null via the "UI Mode" but instead the "YAML Mode" in the UI should be used.

### Notes

1. The action does not propagate inner action failures (exceptions) since the attempts are done in the background. However, the action logs a warning when the inner function fails (on every attempt). It also logs an error and issue a repair ticket when the maximum amount of attempts is reached.
2. Action supports a list of entities either by providing an explicit list or by [targeting areas and devices](https://www.home-assistant.io/docs/scripts/service-calls/#targeting-areas-and-devices). It's also possible to specify a [group](https://www.home-assistant.io/integrations/group) entity. The inner action is performed individually per entity to isolate failures. Group entities are expanded (recursively.)

## Install

HACS is the preferred and easier way to install the component, and can be done by using this My button:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=amitfin&repository=retry&category=integration)

Otherwise, download `retry.zip` from the [latest release](https://github.com/amitfin/retry/releases), extract and copy the content under `custom_components` directory.

Home Assistant restart is required once the integration files are copied (either by HACS or manually).

The Retry integration should also be added to the configuration in order to use the new custom actions. This can be done via the user interface, by using this My button:

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=retry)

It's also possible to add the integration via `configuration.yaml` by adding the single line `retry:`.

## Contributions are welcome!

If you want to contribute to this please read the [Contribution guidelines](CONTRIBUTING.md)

[Link to post in Home Assistant's community forum](https://community.home-assistant.io/t/improving-automation-reliability/558627)
