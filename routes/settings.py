from fastapi import APIRouter, Request, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func, delete
from utils.logger import logger
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR


from models import SessionLocal, Language, SupportGroup, User, Translation, ModeratorGroupLink, SupportGroupLanguage, ButtonVisibility
from utils.utils import get_group_photo_url

router = APIRouter()
templates = Jinja2Templates(directory="templates")

@router.get("/settings")
async def settings_page(request: Request):
    try:
        async with SessionLocal() as session:
            # языки, группы, модераторы и коды без переводов
            langs_res = await session.execute(select(Language))
            languages = langs_res.scalars().all()

            groups_res = await session.execute(
                select(SupportGroup)
                .options(
                    selectinload(SupportGroup.languages),
                    selectinload(SupportGroup.moderators),
                )
            )
            groups = groups_res.scalars().all()

            mods_res = await session.execute(select(User).where(User.role == "moderator"))
            moderators = mods_res.scalars().all()

            unavailable_res = await session.execute(
                select(Language.code)
                .outerjoin(Translation, Translation.lang == Language.code)
                .group_by(Language.code)
                .having(func.count(Translation.id) == 0)
            )
            unavailable_codes = {row[0] for row in unavailable_res}

            # читаем всё из button_visibility
            btn_res = await session.execute(select(ButtonVisibility))
            btn_rows = btn_res.scalars().all()
            # словарь button_key → is_visible
            buttons = {b.button_key: b.is_visible for b in btn_rows}

            # теперь подгружаем переводы для этих ключей на русском
            keys = list(buttons.keys())
            tr_res = await session.execute(
                select(Translation.key, Translation.text)
                .where(
                    Translation.lang == "ru",
                    Translation.key.in_(keys)
                )
            )
            # получается список кортежей (key, text)
            tr_map = {key: text for key, text in tr_res.all()}

        # Передаём в шаблон
        return templates.TemplateResponse("settings.html", {
            "request": request,
            "languages": languages,
            "groups": groups,
            "moderators": moderators,
            "unavailable_codes": unavailable_codes,
            "buttons": buttons,
            "button_labels": tr_map,
            "get_photo_url": lambda path: get_group_photo_url(path),
        })

    except SQLAlchemyError as e:
        print(f"[DB ERROR] {e}")
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Ошибка при подключении к базе данных."
        )

    except Exception as e:
        print(f"[UNEXPECTED ERROR] {e}")
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Неизвестная ошибка при загрузке настроек."
        )

@router.post("/settings/toggle-button/{button_key}")
async def toggle_button(request: Request, button_key: str):
    try:
        async with SessionLocal() as session:
            # Попробуем найти существующую запись
            result = await session.execute(
                select(ButtonVisibility)
                .where(ButtonVisibility.button_key == button_key)
            )
            bv = result.scalar_one_or_none()

            if bv:
                bv.is_visible = not bv.is_visible
            else:
                # Если ещё нет записи – создаём (по умолчанию скрыто)
                bv = ButtonVisibility(button_key=button_key, is_visible=False)
                session.add(bv)

            await session.commit()

        # После сохранения – редирект обратно на страницу настроек
        return RedirectResponse(
            url=request.url_for("settings_page"),
            status_code=303
        )

    except SQLAlchemyError as e:
        logger.exception(f"[DB ERROR toggling button {button_key}] {e}")
        raise HTTPException(
            status_code=500,
            detail="Ошибка при сохранении настроек кнопки."
        )
    except Exception as e:
        logger.exception(f"[UNEXPECTED ERROR toggling button {button_key}] {e}")
        raise HTTPException(
            status_code=500,
            detail="Неожиданная ошибка при переключении кнопки."
        )

@router.post("/settings/toggle-language/{code}")
async def toggle_language(code: str):
    try:
        async with SessionLocal() as session:
            lang = await session.get(Language, code)
            if not lang:
                raise HTTPException(status_code=404, detail="Язык не найден")
            lang.available = not lang.available
            await session.commit()
            return RedirectResponse(url="/settings", status_code=303)

    except SQLAlchemyError as e:
        logger.exception(f"[DB ERROR toggle_language {code}] {e}")
        raise HTTPException(
            status_code=500,
            detail="Ошибка при переключении доступности языка в базе данных."
        )
    except Exception as e:
        logger.exception(f"[UNEXPECTED ERROR toggle_language {code}] {e}")
        raise HTTPException(
            status_code=500,
            detail="Неожиданная ошибка при переключении языка."
        )

