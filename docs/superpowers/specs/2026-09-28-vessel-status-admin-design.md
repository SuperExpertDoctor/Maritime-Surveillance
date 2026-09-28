# Admin vessel status and placement design

## Scope

The right sidebar keeps its mission overview, UAV list, model summary, and other
unrelated controls. Only its vessel editor changes: it retains the Type I and
Type II drag palette, the placement affordance, and placement feedback. Its
vessel inventory, selected-vessel detail, AIS controls, and delete action move
to the bottom drawer. The existing `AIS` tab becomes `船舶状态`.

The status tab is an administrator truth view. It contains every current
scenario vessel, including undetected Type II targets. Its data must not be
inserted into red-side observations or decision-maker prompts.

## Interaction

- Dropping a vessel palette item or placing the selected type on the map
  creates a vessel at the center of the targeted grid cell. The entire task
  grid is addressable, subject to the existing land, obstacle, collision, and
  capacity validation. An invalid drop reports the server rejection.
- The table keeps one row per `scenario_entity_id`. It adds rows for new ships,
  updates position and status in place from authoritative frames, and removes
  rows only after deletion is confirmed by a frame. It does not append a row
  for every telemetry update.
- Columns show vessel ID, class, AIS status, maneuver parameters, adjustment
  reason, current true grid position, and a delete icon. Type I AIS displays an
  on/locked state. Type II AIS uses an accessible switch. Class, position, and
  command results remain readable at the bottom drawer's mobile width through
  contained horizontal scrolling.
- The delete icon has a tooltip naming the action and ship. There is no
  right-click menu. AIS and delete commands use the existing episode-scoped,
  revision-checked queue; show pending and rejection feedback on the relevant
  row; and are disabled when disconnected, read-only in replay, finished, or
  already pending. Never optimistically change the authoritative AIS value or
  remove a row before frame confirmation. Selection is independent of map
  positioning, so one row cannot accidentally operate on another ship.

## Data and causality

- Extend the published `scenario_vessels` inventory with current motion
  fields. Type I shows actual speed and heading, without attributing its
  movement to an LLM. Type II shows the installed `RedMotionParameters`
  (speed, heading offset, zigzag heading and period, phase) when active, or an
  explicit no-active-plan state.
- When a `red_commander` model call produces a plan, associate that call's
  sanitized `reason_content` with its snapshot/plan ID and each vessel named
  in the installed commands. Publish the latest installed plan ID, decision
  time, parameters, and reason with the corresponding vessel inventory row.
  Never reuse another role's reason or attribute a reused or retired plan to
  a fresh model decision; unavailable reason content is shown as unavailable.
- Preserve the existing AIS evidence semantics: disabling Type II AIS clears
  its current signal and stops new AIS samples but does not erase previously
  observed contacts/evidence; their existing TTL and information update
  mechanisms continue. Re-enabling forces the next AIS refresh and the
  resulting information delta and trigger behavior. Type I cannot be
  disabled at the API or engine layer.
- Replay consumes recorded inventory as read-only truth. Older recordings
  lacking new maneuver fields show `not recorded`, not invented values.

## Verification

Test per-row AIS and deletion commands, revision conflicts, episode resets,
the immutable Type I switch, AIS off/on evidence refresh and expiry, plan-to-
vessel reason attribution and plan retirement, and desktop/mobile placement
and table rendering. Confirm replay remains read-only and older recordings
render safely. Existing mission overview and UAV controls must remain intact.
