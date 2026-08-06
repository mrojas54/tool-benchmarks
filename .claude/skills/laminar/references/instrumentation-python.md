# Python instrumentation

## Contents

- Install
- Initialize once
- Auto-instrumentation control
- Manual spans with `@observe`
- Manual span context
- Trace context: user, session, metadata
- Tags
- Privacy controls
- Custom LLM spans and cost tracking
- Cross-service / async context propagation
- Flushing and shutdown

## Install

Detect the dependency manager and install `lmnr`:

```bash
# Poetry        -> poetry add lmnr
# uv (uv.lock)  -> uv add lmnr
# requirements.txt -> add `lmnr`, then pip install -r requirements.txt
# pip directly  -> pip install lmnr   (or: pip install 'lmnr[all]')
```

If auto-instrumentation is missing spans for a specific provider, install that provider's extra (for example `pip install -U 'lmnr[vertexai]'`). Check the Laminar integrations docs when unsure which extra applies.

## Initialize once

Call `Laminar.initialize()` at startup, before traced code runs.

```python
import os
from lmnr import Laminar

Laminar.initialize(
    project_api_key=os.environ["LMNR_PROJECT_API_KEY"],
    # Self-hosted Laminar:
    # base_url="http://localhost",
    # http_port=8000,
    # grpc_port=8001,
)
```

## Auto-instrumentation control

Pass `instruments` to enable a specific set, or `disabled_instruments` to turn some off. `set()` disables all auto-instrumentation.

```python
from lmnr import Laminar, Instruments

Laminar.initialize(instruments={Instruments.OPENAI})   # only OpenAI
# Laminar.initialize(disabled_instruments={Instruments.LANGCHAIN})
```

Note: some instruments auto-enable when their library is installed (e.g. `pydantic_ai`, `deepagents`) and auto-disable overlapping raw-provider instrumentors to avoid double-tracing the same call. Pass an explicit `instruments` set or `disabled_instruments` to override.

## Manual spans with `@observe`

Decorate functions that represent meaningful steps.

```python
from lmnr import observe

@observe(
    name="agent.run",
    tags=["agent", "search"],
    metadata={"route": "/search"},
)
def run_agent():
    retrieve_context()

@observe(name="retrieve.context", ignore_inputs=["secrets"])  # hide sensitive args
def retrieve_context():
    ...
```

## Manual span context

For ad-hoc spans, use the context manager. For cases where you must hold a span object, use `start_span()` + `use_span()`. Always let the context manager end the span.

```python
from lmnr import Laminar

with Laminar.start_as_current_span("step", input=payload) as span:
    result = do_work(payload)
    Laminar.set_span_output(result)
```

## Trace context: user, session, metadata

Set these near the start of the trace so downstream spans inherit them.

```python
Laminar.set_trace_user_id(user_id)
Laminar.set_trace_session_id(session_id)        # reuse across turns/workflows
Laminar.set_trace_metadata({"environment": os.getenv("ENVIRONMENT")})  # JSON-serializable, stable keys, no PII
```

## Tags

Categorical, low-cardinality labels (environment, feature flag, outcome).

- At creation: `@observe(tags=[...])` or `start_as_current_span(..., tags=[...])`.
- Inside a span context: `Laminar.add_span_tags([...])`.
- Post-hoc (e.g. user feedback): capture the trace ID in a span context, then later `LaminarClient.tags.tag(trace_id, ...)`.

## Privacy controls

- Disable capture with `ignore_inputs` / `ignore_input` / `ignore_output`, or pass input/output formatters to redact.
- Never put secrets/PII into span names, tags, or metadata.

## Custom LLM spans and cost tracking

For a custom provider, set `span_type="LLM"` and report usage attributes so cost is tracked.

```python
from lmnr import Attributes, Laminar

with Laminar.start_as_current_span("llm.call", span_type="LLM", input=prompt):
    response = call_model(prompt)
    Laminar.set_span_output(response.text)
    Laminar.set_span_attributes({
        Attributes.PROVIDER: "openai",
        Attributes.REQUEST_MODEL: response.model,
        Attributes.RESPONSE_MODEL: response.model,
        Attributes.INPUT_TOKEN_COUNT: response.usage.prompt_tokens,
        Attributes.OUTPUT_TOKEN_COUNT: response.usage.completion_tokens,
    })
```

## Cross-service / async context propagation

When you can't pass span objects directly (HTTP, queues, cron), serialize context upstream and continue downstream.

```python
# Upstream
context = Laminar.serialize_span_context()
requests.post("https://service-b/api", headers={"X-Laminar-Span-Context": context or ""})

# Downstream
parent = Laminar.deserialize_span_context(request.headers.get("X-Laminar-Span-Context"))
with Laminar.start_as_current_span(name="service_b_handler", parent_span_context=parent):
    handle_request()
```

If the context is missing or invalid, start a new trace — never break the app.

## Flushing and shutdown

- Don't flush in hot paths.
- In short-lived scripts, call `Laminar.flush()` at the end so spans export before the process exits.
- In serverless handlers, use `Laminar.force_flush()` when needed.
