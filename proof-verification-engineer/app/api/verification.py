from fastapi import APIRouter
from pydantic import BaseModel

from app.core.contracts import contract_to_dict
from app.llm.nebius import generate_contract_with_nebius

router = APIRouter(prefix="/api", tags=["verification"])

class ContractRequest(BaseModel):
    goal: str

@router.post("/contracts/generate")
def generate_contract(request: ContractRequest) -> dict:
    return contract_to_dict(generate_contract_with_nebius(request.goal))
