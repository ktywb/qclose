# Chisel/CIRCT -> Quartus Mapping Reference

## Purpose

Use this reference when a Quartus fitted path contains opaque synthesized nodes but the source design is Chisel/Scala.

## Mapping chain

```text
Scala/Chisel
  -> FIRRTL/CIRCT lowering
  -> generated SystemVerilog
  -> Quartus synthesis
  -> Fitter / Hyper-Retiming / duplication
  -> STA fitted path
```

The reverse investigation must walk this chain carefully.

## Reliable anchors

Usually useful:

- named Chisel registers that survive lowering;
- module hierarchy;
- RAM instance/port names;
- obvious bus/register names;
- generated SV assignments;
- source-line comments emitted by CIRCT when present.

Less reliable:

- synthesized `iNNNN`;
- auto-numbered operators;
- optimized mux/reduction names;
- fitted replica suffixes.

## Procedure

1. From `path_skeleton --details`, record:
   - launch;
   - capture;
   - stable intermediate source names;
   - cumulative delay and fanout.

2. In generated SV, find the stable anchors.

3. Trace the assignment/update dependency between those anchors.

4. Search the Chisel source for the corresponding named wires/registers and the expressions that define them.

5. Compare topology, not superficial names.

6. If Hyper-Retiming is present, do not assume the source `Reg` still cuts the fitted path.

## Example interpretation

Generated SV:

```verilog
wire [7:0] totalBytes =
  {1'b0, opFillReg} + {1'b0, opInputValidBytesReg};
wire totalBelowFull = totalBytes < 8'h40;

lastCommitHit =
  lastCommitValidReg &&
  lastCommitHashReg == rawRspHeadBitsReg_hash;

scoredMeta_fill =
  lastCommitHit ? lastCommitMetaReg_fill
                : rawRspHeadBitsReg_meta_fill;
```

Fitted path:

```text
lastCommitHashReg
 -> opaque synthesized cells
 -> opFillReg
 -> add_0
 -> opInputValidBytesReg
 -> LessThan_1
```

Supported conclusion:

```text
the critical functional cone connects last-commit forwarding,
fill-state selection, fill+input-byte arithmetic, and the full/underfull comparison.
```

Unsupported conclusion without extra cross-probe:

```text
i13310 is exactly the `lastCommitHit` equality LUT.
```

## Retiming markers

Treat these as evidence that physical boundaries differ from the source topology:

```text
~RTM
~RTMUX
..._dff
Hyper-Register-specific fitted nodes
```

Inspect the actual fitted cell/pin types.

## Replication markers

These suggest automated replication:

```text
~Duplicate
~DUPLICATE
```

Confirm with Fitter Duplication Summary before adding an RTL copy.

## Confidence language

Use:

```text
"exactly maps to"
```

only with direct generated-RTL/cross-probe evidence.

Use:

```text
"belongs to the functional cone"
```

when the opaque fitted cells are bracketed by proven anchors.

Use:

```text
"likely"
```

for remaining architectural/placement inference.
