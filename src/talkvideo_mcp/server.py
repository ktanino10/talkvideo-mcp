from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from mcp.server import Server, ServerRequestContext
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)
from pydantic import BaseModel, Field, ValidationError

from talkvideo_mcp import __version__
from talkvideo_mcp.engine import Engine
from talkvideo_mcp.errors import Problem, TalkVideoError
from talkvideo_mcp.models import (
    MAX_JSON_BYTES,
    Capabilities,
    Inspection,
    Job,
    JobId,
    Model,
    PreparedScript,
    ReviseInput,
    Revision,
    RevisionRef,
    SaveRevisionInput,
    ScriptInput,
    Stage,
)
from talkvideo_mcp.text import prepare_script

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)
logger = logging.getLogger(__name__)


class Response(Model, Generic[OutputT]):
    ok: bool
    data: OutputT | None = None
    error: Problem | None = None
    next_action: str
    needs_user_action: bool = False


class EmptyInput(Model):
    pass


class RevisionInput(Model):
    ref: RevisionRef


class JobInput(Model):
    job_id: JobId


class StartInput(RevisionInput):
    stage: Stage = "preview"


class InspectInput(RevisionInput):
    offset: int = Field(default=0, ge=0, le=1000, strict=True)
    limit: int = Field(default=20, ge=1, le=50, strict=True)


@dataclass(frozen=True)
class Binding:
    tool: Tool
    invoke: Callable[[dict[str, object]], Awaitable[CallToolResult]]


def bind(
    name: str,
    description: str,
    input_model: type[InputT],
    response_model: type[Response[OutputT]],
    handler: Callable[[InputT], Awaitable[OutputT]],
    *,
    read_only: bool,
    next_action: str,
    idempotent: bool = False,
    destructive: bool = False,
) -> Binding:
    async def invoke(arguments: dict[str, object]) -> CallToolResult:
        try:
            if len(json.dumps(arguments, ensure_ascii=False).encode("utf-8")) > MAX_JSON_BYTES:
                raise TalkVideoError(
                    "input_too_large",
                    "Tool arguments exceed the 1 MiB limit.",
                    "Use a shorter script; inputs are not truncated.",
                )
            request = input_model.model_validate(arguments)
            data = await handler(request)
            response = response_model(
                ok=True,
                data=data,
                next_action=data.next_action if isinstance(data, Job) else next_action,
                needs_user_action=data.needs_user_action if isinstance(data, Job) else False,
            )
        except ValidationError as exc:
            errors = exc.errors(include_input=False, include_context=False, include_url=False)
            locations = [".".join(str(item) for item in error["loc"]) for error in errors[:8]]
            problem = Problem(
                code="invalid_input",
                message="Invalid tool input fields: " + ", ".join(locations),
                next_action="Read tools/list for the exact schema and bounds; no input was echoed.",
            )
            response = response_model(ok=False, error=problem, next_action=problem.next_action)
        except TalkVideoError as exc:
            response = response_model(
                ok=False,
                error=exc.problem,
                next_action=exc.problem.next_action,
                needs_user_action=exc.problem.needs_user_action,
            )
        except Exception as exc:
            logger.error("tool_failed type=%s", type(exc).__name__)
            problem = Problem(
                code="internal_error",
                message="The tool failed. Private inputs and OS error details are suppressed.",
                next_action="Inspect durable job state; report the error code in this repo only.",
                needs_user_action=True,
            )
            response = response_model(
                ok=False, error=problem, next_action=problem.next_action, needs_user_action=True
            )
        text = response.model_dump_json()
        if len(text.encode("utf-8")) > MAX_JSON_BYTES:
            problem = Problem(
                code="response_too_large",
                message="The complete response exceeds the 1 MiB limit.",
                next_action="Use a smaller inspection page or shorter prepared script.",
            )
            response = response_model(ok=False, error=problem, next_action=problem.next_action)
            text = response.model_dump_json()
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content=response.model_dump(mode="json"),
            is_error=not response.ok,
        )

    return Binding(
        tool=Tool(
            name=name,
            description=description,
            input_schema=input_model.model_json_schema(),
            output_schema=response_model.model_json_schema(),
            annotations=ToolAnnotations(
                read_only_hint=read_only,
                destructive_hint=destructive,
                idempotent_hint=read_only or idempotent,
                open_world_hint=False,
            ),
        ),
        invoke=invoke,
    )


