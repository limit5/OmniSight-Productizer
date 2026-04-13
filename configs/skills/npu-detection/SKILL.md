---
name: npu-detection
description: NPU object/defect detection model deployment. Use when tasks mention YOLO, object detection, defect detection, bounding box, mAP, or NPU inference.
keywords: [yolo, detection, object, defect, bounding-box, nms, mAP, npu, rknn, tflite, tensorrt, inference, quantize]
---

# NPU Object/Defect Detection Deployment

Deploy YOLO-based detection models to NPU hardware with quantization and accuracy verification.

## Workflow

### Phase 1: Model Preparation
- Verify ONNX model exists and is valid
- Check input dimensions match target sensor resolution
- Confirm class labels file exists

### Phase 2: Quantization & Conversion
- Prepare calibration dataset (100-500 representative images)
- Run vendor conversion tool:
  - RKNN: `rknn.build(do_quantization=True, dataset='calibration.txt')`
  - TFLite: `converter.optimizations = [tf.lite.Optimize.DEFAULT]`
  - TensorRT: `trtexec --onnx=model.onnx --int8 --calib=calibration`
- Record model size before/after quantization

### Phase 3: Accuracy Verification
- Run `simulate.sh --type=npu --module={module} --npu-model={model_path} --test-images={dataset}`
- Compare mAP before (FP32) vs after (INT8) quantization
- Accuracy drop threshold: **<= 2%**
- If exceeds: try mixed-precision (FP16 for sensitive layers) or QAT (Quantization-Aware Training)

### Phase 4: Post-Processing
- NMS (Non-Maximum Suppression) parameter tuning:
  - IoU threshold: 0.45 (default)
  - Confidence threshold: 0.25 (default)
- Validate on edge cases: small objects, overlapping objects, low light

## Key Metrics
| Metric | Target | Fail Threshold |
|--------|--------|---------------|
| mAP@0.5 | >= 0.85 | < 0.80 |
| Accuracy delta | <= 2% | > 5% |
| Latency | <= 30ms/frame | > 50ms |
| Throughput | >= 30fps | < 15fps |

## Tools
- `run_simulation` with `track="npu"`
- `get_platform_config` for NPU SDK paths
- `search_past_solutions` for historical quantization fixes
