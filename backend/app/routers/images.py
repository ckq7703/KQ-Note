from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import Image, User
from ..schemas import ImageManifest

router = APIRouter(prefix="/images", tags=["images"])

MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8MB safety cap


@router.get("/manifest", response_model=ImageManifest)
def get_manifest(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(Image.id).filter(Image.user_id == user.id).all()
    return ImageManifest(ids=[row[0] for row in rows])


@router.put("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def upload_image(
    file_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    data = await request.body()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty image body")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Image too large")

    image = db.get(Image, (user.id, file_id))
    if image is None:
        db.add(Image(user_id=user.id, id=file_id, data=data))
    else:
        image.data = data
    db.commit()


@router.get("/{file_id}")
def download_image(
    file_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    image = db.get(Image, (user.id, file_id))
    if image is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Image not found")
    return Response(content=image.data, media_type="image/png")
