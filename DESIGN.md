# Design

## Source of truth
- Status: Active
- Last refreshed: 2026-09-23
- Primary product surfaces: ZeroAgent desktop session workspace
- Evidence reviewed: `zero_agent/frontends/desktop/static/index.html`, `styles.css`, `app.js`, and the session sidebar design specs.

## Brand
- Personality: Quiet, focused, tool-like.
- Trust signals: Clear state indicators and predictable controls.
- Avoid: Decorative cards, excessive borders, and competing background blocks.

## Product goals
- Goals: Keep sessions scannable, make user-created groups easy to recognize, and make moving a session predictable.
- Non-goals: Change session organization behavior or the conversation workspace.
- Success signals: Group headings and session rows form a quiet, clear sidebar hierarchy; move actions use a compact, readable popover.

## Personas and jobs
- Primary personas: Developers managing multiple agent conversations.
- User jobs: Find, switch, group, and remove sessions quickly.
- Key contexts of use: Desktop, keyboard and mouse, light or dark theme.

## Information architecture
- Primary navigation: Session list on the left, active conversation in the center.
- Core routes/screens: Session workspace and modal configuration surfaces.
- Content hierarchy: Active conversation > session title > optional user-created group.

## Design principles
- Principle 1: Use typography and spacing before containers or fills to express hierarchy, following the restrained ChatGPT sidebar reference.
- Principle 2: Keep inactive navigation visually quiet and preserve state dots for runtime status.
- Tradeoffs: The active session uses weight and text color instead of a background highlight.

## Visual language
- Color: Neutral surfaces; no background-color distinction between session states.
- Typography: System sans-serif with medium weight for the active session.
- Spacing/layout rhythm: Compact rows with consistent vertical padding; group children are slightly indented.
- Shape/radius/elevation: Session rows use a soft hover surface; the move menu is a rounded floating surface with a light shadow.
- Motion: Short opacity/color transitions only for controls and runtime indicators.
- Imagery/iconography: Small status dot; action icons appear on hover/focus.

## Components
- Existing components to reuse: `.session-item`, `.session-group-header`.
- New/changed components: The group heading separates its disclosure control from its delete action; the move-to-group popover uses standard buttons for each destination and action.
- Variants and states: Active, hover, busy, error, configured, unconfigured, connected, disconnected, keyboard focus.
- Token/component ownership: Existing CSS variables in `styles.css` remain authoritative.

## Accessibility
- Target standard: Preserve current keyboard and screen-reader semantics.
- Keyboard/focus behavior: Group toggle, delete, move, and popover actions remain separate keyboard controls with visible focus; Escape closes the popover and returns focus to its trigger.
- Contrast/readability: Active text must remain distinguishable without a background fill.
- Screen-reader semantics: Group disclosure and move destinations expose their current state; avoid nested interactive elements.
- Reduced motion and sensory considerations: Do not add new motion.

## Responsive behavior
- Supported breakpoints/devices: Existing desktop and compact desktop layout.
- Layout adaptations: Clamp the move popover to the viewport; reveal row actions on hover and keyboard focus.
- Touch/hover differences: Focus-visible states remain available when hover is absent.

## Interaction states
- Loading: Use the existing status dot behavior.
- Empty: Use the existing empty-session presentation.
- Error: Keep the existing error dot and error messaging.
- Success: Keep the existing green status dot.
- Disabled: Preserve existing button disabled behavior.
- Channel configuration: Every channel card shows a read-only connection state; WeChat QR login is available only inside its configuration modal. The run control is off until the runtime reports that the channel is running.
- Offline/slow network, if applicable: Preserve existing bridge status handling.

## Content voice
- Tone: Concise and neutral.
- Terminology: Keep existing session and group labels; use `未连接`, `已连接`, and `已断开` for the WeChat connection status.
- Microcopy rules: Use concise Chinese labels for group actions.

## Implementation constraints
- Framework/styling system: Static HTML/CSS/JavaScript frontend.
- Design-token constraints: Reuse existing neutral and status color variables.
- Performance constraints: Keep status rendering local to the existing channel refresh flow.
- Compatibility constraints: Keep Tauri/Web2 shared static assets compatible.
- Test/screenshot expectations: Run focused frontend tests and CSS/source checks.

## Open questions
- [ ] Whether the same flat treatment should later be applied to modal cards and agent cards.
