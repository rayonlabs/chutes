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

# Example image def.
# from chutes.image import Image
# image = (
#     Image(
#         username="chutes",
#         name="sglang",
#         tag="0.4.7.post1a",
#         readme="SGLang is a fast serving framework for large language models and vision language models. It makes your interaction with models faster and more controllable by co-designing the backend runtime and frontend language.",
#     )
#     .from_base("parachutes/python:3.12.9")
#     .run_command("pip install --upgrade pip")
#     .run_command("pip install --upgrade 'sglang[all]==0.4.7.post1' datasets blobfile")
#     .with_env("SGL_ENABLE_JIT_DEEPGEMM", "1")
# )

chute = build_sglang_chute(
    username="chutes",
    readme="DeepSeek-V3",
    model_name="deepseek-ai/DeepSeek-V3",
    image="chutes/sglang:0.4.6.post5b",
    concurrency=20,
    node_selector=NodeSelector(
        gpu_count=8,
        min_vram_gb_per_gpu=140,
        exclude=["mi300x", "b200"],
    ),
    engine_args=(
        "--trust-remote-code "
        "--revision 1d044fd82b15f1cedb197a288e50cc96a2c27205 "
        "--tool-call-parser deepseekv3 "
        "--chat-template /app/chat_template.jinja"
    ),
)
