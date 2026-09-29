# Prefect: what the documentation says

Researched on 2026-09-29 for ticket #16 and ADR 0003.

## How far to trust this file

- **Read through a summarising fetch tool**: the Prefect documentation pages linked below. Facts are reliable; wording may not be letter-perfect.
- **Read directly**: the installed package, `prefect` 3.8.7 in `backend/.venv` (signatures printed with `inspect.signature`). Marked "installed source".
- **Tried locally**: small scripts run against the installed package on Windows, with no Prefect server running. Marked "tried". Nothing was sent anywhere but a temporary local server.
- **Not confirmed**: marked as such.

## Version and installation

| Fact | Value | Source |
| --- | --- | --- |
| Current version | 3.8.7, released 27 September 2026. Major version 3 | <https://pypi.org/project/prefect/> |
| Python | 3.10 up to 3.14 | <https://pypi.org/project/prefect/> |
| Install | `uv add prefect` (or `pip install -U prefect`) | <https://pypi.org/project/prefect/> |

## Flows and tasks

| Fact | Value | Source |
| --- | --- | --- |
| Defining a flow | `@flow` on a function; called like a normal function | <https://docs.prefect.io/v3/develop/write-flows> |
| Defining a task | `@task` on a function | <https://docs.prefect.io/v3/develop/write-tasks> |
| Run names | `flow_run_name` / `task_run_name`: a template over the parameters (`"hello-{name}"`) or a callable returning a string | <https://docs.prefect.io/v3/develop/write-flows>, <https://docs.prefect.io/v3/develop/write-tasks> |
| Parameter validation | `validate_parameters` (default `True`) validates flow parameters with Pydantic | <https://docs.prefect.io/v3/develop/write-flows> |
| Parameters that are not JSON | A flow called directly accepts arbitrary objects with `validate_parameters=False`; it ran. A task given such an object fails to compute its cache key unless `cache_policy=NO_CACHE` is set (the error message says so) | tried |
| Retries | `retries=` number of retries; `retry_delay_seconds=` a number or a list of delays | <https://docs.prefect.io/v3/how-to-guides/workflows/retries> |
| Backoff | `from prefect.tasks import exponential_backoff`; `exponential_backoff(backoff_factor=10)` gives delays for each retry (10, 20, 40 for three retries) | <https://docs.prefect.io/v3/how-to-guides/workflows/retries>; installed source |
| Jitter | `retry_jitter_factor=` adds random time to each delay so retries do not arrive together | <https://docs.prefect.io/v3/how-to-guides/workflows/retries> |
| Retry only some failures | `retry_condition_fn(task, task_run, state) -> bool`; `False` ends the task with its exception | <https://docs.prefect.io/v3/how-to-guides/workflows/retries> |
| Retries as settings | `PREFECT_TASKS_DEFAULT_RETRIES`, `PREFECT_TASKS_DEFAULT_RETRY_DELAY_SECONDS` | <https://docs.prefect.io/v3/how-to-guides/workflows/retries> |
| Testing a task without the engine | `.fn()` calls the undecorated function | <https://docs.prefect.io/v3/how-to-guides/workflows/test-workflows> |

## Running tasks concurrently

| Fact | Value | Source |
| --- | --- | --- |
| Default task runner | `ThreadPoolTaskRunner`: each submitted task runs in a thread | <https://docs.prefect.io/v3/concepts/task-runners> |
| Submitting | `task.submit(...)` returns a `PrefectFuture`; `task.map(...)` submits one run per item; `wait_for=` orders runs | <https://docs.prefect.io/v3/concepts/task-runners> |
| Futures | `.result()` blocks and raises by default; `.result(raise_on_failure=False)` returns the exception; `.wait()`, `wait()`, `as_completed()` | <https://docs.prefect.io/v3/concepts/task-runners>; tried |
| Unresolved futures | Must be resolved before the flow ends | <https://docs.prefect.io/v3/concepts/task-runners> |
| `ThreadPoolTaskRunner(max_workers=None)` | `max_workers` caps how many tasks run at once; default from `PREFECT_TASKS_RUNNER_THREAD_POOL_MAX_WORKERS` | <https://docs.prefect.io/v3/concepts/task-runners>, <https://docs.prefect.io/v3/api-ref/settings-ref.md>; installed source |
| A runner opened inside a flow | `with ThreadPoolTaskRunner(max_workers=2) as runner: runner.submit(task, parameters={...})` inside a flow ran eight tasks, never more than two at once, all linked to the flow run | tried |
| Deadlock warning | A bounded pool can deadlock when a task waits on tasks it submitted | <https://docs.prefect.io/v3/concepts/task-runners> |

## Limiting concurrency

| Mechanism | Needs a Prefect server? | Source |
| --- | --- | --- |
| `max_workers` on `ThreadPoolTaskRunner` | No: it is a thread pool in the process | <https://docs.prefect.io/v3/concepts/task-runners>; tried |
| Global concurrency limits: `from prefect.concurrency.sync import concurrency`; created with `prefect gcl create NAME --limit N` | Yes: the limit lives on the server. A limit that does not exist logs a warning and does not block, unless `strict=True` | <https://docs.prefect.io/v3/how-to-guides/workflows/global-concurrency-limits> |
| Rate limits: `rate_limit(...)` on a global concurrency limit with `--slot-decay-per-second` | Yes, as above | <https://docs.prefect.io/v3/how-to-guides/workflows/global-concurrency-limits> |
| Task run concurrency limits by tag: `prefect concurrency-limit create TAG N` | Yes: "set on the Prefect server, not the Prefect client". Since 3.4.19 backed by global limits named `tag:{tag}` | <https://docs.prefect.io/v3/how-to-guides/workflows/tag-based-concurrency-limits> |

