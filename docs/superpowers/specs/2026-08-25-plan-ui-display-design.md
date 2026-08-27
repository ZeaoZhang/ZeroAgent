# Plan UI Display Design

## Problem

Desktop Plan mode currently renders ACP `plan` updates as generic assistant turns, so users cannot quickly identify the plan task, step states, or progress. Long Plan tasks also become session titles without a width constraint, allowing the topbar title to compete with the normal conversation area and status controls.

## Goals

- Render the latest ACP plan entries as a structured Plan card.
- Make task, progress, current state, and executable readiness visually distinct.
- Keep full task text inside the message card while constraining the topbar title to one line with ellipsis.
- Preserve existing Plan RPC/state behavior and existing execute-button semantics.
- Avoid fabricating plan steps when no ACP entries are available.

## Non-goals

- No backend API or persistence schema changes.
- No change to Plan execution lifecycle or status transitions.
- No redesign of the general message renderer or session list.
- No changes to unrelated existing working-tree files.

## Design

### Plan data flow

Each local session gains `planEntries`, initialized to an empty array. On an ACP `session/update` whose `sessionUpdate` is `plan`, normalize each entry to `{ content, status }`, replace the session snapshot, and refresh only the active session's Plan card. The backend `planStatus` remains the lifecycle status (`planning`, `ready`, `executing`, etc.); entry status remains the step status (`pending`, `in_progress`, `completed`, or unknown).

The Plan card is rebuilt from session metadata and the latest entries. Historical sessions without entries continue to show task/path/lifecycle status only. All user/model-provided values enter the DOM through `textContent`.

### Plan card

`renderPlanStatus()` produces:

- a `计划` header and localized lifecycle status;
- the complete task text, wrapping within the card;
- a progress summary (`completed / total`) when entries exist, otherwise a waiting label;
- a list of entries with distinct completed, in-progress, and pending/unknown visual states;
- the existing `执行计划` button only when lifecycle status is `ready`.

The current card replacement behavior remains, preventing duplicate cards during state refreshes.

### Title layout

The topbar's left region and title may shrink. `#session-title` receives a bounded width, `min-width: 0`, one-line overflow hiding, and ellipsis. Its full value remains available through the element title/accessibility label. The right-side controls remain non-shrinking.

## Testing

Extend `tests/frontend_plan_ui.test.js` to cover:

- local session Plan entry initialization;
- ACP plan entry normalization and replacement;
- Plan card progress and entry state rendering;
- empty-entry cards not fabricating steps;
- long title metadata remaining intact for accessibility.

Run the focused Node UI test and the relevant desktop bridge Python tests. Verify the actual static desktop surface with a browser smoke check when the desktop bridge is available.

## Risks and mitigations

- ACP may omit `status` or `content`: normalize to safe defaults and render a readable fallback.
- Repeated plan updates may reorder entries: replace the snapshot rather than append duplicates.
- Long unbroken paths/text may overflow: enable wrapping on card content and path text.
- Existing fake DOM tests lack selector traversal: add only the minimal fake DOM capability needed for observable card assertions.
