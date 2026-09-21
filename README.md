# qclose

`qclose` collects structured Quartus timing and compilation-report data, builds
compact Markdown/JSON summaries, and compares timing-closure runs.

The schema-v5 analyzer also:

- checks that detailed point delays reproduce Quartus `data_delay` and slack;
- separates cell, local-interconnect, and fabric-routing delay;
- normalizes useful Report DB panels and text-only physical reports behind one
  record schema;
- correlates critical paths with high-fanout, register-spread, routing-pressure,
  and retiming evidence using confidence-ranked node identities;
- clusters issues around a conservative `issue_anchor`, retains alternate
  `secondary_anchors`, and labels only well-supported bottleneck anchors as a
  `root_cause_candidate`;
- reports separate timing-consistency, Quartus-breakdown, anchor-confidence,
  evidence-strength, and evidence-coverage components;
- emits a `health` preflight for constraints, CDC, Design Assistant, and data
  quality; serious constraint findings suppress RTL advice;
- normalizes existing Design Assistant, Fast Forward, and retiming guidance
  without launching those Quartus flows;
- generates deterministic `advice.json` and `advice.md`; official-report
  origin, issue-association method/confidence, and diagnosis confidence are
  separate fields;
- emits evidence-backed issue records and compares issue, delay-character,
  resource, routing, Fmax, Fast Forward, and physical metrics between runs.

The original `.rpt` files remain the source of truth when Quartus exposes no
equivalent collection or Report DB table.  Their parsed rows carry a `source`
field so downstream tools can distinguish Tcl objects, Report DB JSON, and text
reports.  `entered-sample`/`left-sample` in comparisons refer only to the bounded
path sample and must not be read as proof that a problem globally appeared or
disappeared.

`exact-node` and validated replica `base-node` matches are strong evidence;
bus-index-only `normalized-node` matches are medium evidence. A
`same-hierarchy` match is contextual and cannot trigger a root-cause diagnosis.

It consists of:

- `quartus_timing_analyze.py`: CLI orchestration, normalization, summaries, and
  comparisons.
- `quartus_timing_collect.tcl`: Timing Analyzer collection.
- `quartus_report_collect.tcl`: Compilation Report Database collection.

## Requirements

- Python 3.10 or newer.
- Intel Quartus Prime Pro with a completed project database appropriate for the
  requested reports.

No third-party Python packages are required.

The regression suite includes minimized, non-synthetic Quartus Prime Pro 25.1
/ Agilex 7 timing and five-snapshot fixtures under `tests/fixtures`.

## Usage

Collect and summarize a completed Quartus build:

```sh
python3 quartus_timing_analyze.py collect \
  --project vpart_pcie \
  --project-root /path/to/project \
  --quartus-bin /path/to/quartus/bin
```

Regenerate one run's summary:

```sh
python3 quartus_timing_analyze.py summarize logs/timing-analysis/<run>
```

Show the data-path skeletons behind the Worst setup paths table (or select its
1-based row number):

```sh
python3 path_skeleton.py logs/timing-analysis/<run>
python3 path_skeleton.py logs/timing-analysis/<run> --rank 6
python3 path_skeleton.py logs/timing-analysis/<run> --rank 6 --details
python3 path_skeleton.py logs/timing-analysis/<run> --rank 6 --routing
```

This reads the matching `summary.json` and `detailed_paths.jsonl` in the same
directory. It omits clock-tree and anonymous routing nodes, groups adjacent
technology cells with the same signal name, and retains RAM port names. Rows
without collected detailed points are reported explicitly; recollect with a
larger `--detailed-paths` value to inspect those rows. Node names identify
fitted logic, not guaranteed Scala source expressions. `--details` retains the
original cell/pin names, indices, cumulative delays, and fanout; `--routing`
also includes physical routing points. Both omit the launch clock tree.

Generate deterministic advice from an existing summary:

```sh
python3 quartus_timing_analyze.py advise logs/timing-analysis/<run>
```

DSE II is never marked eligible unless an engineer asserts that RTL and
constraints are stable. The closeness test uses `abs(WNS) / clock_period`, not
an absolute nanosecond threshold:

```sh
python3 quartus_timing_analyze.py advise logs/timing-analysis/<run> \
  --design-stable \
  --dse-slack-ratio-threshold 0.05
```

Analyze an already-existing intermediate Quartus database without running
Fitter or compilation:

```sh
python3 quartus_timing_analyze.py collect \
  --project vpart_pcie \
  --project-root /path/to/project \
  --quartus-bin /path/to/quartus/bin \
  --snapshot routed
```

Quartus Prime Pro 25.1 exposes `planned`, `placed`, `routed`, `retimed`, and
`final` snapshots. qclose does not mix final Compilation Report DB panels into
an intermediate-snapshot timing run. Stage-dependent diagnostics are reported
as `unavailable-for-stage`, not as clean/empty.

Compare the newest two collections:

```sh
python3 quartus_timing_analyze.py compare-latest \
  --output-root logs/timing-analysis
```

Run `python3 quartus_timing_analyze.py --help` for all commands and options.

Run all synthetic and real-data regression tests:

```sh
python3 -m unittest discover -s tests -v
```

## Timing Closure Workflow

1. Collect the earliest existing snapshot that can answer the current
   question. Use `planned` for coarse logic/constraint checks, `placed` for
   physical spread, `routed` for route-delay evidence, `retimed` for retiming
   limits, and `final` for sign-off comparison and Report DB correlation.
2. Read `health` first. A `blocked` result means constraint or data-quality
   findings must be resolved or explicitly waived before acting on RTL advice.
3. Use `summary.json` for evidence and `advice.md` for the bounded action list.
   `issue_anchor` is a grouping/diagnostic anchor, not automatically a proven
   root cause.
4. Validate an RTL change at the `validation_snapshot` attached to its action.
   Logic/depth uses `planned`, physical spread uses `placed`, route behavior
   uses `routed`, retiming uses `retimed`, and sign-off uses `final`.
   qclose intentionally does not invent compile commands or mutate RTL, QSF,
   or SDC files.
5. Compare runs only after confirming their source fingerprints, snapshot, and
   sampling settings. `entered-sample` and `left-sample` still describe the
   bounded sample, not the whole design.
6. Consider DSE II only when constraints and RTL are stable, timing is close,
   and no high-confidence structural issue remains.
