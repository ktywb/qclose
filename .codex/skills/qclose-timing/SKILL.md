---
name: qclose-timing
description: Use qclose for Intel Quartus timing-closure diagnosis and validation when a qclose collection exists or a completed Quartus database can be inspected. Trigger for WNS/Fmax regressions, critical path families, routed/retimed evidence, fitted-node-to-source mapping, or timing-experiment validation.
---

# qclose Timing Closure

Use qclose as an **evidence-first timing-closure workflow**. Do not jump from headline WNS/Fmax to an RTL, QSF, SDC, placement, or retiming change.

```text
existing evidence
  -> critical path family
  -> fitted dependency
  -> generated RTL / source mapping
  -> one attributable experiment
  -> structural validation
  -> final timing
```


Before running qclose commands, resolve its repository root once:

   ```sh
   QCLOSE=scripts/qclose   # project integration
   # QCLOSE=.              # inside qclose
   ```

## Procedure

1. **Reuse existing evidence first.** Prefer an existing `logs/timing-analysis/<run>/` collection. Do not recompile only to reproduce evidence already available.

2. **Read health before timing-driven design advice.** If health is blocked by constraint or data-quality findings, do not propose structural RTL/QSF/SDC/placement changes until the finding is resolved or explicitly waived. Missing stage-specific evidence is not a clean result.

3. **Check comparability before attributing a regression or improvement.** Confirm source fingerprint, snapshot, requested clock, corner, period, sampling scope, and relevant Quartus/project settings. Different source fingerprints prove different source states, not which source change caused the delta.

4. **Use the earliest snapshot that answers the question.**

   ```text
   planned  -> logic/depth/coarse constraint evidence
   placed   -> placement/spread/locality
   routed   -> actual route delay/geography
   retimed  -> Hyper-Retiming movement/restrictions
   final    -> sign-off/Fmax/final report correlation
   ```

5. **Analyze a path family, not only rank #1.** Use repeated launch/capture families, hierarchy, operator family, issue anchors, and delay character. A single worst path can be seed-specific.

6. **Inspect the fitted path.** 

   Start with logical/technology detail:

   ```sh
   python3 "$QCLOSE/path_skeleton.py" \
     logs/timing-analysis/<run> \
     --rank <N> \
     --details
   ```

   Add routing only when route delay or locality is part of the question:

   ```sh
   python3 "$QCLOSE/path_skeleton.py" \
     logs/timing-analysis/<run> \
     --rank <N> \
     --details \
     --routing
   ```

7. **Map fitted evidence to function before changing RTL.** Treat `iNNNN`, `LessThan_*`, `add_*`, `~RTM`, `~RTMUX`, and `~Duplicate` as fitted/synthesis identities, not source expressions. For Chisel/CIRCT, use generated SystemVerilog as the bridge to Scala. Read `references/chisel-quartus-mapping.md` when needed.

8. **Classify the measured bottleneck and choose one experiment.** Read only the relevant sections of `references/workflow.md` for the observed problem: delay character, automatic duplication, control recurrence, look-ahead, routing/locality, experiment interpretation, multi-seed, DSE, or floorplanning.

9. **Validate topology before headline Fmax.** Verify that the intended path family disappeared, shortened, or changed topology as predicted; identify the replacement family; compare delay character; then interpret final WNS/Fmax.

## Common qclose commands

```sh
python3 "$QCLOSE/quartus_timing_analyze.py" summarize \
  logs/timing-analysis/<run>

python3 "$QCLOSE/quartus_timing_analyze.py" advise \
  logs/timing-analysis/<run>

python3 "$QCLOSE/quartus_timing_analyze.py" compare-latest \
  --output-root logs/timing-analysis
```

Collect an existing Quartus snapshot only when required evidence has not already been collected:

```sh
python3 "$QCLOSE/quartus_timing_analyze.py" collect \
  --project <project> \
  --project-root <project-root> \
  --quartus-bin <quartus-bin> \
  --snapshot <planned|placed|routed|retimed|final>
```

## Hard rules

- Keep direct post-fit/report evidence, generated-RTL evidence, source-level evidence, qclose correlation, and engineering inference distinct. Do not present inference as direct evidence.
- `issue_anchor` is a grouping/diagnostic anchor, not proof of root cause. `same-hierarchy` correlation alone cannot establish root cause.
- Do not map an opaque fitted node to source logic from its generated name alone.
- Do not treat an RTL `Reg` as a guaranteed post-fit timing boundary when retiming evidence is present.
- Before manual register duplication, check accepted fitter/synthesis duplicates and require evidence for a distinct consumer-local partition.
- `entered-sample` and `left-sample` describe the bounded sample, not global appearance/disappearance.
- Do not attribute a timing delta to one RTL change when source states or relevant settings are uncontrolled.
- Make one attributable timing experiment at a time.
- Do not declare success or failure from Fmax alone.

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

Evaluate three separate outcomes:

```text
functional validation:
  required tests/properties pass and the functional invariant is preserved

structural validation:
  intended path family/topology changed as predicted

retention/sign-off:
  final timing is neutral or better in an appropriate controlled comparison
```

Treat these outcomes independently. A structurally validated experiment may
still be rejected for integration if it fails functional validation or if a
replacement bottleneck, placement effect, or seed-stable timing regression
makes overall timing worse.

## Output contract

```text
Baseline
  run / snapshot / clock / WNS / Fmax when available

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
- the suspected root cause has only weak hierarchy correlation;
- required evidence exists in another snapshot but has not been inspected;
- the previous experiment has not been structurally validated;
- Quartus already performs the proposed optimization and no evidence shows manual control is needed.

Collect or inspect the missing evidence instead.

## References

Read references selectively:

- `references/workflow.md` — deep timing diagnosis: evidence strength, path families, delay character, duplication, recurrence/control cones, look-ahead, routing/locality, experiment interpretation, multi-seed, DSE, and floorplanning.
- `references/chisel-quartus-mapping.md` — fitted node -> generated SystemVerilog -> Chisel/Scala mapping, including retiming and replication markers.
