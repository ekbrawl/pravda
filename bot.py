import asyncio
import random
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = "9192313074:AAHh3Fs4OaruOoK0rU9tcK1L2pORXZC459k"
MAX_SKIPS = 3
COOLDOWN_TURNS = 15  # Через сколько собственных ходов игрока задание вернётся в пул

bot = Bot(token=TOKEN)
dp = Dispatcher()

TRUTHS = [
    "Какой самый кринжовый поступок ты совершал в этом году?",
    "Кому из присутствующих в чате ты бы доверил свой разблокированный телефон на час?",
    "О чём ты соврал в последний раз?",
    "Какая самая странная вещь есть в твоей истории поиска?",
]

DARES = [
    "Запиши кружочек в этот чат, где ты с максимально серьёзным лицом читаешь рэп 15 секунд.",
    "Поставь на аватарку в ТГ фото, которое выберет следующий игрок, на 2 часа.",
    "Отправь голосовое с пародией на кого-то из этой компании.",
    "Напиши любому человеку из списка контактов «Я всё знаю» и скинь скрин ответа.",
]

games = {}


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


def get_task_kb(skips_left: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Выполнено / Дальше", callback_data="next_turn")
    if skips_left > 0:
        kb.button(text=f"🔄 Скипнуть ({skips_left}/{MAX_SKIPS})", callback_data="skip_task")
    kb.button(text="🐔 Слился", callback_data="give_up")
    kb.adjust(1)
    return kb.as_markup()


def pick_task(game: dict, player_id: int, choice: str) -> str:
    p_state = game["players"][player_id]
    current_turn = p_state["turns"]

    # Снимаем кулдаун с заданий, у которых подошёл срок (unlock_turn <= current_turn)
    cooldowns = p_state["cooldowns"]
    expired = [task for task, unlock_turn in cooldowns.items() if unlock_turn <= current_turn]
    for task in expired:
        del cooldowns[task]

    pool = TRUTHS if choice == "truth" else DARES
    available = [
        t for t in pool
        if t not in cooldowns and t not in p_state["seen_in_session"]
    ]

    if not available:
        p_state["seen_in_session"].clear()
        available = [t for t in pool if t not in cooldowns]

    if not available:
        available = pool

    task = random.choice(available)
    p_state["seen_in_session"].add(task)
    p_state["active_task"] = task
    p_state["active_choice"] = choice

    header = "🗣 <b>ПРАВДА</b>" if choice == "truth" else "🔥 <b>ДЕЙСТВИЕ</b>"
    mention = p_state["mention"]
    skips_left = p_state["skips_left"]

    return (
        f"{header} для {mention} <i>(скипов: {skips_left}/{MAX_SKIPS})</i>:\n\n"
        f"— <i>{task}</i>"
    )


@dp.message(Command("newgame"))
async def cmd_newgame(message: types.Message):
    if message.chat.type == "private":
        await message.answer("Добавь меня в групповой чат, там и зарубимся!")
        return

    chat_id = message.chat.id
    games[chat_id] = {
        "mode": None,
        "players": {},
        "queue": [],
        "current_player": None,
        "started": False,
    }
    await message.answer(
        "🎮 <b>Новая игра «Правда или Действие»!</b>\nСначала выберите режим игры:",
        reply_markup=get_mode_kb(),
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
        await callback.answer("Набор уже закрыт или игра не создана.", show_alert=True)
        return

    user = callback.from_user
    if user.id in game["players"]:
        await callback.answer("Ты уже в списке!")
        return

    game["players"][user.id] = {
        "mention": user.mention_html(),
        "skips_left": MAX_SKIPS,
        "turns": 0,
        "cooldowns": {},
        "seen_in_session": set(),
        "active_task": None,
        "active_choice": None,
    }

    mode_name = "🎯 С выбором" if game["mode"] == "choice" else "🎲 Полный рандом"
    players_list = "\n".join(f"• {p['mention']}" for p in game["players"].values())

    await callback.message.edit_text(
        f"🎮 <b>Режим: {mode_name}</b>\n\n<b>Участники ({len(game['players'])}):</b>\n{players_list}",
        reply_markup=get_lobby_kb(),
        parse_mode="HTML",
    )
    await callback.answer("Ты в игре!")


async def next_round(message: types.Message, chat_id: int):
    game = games[chat_id]
    if not game["queue"]:
        game["queue"] = list(game["players"].keys())
        random.shuffle(game["queue"])

    player_id = game["queue"].pop(0)
    game["current_player"] = player_id

    p_state = game["players"][player_id]
    p_state["turns"] += 1
    p_state["active_task"] = None
    p_state["active_choice"] = None

    mention = p_state["mention"]

    if game["mode"] == "choice":
        await message.answer(
            f"👉 Ходит {mention} (ход #{p_state['turns']}, скипов: {p_state['skips_left']}/{MAX_SKIPS})!\n"
            f"Выбирай: <b>Правда</b> или <b>Действие</b>?",
            reply_markup=get_choice_kb(),
            parse_mode="HTML",
        )
    else:
        choice = random.choice(["truth", "dare"])
        text = pick_task(game, player_id, choice)
        await message.answer(
            f"🎲 Рандом решил судьбу!\n\n{text}",
            reply_markup=get_task_kb(p_state["skips_left"]),
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
    await callback.message.edit_text("🔥 <b>Игра началась!</b>", parse_mode="HTML")
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
    text = pick_task(game, player_id, choice)
    skips_left = game["players"][player_id]["skips_left"]

    await callback.message.edit_text(
        text,
        reply_markup=get_task_kb(skips_left),
        parse_mode="HTML",
    )


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

    p_state["skips_left"] -= 1
    if p_state["active_task"]:
        p_state["cooldowns"][p_state["active_task"]] = p_state["turns"] + COOLDOWN_TURNS

    choice = p_state["active_choice"] or random.choice(["truth", "dare"])
    new_text = pick_task(game, player_id, choice)

    await callback.message.edit_text(
        f"🔄 <i>Задание скипнуто! Вернётся через {COOLDOWN_TURNS} твоих ходов.</i>\n\n{new_text}",
        reply_markup=get_task_kb(p_state["skips_left"]),
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

    status = "✅ Засчитано!" if callback.data == "next_turn" else "🐔 Засчитан слив!"
    await callback.message.edit_text(
        f"{callback.message.html_text}\n\n<b>{status}</b>",
        parse_mode="HTML",
    )
    await next_round(callback.message, chat_id)


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
