from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Generator, Literal, Optional
from urllib.parse import unquote
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


ROOT = Path(__file__).resolve().parent
engine = create_engine(f"sqlite:///{ROOT / 'supply_chain.db'}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Receivable(Base):
    __tablename__ = "receivables"

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    supplier: Mapped[str] = mapped_column(String(80))
    core_enterprise: Mapped[str] = mapped_column(String(80))
    amount: Mapped[float] = mapped_column(Float)
    due_date: Mapped[date] = mapped_column(Date)
    contract_no: Mapped[str] = mapped_column(String(80))
    file_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    loans: Mapped[list["Loan"]] = relationship(back_populates="receivable")


class Loan(Base):
    __tablename__ = "loans"

    id: Mapped[int] = mapped_column(primary_key=True)
    loan_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    receivable_id: Mapped[int] = mapped_column(ForeignKey("receivables.id"))
    loan_amount: Mapped[float] = mapped_column(Float)
    risk_score: Mapped[int] = mapped_column(Integer)
    risk_level: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), default="APPLIED")
    interest_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    receivable: Mapped[Receivable] = relationship(back_populates="loans")


class ChainEvent(Base):
    __tablename__ = "chain_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(32), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(String(80))
    role: Mapped[str] = mapped_column(String(30))
    payload_hash: Mapped[str] = mapped_column(String(64))
    tx_hash: Mapped[str] = mapped_column(String(66), unique=True)
    block_height: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


Base.metadata.create_all(engine)


def db_session() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def identity(
    x_role: str = Header(default="供应商"),
    x_actor: str = Header(default="华星电子"),
) -> tuple[str, str]:
    return unquote(x_role), unquote(x_actor)


def require(identity_data: tuple[str, str], *roles: str) -> tuple[str, str]:
    if identity_data[0] not in roles:
        raise HTTPException(403, f"当前角色“{identity_data[0]}”无权执行此操作")
    return identity_data


def chain_write(db: Session, asset_id: str, event_type: str, actor: str, role: str, payload: dict) -> ChainEvent:
    payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    payload_hash = hashlib.sha256(payload_text.encode()).hexdigest()
    block_height = (db.scalar(select(func.max(ChainEvent.block_height))) or 102400) + 1
    tx_hash = "0x" + hashlib.sha256(f"{asset_id}{event_type}{actor}{datetime.now().isoformat()}{uuid4()}".encode()).hexdigest()
    event = ChainEvent(
        asset_id=asset_id,
        event_type=event_type,
        actor=actor,
        role=role,
        payload_hash=payload_hash,
        tx_hash=tx_hash,
        block_height=block_height,
    )
    db.add(event)
    return event


def risk_for(receivable: Receivable, amount: float) -> tuple[int, str, list[str]]:
    days = (receivable.due_date - date.today()).days
    ratio = amount / receivable.amount
    score = 88
    reasons: list[str] = []
    if days < 30:
        score -= 18
        reasons.append("距到期日不足30天")
    elif days < 90:
        score -= 8
        reasons.append("账款剩余期限较短")
    if ratio > 0.8:
        score -= 16
        reasons.append("融资比例超过账款金额的80%")
    elif ratio > 0.6:
        score -= 7
        reasons.append("融资比例偏高")
    if receivable.status != "CONFIRMED":
        score -= 30
        reasons.append("账款尚未完成核心企业确权")
    score = max(0, min(100, score))
    level = "低风险" if score >= 75 else "中风险" if score >= 55 else "高风险"
    return score, level, reasons or ["账款期限与融资比例均处于合理区间"]


def receivable_out(row: Receivable) -> dict:
    return {
        "asset_id": row.asset_id,
        "supplier": row.supplier,
        "core_enterprise": row.core_enterprise,
        "amount": row.amount,
        "due_date": row.due_date,
        "contract_no": row.contract_no,
        "file_hash": row.file_hash,
        "status": row.status,
        "created_at": row.created_at,
    }


