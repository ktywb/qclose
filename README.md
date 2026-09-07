# qclose

`qclose` collects structured Quartus timing and compilation-report data, builds
compact Markdown/JSON summaries, and compares timing-closure runs.

The schema-v3 analyzer also:

- checks that detailed point delays reproduce Quartus `data_delay` and slack;
- separates cell, local-interconnect, and fabric-routing delay;
- normalizes useful Report DB panels and text-only physical reports behind one
  record schema;
- correlates critical paths with high-fanout, register-spread, routing-pressure,
  and retiming evidence using confidence-ranked node identities;
- clusters issues by a repeated Quartus bottleneck/common path node, falling
  back to endpoint identity only when no reliable common node exists;
- reports separate timing-consistency, Quartus-breakdown, evidence-match, and
  root-grouping confidence components;
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

The regression suite includes a minimized, non-synthetic Quartus Prime Pro
25.1 / Agilex 7 fixture under `tests/fixtures/quartus_25_1_agilex7_real`.

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
