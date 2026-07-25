# Alarm Response Design

## Goals

- Separate "dedup for persistence/reporting" from "dedup for frontend UX".
- Keep semantics of different `alarm_type` events.
- Make frontend rendering idempotent, low-noise, and traceable.
- Keep backward compatibility for current `/task/msg/{client_id}` and `/task/message/{client_id}` consumers.

## Current State Summary

- Alarm production:
  - Temporal analyzers produce `AlarmInfo`.
  - Health monitor can also produce alarms (for example timeout).
- Realtime channel:
  - In-memory ring buffer `ClientQueues._alarm_log` stores recent alarms.
  - Frontend reads `recent_alarms` via websocket `/task/msg/{client_id}` or polling `/task/message/{client_id}`.
- Persistence/reporting channel:
  - `PersistenceManager.persist_alarm()` -> `AlarmWorkerPool` batch + cooldown -> HTTP report.
  - Aggregation key is currently `task_id + stage`.

## Current Risks

1. Semantic merge risk:
   - Different `alarm_type` under same `task_id + stage` may be merged.
2. Frontend dedup instability:
   - Realtime payload lacks stable event id; frontend often must dedup by timestamp/message heuristics.
3. Inconsistent alarm families:
   - Some alarms (for example stage-less/system alarms) may not enter HTTP report path due to `task_id && stage` guard.
4. Weak lifecycle model:
   - No explicit "open/update/resolve" event model for UI cards/toasts.

## Design Principles

1. Dual-layer dedup:
   - Layer A (backend reporting dedup): reduce upstream pressure.
   - Layer B (frontend display dedup): avoid duplicate toasts/cards.
2. Type-safe identity:
   - Identity must include `alarm_type` at minimum.
3. Event-first contract:
   - Frontend consumes immutable event records with stable `event_id`.
4. Compatibility-first migration:
   - Keep old fields; add new fields first; remove old behavior later.

## Alarm Taxonomy

Use two dimensions:

- `alarm_type`: business class (`PROCESS_VIOLATION`, `TASK_TIMEOUT`, ...).
- `alarm_source`: where it is generated (`temporal`, `health_monitor`, `manual`, ...).

Keep `alarm_level` as severity only (`low|medium|high|critical`).

## Identity And Dedup Model

### 1) Event Identity (No merge)

Every produced alarm event should get:

- `event_id`: UUIDv7 or ULID (sortable).
- `occurred_at`: unix seconds/ms.
- `fingerprint`: hash over a canonical tuple.

Suggested canonical tuple:

- `(task_id, stage_or_none, alarm_type, source, normalized_message, major_metadata_signature)`

`event_id` is unique per event instance.
`fingerprint` is used for dedup/window counting.

### 2) Reporting Dedup (Backend)

Replace aggregation key from:

- `task_id + stage`

to:

- `task_id + stage_or_none + alarm_type + source + fingerprint_scope`

Recommended `fingerprint_scope`:

- For process alarms: include detector/analyzer logical key (for example `bubble_rate`, `bending_streak`).
- For system alarms: include subsystem key (for example `task_timeout`, `decoder_disconnect`).

Aggregation output payload keeps:

- `first_seen`, `last_seen`, `alarm_count`
- plus a `sample_event_id` for traceability.

### 3) Frontend Dedup (Client UX)

Frontend should dedup by:

- Primary key: `event_id` (never duplicate render).
- Secondary anti-flood window (optional): same `fingerprint` within `N` seconds can collapse into "xN" badge in UI list (not dropping raw events).

## Frontend Contract Proposal

## Realtime Payload (`/task/msg/{client_id}` and `/task/message/{client_id}`)

Keep existing:

- `stage`
- `detections`
- `recent_alarms` (legacy)

Add new field:

- `alarm_stream`: list of event objects (latest first or oldest first, fixed and documented).

`alarm_stream` event schema:

- `event_id: str`
- `fingerprint: str`
- `task_id: int | null`
- `stage: str | null`
- `alarm_type: str`
- `alarm_level: str`
- `alarm_source: str`
- `message: str`
- `occurred_at: int`
- `status: "open" | "update" | "resolved"`
- `count: int` (for aggregated updates, default 1)
- `metadata: object`

Legacy compatibility:

- Continue filling `recent_alarms` from `alarm_stream` projection.

## Historical Query (`/task/{task_id}/alarms`)

Add optional query params:

- `include_aggregated=true|false`
- `group_by=fingerprint|none`
- `since=<timestamp>`

Return should include `event_id` and `fingerprint` whenever available.

## UI Interaction Model

1. Toast rules:
   - Show toast on `status=open`.
   - If same `fingerprint` gets `status=update`, update existing toast/card counter instead of new toast.
2. Panel/list rules:
   - Display chronological events with badge count.
   - Filter by `alarm_type`, `alarm_level`, `stage`.
3. Resolve rules:
   - `status=resolved` visually closes/open card.
4. Offline/reconnect rules:
   - Frontend keeps `last_seen_event_id` and requests replay (future extension) or reconciles with `/task/message/{client_id}` snapshot.

## Backend State Model (Recommended)

For each `fingerprint` maintain lightweight state in memory:

- `state: open | resolved`
- `open_event_id`
- `last_event_id`
- `count_in_window`
- `first_seen`, `last_seen`

This state serves:

- Better `status` emission for UI.
- Better aggregated reporting payload.

## Migration Plan

1. Phase 1 (compatible):
   - Add `event_id`, `fingerprint`, `alarm_source`, `occurred_at` at producer path.
   - Keep old fields and endpoints unchanged.
2. Phase 2 (dedup fix):
   - Update persistence aggregation key to include `alarm_type` and source dimensions.
3. Phase 3 (frontend contract):
   - Add `alarm_stream` in realtime message.
   - Frontend switches dedup key to `event_id`.
4. Phase 4 (history consistency):
   - Extend history endpoint with grouped/raw options.
5. Phase 5 (cleanup):
   - Deprecate `recent_alarms` if frontend fully migrated.

## Minimum Safe Change Set

If we only do one small but high-value backend fix first:

- Change dedup key from `task_id + stage` to `task_id + stage + alarm_type`.

This alone prevents cross-type semantic merge while keeping behavior mostly stable.

