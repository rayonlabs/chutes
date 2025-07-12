import os
from chutes.chute import NodeSelector
from chutes.chute.template.sglang import build_sglang_chute

os.environ["NO_PROXY"] = "localhost,127.0.0.1"

chute = build_sglang_chute(
    username="chutes",
    readme="Qwen/Qwen2.5-VL-32B-Instruct",
    model_name="Qwen/Qwen2.5-VL-32B-Instruct",
    image="chutes/sglang:0.4.6.post5b",
    node_selector=NodeSelector(
        gpu_count=8,
        min_vram_gb_per_gpu=48,
        exclude=["b200", "mi300x", "h200"],
    ),
    engine_args=(
        "--trust-remote-code "
        "--context-length 16384 "
        "--revision 6bcf1c9155874e6961bcf82792681b4f4421d2f7 "
        "--enable-multimodal "
        "--chat-template qwen2-vl "
        "--grammar-backend xgrammar"
    ),
    concurrency=8,
)
