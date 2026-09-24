---
name: qclose-timing
description: Diagnose Intel Quartus timing closure with qclose when a qclose run or completed Quartus database is available. Use for WNS/Fmax regressions, critical path families, routed/retimed evidence, fitted-node-to-source mapping, and validation of a timing experiment.
---

# qclose Timing Closure

Use qclose as an **evidence-first timing-closure workflow**. Do not jump from headline WNS/Fmax to an RTL, QSF, SDC, placement, or retiming change.

Core loop:

```text
existing evidence
  -> path family
  -> fitted dependency
  -> generated RTL / source mapping
  -> one attributable experiment
  -> structural validation
  -> final WNS/Fmax
```

## Procedure

1. **Reuse existing evidence first.**
   Prefer an existing `logs/timing-analysis/<run>/` collection. Do not recompile only to reproduce information already present.

2. **Read health before RTL advice.**
   If health is blocked by constraint or data-quality findings, stop structural timing advice until the issue is resolved or explicitly waived. Missing stage-specific evidence is not a clean result.

3. **Check comparability before attributing a regression/improvement.**
   Confirm source fingerprint, snapshot, requested clock, corner, period, and relevant sampling/settings. Different source fingerprints prove different source states, not which source change caused the delta.

4. **Use the earliest snapshot that answers the question.**

   ```text
   planned  -> logic/depth/coarse constraints
   placed   -> placement/spread/locality
   routed   -> actual route-delay/geography
   retimed  -> Hyper-Retiming movement/restrictions
   final    -> sign-off/Fmax/final report correlation
   ```

5. **Analyze a path family, not only rank #1.**
   Use `summary.md` / `summary.json`, issue anchors, repeated launch/capture families, hierarchy, operator family, and delay character to identify repeated bottlenecks.

6. **Inspect the fitted path.**
   Start without routing detail:

   ```sh
   python3 path_skeleton.py logs/timing-analysis/<run>      --rank <N> --details
   ```

   Add physical routing only when locality/routing is part of the question:

   ```sh
   python3 path_skeleton.py logs/timing-analysis/<run>      --rank <N> --details --routing
   ```

7. **Map fitted evidence to function before changing RTL.**
   Treat Quartus names such as `iNNNN`, `LessThan_*`, `add_*`, `~RTM`, `~RTMUX`, and `~Duplicate` as fitted/synthesis identities, not source expressions. For Chisel/CIRCT, use generated SystemVerilog as the bridge to Scala. Read `references/chisel-quartus-mapping.md` when this mapping is needed.

8. **Classify the measured bottleneck and choose one experiment.**
   Use `references/workflow.md` for delay interpretation, automatic duplication, deep-control cones, control/data separation, look-ahead, routing/locality, multi-seed decisions, and floorplanning escalation.

9. **Validate topology before headline Fmax.**
   After a change, confirm that the intended old path family disappeared/shortened or changed topology, identify the replacement family, compare delay character, and only then interpret final WNS/Fmax. Use controlled multi-seed only after the structural change is verified and the remaining delta is small or route-sensitive.

## Common qclose commands

Inside qclose:

```sh
python3 quartus_timing_analyze.py summarize logs/timing-analysis/<run>
python3 quartus_timing_analyze.py advise logs/timing-analysis/<run>
python3 quartus_timing_analyze.py compare-latest   --output-root logs/timing-analysis
```

When qclose is vendored/submoduled under a project, the scripts are commonly:

```text
scripts/qclose/quartus_timing_analyze.py
scripts/qclose/path_skeleton.py
```

Collect an existing Quartus snapshot only when the required evidence is not already collected:

```sh
python3 quartus_timing_analyze.py collect   --project <project>   --project-root <project-root>   --quartus-bin <quartus-bin>   --snapshot <planned|placed|routed|retimed|final>
```

## Hard rules

- Do not claim a source-level mapping from an opaque fitted node name alone.
- Do not treat an RTL `Reg` as a guaranteed post-fit sequential boundary when retiming evidence is present.
- Before recommending manual register duplication, check whether Quartus already accepted fitter/synthesis duplicates and whether a distinct consumer-local partition is actually justified.
- Do not infer global appearance/disappearance from `entered-sample` or `left-sample`; they refer to the bounded sample.
- Do not attribute a timing delta to one RTL change when the compared source states or settings are not controlled.
- Do not make several unrelated timing changes in one experiment.
- Do not declare an experiment successful or failed from Fmax alone.

## Experiment contract

Before editing, record:

```text
baseline:
target path family:
measured problem:
single change:
unchanged:
expected old topology:
expected new topology:
functional invariant:
validation snapshot:
success criteria:
```

A useful success criterion normally includes both:

```text
structural: intended path family/topology changed as predicted
sign-off:   final timing is neutral or better across an appropriate controlled comparison
```

## Output contract

For a timing diagnosis, report:

```text
Baseline
  run / snapshot / clock / WNS / Fmax

Critical family
  launch -> functional cone -> capture
  repetition/count/ranks when known

Measured character
  logic levels
  route/cell/local-interconnect contribution
  fanout/spread/retiming evidence

Source mapping
  fitted anchors
  generated RTL dependency
  source dependency
  confidence / remaining uncertainty

Diagnosis
  proven facts
  strongly supported interpretation
  inference
  hypotheses ruled out

Next experiment
  one exact change
  expected path-topology effect
  functional invariant

Validation
  functional tests
  qclose snapshot
  path-family success criteria
  final timing criteria
```

## Stop conditions

Do not propose a timing-driven RTL/QSF/SDC/placement change yet when:

- health is blocked;
- the suspected root cause is supported only by weak hierarchy correlation;
- the required evidence exists in another snapshot but has not been inspected;
- the previous experiment has not been structurally validated;
- Quartus is already performing the proposed optimization and there is no evidence that manual control is needed.

Collect or inspect the missing evidence instead.

## References

Read these only when the active task needs them:

- `references/workflow.md` — evidence tiers, path-family reasoning, bottleneck classification, look-ahead/control-data patterns, experiment interpretation, multi-seed and floorplanning escalation.
- `references/chisel-quartus-mapping.md` — fitted-node -> generated SystemVerilog -> Chisel/Scala mapping, retiming and replication markers.
