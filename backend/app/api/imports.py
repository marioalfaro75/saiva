"""File import: sniff (guided mapping), preview (with dedup), commit (PRD R4–R8)."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

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


def _suggest_account(value: str, accounts: Sequence[models.Account]) -> str | None:
    """Best existing account for an account-column value. Statement columns usually
    carry something like "Everyday 062-000 12345678", so a contained account name is
    a strong signal; otherwise fall back to overall similarity."""
    norm = dedup.fuzzy_norm(value)
    if not norm:
        return None
    best: str | None = None
    best_score = 0.0
    for a in accounts:
        name = dedup.fuzzy_norm(a.name)
        if not name:
            continue
        score = 1.0 if name in norm or norm in name else dedup.similarity(norm, name)
        if score > best_score:
            best, best_score = a.id, score
    return best if best_score >= 0.6 else None


def _remembered_assignments(db: Session, household_id: str, valid: set[str]) -> dict[str, str]:
    """The value -> account map from the most recent multi-account import, so a
    repeat of the same statement does not have to be mapped again."""
    batch = (
        db.execute(
            select(models.ImportBatch)
            .where(
                models.ImportBatch.household_id == household_id,
                models.ImportBatch.mapping_profile.is_not(None),
            )
            .order_by(models.ImportBatch.created_at.desc())
        )
        .scalars()
        .first()
    )
    saved = (batch.mapping_profile or {}).get("account_assignments") if batch else None
    if not isinstance(saved, dict):
        return {}
    # An account may have been deleted since; drop stale entries rather than 404 later.
    return {k: v for k, v in saved.items() if isinstance(v, str) and v in valid}


def _account_summaries(rows: list[schemas.PreviewRow]) -> list[schemas.ImportAccountSummary]:
    order: list[str] = []
    counts: dict[str, list[int]] = {}
    for r in rows:
        if r.account_name is None:
            continue
        if r.account_name not in counts:
            counts[r.account_name] = [0, 0]
            order.append(r.account_name)
        counts[r.account_name][0 if r.status == dedup.NEW else 1] += 1
    ids = {r.account_name: r.account_id for r in rows if r.account_name}
    return [
        schemas.ImportAccountSummary(
            account_id=ids.get(name),
            account_name=name,
            new_count=counts[name][0],
            duplicate_count=counts[name][1],
        )
        for name in order
    ]


@dataclass
class _Target:
    """Where the rows carrying one account-column value are headed."""

    name: str
    account_id: str | None = None  # None while an account is only proposed
    skip: bool = False
    # Key the deduper matches against. A not-yet-created account has no stored
    # transactions, so a synthetic key correctly yields "everything is new".
    dedup_key: str = ""


_UNASSIGNED = _Target(name="Unassigned", skip=True, dedup_key="")


def _parse_assignments(raw: str | None) -> list[schemas.AccountAssignment]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid assignments JSON") from exc
    if not isinstance(data, list):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Assignments must be a list")
    return [schemas.AccountAssignment.model_validate(a) for a in data]


def _build_targets(
    db: Session, household_id: str, assignments: list[schemas.AccountAssignment], *, create: bool
) -> dict[str, _Target]:
    """Map each account-column value to its destination.

    With `create=False` (preview) a requested new account is only described, never
    written; with `create=True` (commit) it is created and the rows land in it.
    """
    targets: dict[str, _Target] = {}
    for a in assignments:
        if a.skip:
            targets[a.value] = _Target(name="Skipped", skip=True)
        elif a.account_id:
            account = _account_or_404(db, a.account_id, household_id)
            targets[a.value] = _Target(
                name=account.name, account_id=account.id, dedup_key=account.id
            )
        elif a.create:
            if create:
                account = models.Account(
                    household_id=household_id,
                    name=a.create.name,
                    type=a.create.type,
                    institution=a.create.institution,
                )
                db.add(account)
                db.flush()
                targets[a.value] = _Target(
                    name=account.name, account_id=account.id, dedup_key=account.id
                )
            else:
                targets[a.value] = _Target(name=a.create.name, dedup_key=f"__new__:{a.value}")
        else:
            targets[a.value] = _Target(name="Unassigned", skip=True)
    return targets


def _decide(
    db: Session,
    parsed: list[importers.ParsedTxn],
    resolve: Callable[[importers.ParsedTxn], _Target],
) -> list[tuple[int, importers.ParsedTxn, _Target, dedup.Verdict | None]]:
    """Resolve every parsed row to its account and duplicate verdict, in file order.
    Skipped rows get no verdict — there is nothing to compare them against."""
    deduper = dedup.Deduper(db)
    out = []
    for i, p in enumerate(parsed):
        target = resolve(p)
        verdict = None if target.skip else deduper.match(target.dedup_key, p)
        out.append((i, p, target, verdict))
    return out


def _resolver(
    mapping: schemas.CsvMapping | None, targets: dict[str, _Target], single: _Target
) -> Callable[[importers.ParsedTxn], _Target]:
    if mapping is None or mapping.account_col is None:
        return lambda _p: single
    return lambda p: targets.get((p.account_value or "").strip(), _UNASSIGNED)


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


def _will_import(
    verdict: dedup.Verdict | None, index: int, force: set[int], skip: set[int]
) -> bool:
    """Definite duplicates are never imported — allowing that would knowingly create
    a double-up. Probable ones are skipped unless the reviewer opts them in."""
    if verdict is None or index in skip:  # no verdict = the row's account was skipped
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


@router.post("/accounts/scan", response_model=list[schemas.AccountScanRow])
async def scan_accounts(
    file: UploadFile = File(...),
    mapping: str = Form(...),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[schemas.AccountScanRow]:
    """List the distinct values of the chosen account column, each matched to the
    account it most likely belongs to (remembered from a previous import, else by
    name), so the user confirms rather than types."""
    parsed_mapping = _parse_mapping(mapping)
    if parsed_mapping is None or parsed_mapping.account_col is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Choose the column that identifies the account"
        )
    content = await _read(file)
    try:
        found = importers.scan_account_values(content, parsed_mapping)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    accounts = (
        db.execute(select(models.Account).where(models.Account.household_id == user.household_id))
        .scalars()
        .all()
    )
    remembered = _remembered_assignments(db, user.household_id, {a.id for a in accounts})
    return [
        schemas.AccountScanRow(
            value=value,
            row_count=count,
            sample_description=sample,
            suggested_account_id=remembered.get(value) or _suggest_account(value, accounts),
        )
        for value, count, sample in found
    ]


@router.post("/preview", response_model=schemas.ImportPreviewOut)
async def preview(
    file: UploadFile = File(...),
    account_id: str | None = Form(None),
    file_format: str = Form("csv"),
    mapping: str | None = Form(None),
    assignments: str | None = Form(None),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.ImportPreviewOut:
    parsed_mapping = _parse_mapping(mapping)
    multi = parsed_mapping is not None and parsed_mapping.account_col is not None
    single = _Target(name="", skip=True)
    if not multi:
        if not account_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose an account")
        account = _account_or_404(db, account_id, user.household_id)
        single = _Target(name=account.name, account_id=account.id, dedup_key=account.id)
    targets = _build_targets(
        db, user.household_id, _parse_assignments(assignments), create=False
    )

    content = await _read(file)
    try:
        parsed = importers.parse_file(content, file_format, parsed_mapping)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    categoriser = build_categoriser(db, user.household_id)
    names = _category_names(db, user.household_id)

    rows: list[schemas.PreviewRow] = []
    resolve = _resolver(parsed_mapping, targets, single)
    for index, p, target, verdict in _decide(db, parsed, resolve):
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
                is_duplicate=verdict.is_duplicate if verdict else False,
                status=verdict.status if verdict else "unassigned",
                duplicate_reason=verdict.reason if verdict else None,
                matched_txn_id=verdict.matched.id if verdict and verdict.matched else None,
                matched_date=verdict.matched.txn_date if verdict and verdict.matched else None,
                matched_description=(
                    verdict.matched.raw_description if verdict and verdict.matched else None
                ),
                will_import=_will_import(verdict, index, set(), set()),
                account_id=target.account_id,
                account_name=target.name if not target.skip else None,
            )
        )
    probable = sum(1 for r in rows if r.status == dedup.DUPLICATE_PROBABLE)
    duplicates = sum(1 for r in rows if r.is_duplicate)
    unassigned = sum(1 for r in rows if r.status == "unassigned")
    return schemas.ImportPreviewOut(
        account_id=account_id if not multi else None,
        file_format=file_format,
        total_rows=len(parsed),
        rows=rows,
        new_count=sum(1 for r in rows if r.status == dedup.NEW),
        duplicate_count=duplicates,
        probable_count=probable,
        accounts=_account_summaries(rows) if multi else [],
        unassigned_count=unassigned,
    )


@router.post("/commit", response_model=schemas.ImportCommitOut)
async def commit(
    file: UploadFile = File(...),
    account_id: str | None = Form(None),
    file_format: str = Form("csv"),
    mapping: str | None = Form(None),
    assignments: str | None = Form(None),
    force_import: str | None = Form(None),
    force_skip: str | None = Form(None),
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.ImportCommitOut:
    parsed_mapping = _parse_mapping(mapping)
    multi = parsed_mapping is not None and parsed_mapping.account_col is not None
    single = _Target(name="", skip=True)
    if not multi:
        if not account_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose an account")
        account = _account_or_404(db, account_id, user.household_id)
        single = _Target(name=account.name, account_id=account.id, dedup_key=account.id)

    content = await _read(file)
    try:
        parsed = importers.parse_file(content, file_format, parsed_mapping)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    # Creates any accounts the user asked for, so rows can be attributed below.
    account_assignments = _parse_assignments(assignments)
    targets = _build_targets(db, user.household_id, account_assignments, create=True)
    forced = _index_set(force_import)
    skipped_rows = _index_set(force_skip)
    categoriser = build_categoriser(db, user.household_id)

    batch = models.ImportBatch(
        household_id=user.household_id,
        account_id=None if multi else account_id,
        filename=file.filename or "upload",
        file_format=file_format.lower(),
        created_by_user_id=user.id,
        # Remembered so the next import of the same statement pre-fills its mapping.
        mapping_profile=(
            {
                "account_col": parsed_mapping.account_col if parsed_mapping else None,
                "account_assignments": {
                    v: t.account_id for v, t in targets.items() if t.account_id
                },
            }
            if multi
            else None
        ),
    )
    db.add(batch)
    db.flush()

    added = 0
    skipped = 0
    resolve = _resolver(parsed_mapping, targets, single)
    for index, p, target, verdict in _decide(db, parsed, resolve):
        if target.account_id is None or not _will_import(verdict, index, forced, skipped_rows):
            skipped += 1
            continue
        result = categoriser.categorise(p.raw_description, p.merchant)
        db.add(
            models.Transaction(
                household_id=user.household_id,
                account_id=target.account_id,
                txn_date=p.txn_date,
                amount_cents=p.amount_cents,
                raw_description=p.raw_description,
                merchant=p.merchant,
                category_id=result.category_id,
                confidence=result.confidence if result.category_id else None,
                source="import",
                dedup_hash=importers.dedup_hash(
                    target.account_id, p.txn_date, p.amount_cents, p.raw_description
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
