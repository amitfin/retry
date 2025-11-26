# Retry

[![HACS Badge](https://img.shields.io/badge/HACS-Default-31A9F4.svg?style=for-the-badge)](https://github.com/hacs/integration)

[![GitHub Release](https://img.shields.io/github/release/amitfin/retry.svg?style=for-the-badge&color=blue)](https://github.com/amitfin/retry/releases)

![Download](https://img.shields.io/github/downloads/amitfin/retry/total.svg?style=for-the-badge&color=blue) ![Analytics](https://img.shields.io/badge/dynamic/json?style=for-the-badge&color=blue&label=Analytics&suffix=%20Installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$.retry.total)

![Project Maintenance](https://img.shields.io/badge/maintainer-Amit%20Finkelstein-blue.svg?style=for-the-badge)

Smart homes include a network of devices. A case of a failed command can happen due to temporary connectivity issues or invalid device states. The cost of such a failure can be high, especially for non-interactive automation. For example, failing to shutdown an irrigation system which should run for 20 minutes can have severe consequences.

The integration increases the automation reliability by implementing 2 custom actions - `retry.actions` and `retry.action`.
`retry.actions` is the recommended and UI friendly action which should be used. `retry.action` is the engine behind the scenes. It's being used by `retry.actions` but can also be used directly by advanced users.

## `retry.actions`

Here is a short demo of using `retry.actions` in the automation rule editor:

https://github.com/user-attachments/assets/69b4db6b-80c6-4527-b088-e10b68e0f18c

`retry.actions` wraps any action inside the sequence of actions with `retry.action`. `retry.action` performs the original action with a retry logic on failures. A complex sequence of actions with a nested structure and conditions is supported. `retry.actions` traverses through the steps and wraps any action step. There is no impact or changes to the rest of the steps. The detailed behavior and the list of optional parameters of `retry.action` is explained in the section below. All features and parameters available in `retry.action` are also supported by `retry.actions`, so using a YAML configuration provides no additional benefit. However, note that the parameters defined for `retry.actions` are shared across all actions in its sequence. Each inner `retry.action` inherits the same configuration. For this reason, the recommended best practice is to include only a single action inside a `retry.actions` sequence.

Note: `retry.actions` and `retry.action` are not suitable for relative state changes. For example, `homeassistant.toggle` and `fan.increase_speed` are relative actions while `light.turn_on` is an absolute action. The reason is that a relative action might change the state and only then a failure occurs. Performing it again might have an unintentional result.

## `retry.action`

This action wraps an inner action with retries on failures. It can be useful to mitigate temporary issues of connectivity or invalid device states.

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

The amount of seconds to wait between attempts. It's expressed in a special template format with square brackets `"[[ ... ]]"` instead of curly brackets `"{{ ... }}"`. This is needed to prevent from rendering the expression in advance (relevant core's code is [1](https://github.com/home-assistant/core/blob/c5453835c258a7625c93de826103b355ecdaa445/homeassistant/helpers/config_validation.py#L1463), [2](https://github.com/home-assistant/core/blob/c5453835c258a7625c93de826103b355ecdaa445/homeassistant/helpers/config_validation.py#L782), [3](https://github.com/home-assistant/core/blob/c5453835c258a7625c93de826103b355ecdaa445/homeassistant/helpers/script.py#L734), [4](https://github.com/home-assistant/core/blob/c5453835c258a7625c93de826103b355ecdaa445/homeassistant/helpers/service.py#L414)). The template can use the following variables: `entity_id`, `action`, `attempt`, and any other parameter provided to the inner action. `attempt` is a zero-based counter - it's zero after the 1st inner action failure, and increasing by one for subsequence failures.

Note that there is no delay before the initial attempt, so the `backoff` parameter is used only after the 1st failure.

The default value is `"[[ 2 ** attempt ]]"`, which implements an exponential backoff policy:
- Delay intervals between failures: `[1, 2, 4, 8, 16, 32]`. Each interval is twice the duration of the preceding one.
- Cumulative execution offsets: `[0, 1, 3, 7, 15, 31, 63]`. These values represent the absolute time offsets at which each successive inner action is performed.

Linear backoff is an alternative strategy that can be expressed as a plain (non-template) string, without brackets:
- Example: `"10"`
- Delay intervals between failures: `[10, 10, 10, 10, 10, 10]`
- Cumulative execution offsets: `[0, 10, 20, 30, 40, 50, 60]`

A slower exponential backoff can be specified by scaling the exponential factor:
- Example: `"[[ 10 * 2 ** attempt ]]"`
- Delay intervals between failures: `[10, 20, 40, 80, 160, 320]`
- Cumulative execution offsets: `[0, 10, 30, 70, 150, 310, 630]`

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

The state is also checked before the first attempt. If it's valid, the action is not performed even once. It's possible to disable the initial check in the [integration's configuration dialog](https://my.home-assistant.io/redirect/integration/?domain=retry) which ensures the action gets performed at least once.

#### `validation` parameter (optional)

A boolean expression of a special template format with square brackets `"[[ ... ]]"` instead of curly brackets `"{{ ... }}"`. This is needed to prevent from rendering the expression in advance. The template can use the following variables: `entity_id`, `action`, `attempt` (zero-based counter), and any other parameter provided to the inner action. For example:

```
action: retry.action
data:
  action: light.turn_on
  brightness: 70
  validation: "[[ state_attr(entity_id, 'brightness') == brightness ]]"
target:
  entity_id: light.kitchen
```

The boolean expression is rendered after each inner action attempt. If the value is False, the attempt is considered a failure and the loop of retries continues.

The `validation` is also checked before the first attempt. If it passes, the action is not performed even once. It's possible to disable the initial check in the [integration's configuration dialog](https://my.home-assistant.io/redirect/integration/?domain=retry) which ensures the action gets performed at least once.

Note: `validation: "[[ states(entity_id) == 'on' ]]"` has an identical logic and impact as setting `expected_state: "on"`. Therefore, `expected_state` is simpler and preferable.

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

`entity_id`, `action`, and any other parameter provided to the inner action are provided as variables and can be used by `on_error` templates.

Note that each entity is running individually when the inner action has a list of entities. In such a case `on_error` can get performed multiple times, once per each failed entity. Similarly, `retry.actions` has a sequence of actions which might include multiple actions. This can also cause `on_error` to get performed multiple times, once per each failed inner action.

#### `ignore_target` parameter (optional)

Many actions support a list of entities either by providing an explicit list in the `entity_id` parameter or by [targeting areas and devices](https://www.home-assistant.io/docs/scripts/service-calls/#targeting-areas-and-devices). It's also possible to specify a [group](https://www.home-assistant.io/integrations/group) entity. By default, there is a separate retry loop per entity to isolate failures. Group entities are expanded, recursively. However, there are cases where the inner action should get the target parameters without modifications. When this parameter is set to true, there is no try to resolve, expand and isolate the entities. The original target parameters are passed to the inner action as provided.

There are multiple implications for using this option:
1. There is no validation of entities' availability.
2. The parameter `expected_state` can't be used.
3. `entity_id` is not provided to template expressions.
4. There is a single retry loop, i.e. no failure isolation between different entities.
5. The `retry_id` is the action name, similar to actions without `entity_id`.

It's recommended to use the `validation` parameter when using this option.

#### `repair` parameter (optional)

A boolean parameter controlling whether to issue repair tickets on failure. The system default is used when the parameter is not provided. The system default can be configured via the [integration's configuration dialog](https://my.home-assistant.io/redirect/integration/?domain=retry).

#### `retry_id` parameter (optional)

An action cancels a previous running action with the same retry ID. This parameter can be used to set the retry ID explicitly but it should be rarely used, if at all. The default value of `retry_id` is the `entity_id` of the inner action. For an inner action with no `entity_id`, the default value of `retry_id` is the action name (e.g. `homeassistant.reload_all`).

An example of the cancellation scenario might be when turning off a light while the turn on retry loop of the same light is still running due to failures or light's transition time. The turn on retry loop will be getting canceled by the turn off action since both share the same `retry_id` by default (the entity ID).

Note that each entity is running individually when the inner action has a list of entities. Therefore, they have a different default `retry_id`. However, an explicit `retry_id` is shared for all entities of the same action. Nevertheless, retry loops created by the same action (`retry.action` or `retry.actions`) are not canceling each other even when they share the same `retry_id`.

It's possible to disable the cancellation logic by setting `retry_id` to an empty string (`retry_id: ""`) or null (`retry_id: null`). In such a case, the action doesn't cancel any other running action and will not be canceled by any other future action. Note that it's not possible to set `retry_id` to an empty string or null via the "UI Mode" but instead the "YAML Mode" in the UI should be used.

### Error Handling: Logging, Exceptions, and Repair

Each inner action failure is logged. On the **final** failure (when the maximum number of attempts is reached), the action issues a repair ticket and propagates the exception to the caller.  
An exception is also raised when a retry loop is canceled due to a duplicate `retry_id`.  
When used inside automations or scripts, any propagated exception will halt execution of subsequent steps unless [continuing-on-error](https://www.home-assistant.io/docs/scripts/#continuing-on-error) is enabled.

**Note:** When the inner action targets multiple entities, each entity runs in its own retry loop. `retry.action` ensures that *all* retry loops complete before propagating any exception. However, if multiple retry loops fail, there is **no guarantee** which exception will be raised.

### Response Data

On success, `retry.action` (but not `retry.actions`) returns the inner action’s [response data](https://www.home-assistant.io/docs/scripts/perform-actions/#use-templates-to-handle-response-data).

**Note:** When targeting multiple entities, each entity runs independently, and there is **no guarantee** which loop’s response is returned.  
To avoid this ambiguity, it is recommended to use the `ignore_target` option when providing a list of entities. This forces a single retry loop whose response will be used.

## Install

HACS is the preferred and easier way to install the component, and can be done by using this My button:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=amitfin&repository=retry&category=integration)

Otherwise, download `retry.zip` from the [latest release](https://github.com/amitfin/retry/releases), extract and copy the content under `custom_components` directory.

Home Assistant restart is required once the integration files are copied (either by HACS or manually).

The Retry integration should also be added to the configuration in order to use the new custom actions. This can be done via the user interface, by using this My button:

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=retry)

It's also possible to add the integration via `configuration.yaml` by adding the single line `retry:`.

## Uninstall

1. **Delete the configuration:**
   - Open the integration page ([my-link](https://my.home-assistant.io/redirect/integration/?domain=retry)), click the 3‑dot menu (⋮), and select **Delete**.
   - Delete the line `retry:` if the integration was added to the `configuration.yaml`.

2. **Remove the integration files:**
   - If the integration was installed via **HACS**, follow the [official HACS removal instructions](https://www.hacs.xyz/docs/use/repositories/dashboard/#removing-a-repository).
   - Otherwise, manually delete the integration’s folder `custom_components/retry`.

📌 A **Home Assistant core restart** is required to fully apply the removal.

## Contributions are welcome!

If you want to contribute to this please read the [Contribution guidelines](CONTRIBUTING.md)

[Link to post in Home Assistant's community forum](https://community.home-assistant.io/t/improving-automation-reliability/558627)
