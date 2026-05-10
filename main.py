import io
import os
from datetime import date
from typing import Any, Dict

import openpyxl
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import selectinload

from models import Base, Hawb, Mawb

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is required")


# =========================================================
# DATABASE
# =========================================================

# Para Railway / Neon / Supabase / Render
engine = create_async_engine(
    DATABASE_URL,
    echo=True,
    future=True,
    pool_pre_ping=True,
    connect_args={
        "ssl": "require"
    }
)

# Si usas PostgreSQL LOCAL usa esto:
#
# engine = create_async_engine(
#     DATABASE_URL,
#     echo=True,
#     future=True,
#     pool_pre_ping=True
# )

async_session = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession
)

# =========================================================
# APP
# =========================================================

app = FastAPI(title="AWB Backend API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://proyectoinformaticoenriquemolina.netlify.app/"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================================================
# HELPERS
# =========================================================


def parse_float(value: Any) -> float | None:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()

    if not text:
        return None

    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def format_date_to_colombian(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def parse_query_date(date_str: str) -> date:
    try:
        if "-" in date_str:
            year, month, day = date_str.split("-")
            return date(int(year), int(month), int(day))

        if "/" in date_str:
            day, month, year = date_str.split("/")
            return date(int(year), int(month), int(day))

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Fecha inválida: {date_str}"
        ) from exc

    raise HTTPException(
        status_code=400,
        detail="Use el formato DD/MM/YYYY o YYYY-MM-DD"
    )


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session


# =========================================================
# STARTUP
# =========================================================

@app.on_event("startup")
async def startup() -> None:
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        print("✅ Base de datos conectada correctamente")

    except Exception as exc:
        print("❌ Error conectando a la base de datos")
        print(exc)
        raise exc


# =========================================================
# UPLOAD EXCEL
# =========================================================

@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session)
):
    try:
        contents = await file.read()

        workbook = openpyxl.load_workbook(
            io.BytesIO(contents),
            data_only=True
        )

        sheet = workbook.active
        rows = list(sheet.values)

        column_positions = {
            "mawb": None,
            "hawb": None,
            "piec": None,
            "peso": None
        }

        # Buscar columnas
        for row in rows:

            if not row:
                continue

            for col_num, cell in enumerate(row):

                if cell == "MAWB":
                    column_positions["mawb"] = col_num

                elif cell == "# HAWB":
                    column_positions["hawb"] = col_num

                elif cell == "PIEC":
                    column_positions["piec"] = col_num

                elif cell == "PESO":
                    column_positions["peso"] = col_num

            if all(v is not None for v in column_positions.values()):
                break

        if not all(v is not None for v in column_positions.values()):
            raise HTTPException(
                status_code=400,
                detail="No se encontraron las columnas requeridas"
            )

        mawbs_data: Dict[str, Dict[str, Any]] = {}

        # Procesar filas
        for row in rows:

            if not row:
                continue

            mawb_value = row[column_positions["mawb"]]
            hawb_value = row[column_positions["hawb"]]
            pcs_value = row[column_positions["piec"]]
            wgt_value = row[column_positions["peso"]]

            mawb_number = (
                str(mawb_value).strip()
                if mawb_value is not None
                else ""
            )

            hawb_number = (
                str(hawb_value).strip()
                if hawb_value is not None
                else ""
            )

            pcs = parse_float(pcs_value)
            wgt = parse_float(wgt_value)

            if (
                not mawb_number
                or not hawb_number
                or pcs is None
                or wgt is None
            ):
                continue

            if mawb_number not in mawbs_data:
                mawbs_data[mawb_number] = {
                    "date": date.today(),
                    "hawbs": {}
                }

            if hawb_number not in mawbs_data[mawb_number]["hawbs"]:
                mawbs_data[mawb_number]["hawbs"][hawb_number] = {
                    "expected_pcs": pcs,
                    "expected_wgt": wgt
                }

        if not mawbs_data:
            raise HTTPException(
                status_code=400,
                detail="No se encontraron filas válidas"
            )

        result_data = []

        async with session.begin():

            for mawb_number, mawb_info in mawbs_data.items():

                hawb_items = mawb_info["hawbs"]

                total_expected_pcs = sum(
                    item["expected_pcs"]
                    for item in hawb_items.values()
                )

                total_expected_wgt = sum(
                    item["expected_wgt"]
                    for item in hawb_items.values()
                )

                today_date = mawb_info["date"]

                existing_mawb = await session.scalar(
                    select(Mawb).where(
                        Mawb.mawb_number == mawb_number
                    )
                )

                # Si existe -> actualizar
                if existing_mawb is not None:
                    existing_mawb.total_expected_pcs = total_expected_pcs
                    existing_mawb.total_expected_wgt = total_expected_wgt
                    existing_mawb.date = today_date

                    mawb_obj = existing_mawb

                    # Preserve `real_*` already stored for HAWBs (only update expected_*).
                    existing_hawbs_result = await session.execute(
                        select(Hawb).where(Hawb.mawb_id == existing_mawb.id)
                    )
                    existing_hawbs_by_number = {
                        hawb_obj.hawb_number: hawb_obj
                        for hawb_obj in existing_hawbs_result.scalars().all()
                    }

                    new_hawbs_numbers = set(hawb_items.keys())

                    for hawb_number, hawb_data in hawb_items.items():
                        existing_hawb = existing_hawbs_by_number.get(hawb_number)
                        if existing_hawb is not None:
                            existing_hawb.expected_pcs = hawb_data["expected_pcs"]
                            existing_hawb.expected_wgt = hawb_data["expected_wgt"]
                        else:
                            session.add(
                                Hawb(
                                    hawb_number=hawb_number,
                                    expected_pcs=hawb_data["expected_pcs"],
                                    expected_wgt=hawb_data["expected_wgt"],
                                    real_pcs=0.0,
                                    real_wgt=0.0,
                                    mawb_id=existing_mawb.id
                                )
                            )

                    # Keep DB consistent with the uploaded file:
                    # delete HAWBs that are no longer present.
                    hawbs_to_delete = [
                        hawb_obj
                        for hawb_num, hawb_obj in existing_hawbs_by_number.items()
                        if hawb_num not in new_hawbs_numbers
                    ]
                    for hawb_obj in hawbs_to_delete:
                        session.delete(hawb_obj)

                    # Recompute totals_real_* from preserved/new real values.
                    totals_result = await session.execute(
                        select(
                            func.coalesce(func.sum(Hawb.real_pcs), 0),
                            func.coalesce(func.sum(Hawb.real_wgt), 0),
                        ).where(Hawb.mawb_id == existing_mawb.id)
                    )
                    real_total_pcs, real_total_wgt = totals_result.one()
                    existing_mawb.total_real_pcs = real_total_pcs
                    existing_mawb.total_real_wgt = real_total_wgt

                else:
                    mawb_obj = Mawb(
                        mawb_number=mawb_number,
                        total_expected_pcs=total_expected_pcs,
                        total_expected_wgt=total_expected_wgt,
                        total_real_pcs=0.0,
                        total_real_wgt=0.0,
                        date=today_date
                    )

                    session.add(mawb_obj)

                    await session.flush()

                # Crear HAWBs
                if existing_mawb is None:
                    for hawb_number, hawb_data in hawb_items.items():
                        session.add(
                            Hawb(
                                hawb_number=hawb_number,
                                expected_pcs=hawb_data["expected_pcs"],
                                expected_wgt=hawb_data["expected_wgt"],
                                real_pcs=0.0,
                                real_wgt=0.0,
                                mawb_id=mawb_obj.id
                            )
                        )

                result_data.append(
                    {
                        "MAWB": mawb_number,
                        "date": format_date_to_colombian(today_date),
                        "total_expected_pcs": total_expected_pcs,
                        "total_expected_wgt": total_expected_wgt,
                        "hawb_count": len(hawb_items)
                    }
                )

        return {
            "message": "Data loaded successfully",
            "data": result_data
        }

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc)
        ) from exc


