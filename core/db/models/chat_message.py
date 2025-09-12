from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from core.db.models import Base, ChatConvo


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    convo_id: Mapped[UUID] = mapped_column(ForeignKey("chat_convos.convo_id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    message_type: Mapped[str] = mapped_column()
    message: Mapped[str] = mapped_column()
    prev_message_id: Mapped[Optional[UUID]] = mapped_column(ForeignKey("chat_messages.id", ondelete="SET NULL"))

    # Relationships
    convo: Mapped["ChatConvo"] = relationship(back_populates="messages", lazy="selectin")
