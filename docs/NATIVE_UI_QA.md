# Native design and behavior QA — 2026-10-12

Final result: passed (native desktop scope).

Compared the approved `windows-revised-composer.png` and
`windows-seat-plan-preview.png` with real Compose desktop captures at 1500×1050:
[`single request`](../app/screenshots/09_windows_single_request.png) and
[`seat plan`](../app/screenshots/10_windows_seat_plan.png).

The light layout preserves top navigation, goal/template rail, editable middle pane,
and colored server evidence. All six single-request goals remain visible. Seat roles,
fixed/app bindings, exact membership chips, shared/per-seat filters, reference identities
and PR revisions are editable. Blue/green/amber/neutral statuses carry text labels;
reused weight identity keeps the same color and app-fixed evidence stays neutral.

Real local FastAPI/router fixtures, with no provider calls, resolved the overlapping
review pair and triple as B/C/A/B. The two reviewers differ from the registered
head-bound author A. Clicking “All seats” added the exact four-slot group and the
server returned UNSATISFIABLE rather than waiving diversity. Stale draft previews
are labeled. Missing author provenance produced UNKNOWN, with the missing fact shown.
A native text edit followed by selecting Quick answer preserved the edited request
and labeled the previous preview as a changed draft (screenshot 11).

Offline verification: 33 desktop tests pass; desktop runnable JAR and Android debug
APK build. Tests cover strict wire keys, role/count validation, exact costs and unknowns,
generic versus PR references, separate token estimates, custom edit preservation,
overlapping groups, explicit bindings, and absence of prompt/task bodies in plans.

Minor visual differences: the native app uses bundled IBM Plex fonts; additional
controls and provenance details scroll in their own panes. Dense considered-model
tables retain horizontal scrolling. Windows MSI install/launch and Android device
runtime verification are outside this Linux native render check.
