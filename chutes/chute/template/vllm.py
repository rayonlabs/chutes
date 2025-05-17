import asyncio
import json
import os
from enum import Enum
from pydantic import BaseModel, Field
from typing import Dict, Any, Callable, Literal, Optional, Union, List
from chutes.image import Image
from chutes.image.standard.vllm import VLLM
from chutes.chute import Chute, ChutePack, NodeSelector

os.environ["VLLM_WORKER_MULTIPROC_METHOD"] = "spawn"


class DefaultRole(Enum):
    user = "user"
    assistant = "assistant"


class ChatMessage(BaseModel):
    role: str
    content: str


class Logprob(BaseModel):
    logprob: float
    rank: Optional[int] = None
    decoded_token: Optional[str] = None


class ResponseFormat(BaseModel):
    type: Literal["text", "json_object", "json_schema"]
    json_schema: Optional[Dict] = None


class BaseRequest(BaseModel):
    model: str
    frequency_penalty: Optional[float] = 0.0
    logit_bias: Optional[Dict[str, float]] = None
    logprobs: Optional[bool] = False
    top_logprobs: Optional[int] = 0
    max_tokens: Optional[int] = None
    presence_penalty: Optional[float] = 0.0
    response_format: Optional[ResponseFormat] = None
    seed: Optional[int] = Field(None, ge=0, le=9223372036854775807)
    stop: Optional[Union[str, List[str]]] = Field(default_factory=list)
    stream: Optional[bool] = False
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 1.0
    best_of: Optional[int] = None
    use_beam_search: bool = False
    top_k: int = -1
    min_p: float = 0.0
    repetition_penalty: float = 1.0
    length_penalty: float = 1.0
    stop_token_ids: Optional[List[int]] = Field(default_factory=list)
    include_stop_str_in_output: bool = False
    ignore_eos: bool = False
    min_tokens: int = 0
    skip_special_tokens: bool = True
    spaces_between_special_tokens: bool = True
    prompt_logprobs: Optional[int] = None


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    total_tokens: int = 0
    completion_tokens: Optional[int] = 0


class TokenizeRequest(BaseRequest):
    model: str
    prompt: str
    add_special_tokens: bool

class DetokenizeRequest(BaseRequest):
    model: str
    tokens: List[int]

class ChatCompletionRequest(BaseRequest):
    messages: List[ChatMessage]


class CompletionRequest(BaseRequest):
    prompt: str


class ChatCompletionLogProb(BaseModel):
    token: str
    logprob: float = -9999.0
    bytes: Optional[List[int]] = None


class ChatCompletionLogProbsContent(ChatCompletionLogProb):
    top_logprobs: List[ChatCompletionLogProb] = Field(default_factory=list)


class ChatCompletionLogProbs(BaseModel):
    content: Optional[List[ChatCompletionLogProbsContent]] = None


class ChatCompletionResponseChoice(BaseModel):
    index: int
    message: ChatMessage
    logprobs: Optional[ChatCompletionLogProbs] = None
    finish_reason: Optional[str] = "stop"
    stop_reason: Optional[Union[int, str]] = None


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionResponseChoice]
    usage: UsageInfo
    prompt_logprobs: Optional[List[Optional[Dict[int, Logprob]]]] = None

class TokenizeResponse(BaseRequest):
    count: int
    max_model_len: int
    tokens: List[int]

class DetokenizeResponse(BaseRequest):
    prompt: str