# =========================================================
# GET ALL MAWBS
# =========================================================

@app.get("/mawb")
async def get_mawb(
    session: AsyncSession = Depends(get_session)
):
    result = await session.execute(
        select(Mawb.mawb_number)
    )

    return [row[0] for row in result.all()]


# =========================================================
# GET MAWB DETAILS
# =========================================================

@app.get("/mawb/{mawb}")
async def get_mawb_details(
    mawb: str,
    session: AsyncSession = Depends(get_session)
):
    result = await session.execute(
        select(Mawb)
        .options(selectinload(Mawb.hawbs))
        .where(Mawb.mawb_number == mawb)
    )

    mawb_obj = result.scalar_one_or_none()

    if mawb_obj is None:
        raise HTTPException(
            status_code=404,
            detail="MAWB not found"
        )

    return {
        "MAWB": mawb_obj.mawb_number,
        "date": format_date_to_colombian(mawb_obj.date),
        "total_expected_pcs": mawb_obj.total_expected_pcs,
        "total_expected_wgt": mawb_obj.total_expected_wgt,
        "total_real_pcs": mawb_obj.total_real_pcs,
        "total_real_wgt": mawb_obj.total_real_wgt,
        "hawbs": [
            {
                "HAWB": hawb.hawb_number,
                "expected_pcs": hawb.expected_pcs,
                "expected_wgt": hawb.expected_wgt,
                "real_pcs": hawb.real_pcs,
                "real_wgt": hawb.real_wgt,
            }
            for hawb in mawb_obj.hawbs
        ]
    }


