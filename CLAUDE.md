# CLAUDE.md

Guidance for Claude Code sessions in this repository.

## What this is

`retry` is a Home Assistant custom integration (distributed via HACS, domain `retry`) that adds two actions:

- `retry.action`: the engine. It calls one inner action (`action: light.turn_on`, …) and retries it on failure with a templated backoff. Optionally it validates the result (`expected_state`, `validation`) and runs `on_error` after the final failure.
- `retry.actions`: the UI-friendly wrapper. It walks a script `sequence`, rewrites every `call_service` step into a `retry.action` call carrying the shared retry parameters, and runs the result as an ad-hoc `script.Script`.

README.md is the user-facing spec for every parameter. Keep it in sync with the code.

## Layout

| Path | Role |
|------|------|
| `custom_components/retry/__init__.py` | Almost all logic: schemas, `RetryParams` (parse/validate, target resolution), `RetryAction` (per-entity retry loop), `_wrap_actions` (retry.actions rewriting), service registration in `async_setup` |
| `custom_components/retry/config_flow.py` | Single-instance config flow + options flow (`disable_initial_check`, `disable_repair`) |
| `custom_components/retry/const.py` | Constants / parameter names |
| `custom_components/retry/diagnostics.py` | Returns `{}` |
| `services.yaml`, `strings.json`, `translations/*.json`, `icons.json` | Action UI metadata. `translations/en.json` mirrors `strings.json` with the `[%key:…%]` references expanded |
| `tests/test_init.py` | Nearly all behavior tests (~90 cases) |
| `config/configuration.yaml` | Dev HA instance config for `scripts/develop` |

## Commands

```bash
scripts/setup            # install pytest-homeassistant-custom-component (pre-releases allowed), ruff, mypy, prek; install hooks
scripts/lint             # ruff format + ruff check --fix + mypy --strict custom_components/retry
scripts/lint --no-fix    # what CI runs
pytest                   # pytest.ini adds --cov ... --cov-fail-under=100 (100% line coverage is REQUIRED)
pytest tests/test_init.py -k retry_id -o addopts=""   # quick targeted run without the coverage gate
scripts/develop          # run a dev HA on :8123 with ./config
```

The pre-commit hooks (`prek.toml`) run lint and the full pytest suite.

## Architecture notes (non-obvious)

- **Services are registered in `async_setup`**, not per entry. Each call looks up the single config entry (`get_config_entry()`) and fails with `ServiceValidationError` if the entry is missing or not loaded. When no entry exists, `async_setup` starts an import flow (the `retry:` YAML key or the UI both end up with one entry).
- **Callers render templates before the handler runs.** When `retry.action`/`retry.actions` is called from an automation or script, HA's `template_complex` / `render_complex` renders every `{{ }}` string nested anywhere in `data` *before* the service handler runs. That is why `backoff` and `validation` use the special `[[ … ]]` / `[% … %]` / `[# … #]` syntax, converted by `_fix_template_tokens`. The catch: `on_error` templates and templates inside a `retry.actions` `sequence` are rendered by the caller, with the caller's variables. `{% raw %}` defers them.
- **`retry.actions` keeps `on_error` raw.** `_script_schema_validate_only` validates it but passes the original structure on, so it isn't double-converted into `Template` objects.
- **Target resolution** (`RetryParams._entity_ids`): a string `entity_id` is first normalized, best-effort, with `cv.comp_entity_ids`. That way comma-separated/padded ids and any-case `ALL`/`NONE` resolve like their canonical forms. Strings entity services would reject (e.g. `""`) are used as-is. Only strings have non-canonical forms; list items are lowercased by the target helper. It then uses `homeassistant.helpers.target.async_extract_referenced_entity_ids` (HA already expands old-style `group.*` and `GenericGroup` entities). `_expand_group` additionally expands group-*platform* entities (light/switch/… groups) through their `entity_id` attribute. Indirect references (area/device/floor/label) are filtered to the action's domain/integration, except `homeassistant.*`. Each resolved entity gets its own `RetryAction` loop, run with `asyncio.gather`.
- **Splitting is deliberately not automatic.** Every action with a target field is split per entity unless `ignore_target` is set. Detecting "real" entity actions (e.g. `cv.is_entity_service_schema()`) was tried and rejected: plain actions registered with `hass.services.async_register` give no signal whether `entity_id` is a target (`ffmpeg.start`, `homeassistant.turn_on`) or a parameter (`logbook.log`), so any rule misclassifies some actions and makes the behavior harder to predict. The README tells users when `ignore_target` is needed.
- **Availability and state checks use `Entity` objects** from `hass.data[DATA_INSTANCES]` (`_get_entity`), not the state machine. This matches HA's own "skip unavailable entities" logic in `entity_service_call`.
- **`retry_id`** (default: entity_id, else the action name) lives in the module-global `_running_retries: {retry_id: (context.id, count)}`. A newer call with a different `Context` takes ownership. The older loop notices at its next attempt boundary and raises `IntegrationError`. Loops sharing a context (e.g. several entities of one call) share the counter.
- **Repairs** are created on the final failure. The issue id is `str(RetryAction)`, which includes the inner action data. They're non-persistent and never deleted on a later success.
- `TargetSelection` is imported with a fallback to `TargetSelectorData`. Older HA (e.g. 2025.8) only has the latter, which is deprecated in current HA and breaks in 2026.12.

