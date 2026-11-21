# 0006: One OpenAI-compatible adapter with presets, plus native Anthropic and Gemini

Status: proposed

## Context

Prism v1 targets OpenAI, Anthropic, Gemini, Groq, Mistral, Cerebras, NVIDIA NIM,
Cloudflare Workers AI, GitHub Models and Ollama. Most of them accept the OpenAI chat
completions format and differ only in small ways: base URL, unsupported
parameters, renamed parameters and where streaming usage appears. Anthropic and
Gemini have their own formats.

Facts were checked against the providers' documentation in November 2026. Bifrost
and LiteLLM were used only to learn edge cases; no code was copied.

## Decision

- **`OpenAICompatAdapter`** serves every OpenAI-compatible provider. Each provider's
  differences live in an `OpenAICompatPreset` in `providers/registry.py`: base URL,
  token field (`max_completion_tokens` for OpenAI), parameters to drop or rename
  (Mistral `seed` → `random_seed`), whether to request `stream_options`, and
  whether to strip `messages[].name` (Groq).
- **`AnthropicAdapter`** and **`GeminiAdapter`** translate to and from the native
  Messages and `generateContent` APIs, including tool calls, system prompts,
  images, stop reasons, usage and streaming events.
- Anthropic requests use API version **`2023-06-01`**. The newer `2026-10-21`
  version rejects `temperature`, `top_p` and `top_k` on every model, which would
  break ordinary OpenAI-style requests.
- Gemini uses `generateContent` / `streamGenerateContent`, not the newer
  Interactions API. Tool parameters and JSON output use `parametersJsonSchema` and
  `responseJsonSchema`, so standard JSON Schema works unchanged. Gemini
  `thoughtSignature` values round-trip through `tool_calls[].extra_content.google`,
  the same field Google's own OpenAI-compatible endpoint uses.
- **GitHub Models was retired on 2026-07-30.** The `github_models` kind stays in the
  config schema so old files give a clear error, but building it fails with a
  config error.
- Errors are classified once, in `providers/http.py`. A 402 (billing) and an
  Anthropic `enforced_spend_limit_reached` 429 count as `auth_failed`: the key
  cannot be used until someone acts, so it is disabled and the next target is
  tried. Groq's 498 (capacity) and 424 count as `upstream_unavailable`.
- Prism always asks the provider for stream usage (where supported) so later
  phases can meter tokens, but only forwards it to the client when the client
  asked with `stream_options.include_usage`.

## Consequences

- Adding an OpenAI-compatible provider is a new preset, not a new class.
- Native adapters carry more translation code and are covered by their own
  contract tests and translation tests.
- Some parameter support is undocumented (Cerebras, NVIDIA NIM and Cloudflare
  `stream_options`). Their presets do not send it, so streamed usage may be
  missing for them until verified.
- Newer Claude models reject forced tool use (`tool_choice: "required"` or a named
  tool). Prism passes the request through and the provider's 400 reaches the
  client as `invalid_request`.