@router.post("/settings/assign-language/{group_id}")
async def assign_language(group_id: int, request: Request):
    try:
        form = await request.form()
        language_code = form.get("language_code")

        async with SessionLocal() as session:
            # проверяем, что язык существует и активен
            lang = await session.get(Language, language_code)
            if not lang or not lang.available:
                raise HTTPException(status_code=400, detail="Недопустимый язык")

            # не даём дважды привязать один и тот же язык
            exists = await session.execute(
                select(SupportGroupLanguage).where(
                    SupportGroupLanguage.group_id == group_id,
                    SupportGroupLanguage.language_code == language_code
                )
            )
            if exists.scalar_one_or_none():
                return RedirectResponse(url="/settings", status_code=303)

            # сохраняем новую связь
            session.add(SupportGroupLanguage(
                group_id=group_id,
                language_code=language_code
            ))
            await session.commit()

        # редиректим обратно на страницу настроек
        return RedirectResponse(url="/settings", status_code=303)

    except SQLAlchemyError as e:
        logger.exception(f"[DB ERROR assign_language group={group_id} lang={language_code}] {e}")
        raise HTTPException(
            status_code=500,
            detail="Ошибка при сохранении языка в базе данных."
        )

    except HTTPException:
        raise

    except Exception as e:
        logger.exception(f"[UNEXPECTED ERROR assign_language group={group_id} lang={language_code}] {e}")
        raise HTTPException(
            status_code=500,
            detail="Неожиданная ошибка при назначении языка группе."
        )

@router.post("/settings/unassign-language/{group_id}/{language_code}")
async def unassign_language(group_id: int, language_code: str):
    try:
        async with SessionLocal() as session:
            # Удаляем связь группы и языка
            await session.execute(
                delete(SupportGroupLanguage).where(
                    SupportGroupLanguage.group_id == group_id,
                    SupportGroupLanguage.language_code == language_code
                )
            )
            await session.commit()

        # Редиректим назад на настройки
        return RedirectResponse(url="/settings", status_code=303)

    except SQLAlchemyError as e:
        logger.exception(f"[DB ERROR unassign_language group={group_id} lang={language_code}] {e}")
        raise HTTPException(
            status_code=500,
            detail="Ошибка при удалении языка из группы в базе данных."
        )

    except Exception as e:
        logger.exception(f"[UNEXPECTED ERROR unassign_language group={group_id} lang={language_code}] {e}")
        raise HTTPException(
            status_code=500,
            detail="Неожиданная ошибка при удалении языка из группы."
        )

@router.post("/settings/assign-moderator/{group_id}")
async def assign_moderator(group_id: int, request: Request):
    try:
        form = await request.form()
        moderator_id = int(form.get("moderator_id"))

        async with SessionLocal() as session:
            # Проверяем, что пользователь существует и у него роль «moderator»
            user = await session.get(User, moderator_id)
            if not user or user.role != "moderator":
                raise HTTPException(status_code=400, detail="Недопустимый модератор")

            # Не добавляем дубликаты
            exists = await session.execute(
                select(ModeratorGroupLink).where(
                    ModeratorGroupLink.group_id == group_id,
                    ModeratorGroupLink.moderator_id == moderator_id
                )
            )
            if exists.scalar():
                return RedirectResponse(url="/settings", status_code=303)

            # Добавляем связь и сохраняем
            session.add(ModeratorGroupLink(
                group_id=group_id,
                moderator_id=moderator_id
            ))
            await session.commit()

        # После успешного сохранения – назад в настройки
        return RedirectResponse(url="/settings", status_code=303)

    except SQLAlchemyError as e:
        logger.exception(f"[DB ERROR assign_moderator group={group_id} mod={moderator_id}] {e}")
        raise HTTPException(
            status_code=500,
            detail="Ошибка при добавлении модератора в группу."
        )

    except Exception as e:
        logger.exception(f"[UNEXPECTED ERROR assign_moderator group={group_id} mod={moderator_id}] {e}")
        raise HTTPException(
            status_code=500,
            detail="Неожиданная ошибка при добавлении модератора."
        )

@router.post("/settings/unassign-moderator/{group_id}/{moderator_id}")
async def unassign_moderator(group_id: int, moderator_id: int):
    try:
        async with SessionLocal() as session:
            # Удаляем связь модератора с группой
            await session.execute(
                delete(ModeratorGroupLink).where(
                    ModeratorGroupLink.group_id == group_id,
                    ModeratorGroupLink.moderator_id == moderator_id
                )
            )
            await session.commit()

        # После успешного удаления – редирект обратно
        return RedirectResponse(url="/settings", status_code=303)

    except SQLAlchemyError as e:
        logger.exception(f"[DB ERROR unassign_moderator group={group_id} mod={moderator_id}] {e}")
        raise HTTPException(
            status_code=500,
            detail="Ошибка при удалении модератора из группы."
        )

    except Exception as e:
        logger.exception(f"[UNEXPECTED ERROR unassign_moderator group={group_id} mod={moderator_id}] {e}")
        raise HTTPException(
            status_code=500,
            detail="Неожиданная ошибка при удалении модератора."
        )