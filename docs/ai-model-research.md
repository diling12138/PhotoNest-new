# PhotoNest 本地识图方案核实

核实日期：2026-10-26。本文仅为方案研究；未修改应用源码、安装依赖或下载模型。

## 建议

以 **Chinese-CLIP ViT-B/16** 为中文搜索基线，用 **SigLIP 2 base-patch16-224** 做小样本对照。先在 Windows CPU 上验证导出后的 FP32 ONNX 图文双编码器，再决定是否量化、是否增加 GPU 后端。本文不承诺索引速度、查询延迟、内存峰值或两者在用户照片库中的准确率。

## 已核实的候选

| 候选 | 官方支持与已核实数字 | 对 PhotoNest 的意义 |
|---|---|---|
| Chinese-CLIP ViT-B/16 | 官方定位包含中文图文检索和零样本图片分类；约 188M 参数；输入分辨率 224；配置的 `projection_dim` 为 512。 | 中文查询有直接的官方训练与使用依据，适合作为首个基线。 |
| SigLIP 2 base-patch16-224 | Google 官方模型卡支持图文检索及零样本分类，关联论文为多语言视觉语言编码器研究；官方单个 `model.safetensors` 约 1.5 GB，另有分词器文件，目录总计约 1.54 GB。 | 可作对照，不宜因为名称含 base 就假定体积小、中文效果更好或 CPU 更快。 |