def loan_out(row: Loan) -> dict:
    return {
        "loan_id": row.loan_id,
        "asset_id": row.receivable.asset_id,
        "supplier": row.receivable.supplier,
        "loan_amount": row.loan_amount,
        "risk_score": row.risk_score,
        "risk_level": row.risk_level,
        "status": row.status,
        "interest_rate": row.interest_rate,
        "created_at": row.created_at,
    }


class ReceivableCreate(BaseModel):
    supplier: str = Field(min_length=2, max_length=80)
    core_enterprise: str = Field(min_length=2, max_length=80)
    amount: float = Field(gt=0)
    due_date: date
    contract_no: str = Field(min_length=2, max_length=80)


class ConfirmRequest(BaseModel):
    result: Literal["CONFIRMED", "REJECTED"]


class LoanCreate(BaseModel):
    asset_id: str
    loan_amount: float = Field(gt=0)


class ApprovalRequest(BaseModel):
    result: Literal["APPROVED", "REJECTED"]
    interest_rate: Optional[float] = Field(default=None, ge=0, le=30)


app = FastAPI(title="供应链金融平台", version="1.0.0")


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/dashboard")
def dashboard(db: Session = Depends(db_session)) -> dict:
    receivables = db.scalars(select(Receivable).order_by(Receivable.created_at.desc())).all()
    loans = db.scalars(select(Loan).order_by(Loan.created_at.desc())).all()
    events = db.scalars(select(ChainEvent).order_by(ChainEvent.block_height.desc()).limit(8)).all()
    return {
        "metrics": {
            "receivable_count": len(receivables),
            "receivable_amount": round(sum(x.amount for x in receivables), 2),
            "loan_amount": round(sum(x.loan_amount for x in loans if x.status != "REJECTED"), 2),
            "chain_events": db.scalar(select(func.count(ChainEvent.id))) or 0,
        },
        "receivables": [receivable_out(x) for x in receivables],
        "loans": [loan_out(x) for x in loans],
        "events": [event_dict(x) for x in events],
    }


@app.post("/api/receivables")
def create_receivable(
    body: ReceivableCreate,
    who: tuple[str, str] = Depends(identity),
    db: Session = Depends(db_session),
) -> dict:
    role, actor = require(who, "供应商", "管理员")
    if body.due_date <= date.today():
        raise HTTPException(400, "到期日必须晚于今天")
    asset_id = "AR" + datetime.now().strftime("%Y%m%d") + uuid4().hex[:4].upper()
    file_hash = hashlib.sha256(f"{body.contract_no}|{body.supplier}|{body.amount}".encode()).hexdigest()
    row = Receivable(asset_id=asset_id, file_hash=file_hash, status="PENDING", **body.model_dump())
    db.add(row)
    chain_write(db, asset_id, "RECEIVABLE_REGISTERED", actor, role, body.model_dump())
    db.commit()
    return receivable_out(row)


@app.post("/api/receivables/{asset_id}/confirm")
def confirm_receivable(
    asset_id: str,
    body: ConfirmRequest,
    who: tuple[str, str] = Depends(identity),
    db: Session = Depends(db_session),
) -> dict:
    role, actor = require(who, "核心企业", "管理员")
    row = db.scalar(select(Receivable).where(Receivable.asset_id == asset_id))
    if not row:
        raise HTTPException(404, "未找到该应收账款")
    if role == "核心企业" and row.core_enterprise != actor:
        raise HTTPException(403, "只能确权本企业相关账款")
    row.status = body.result
    chain_write(db, asset_id, "RECEIVABLE_CONFIRMED" if body.result == "CONFIRMED" else "RECEIVABLE_REJECTED", actor, role, body.model_dump())
    db.commit()
    return receivable_out(row)


