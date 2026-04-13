---
name: npu-barcode
description: NPU-assisted barcode/QR code detection and decoding. Use when tasks mention barcode, QR code, scanning, ZBar, or decode rate.
keywords: [barcode, qr, qrcode, scan, zbar, decode, 1d, 2d, datamatrix, npu, hybrid]
---

# NPU Barcode/QR Code Hybrid Detection

Deploy NPU-accelerated barcode localization with traditional decoder fallback.

## Architecture
```
Camera Frame → [NPU: Region Detector] → Crop ROIs → [CPU: ZBar/ZXing Decoder] → Result
```

The NPU handles fast localization (where are the barcodes?), while proven
traditional decoders (ZBar, ZXing) handle the actual symbol decoding.
This hybrid approach gives NPU speed with decoder reliability.

## Workflow

### Phase 1: Localization Model
- Train/convert lightweight detector (YOLO-tiny or SSD-MobileNet)
- Classes: 1D barcode, QR code, DataMatrix (3 classes)
- Output: bounding boxes for barcode regions

### Phase 2: Decoder Integration
- Crop detected regions from frame
- Apply perspective correction (homography)
- Feed to ZBar (1D + QR) or ZXing (DataMatrix)
- Fallback: full-frame ZBar scan if NPU detects nothing

### Phase 3: Accuracy Verification
- Test dataset: 500+ images covering:
  - Normal, angled (up to 45°), blurry, low-contrast
  - Multiple barcodes per frame
  - Extreme distances (near/far)
- Metric: **decode success rate** per scenario

### Phase 4: Optimization
- Adaptive frame skipping (only re-detect every N frames, track between)
- ROI padding for decoder (add 10% margin around NPU bbox)

## Key Metrics
| Metric | Target | Fail Threshold |
|--------|--------|---------------|
| Detection rate | >= 98% | < 95% |
| Decode success (normal) | >= 99% | < 97% |
| Decode success (angled 45°) | >= 90% | < 80% |
| Latency (detect+decode) | <= 25ms | > 50ms |

## Tools
- `run_simulation` with `track="npu"`
- `run_bash` for ZBar integration testing
