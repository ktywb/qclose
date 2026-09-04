# qclose

`qclose` collects structured Quartus timing and compilation-report data, builds
compact Markdown/JSON summaries, and compares timing-closure runs.

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
