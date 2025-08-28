# main.py
from fastapi import FastAPI, Request, Depends, HTTPException, Cookie, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from models import SessionLocal, Translation, User, Status, Language, SupportRequest, Credentials
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import selectinload
from utils.telegram import resolve_photo_url
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import select, update, insert, func
from collections import defaultdict
from jinja2 import TemplateNotFound
import uvicorn
import json
import bcrypt
import secrets
import traceback
from starlette.responses import Response
from starlette.status import HTTP_303_SEE_OTHER
from utils.logger import logger
from services.gpt_translate import translate_with_gpt
from config import SHARED_SECRET
from routes import (
    gpt_translations,
    save_translations,
    settings
)

class UpdateRequest(BaseModel):
    key: str
    lang: str
    text: str

# Загрузка описаний ключей
with open("descriptions.json", encoding="utf-8") as f:
    key_descriptions = json.load(f)

with open("flags.json", encoding="utf-8") as f:
    flags = json.load(f)

with open("status_labels.json", encoding="utf-8") as f:
    status_labels = json.load(f)

app = FastAPI()
app.include_router(gpt_translations.router)
app.include_router(save_translations.router)
app.include_router(settings.router)
templates = Jinja2Templates(directory="templates")

app.mount("/static", StaticFiles(directory="static"), name="static")


# Этот middleware позволит перехватывать 500 ошибки
@app.middleware("http")
async def custom_error_handler(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        logger.error(
            f"❌ Unhandled error on {request.method} {request.url.path}: {e}\n{traceback.format_exc()}"
        )

        return HTMLResponse(
            content="""
            <!DOCTYPE html>
            <html lang="ru">
            <head>
              <meta charset="UTF-8">
              <title>Ошибка</title>
              <meta name="viewport" content="width=device-width, initial-scale=1.0">
              <style>
                body {
                  font-family: sans-serif;
                  background: #fdf2f2;
                  color: #333;
                  padding: 2rem;
                  text-align: center;
                }
                .error-box {
                  background: #fff;
                  border: 1px solid #e0e0e0;
                  max-width: 400px;
                  margin: 4rem auto;
                  padding: 2rem;
                  border-radius: 8px;
                  box-shadow: 0 4px 12px rgba(0,0,0,0.05);
                }
              </style>
            </head>
            <body>
              <div class="error-box">
                <h2>Что-то пошло не так</h2>
                <p>Пожалуйста, перезагрузите страницу.</p>
              </div>
            </body>
            </html>
            """,
            status_code=500
        )

@app.get("/login")
async def login_form(request: Request, response: Response, token: str | None = None):
    # если пришёл корректный token вида "{user_id}:{secret}"
    if token:
        parts = token.split(":", 1)
        if len(parts) == 2:
            user_id_str, secret = parts
            if secret == SHARED_SECRET and user_id_str.isdigit():
                # проверим, что такой админ есть в БД
                async with SessionLocal() as session:
                    async with session.begin():
                        result = await session.execute(
                            select(Credentials).where(Credentials.user_id == int(user_id_str))
                        )
                        cred = result.scalar_one_or_none()
                        
                    if cred:
                        resp = RedirectResponse("/", status_code=303)
                        resp.set_cookie("user_id", user_id_str, httponly=True)
                        return resp

    # иначе – рисуем классическую форму входа
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...)
):
    logger.info(f"[POST /login] Попытка входа с email: {email}")
    async with SessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                select(Credentials).where(Credentials.email == email)
            )
            cred = result.scalar_one_or_none()

            if not cred or not bcrypt.checkpw(password.encode(), cred.password_hash.encode()):
                logger.warning(f"[POST /login] Неудачная попытка входа для email: {email}")
                return templates.TemplateResponse(
                    "login.html",
                    {"request": request, "error": "Неверная пара логин/пароль"}
                )

        logger.info(f"[POST /login] Успешный вход. user_id={cred.user_id}")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("user_id", str(cred.user_id), httponly=True)
        return response

@app.get("/logout")
def logout(response: Response):
    logger.info("[GET /logout] Выход пользователя. Очистка куки.")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("user_id")
    return response