# =========================================================
# UPDATE HAWB REAL VALUES
# =========================================================

@app.put("/hawb/real")
async def update_hawb_real(
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session)
):
    mawb_number = str(payload.get("mawb", "")).strip()
    hawb_number = str(payload.get("hawb", "")).strip()

    pcs = parse_float(payload.get("pcs")) or 0.0
    wgt = parse_float(payload.get("wgt")) or 0.0

    if not mawb_number or not hawb_number:
        raise HTTPException(
            status_code=400,
            detail="mawb y hawb son obligatorios"
        )

    result = await session.execute(
        select(Mawb).where(
            Mawb.mawb_number == mawb_number
        )
    )

    mawb_obj = result.scalar_one_or_none()

    if mawb_obj is None:
        raise HTTPException(
            status_code=404,
            detail="MAWB no encontrado"
        )

    result = await session.execute(
        select(Hawb).where(
            Hawb.mawb_id == mawb_obj.id,
            Hawb.hawb_number == hawb_number
        )
    )

    hawb_obj = result.scalar_one_or_none()

    if hawb_obj is None:
        raise HTTPException(
            status_code=404,
            detail="HAWB no encontrado"
        )

    hawb_obj.real_pcs = pcs
    hawb_obj.real_wgt = wgt

    # Recalcular totales
    totals_result = await session.execute(
        select(
            func.coalesce(func.sum(Hawb.real_pcs), 0),
            func.coalesce(func.sum(Hawb.real_wgt), 0)
        ).where(Hawb.mawb_id == mawb_obj.id)
    )

    real_total_pcs, real_total_wgt = totals_result.one()

    mawb_obj.total_real_pcs = real_total_pcs
    mawb_obj.total_real_wgt = real_total_wgt

    await session.commit()

    return {
        "message": "HAWB actualizado",
        "MAWB": mawb_number,
        "HAWB": hawb_number,
        "real_pcs": pcs,
        "real_wgt": wgt
    }


# =========================================================
# UPDATE MAWB DATE
# =========================================================

