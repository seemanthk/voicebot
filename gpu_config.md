# GPU Optimization Guide (24GB GPU)

## Current Status
Your system has a 24GB GPU but the warning shows GPU device discovery failed. This is because `onnxruntime` (CPU version) is installed instead of `onnxruntime-gpu`.

## Optional: Enable GPU Acceleration

If you want to leverage your 24GB GPU for better performance:

### 1. Uninstall CPU version and install GPU version:
```bash
pip uninstall onnxruntime
pip install onnxruntime-gpu
```

### 2. Verify CUDA is installed:
```bash
nvidia-smi
```

### 3. Install CUDA toolkit if needed:
```bash
# For Ubuntu/Debian
sudo apt-get install nvidia-cuda-toolkit
```

## Benefits of GPU Acceleration
- Faster Voice Activity Detection (VAD) processing
- Lower latency for Silero VAD model
- Better performance with multiple concurrent calls

## Note
The application works fine without GPU acceleration. GPU is optional for optimization.
