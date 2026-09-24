# qclose Deep Timing Analysis Reference

This is the **conditional FPGA timing methodology** behind `qclose-timing`.

The mandatory execution order, health gate, snapshot-selection table, commands, experiment/output contracts, and stop conditions are in `../SKILL.md`. Do not duplicate them here. Read only the sections needed for the active bottleneck.

## 1. Evidence confidence

Keep physical facts, logical dependencies, correlations, and hypotheses separate.

### Tier A — direct post-fit / Quartus evidence

Examples: exact launch/capture nodes, detailed path points, incremental/cumulative delay, logic levels, fitted cell/pin identities, routing resources, path-local fanout, Fitter Duplication Summary, retiming restrictions, physical spread/location reports.

Tier A supports strong claims about the implemented design.

### Tier B — generated RTL dependency

Generated Verilog/SystemVerilog supports claims about the functional dependency Quartus received after front-end lowering. For Chisel/CIRCT it is usually the best bridge between fitted nodes and Scala.

### Tier C — source RTL / Chisel / Scala

Source establishes intended architecture and semantics. It does not establish post-fit placement, routing, replication, or retiming.

### Tier D — qclose correlation

Interpret match strength conservatively:

```text
exact-node / validated replica base-node -> strong
normalized-node                           -> medium
same-hierarchy                            -> contextual only
```

`issue_anchor` and `secondary_anchors` organize evidence; they are not automatically root causes.

### Tier E — engineering inference

Use inference to choose the next experiment, but label it as inference until stronger evidence exists.

## 2. Path-family reasoning

Do not optimize only the single worst row. A useful family is a repeated physical/functional dependency that survives incidental differences such as PE/bank index, bit index, fitter duplicate suffix, small destination variation, path rank, or timing corner.

Useful grouping dimensions:

```text
launch base register
capture base register
module / PE / bank role
generated operator family
issue anchor / secondary anchors
logic depth
route-vs-cell character
retiming / duplication markers
```

Strong evidence:

```text
same feedback topology across several banks
same forwarding/control cone across several PEs
several paths launching from replicas of the same base register
```

Weak evidence:

```text
same hierarchy only
one similarly named synthesized operator
```

Repeated families support architectural conclusions better than a single seed-local path.

## 3. Delay interpretation

Decompose the path before choosing an optimization:

```text
data delay
logic levels
cell delay
local-interconnect delay
fabric-routing delay
launch-to-first-logic route
largest routing hops
fanout at actual path points
clock skew
```

### Logic-dominated

Many logic levels, substantial cell delay, no unusually long routes. Prefer topology reduction, predecode, look-ahead, or arithmetic restructuring.

### Routing-dominated

High route fraction, long/repeated H/V resources, physical spread consistent with the path, modest logic depth. Prefer localization or consumer partitioning before adding logic.

### Mixed deep-and-routed cone

Many levels plus many medium routing hops. This is common in state/control recurrences: each logic level creates another placement/routing boundary. Architectural shortening is usually more promising than shaving one LUT or one first-hop route.

### First-hop sanity check

If the whole path is about 2.8 ns but launch-to-first-logic routing is about 0.28 ns, source localization alone cannot remove most of the path. A manual launch-register copy is unlikely to be the primary fix unless it also changes downstream topology.

## 4. Bottleneck classes

Classify from measured post-fit evidence, not RTL appearance.

### 4.1 Source-fanout dominated

Evidence:

- high launch fanout;
- disproportionate first-hop routing;
- shallow downstream logic;
- separable physical consumer regions;
- fitter replication absent or ineffective.

Possible experiment: consumer-local register replication.

Before doing it, inspect automatic duplication. A logical duplicate that still serves multiple distant regions is not local.

### 4.2 Deep control cone

Evidence:

- branch/mux/reduction chain;
- high logic depth;
- several medium routing hops;
- source replication does not remove the dependency;
- path represents a state/hazard decision.

Possible experiments:

```text
predecode
look-ahead
precompute next-cycle control
narrow-control extraction
compute candidates before selection
```

### 4.3 Wide-select / control-fanout dominated

Evidence:

- narrow control drives many wide mux bits;
- control net spans a large datapath;
- select routing/fanout dominates payload computation.

Possible experiments:

```text
separate narrow recurrence/control from wide payload
localize control copies to real consumer regions
move candidate computation ahead of a wide select
```

### 4.4 Routing/locality dominated

Evidence:

- high routing fraction;
- repeated long H/V resources;
- spread reports agree with routed paths;
- similar long routes recur across seeds or instances.

Prefer architectural localization, bank/consumer ownership changes, or interface-stage boundaries before hard floorplanning.

## 5. Automatic duplication

Before manual RTL replication, inspect fitter/synthesis duplication.

If Quartus already accepted several physical duplicates:

- fanout reduction is already occurring;
- a new logical copy must serve a demonstrably different consumer partition;
- expected benefit must exceed what Quartus already does;
- even trivial extra state can perturb placement.

A launch from:

```text
fooReg[...]~Duplicate_*
```

is direct evidence of physical replication.

Manual duplication is justified only when existing replicas do not align with the desired consumer-local ownership.

## 6. Control/data separation

High-frequency FPGA designs often benefit from separating:

```text
narrow state/control recurrence
```

from:

```text
wide payload/datapath movement
```

A narrow state becomes problematic when it simultaneously owns a tight recurrence and geographically dispersed wide consumers.

A valid split should create meaningful physical ownership, for example:

```text
recurrence/control
payload/address/select
```

