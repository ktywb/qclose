---
name: qclose-timing
description: Analyze Intel Quartus timing-closure runs with qclose. Use for setup-path/Fmax regressions, path-family/root-cause analysis, routed/retimed evidence, generated-RTL-to-source mapping, and validating a single RTL timing experiment.
---

# qclose Timing Closure Skill

Use this skill when the task is to understand or improve FPGA timing using a completed Quartus database or an existing qclose collection.

The objective is **evidence-first timing closure**, not generic RTL optimization.

## Core rule

Follow this loop:

**existing evidence -> fitted path -> functional dependency -> single-variable experiment -> path-family validation -> Fmax/sign-off**

Do not jump from a headline WNS/Fmax number directly to an RTL edit.

## 1. Discover qclose and existing evidence

Prefer existing collections before running Quartus again.

Typical repository-local qclose paths:

```sh
scripts/qclose/quartus_timing_analyze.py
scripts/qclose/path_skeleton.py
```

or, when working inside qclose itself:

```sh
quartus_timing_analyze.py
path_skeleton.py
```

Look for an existing run directory such as:

```text
logs/timing-analysis/<timestamp>/
```

Useful files commonly include:

```text
summary.md
summary.json
advice.md
advice.json
paths.jsonl
detailed_paths.jsonl
collection_metadata.json
comparison.md
panels/
*.rpt
```

If the evidence already exists, **do not recompile solely to reproduce it**.

## 2. Health gate first

Before giving RTL advice, inspect health/constraint/data-quality findings.

If qclose reports a blocked health state:

- stop structural RTL recommendations;
- resolve or explicitly waive the constraint/data-quality problem first;
- state that timing conclusions may be invalid until the health issue is resolved.

Do not treat missing stage-specific evidence as “clean”.

## 3. Confirm run comparability

Before claiming that a change improved or regressed timing, compare:

- source fingerprint;
- snapshot (`planned`, `placed`, `routed`, `retimed`, `final`);
- requested clock;
- corner;
- target period;
- path/detailed-path sampling settings;
- relevant Quartus/project settings if recorded.

If source fingerprints differ, that proves the sources differ, not which source change caused the timing delta.

`entered-sample` and `left-sample` describe only the bounded sample.

## 4. Pick the right snapshot

Use the earliest snapshot that can answer the current question:

```text
logic/depth/coarse constraints -> planned
placement/spread/locality      -> placed
route-delay/geography          -> routed
retiming movement/restriction  -> retimed
final Fmax/sign-off            -> final
```

Do not use final by reflex if a cheaper existing stage answers the question.

## 5. Identify a path family, not just one path

Read `summary.md` / `summary.json` and group related failures by:

- source register family;
- destination register family;
- hierarchy;
- generated operator;
- repeated fitted replicas;
- common issue anchor;
- delay character.

A single worst path can be seed-specific. A repeated path family is much stronger evidence.

Prefer statements like:

```text
"17 sampled paths share bankHeadRowOH -> endpoint-select -> consume feedback"
```

over:

```text
"rank #1 is bad"
```

## 6. Inspect the fitted path

For a row in the Worst Setup Paths table:

```sh
python3 path_skeleton.py logs/timing-analysis/<run> --rank <N> --details
```

Add physical routing only when needed:

```sh
python3 path_skeleton.py logs/timing-analysis/<run> \
  --rank <N> --details --routing
```

Interpretation:

- `--details`: fitted technology cells/pins, original point indices, cumulative delay, fanout;
- `--routing`: adds routing resources; use when placement/routing is part of the question;
- omit `--routing` initially when the task is to understand logical dependency.

Do not infer source semantics from `iNNNN`, `LessThan_*`, `add_*`, `~RTM`, `~RTMUX`, or `~Duplicate` names alone.

## 7. Build the evidence ladder

Use this confidence order:

### A. Post-fit path evidence
Actual launch/capture nodes, point sequence, delay, fanout, routing.

### B. Generated RTL dependency
The generated Verilog/SystemVerilog shows which expressions and register updates create the fitted cone.

### C. Source RTL / Chisel / Scala dependency
Map the generated dependency to the authoring source.

### D. qclose correlation
High-fanout, register spread, routing pressure, retiming restrictions, issue anchors.

### E. Engineering inference
Architecture/placement hypotheses.

Do not present E as A-C.

For Chisel/CIRCT projects, generated RTL is often the most useful bridge between Quartus fitted nodes and Scala.

## 8. Recognize Hyper-Retiming