来源：[Chinese-CLIP 官方仓库](https://github.com/OFA-Sys/Chinese-CLIP)、[Chinese-CLIP 配置](https://huggingface.co/OFA-Sys/chinese-clip-vit-base-patch16/raw/main/config.json)、[Google 官方模型卡](https://huggingface.co/google/siglip2-base-patch16-224)、[Google 官方文件列表](https://huggingface.co/google/siglip2-base-patch16-224/tree/main)。

Chinese-CLIP 官方仓库中的 `clip_cn_vit-b-16.pt` 单文件约 **753 MB**。同一目录还有另一份序列化格式的模型；目录显示约 1.51 GB，不能把两份格式相加当成必须下载量。部署版 ONNX 的实际体积应在导出后统计，不能直接套用训练检查点大小。[官方文件列表](https://huggingface.co/OFA-Sys/chinese-clip-vit-base-patch16/tree/main)

## Windows CPU 部署边界

Chinese-CLIP 官方 API 示例支持 `cpu` 设备；其 ONNX 部署文档提供图文编码器导出流程，但演示推理使用 CUDA FP16，并不是可直接宣称已验证的 Windows CPU 便携包。应另行验证 FP32 导出图、中文分词、图像预处理、向量归一化与 PyTorch 结果的一致性。ONNX Runtime 官方提供 Windows x64 CPU 发行包，这证明运行时路径可行，不代表具体模型已经兼容或达到可接受速度。[官方 API](https://github.com/OFA-Sys/Chinese-CLIP/blob/master/README_En.md)、[官方部署流程](https://github.com/OFA-Sys/Chinese-CLIP/blob/master/deployment_En.md)、[ONNX Runtime Python 支持平台](https://onnxruntime.ai/docs/get-started/with-python.html)

## 自动分类与 OCR

同一个图文编码器可把图片向量与预定义中文类别描述比较，生成“风景、宠物、美食、建筑”等候选标签；可允许多标签和“未分类”，支持人工纠正。相似度并非已校准的正确概率；阈值应按类别在实际照片样本上检查。语义相关性也不能代替读取截图中的准确文字，更不能直接确定具体人物身份。

若用户常找聊天截图、票据、课件文字，可另加 PaddleOCR，将识别文本写入本地全文索引。官方 PP-OCRv5 文档列出移动端识别模型约 16 MB，但这是识别模块，不是整套 OCR 的总下载量或总内存；还需要检测模型及运行时。其官方部署说明支持 Windows 和 CPU。该选项适合后续独立加入。[PaddleOCR 官方使用文档](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/OCR.html)、[官方 PP-OCRv5 介绍](https://www.paddleocr.ai/main/en/version3.x/algorithm/PP-OCRv5/PP-OCRv5.html)

## 许可核实

- Chinese-CLIP **代码仓库**为 MIT；所查 Hugging Face 模型卡没有单独列出明确的权重许可标签，因此不能仅凭代码 MIT 宣称已完整核实权重再分发条件。对外打包分发前应进一步确认所选权重的条款。
- SigLIP 2 该 **模型仓库**明确标注 Apache-2.0。实际分发仍应随所采用代码、权重及依赖保留各自要求的许可和声明。

来源：[Chinese-CLIP MIT 文件](https://github.com/OFA-Sys/Chinese-CLIP/blob/master/MIT-LICENSE.txt)、[Chinese-CLIP 模型卡](https://huggingface.co/OFA-Sys/chinese-clip-vit-base-patch16)、[SigLIP 2 模型许可标注](https://huggingface.co/google/siglip2-base-patch16-224)。

## 批准实施后的验证顺序

1. 选取用户认可的 100–300 张代表性照片，并准备中文搜索词及类别。
2. 对照两模型的检索结果、分类误判、CPU 索引耗时、内存与模型包体积，再选型。
3. 第一版先做本地增量索引、语义搜索、可纠正多标签；OCR 作为可选第二阶段。

以上顺序为工程建议，不是现有应用已经具备的功能。

## 轻量化与 OCR 补充（2026-10-26）

- **OCR 可以远小于通用图文模型。** 按 PaddleOCR 官方模型列表中的“模型存储大小”相加，PP-OCRv4 移动检测 4.7 MB + 中文移动识别 10.5 MB，约 **15.2 MB**；PP-OCRv5 移动检测 4.7 MB + 中文移动识别 16 MB，约 **20.7 MB**。这是两个核心 Paddle 推理模型的存储量级，不包含运行库、字典、可选方向/矫正模型，也不是已经实测的 ONNX 便携包体积。转换格式、打包后应重新统计。[官方模型列表](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/model_list.md)
- **减小图文语义模型可用小骨架和量化。** Chinese-CLIP RN50 官方为 77M 参数，单份原始权重约 308 MB。若所有权重均以 INT8 储存，纯参数理论约 77 MB；ViT-B/16 的 188M 参数则约 188 MB。实际 ONNX 包还包含未量化部分、量化尺度及图结构，不能把理论值当作下载承诺，精度与速度也需要实测。[官方模型表](https://github.com/OFA-Sys/Chinese-CLIP)、[RN50 权重列表](https://huggingface.co/OFA-Sys/chinese-clip-rn50/tree/main)、[ONNX Runtime 量化说明](https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html)
- **查找快应依靠预先识别和索引。** 在导入或空闲时完成 OCR/图像编码，保存结果；搜索时只查文字索引或比较已有向量，避免每次重新遍历识别照片。首次建立索引与后续查询是不同的成本。
- **中文全文索引要单独设计。** SQLite FTS5 默认 `unicode61` 按连续字母/数字等字符形成词元，不能直接视为中文分词器；`trigram` 支持子串，但 `MATCH` 中不足三个 Unicode 字符的查询不匹配任何行。两字中文查询须有专门分词/字粒度索引或安全的短词回退方案，不能承诺开箱即有完整中文全文检索。[SQLite 官方 FTS5 文档](https://www.sqlite.org/fts5.html)
- **Windows 系统 OCR 是备选。** `Windows.Media.Ocr.OcrEngine` 能复用设备可用的识别语言，但指定语言不可用时 `TryCreateFromLanguage` 返回空；应检查本机中文语言能力及 Python/WinRT 打包兼容性。它可能减少随应用携带的权重，但不等于所有 Windows 电脑都无需额外安装或无需适配。本次没有验证这台电脑的系统 OCR。[Microsoft OcrEngine](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine?view=winrt-26100)、[语言创建条件](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine.trycreatefromlanguage?view=winrt-26100)

这一补充仅核实可选路线，未修改应用代码、下载模型或执行性能测试。
