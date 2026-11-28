from fastapi import APIRouter

from ai_service.infra.logger import LOG
from ai_service.schemas.api import AgentRunRequest, AgentRunResponse, ReplyType

router = APIRouter(tags=["Ai Service 运行路由"])


@router.post(
    "/internal/v1/agent/runs",
    response_model=AgentRunResponse,
)
async def start_run_endpoint(
    payload: AgentRunRequest,
) -> AgentRunResponse:
    LOG.info(
        f"Agent run request: conversation_id={payload.conversation_id} "
        f"turn_id={payload.turn_id} revision={payload.turn_revision}"
    )

    reply_text = payload.messages[-1].content.get("text", "")
    return AgentRunResponse(
        status="success",
        revision=payload.turn_revision,
        reply_type=ReplyType.ANSWER,
        reply_text=f"Get your question: {reply_text}",
    )