def build_server(engine: Engine) -> Server[None]:
    async def capabilities(_: EmptyInput) -> Capabilities:
        return engine.capabilities()

    async def prepare(request: ScriptInput) -> PreparedScript:
        return prepare_script(request)

    async def save(request: SaveRevisionInput) -> Revision:
        return engine.save_revision(request)

    async def revision(request: RevisionInput) -> Revision:
        return engine.get_revision(request.ref)

    async def audio(request: StartInput) -> Job:
        return engine.start_job(request.ref, "audio", request.stage)

    async def video(request: StartInput) -> Job:
        return engine.start_job(request.ref, "video", request.stage)

    async def status(request: JobInput) -> Job:
        return engine.get_job(request.job_id)

    async def cancel(request: JobInput) -> Job:
        return await engine.cancel_job(request.job_id)

    async def resume(request: JobInput) -> Job:
        return engine.resume_job(request.job_id)

    async def inspect(request: InspectInput) -> Inspection:
        return await engine.inspect_output(request.ref, request.offset, request.limit)

    async def revise(request: ReviseInput) -> Revision:
        return engine.revise_cues(request)

    bindings = [
        bind(
            "talkvideo_get_capabilities",
            "Read truthful backend availability, limits and review gates. Never generates media.",
            EmptyInput,
            Response[Capabilities],
            capabilities,
            read_only=True,
            next_action="Production is unavailable. Prepare scripts or explicitly use diagnostics.",
        ),
        bind(
            "talkvideo_prepare_script",
            "Read-only lossless Unicode cue splitting. Default normalization is none; "
            "returns display/spoken tracks, stable cue/chunk IDs and a plan digest. "
            "Provider limits are not verified. Does not write or generate audio.",
            ScriptInput,
            Response[PreparedScript],
            prepare,
            read_only=True,
            next_action="Review both tracks and normalization, then save the exact plan digest.",
        ),
        bind(
            "talkvideo_save_revision",
            "Save an immutable script/settings revision under output/<video>/<revision>. "
            "No media is generated. Production stays unavailable; diagnostics need server opt-in.",
            SaveRevisionInput,
            Response[Revision],
            save,
            read_only=False,
            next_action="The user reviews this revision using the interactive local review CLI.",
        ),
        bind(
            "talkvideo_get_revision",
            "Read an immutable revision, display/spoken text and cue IDs; verify its digest.",
            RevisionInput,
            Response[Revision],
            revision,
            read_only=True,
            next_action="Use stable cue IDs for corrections; never edit stored manifests.",
        ),
        bind(
            "talkvideo_start_audio_job",
            "Start/return a durable sequential diagnostic audio job and return immediately. "
            "Preview uses at most 3 whole chunks. Full audio requires script and preview review. "
            "Production speech always errors. Repeated start returns the same job; use resume.",
            StartInput,
            Response[Job],
            audio,
            read_only=False,
            idempotent=True,
            next_action="Poll get_job; a started job is not a completed output.",
        ),
        bind(
            "talkvideo_start_video_job",
            "Start/return a local diagnostic test-pattern MP4 job from validated audio. "
            "Not face animation or lip-sync. Full video also needs a video-preview review. "
            "Real-person generation is unavailable; no footage is accepted or uploaded.",
            StartInput,
            Response[Job],
            video,
            read_only=False,
            idempotent=True,
            next_action="Poll get_job; output is a diagnostic pattern, not production video.",
        ),
        bind(
            "talkvideo_get_job",
            "Read durable state, progress, remaining action and failure classification by job ID.",
            JobInput,
            Response[Job],
            status,
            read_only=True,
            next_action="Follow the job's next_action; do not infer success from file existence.",
        ),
        bind(
            "talkvideo_cancel_job",
            "Cancel a queued/running job and reap its local process group. Keeps completed chunks. "
            "A possibly submitted external request is not automatically retried.",
            JobInput,
            Response[Job],
            cancel,
            read_only=False,
            idempotent=True,
            destructive=True,
            next_action="Inspect the retained job; resume only unchanged verified input.",
        ),
        bind(
            "talkvideo_resume_job",
            "Resume the same immutable job after checking hashes, settings and review receipts. "
            "Keeps retry budgets; never repeats ambiguous submissions. Edits need a new revision.",
            JobInput,
            Response[Job],
            resume,
            read_only=False,
            next_action="Poll get_job and inspect output once succeeded.",
        ),
        bind(
            "talkvideo_inspect_output",
            "Read/verify hashes, PCM frames, assembly/timing and paginated artifact metadata. "
            "Selected MP4s are locally probed. This is not a perceptual media-quality review.",
            InspectInput,
            Response[Inspection],
            inspect,
            read_only=True,
            next_action="Present local artifacts for human review; do not claim perceived quality.",
        ),
        bind(
            "talkvideo_revise_cues",
            "Create a new revision for targeted cue edits. Keeps cue IDs; copies only verified "
            "unchanged audio chunks. Invalidates assemblies, downstream timing and all approvals.",
            ReviseInput,
            Response[Revision],
            revise,
            read_only=False,
            next_action="Review the new revision and its preview; the original remains unchanged.",
        ),
    ]
    tools = {binding.tool.name: binding for binding in bindings}

    async def list_tools(
        _: ServerRequestContext[None], params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        if params is not None and params.cursor is not None:
            return ListToolsResult(tools=[])
        return ListToolsResult(tools=[binding.tool for binding in bindings])

    async def call_tool(
        _: ServerRequestContext[None], params: CallToolRequestParams
    ) -> CallToolResult:
        binding = tools.get(params.name)
        if binding is None:
            problem = Problem(
                code="unknown_tool",
                message="This tool is not registered.",
                next_action="Read tools/list and use an actual registered tool name.",
            )
            response = Response[EmptyInput](
                ok=False, error=problem, next_action=problem.next_action
            )
            return CallToolResult(
                content=[TextContent(type="text", text=response.model_dump_json())],
                structured_content=response.model_dump(mode="json"),
                is_error=True,
            )
        return await binding.invoke(params.arguments or {})

    return Server(
        "talkvideo_mcp",
        version=__version__,
        instructions=(
            "Local-only independent workflow. Production voice and real-person lip-sync are "
            "unavailable. Diagnostic tones/patterns are not speech or perceptual quality evidence. "
            "Only the user records local reviews; tool arguments cannot grant rights. "
            "Do not upload media, contact upstream authors, or substitute voices/services."
        ),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
