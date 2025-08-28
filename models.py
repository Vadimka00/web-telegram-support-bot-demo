# models.py
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, BigInteger, String, DateTime, ForeignKey, Text, Boolean, Numeric, UniqueConstraint
from sqlalchemy.orm import relationship, Mapped, mapped_column
from typing import List
from decimal import Decimal
from datetime import datetime
from config import DATABASE_URL 

engine = create_async_engine(DATABASE_URL)
SessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)
Base = declarative_base()

# Модель заявок на покупки
class Purchase(Base):
    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )

    # Пользователь, который совершил покупку
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Ключ сертификата
    certificate_key: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("certificates.key", ondelete="CASCADE"),
        nullable=False,
    )

    # Подтип способа оплаты
    method_key: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    # Сумма, по которой совершена покупка
    price: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    # file_id фотографии скрина оплаты
    photo_file_id: Mapped[str] = mapped_column(
        String(255),
        nullable=True,
    )

    # Статус: pending / confirmed / rejected
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
    )

    # ORM-связи
    user = relationship("User", back_populates="purchases")
    certificate = relationship("Certificate")

# Модель сертификатов
class Certificate(Base):
    __tablename__ = "certificates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    price_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    price_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    price_usdt: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    payment_methods: Mapped[list["PaymentMethod"]] = relationship(
        "PaymentMethod",
        back_populates="certificate",
        cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("key", name="uq_certificates_key"),
    )

# Модель способов оплаты
class PaymentMethod(Base):
    __tablename__ = "payment_methods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # К сертификату, к которому этот метод относится
    certificate_key: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("certificates.key", ondelete="CASCADE"),
        nullable=False
    )

    # Тип/подтип способа оплаты в рамках этого сертификата
    method_key: Mapped[str] = mapped_column(String(50), nullable=False)

    # Текстовое поле с любыми реквизитами
    details: Mapped[str] = mapped_column(Text, nullable=False)

    # Необязательное ФИО получателя платежа
    recipient_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Ключ перевода предупреждения
    warning_key: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Флаг видимости
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("certificate_key", "method_key", name="uq_cert_method"),
    )
    certificate = relationship("Certificate", back_populates="payment_methods")

# Модель видимости кнопок
class ButtonVisibility(Base):
    __tablename__ = "button_visibility"

    id = Column(Integer, primary_key=True, autoincrement=True)
    button_key = Column(String(50), nullable=False)
    is_visible = Column(Boolean, nullable=False, default=True)

# Модель пользователя
class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True)
    username = Column(String(100))
    full_name = Column(String(255))
    language_code = Column(String(3))
    role = Column(String(50), default='user')

    requests = relationship(
        "SupportRequest",
        back_populates="user",
        foreign_keys="[SupportRequest.user_id]"
    )

    credentials = relationship(
        "Credentials",
        uselist=False,
        back_populates="user"
    )

    invitations = relationship(
        "InvitedUser",
        foreign_keys="[InvitedUser.user_id]",
        back_populates="user",
        uselist=True,
        cascade="all, delete-orphan"
    )

    invited_users = relationship(
        "InvitedUser",
        foreign_keys="[InvitedUser.invited_by]",
        back_populates="inviter",
        uselist=True,
        cascade="all, delete-orphan"
    )

    purchases: Mapped[list["Purchase"]] = relationship(
        "Purchase",
        back_populates="user",
        cascade="all, delete-orphan",
    )

# Модель реферальных 
class InvitedUser(Base):
    __tablename__ = "invited_users"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )  # Telegram ID присоединившегося
    invited_by = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )  # Telegram ID пригласителя

    user = relationship(
        "User",
        foreign_keys=[user_id],
        back_populates="invitations",
    )

    inviter = relationship(
        "User",
        foreign_keys=[invited_by],
        back_populates="invited_users",
    )

# Модель запросов в саппорт
class SupportRequest(Base):
    __tablename__ = "support_requests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    assigned_moderator_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default='pending')
    language: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    taken_at: Mapped[datetime | None] = mapped_column(nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    user = relationship(
        "User",
        foreign_keys=[user_id],
        back_populates="requests"
    )

    moderator = relationship(
        "User",
        foreign_keys=[assigned_moderator_id]
    )
    messages = relationship("MessageHistory", back_populates="request")

    messages_metadata = relationship("SupportRequestMessage", back_populates="request", cascade="all, delete-orphan")

# Модель сообщение в саппорт
class SupportRequestMessage(Base):
    __tablename__ = "support_request_messages"

    request_id = mapped_column(ForeignKey("support_requests.id"), primary_key=True)
    chat_id = mapped_column(BigInteger, primary_key=True)
    message_id = mapped_column(BigInteger, nullable=False)

    text = mapped_column(Text, nullable=True)
    caption = mapped_column(Text, nullable=True)
    photo_file_id = mapped_column(String(255), nullable=True)

    request = relationship("SupportRequest", back_populates="messages_metadata")

# Модель истории сообщений саппорт
class MessageHistory(Base):
    __tablename__ = "message_history"

    id = Column(Integer, primary_key=True)
    request_id = Column(Integer, ForeignKey("support_requests.id"))
    sender_id = Column(BigInteger, ForeignKey("users.id"))
    text = Column(Text, nullable=True)
    photo_file_id = Column(String(255), nullable=True)
    caption = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

    request = relationship("SupportRequest", back_populates="messages")

# Модель переводов
class Translation(Base):
    __tablename__ = "translations"

    id = Column(Integer, primary_key=True)
    key = Column(String(100), index=True)
    lang = Column(String(3))
    text = Column(Text)

# Модель языков
class Language(Base):
    __tablename__ = "languages"

    code = Column(String(10), primary_key=True)
    name = Column(String(50), nullable=False)
    name_ru = Column(String(50), nullable=False)
    emoji = Column(String(10), default="")
    available = Column(Boolean, default=True)

# Модель статусов
class Status(Base):
    __tablename__ = "status"

    id = Column(BigInteger, primary_key=True)
    language_code = Column(String(3))
    role = Column(String(50))
    text = Column(Text, nullable=True) 

# Модель прав
class Credentials(Base):
    __tablename__ = "credentials"

    user_id       = Column(BigInteger, ForeignKey("users.id"), primary_key=True)
    email         = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)

    user = relationship("User", back_populates="credentials")

# Модель саппорт групп
class SupportGroup(Base):
    __tablename__ = "support_groups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    photo_url: Mapped[str] = mapped_column(String(512))

    languages: Mapped[List["SupportGroupLanguage"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    moderators: Mapped[List["ModeratorGroupLink"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )

# Модель языков саппорт групп
class SupportGroupLanguage(Base):
    __tablename__ = "support_group_languages"

    group_id: Mapped[int] = mapped_column(ForeignKey("support_groups.id"), primary_key=True)
    language_code: Mapped[str] = mapped_column(String(5), primary_key=True)  # 'ru', 'en', 'pl'

    group: Mapped["SupportGroup"] = relationship(back_populates="languages")

# Модель модераторов и их саппорт групп
class ModeratorGroupLink(Base):
    __tablename__ = "moderator_group_links"

    moderator_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("support_groups.id"), primary_key=True)

    group: Mapped["SupportGroup"] = relationship(back_populates="moderators")