while preserving semantics. Do not duplicate control merely to reduce a reported fanout number.

## 7. Look-ahead and compute-before-select

A common critical topology is:

```text
state
  -> select
  -> arithmetic
  -> compare/classify
  -> next control
```

When candidate states are few and inputs are available, consider:

```text
candidate A -> arithmetic/classify --\
candidate B -> arithmetic/classify ----> select narrow result -> register
candidate C -> arithmetic/classify --/
```

This changes:

```text
select -> compute
```

to:

```text
compute candidates in parallel -> select narrow result
```

Potential benefits:

- arithmetic/comparison leaves the select-controlled path;
- late mux width shrinks;
- independent candidate logic gains placement freedom;
- II=1 and latency can remain unchanged when precomputation occurs in the existing producer stage.

Risks:

- duplicated arithmetic costs area;
- an upstream candidate may create a new recurrence;
- extra logic can worsen placement;
- retiming can move the apparent boundary again.

Validate the replacement path family, not only Fmax.

## 8. Hyper-Retiming implications

An RTL-visible register is not guaranteed to remain the post-fit timing boundary at the same location.

Consequences:

- source-stage diagrams are insufficient for post-fit diagnosis;
- `~RTM`, `~RTMUX`, `combout`, Hyper-Register-related nodes, or a path crossing source-register names require fitted interpretation;
- adding another RTL register is not automatically equivalent to creating a new physical boundary;
- reset/enable/clock-control/semantic constraints can limit retiming freedom.

For exact fitted -> generated-SV -> Chisel mapping, use `chisel-quartus-mapping.md`.

## 9. Single-variable timing experiments

The objective is causality, not merely a faster compile.

Define:

```text
baseline run
target path family
measured physical problem
one RTL/QSF/SDC/placement change
functional invariant
expected old topology
expected replacement topology
validation snapshot
success criteria
```

Good experiments:

```text
separate one measured consumer class
precompute one late comparator/control result
replace one select-before-compute cone
remove one measured recurrence level
```

Poor experiments:

```text
edit unrelated modules together
change RTL and floorplan together
change global synthesis settings and RTL together
add several speculative register copies
```

## 10. Interpreting outcomes

### Fmax worse, intended old path gone

Do not immediately reject the structural idea. Inspect the replacement family, whether it is a consequence of the change, global delay character, placement perturbation, and seed sensitivity. This can be normal bottleneck migration.

### Fmax better, intended old path unchanged

Do not claim the intended optimization worked. Improvement may be placement/seed variation.

### Old path shorter, new direct bypass appears

The original hypothesis may be correct but incomplete. The new fitted dependency is now stronger evidence than the old theory.

### Path changed as predicted, final delta is small

This is the point where controlled multi-seed becomes useful.

## 11. Multi-seed methodology

Use multiple controlled seeds only after the intended topology change is verified and placement sensitivity plausibly dominates the remaining delta.

Keep RTL, constraints, device, Quartus version, and relevant project settings fixed.

Compare more than the single best seed:

```text
median Fmax / WNS
spread
worst seed
best seed
dominant path family
```

Do not spend multi-seed compile time before structural validation.

## 12. DSE

Use DSE only when:

- constraints are healthy;
- RTL/constraints are stable;
- no high-confidence structural bottleneck remains;
- timing is close enough that search/placement variation is plausibly the limiter.

DSE is not a substitute for understanding a repeated recurrence, deep-control cone, or routing-locality problem. DSE eligibility is an optimization-search recommendation, not root-cause evidence.

## 13. Floorplanning escalation

Escalate to placement constraints only after repeated routed evidence shows a stable locality problem.

Prerequisites:

- the same locality family repeats;
- placed/routed evidence agrees;
- the functional architecture already has sensible locality;
- the proposed region matches resource needs and FPGA macro-architecture;
- the constraint is unlikely merely to move the bottleneck.

Prefer:

```text
module/consumer-aware locality
soft or coarse regional guidance
resource-aware boundaries
```

before fine-grained hard placement. Over-constraining can reduce fitter freedom and repeatability.

## 14. FPGA architecture principles

Apply these only when measured evidence supports them.

### Shorten dependencies, not just source nets

A path with many logic levels and many medium routes is usually improved more by removing dependency levels than by shaving a small first-hop route.

### Narrow late control

Late 1-bit/small control decisions are easier to place and route than late wide-state selection. Prefer early wide computation and late narrow selection when semantics and area allow.

### Preserve physical freedom

Extra pipeline stages, `dont_touch`, preserve directives, forced duplicates, and hard regions can reduce fitter freedom. Add them only to solve a measured problem.

### Respect device structure

Agilex timing depends on LAB/ALM locality, sector structure, embedded-memory/DSP placement, and Hyper-Registers. Architectural boundaries reflecting those structures are generally more robust than arbitrary logical decomposition.

### Validate in integrated context

A micro-block can close at high frequency yet fail after replication/integration because routing distance and placement pressure change. Validate the relevant full design.

## 15. Claim language

Match wording to evidence strength.

Direct evidence:

```text
"The routed path contains..."
"Quartus accepted four duplicates..."
"The generated SV dependency is..."
```

Supported interpretation:

```text
"This functional cone is strongly supported by the fitted anchors and generated RTL."
```

Inference:

```text
"This placement explanation is likely..."
"This may be the next recurrence exposed after the change..."
```

Avoid claims such as:

```text
"This synthesized iNNNN node is definitely Scala expression X."
```

unless direct cross-probe or generated-netlist evidence proves it.
