# AGENTS.md

## Purpose

This repository provides **evidence collection and deterministic analysis for Quartus timing closure**.
Agents working in this repository must preserve that role:

- collect or parse Quartus evidence;
- normalize it without inventing facts;
- compare runs reproducibly;
- expose enough evidence for an engineer or a downstream agent to make an RTL/QSF/SDC decision.

qclose is **not** an autonomous RTL optimizer. Do not make qclose silently mutate a user's RTL, QSF, SDC, project settings, or Quartus database.

## Source of truth

Treat evidence in this order:

1. Quartus Timing Analyzer / Compilation Report Database output.
2. qclose normalized records that preserve source provenance.
3. generated RTL and synthesis/fitter node identities.
4. source RTL/Chisel/Scala.
5. heuristic interpretation.

Never promote a heuristic or hierarchy-only correlation to a proven root cause.

Keep these distinctions intact:

- `exact-node`: strong evidence.
- validated replica `base-node`: strong evidence.
- `normalized-node`: medium evidence.
- `same-hierarchy`: contextual evidence only.
- `issue_anchor`: grouping/diagnostic anchor, not automatically a root cause.
- `entered-sample` / `left-sample`: bounded-sample statements only.

## Invariants to preserve

Changes must preserve these existing qclose behaviors unless the task explicitly changes the schema:

- detailed point delays reconcile with Quartus data delay/slack within the existing tolerance;
- cell/local-interconnect/fabric-routing delay remain separated;
- source provenance remains attached to parsed report records;
- final and intermediate snapshots are not mixed;
- stage-dependent diagnostics report `unavailable-for-stage` instead of pretending to be empty/clean;
- serious constraint/data-quality findings can suppress RTL advice;
- source fingerprint, snapshot, clock, corner, and sampling settings remain available for run comparison;
- deterministic outputs remain deterministic.

If a schema changes, update tests and migration/compatibility handling in the same change.

## Repository map

- `quartus_timing_analyze.py`
  - CLI orchestration
  - normalization
  - summary generation
  - advice
  - comparison
- `quartus_timing_collect.tcl`
  - Timing Analyzer collection
- `quartus_report_collect.tcl`
  - Compilation Report Database collection
- `path_skeleton.py`
  - compact logical/physical skeleton for a collected detailed setup path
- `tests/`
  - unit and real-data regression coverage

## Development workflow

Before changing code:

1. Read the relevant CLI/help path and current tests.
2. Identify whether the requested behavior belongs in:
   - collection,
   - normalization,
   - correlation,
   - summary/advice,
   - comparison,
   - or path inspection.
3. Prefer the smallest layer that can implement the behavior without duplicating facts.
4. Do not parse a text report if an existing structured Report DB/Tcl source already provides the same fact with sufficient fidelity.
5. If text parsing is unavoidable, retain a `source`/origin field.

After changing code:

```sh
python3 -m unittest discover -s tests -v
```

For `path_skeleton.py` changes, also run:

```sh
python3 -m unittest tests.test_path_skeleton -v
```

For analyzer/schema changes, also run:

```sh
python3 -m unittest tests.test_quartus_timing_analyze -v
```

When a real Quartus fixture is relevant, validate against the smallest fixture that contains the affected report or snapshot.

## Timing-analysis semantics

Do not infer source-level meaning from a Quartus-generated node name alone.

Names such as these are fitted/synthesis identities, not guaranteed source expressions:

- `i12345`
- `LessThan_1`
- `add_0`
- `~RTM`
- `~RTMUX`
- `~Duplicate`
- `~DUPLICATE`

When mapping a fitted path to source logic:

1. obtain the detailed fitted path;
2. identify stable user-visible anchors;
3. inspect generated RTL if available;
4. map generated RTL dependency back to source RTL/Chisel/Scala;
5. state any remaining mapping as inference, not fact.

A path that crosses an apparent RTL register name after Hyper-Retiming does not prove a physical sequential boundary remains at that location.

## Snapshot discipline

Use the earliest existing snapshot that can answer the question:

- `planned`: logic/depth/coarse constraint questions;
- `placed`: placement/spread/locality;
- `routed`: actual routing delay and path geography;
- `retimed`: retiming movement/restrictions;
- `final`: sign-off, final Fmax, final report correlation.

Do not request a new full compile when an existing snapshot or prior qclose collection already answers the question.

## Advice discipline

qclose advice should be bounded and evidence-backed.

Good advice:

- inspect a named path family;
- collect a missing stage;
- verify a retiming restriction;
- reduce a measured high-fanout control cone;
- compare a single RTL experiment against a named baseline.

Bad advice:

- “add a pipeline register” without locating the dependency;
- “duplicate this register” when fitter duplication evidence already shows automatic replicas;
- “floorplan this module” without placement/routing evidence;
- “the problem is X” based only on hierarchy co-occurrence;
- declaring success/failure from one headline Fmax number without checking path migration.

## Change validation

For timing-related feature changes, tests should cover the evidence contract, not only formatting.

Examples:

- a path-family issue remains associated after a harmless fitted replica rename;
- `same-hierarchy` alone cannot become `root_cause_candidate`;
- a comparison rejects or clearly flags incompatible snapshots/source fingerprints;
- missing stage-specific diagnostics remain `unavailable-for-stage`;
- `path_skeleton.py --details` preserves original point index, cumulative delay, and fanout;
- `--routing` adds routing resources without duplicating the clock tree.

## Security and mutation boundary

qclose may execute Quartus read/report commands needed to inspect an existing project database.
Do not add hidden behavior that edits project design files, constraints, IP, or source code.

If a future feature proposes auto-fixes, it must be explicitly opt-in and produce a reviewable patch rather than silently editing a design.
