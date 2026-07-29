"""ORM models the gateway touches. (Copy-paste per service — no shared lib yet.)"""
from sqlalchemy import Boolean, Column, DateTime, Integer, Text
from sqlalchemy.sql import func

from db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(Text, unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    full_name = Column(Text)
    role = Column(Text, nullable=False, default="staff")   # one role for everyone
    is_active = Column(Boolean, nullable=False, default=True)
    last_login_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # W4 / adr/0011. Patient-portal accounts point at their own chart; NULL for
    # staff. This is the column that makes "own record" expressible at all.
    patient_id = Column(Integer)
