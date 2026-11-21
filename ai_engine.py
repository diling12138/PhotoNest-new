"""Offline ONNX inference. No downloads, network clients or training dependencies."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import unicodedata

import numpy as np
from PIL import Image, ImageOps


CATEGORIES = (
    ("人物", "人物肖像", "一群人在一起的照片"),
    ("猫咪", "一只猫的照片", "宠物猫"),
    ("狗狗", "一只狗的照片", "宠物狗"),
    ("动物", "野生动物的照片", "鸟类动物"),
    ("美食", "美食和菜肴", "餐桌上的食物"),
    ("海边", "大海和沙滩", "海边的风景照片"),
    ("山林", "山脉和森林的风景", "高山树林"),
    ("花草", "花卉和绿色植物", "盛开的鲜花"),
    ("建筑", "建筑物的照片", "城市建筑和街道"),
    ("车辆", "汽车的照片", "交通工具"),
    ("天空", "蓝天白云", "天空中的云朵"),
    ("日落", "夕阳和日落", "傍晚的晚霞"),
    ("夜景", "夜晚城市灯光", "夜景照片"),
    ("雪景", "下雪的风景", "雪山和冰雪"),
    ("室内", "房间内部的照片", "室内家具和装饰"),
    ("运动", "运动比赛的照片", "人们在户外运动"),
    ("文档", "纸上的印刷文档", "书本和文件的照片"),
    ("票据", "发票和收据的照片", "车票和票据"),
    ("屏幕截图", "电脑软件界面的截图", "手机屏幕的截图"),
    ("绘画", "绘画和插画", "动漫卡通人物"),
)


def model_manifest(root):
    root = Path(root).resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1:
        raise ValueError("模型包版本不受支持")
    for item in manifest["files"].values():
        path = (root / item["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("模型文件缺失，请完整解压模型包")
    return manifest


class WordPiece:
    """Uncased BERT basic tokenization + WordPiece for the official Chinese vocabulary."""
    def __init__(self, path):
        with Path(path).open(encoding="utf-8") as stream:
            self.vocab = {word.strip(): i for i, word in enumerate(stream)}

    @staticmethod
    def chinese(code):
        return any(lo <= code <= hi for lo, hi in (
            (0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x20000, 0x2A6DF),
            (0x2A700, 0x2B73F), (0x2B740, 0x2B81F), (0x2B820, 0x2CEAF),
            (0xF900, 0xFAFF), (0x2F800, 0x2FA1F)))

    def encode(self, texts, length=52):
        result = np.zeros((len(texts), length), dtype=np.int64)
        for row, text in enumerate(texts):
            clean = []
            for char in str(text):
                code, category = ord(char), unicodedata.category(char)
                if char.isspace():
                    clean.append(" ")
                elif code in (0, 0xFFFD) or category.startswith("C"):
                    continue
                elif self.chinese(code):
                    clean.extend((" ", char, " "))
                else:
                    clean.append(char)
            text = ''.join(c for c in unicodedata.normalize("NFD", ''.join(clean).lower())
                           if unicodedata.category(c) != "Mn")
            basic, word = [], ""
            for char in text + " ":
                code = ord(char)
                punctuation = unicodedata.category(char).startswith("P") or any(
                    lo <= code <= hi for lo, hi in ((33, 47), (58, 64), (91, 96), (123, 126)))
                if char.isspace() or punctuation:
                    if word:
                        basic.append(word)
                        word = ""
                    if punctuation:
                        basic.append(char)
                else:
                    word += char
            pieces = []
            for word in basic:
                start, current = 0, []
                if len(word) > 100:
                    pieces.append("[UNK]")
                    continue
                while start < len(word):
                    found = None
                    for end in range(len(word), start, -1):
                        part = ("##" if start else "") + word[start:end]
                        if part in self.vocab:
                            found = (part, end)
                            break
                    if found is None:
                        current = ["[UNK]"]
                        break
                    current.append(found[0])
                    start = found[1]
                pieces.extend(current)
            tokens = ["[CLS]"] + pieces[:length - 2] + ["[SEP]"]
            result[row, :len(tokens)] = [self.vocab.get(t, self.vocab["[UNK]"]) for t in tokens]
        return result


def read_image(path, crop=None):
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source)
        if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, "white")
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")
        if crop is not None:
            x0, y0, x1, y1 = [min(1, max(0, float(v))) for v in crop]
            if x1 <= x0 or y1 <= y0:
                raise ValueError("请选择有效的文字区域")
            image = image.crop((round(x0 * image.width), round(y0 * image.height),
                                round(x1 * image.width), round(y1 * image.height)))
        # convert/composite/crop have already detached this RGB image from source.
        return image


def clip_pixels(image):
    # Chinese-CLIP's official image_transform resizes both axes to 224 (no center crop).
    image = image.resize((224, 224), Image.Resampling.BICUBIC)
    array = np.asarray(image, dtype=np.float32) / 255.0
    array = (array - np.array([0.48145466, 0.4578275, 0.40821073], np.float32)) / np.array(
        [0.26862954, 0.26130258, 0.27577711], np.float32)
    return array.transpose(2, 0, 1)[None].copy()


def normalized(array):
    array = np.asarray(array, dtype=np.float32)
    return array / np.maximum(np.linalg.norm(array, axis=-1, keepdims=True), 1e-12)


class Engine:
    def __init__(self, root):
        import onnxruntime as ort
        self.root = Path(root).resolve()
        self.manifest = model_manifest(self.root)
        self.version = self.manifest["version"]
        files = {}
        for name, item in self.manifest["files"].items():
            path = self.root / item["path"]
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                    raise ValueError("模型校验失败，请重新解压完整模型包")
            files[name] = path
        options = ort.SessionOptions()
        available_cpus = getattr(os, "process_cpu_count", os.cpu_count)() or os.cpu_count() or 2
        options.intra_op_num_threads = max(1, min(4, available_cpus))
        options.inter_op_num_threads = 1
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        options.log_severity_level = 3
        self.det = ort.InferenceSession(str(files["ocr_detector"]), options, providers=["CPUExecutionProvider"])
        self.rec = ort.InferenceSession(str(files["ocr_recognizer"]), options, providers=["CPUExecutionProvider"])
        self.vision = ort.InferenceSession(str(files["clip_image"]), options, providers=["CPUExecutionProvider"])
        self.text = ort.InferenceSession(str(files["clip_text"]), options, providers=["CPUExecutionProvider"])
        chars = json.loads(files["ocr_characters"].read_text(encoding="utf-8"))
        self.chars = [""] + chars + [" "]
        self.tokenizer = WordPiece(files["clip_vocab"])
        self.category_vectors = np.vstack([
            normalized(self.encode_text(prompts).mean(axis=0)[None])[0] for _, *prompts in CATEGORIES])

    def encode_text(self, texts):
        tokens = self.tokenizer.encode(texts)
        return normalized(self.text.run(None, {self.text.get_inputs()[0].name: tokens})[0])

    def encode_image(self, image):
        return normalized(self.vision.run(None, {self.vision.get_inputs()[0].name: clip_pixels(image)})[0])[0]

    def classify(self, vector):
        scores = self.category_vectors @ vector
        order = np.argsort(-scores)
        best = float(scores[order[0]])
        return [CATEGORIES[i][0] for i in order[:3] if scores[i] >= max(0.22, best - 0.025)]

    def ocr(self, image):
        import cv2
        import pyclipper
        bgr = np.asarray(image)[:, :, ::-1].copy()
        height, width = bgr.shape[:2]
        ratio = min(1.0, 1280 / max(height, width))
        rh, rw = max(32, round(height * ratio / 32) * 32), max(32, round(width * ratio / 32) * 32)
        array = cv2.resize(bgr, (rw, rh)).astype(np.float32) / 255.0
        array = (array - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
        tensor = array.transpose(2, 0, 1)[None].copy()
        probability = np.asarray(self.det.run(None, {self.det.get_inputs()[0].name: tensor})[0]).squeeze()
        mh, mw = probability.shape
        contours, _ = cv2.findContours((probability > 0.3).astype(np.uint8) * 255,
                                       cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        for contour in contours[:1000]:
            rect = cv2.minAreaRect(contour)
            if min(rect[1]) < 3:
                continue
            points = cv2.boxPoints(rect)
            x0, y0 = np.maximum(np.floor(points.min(axis=0)).astype(int), 0)
            x1, y1 = np.minimum(np.ceil(points.max(axis=0)).astype(int) + 1, [mw, mh])
            mask = np.zeros((y1 - y0, x1 - x0), np.uint8)
            cv2.fillPoly(mask, [np.round(points - [x0, y0]).astype(np.int32)], 1)
            if cv2.mean(probability[y0:y1, x0:x1], mask)[0] < 0.6:
                continue
            perimeter = cv2.arcLength(points, True)
            distance = cv2.contourArea(points) * 1.5 / max(perimeter, 1e-5)
            offset = pyclipper.PyclipperOffset()
            offset.AddPath(np.round(points * 1024).astype(np.int64).tolist(), pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
            expanded = offset.Execute(distance * 1024)
            if len(expanded) != 1:
                continue
            rect = cv2.minAreaRect(np.array(expanded[0], np.float32) / 1024)
            if min(rect[1]) < 5:
                continue
            points = cv2.boxPoints(rect)
            points[:, 0] = np.clip(points[:, 0] * width / mw, 0, width - 1)
            points[:, 1] = np.clip(points[:, 1] * height / mh, 0, height - 1)
            points = self._ordered(points)
            boxes.append(points)
        boxes.sort(key=lambda b: (float(b[0, 1]), float(b[0, 0])))
        for i in range(len(boxes) - 1):
            for j in range(i, -1, -1):
                if abs(boxes[j + 1][0, 1] - boxes[j][0, 1]) < 10 and boxes[j + 1][0, 0] < boxes[j][0, 0]:
                    boxes[j], boxes[j + 1] = boxes[j + 1], boxes[j]
                else:
                    break
        blocks = []
        for box in boxes:
            crop_width = max(1, round(max(np.linalg.norm(box[0] - box[1]), np.linalg.norm(box[2] - box[3]))))
            crop_height = max(1, round(max(np.linalg.norm(box[0] - box[3]), np.linalg.norm(box[1] - box[2]))))
            target = np.array([[0, 0], [crop_width, 0], [crop_width, crop_height], [0, crop_height]], np.float32)
            crop = cv2.warpPerspective(bgr, cv2.getPerspectiveTransform(box, target),
                                        (crop_width, crop_height), borderMode=cv2.BORDER_REPLICATE)
            if crop_height / crop_width >= 1.5:
                crop = np.rot90(crop).copy()
            aspect = crop.shape[1] / crop.shape[0]
            rec_width = max(320, min(3200, math.ceil(48 * aspect / 8) * 8))
            resized_width = min(rec_width, max(1, math.ceil(48 * aspect)))
            rec_image = cv2.resize(crop, (resized_width, 48)).astype(np.float32) / 127.5 - 1
            padded = np.zeros((3, 48, rec_width), dtype=np.float32)
            padded[:, :, :resized_width] = rec_image.transpose(2, 0, 1)
            predictions = self.rec.run(None, {self.rec.get_inputs()[0].name: padded[None]})[0][0]
            if predictions.shape[-1] != len(self.chars):
                raise ValueError("OCR 字典与模型不匹配")
            indices, confidences = predictions.argmax(axis=-1), predictions.max(axis=-1)
            text, scores, previous = [], [], -1
            for index, confidence in zip(indices, confidences):
                if index and index != previous:
                    text.append(self.chars[int(index)])
                    scores.append(float(confidence))
                previous = index
            text = ''.join(text).strip()
            score = float(np.mean(scores)) if scores else 0.0
            if text and score >= 0.45:
                blocks.append({"text": text, "score": round(score, 4), "box": box.round(1).tolist()})
        return '\n'.join(b["text"] for b in blocks), blocks

    @staticmethod
    def _ordered(points):
        points = np.asarray(points, np.float32)
        xsorted = points[np.argsort(points[:, 0])]
        left, right = xsorted[:2], xsorted[2:]
        tl, bl = left[np.argsort(left[:, 1])]
        tr, br = right[np.argsort(right[:, 1])]
        return np.array([tl, tr, br, bl], np.float32)


def self_check(model_dir, output=None):
    """Verify bundled native libraries and both OCR/semantic paths without a photo library."""
    import time
    import sys
    import tempfile
    import cv2
    from media import video_probe, video_thumbnail
    engine = Engine(model_dir)
    fixture = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "assets/ai-check.png"
    image = read_image(fixture)
    started = time.perf_counter()
    text, blocks = engine.ocr(image)
    seconds = time.perf_counter() - started
    compact = "".join(text.split())
    if not all(term in compact.lower() for term in ("会议通知", "项目报销发票", "photonest2026")):
        raise ValueError("OCR 自检未通过：" + text)
    features = engine.encode_image(image)
    if features.shape != (engine.manifest["clip_dim"],) or not np.isfinite(features).all():
        raise ValueError("图像向量自检未通过")
    prompts = engine.encode_text(["会议通知", "项目报销发票", "白色文档上的文字", "蓝色大海的照片"])
    scores = prompts @ features
    if max(scores[:3]) <= scores[3]:
        raise ValueError("图文匹配自检未通过")
    crop_text, _ = engine.ocr(image.crop((45, 30, 420, 125)))
    if "会议通知" not in "".join(crop_text.split()):
        raise ValueError("区域 OCR 自检未通过：" + crop_text)
    report = dict(ocr_text=text, ocr_blocks=len(blocks), ocr_seconds=round(seconds, 3),
                  crop_text=crop_text, tags=engine.classify(features), semantic_scores=scores.tolist(),
                  model_version=engine.version)
    # Exercise the codecs inside the portable runtime using owned fixtures.
    with tempfile.TemporaryDirectory(prefix='PhotoNest-media-check-',
            dir=Path(output).parent if output else None) as temporary:
        gif = Path(temporary)/'animation.gif'
        Image.new('RGB', (64, 48), 'red').save(gif, save_all=True,
            append_images=[Image.new('RGB', (64, 48), 'blue')], duration=80, loop=0)
        with Image.open(gif) as animation:
            animation.seek(1)
            animation.convert('RGB').load()
            report['gif_frames'] = animation.n_frames
        video = Path(temporary)/'video.avi'
        writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'MJPG'), 10, (64, 48))
        try:
            if not writer.isOpened():
                raise ValueError('视频自检未能创建临时样本')
            for _ in range(5):
                writer.write(np.full((48, 64, 3), 128, dtype=np.uint8))
        finally:
            writer.release()
        cover = video_thumbnail(video)
        info = video_probe(video)
        if cover is None or cover.size != (64, 48) or info['duration'] <= 0:
            raise ValueError('视频封面自检未通过')
        report['video'] = info
    if output:
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
