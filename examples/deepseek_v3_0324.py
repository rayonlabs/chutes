import os
import requests
from chutes.chute import NodeSelector
from chutes.chute.template.sglang import build_sglang_chute

os.environ["NO_PROXY"] = "localhost,127.0.0.1"

# Download a chat template from SGL repo that supports tool calling.
if os.getenv("CHUTES_EXECUTION_CONTEXT") == "REMOTE":
    with open("/app/chat_template.jinja", "w") as outfile:
        outfile.write(
            requests.get(
                "https://raw.githubusercontent.com/sgl-project/sglang/9a405274e287ce370a7788c6c70c4d40b06b688b/examples/chat_template/tool_chat_template_deepseekv3.jinja"
            ).text
        )

chute = build_sglang_chute(
    username="chutes",
    readme="DeepSeek-V3-0324",
    model_name="deepseek-ai/DeepSeek-V3-0324",
    image="chutes/sglang:0.4.6.post5b",
    concurrency=24,
    node_selector=NodeSelector(
        gpu_count=8,
        min_vram_gb_per_gpu=140,
    ),
    engine_args=(
        "--trust-remote-code "
        "--revision f6be68c847f9ac8d52255b2c5b888cc6723fbcb2 "
        "--tool-call-parser deepseekv3 "
        "--chat-template /app/chat_template.jinja"
    ),
)
