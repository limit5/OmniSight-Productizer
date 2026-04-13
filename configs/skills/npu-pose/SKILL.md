---
name: npu-pose
description: NPU pose estimation model deployment. Use when tasks mention pose, skeleton, keypoint, gesture, body tracking, or OKS.
keywords: [pose, skeleton, keypoint, gesture, body, hand, OKS, mediapipe, movenet, hrnet, npu, estimation]
---

# NPU Pose Estimation Deployment

Deploy human pose/gesture estimation models to NPU with keypoint accuracy verification.

## Workflow

### Phase 1: Model Selection
- Top-down (detect person first, then estimate pose) vs Bottom-up (detect all keypoints at once)
- Common models: MoveNet, HRNet, MediaPipe Pose
- Verify output format: N keypoints × (x, y, confidence)

### Phase 2: Quantization
- Keypoint regression models are sensitive to quantization
- Start with INT8, fall back to FP16 if OKS drops
- Calibration: 200+ images with diverse poses and body sizes

### Phase 3: Accuracy Verification
- **OKS (Object Keypoint Similarity)**: primary metric
- Run on COCO keypoint validation subset
- Per-joint accuracy analysis (wrists/ankles most sensitive)
- Verify temporal stability on video sequences (jitter < 3px)

### Phase 4: Post-Processing
- Keypoint connection graph (skeleton visualization)
- Smoothing filter for video (exponential moving average)
- Gesture classification from keypoint sequences

## Key Metrics
| Metric | Target | Fail Threshold |
|--------|--------|---------------|
| OKS@0.5 | >= 0.70 | < 0.60 |
| Accuracy delta | <= 3% | > 5% |
| Latency | <= 40ms/frame | > 80ms |
| Keypoint jitter (video) | < 3px | > 8px |

## Tools
- `run_simulation` with `track="npu"`
- `get_platform_config` for NPU configuration