def get_current_user(user_id: str = Cookie(None)):
    if not user_id:
        logger.debug("[AUTH] Отсутствует user_id в cookie. Перенаправление на /login")
        raise HTTPException(status_code=HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    logger.debug(f"[AUTH] Получен user_id из cookie: {user_id}")
    return int(user_id)

@app.get("/", dependencies=[Depends(get_current_user)], response_class=HTMLResponse)
async def index(request: Request):
    logger.info("[GET /] Загрузка главной страницы")

    try:
        # Читаем всё в одном контексте без begin()
        async with SessionLocal() as session:
            # статистика пользователей
            u = await session.execute(
                select(User.language_code, func.count()).group_by(User.language_code)
            )
            user_stats = dict(u.all())
            total_users = sum(user_stats.values())

            # статистика модераторов
            m = await session.execute(
                select(User.language_code, func.count())
                .where(User.role == "moderator")
                .group_by(User.language_code)
            )
            mod_stats = dict(m.all())
            total_mods = sum(mod_stats.values())

            # статистика заявок
            r = await session.execute(
                select(SupportRequest.language, SupportRequest.status, func.count())
                .group_by(SupportRequest.language, SupportRequest.status)
            )
            raw = r.all()

            # названия языков
            langs_result = await session.execute(select(Language))
            lang_names = {l.code: l.name_ru for l in langs_result.scalars().all()}

        # Обработка результатов вне сессии
        req_stats = {}
        for lang, st, cnt in raw:
            rec = req_stats.setdefault(lang, {
                "total": 0, "pending": 0, "in_progress": 0, "closed": 0
            })
            rec[st] += cnt
            rec["total"] += cnt
        total_reqs = sum(v["total"] for v in req_stats.values())

        logger.info(f"[/index] Статистика: users={total_users}, mods={total_mods}, requests={total_reqs}")

        def safe_lang(lang):
            return lang if lang is not None else "None"

        user_stats = {safe_lang(k): v for k, v in user_stats.items()}
        mod_stats = {safe_lang(k): v for k, v in mod_stats.items()}
        req_stats = {safe_lang(k): v for k, v in req_stats.items()}

        languages = sorted(set(user_stats) | set(mod_stats) | set(req_stats))
        statuses = ["pending", "in_progress", "closed"]

        return templates.TemplateResponse("index.html", {
            "request": request,
            "user_stats": user_stats,
            "mod_stats": mod_stats,
            "req_stats": req_stats,
            "languages": languages,
            "statuses": statuses,
            "flags": flags,
            "lang_names": lang_names,
            "status_labels": status_labels,
            "total_users": total_users,
            "total_mods": total_mods,
            "total_reqs": total_reqs,
        })

    except Exception as e:
        logger.exception(f"[GET /] Ошибка при загрузке главной страницы: {e}")
        raise

@app.get("/translations", dependencies=[Depends(get_current_user)], response_class=HTMLResponse)
async def show_translations(request: Request):
    logger.info("[GET /translations] Загрузка страницы переводов")

    try:
        # Открываем сессию и читаем всё, что нужно
        async with SessionLocal() as session:
            # все переводы
            result = await session.execute(select(Translation))
            translations_raw = result.scalars().all()
            # все языки
            langs_result = await session.execute(select(Language))
            all_langs = langs_result.scalars().all()

        # Собираем в двухуровневый словарь: translations[key][lang] = text
        translations = defaultdict(dict)
        used_lang_codes = set()
        for row in translations_raw:
            translations[row.key][row.lang] = row.text
            used_lang_codes.add(row.lang)

        # Фильтруем ключ "welcome" из статистики
        filtered_keys = [k for k in translations if k != "welcome"]
        total_keys = len(filtered_keys)

        # Выбираем, какие языки показывать
        selected_code = request.query_params.get("add")
        selected_lang = next((l for l in all_langs if l.code == selected_code), None)

        if selected_lang:
            # когда выбран новый язык – показываем только "ru" + его
            langs = [l for l in all_langs if l.code == "ru"]
            if selected_lang.code != "ru":
                langs.append(selected_lang)
        else:
            # иначе – все уже используемые
            langs = [l for l in all_langs if l.code in used_lang_codes]

        # Сортируем: "ru" первым, остальные по name_ru
        langs = sorted(langs, key=lambda l: (l.code != "ru", l.name_ru))

        # Считаем, сколько ключей уже заполнено и сколько пропущено
        filled_count = {
            l.code: sum(1 for k in filtered_keys if l.code in translations[k])
            for l in langs
        }
        missing_count = {
            code: total_keys - filled
            for code, filled in filled_count.items()
        }

        # Языки, которых ещё нет ни в одном переводе
        missing_langs = [l for l in all_langs if l.code not in used_lang_codes]

        # Если пользователь запросил GPT-перевод нового языка – гоняем
        temp_translations = {}
        if selected_lang:
            ru_texts = {
                k: translations[k]["ru"]
                for k in filtered_keys
                if "ru" in translations[k]
            }

            gpt_translations = await translate_with_gpt(
                ru_translations=ru_texts,
                target_lang=selected_lang.code,
                lang_name=selected_lang.name_ru,
                emoji=selected_lang.emoji or ""
            )
            # сразу вмёрдживаем в основной словарь, чтобы шаблон показал их
            for k, v in gpt_translations.items():
                translations[k][selected_lang.code] = v
            temp_translations = gpt_translations

        # Рендерим шаблон
        return templates.TemplateResponse("translations.html", {
            "request": request,
            "translations": translations,
            "langs": langs,
            "missing_langs": missing_langs,
            "selected_lang": selected_lang,
            "temp_translations": temp_translations,
            "key_descriptions": key_descriptions,
            "flags": flags,
            "total_keys": total_keys,
            "filled_count": filled_count,
            "missing_count": missing_count,
        })

    except Exception as e:
        logger.exception(f"[GET /translations] Ошибка при загрузке переводов: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")


@app.get("/translations/translate_missing", response_class=JSONResponse)
async def translate_missing(lang: str):
    # Читаем всё за одну сессию
    async with SessionLocal() as session:
        # все переводы
        result = await session.execute(select(Translation))
        rows = result.scalars().all()

        # параметры целевого языка
        lang_row = await session.get(Language, lang)
        if not lang_row:
            raise HTTPException(404, f"Language '{lang}' not found")
        lang_name = lang_row.name_ru
        emoji     = lang_row.emoji or ""

    # Собираем существующие переводы по ключам
    existing = defaultdict(set)
    for r in rows:
        existing[r.key].add(r.lang)

    # Ищем ключи, у которых есть 'ru', но нет целевого lang
    missing_keys = [
        key for key, langs in existing.items()
        if "ru" in langs and lang not in langs
    ]

    # Собираем тексты на русском (без StopIteration)
    ru_texts = {}
    for key in missing_keys:
        for r in rows:
            if r.key == key and r.lang == "ru":
                ru_texts[key] = r.text
                break

    # Переводим через GPT
    new_translations = await translate_with_gpt(
        ru_translations=ru_texts,
        target_lang=lang,
        lang_name=lang_name,
        emoji=emoji
    )

    return JSONResponse({"translations": new_translations})

@app.post("/update")
async def update_translation(data: UpdateRequest):
    logger.info(f"[POST /update] Обновление перевода: key={data.key!r}, lang={data.lang!r}")

    try:
        async with SessionLocal() as session:
            async with session.begin():
                # Находим запись
                result = await session.execute(
                    select(Translation).where(
                        Translation.key  == data.key,
                        Translation.lang == data.lang
                    )
                )
                translation = result.scalar_one_or_none()
                if translation is None:
                    logger.warning(
                        f"[POST /update] Не найден перевод для key={data.key!r}, lang={data.lang!r}"
                    )
                    raise HTTPException(status_code=404, detail="Translation not found")

                # Обновляем текст
                translation.text = data.text

            # session.commit() выполнится автоматически при выходе из session.begin()
        logger.info(f"[POST /update] Перевод успешно обновлён")
        return JSONResponse({"status": "updated"})

    except HTTPException:
        # пробрасываем 404 дальше
        raise
    except Exception as e:
        logger.exception(f"[POST /update] Ошибка обновления перевода: {e}")
        # возвращаем 500 с сообщением
        raise HTTPException(status_code=500, detail="Internal Server Error")

@app.get("/users", dependencies=[Depends(get_current_user)], response_class=HTMLResponse)
async def users_view(
    request: Request,
    q: str = "",
    role: str = "",
    page: int = 1,
    per_page: int = 20
):
    offset = (page - 1) * per_page
    client_ip = request.client.host
    current_user = request.scope.get("user")
    logger.info(f"[GET /users] by {current_user!r} from {client_ip} | q={q!r}, role={role!r}, page={page}")

    try:
        async with SessionLocal() as session:
            async with session.begin():
                # --- Фильтры для WHERE ---
                filters = []
                if q:
                    pattern = f"%{q.lower()}%"
                    filters.append(
                        func.lower(User.username).like(pattern) |
                        func.lower(User.full_name).like(pattern)
                    )
                if role:
                    filters.append(User.role == role)

                # --- Общее число подходящих юзеров ---
                count_stmt = select(func.count()).select_from(User)
                if filters:
                    count_stmt = count_stmt.where(*filters)
                total = await session.scalar(count_stmt)

                # --- Статистика по языкам среди этих же фильтров ---
                lang_stats_stmt = (
                    select(User.language_code, func.count())
                    .group_by(User.language_code)
                )
                if filters:
                    lang_stats_stmt = lang_stats_stmt.where(*filters)
                lang_counts = dict((await session.execute(lang_stats_stmt)).all())

                # --- Получаем сам список юзеров ---
                user_stmt = select(User)
                if filters:
                    user_stmt = user_stmt.where(*filters)
                user_stmt = user_stmt.order_by(User.id.desc()).offset(offset).limit(per_page)
                users = (await session.execute(user_stmt)).scalars().all()

                # --- Доступные для выбора языки ---
                available_stmt = select(Language).where(Language.available.is_(True))
                available_languages = (await session.execute(available_stmt)).scalars().all()

        # Пост-обработка вне сессии
        total_pages = (total + per_page - 1) // per_page
        lang_names   = {lang.code: lang.name_ru for lang in available_languages}

        return templates.TemplateResponse("users.html", {
            "request": request,
            "total": total,
            "lang_counts": lang_counts,
            "lang_names": lang_names,
            "users": users,
            "query": q,
            "selected_role": role,
            "page": page,
            "total_pages": total_pages,
            "available_languages": available_languages,
            "flags": flags,
        })

    except TemplateNotFound:
        # Шаблон упал, пробрасываем 500
        logger.exception("[GET /users] Template not found")
        raise HTTPException(status_code=500, detail="Template error")
    except SQLAlchemyError as e:
        logger.exception(f"[GET /users] Database error: {e}")
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.exception(f"[GET /users] Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/users/set-language")
async def set_user_language(
    request: Request,
    user_id: int = Form(...),
    lang: str = Form(...)
):
    client_ip = request.client.host
    try:
        async with SessionLocal() as session:
            async with session.begin():
                # Проверяем, что пользователь существует
                user = await session.get(User, user_id)
                if not user:
                    logger.warning(f"[SET-LANG] Non-existent user_id={user_id} from {client_ip}")
                    # просто возвращаем редирект, не показываем ошибку
                    return RedirectResponse("/users", status_code=303)

                logger.info(f"[SET-LANG] user_id={user_id} (@{user.username}) → lang={lang}")

                # Обновляем код языка
                await session.execute(
                    update(User)
                    .where(User.id == user_id)
                    .values(language_code=lang)
                )
                # commit выполнится автоматически при выходе из session.begin()

        #  Всё ок - редирект обратно
        return RedirectResponse("/users", status_code=303)

    except SQLAlchemyError as e:
        logger.exception(f"[SET-LANG] Database error for user_id={user_id}: {e}")
        # в случае бага базы просто перенаправим, не хотим падать
        return RedirectResponse("/users", status_code=303)

    except Exception as e:
        logger.exception(f"[SET-LANG] Unexpected error for user_id={user_id}: {e}")
        return RedirectResponse("/users", status_code=303)

@app.post("/users/set-role")
async def set_user_role(
    request: Request,
    user_id: int = Form(...),
    role: str = Form(...)
):
    client_ip = request.client.host
    try:
        async with SessionLocal() as session:
            async with session.begin():
                # Убедимся, что пользователь существует
                user = await session.get(User, user_id)
                if not user:
                    logger.warning(f"[SET-ROLE] Non-existent user_id={user_id} from {client_ip}")
                    return RedirectResponse("/users", status_code=303)

                logger.info(f"[SET-ROLE] user_id={user_id} (@{user.username}) → role={role}")

                # Обновляем роль
                await session.execute(
                    update(User)
                    .where(User.id == user_id)
                    .values(role=role)
                )

                admin_credentials_info = None

                # Если назначили админом – (re)создаем учётку
                if role == "admin":
                    username = (user.username or "").lstrip("@") or f"user{user_id}"
                    email = f"{username}@admin.grandtime.com"
                    raw_pw = username + secrets.token_hex(3)
                    pw_hash = bcrypt.hashpw(raw_pw.encode(), bcrypt.gensalt()).decode()

                    stmt = mysql_insert(Credentials).values(
                        user_id=user.id,
                        email=email,
                        password_hash=pw_hash
                    ).on_duplicate_key_update(
                        password_hash=pw_hash
                    )
                    await session.execute(stmt)

                    admin_credentials_info = f"Email: {email}\nPassword: {raw_pw}"
                    logger.info(f"[SET-ROLE] Admin credentials for user_id={user_id}: {email}")

                # Логируем текущее состояние в таблице Status
                status_stmt = insert(Status).values(
                    id=user.id,
                    language_code=user.language_code,
                    role=role,
                    text=admin_credentials_info
                )
                await session.execute(status_stmt)

            # здесь session.begin() автоматически коммитит

        logger.info(f"[SET-ROLE] Role '{role}' applied to user_id={user_id}")
    except SQLAlchemyError as e:
        logger.exception(f"[SET-ROLE] DB error for user_id={user_id}: {e}")
    except Exception as e:
        logger.error(f"[SET-ROLE] Unexpected error for user_id={user_id}: {e}\n{traceback.format_exc()}")

    # Всегда возвращаем на список пользователей
    return RedirectResponse("/users", status_code=303)

@app.get("/requests", dependencies=[Depends(get_current_user)], response_class=HTMLResponse)
async def requests_view(
    request: Request,
    lang: str = "all",
    status: str = "all",
    page: int = 1,
    per_page: int = 20,
):
    client_ip = request.client.host
    current_user = request.scope.get("user")
    logger.info(
        f"[REQUESTS] by {current_user} from {client_ip} | "
        f"lang={lang} status={status} page={page} per_page={per_page}"
    )

    try:
        async with SessionLocal() as session:
            async with session.begin():
                # Получаем все языки (для фильтра и подписи)
                langs_res = await session.execute(select(Language))
                all_langs = langs_res.scalars().all()
                lang_names = {l.code: l.name_ru for l in all_langs}

                # Базовый запрос по SupportRequest
                base_q = (
                    select(SupportRequest)
                    .options(
                        selectinload(SupportRequest.user),
                        selectinload(SupportRequest.moderator)
                    )
                    .order_by(SupportRequest.created_at.desc())
                )
                # Применяем фильтры
                if lang != "all":
                    base_q = base_q.where(SupportRequest.language == lang)
                if status != "all":
                    base_q = base_q.where(SupportRequest.status == status)

                # Считаем общее число запросов
                total = await session.scalar(
                    select(func.count()).select_from(base_q.subquery())
                )

                # Пагинация
                offset = (page - 1) * per_page
                res = await session.execute(base_q.offset(offset).limit(per_page))
                requests_list = res.scalars().all()

                # Собираем статистику по языкам и статусам
                stats_res = await session.execute(
                    select(
                        SupportRequest.language,
                        SupportRequest.status,
                        func.count()
                    )
                    .group_by(SupportRequest.language, SupportRequest.status)
                )
                raw_stats = stats_res.all()  # list of (lang, status, count)

        # Формируем словарь статистики
        lang_stats: dict[str, dict[str, int]] = {}
        for l_code, st, cnt in raw_stats:
            rec = lang_stats.setdefault(
                l_code,
                {"total": 0, "pending": 0, "in_progress": 0, "closed": 0}
            )
            rec[st] += cnt
            rec["total"] += cnt

        total_pages = (total + per_page - 1) // per_page

        logger.info(f"[REQUESTS] returned {len(requests_list)} of {total}")

        return templates.TemplateResponse("requests.html", {
            "request": request,
            "requests_list": requests_list,
            "lang_stats":    lang_stats,
            "total_requests": total,
            "languages":     sorted(lang_stats.keys()),
            "statuses":      ["pending", "in_progress", "closed"],
            "current_lang":  lang,
            "current_status":status,
            "flags":         flags,
            "lang_names":    lang_names,
            "status_labels": status_labels,
            "page":          page,
            "total_pages":   total_pages,
            "per_page":      per_page,
        })
    except SQLAlchemyError as e:
        logger.exception(f"[REQUESTS] DB error: {e}")
        raise HTTPException(500, "Database error")
    except Exception as e:
        logger.exception(f"[REQUESTS] Unexpected error: {e}")
        raise HTTPException(500, "Internal server error")

@app.get(
    "/chat/{request_id}",
    dependencies=[Depends(get_current_user)],
    response_class=HTMLResponse
)
async def chat_view(request: Request, request_id: int):
    client_ip = request.client.host
    current_user = request.scope.get("user")
    logger.info(f"[CHAT] /chat/{request_id} by {current_user} from {client_ip}")

    try:
        async with SessionLocal() as session:
            async with session.begin():
                # Забираем заявку вместе с пользователем, модератором и сообщениями
                q = (
                    select(SupportRequest)
                    .options(
                        selectinload(SupportRequest.user),
                        selectinload(SupportRequest.moderator),
                        selectinload(SupportRequest.messages)
                    )
                    .where(SupportRequest.id == request_id)
                )
                result = await session.execute(q)
                support_request = result.scalar_one_or_none()

                if not support_request:
                    logger.warning(f"[CHAT] Request {request_id} not found")
                    raise HTTPException(404, "Request not found")

        # Собираем сообщения в хронологическом порядке
        messages = []
        for msg in sorted(support_request.messages, key=lambda m: m.timestamp):
            photo_url = None
            if msg.photo_file_id:
                try:
                    photo_url = await resolve_photo_url(msg.photo_file_id)
                except Exception as e:
                    logger.error(f"[CHAT] Failed to load photo for message {msg.id}: {e}")
                    logger.debug(traceback.format_exc())
            messages.append({
                "text":         msg.text,
                "caption":      msg.caption,
                "photo_url":    photo_url,
                "timestamp":    msg.timestamp,
                "sender_id":    msg.sender_id,
                "is_user":      msg.sender_id == support_request.user.id,
                "is_moderator": msg.sender_id == (support_request.assigned_moderator_id or 0),
            })

        logger.info(f"[CHAT] Loaded {len(messages)} messages for request {request_id}")
        return templates.TemplateResponse("chat.html", {
            "request":         request,
            "support":         support_request,
            "messages":        messages,
        })

    except HTTPException:
        # Перехватываем 404 для «не найдена»
        raise
    except SQLAlchemyError as e:
        logger.exception(f"[CHAT] DB error on /chat/{request_id}: {e}")
        raise HTTPException(500, "Database error")
    except Exception as e:
        logger.exception(f"[CHAT] Unexpected error on /chat/{request_id}: {e}")
        raise HTTPException(500, "Internal server error")

if __name__ == "__main__":
    uvicorn.run("main:app", reload=True)