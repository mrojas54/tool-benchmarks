# TypeScript / JavaScript instrumentation

## Contents

- Install
- Initialize once
- Next.js
- Vercel AI SDK
- Coexisting with another OpenTelemetry SDK
- Manual spans with `observe`
- Low-level span APIs
- Trace context: user, session, metadata
- Tags
- Privacy controls
- Custom LLM spans and cost tracking
- Cross-service / async context propagation
- Flushing and shutdown

## Install

Detect the package manager from the lockfile, then install `@lmnr-ai/lmnr`:

```bash
# pnpm-lock.yaml -> pnpm add @lmnr-ai/lmnr
# yarn.lock      -> yarn add @lmnr-ai/lmnr
# package-lock.json -> npm install @lmnr-ai/lmnr
# bun.lockb      -> bun add @lmnr-ai/lmnr
```

## Initialize once

Call `Laminar.initialize()` as early as possible, before the traced code runs. For most semi-automatic instrumentations to work you must pass `instrumentModules` with the LLM SDK modules you use.

```ts
import { Laminar } from '@lmnr-ai/lmnr';
import OpenAI from 'openai';
import Anthropic from '@anthropic-ai/sdk';

Laminar.initialize({
  projectApiKey: process.env.LMNR_PROJECT_API_KEY,
  instrumentModules: {
    openAI: OpenAI,
    anthropic: Anthropic,
  },
  // Self-hosted Laminar (baseUrl must not include the port):
  // baseUrl: 'http://localhost',
  // httpPort: 8000,
  // grpcPort: 8001,
});
```

Spans export over gRPC by default. If you see an export error (`ECONNRESET`, `14 UNAVAILABLE`, "Protocol error"), check that `grpcPort` matches your instance — a wrong gRPC port is the usual cause, not the transport itself.

If a module is imported before `Laminar.initialize()` runs (common in serverless and Next.js server components), patch it in the module that constructs the client instead:

```ts
import { Laminar } from '@lmnr-ai/lmnr';
import OpenAI from 'openai';

Laminar.patch({ openAI: OpenAI });
```

## Next.js

- Add `serverExternalPackages: ['@lmnr-ai/lmnr']` to `next.config.ts`.
- Initialize inside `register()` in `instrumentation.ts`, gated on the Node runtime:

```ts
// instrumentation.ts
export async function register() {
  if (process.env.NEXT_RUNTIME === 'nodejs') {
    const { Laminar } = await import('@lmnr-ai/lmnr');
    Laminar.initialize({ projectApiKey: process.env.LMNR_PROJECT_API_KEY });
  }
}
```

- Because `instrumentation.ts` imports are isolated, patch LLM SDKs with `Laminar.patch({ ... })` in the module where you construct the clients.

## Vercel AI SDK

The AI SDK changed its telemetry API in **v7**. Check the installed `ai` version (`package.json`) and pick the matching wiring.

**AI SDK v7 (`ai@7+`):** register Laminar once at startup; every `generate*` / `stream*` / `embed` call is then traced with no per-call config. Use the AI SDK's native `registerTelemetry` with Laminar's integration class, or Laminar's all-in-one `registerAiSdkTelemetry()` (same effect, no `ai` import — handy when you want to register before the AI SDK loads). Both initialize Laminar for you; pass `laminarOptions` (e.g. `projectApiKey`, self-hosted `baseUrl`) to configure it without a separate `Laminar.initialize()` call.

```ts
import { registerTelemetry } from 'ai';
import { LaminarAiSdkTelemetry } from '@lmnr-ai/lmnr';

registerTelemetry(new LaminarAiSdkTelemetry());

// — or, equivalently, without importing from 'ai':
// import { registerAiSdkTelemetry } from '@lmnr-ai/lmnr';
// registerAiSdkTelemetry();
```

Then call the SDK as usual — no `experimental_telemetry`. Skip one call with `telemetry: { isEnabled: false }`; name a span or attach data with `telemetry: { functionId, metadata }`.

**AI SDK v5 / v6 (`ai@5`, `ai@6`):** instrumentation is **manual** — pass the Laminar tracer to `experimental_telemetry` on each call so model/tool spans attach to the right trace.

```ts
import { getTracer } from '@lmnr-ai/lmnr';
import { generateText } from 'ai';
import { openai } from '@ai-sdk/openai';

await generateText({
  model: openai('gpt-4.1-nano'),
  prompt: 'What is Laminar?',
  experimental_telemetry: {
    isEnabled: true,
    tracer: getTracer(),
  },
});
```