A source register name appearing as `~RTM`, `~RTMUX`, or a combinational fitted node does not imply the original RTL register boundary remains physically where the source code places it.

If a critical path appears to pass “through” an RTL register:

- inspect `retimed` or final evidence;
- examine fitted node types (`combout`, `q`, `dff`, Hyper-Register naming);
- reason about the post-fit boundary, not only the source boundary.

Do not add another register until the actual retimed dependency is understood.

## 9. Diagnose the type of problem

Use measured delay distribution.

### Source-fanout dominated
Evidence:

- large launch fanout;
- large first-hop routing;
- fitter did not already create effective replicas;
- downstream logic is shallow.

Possible experiment:
- localized register replication.

But first check fitter duplication reports. If Quartus already generated multiple replicas, do not mechanically add an RTL duplicate.

### Deep control cone
Evidence:

- many logic levels;
- branch/mux/reduction chain;
- several medium routing hops;
- source replication does not remove the dependency.

Possible experiment:
- look-ahead;
- predecode;
- precompute next-cycle control;
- narrow-control extraction;
- restructure `select -> compute` into `compute candidates -> select narrow result`.

### Wide datapath / select fanout
Evidence:

- one control bit drives many wide mux bits;
- control routing/fanout dominates.

Possible experiment:
- separate narrow control and wide payload;
- register local control copies only when there are real physical consumer regions;
- move computation ahead of a wide mux if semantics allow.

### Routing/locality dominated
Evidence:

- high route percentage;
- long H/V resources;
- repeated cross-region paths;
- placement/spread reports agree.

Possible experiment:
- architectural localization first;
- floorplanning only after repeated routed evidence.

## 10. Prefer look-ahead over arbitrary pipelining

When the critical dependency is a state-feedback decision:

```text
state -> decision -> state update
```

prefer:

```text
precompute next-cycle decision -> register narrow decision -> consume next cycle
```

when semantics permit.

The goal is to cut the recurrence without adding avoidable wide-state movement.

Do not pipeline blindly: extra stages can worsen placement and may not improve a routing-dominated design.

## 11. One experiment at a time

A timing experiment must be attributable.

Do not simultaneously change:

- multiple unrelated modules;
- RTL plus floorplan;
- RTL plus global synthesis settings;
- several independent path families.

For each experiment record:

```text
baseline run
source change
expected path family to disappear/shorten
expected replacement path
validation snapshot
functional test required
```

## 12. Validate structure before headline Fmax

After the experiment:

1. verify source fingerprint changed as expected;
2. collect the appropriate snapshot;
3. confirm the old path family changed/disappeared;
4. inspect the new top path family;
5. compare route/cell/local-IC character;
6. only then interpret final WNS/Fmax.

A lower Fmax does not by itself prove the structural idea is wrong; placement seed variation exists.
A higher Fmax does not prove the intended path changed.

For close calls, use multiple controlled seeds.

## 13. Chisel/generated-RTL mapping

When generated RTL is available:

1. identify stable source registers/signals from the fitted path;
2. search generated SV for those names;
3. locate the next-state/update expression;
4. trace the combinational dependency to the fitted destination/operator;
5. map that expression back to Scala;
6. note which transformations are CIRCT/Quartus artifacts.

Do not claim `LessThan_1 == Scala line X` without generated-RTL or cross-probe evidence.

## 14. Output format for an agent analysis

Return:

```text
Baseline:
- run/snapshot/clock/WNS/Fmax

Critical family:
- launch -> functional cone -> capture
- count/ranks if known

Measured character:
- levels
- route %
- cell delay
- high-fanout/spread/retiming evidence

Source mapping:
- fitted node(s)
- generated RTL dependency
- source RTL dependency
- confidence

Diagnosis:
- what is proven
- what is inferred
- what is explicitly ruled out

Next experiment:
- exact local change
- why it targets this dependency
- expected old/new path topology

Validation:
- functional tests
- qclose snapshot
- path-family success criteria
- final Fmax criteria
```

## 15. Stop conditions

Do not propose an RTL edit when:

- health is blocked;
- the path cannot yet be mapped beyond a weak hierarchy correlation;
- the required evidence exists in another snapshot but has not been inspected;
- the previous experiment was not structurally validated;
- the suspected optimization is already being performed automatically by Quartus and no evidence shows manual control is needed.

Instead request or collect the missing evidence.

## References

Read these only when needed:

- `references/workflow.md` - detailed decision tree and evidence rules.
- `references/chisel-quartus-mapping.md` - fitted-node to generated-SV to Chisel mapping.
