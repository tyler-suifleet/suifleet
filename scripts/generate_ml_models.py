#!/usr/bin/env python3
"""Reproduce original SF-073 fixtures; generation only, never device dependencies.

Requires tflite==2.18.0, flatbuffers==25.2.10, numpy==1.26.4.
The graph uses only TensorFlow Lite schema-v3 operators available in v2.14.0.
Weights are analytical, not downloaded or trained on third-party data.
"""
import hashlib
import json
from pathlib import Path
import struct

import flatbuffers
import tflite as t

ROOT = Path(__file__).resolve().parents[1] / "interfaces/compat/ml"


def model(task):
    b = flatbuffers.Builder(2048)

    def vector(start, values, offsets=False):
        start(b, len(values))
        for value in reversed(values):
            (b.PrependUOffsetTRelative if offsets else b.PrependInt32)(value)
        return b.EndVector()

    buffers = []
    data = [b"", struct.pack("<32f", *([-1/16]*16 + [1/16]*16)),
            struct.pack("<2f", 1, 0)] if task == "classifier" else [
                b"", struct.pack("<4f", *([0.25]*4)), struct.pack("<f", 0)]
    for raw in data:
        value = b.CreateByteVector(raw)
        t.BufferStart(b)
        t.BufferAddData(b, value)
        buffers.append(t.BufferEnd(b))
    tensors = []
    shapes = [[1, 16], [2, 16], [2], [1, 2]] if task == "classifier" else [
        [1, 4, 4, 1], [1, 2, 2, 1], [1], [1, 2, 2, 1]]
    for index, shape in enumerate(shapes):
        name = b.CreateString(["pixels", "weights", "bias", "scores"][index])
        dims = vector(t.TensorStartShapeVector, shape)
        t.TensorStart(b)
        t.TensorAddShape(b, dims)
        t.TensorAddType(b, t.TensorType.FLOAT32)
        t.TensorAddBuffer(b, index if index in (1, 2) else 0)
        t.TensorAddName(b, name)
        tensors.append(t.TensorEnd(b))
    if task == "classifier":
        t.FullyConnectedOptionsStart(b)
        options = t.FullyConnectedOptionsEnd(b)
        builtin, option_type = t.BuiltinOperator.FULLY_CONNECTED, t.BuiltinOptions.FullyConnectedOptions
    else:
        t.Conv2DOptionsStart(b)
        t.Conv2DOptionsAddPadding(b, t.Padding.VALID)
        t.Conv2DOptionsAddStrideW(b, 2)
        t.Conv2DOptionsAddStrideH(b, 2)
        t.Conv2DOptionsAddDilationWFactor(b, 1)
        t.Conv2DOptionsAddDilationHFactor(b, 1)
        options = t.Conv2DOptionsEnd(b)
        builtin, option_type = t.BuiltinOperator.CONV_2D, t.BuiltinOptions.Conv2DOptions
    inputs = vector(t.OperatorStartInputsVector, [0, 1, 2])
    outputs = vector(t.OperatorStartOutputsVector, [3])
    t.OperatorStart(b)
    t.OperatorAddInputs(b, inputs)
    t.OperatorAddOutputs(b, outputs)
    t.OperatorAddBuiltinOptionsType(b, option_type)
    t.OperatorAddBuiltinOptions(b, options)
    op = t.OperatorEnd(b)
    tensor_vec = vector(t.SubGraphStartTensorsVector, tensors, True)
    input_vec = vector(t.SubGraphStartInputsVector, [0])
    output_vec = vector(t.SubGraphStartOutputsVector, [3])
    ops = vector(t.SubGraphStartOperatorsVector, [op], True)
    t.SubGraphStart(b)
    t.SubGraphAddTensors(b, tensor_vec)
    t.SubGraphAddInputs(b, input_vec)
    t.SubGraphAddOutputs(b, output_vec)
    t.SubGraphAddOperators(b, ops)
    graph = t.SubGraphEnd(b)
    t.OperatorCodeStart(b)
    t.OperatorCodeAddBuiltinCode(b, builtin)
    t.OperatorCodeAddDeprecatedBuiltinCode(b, builtin)
    t.OperatorCodeAddVersion(b, 1)
    code = t.OperatorCodeEnd(b)
    codes = vector(t.ModelStartOperatorCodesVector, [code], True)
    graphs = vector(t.ModelStartSubgraphsVector, [graph], True)
    bufs = vector(t.ModelStartBuffersVector, buffers, True)
    description = b.CreateString(f"SuiFleet {task} analytical fixture 1.0.0; MIT")
    t.ModelStart(b)
    t.ModelAddVersion(b, 3)
    t.ModelAddOperatorCodes(b, codes)
    t.ModelAddSubgraphs(b, graphs)
    t.ModelAddBuffers(b, bufs)
    t.ModelAddDescription(b, description)
    result = t.ModelEnd(b)
    b.Finish(result, file_identifier=b"TFL3")
    return bytes(b.Output())


def generate():
    for task in ("classifier", "detector"):
        (ROOT / f"models/{task}-1.0.0.tflite").write_bytes(model(task))
    fixtures = {
        "dark": [0]*16, "bright": [255]*16,
        "marker": [255, 255, 0, 0, 255, 255, 0, 0] + [0]*8,
        "two-markers": [0, 0, 255, 255]*2 + [255, 255, 0, 0]*2,
        "gray": [128]*16,
    }
    for name, pixels in fixtures.items():
        (ROOT / f"images/{name}.pgm").write_bytes(b"P5\n4 4\n255\n" + bytes(pixels))
    print(json.dumps({str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for directory in ("models", "images") for p in sorted((ROOT / directory).iterdir())}, indent=2))


if __name__ == "__main__":
    generate()
