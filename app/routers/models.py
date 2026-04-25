from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.model_catalog import ModelCatalogCreate, ModelCatalogDetailOut, ModelCatalogOut
from app.schemas.model_catalog import ModelCatalogUpdate, UserModelOut
from app.services.model_catalog_service import ModelCatalogService
from app.services.user_auth_service import require_admin_api_user, require_session_api_user


router = APIRouter(tags=["models"])


@router.get("/api/models", response_model=list[ModelCatalogOut], dependencies=[Depends(require_admin_api_user)])
def list_models(db: Session = Depends(get_db)) -> list[ModelCatalogOut]:
    return [ModelCatalogOut(**item) for item in ModelCatalogService.list_model_dicts(db)]


@router.post("/api/models", response_model=ModelCatalogDetailOut, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin_api_user)])
def create_model(payload: ModelCatalogCreate, db: Session = Depends(get_db)) -> ModelCatalogDetailOut:
    try:
        catalog = ModelCatalogService.create_model(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    detail = ModelCatalogService.get_model_detail(db, catalog.model_name)
    return ModelCatalogDetailOut(**detail)


@router.get("/api/models/{model_name}", response_model=ModelCatalogDetailOut, dependencies=[Depends(require_admin_api_user)])
def get_model_detail(model_name: str, db: Session = Depends(get_db)) -> ModelCatalogDetailOut:
    detail = ModelCatalogService.get_model_detail(db, model_name)
    if detail is None:
        raise HTTPException(status_code=404, detail="模型不存在")
    return ModelCatalogDetailOut(**detail)


@router.put("/api/models/{model_name}", response_model=ModelCatalogDetailOut, dependencies=[Depends(require_admin_api_user)])
def update_model(model_name: str, payload: ModelCatalogUpdate, db: Session = Depends(get_db)) -> ModelCatalogDetailOut:
    catalog = ModelCatalogService.get_catalog(db, model_name)
    if catalog is None:
        raise HTTPException(status_code=404, detail="模型不存在")
    try:
        ModelCatalogService.update_model(db, catalog, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    detail = ModelCatalogService.get_model_detail(db, model_name)
    return ModelCatalogDetailOut(**detail)


@router.get("/api/user/models", response_model=list[UserModelOut])
def list_user_models(current_user=Depends(require_session_api_user), db: Session = Depends(get_db)) -> list[UserModelOut]:
    return [UserModelOut(**item) for item in ModelCatalogService.list_user_models(db, user=current_user)]
