# SQL Query API

Run SELECT-only ClickHouse SQL over your project's data. Two ways in:

- **CLI** — `lmnr-cli sql query "<sql>"` (see [cli.md](cli.md)). Use the CLI is the most convenient way to query.
- **HTTP API** — `POST /v1/sql/query` (covered below). Only use the HTTP API for scripting or if you don't have CLI access.

## Endpoint and auth

- **Method/path:** `POST /v1/sql/query`
- **Base URL:** `https://api.lmnr.ai` by default. For self-hosted, use your Laminar base URL.
- **Headers:**
  - `Authorization: Bearer <project_api_key>`
  - `Content-Type: application/json`
  - `Accept: application/json`

## Request body

```json
{
  "query": "SELECT * FROM spans WHERE start_time > now() - INTERVAL 1 DAY",
  "parameters": {}
}
```

- `query` is required and must be SELECT-only.
- `parameters` is optional (send `{}` when unused). Placeholders use typed syntax `{name:Type}`:

```json
{
  "query": "SELECT * FROM spans WHERE trace_id = {trace_id:UUID} AND start_time > now() - INTERVAL 1 DAY",
  "parameters": { "trace_id": "01234567-89ab-4def-1234-426614174000" }
}
```

## Response

```json
{ "data": [ { "name": "span1", "output": "{\"result\": \"ok\"}" } ] }
```

`data` is an array of row objects. Non-2xx responses include an error body; SDKs raise.

## Tables

`spans`, `traces`, `events`, `tags`, `dataset_datapoints`, `dataset_datapoint_versions`, `evaluation_datapoints`. SELECT only. Queries are scoped to your project automatically — no tenant filter needed.

## Query guidance

- **Always filter by time** (`start_time`) for performance.
- **Avoid joins** — run multiple queries and combine in your app.
- **Bucket by interval** with `toStartOfInterval` / `toStartOfDay` / `toStartOfHour`.
- **JSON is stored as strings** — use `simpleJSONExtract*` for fast access and `simpleJSONHas` to check keys.
- **Common JSON columns:** `spans` → `input`, `output`, `attributes`; `evaluation_datapoints` → `data`, `target`, `metadata`, `executor_output`, `scores`; `dataset_datapoints` → `data`, `target`, `metadata`.

## Example queries

Cost by model:

```sql
SELECT model, sum(total_cost) AS total_cost, count(*) AS call_count
FROM spans
WHERE span_type = 'LLM' AND start_time > now() - INTERVAL 7 DAY
GROUP BY model ORDER BY total_cost DESC
```

Slowest operations:

```sql
SELECT name, avg(end_time - start_time) AS avg_duration_ms
FROM spans
WHERE start_time > now() - INTERVAL 1 DAY
GROUP BY name ORDER BY avg_duration_ms DESC LIMIT 10
```

Error rate by span name:

```sql
SELECT name, countIf(status = 'error') AS errors, count(*) AS total,
       round(errors / total * 100, 2) AS error_rate
FROM spans
WHERE start_time > now() - INTERVAL 1 DAY
GROUP BY name HAVING total > 10 ORDER BY error_rate DESC
```

## curl example

```bash
curl -sS -X POST "${LMNR_BASE_URL:-https://api.lmnr.ai}/v1/sql/query" \
  -H "Authorization: Bearer $LMNR_PROJECT_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -d '{"query":"SELECT name, input, output FROM spans WHERE start_time > now() - INTERVAL 1 DAY","parameters":{}}'
```
