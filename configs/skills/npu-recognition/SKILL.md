---
name: npu-recognition
description: NPU face/identity recognition model deployment. Use when tasks mention face recognition, identity verification, FAR, FRR, feature extraction, or embedding.
keywords: [face, recognition, identity, verification, FAR, FRR, embedding, cosine, feature-vector, npu, arcface, insightface]
---

# NPU Face/Identity Recognition Deployment

Deploy face recognition models to NPU with feature extraction and similarity matching.

## Workflow

### Phase 1: Pipeline Setup
- Face detection model (small YOLO or RetinaFace) → crop + align
- Feature extraction model (ArcFace/InsightFace) → 512-dim embedding
- Verify both models have ONNX exports

### Phase 2: Quantization
- Calibration dataset: 500+ face images covering diverse demographics
- Quantize both models independently
- Feature extraction model is more sensitive — consider FP16 fallback

### Phase 3: Accuracy Verification
- Run verification on LFW (Labeled Faces in the Wild) or custom dataset
- Metrics:
  - **FAR (False Accept Rate)**: probability of wrongly matching
  - **FRR (False Reject Rate)**: probability of wrongly rejecting
  - **Cosine similarity threshold**: calibrate on validation set
- Compare FP32 vs INT8 embeddings: cosine distance should be < 0.01

### Phase 4: Integration
- Feature vector database (SQLite or FAISS for production)
- 1:1 verification: compare two embeddings
- 1:N identification: search against enrolled database

## Key Metrics
| Metric | Target | Fail Threshold |
|--------|--------|---------------|
| FAR@FRR=0.01 | <= 0.001 | > 0.01 |
| Accuracy delta | <= 1% | > 3% |
| Latency (detect+embed) | <= 50ms | > 100ms |
| Embedding distance drift | < 0.01 | > 0.05 |

## Tools
- `run_simulation` with `track="npu"`
- `search_past_solutions` for quantization sensitivity issues
