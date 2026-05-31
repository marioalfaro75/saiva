"""File import: sniff (guided mapping), preview (with dedup), commit (PRD R4–R8)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user, require_writer
from ..services import audit, importers
from ..services.categorise import build_categoriser
from ..services.transfers import detect_transfers

router = APIRouter(prefix="/imports", tags=["imports"])
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _parse_mapping(mapping_json: str | None) -> schemas.CsvMapping | None:
    if not mapping_json:
        return None
    try:
        data = json.loads(mapping_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid mapping JSON") from exc
    return schemas.CsvMapping.model_validate(data)


def _account_or_404(db: Session, account_id: str, household_id: str) -> models.Account:
    account = db.get(models.Account, account_id)
    if account is None or account.household_id != household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return account


async def _read(file: UploadFile) -> bytes:
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File too large (max 10 MB)")
    return content


def _category_names(db: Session, household_id: str) -> dict[str, str]:
    return {
        c.id: c.name
        for c in db.execute(
            select(models.Category).where(models.Category.household_id == household_id)
        )
        .scalars()
        .all()
    }


def _existing_hashes(db: Session, account_id: str) -> set[str]:
    return {
        r[0]
        for r in db.execute(
            select(models.Transaction.dedup_hash).where(
                models.Transaction.account_id == account_id
            )
        ).all()
    }


@router.post("/sniff", response_model=schemas.ImportSniffOut)
async def sniff(
    file: UploadFile = File(...),
    user: models.User = Depends(get_current_user),
) -> schemas.ImportSniffOut:
    return importers.sniff_csv(await _read(file))


@router.post("/preview", response_model=schemas.ImportPreviewOut)
async def preview(
    file: UploadFile = File(...),
    account_id: str = Form(...),
    file_format: str = Form("csv"),
    mapping: str | None = Form(None),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.ImportPreviewOut:
    _account_or_404(db, account_id, user.household_id)
    content = await _read(file)
    try:
        parsed = importers.parse_file(content, file_format, _parse_mapping(mapping))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    categoriser = build_categoriser(db, user.household_id)
    names = _category_names(db, user.household_id)
    existing = _existing_hashes(db, account_id)

    new_rows: list[schemas.PreviewRow] = []
    seen: set[str] = set()
    duplicates = 0
    for p in parsed:
        h = importers.dedup_hash(account_id, p.txn_date, p.amount_cents, p.raw_description)
        if h in existing or h in seen:
            duplicates += 1
            continue
        seen.add(h)
        result = categoriser.categorise(p.raw_description, p.merchant)
        new_rows.append(
            schemas.PreviewRow(
                txn_date=p.txn_date,
                amount_cents=p.amount_cents,
                raw_description=p.raw_description,
                merchant=p.merchant,
                suggested_category_id=result.category_id,
                suggested_category_name=(
                    names.get(result.category_id) if result.category_id else None
                ),
                confidence=result.confidence if result.category_id else None,
                is_duplicate=False,
            )
        )
    return schemas.ImportPreviewOut(
        account_id=account_id,
        file_format=file_format,
        total_rows=len(parsed),
        new_rows=new_rows,
        duplicate_count=duplicates,
    )


@router.post("/commit", response_model=schemas.ImportCommitOut)
async def commit(
    file: UploadFile = File(...),
    account_id: str = Form(...),
    file_format: str = Form("csv"),
    mapping: str | None = Form(None),
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.ImportCommitOut:
    _account_or_404(db, account_id, user.household_id)
    content = await _read(file)
    try:
        parsed = importers.parse_file(content, file_format, _parse_mapping(mapping))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    categoriser = build_categoriser(db, user.household_id)
    existing = _existing_hashes(db, account_id)

    batch = models.ImportBatch(
        household_id=user.household_id,
        account_id=account_id,
        filename=file.filename or "upload",
        file_format=file_format.lower(),
        created_by_user_id=user.id,
    )
    db.add(batch)
    db.flush()

    added = 0
    skipped = 0
    seen: set[str] = set()
    for p in parsed:
        h = importers.dedup_hash(account_id, p.txn_date, p.amount_cents, p.raw_description)
        if h in existing or h in seen:
            skipped += 1
            continue
        seen.add(h)
        result = categoriser.categorise(p.raw_description, p.merchant)
        db.add(
            models.Transaction(
                household_id=user.household_id,
                account_id=account_id,
                txn_date=p.txn_date,
                amount_cents=p.amount_cents,
                raw_description=p.raw_description,
                merchant=p.merchant,
                category_id=result.category_id,
                confidence=result.confidence if result.category_id else None,
                source="import",
                dedup_hash=h,
                import_batch_id=batch.id,
            )
        )
        added += 1

    batch.added_count = added
    batch.skipped_count = skipped
    db.commit()
    transfers_linked = detect_transfers(db, user.household_id)
    audit.record(
        db, action="import_commit", household_id=user.household_id, actor_user_id=user.id,
        entity="import_batch", entity_id=batch.id, detail={"added": added, "skipped": skipped},
    )
    return schemas.ImportCommitOut(
        batch_id=batch.id, added=added, skipped=skipped, transfers_linked=transfers_linked
    )
