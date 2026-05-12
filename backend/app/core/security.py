from fastapi import HTTPException


def assert_required(value: str, field_name: str) -> None:
    if not value:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")

