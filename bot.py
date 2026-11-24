import asyncio
import json
import os
import random
import re
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = "9192313074:AAHh3Fs4OaruOoK0rU9tcK1L2pORXZC459k"
ADMIN_IDS = {1071268617}

MAX_SKIPS = 3
COOLDOWN_TURNS = 15

# Правило-ивент на каждые 30 раундов: от RULE_MIN_HITS до RULE_MAX_HITS срабатываний за 30 раундов
RULE_WINDOW_ROUNDS = 30
RULE_MIN_HITS = 0
RULE_MAX_HITS = 7

TASKS_FILE = "tasks.json"
HISTORY_FILE = "used_history.json"

DEFAULT_TASKS = {
    "truths": [
        "Какой самый кринжовый поступок ты совершал в этом году?",
        "Кому из присутствующих в чате ты бы доверил свой разблокированный телефон на час?",
        "О чём ты соврал в последний раз?",
        "Какая самая странная вещь есть в твоей истории поиска?",
    ],
    "dares": [
        "[timer:15] Запиши кружочек в этот чат, где ты с максимально серьёзным лицом читаешь рэп 15 секунд.",
        "[pair] Запиши голосовое, где ты 15 секунд пародируешь {target}.",
        "[pair] [timer:30] Напиши {target} сообщение и жди ответ полминуты.",
        "[timer:60] Напиши любому человеку из списка контактов «Я всё знаю» и скинь скрин ответа за минуту.",
    ],
    "rules": [
        "До конца этого раунда нельзя говорить слова «да», «нет» и «не знаю».",
        "Весь этот раунд говори максимально пафосно, как диктор трейлера.",
        "Назначенный игрок должен ответить на следующий вопрос любого участника в чате максимально честно.",
    ],
}

bot = Bot(token=TOKEN)
dp = Dispatcher()

pending_admin_upload: dict[int, str] = {}
games: dict[int, dict] = {}
active_timers: dict[tuple[int, int], asyncio.Task] = {}


# --- РАБОТА С БАЗОЙ ЗАДАНИЙ И ИСТОРИЕЙ МЕЖДУ ИГРАМИ ---

def load_tasks() -> dict[str, list[str]]:
    if not os.path.exists(TASKS_FILE):
        save_tasks(DEFAULT_TASKS)
        return {k: list(v) for k, v in DEFAULT_TASKS.items()}
    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {
        "truths": [str(x).strip() for x in data.get("truths", []) if str(x).strip()],
        "dares": [str(x).strip() for x in data.get("dares", []) if str(x).strip()],
        "rules": [str(x).strip() for x in data.get("rules", []) if str(x).strip()],
    }


def save_tasks(data: dict[str, list[str]]) -> None:
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_history() -> dict[str, dict[str, set[str]]]:
    if not os.path.exists(HISTORY_FILE):
        return {}
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    result = {}
    for chat_id_str, cats in raw.items():
        result[chat_id_str] = {
            "truths": set(cats.get("truths", [])),
            "dares": set(cats.get("dares", [])),
            "rules": set(cats.get("rules", [])),
        }
    return result


def save_history(history: dict[str, dict[str, set[str]]]) -> None:
    serializable = {}
    for chat_id_str, cats in history.items():
        serializable[chat_id_str] = {
            "truths": list(cats.get("truths", set())),
            "dares": list(cats.get("dares", set())),
            "rules": list(cats.get("rules", set())),
        }
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False)


tasks_db = load_tasks()
chat_history = load_history()


def get_chat_used(chat_id: int, category: str) -> set[str]:
    key = str(chat_id)
    if key not in chat_history:
        chat_history[key] = {"truths": set(), "dares": set(), "rules": set()}
    chat_history[key].setdefault(category, set())
    return chat_history[key][category]


def mark_chat_used(chat_id: int, category: str, task_text: str) -> None:
    used = get_chat_used(chat_id, category)
    used.add(task_text)
    save_history(chat_history)


