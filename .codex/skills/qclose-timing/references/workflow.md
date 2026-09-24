# qclose Agent Timing-Closure Workflow

This document is the deep reference behind the `qclose-timing` skill.

## 1. Goal

The agent's job is not to produce generic timing suggestions. It is to reduce uncertainty around a measured Quartus timing failure until one narrowly-scoped experiment is justified.

The central loop is:

```text
measurement
  -> path family
  -> fitted dependency
  -> generated RTL
  -> source architecture
  -> one modification
  -> structural validation
  -> sign-off comparison
```

## 2. Evidence contract

### Tier A - direct post-fit evidence

Examples:

- exact setup path;
- incremental and cumulative point delay;
- fitted node and pin identities;
- routing resources;
- fanout on the actual path;
- accepted fitter duplication;
- retiming restriction/report.

These support strong physical claims.

### Tier B - generated RTL

Generated Verilog/SystemVerilog supports functional dependency claims after front-end lowering.

It is especially important for Chisel/CIRCT designs because Quartus sees generated RTL rather than Scala directly.

### Tier C - source RTL / Chisel / Scala

Source code explains architecture and intended semantics.

It does not by itself prove how Quartus mapped or retimed the logic.

### Tier D - qclose correlation

Examples:

- high-fanout association;
- register spread;
- route-pressure association;
- issue anchor;
- secondary anchors.

These help prioritize investigation but must respect match confidence.

### Tier E - architectural inference

Examples:

- “this copy is probably physically pulled toward two consumer regions”;
- “this mux select is likely the next bottleneck”.

Useful, but label as inference until A-C support it.

## 3. Run compatibility checklist

Before comparing two runs, establish:

```text
same target design intent?
same target clock?
same snapshot?
same corner interpretation?
same source fingerprint or intentionally changed source?
same collection/sampling scope?
same major Quartus settings?
```

If source fingerprint differs and the exact source diff is unknown:

```text
"timing changed between two different source states"
```

is valid.

```text
"change X caused all of the delta"
```

is not yet valid.

## 4. Health gate

A timing diagnosis is subordinate to constraint correctness.

Blocked examples:

- unconstrained endpoints;
- invalid/missing generated clocks;
- suspicious clock relationships;
- data-quality/collection inconsistency.

An agent should not “optimize” RTL around a constraint bug.

## 5. Snapshot selection

### planned

Use for:

- coarse logic depth;
- obvious combinational topology;
- constraint sanity that is already visible.

Do not use for final route claims.

### placed

Use for:

- register/hierarchy spread;
- whether replicated logic is geographically localized;
- large placement separation.

### routed

Use for:

- actual route delay;
- H/V routing-resource chains;
- long interconnect;
- path-level route-vs-cell breakdown.

### retimed

Use for:

- Hyper-Retiming behavior;
- moved register boundaries;
- retiming restrictions.

### final

Use for:

- final sign-off;
- final Fmax;
- final Report DB panels;
- final comparison after the structural experiment is understood.

## 6. Path-family method

Do not optimize rank #1 in isolation.

Group by repeated topology. Useful grouping keys include:

```text
launch base register
capture base register
hierarchy
operator family
issue anchor
secondary anchors
route/cell character
```

Strong family evidence:

```text
same feedback recurrence appears in many PEs/banks
```

Weak family evidence:

```text
several failures merely share the same module hierarchy
```

## 7. `path_skeleton.py`

Start with:

```sh
python3 path_skeleton.py RUN --rank N --details
```

Use `--routing` only if physical geography is part of the question.

Read the sequence as fitted logic, not source syntax.

Important names:

```text
~Duplicate / ~DUPLICATE
```

Usually indicate fitter/synthesis replication. Inspect duplication reports before manually duplicating the RTL register.

```text
~RTM / ~RTMUX
```

Indicate retiming/mux structures. The source register boundary may have moved.

```text
iNNNN
```

Opaque synthesized cell identity. Do not guess the Scala expression from the number.

```text
LessThan_N / add_N / reduce_*
```

Generated operator families. Use generated RTL/cross-probe to map them.

## 8. Mapping a fitted path in a Chisel design

Suppose the path skeleton is:

```text
lastCommitHashReg
 -> i13310
 -> i13363
 -> i13365
 -> opFillReg
 -> add_0
 -> opInputValidBytesReg
 -> LessThan_1
```

The correct process is:

1. identify stable names: `lastCommitHashReg`, `opFillReg`, `opInputValidBytesReg`;
2. inspect generated SV declarations and assignments;
3. find the only source dependency that links those anchors;
4. verify the end operator in generated SV;
5. map back to Scala;
6. leave opaque fitted cells grouped inside the proven functional cone unless exact cross-probe is available.

Do not force an unsupported one-to-one mapping such as:

```text
i13310 == scoredMeta mux
```

