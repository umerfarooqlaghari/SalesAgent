from typing import Annotated, List, Optional
from typing_extensions import TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

class LeadProfile(BaseModel):
    company: Optional[str] = None
    job_title: Optional[str] = None
    intent_score: int = 0
    status: str = "New"  # "New", "Qualified", "Unqualified", "Handoff"
    fit: Optional[bool] = None

class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    intent: Optional[str]  # "Purchase", "Inquiry", "Support"
    lead_profile: LeadProfile
    requires_handoff: bool
    thread_id: str