def extract_timer_seconds(task_text: str) -> tuple[str, int | None]:
    match = re.search(r"\[(?:timer|t)\s*:\s*(\d+)\]", task_text, flags=re.IGNORECASE)
    if not match:
        return task_text, None
    seconds = int(match.group(1))
    clean_text = re.sub(r"\[(?:timer|t)\s*:\s*\d+\]", "", task_text, flags=re.IGNORECASE).strip()
    clean_text = re.sub(r"\s{2,}", " ", clean_text)
    return clean_text, seconds if seconds > 0 else None


def resolve_pair_interaction(game: dict, actor_id: int, task_text: str) -> tuple[str, str | None]:
    """
    Если задание помечено как парное ([pair] или содержит {target}):
    - Выбирает случайного второго игрока из активных участников (кроме actor_id, если есть другие).
    - Подставляет его упоминание вместо {target}.
    Возвращает (обработанный_текст, упоминание_второго_игрока_или_None).
    """
    has_pair_tag = bool(re.search(r"\[pair\]", task_text, flags=re.IGNORECASE))
    has_target_placeholder = "{target}" in task_text

    text = re.sub(r"\[pair\]", "", task_text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\s{2,}", " ", text)

    if not has_pair_tag and not has_target_placeholder:
        return text, None

    candidates = [uid for uid in game["players"].keys() if uid != actor_id]
    if not candidates:
        candidates = list(game["players"].keys())
    if not candidates:
        return text.replace("{target}", "другому игроку"), None

    partner_id = random.choice(candidates)
    partner_mention = game["players"][partner_id]["mention"]

    if "{target}" in text:
        text = text.replace("{target}", partner_mention)
    else:
        text = f"{text} (в паре с {partner_mention})"

    return text, partner_mention


def parse_bulk_items(raw_text: str) -> list[str]:
    """
    Парсит пачку пунктов:
    - Поддерживает [timer:30], [pair], [Текст задания]
    - Поддерживает просто каждый пункт с новой строки
    """
    lines_result = []
    for line in raw_text.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith(("/add_truths", "/add_dares", "/add_rules")):
            continue

        prefixes = []
        for pattern in (r"\[pair\]", r"\[(?:timer|t)\s*:\s*\d+\]"):
            found = re.findall(pattern, cleaned, flags=re.IGNORECASE)
            for tag in found:
                prefixes.append(tag)
                cleaned = cleaned.replace(tag, "", 1).strip()

        prefix_str = " ".join(prefixes) + (" " if prefixes else "")

        inner_matches = re.findall(r"\[([^\[\]]+?)\]", cleaned)
        if inner_matches:
            for item in inner_matches:
                item_clean = re.sub(r"\s+", " ", item).strip()
                if item_clean:
                    lines_result.append(f"{prefix_str}{item_clean}".strip())
        else:
            if cleaned:
                lines_result.append(f"{prefix_str}{cleaned}".strip())

    return lines_result


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def make_player_state(mention_html: str) -> dict:
    return {
        "mention": mention_html,
        "skips_left": MAX_SKIPS,
        "turns": 0,
        "done_count": 0,
        "skip_count": 0,
        "giveup_count": 0,
        "cooldowns": {},
        "active_task": None,
        "active_choice": None,
        "timer_seconds": None,
    }


def plan_rule_trigger_rounds(window_start: int, window_end: int) -> set[int]:
    if not tasks_db["rules"]:
        return set()
    all_rounds = list(range(window_start, window_end + 1))
    max_possible = min(RULE_MAX_HITS, len(all_rounds))
    min_possible = min(RULE_MIN_HITS, max_possible)
    hits = random.randint(min_possible, max_possible)
    if hits <= 0:
        return set()
    return set(random.sample(all_rounds, k=hits))


def ensure_rule_plan_for_round(game: dict, round_num: int) -> bool:
    plan = game["rule_plan"]
    if round_num > plan["window_end"]:
        new_start = round_num
        new_end = round_num + RULE_WINDOW_ROUNDS - 1
        game["rule_plan"] = {
            "window_start": new_start,
            "window_end": new_end,
            "trigger_rounds": plan_rule_trigger_rounds(new_start, new_end),
        }
        plan = game["rule_plan"]
    return round_num in plan["trigger_rounds"]


def pick_random_rule_text(chat_id: int, target_mention: str) -> tuple[str, int | None]:
    pool = tasks_db["rules"]
    if not pool:
        return "", None
    used = get_chat_used(chat_id, "rules")
    available = [r for r in pool if r not in used]
    if not available:
        used.clear()
        save_history(chat_history)
        available = pool
    raw_rule = random.choice(available)
    mark_chat_used(chat_id, "rules", raw_rule)
    clean_rule, timer_sec = extract_timer_seconds(raw_rule)
    clean_rule = clean_rule.replace("{target}", target_mention)
    timer_note = f"\n⏱ <b>На выполнение:</b> {format_timer_label(timer_sec)}" if timer_sec else ""
    text = (
        f"⚡ <b>СПЕЦ-ПРАВИЛО РАУНДА!</b>\n"
        f"Бот назначил: {target_mention}\n\n"
        f"— <i>{clean_rule}</i>{timer_note}"
    )
    return text, timer_sec


# --- КЛАВИАТУРЫ ---

def get_mode_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎯 С выбором (Правда / Действие)", callback_data="mode_choice")
    kb.button(text="🎲 Полный рандом", callback_data="mode_random")
    kb.adjust(1)
    return kb.as_markup()


def get_lobby_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✋ Я в игре!", callback_data="join")
    kb.button(text="🚀 Начать игру", callback_data="start_game")
    kb.adjust(1)
    return kb.as_markup()


def get_choice_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🗣 Правда", callback_data="pick_truth")
    kb.button(text="🔥 Действие", callback_data="pick_dare")
    kb.adjust(2)
    return kb.as_markup()


def get_task_kb(skips_left: int, timer_seconds: int | None = None, timer_started: bool = False):
    kb = InlineKeyboardBuilder()
    if timer_seconds and not timer_started:
        kb.button(text=f"⏱ Запустить таймер ({timer_seconds} сек)", callback_data="start_timer")
    kb.button(text="✅ Выполнено / Дальше", callback_data="next_turn")
    if skips_left > 0:
        kb.button(text=f"🔄 Скипнуть ({skips_left}/{MAX_SKIPS})", callback_data="skip_task")
    kb.button(text="🐔 Слился", callback_data="give_up")
    kb.adjust(1)
    return kb.as_markup()


def format_timer_label(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} сек."
    mins, secs = divmod(seconds, 60)
    return f"{mins} мин. {secs} сек." if secs else f"{mins} мин."


def pick_task(chat_id: int, game: dict, player_id: int, choice: str) -> tuple[str, int | None]:
    p_state = game["players"][player_id]
    current_turn = p_state["turns"]

    cooldowns = p_state["cooldowns"]
    expired = [task for task, unlock_turn in cooldowns.items() if unlock_turn <= current_turn]
    for task in expired:
        del cooldowns[task]

    category = "truths" if choice == "truth" else "dares"
    pool = tasks_db[category]
    if not pool:
        return (
            "⚠️ В базе пока нет заданий этого типа. Админ может добавить их в личке через /add_truths или /add_dares.",
            None,
        )

    global_used = get_chat_used(chat_id, category)
    available = [t for t in pool if t not in global_used and t not in cooldowns]

    if not available:
        global_used.clear()
        save_history(chat_history)
        available = [t for t in pool if t not in cooldowns]

    if not available:
        available = pool

    raw_task = random.choice(available)
    mark_chat_used(chat_id, category, raw_task)

    clean_task, timer_sec = extract_timer_seconds(raw_task)
    pair_note = ""
    if choice == "dare":
        clean_task, partner_mention = resolve_pair_interaction(game, player_id, clean_task)
        if partner_mention:
            pair_note = f"\n🤝 <b>Второй игрок:</b> {partner_mention}"

    p_state["active_task"] = raw_task
    p_state["active_choice"] = choice
    p_state["timer_seconds"] = timer_sec

    header = "🗣 <b>ПРАВДА</b>" if choice == "truth" else "🔥 <b>ДЕЙСТВИЕ</b>"
    mention = p_state["mention"]
    skips_left = p_state["skips_left"]
    timer_note = f"\n⏱ <b>На выполнение:</b> {format_timer_label(timer_sec)}" if timer_sec else ""

    text = (
        f"{header} для {mention} <i>(скипов: {skips_left}/{MAX_SKIPS})</i>:\n\n"
        f"— <i>{clean_task}</i>{pair_note}{timer_note}"
    )
    return text, timer_sec


def cancel_chat_timers(chat_id: int):
    to_remove = [key for key in active_timers if key[0] == chat_id]
    for key in to_remove:
        task = active_timers.pop(key)
        task.cancel()


def build_rating_text(game: dict) -> str:
    if not game["players"]:
        return "В игре не было участников."

    sorted_players = sorted(
        game["players"].values(),
        key=lambda p: (-p["done_count"], p["giveup_count"], p["skip_count"]),
    )
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>ИТОГОВЫЙ РЕЙТИНГ ИГРЫ:</b>\n"]
    for idx, p in enumerate(sorted_players):
        prefix = medals[idx] if idx < 3 else f"{idx + 1}."
        lines.append(
            f"{prefix} {p['mention']} — ✅ <b>{p['done_count']}</b> | 🔄 {p['skip_count']} | 🐔 {p['giveup_count']}"
        )
    return "\n".join(lines)


# --- АДМИН-КОМАНДЫ В ЛИЧКЕ ---

async def start_admin_upload(message: types.Message, category: str):
    if message.chat.type != "private":
        return
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Эта команда доступна только администратору бота.")
        return

    parts = message.text.split(maxsplit=1) if message.text else []
    if len(parts) > 1 and parts[1].strip():
        await process_admin_items(message, category, parts[1])
        return

    pending_admin_upload[message.from_user.id] = category
    titles = {
        "truths": "ПРАВДУ",
        "dares": "ДЕЙСТВИЯ",
        "rules": "СПЕЦ-ПРАВИЛА (на 30 раундов)",
    }
    await message.answer(
        f"✍️ Режим добавления в раздел <b>{titles[category]}</b> включён.\n\n"
        f"Скинь пачку пунктов следующим сообщением:\n"
        f"<code>[Первый пункт]\n[pair] [timer:30] [Действие с {target} на 30 сек]</code>",
        parse_mode="HTML",
    )


async def process_admin_items(message: types.Message, category: str, raw_text: str):
    new_items = parse_bulk_items(raw_text)
    if not new_items:
        await message.answer("⚠️ Не удалось распознать ни одного пункта.")
        return

    existing_set = set(tasks_db[category])
    added = []
    skipped_dupes = 0

    for item in new_items:
        if item not in existing_set:
            tasks_db[category].append(item)
            existing_set.add(item)
            added.append(item)
        else:
            skipped_dupes += 1

    save_tasks(tasks_db)
    titles = {
        "truths": "Правда",
        "dares": "Действия",
        "rules": "Спец-правила",
    }
    preview = "\n".join(f"• {x}" for x in added[:10])
    more_note = f"\n<i>...и ещё {len(added) - 10} шт.</i>" if len(added) > 10 else ""
    dupe_note = f"\n⏭ Пропущено дублей: {skipped_dupes}" if skipped_dupes else ""

    await message.answer(
        f"✅ Добавлено в раздел <b>{titles[category]}</b>: <b>{len(added)}</b> шт. (всего в базе: {len(tasks_db[category])})\n"
        f"{preview}{more_note}{dupe_note}",
        parse_mode="HTML",
    )


@dp.message(Command("add_truths"))
async def cmd_add_truths(message: types.Message):
    await start_admin_upload(message, "truths")


@dp.message(Command("add_dares"))
async def cmd_add_dares(message: types.Message):
    await start_admin_upload(message, "dares")


@dp.message(Command("add_rules"))
async def cmd_add_rules(message: types.Message):
    await start_admin_upload(message, "rules")


@dp.message(Command("tasks_info"))
async def cmd_tasks_info(message: types.Message):
    if message.chat.type != "private" or not is_admin(message.from_user.id):
        return
    await message.answer(
        f"📊 <b>База заданий ({TASKS_FILE}):</b>\n"
        f"• Правда: <b>{len(tasks_db['truths'])}</b>\n"
        f"• Действия: <b>{len(tasks_db['dares'])}</b>\n"
        f"• Спец-правила (на 30 раундов): <b>{len(tasks_db['rules'])}</b>",
        parse_mode="HTML",
    )


@dp.message(Command("clear_tasks"))
async def cmd_clear_tasks(message: types.Message):
    if message.chat.type != "private" or not is_admin(message.from_user.id):
        return
    args = (message.text or "").split()
    valid = ("truth", "truths", "dare", "dares", "rule", "rules", "all")
    if len(args) < 2 or args[1].lower() not in valid:
        await message.answer(
            "Используй:\n"
            "<code>/clear_tasks truth</code> — очистить Правду\n"
            "<code>/clear_tasks dare</code> — очистить Действия\n"
            "<code>/clear_tasks rule</code> — очистить Спец-правила\n"
            "<code>/clear_tasks all</code> — очистить всё",
            parse_mode="HTML",
        )
        return

    target = args[1].lower()
    if target in ("truth", "truths", "all"):
        tasks_db["truths"].clear()
    if target in ("dare", "dares", "all"):
        tasks_db["dares"].clear()
    if target in ("rule", "rules", "all"):
        tasks_db["rules"].clear()
    save_tasks(tasks_db)
    await message.answer("🗑 Выбранный раздел очищен.")


@dp.message(F.chat.type == "private", F.text)
async def handle_private_text(message: types.Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    category = pending_admin_upload.pop(user_id, None)
    if not category:
        await message.answer(
            "👋 Привет, админ! Команды в личке:\n"
            "• <code>/add_truths</code> — добавить вопросы\n"
            "• <code>/add_dares</code> — добавить действия (<code>[pair]</code>, <code>{target}</code>, <code>[timer:30]</code>)\n"
            "• <code>/add_rules</code> — добавить спец-правила (на окно 30 раундов)\n"
            "• <code>/tasks_info</code> — статистика базы\n"
            "• <code>/clear_tasks truth|dare|rule|all</code> — очистка",
            parse_mode="HTML",
        )
        return
    await process_admin_items(message, category, message.text)


# --- ИГРОВАЯ ЛОГИКА И УПРАВЛЕНИЕ ИГРОКАМИ В ГРУППЕ ---

@dp.message(Command("newgame"))
async def cmd_newgame(message: types.Message):
    if message.chat.type == "private":
        await message.answer("Добавь меня в групповой чат, там и зарубимся!")
        return

    chat_id = message.chat.id
    if chat_id in games and games[chat_id]["started"]:
        await message.answer("⚠️ В этом чате уже идёт игра! Сначала заверши её через /stopgame.")
        return

    cancel_chat_timers(chat_id)
    first_start = 1
    first_end = RULE_WINDOW_ROUNDS
    games[chat_id] = {
        "mode": None,
        "players": {},
        "queue": [],
        "current_player": None,
        "round_number": 0,
        "rule_plan": {
            "window_start": first_start,
            "window_end": first_end,
            "trigger_rounds": plan_rule_trigger_rounds(first_start, first_end),
        },
        "started": False,
    }
    await message.answer(
        "🎮 <b>Новая игра «Правда или Действие»!</b>\nСначала выберите режим игры:",
        reply_markup=get_mode_kb(),
        parse_mode="HTML",
    )


@dp.message(Command("join"))
async def cmd_join_midgame(message: types.Message):
    if message.chat.type == "private":
        return
    chat_id = message.chat.id
    game = games.get(chat_id)
    if not game:
        await message.answer("Сейчас нет активной игры. Запусти через /newgame.")
        return

    user = message.from_user
    if user.id in game["players"]:
        await message.answer(f"{user.mention_html()}, ты уже в игре!", parse_mode="HTML")
        return

    game["players"][user.id] = make_player_state(user.mention_html())
    if game["started"]:
        insert_pos = random.randint(0, len(game["queue"]))
        game["queue"].insert(insert_pos, user.id)
        await message.answer(
            f"➕ {user.mention_html()} залетел в игру на ходу! Скоро дойдёт очередь.",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"✋ {user.mention_html()} добавлен в лобби!",
            parse_mode="HTML",
        )


@dp.message(Command("leave"))
async def cmd_leave_game(message: types.Message):
    if message.chat.type == "private":
        return
    chat_id = message.chat.id
    game = games.get(chat_id)
    user = message.from_user
    if not game or user.id not in game["players"]:
        await message.answer("Тебя нет в списке игроков.")
        return

    await remove_player_from_game(message, chat_id, user.id, reason="вышел из игры")


@dp.message(Command("kick"))
async def cmd_kick_player(message: types.Message):
    if message.chat.type == "private":
        return
    chat_id = message.chat.id
    game = games.get(chat_id)
    if not game or not game["players"]:
        await message.answer("Активной игры или игроков сейчас нет.")
        return

    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
        if target_id not in game["players"]:
            await message.answer("Этого пользователя нет в текущей игре.")
            return
        await remove_player_from_game(message, chat_id, target_id, reason="удалён из игры")
        return

    kb = InlineKeyboardBuilder()
    for uid, p in game["players"].items():
        plain_name = re.sub(r"<[^>]+>", "", p["mention"])
        kb.button(text=f"❌ {plain_name}", callback_data=f"kick_{uid}")
    kb.adjust(1)
    await message.answer("Кого удалить из текущей игры?", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("kick_"))
async def cb_kick_player(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    game = games.get(chat_id)
    if not game:
        await callback.answer("Игра уже завершена.", show_alert=True)
        return

    target_id = int(callback.data.split("_")[1])
    if target_id not in game["players"]:
        await callback.answer("Игрок уже удалён.", show_alert=True)
        return

    await callback.message.delete()
    await remove_player_from_game(callback.message, chat_id, target_id, reason="удалён из игры")


async def remove_player_from_game(message: types.Message, chat_id: int, target_id: int, reason: str):
    game = games.get(chat_id)
    if not game or target_id not in game["players"]:
        return

    mention = game["players"][target_id]["mention"]
    del game["players"][target_id]
    game["queue"] = [uid for uid in game["queue"] if uid != target_id]

    await message.answer(f"🚪 {mention} {reason}.", parse_mode="HTML")

    if game["started"] and len(game["players"]) < 2:
        cancel_chat_timers(chat_id)
        rating = build_rating_text(game)
        del games[chat_id]
        await message.answer(
            f"🏁 Осталось меньше 2 игроков — игра завершена!\n\n{rating}",
            parse_mode="HTML",
        )
        return

    if game["started"] and game["current_player"] == target_id:
        cancel_chat_timers(chat_id)
        await next_round(message, chat_id)


@dp.message(Command("stopgame"))
async def cmd_stopgame(message: types.Message):
    if message.chat.type == "private":
        return
    chat_id = message.chat.id
    game = games.get(chat_id)
    if not game:
        await message.answer("Сейчас нет запущенной игры.")
        return

    cancel_chat_timers(chat_id)
    rating = build_rating_text(game)
    del games[chat_id]

    await message.answer(
        f"🛑 <b>Игра остановлена!</b>\n"
        f"<i>Все сыгранные задания и правила запомнены и не повторятся в следующих партиях.</i>\n\n{rating}",
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("mode_"))
async def cb_mode(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    game = games.get(chat_id)
    if not game or game["mode"] is not None:
        await callback.answer("Режим уже выбран или игра не создана.", show_alert=True)
        return

    mode = callback.data.split("_")[1]
    game["mode"] = mode
    mode_name = "🎯 С выбором" if mode == "choice" else "🎲 Полный рандом"

    await callback.message.edit_text(
        f"🎮 <b>Режим: {mode_name}</b>\n\nЖмите кнопку ниже, чтобы залететь в лобби!",
        reply_markup=get_lobby_kb(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "join")
async def cb_join(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    game = games.get(chat_id)
    if not game or game["started"]:
        await callback.answer("Набор в лобби закрыт. Используй /join в чате, чтобы войти в игру!", show_alert=True)
        return

    user = callback.from_user
    if user.id in game["players"]:
        await callback.answer("Ты уже в списке!")
        return

    game["players"][user.id] = make_player_state(user.mention_html())
    mode_name = "🎯 С выбором" if game["mode"] == "choice" else "🎲 Полный рандом"
    players_list = "\n".join(f"• {p['mention']}" for p in game["players"].values())

    await callback.message.edit_text(
        f"🎮 <b>Режим: {mode_name}</b>\n\n<b>Участники ({len(game['players'])}):</b>\n{players_list}",
        reply_markup=get_lobby_kb(),
        parse_mode="HTML",
    )
    await callback.answer("Ты в игре!")


async def next_round(message: types.Message, chat_id: int):
    game = games.get(chat_id)
    if not game or not game["players"]:
        return

    game["round_number"] += 1
    round_num = game["round_number"]

    if ensure_rule_plan_for_round(game, round_num) and tasks_db["rules"]:
        lucky_id = random.choice(list(game["players"].keys()))
        lucky_mention = game["players"][lucky_id]["mention"]
        rule_text, rule_timer = pick_random_rule_text(chat_id, lucky_mention)
        if rule_text:
            await message.answer(rule_text, parse_mode="HTML")
            if rule_timer:
                await asyncio.sleep(0.2)

    if not game["queue"]:
        game["queue"] = list(game["players"].keys())
        random.shuffle(game["queue"])

    player_id = game["queue"].pop(0)
    if player_id not in game["players"]:
        await next_round(message, chat_id)
        return

    game["current_player"] = player_id
    p_state = game["players"][player_id]
    p_state["turns"] += 1
    p_state["active_task"] = None
    p_state["active_choice"] = None
    p_state["timer_seconds"] = None

    mention = p_state["mention"]

    if game["mode"] == "choice":
        await message.answer(
            f"👉 <b>Раунд #{round_num}</b> | Ходит {mention} (ход #{p_state['turns']}, скипов: {p_state['skips_left']}/{MAX_SKIPS})!\n"
            f"Выбирай: <b>Правда</b> или <b>Действие</b>?",
            reply_markup=get_choice_kb(),
            parse_mode="HTML",
        )
    else:
        choice = random.choice(["truth", "dare"])
        text, timer_sec = pick_task(chat_id, game, player_id, choice)
        await message.answer(
            f"🎲 <b>Раунд #{round_num}</b> — рандом решил судьбу!\n\n{text}",
            reply_markup=get_task_kb(p_state["skips_left"], timer_sec),
            parse_mode="HTML",
        )


@dp.callback_query(F.data == "start_game")
async def cb_start_game(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    game = games.get(chat_id)
    if not game or len(game["players"]) < 2:
        await callback.answer("Нужно минимум 2 игрока!", show_alert=True)
        return

    game["started"] = True
    await callback.message.edit_text(
        "🔥 <b>Игра началась!</b>\n<i>(Войти на ходу: /join | Выйти: /leave | Кикнуть: /kick | Стоп: /stopgame)</i>",
        parse_mode="HTML",
    )
    await next_round(callback.message, chat_id)


@dp.callback_query(F.data.startswith("pick_"))
async def cb_pick(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    game = games.get(chat_id)
    player_id = callback.from_user.id
    if not game or player_id != game["current_player"]:
        await callback.answer("Сейчас не твой ход!", show_alert=True)
        return

    choice = callback.data.split("_")[1]
    text, timer_sec = pick_task(chat_id, game, player_id, choice)
    skips_left = game["players"][player_id]["skips_left"]

    await callback.message.edit_text(
        text,
        reply_markup=get_task_kb(skips_left, timer_sec),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "start_timer")
async def cb_start_timer(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    game = games.get(chat_id)
    player_id = callback.from_user.id
    if not game or player_id != game["current_player"]:
        await callback.answer("Таймер запускает только текущий игрок!", show_alert=True)
        return

    p_state = game["players"][player_id]
    seconds = p_state.get("timer_seconds")
    if not seconds:
        await callback.answer("У этого задания нет таймера.", show_alert=True)
        return

    msg_id = callback.message.message_id
    timer_key = (chat_id, msg_id)
    if timer_key in active_timers:
        await callback.answer("Таймер уже запущен!")
        return

    await callback.message.edit_text(
        f"{callback.message.html_text}\n\n⏳ <b>Таймер пошёл ({format_timer_label(seconds)})!</b>",
        reply_markup=get_task_kb(p_state["skips_left"], seconds, timer_started=True),
        parse_mode="HTML",
    )
    await callback.answer(f"Время пошло: {seconds} сек!")

    async def timer_worker():
        try:
            await asyncio.sleep(seconds)
            if chat_id in games and games[chat_id].get("current_player") == player_id:
                await bot.send_message(
                    chat_id,
                    f"⏰ <b>Время вышло!</b> {p_state['mention']}, пора отчитываться!",
                    reply_to_message_id=msg_id,
                    parse_mode="HTML",
                )
        except asyncio.CancelledError:
            pass
        finally:
            active_timers.pop(timer_key, None)

    active_timers[timer_key] = asyncio.create_task(timer_worker())


@dp.callback_query(F.data == "skip_task")
async def cb_skip_task(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    game = games.get(chat_id)
    player_id = callback.from_user.id
    if not game or player_id != game["current_player"]:
        await callback.answer("Скинуть задание может только текущий игрок!", show_alert=True)
        return

    p_state = game["players"][player_id]
    if p_state["skips_left"] <= 0:
        await callback.answer("Скипы закончились!", show_alert=True)
        return

    cancel_chat_timers(chat_id)
    p_state["skips_left"] -= 1
    p_state["skip_count"] += 1

    if p_state["active_task"]:
        p_state["cooldowns"][p_state["active_task"]] = p_state["turns"] + COOLDOWN_TURNS

    choice = p_state["active_choice"] or random.choice(["truth", "dare"])
    new_text, timer_sec = pick_task(chat_id, game, player_id, choice)

    await callback.message.edit_text(
        f"🔄 <i>Задание скипнуто! Вернётся через {COOLDOWN_TURNS} твоих ходов.</i>\n\n{new_text}",
        reply_markup=get_task_kb(p_state["skips_left"], timer_sec),
        parse_mode="HTML",
    )
    await callback.answer(f"Скип использован! Осталось: {p_state['skips_left']}")


@dp.callback_query(F.data.in_(["next_turn", "give_up"]))
async def cb_finish_turn(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    game = games.get(chat_id)
    player_id = callback.from_user.id
    if not game or player_id != game["current_player"]:
        await callback.answer("Нажать может только текущий игрок!", show_alert=True)
        return

    cancel_chat_timers(chat_id)
    p_state = game["players"][player_id]

    if callback.data == "next_turn":
        p_state["done_count"] += 1
        status = "✅ Засчитано!"
    else:
        p_state["giveup_count"] += 1
        status = "🐔 Засчитан слив!"

    await callback.message.edit_text(
        f"{callback.message.html_text}\n\n<b>{status}</b>",
        parse_mode="HTML",
    )
    await next_round(callback.message, chat_id)


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