The AI SDK call emits its own span with nested LLM and tool spans, so **do not wrap the call in `observe`** just to get a root span; that adds a redundant layer. Reach for `observe` only to capture your own orchestration steps *outside* the AI SDK call (see [Manual spans](#manual-spans-with-observe)). To attach trace context, set it with `Laminar.setTraceUserId` / `setTraceSessionId` from an enclosing span you already have (or, on v7, pass it through `telemetry.metadata` / on v5–v6 `experimental_telemetry.metadata`) — not by adding a span solely to hold these values.

## Coexisting with another OpenTelemetry SDK

If the app already uses OpenTelemetry (e.g. `@vercel/otel`), pick one pattern and make sure only one pipeline instruments each module.

**A) Dual pipeline (Recommended) — general observability elsewhere, LLM tracing in Laminar.** Initialize your existing OTel SDK first, then call `Laminar.initialize()` to instrument only the LLM SDKs you want in Laminar.

If you see noisy HTTP/fs/dns spans, another OTel auto-instrumentation is running before Laminar — disable it so Laminar stays high-signal.

**B) Single pipeline — export Laminar spans through your existing OTel setup.** Use `LaminarSpanProcessor` and `initializeLaminarInstrumentations()` inside your OTel init. Do **not** also call `Laminar.initialize()`.

```ts
import { registerOTel } from '@vercel/otel';

export async function register() {
  if (process.env.NEXT_RUNTIME === 'nodejs') {
    const { LaminarSpanProcessor, initializeLaminarInstrumentations } =
      await import('@lmnr-ai/lmnr');

    registerOTel({
      serviceName: 'my-service',
      spanProcessors: [new LaminarSpanProcessor()],
      instrumentations: initializeLaminarInstrumentations(),
    });
  }
}
```


## Manual spans with `observe`

Wrap functions that represent meaningful steps. Arguments passed after the function are captured as span input (or set `input` explicitly).

```ts
import { observe } from '@lmnr-ai/lmnr';

const result = await observe(
  {
    name: 'agent.run',
    sessionId,
    userId,
    tags: ['agent', 'search'],
    metadata: { route: '/search' },
  },
  async (query, limit) => await retrieve(query, limit),
  query,
  limit,
);
```

## Low-level span APIs

- `Laminar.startActiveSpan(...)` — create a span that becomes the active parent for nested work.
- `Laminar.startSpan(...)` — create a detached span; activate it with `Laminar.withSpan(span, fn)`.
- Always end spans (`try/finally`). Never leak spans.

## Trace context: user, session, metadata

Set these near the start of the trace so every downstream span inherits them.

```ts
Laminar.setTraceUserId(userId);
Laminar.setTraceSessionId(sessionId);     // reuse across turns/workflows
Laminar.setTraceMetadata({ environment: process.env.NODE_ENV }); // JSON-serializable, stable keys, no PII
```

## Tags

Tags are categorical, low-cardinality labels (environment, feature flag, outcome).

- At creation: `observe({ tags: [...] }, ...)` or `startSpan({ tags: [...] })`.
- Inside a span context: `Laminar.setSpanTags([...])`.
- Post-hoc (e.g. user feedback): capture the trace ID inside a span context, then later `LaminarClient.tags.tag(traceId, ...)` to tag the root span.

## Privacy controls

- Disable capture with `ignoreInput` / `ignoreOutput`, or pass an explicit `input` formatter to redact.
- Never put secrets/PII into span names, tags, or metadata.

## Custom LLM spans and cost tracking

For a custom provider/tool, set `spanType: 'LLM'` (or `'TOOL'`) so the UI renders it correctly, and report usage attributes for cost tracking.

```ts
import { Laminar, LaminarAttributes, observe } from '@lmnr-ai/lmnr';

await observe({ name: 'llm.call', spanType: 'LLM', input: { prompt } }, async () => {
  const response = await callModel(prompt);
  Laminar.setSpanAttributes({
    [LaminarAttributes.PROVIDER]: 'openai',
    [LaminarAttributes.REQUEST_MODEL]: response.model,
    [LaminarAttributes.RESPONSE_MODEL]: response.model,
    [LaminarAttributes.INPUT_TOKEN_COUNT]: response.usage?.prompt_tokens,
    [LaminarAttributes.OUTPUT_TOKEN_COUNT]: response.usage?.completion_tokens,
  });
  Laminar.setSpanOutput(response.text);
  return response;
});
```

## Cross-service / async context propagation

When you can't pass span objects directly (HTTP, queues, cron), serialize context upstream and continue downstream.

```ts
// Upstream
const context = Laminar.serializeLaminarSpanContext();
await fetch('https://service-b/api', { headers: { 'X-Laminar-Span-Context': context ?? '' } });

// Downstream
const span = Laminar.startSpan({
  name: 'serviceBHandler',
  parentSpanContext: req.headers['x-laminar-span-context'] as string | undefined,
});
try {
  // ...
} finally {
  span.end();
}
```

If the context is missing or invalid, start a new trace — never break the app.

## Flushing and shutdown

- Don't flush in hot paths.
- In short-lived scripts and serverless handlers, `await Laminar.flush()` at the end so spans export before the process exits. The `await` is required.
- For one-off Node scripts, also call `await Laminar.shutdown()` when appropriate.
