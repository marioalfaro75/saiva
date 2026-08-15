"""File import: sniff (guided mapping), preview (with dedup), commit (PRD R4–R8)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user, require_writer
from ..services import audit, dedup, importers
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


def _decide(
    db: Session, account_id: str, parsed: list[importers.ParsedTxn]
) -> list[tuple[int, importers.ParsedTxn, dedup.Verdict]]:
    """Resolve every parsed row to a duplicate verdict, in file order."""
    deduper = dedup.Deduper(db)
    return [(i, p, deduper.match(account_id, p)) for i, p in enumerate(parsed)]


def _index_set(raw: str | None) -> set[int]:
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid decisions JSON") from exc
    if not isinstance(data, list) or not all(isinstance(i, int) for i in data):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Decisions must be a list of row indexes")
    return set(data)


def _will_import(verdict: dedup.Verdict, index: int, force: set[int], skip: set[int]) -> bool:
    """Definite duplicates are never imported — allowing that would knowingly create
    a double-up. Probable ones are skipped unless the reviewer opts them in."""
    if index in skip:
        return False
    if verdict.status == dedup.NEW:
        return True
    if verdict.status == dedup.DUPLICATE_PROBABLE:
        return index in force
    return False


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

    rows: list[schemas.PreviewRow] = []
    for index, p, verdict in _decide(db, account_id, parsed):
        result = categoriser.categorise(p.raw_description, p.merchant)
        rows.append(
            schemas.PreviewRow(
                row_index=index,
                txn_date=p.txn_date,
                amount_cents=p.amount_cents,
                raw_description=p.raw_description,
                merchant=p.merchant,
                suggested_category_id=result.category_id,
                suggested_category_name=(
                    names.get(result.category_id) if result.category_id else None
                ),
                confidence=result.confidence if result.category_id else None,
                is_duplicate=verdict.is_duplicate,
                status=verdict.status,
                duplicate_reason=verdict.reason,
                matched_txn_id=verdict.matched.id if verdict.matched else None,
                matched_date=verdict.matched.txn_date if verdict.matched else None,
                matched_description=(
                    verdict.matched.raw_description if verdict.matched else None
                ),
                will_import=_will_import(verdict, index, set(), set()),
            )
        )
    probable = sum(1 for r in rows if r.status == dedup.DUPLICATE_PROBABLE)
    duplicates = sum(1 for r in rows if r.is_duplicate)
    return schemas.ImportPreviewOut(
        account_id=account_id,
        file_format=file_format,
        total_rows=len(parsed),
        rows=rows,
        new_count=len(rows) - duplicates,
        duplicate_count=duplicates,
        probable_count=probable,
    )


@router.post("/commit", response_model=schemas.ImportCommitOut)
async def commit(
    file: UploadFile = File(...),
    account_id: str = Form(...),
    file_format: str = Form("csv"),
    mapping: str | None = Form(None),
    force_import: str | None = Form(None),
    force_skip: str | None = Form(None),
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.ImportCommitOut:
    _account_or_404(db, account_id, user.household_id)
    content = await _read(file)
    try:
        parsed = importers.parse_file(content, file_format, _parse_mapping(mapping))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    forced = _index_set(force_import)
    skipped_rows = _index_set(force_skip)
    categoriser = build_categoriser(db, user.household_id)

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
    for index, p, verdict in _decide(db, account_id, parsed):
        if not _will_import(verdict, index, forced, skipped_rows):
            skipped += 1
            continue
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
                dedup_hash=importers.dedup_hash(
                    account_id, p.txn_date, p.amount_cents, p.raw_description
                ),
                provider_txn_id=p.provider_txn_id,
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
