# Quartus 25.1 Agilex 7 stage fixture

This fixture records a real five-snapshot probe of one project state. The four
intermediate collections and final collection share source fingerprint
`952960cdd4b83978aec4f6be3449dd21733bdab70921c65c5832571f80ac3a3b`.

The probe observed timing paths at `planned`, register spread at `placed`,
routing detail at `routed`, retiming reports at `retimed`, and Compilation
Report DB correlation at `final`. Unsupported reports were deliberately marked
`unavailable-for-stage` rather than executed.

`stage_capabilities.json` also preserves the number of structured records
produced at every stage. This keeps the regression fixture small while proving
that each supported report actually returned data in the real run.

`design_assistant_panel.json` is the normalized selected-panel object extracted
from the matching Quartus Prime Pro 25.1 final Report DB. It validates the
recognized zero-violation format and prevents an unrecognized format from
being reported as `pass`.

`check_timing_summary.json` is the real Quartus 25.1 summary used to validate
the health classification. Its only nonzero finding is `no_clock = 311`, which
must remain blocking; uncalibrated non-blocking names default to warning rather
than informational.
