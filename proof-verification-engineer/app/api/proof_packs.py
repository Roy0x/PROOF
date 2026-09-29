from fastapi import APIRouter, HTTPException

from app.storage.sqlite import get_pack, list_packs

router = APIRouter(prefix="/api", tags=["proof-packs"])


@router.get("/proof-packs")
def proof_packs() -> list[dict]:
    return list_packs()


@router.get("/proof-packs/{pack_id}")
def proof_pack(pack_id: int) -> dict:
    pack = get_pack(pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="Proof Pack not found")
    pack["proof_id"] = pack_id
    return pack