@app.post("/api/loans/apply")
def apply_loan(
    body: LoanCreate,
    who: tuple[str, str] = Depends(identity),
    db: Session = Depends(db_session),
) -> dict:
    role, actor = require(who, "供应商", "管理员")
    receivable = db.scalar(select(Receivable).where(Receivable.asset_id == body.asset_id))
    if not receivable:
        raise HTTPException(404, "未找到该应收账款")
    if role == "供应商" and receivable.supplier != actor:
        raise HTTPException(403, "只能使用本企业账款申请融资")
    if body.loan_amount > receivable.amount:
        raise HTTPException(400, "融资金额不能超过账款金额")
    if receivable.status != "CONFIRMED":
        raise HTTPException(400, "账款完成核心企业确权后方可融资")
    score, level, reasons = risk_for(receivable, body.loan_amount)
    row = Loan(
        loan_id="LN" + datetime.now().strftime("%Y%m%d") + uuid4().hex[:4].upper(),
        receivable=receivable,
        loan_amount=body.loan_amount,
        risk_score=score,
        risk_level=level,
    )
    db.add(row)
    chain_write(db, body.asset_id, "FINANCING_APPLIED", actor, role, {**body.model_dump(), "risk_score": score, "risk_level": level})
    db.commit()
    result = loan_out(row)
    result["risk_reasons"] = reasons
    return result


@app.post("/api/loans/{loan_id}/approve")
def approve_loan(
    loan_id: str,
    body: ApprovalRequest,
    who: tuple[str, str] = Depends(identity),
    db: Session = Depends(db_session),
) -> dict:
    role, actor = require(who, "金融机构", "管理员")
    row = db.scalar(select(Loan).where(Loan.loan_id == loan_id))
    if not row:
        raise HTTPException(404, "未找到该融资申请")
    if body.result == "APPROVED" and body.interest_rate is None:
        raise HTTPException(400, "审批通过时必须填写融资利率")
    row.status = body.result
    row.interest_rate = body.interest_rate if body.result == "APPROVED" else None
    chain_write(db, row.receivable.asset_id, "FINANCING_" + body.result, actor, role, body.model_dump())
    db.commit()
    return loan_out(row)


def event_dict(row: ChainEvent) -> dict:
    return {
        "event_type": row.event_type,
        "asset_id": row.asset_id,
        "actor": row.actor,
        "role": row.role,
        "payload_hash": row.payload_hash,
        "tx_hash": row.tx_hash,
        "block_height": row.block_height,
        "created_at": row.created_at,
    }


@app.get("/api/audit/{asset_id}")
def audit(asset_id: str, db: Session = Depends(db_session)) -> dict:
    receivable = db.scalar(select(Receivable).where(Receivable.asset_id == asset_id))
    if not receivable:
        raise HTTPException(404, "未找到该业务编号")
    events = db.scalars(select(ChainEvent).where(ChainEvent.asset_id == asset_id).order_by(ChainEvent.block_height)).all()
    valid = all(e.payload_hash and len(e.payload_hash) == 64 for e in events)
    return {"asset": receivable_out(receivable), "chain_valid": valid, "events": [event_dict(x) for x in events]}


def seed() -> None:
    with SessionLocal() as db:
        if db.scalar(select(func.count(Receivable.id))):
            return
        sample = Receivable(
            asset_id="AR202606001",
            supplier="华星电子",
            core_enterprise="远航科技",
            amount=500000,
            due_date=date.today() + timedelta(days=180),
            contract_no="HT-2026-0618",
            file_hash=hashlib.sha256(b"demo-contract").hexdigest(),
            status="CONFIRMED",
        )
        db.add(sample)
        chain_write(db, sample.asset_id, "RECEIVABLE_REGISTERED", "华星电子", "供应商", {"amount": 500000})
        chain_write(db, sample.asset_id, "RECEIVABLE_CONFIRMED", "远航科技", "核心企业", {"result": "CONFIRMED"})
        db.commit()


seed()
