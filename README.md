# Prism

**An OpenAI-compatible AI gateway.** Prism puts one endpoint in front of many LLM providers. It handles routing, failover, caching, governance and observability, so your applications don't have to.

> [!WARNING]
> Prism is in early development (pre-alpha, `0.1.0`). The core gateway works: chat completions with streaming, nine providers, retries, fallback chains, circuit breakers and virtual keys. Budgets, rate limits, caching and metrics are not built yet, and APIs and configuration may change without notice. Planned work is tracked on the [project board](https://github.com/users/EduardoSaverin/projects/3), with one [milestone](https://github.com/EduardoSaverin/Prism/milestones) per phase.

## Why Prism

Teams that use several LLM providers often end up with provider SDKs scattered through their code, no shared view of spend, and features that go down whenever one provider does. Prism moves keys, routing, limits and monitoring into one gateway, so applications call a single stable endpoint and providers can change behind it.

- **One API.** Use the OpenAI SDK you already have. Change the base URL, and switch models by changing a string.
- **Resilient by default.** Retries with exponential backoff and jitter, ordered fallback chains, and a circuit breaker for each target.
- **Governed access.** Virtual keys per team, limited to the routes they may call, with instant revocation. Provider keys can be reserved for particular routes.
- **Extensible.** Provider adapters, plugins and routing policies are small, well-defined contracts. New behaviour never touches the core.
- **Zero-config start.** SQLite and an in-memory key cache by default. PostgreSQL and Redis are optional, for production.

## What works today

| Area | Capabilities |
| --- | --- |
| API | `/v1/chat/completions` with SSE streaming and tool calls, and `/v1/models`, in the OpenAI format |
| Providers | OpenAI, Anthropic, Gemini, Groq, Mistral, Cerebras, NVIDIA NIM, Cloudflare Workers AI, Ollama, and any OpenAI-compatible server (vLLM, llama.cpp, LM Studio) |
| Routing | Priority, weighted, round-robin and latency policies. Retries with backoff and jitter, fallback chains, a circuit breaker per target. Weighted key pools, optionally limited per target. |
| Governance | Teams and hashed virtual keys with allowed routes, expiry and revocation, managed with the CLI. Optional Redis cache shared by instances. |
| Storage | SQLite or PostgreSQL, with Alembic migrations |

The full product specification is in the [product requirements document](docs/PRD.md).

## Quickstart

**Requirements:** [uv](https://docs.astral.sh/uv/). uv installs Python 3.13 for you if it is not already present.

```bash
git clone https://github.com/EduardoSaverin/Prism.git prism
cd prism
uv sync
```

Copy the environment file and add your provider keys:

```bash
cp .env.example .env
```

Copy the example configuration and replace each `<model-id>` with a model your providers offer:

```bash
cp config/prism.example.yaml config/prism.yaml
```

Set `PRISM_KEY_PEPPER` in `.env` to a long random secret. Prism uses it to hash virtual keys, so keep it safe: if it changes, every key stops working.

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Validate the config, create the database, and create a team and a virtual key. The key is printed once, so copy it:

```bash
uv run --env-file .env prism check-config config/prism.yaml --require-env
uv run --env-file .env prism db-upgrade
uv run --env-file .env prism teams create my-team
uv run --env-file .env prism keys create --team my-team --name my-app
```

Start the server. `make run` loads `.env` for you:

```bash
make run CONFIG=config/prism.yaml
```

Send a request with your key:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer pk-prism-..." \
  -H "Content-Type: application/json" \
  -d '{"model": "fast-chat", "messages": [{"role": "user", "content": "Hello"}]}'
```

For a quick local try without keys, `prism serve --no-auth` accepts every request. It only starts on a loopback address such as `127.0.0.1`.

### Usage

Point any OpenAI client at Prism and use a route name as the model:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="pk-prism-...")  # your virtual key

reply = client.chat.completions.create(
    model="fast-chat",  # a Prism route, not a provider model
    messages=[{"role": "user", "content": "Hello"}],
)

for chunk in client.chat.completions.create(
    model="fast-chat",
    messages=[{"role": "user", "content": "Count to five"}],
    stream=True,
):
    print(chunk.choices[0].delta.content or "", end="")
```

Every response carries `x-prism-request-id`, `x-prism-provider` and `x-prism-model`, so you can see which provider answered. Errors use the OpenAI error shape, and rate limits include a `Retry-After` header.

## Configuration

Prism reads providers and routes from a YAML file. Provider keys never appear in the file. Each key entry names an environment variable that holds the secret.

```yaml
providers:
  - id: groq
    kind: groq
    keys:
      - env: GROQ_KEY_1
        weight: 2
      - env: GROQ_KEY_2
        weight: 1

routes:
  - name: fast-chat        # clients send this as the model name
    policy: priority
    targets:               # tried in order on failure
      - provider: groq
        model: <model-id>
      - provider: gemini
        model: <model-id>
    retry:
      max_retries: 2
      backoff_ms: 200
    cache:
      exact: true
      ttl_s: 300
```

See [`config/prism.example.yaml`](config/prism.example.yaml) for a complete example.

### Providers

| `kind` | Default base URL | Notes |
| --- | --- | --- |
| `openai` | `https://api.openai.com/v1` | Sends `max_completion_tokens` |
| `anthropic` | `https://api.anthropic.com` | Native Messages API, version `2023-06-01` |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta` | Native `generateContent` API |
| `groq` | `https://api.groq.com/openai/v1` | |
| `mistral` | `https://api.mistral.ai/v1` | `seed` is sent as `random_seed` |
| `cerebras` | `https://api.cerebras.ai/v1` | |
| `nvidia_nim` | `https://integrate.api.nvidia.com/v1` | |
| `cloudflare` | none: set `base_url` | `https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/v1` |
| `ollama` | `http://localhost:11434/v1` | No key needed |
| `openai_compatible` | none: set `base_url` | vLLM, llama.cpp, LM Studio and similar. Key optional. |

Every provider also accepts `base_url` (to override the default) and `timeout_s` (default 60). `github_models` is rejected: GitHub retired GitHub Models on 2026-07-30.

### Routing and resilience

| Setting | Where | Default | Meaning |
| --- | --- | --- | --- |
| `policy` | route | `priority` | `priority` (in order), `weighted` (by target `weight`), `round_robin` or `latency` (fastest first). `cost` and `quota` are not available yet. |
| `keys` | target | all of the provider's keys | Env names of the provider keys this target may use, for example `[GROQ_KEY_1]`. Each must be one of the provider's `keys`. |
| `retry.max_retries` | route | `2` | Extra tries on the same target for rate limits and upstream errors |
| `retry.backoff_ms` | route | `200` | Base delay. Grows exponentially to at most 2 s, with jitter. A provider's `Retry-After` is honoured up to 5 s; longer waits move to the next target. |
| `fallback_on_content_blocked` | route | `false` | Try the next target when a provider blocks the content |
| `circuit_breaker.failure_threshold` | top level | `5` | Failures in a row before a target is skipped |
| `circuit_breaker.cooldown_s` | top level | `30` | Time before a skipped target gets one probe request |

A key that fails authentication is disabled. A rate-limited key rests for its `Retry-After`, and another key in the pool is used. Key health is shared: a target limited to some keys sees the same disabled and resting keys as every other target of that provider. When none of a target's keys is usable, the next target is tried. Streams fall back only before the first chunk; after that, a failure ends the stream with an error event. The route `cache` settings are accepted but have no effect yet.

### Virtual keys

Every `/v1` request needs a virtual key, sent as `Authorization: Bearer pk-prism-...`. Each key belongs to a team and can be limited to some routes. Prism stores only an HMAC of each key, made with `PRISM_KEY_PEPPER`, and shows the key once, when it is created.

`--routes` names are checked against the config file (`--config`, or `PRISM_CONFIG`), so a mistyped route is refused instead of creating a key that can never be used. Without a config file, the key is created with a warning that the routes were not checked.

| Situation | Response |
| --- | --- |
| No key, an unknown key, or a revoked or expired key | 401 `invalid_api_key` |
| A route the key is not allowed to use | 403 `permission_denied` |
| `GET /v1/models` | Lists only the routes the key may use |

Revoking a key takes effect within about 2 seconds: every instance checks the database for changed keys every 2 seconds. With `PRISM_REDIS_URL` set, Redis is a cache shared by all instances, and `prism keys revoke` removes the key from it, so it stops working at once. See [ADR 0008](docs/adr/0008-virtual-keys.md).

### Environment variables

| Variable | Description | Default |
| --- | --- | --- |
| `PRISM_CONFIG` | Path to the YAML config file | none |
| `PRISM_DATABASE_URL` | SQLAlchemy database URL. For PostgreSQL, install the `postgres` extra and use `postgresql+asyncpg://...` | `sqlite+aiosqlite:///./prism.db` |
| `PRISM_KEY_PEPPER` | Secret used to hash virtual keys, at least 32 characters. Required unless the server runs with `--no-auth`. | none |
| `PRISM_REDIS_URL` | Redis URL, for running more than one instance. Install the `redis` extra (`pip install 'prismgw[redis]'`). | none (in-memory) |
| Provider keys | Any variable that the config file names, for example `GROQ_KEY_1` | none |

### CLI

| Command | Description |
| --- | --- |
| `prism serve [--config PATH] [--host HOST] [--port PORT] [--no-auth]` | Start the gateway (default `127.0.0.1:8000`). `--no-auth` accepts requests without a key, on a loopback host only. |
| `prism check-config PATH [--require-env]` | Validate a config file. With `--require-env`, also fail if a referenced key variable is not set. |
| `prism db-upgrade [--url URL]` | Create or migrate the database schema |
| `prism teams create NAME` / `prism teams list` | Manage teams |
| `prism keys create --team TEAM --name NAME [--routes A,B] [--expires-in-days N] [--config PATH]` | Create a virtual key. It is printed once. Routes are checked against the config. |
| `prism keys list [--team TEAM]` | List keys with their prefix, routes and status. Secrets are never shown. |
| `prism keys revoke ID_OR_PREFIX` | Revoke a key |
| `prism --version` | Print the version |
| `prism --install-completion` | Install shell completion for the current shell |

Run `prism --help` or `prism <command> --help` for every option.

### Endpoints

| Endpoint | Description |
| --- | --- |
| `GET /healthz` | Liveness check |
| `GET /readyz` | Readiness check: version and loaded provider and route counts |
| `POST /v1/chat/completions` | OpenAI chat completions, with SSE streaming and tool calls. Needs a virtual key. |
| `GET /v1/models` | The routes the virtual key may use |

## Architecture

![Prism architecture](docs/diagrams/architecture.svg)

The diagram shows the target design. Today Prism runs as the data plane only: it reads the YAML config at startup, checks virtual keys against the database (through an in-memory or Redis cache), and keeps no other state, so several instances can run side by side. The control plane (admin API, dashboard and live config reload) is not built yet.

Each request passes through five stages: authenticate (virtual key), pre-process (plugins), route, call the provider, post-process. Every failure is classified. It is then retried, failed over to the next target, or returned to the client as a standard OpenAI error object.

![Request lifecycle](docs/diagrams/request-flow.svg)

Most of the logic sits behind three contracts:

| Contract | Location | Purpose |
| --- | --- | --- |
| `ProviderAdapter` | [`src/prism/providers/base.py`](src/prism/providers/base.py) | Translates requests, responses, streams and errors for one provider |
| `Plugin` | [`src/prism/plugins/base.py`](src/prism/plugins/base.py) | Runs hooks before and after each request |
| `RoutingPolicy` | [`src/prism/core/policies/base.py`](src/prism/core/policies/base.py) | Decides the order in which a route's targets are tried |

Design decisions are recorded as ADRs in [`docs/adr/`](docs/adr/).

## Development

```bash
make sync        # install dependencies
make hooks       # install the Git hooks (once per clone)
make check       # lint, type check, test, and verify the Postman collection
make format      # auto-fix lint issues and format
make run         # start the server with the example config (after the Quickstart setup)
make postman     # regenerate the Postman collection
```

The test suite has three layers:

- **Unit tests** (`tests/unit/`) run fast and do no I/O.
- **Contract tests** (`tests/contract/`) check that a provider adapter behaves correctly.
- **Integration tests** (`tests/integration/`) cover the database and the web app.

The suite fails if coverage drops below 85%. Pyright runs in strict mode on `core`, `providers` and `plugins`.

The database tests run on SQLite by default. To also run them on PostgreSQL, as CI does, point them at an empty database that they may wipe:

```bash
docker run -d --rm --name prism-test-pg -e POSTGRES_USER=prism -e POSTGRES_PASSWORD=prism -e POSTGRES_DB=prism_test -p 127.0.0.1:55432:5432 postgres:16-alpine
```

```bash
PRISM_TEST_POSTGRES_URL=postgresql+asyncpg://prism:prism@127.0.0.1:55432/prism_test make test
```

### Git hooks

`make hooks` points Git at the hooks in [`scripts/githooks/`](scripts/githooks/):

| Hook | Runs | Checks |
| --- | --- | --- |
| `pre-commit` | On every commit, on staged files only | Blocks `.env` files and private keys. Runs ruff lint and format checks. |
| `pre-push` | Before every push | The full CI gate: ruff, pyright, tests with coverage, and the Postman collection check |

To skip a hook once, add `--no-verify` to `git commit` or `git push`.

### Postman collection

`make postman` generates these files in [`docs/postman/`](docs/postman/) from the app's OpenAPI schema:

| File | Contents |
| --- | --- |
| `prism.postman_collection.json` | Every endpoint, grouped by tag, with example request bodies |
| `prism-local.postman_environment.json` | The **Prism (Local)** environment |

To use them:

1. In Postman, choose **Import** and select both files.
2. Select the **Prism (Local)** environment in the top-right corner.
3. Set `virtualKey` to your Prism virtual key. It's stored as a secret, so Postman masks it.

| Variable | Default | Purpose |
| --- | --- | --- |
| `baseUrl` | `http://127.0.0.1:8000` | Address of the gateway |
| `virtualKey` | empty | Bearer token sent with authenticated requests |

Don't put your key into the files before committing them. Set it only in Postman.

To add another environment, such as staging, add an entry to `ENVIRONMENTS` in [`scripts/generate_postman.py`](scripts/generate_postman.py) and run `make postman`.

After changing an endpoint, run `make postman` and commit the result. The pre-push hook and CI fail if any of these files are out of date.

### Adding a provider adapter

1. Create `src/prism/providers/<name>.py` with a class that implements `ProviderAdapter` (`chat`, `chat_stream`, `classify_error`). If the provider speaks the OpenAI format, add a preset for `OpenAICompatAdapter` instead.
2. Add a `ProviderKind` in [`config.py`](src/prism/config.py) and build the adapter in `build_adapter` in [`providers/registry.py`](src/prism/providers/registry.py).
3. Write a test harness that drives a simulated upstream. Use [`tests/contract/test_fake_adapter.py`](tests/contract/test_fake_adapter.py) as a reference.
4. Subclass `AdapterContractSuite` with your harness. The suite checks usage reporting, the shape of the stream, and how every kind of error is classified.

An adapter is complete when it passes the full contract suite.

### Project layout

```text
src/prism/
├── api/           HTTP routes (/v1/chat/completions, /v1/models)
├── core/          types, errors, gateway pipeline, key pools, circuit breakers
│   ├── policies/      routing policy contract, one file per policy, and the registry that picks one
│   └── governance/    virtual key and team rules, key hashing, cache and authentication
├── providers/     adapter contract, OpenAI-compatible, Anthropic and Gemini adapters, registry
├── plugins/       plugin contract
├── store/         database models, migrations, team and key management, Redis key cache
├── config.py      configuration schema and loader
├── main.py        ASGI application
└── cli.py         the `prism` command
tests/
├── unit/
├── contract/
└── integration/
scripts/
├── githooks/              pre-commit and pre-push hooks
├── install-githooks.sh
└── generate_postman.py    builds the Postman collection from the OpenAPI schema
docs/
├── PRD.md         product requirements
├── adr/           architecture decision records
├── postman/       generated Postman collection
└── diagrams/
```

## Contributing

Contributions are welcome. Before opening a pull request:

1. Open an issue to discuss significant changes.
2. Make sure `make check` passes.
3. For a design change, add or update an ADR in `docs/adr/`.

Prism is also offered under a commercial license, so contributors will be asked to sign a Contributor License Agreement (CLA). The CLA is still being prepared, so please open an issue before starting work on a pull request.

## License

Prism is licensed under the [GNU Affero General Public License v3.0](LICENSE) (`AGPL-3.0-only`).

You can use, modify and self-host Prism, including inside a company. If you modify Prism and let others use it over a network, for example as a hosted service, you must make your modified source code available to those users under the same license.

**Commercial license:** a separate commercial license is available for organisations that cannot meet the AGPL's terms, for example to offer Prism as part of a proprietary or hosted product. Open an issue to get in touch.