@app.put("/mawb/{mawb}/date")
async def update_mawb_date(
    mawb: str,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session)
):
    mawb_number = str(mawb).strip()
    new_date_str = str(payload.get("date", "")).strip()

    if not new_date_str:
        raise HTTPException(
            status_code=400,
            detail="La fecha es obligatoria"
        )

    # Parsear la fecha
    new_date = parse_query_date(new_date_str)

    result = await session.execute(
        select(Mawb).where(
            Mawb.mawb_number == mawb_number
        )
    )

    mawb_obj = result.scalar_one_or_none()

    if mawb_obj is None:
        raise HTTPException(
            status_code=404,
            detail="MAWB no encontrado"
        )

    mawb_obj.date = new_date
    await session.commit()

    return {
        "message": "Fecha actualizada correctamente",
        "MAWB": mawb_number,
        "date": format_date_to_colombian(new_date)
    }


# =========================================================
# DELETE MAWB
# =========================================================

@app.delete("/mawb/{mawb}")
async def delete_mawb(
    mawb: str,
    session: AsyncSession = Depends(get_session)
):
    mawb_number = str(mawb).strip()

    result = await session.execute(
        select(Mawb).where(
            Mawb.mawb_number == mawb_number
        )
    )

    mawb_obj = result.scalar_one_or_none()

    if mawb_obj is None:
        raise HTTPException(
            status_code=404,
            detail="MAWB no encontrado"
        )

    await session.delete(mawb_obj)
    await session.commit()

    return {
        "message": "MAWB eliminado correctamente",
        "MAWB": mawb_number
    }


# =========================================================
# FILTER BY DATE
# =========================================================

@app.get("/filter/date/{date}")
async def filter_by_date(
    date: str,
    session: AsyncSession = Depends(get_session)
):
    query_date = parse_query_date(date)

    result = await session.execute(
        select(Mawb)
        .options(selectinload(Mawb.hawbs))
        .where(Mawb.date == query_date)
    )

    mawbs = result.scalars().all()

    summary = []
    detail = []

    summary_total_expected_pcs = 0.0
    summary_total_expected_wgt = 0.0
    summary_total_real_pcs = 0.0
    summary_total_real_wgt = 0.0

    for mawb_obj in mawbs:

        real_pcs = sum(
            hawb.real_pcs
            for hawb in mawb_obj.hawbs
        )

        real_wgt = sum(
            hawb.real_wgt
            for hawb in mawb_obj.hawbs
        )

        summary.append(
            {
                "MAWB": mawb_obj.mawb_number,
                "Fecha": format_date_to_colombian(query_date),
                "Esperado PCS": mawb_obj.total_expected_pcs,
                "Esperado WGT": mawb_obj.total_expected_wgt,
                "Real PCS": real_pcs,
                "Real WGT": real_wgt,
            }
        )

        summary_total_expected_pcs += mawb_obj.total_expected_pcs
        summary_total_expected_wgt += mawb_obj.total_expected_wgt
        summary_total_real_pcs += real_pcs
        summary_total_real_wgt += real_wgt

        for hawb_obj in mawb_obj.hawbs:

            detail.append(
                {
                    "MAWB": mawb_obj.mawb_number,
                    "HAWB": hawb_obj.hawb_number,
                    "Esperado PCS": hawb_obj.expected_pcs,
                    "Esperado WGT": hawb_obj.expected_wgt,
                    "Real PCS": hawb_obj.real_pcs,
                    "Real WGT": hawb_obj.real_wgt,
                }
            )

    return {
        "date": format_date_to_colombian(query_date),
        "summary": summary,
        "detail": detail,
        "summaryTotalExpectedPCS": summary_total_expected_pcs,
        "summaryTotalExpectedWGT": summary_total_expected_wgt,
        "summaryTotalRealPCS": summary_total_real_pcs,
        "summaryTotalRealWGT": summary_total_real_wgt,
    }


# =========================================================
# EXPORT
# =========================================================

@app.get("/export/{date}")
async def export_by_date(
    date: str,
    session: AsyncSession = Depends(get_session)
):
    return await filter_by_date(date, session)
