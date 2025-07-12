import os
from chutes.chute import NodeSelector
from chutes.chute.template.sglang import build_sglang_chute

os.environ["NO_PROXY"] = "localhost,127.0.0.1"

chute = build_sglang_chute(
    username="chutes",
    readme="deepseek-ai/DeepSeek-R1",
    model_name="deepseek-ai/DeepSeek-R1",
    image="chutes/sglang:0.4.6.post5b",
    concurrency=24,
    node_selector=NodeSelector(
        gpu_count=8,
        min_vram_gb_per_gpu=140,
        include=["h200"],
    ),
    engine_args=(
        "--trust-remote-code "
        "--revision f7361cd9ff99396dbf6bd644ad846015e59ed4fc"
    ),
)