## Testing conventions

- `tests/test_init.py::async_setup()` loads the integration plus template binary sensors (`binary_sensor.test` = on, `binary_sensor.test2` = off, labeled "Test Label"). It registers a mock `test_service` under the `retry`, `binary_sensor`, `template` and `homeassistant` domains (raises `RetryTestMockError` unless `raises=False`), plus `retry.test_on_error_service`. It returns the list of received `ServiceCall`s. `async_call()` calls `retry.action` (or `retry.actions` with `plural=True`) and suppresses the expected errors.
- The autouse `sleep` fixture patches `asyncio.sleep` **globally**: `custom_components.retry.asyncio` *is* the asyncio module. Backoffs therefore take no time, and `sleep.await_args_list` also records HA-internal sleeps.
- `tests/conftest.py` fails any test that logs WARNING or above, unless the message starts with an allow-listed prefix. Use `@pytest.mark.allowed_logs(["prefix", ...])` for expected warnings.
- Coverage must stay at 100% (`.coveragerc` excludes `if TYPE_CHECKING:` and `except ImportError:`).
- The tests currently rely on Python 3.14 lazy annotations (see "Compatibility").

## Compatibility

- Declared minimum is HA **2025.8** (`hacs.json`); dev/CI run the latest HA on Python 3.14 (`.ruff.toml` targets py314). CI does not test the minimum version.
- To test on the minimum version: `uv venv --python-preference only-managed --python 3.13 <dir>`, then `uv pip install "pytest-homeassistant-custom-component==0.13.272" "pycares<5"` (0.13.272 pins HA 2025.8.3). `tests/conftest.py` and `tests/test_diagnostics.py` also need `from __future__ import annotations` on Python 3.13.
- APIs that changed across versions: `Script.async_unload()` exists only from HA 2026.5 (2026.4 and older have no public way to unload an ad-hoc `Script`). `TargetSelection` is missing in 2025.8 (hence the fallback import).
- HA 2026.9 replaces `voluptuous` with the `probatio` shim at `import homeassistant`. In standalone scripts, import `homeassistant` before `voluptuous`, or schema compilation fails.

## Conventions

- ruff with `select = ["ALL"]` (see `.ruff.toml`) and `mypy --strict`. Docstrings on everything. Match the existing style of `noqa` comments for FBT and PLR rules.
- Adding or changing a parameter means updating `const.py`, the schema in `__init__.py`, `services.yaml` (both `action` and `actions`), `strings.json` **and** `translations/en.json`, `README.md`, and tests. The other translations (`he`, `pt`, `sk`) are community-maintained and partly incomplete.
- Spell-checking runs on everything (cspell). Add new words to `.cspell.json`.
- Releases: publishing a GitHub release runs `release.yml`, which writes the tag into `manifest.json` `version` (it's `1.0.0` in git) and uploads `retry.zip`.

## Working with a live Home Assistant

Sessions may have a Home Assistant MCP connector attached to the maintainer's **production** instance. That instance uses `retry.action`/`retry.actions` in dozens of automations. Harmless test calls are OK (log entries are fine), but don't change the system meaningfully:

- Use temporary helpers/scripts, pass `repair: false`, and delete everything afterwards.
- Never touch real devices, e.g. area-wide `homeassistant.turn_off`.
