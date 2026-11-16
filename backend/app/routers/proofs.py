from fastapi import APIRouter, HTTPException

from app.services import pipeline

router = APIRouter(tags=["主旁路隔离证明"])


@router.get("/isolation-proofs")
def list_isolation_proofs(domain_id: int | None = None, lane: str | None = None):
    """隔离证明列表，只读；供界面按通道区分主/旁路结果。"""
    return pipeline.list_isolation_proofs(domain_id, lane)


@router.get("/isolation-proofs/{job_id}")
def get_isolation_proof(job_id: int):
    """按任务 id 只读查询主旁路隔离证明；只读取，不写库、不改任务状态。"""
    proof = pipeline.get_isolation_proof(job_id)
    if not proof:
        raise HTTPException(status_code=404, detail="该任务暂无隔离证明")
    return proof
