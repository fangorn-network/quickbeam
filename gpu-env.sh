# Put the pip-installed NVIDIA libraries on the loader path. Source, don't run:
#   source gpu-env.sh
#
# WHY THIS EXISTS. onnxruntime-gpu dlopen's libcudnn/libcublas at session-creation
# time and, when it cannot find them, prints a warning and SILENTLY FALLS BACK TO CPU.
# Nothing errors, nothing fails — embedding is just ~10x slower, which reads as "the
# model is slow" rather than "the GPU is not being used". Measured on this box:
# 147 docs/s on CPU vs 1433/s on GPU, i.e. 2.5 hours vs 6 minutes for 500k.
#
# WHY IT GLOBS. The wheels do not agree on a directory name — cuDNN lands in
# nvidia/cudnn/lib but cuBLAS lands in nvidia/cu13/lib (the CUDA-13 wheels bundle
# several libraries under one `cu13` directory). examples/audius/audius-build/rebuild.sh hardcoded
# nvidia/cublas/lib, which has never existed for this wheel set. Globbing every
# nvidia/*/lib survives the next repackaging too.
#
# Requires: pip install nvidia-cudnn-cu13   (pulls nvidia-cublas + nvidia-cuda-nvrtc)
# Check it worked:
#   python -c "from fastembed import TextEmbedding as T; \
#              print(T(model_name='nomic-ai/nomic-embed-text-v1.5').model.model.get_providers())"
# You want CUDAExecutionProvider in that list.

# WHY IT SYMLINKS. onnxruntime dlopen's the UNVERSIONED name — `strings
# libonnxruntime_providers_cuda.so | grep libcudnn` yields exactly `libcudnn.so` — but
# the nvidia-cudnn-cu13 wheel ships only `libcudnn.so.9` and `libcudnn_graph.so.9`, with
# no unversioned symlink. On the loader path but unresolvable, the CUDA provider still
# REGISTERS and then dies partway through the graph:
#   NOT_IMPLEMENTED ... Einsum ... cuDNN is unavailable or disabled for CUDA Execution
#   Provider: dlopen failed for libcudnn.so
# which reads as an unsupported operator rather than a missing library. Recreated here
# rather than by hand because `pip install` rewrites that directory and drops the link.

_QB_VENV="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/venv"
_QB_SP="$("$_QB_VENV/bin/python" -c 'import site; print(site.getsitepackages()[0])' 2>/dev/null)"
if [ -n "$_QB_SP" ] && [ -d "$_QB_SP/nvidia" ]; then
  # Unversioned aliases for anything shipped versioned-only.
  for _QB_L in "$_QB_SP"/nvidia/*/lib/*.so.[0-9]*; do
    [ -e "$_QB_L" ] || continue
    _QB_BASE="${_QB_L%%.so.*}.so"
    if [ ! -e "$_QB_BASE" ] && [ -w "$(dirname "$_QB_L")" ]; then
      ln -sf "$(basename "$_QB_L")" "$_QB_BASE"
    fi
  done
  _QB_LIBS="$(ls -d "$_QB_SP"/nvidia/*/lib 2>/dev/null | paste -sd:)"
  [ -n "$_QB_LIBS" ] && export LD_LIBRARY_PATH="$_QB_LIBS:${LD_LIBRARY_PATH:-}"
fi
unset _QB_VENV _QB_SP _QB_LIBS _QB_L _QB_BASE