class DeltaMessage(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None


class ChatCompletionResponseStreamChoice(BaseModel):
    index: int
    delta: DeltaMessage
    logprobs: Optional[ChatCompletionLogProbs] = None
    finish_reason: Optional[str] = None
    stop_reason: Optional[Union[int, str]] = None


class ChatCompletionStreamResponse(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: List[ChatCompletionResponseStreamChoice]
    usage: Optional[UsageInfo] = Field(default=None)


class CompletionLogProbs(BaseModel):
    text_offset: List[int] = Field(default_factory=list)
    token_logprobs: List[Optional[float]] = Field(default_factory=list)
    tokens: List[str] = Field(default_factory=list)
    top_logprobs: List[Optional[Dict[str, float]]] = Field(default_factory=list)


class CompletionResponseChoice(BaseModel):
    index: int
    text: str
    logprobs: Optional[CompletionLogProbs] = None
    finish_reason: Optional[str] = None
    stop_reason: Optional[Union[int, str]] = Field(
        default=None,
        description=(
            "The stop string or token id that caused the completion "
            "to stop, None if the completion finished for some other reason "
            "including encountering the EOS token"
        ),
    )
    prompt_logprobs: Optional[List[Optional[Dict[int, Logprob]]]] = None


class CompletionResponse(BaseModel):
    id: str
    object: str = "text_completion"
    created: int
    model: str
    choices: List[CompletionResponseChoice]
    usage: UsageInfo


class CompletionResponseStreamChoice(BaseModel):
    index: int
    text: str
    logprobs: Optional[CompletionLogProbs] = None
    finish_reason: Optional[str] = None
    stop_reason: Optional[Union[int, str]] = Field(
        default=None,
        description=(
            "The stop string or token id that caused the completion "
            "to stop, None if the completion finished for some other reason "
            "including encountering the EOS token"
        ),
    )


class CompletionStreamResponse(BaseModel):
    id: str
    object: str
    created: int
    model: str
    choices: List[CompletionResponseStreamChoice]
    usage: Optional[UsageInfo] = Field(default=None)


class VLLMChute(ChutePack):
    chat: Callable
    completion: Callable
    chat_stream: Callable
    completion_stream: Callable
    models: Callable


def build_vllm_chute(
    username: str,
    model_name: str,
    node_selector: NodeSelector,
    image: str | Image = VLLM,
    tagline: str = "",
    readme: str = "",
    concurrency: int = 32,
    engine_args: Dict[str, Any] = {},
):
    chute = Chute(
        username=username,
        name=model_name,
        tagline=tagline,
        readme=readme,
        image=image,
        node_selector=node_selector,
        concurrency=concurrency,
        standard_template="vllm",
    )

    # Semi-optimized defaults for code starts (but not overall perf once hot).
    defaults = {
        "enforce_eager": False,
        "num_scheduler_steps": 1,
        "multi_step_stream_outputs": True,
        "enable_chunked_prefill": False,
        "enable_prefix_caching": False,
        "disable_log_stats": True,
        "disable_custom_all_reduce": True,
        "disable_log_requests": True,
    }
    for key, value in defaults.items():
        if key not in engine_args:
            engine_args[key] = value

    # Minimal input schema with defaults.
    class MinifiedMessage(BaseModel):
        role: DefaultRole = DefaultRole.user
        content: str = Field("")

    class MinifiedStreamChatCompletion(BaseModel):
        messages: List[MinifiedMessage] = [MinifiedMessage()]
        temperature: float = Field(0.7)
        seed: int = Field(42)
        stream: bool = Field(True)
        max_tokens: int = Field(1024)
        model: str = Field(model_name)

    class MinifiedChatCompletion(MinifiedStreamChatCompletion):
        stream: bool = Field(False)

    # Minimal completion input.
    class MinifiedStreamCompletion(BaseModel):
        prompt: str
        temperature: float = Field(0.7)
        seed: int = Field(42)
        stream: bool = Field(True)
        max_tokens: int = Field(1024)
        model: str = Field(model_name)

    class MinifiedCompletion(MinifiedStreamCompletion):
        stream: bool = Field(False)

    @chute.on_startup()
    async def initialize_vllm(self):
        nonlocal engine_args
        nonlocal model_name
        nonlocal image

        # Imports here to avoid needing torch/vllm/etc. to just perform inference/build remotely.
        import torch
        import multiprocessing
        from vllm import AsyncEngineArgs, AsyncLLMEngine
        import vllm.entrypoints.openai.api_server as vllm_api_server
        from vllm.entrypoints.openai.serving_chat import OpenAIServingChat
        from vllm.entrypoints.openai.serving_completion import OpenAIServingCompletion
        import vllm.version as vv

        # Force download in initializer with some retries.
        from huggingface_hub import snapshot_download

        download_path = None
        for attempt in range(5):
            download_kwargs = {}
            if revision := engine_args.get("revision"):
                download_kwargs["revision"] = revision
            try:
                print(f"Attempting to download {model_name} to cache...")
                download_path = snapshot_download(repo_id=model_name, **download_kwargs)
                print(f"Successfully downloaded {model_name} to {download_path}")
                break
            except Exception as exc:
                print(f"Failed downloading {model_name} {download_kwargs or ''}: {exc}")
            await asyncio.sleep(60)
        if not download_path:
            raise Exception(f"Failed to download {model_name} after 5 attempts")

        try:
            from vllm.entrypoints.openai.serving_engine import BaseModelPath
        except Exception:
            from vllm.entrypoints.openai.serving_models import BaseModelPath, OpenAIServingModels
        from vllm.entrypoints.openai.serving_tokenization import OpenAIServingTokenization

        # Reset torch.
        torch.cuda.empty_cache()
        torch.cuda.init()
        torch.cuda.set_device(0)
        multiprocessing.set_start_method("spawn", force=True)

        # Tool args.
        if chat_template := engine_args.pop("chat_template", None):
            if len(chat_template) <= 1024 and os.path.exists(chat_template):
                with open(chat_template) as infile:
                    chat_template = infile.read()
        extra_args = dict(
            tool_parser=engine_args.pop("tool_call_parser", None),
            enable_auto_tools=engine_args.pop("enable_auto_tool_choice", False),
            chat_template=chat_template,
            chat_template_content_format=engine_args.pop("chat_template_content_format", None),
        )

        # Configure engine arguments
        gpu_count = int(os.getenv("CUDA_DEVICE_COUNT", str(torch.cuda.device_count())))
        engine_args = AsyncEngineArgs(
            model=model_name,
            tensor_parallel_size=gpu_count,
            **engine_args,
        )

        # Initialize engine directly in the main process
        self.engine = AsyncLLMEngine.from_engine_args(engine_args)
        model_config = await self.engine.get_model_config()

        base_model_paths = [
            BaseModelPath(name=chute.name, model_path=chute.name),
        ]

        self.include_router(vllm_api_server.router)
        extra_token_args = {}
        version_parts = vv.__version__.split(".")
        old_vllm = False
        if (
            not vv.__version__.startswith("0.1.dev")
            and int(version_parts[0]) == 0
            and int(version_parts[1]) < 7
        ):
            old_vllm = True
        if old_vllm:
            extra_args["lora_modules"] = []
            extra_args["prompt_adapters"] = []
            extra_token_args["lora_modules"] = []
            extra_args["base_model_paths"] = base_model_paths
        else:
            extra_args["models"] = OpenAIServingModels(
                engine_client=self.engine,
                model_config=model_config,
                base_model_paths=base_model_paths,
                lora_modules=[],
                prompt_adapters=[],
            )
            extra_token_args.update(
                {
                    "chat_template": extra_args.get("chat_template"),
                    "chat_template_content_format": extra_args.get("chat_template_content_format"),
                }
            )

        vllm_api_server.chat = lambda s: OpenAIServingChat(
            self.engine,
            model_config=model_config,
            response_role="assistant",
            request_logger=None,
            return_tokens_as_token_ids=True,
            **extra_args,
        )
        vllm_api_server.completion = lambda s: OpenAIServingCompletion(
            self.engine,
            model_config=model_config,
            request_logger=None,
            return_tokens_as_token_ids=True,
            **{
                k: v
                for k, v in extra_args.items()
                if k
                not in (
                    "chat_template",
                    "chat_template_content_format",
                    "tool_parser",
                    "enable_auto_tools",
                )
            },
        )
        vllm_api_server.tokenization = lambda s: OpenAIServingTokenization(
            self.engine,
            model_config,
            base_model_paths,
            request_logger=None,
            **extra_token_args,
        )
        self.state.openai_serving_tokenization = OpenAIServingTokenization(
            self.engine,
            model_config,
            base_model_paths,
            request_logger=None,
            **extra_token_args,
        )
        setattr(self.state, "enable_server_load_tracking", False)
        if not old_vllm:
            self.state.openai_serving_models = extra_args["models"]

    def _parse_stream_chunk(encoded_chunk):
        chunk = encoded_chunk if isinstance(encoded_chunk, str) else encoded_chunk.decode()
        if "data: {" in chunk:
            return json.loads(chunk[6:])
        return None

    @chute.cord(
        passthrough_path="/v1/chat/completions",
        public_api_path="/v1/chat/completions",
        method="POST",
        passthrough=True,
        stream=True,
        input_schema=ChatCompletionRequest,
        minimal_input_schema=MinifiedStreamChatCompletion,
    )
    async def chat_stream(encoded_chunk) -> ChatCompletionStreamResponse:
        return _parse_stream_chunk(encoded_chunk)

    @chute.cord(
        passthrough_path="/v1/completions",
        public_api_path="/v1/completions",
        method="POST",
        passthrough=True,
        stream=True,
        input_schema=CompletionRequest,
        minimal_input_schema=MinifiedStreamCompletion,
    )
    async def completion_stream(encoded_chunk) -> CompletionStreamResponse:
        return _parse_stream_chunk(encoded_chunk)

    @chute.cord(
        passthrough_path="/v1/chat/completions",
        public_api_path="/v1/chat/completions",
        method="POST",
        passthrough=True,
        input_schema=ChatCompletionRequest,
        minimal_input_schema=MinifiedChatCompletion,
    )
    async def chat(data) -> ChatCompletionResponse:
        return data
    
    @chute.cord(
        passthrough_path="/tokenize",
        public_api_path="/tokenize",
        method="POST",
        passthrough=True,
        input_schema=TokenizeRequest,
        minimal_input_schema=TokenizeRequest,
    )
    async def tokenize(data) -> TokenizeResponse:
        return data
    
    @chute.cord(
        passthrough_path="/detokenize",
        public_api_path="/detokenize",
        method="POST",
        passthrough=True,
        input_schema=DetokenizeRequest,
        minimal_input_schema=DetokenizeRequest,
    )
    async def detokenize(data) -> DetokenizeResponse:
        return data

    @chute.cord(
        passthrough_path="/v1/completions",
        public_api_path="/v1/completions",
        method="POST",
        passthrough=True,
        input_schema=CompletionRequest,
        minimal_input_schema=MinifiedCompletion,
    )
    async def completion(data) -> CompletionResponse:
        return data

    @chute.cord(
        passthrough_path="/v1/models",
        public_api_path="/v1/models",
        public_api_method="GET",
        method="GET",
        passthrough=True,
    )
    async def get_models(data):
        return data

    return VLLMChute(
        chute=chute,
        chat=chat,
        chat_stream=chat_stream,
        completion=completion,
        completion_stream=completion_stream,
        models=get_models,
    )
