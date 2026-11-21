"""Convert the three official downloads into PhotoNest's offline ONNX model pack.

Development only: run using the Python environment with torch/cn_clip, PaddlePaddle,
Paddle2ONNX, onnx and onnxruntime installed. None of these training/export frameworks
are imported by the desktop application.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    import numpy as np
    import onnxruntime as ort
    import yaml
    from PIL import Image
    from ai_engine import clip_pixels, normalized

    parser = argparse.ArgumentParser()
    parser.add_argument("--ocr-only", action="store_true")
    parser.add_argument("--clip-only", action="store_true")
    args = parser.parse_args()
    output = ROOT / "models"
    scratch = ROOT / "_build" / "model-source"
    output.mkdir(exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    report = {}
    if not args.clip_only:
        for role, model in (("det", "PP-OCRv5_mobile_det"), ("rec", "PP-OCRv5_mobile_rec")):
            archive = ROOT / f"{model}_infer.tar"
            source = scratch / f"{model}_infer"
            if not source.is_dir():
                with tarfile.open(archive) as tar:
                    tar.extractall(scratch, filter="data")
            config = yaml.safe_load((source / "inference.yml").read_text(encoding="utf-8"))
            target = output / "ocr" / f"{role}.onnx"
            target.parent.mkdir(exist_ok=True)
            if not target.exists():
                # Keep Paddle's import-time cache writes inside the build directory.
                bootstrap = (
                    "import os,runpy; from unittest.mock import patch; "
                    f"base={str(scratch / 'paddle-cache')!r}; "
                    "original=os.path.expanduser; "
                    "redirect=lambda p: base+p[1:] if p.startswith('~') else original(p); "
                    "guard=patch('os.path.expanduser', side_effect=redirect); guard.start(); "
                    "runpy.run_module('paddle2onnx.command',run_name='__main__')"
                )
                subprocess.run([
                    sys.executable, "-c", bootstrap, "--model_dir", source.name,
                    "--model_filename", "inference.json", "--params_filename", "inference.pdiparams",
                    "--opset_version", "17", "--save_file", os.path.relpath(target, scratch),
                ], check=True, cwd=scratch)
            session = ort.InferenceSession(str(target), providers=["CPUExecutionProvider"])
            print(role, "inputs:", [(i.name, i.shape) for i in session.get_inputs()], flush=True)
            if role == "rec":
                characters = config["PostProcess"]["character_dict"]
                write_json(output / "ocr" / "characters.json", characters)
                assert session.get_outputs()[0].shape[-1] == len(characters) + 2
        if args.ocr_only:
            return

    import torch
    import cn_clip.clip as clip
    from onnxruntime.quantization import (
        CalibrationDataReader, QuantFormat, QuantType, quantize_dynamic, quantize_static,
    )
    # ONNX's Windows path-based shape inference mishandles non-ASCII profile paths.
    # The in-memory API performs the same inference without passing paths to C++.
    import importlib
    import onnx
    from onnxruntime.quantization.quant_utils import add_infer_metadata

    def infer_in_memory(path):
        inferred = onnx.shape_inference.infer_shapes(onnx.load(str(path)))
        add_infer_metadata(inferred)
        return inferred

    for module in ("onnxruntime.quantization.quant_utils", "onnxruntime.quantization.quantize",
                   "onnxruntime.quantization.calibrate"):
        importlib.import_module(module).load_model_with_shape_infer = infer_in_memory

    checkpoint = ROOT / "clip_cn_rn50.pt"
    with checkpoint.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == "b196ee3ee528b70be1158ab1aafb1d2f1c801ad2d9ffb3bae31b0d305f82fc88", "RN50 下载文件校验失败"
    torch.set_num_threads(2)
    model, preprocess = clip.load_from_name("RN50", device="cpu", download_root=str(ROOT))
    model.float().eval()
    print("RN50 loaded", flush=True)

    class Vision(torch.nn.Module):
        def __init__(self, encoder):
            super().__init__()
            self.encoder = encoder

        def forward(self, image):
            return self.encoder.encode_image(image)

    class Text(torch.nn.Module):
        def __init__(self, encoder):
            super().__init__()
            self.encoder = encoder

        def forward(self, tokens):
            return self.encoder.encode_text(tokens)

    fp_image, fp_text = scratch / "rn50-image.fp32.onnx", scratch / "rn50-text.fp32.onnx"
    with torch.no_grad():
        if not fp_image.exists():
            torch.onnx.export(Vision(model), torch.zeros((1, 3, 224, 224)), str(fp_image),
                input_names=["image"], output_names=["features"], dynamic_axes={"image": {0: "batch"}, "features": {0: "batch"}},
                opset_version=17, dynamo=False)
        if not fp_text.exists():
            torch.onnx.export(Text(model), clip.tokenize(["照片"]), str(fp_text),
                input_names=["tokens"], output_names=["features"], dynamic_axes={"tokens": {0: "batch"}, "features": {0: "batch"}},
                opset_version=17, dynamo=False)
    print("RN50 exported", flush=True)
    target = output / "chinese_clip"
    target.mkdir(exist_ok=True)
    vocabulary = Path(clip.__file__).parent / "vocab.txt"
    if not vocabulary.exists():
        vocabulary = next(Path(clip.__file__).parent.rglob("vocab.txt"))
    shutil.copy2(vocabulary, target / "vocab.txt")
    from ai_engine import WordPiece
    queries = ["海边日落", "一只猫趴在沙发上", "发票", "PhotoNest 2026", "皮卡丘", "今天的会议通知，项目报销", "照片文件 IMG_2048.jpg"]
    expected = clip.tokenize(queries).cpu().numpy()
    actual = WordPiece(target / "vocab.txt").encode(queries)
    assert np.array_equal(expected, actual), "中文分词与官方实现不一致"
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    original_image = ort.InferenceSession(str(fp_image), options, providers=["CPUExecutionProvider"])
    original_text = ort.InferenceSession(str(fp_text), options, providers=["CPUExecutionProvider"])
    text_reference = normalized(original_text.run(None, {"tokens": expected})[0])
    with torch.no_grad():
        torch_text = normalized(model.encode_text(torch.from_numpy(expected)).cpu().numpy())
    assert np.min(np.sum(torch_text * text_reference, axis=1)) > 0.999

    image_paths = sorted((ROOT / "data" / "cache" / "thumb").rglob("*.jpg"))
    step = max(1, len(image_paths) // 32)
    images = []
    for path in image_paths[::step][:32]:
        try:
            with Image.open(path) as image:
                images.append(image.convert("RGB"))
        except OSError:
            pass
    if not images:
        images = [Image.new("RGB", (480, 320), (r, g, b)) for r, g, b in
                  ((40, 120, 220), (220, 180, 60), (60, 180, 90), (200, 200, 200))]
    samples = [clip_pixels(image) for image in images]
    with torch.no_grad():
        reference = normalized(model.encode_image(torch.from_numpy(samples[0])).cpu().numpy())
    assert float((reference * normalized(original_image.run(None, {"image": samples[0]})[0])).sum()) > 0.999

    class Reader(CalibrationDataReader):
        def __init__(self):
            self.data = iter(samples)

        def get_next(self):
            array = next(self.data, None)
            return {"image": array} if array is not None else None

    int_image, int_text = target / "image.int8.onnx", target / "text.int8.onnx"
    if not int_text.exists():
        quantize_dynamic(str(fp_text), str(int_text), per_channel=True, reduce_range=True,
                         weight_type=QuantType.QInt8, op_types_to_quantize=["MatMul", "Gemm"])
    if not int_image.exists():
        quantize_static(str(fp_image), str(int_image), Reader(), quant_format=QuantFormat.QDQ,
                        per_channel=True, activation_type=QuantType.QUInt8, weight_type=QuantType.QInt8,
                        op_types_to_quantize=["Conv", "MatMul", "Gemm"],
                        extra_options={"WeightSymmetric": True, "ActivationSymmetric": False})
    quant_text = ort.InferenceSession(str(int_text), options, providers=["CPUExecutionProvider"])
    text_cosines = np.sum(text_reference * normalized(quant_text.run(None, {"tokens": expected})[0]), axis=1)
    report["text_min_cosine"] = float(text_cosines.min())
    if text_cosines.min() < 0.98:
        print("INT8 text below validation threshold; validating FP16 weight storage", flush=True)
        selected_text = target / "text.fp16-storage.onnx"
        fp16_storage(fp_text, selected_text)
        compressed = ort.InferenceSession(str(selected_text), options, providers=["CPUExecutionProvider"])
        report["text_storage_min_cosine"] = float(np.min(np.sum(text_reference * normalized(
            compressed.run(None, {"tokens": expected})[0]), axis=1)))
        assert report["text_storage_min_cosine"] > 0.999
    else:
        selected_text = int_text
    quant_image = ort.InferenceSession(str(int_image), options, providers=["CPUExecutionProvider"])
    cosines, elapsed = [], []
    for pixels in samples[:12]:
        baseline = normalized(original_image.run(None, {"image": pixels})[0])
        started = time.perf_counter()
        candidate = normalized(quant_image.run(None, {"image": pixels})[0])
        elapsed.append(time.perf_counter() - started)
        cosines.append(float((baseline * candidate).sum()))
    report.update(image_min_cosine=min(cosines), image_mean_cosine=float(np.mean(cosines)),
                  calibration_images=len(samples), image_mean_seconds=float(np.mean(elapsed)))
    if min(cosines) < 0.95 or np.mean(cosines) < 0.98:
        print("INT8 image below validation threshold; validating FP16 weight storage", flush=True)
        selected_image = target / "image.fp16-storage.onnx"
        fp16_storage(fp_image, selected_image)
        compressed = ort.InferenceSession(str(selected_image), options, providers=["CPUExecutionProvider"])
        storage_cosines = []
        for pixels in samples[:12]:
            baseline = normalized(original_image.run(None, {"image": pixels})[0])
            candidate = normalized(compressed.run(None, {"image": pixels})[0])
            storage_cosines.append(float((baseline * candidate).sum()))
        report["image_storage_min_cosine"] = min(storage_cosines)
        assert report["image_storage_min_cosine"] > 0.999
    else:
        selected_image = int_image
    paths = {"ocr_detector": output / "ocr" / "det.onnx", "ocr_recognizer": output / "ocr" / "rec.onnx",
             "ocr_characters": output / "ocr" / "characters.json", "clip_image": selected_image,
             "clip_text": selected_text, "clip_vocab": target / "vocab.txt"}
    files = {}
    for name, path in paths.items():
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        files[name] = {"path": path.relative_to(output).as_posix(), "sha256": digest, "bytes": path.stat().st_size}
    version = "rn50-ocrv5-" + hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:12]
    manifest = {"format": 1, "version": version, "clip_dim": int(text_reference.shape[-1]), "files": files}
    write_json(output / "manifest.json", manifest)
    report["selected_models"] = {k: v["path"] for k, v in files.items()}
    report["total_model_bytes"] = sum(v["bytes"] for v in files.values())
    write_json(ROOT / "docs" / "ai-model-validation.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


def fp16_storage(source, target):
    """Store constants in half precision; explicit casts retain CPU FP32 computation."""
    import numpy as np
    import onnx
    from onnx import TensorProto, helper, numpy_helper
    model = onnx.load(str(source))
    tensors, casts = [], []
    for tensor in model.graph.initializer:
        if tensor.data_type == TensorProto.FLOAT and np.prod(tensor.dims) >= 256:
            name = "PhotoNest.fp16." + tensor.name
            array = numpy_helper.to_array(tensor).astype(np.float16)
            if not np.isfinite(array).all():
                tensors.append(tensor)
                continue
            tensors.append(numpy_helper.from_array(array, name))
            casts.append(helper.make_node("Cast", [name], [tensor.name], to=TensorProto.FLOAT))
        else:
            tensors.append(tensor)
    nodes = casts + list(model.graph.node)
    del model.graph.node[:]
    del model.graph.initializer[:]
    model.graph.node.extend(nodes)
    model.graph.initializer.extend(tensors)
    onnx.checker.check_model(model)
    onnx.save(model, str(target))


if __name__ == "__main__":
    main()