unless Quartus or generated-netlist evidence proves it.

## 9. Bottleneck classification

Classify from measured post-fit evidence, not from RTL appearance alone.

### Source-fanout dominated

Typical evidence:

- large launch fanout;
- disproportionate first-hop routing;
- shallow downstream logic;
- fitter duplication absent or ineffective.

Before proposing an RTL duplicate, inspect accepted Quartus fitter
duplication and require evidence for a distinct consumer-local region.

### Deep control cone

Typical evidence:

- many logic levels;
- mux/reduction/branch dependency;
- several medium routing hops;
- source replication does not materially shorten the path.

Prefer architectural shortening such as predecode, look-ahead,
or compute-candidates-before-select when semantics permit.

### Wide-select/control-fanout dominated

Typical evidence:

- a narrow control result fans into many wide mux/select destinations;
- select routing dominates more than payload computation.

Consider separating narrow control recurrence from wide payload movement.

### Routing/locality dominated

Typical evidence:

- high routing fraction;
- repeated long H/V resources;
- register/hierarchy spread agrees with routed paths.

Attempt architectural localization before hard floorplanning.

## 10. Delay interpretation

Look at where the time is spent.

Example:

```text
total data delay 2.80 ns
route          1.73 ns
logic cell     0.88 ns
levels         9
```

This is neither “just routing” nor “just logic”.

It is a deep dependency with enough placement/routing exposure that architectural shortening is usually more promising than micro-tuning one LUT.

If the first source-to-first-LUT hop is only ~0.28 ns while the path is 2.8 ns, source register duplication cannot plausibly remove the whole problem.

## 11. Quartus automatic duplication

Before recommending an RTL copy, inspect fitter duplication.

If Quartus already accepted several physical duplicates of the source register:

- automatic fanout reduction is already active;
- a new logical copy must justify a distinct consumer partition;
- otherwise it can perturb placement without shortening the dependency.

Manual copies make sense only when there is evidence for consumer-local ownership that fitter is not achieving.

## 12. Control/data separation

A useful high-frequency pattern is:

```text
narrow control recurrence
wide payload datapath
```

Do not make a narrow control register drive both:

- a feedback recurrence; and
- a geographically large wide datapath

if the two consumers can be safely separated.

But duplication must correspond to actual physical consumer regions. A duplicate that still drives two distant regions is not local.

## 13. Look-ahead transformation

For a path:

```text
state
 -> select
 -> arithmetic
 -> compare
 -> next control register
```

consider whether semantics allow:

```text
candidate A arithmetic/compare
candidate B arithmetic/compare
candidate C arithmetic/compare
 -> select narrow precomputed result
 -> register
```

This converts:

```text
select -> compute
```

into:

```text
compute in parallel -> select narrow result
```

It can shorten a control path without adding throughput latency.

Validate that the new candidate computations do not create a worse path from an upstream recurrence.

## 14. Single-variable experiment record

Before editing, write:

```text
Experiment:
  baseline:
  target path family:
  measured problem:
  change:
  unchanged:
  expected old topology:
  expected new topology:
  functional invariant:
  validation snapshot:
  success criteria:
```

Example success criteria:

```text
- old launch->capture family is absent from top sampled failures;
- new path terminates at the intended look-ahead register;
- no new equal-or-worse recurrence appears;
- route percentage does not regress materially;
- final multi-seed Fmax is neutral or better.
```

## 15. Failure interpretation

### Fmax worse, old path gone

Do not immediately call the idea wrong.

Check:

- new bottleneck family;
- global route/cell character;
- placement perturbation;
- seed sensitivity.

This can be bottleneck migration.

### Fmax better, old path unchanged

Do not call the intended structural optimization successful.

The improvement may be seed/placement luck.

### old path shorter but a new direct bypass appears

The transformation may be semantically correct but timing-architecturally incomplete.

Optimize the new proven path, not the old hypothesis.

## 16. When to use multi-seed

Use multiple controlled seeds when:

- structural validation is already positive;
- the final Fmax delta is small;
- route-dominated placement variation is large;
- deciding whether to retain a change.

Do not spend multi-seed compile time before proving that the intended topology changed.

## 17. Escalation to floorplanning

Only after repeated routed evidence shows a stable locality problem:

- repeated long routes;
- same hierarchy/consumer geometry;
- architectural localization has been exhausted or is impossible.

Prefer module/consumer-aware locality over arbitrary hard placement.

## 18. What the final analysis should say

A strong analysis clearly separates:

```text
Proven:
- direct timing/report/generated-RTL facts.

Strongly supported:
- functional-cone mapping from fitted anchors to generated RTL.

Inference:
- placement/locality explanation not directly proven.

Ruled out:
- hypotheses contradicted by fitter duplication, fanout, or path data.

Experiment:
- one exact change and one validation plan.
```
