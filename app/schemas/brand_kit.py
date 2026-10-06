from datetime import datetime

from pydantic import BaseModel


class BrandKitCreate(BaseModel):
    primary_color: str | None = None
    secondary_color: str | None = None
    accent_color: str | None = None
    primary_font: str | None = None
    secondary_font: str | None = None
    adjectives: str | None = None
    dos: str | None = None
    donts: str | None = None
    preset_name: str | None = None
    pdf_path: str | None = None


class BrandKitOut(BaseModel):
    id: str
    company_id: str
    primary_color: str | None = None
    secondary_color: str | None = None
    accent_color: str | None = None
    primary_font: str | None = None
    secondary_font: str | None = None
    adjectives: str | None = None
    dos: str | None = None
    donts: str | None = None
    preset_name: str | None = None
    pdf_path: str | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class BrandKitUpdate(BaseModel):
    primary_color: str | None = None
    secondary_color: str | None = None
    accent_color: str | None = None
    primary_font: str | None = None
    secondary_font: str | None = None
    adjectives: str | None = None
    dos: str | None = None
    donts: str | None = None
    preset_name: str | None = None
    pdf_path: str | None = None