So only `max_workers` works when the tool runs with no server. The ephemeral server (below) would hold global limits too, but it is a new temporary server each time, so a limit would have to be created on every run. Not tried.

## Running with no server

| Fact | Value | Source |
| --- | --- | --- |
| Ephemeral mode | `server.ephemeral.enabled`, env `PREFECT_SERVER_EPHEMERAL_ENABLED` (alias `PREFECT_SERVER_ALLOW_EPHEMERAL_MODE`): "whether or not a subprocess server can be started when no API URL is provided". Default `False` in the settings reference; the default profile after installation enables it | <https://docs.prefect.io/v3/api-ref/settings-ref.md>, <https://docs.prefect.io/v3/concepts/settings-and-profiles.md> |
| API URL | `PREFECT_API_URL`, default unset; "if not set, the client will attempt to infer it" | <https://docs.prefect.io/v3/api-ref/settings-ref.md> |
| Startup timeout | `PREFECT_SERVER_EPHEMERAL_STARTUP_TIMEOUT_SECONDS`, default 20 | <https://docs.prefect.io/v3/api-ref/settings-ref.md> |
| Where state is kept | a SQLite database under `PREFECT_HOME` (default `~/.prefect`) | <https://docs.prefect.io/v3/concepts/settings-and-profiles.md> |
| What happens | With no API URL and ephemeral mode on, calling a flow starts a temporary server on a random local port, runs, and stops it ("Starting temporary server on http://127.0.0.1:8484") | tried |
| A profile that sets an API URL | On this machine the active profile sets `PREFECT_API_URL=http://127.0.0.1:4200/api`. Then a flow needs that server. Setting `PREFECT_API_URL=""` in the environment starts the temporary server but its events service fails ("Cannot put items in a stopped service instance"). `temporary_settings(updates={PREFECT_SERVER_EPHEMERAL_ENABLED: True}, restore_defaults={PREFECT_API_URL})` from `prefect.settings` runs cleanly | tried |
| Cost | The temporary server takes about 7 seconds to start on this machine | tried |

## Scheduling

| Fact | Value | Source |
| --- | --- | --- |
| `flow.serve(...)` | Creates a deployment and starts a long-running process that "monitors for work from the Prefect server"; each run executes in its own subprocess. Parameters include `name`, `cron`, `interval`, `schedule`, `schedules`, `parameters`, `tags`, `description`, `pause_on_shutdown` | <https://docs.prefect.io/v3/how-to-guides/deployment_infra/run-flows-in-local-processes.md>; installed source |
| Serving needs a server | Yes: the served process must keep running and talks to a Prefect server | <https://docs.prefect.io/v3/how-to-guides/deployment_infra/run-flows-in-local-processes.md> |
| Cron with a time zone | `from prefect.schedules import Cron` (Prefect 3.1.16 and later); `Cron("0 6 3 * *", timezone="Asia/Kolkata")`, passed as `schedule=` | <https://docs.prefect.io/v3/how-to-guides/deployments/create-schedules.md>; installed source |
| Daylight saving | Not documented on the page read. Not relevant for Asia/Kolkata, which has none. **Not confirmed** | <https://docs.prefect.io/v3/how-to-guides/deployments/create-schedules.md> |
| Scheduled time inside a run | `from prefect.runtime import flow_run`; `flow_run.scheduled_start_time`, `flow_run.parameters`, `flow_run.name`. Missing values come back empty. A flow called directly gets the time it was called | <https://docs.prefect.io/v3/concepts/runtime-context.md>; tried |

## Artifacts

| Fact | Value | Source |
| --- | --- | --- |
| Markdown artifact | `from prefect.artifacts import create_markdown_artifact`; `create_markdown_artifact(markdown, key=None, description=None)` | <https://docs.prefect.io/v3/how-to-guides/workflows/artifacts.md>; installed source |
| Key | Gives the artifact a history across runs; without a key it shows only on its run. Allowed characters are not stated on the page; lowercase letters, digits and dashes worked | <https://docs.prefect.io/v3/how-to-guides/workflows/artifacts.md>; tried |

## Testing

| Fact | Value | Source |
| --- | --- | --- |
| Test harness | `from prefect.testing.utilities import prefect_test_harness`, a context manager: a temporary SQLite database and a temporary API server on a local port | <https://docs.prefect.io/v3/how-to-guides/workflows/test-workflows> |
| Scope | Session scope is recommended, as a clean database per test is rarely needed | <https://docs.prefect.io/v3/how-to-guides/workflows/test-workflows> |
| Startup timeout | `prefect_test_harness(server_startup_timeout=30)` by default | <https://docs.prefect.io/v3/how-to-guides/workflows/test-workflows>; installed source |
| Run logger outside a run | `from prefect.logging import disable_run_logger` | <https://docs.prefect.io/v3/how-to-guides/workflows/test-workflows> |

## Other libraries the flow runs alongside

| Fact | Value | Source |
| --- | --- | --- |
| SQLite connections across threads | `check_same_thread=False` lets other threads use a connection; "write operations may need to be serialized by the user". `sqlite3.threadsafety` is 3 (serialized) here, SQLite 3.50.4 | <https://docs.python.org/3/library/sqlite3.html>; tried |
| Playwright's sync API | "Playwright's API is not thread-safe ... create a playwright instance per thread". Calling a browser started in one thread from another fails with `greenlet.error: Cannot switch to a different thread`: a lock is not enough, the calls must be made on the thread that started it | <https://playwright.dev/python/docs/library>; tried |
