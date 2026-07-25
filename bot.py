import discord
from discord.ext import commands, tasks
import json
import os
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# ─── Настройки ──────────────────────────────────────────────────────────────
# Токен можно задать через переменную окружения DISCORD_TOKEN,
# либо вписать прямо в кавычки ниже вместо плейсхолдера.
TOKEN = os.getenv("DISCORD_TOKEN", "ВАШ_ТОКЕН_СЮДА")

# Часовой пояс, по которому считаются даты неявок и момент удаления сообщений.
# МСК (Europe/Moscow) — в России отменён переход на летнее/зимнее время,
# поэтому это всегда UTC+3, без сюрпризов.
TIMEZONE = ZoneInfo("Europe/Moscow")

# Файл данных всегда лежит рядом со скриптом, независимо от того, из какой
# папки бот запущен (двойной клик, ярлык, планировщик задач и т.д.) —
# раньше путь был относительным и после рестарта другим способом бот мог
# не найти уже сохранённый канал и создать новый пустой data.json.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data.json")

intents = discord.Intents.default()
intents.message_content = True  # обязательно для команд с префиксом "!"

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# ─── Хранилище (каналы + отложенные удаления сообщений) ────────────────────
def load_state() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"⚠️ Не удалось прочитать {DATA_FILE} ({exc}), начинаю с пустого состояния.")
            raw = {}
    else:
        raw = {}
    raw.setdefault("channels", {})           # {"<guild_id>": <channel_id>}
    raw.setdefault("pending_deletions", [])  # [{message_id, channel_id, delete_at}]
    return raw


def save_state() -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


state = load_state()
print(f"📄 Файл данных: {DATA_FILE}")
print(f"📌 Загружено каналов для заявок: {len(state['channels'])}, "
      f"отложенных удалений: {len(state['pending_deletions'])}")


# ─── Строгая проверка даты / диапазона дат (без года) ────────────────────────
# Разрешён ТОЛЬКО формат "ДД.ММ" или "ДД.ММ-ДД.ММ" — никаких букв и года.
DATE_RE = re.compile(
    r"^\s*(\d{2})\.(\d{2})\s*(?:-\s*(\d{2})\.(\d{2}))?\s*$"
)


def parse_date_range(raw: str):
    """
    Принимает "ДД.ММ" или "ДД.ММ-ДД.ММ" (год пользователь не вводит).

    Год всегда берётся текущий (по TIMEZONE) — никакого переноса на
    следующий год не происходит.

    Возвращает (start_date, end_date), либо None при некорректном вводе:
    - формат не соответствует ДД.ММ[-ДД.ММ]
    - несуществующая календарная дата (например 31.02)
    - диапазон "перевёрнут" (конец раньше начала)
    - дата/диапазон уже полностью в прошлом (в рамках текущего года)
    """
    match = DATE_RE.match(raw)
    if not match:
        return None

    d1_s, m1_s, d2_s, m2_s = match.groups()
    day1, month1 = int(d1_s), int(m1_s)

    today = datetime.now(TIMEZONE).date()
    year = today.year  # всегда текущий год, без переноса

    try:
        start = date(year, month1, day1)
    except ValueError:
        return None

    if d2_s:
        day2, month2 = int(d2_s), int(m2_s)
        try:
            end = date(year, month2, day2)
        except ValueError:
            return None
        if end < start:
            return None
    else:
        end = start

    if end < today:
        return None

    return start, end


# ─── Безопасное получение канала (даже если он не в кэше) ────────────────────
async def resolve_channel(channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


# ─── Модалка (форма подачи заявки) ───────────────────────────────────────────
class NeyavkaModal(discord.ui.Modal, title="Заявка на неявку — STALZONE"):
    nickname = discord.ui.TextInput(
        label="Ник в игре STALZONE",
        placeholder="Введите ваш игровой ник",
        required=True,
        max_length=100,
    )
    dates = discord.ui.TextInput(
        label="Даты отсутствия (ДД.ММ)",
        placeholder="25.07 или 25.07-30.07",
        required=True,
        max_length=15,
    )
    reason = discord.ui.TextInput(
        label="Причина",
        style=discord.TextStyle.paragraph,
        placeholder="Укажите причину отсутствия",
        required=True,
        max_length=500,
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)
        channel_id = state["channels"].get(guild_id)

        if channel_id is None:
            await interaction.response.send_message(
                "⚠️ Канал для приёма заявок ещё не настроен. "
                "Администратор должен выполнить команду `!here` в нужном канале.",
                ephemeral=True,
            )
            return

        channel = await resolve_channel(channel_id)
        if channel is None:
            await interaction.response.send_message(
                "⚠️ Не удалось найти сохранённый канал. Настройте его заново через `!here`.",
                ephemeral=True,
            )
            return

        parsed = parse_date_range(self.dates.value.strip())
        if parsed is None:
            await interaction.response.send_message(
                "⚠️ Неверный формат даты. Введите строго `ДД.ММ` "
                "(например `25.07`) или диапазон `ДД.ММ-ДД.ММ` "
                "(например `25.07-30.07`). Год указывать не нужно — используется "
                "текущий. Буквы и другие символы недопустимы, дата должна "
                "существовать в календаре и не может быть уже в прошлом.",
                ephemeral=True,
            )
            return

        start_date, end_date = parsed
        if start_date == end_date:
            dates_display = start_date.strftime("%d.%m")
        else:
            dates_display = f"{start_date.strftime('%d.%m')} — {end_date.strftime('%d.%m')}"

        embed = discord.Embed(
            title="📋 Неявка",
            color=discord.Color.red(),
            timestamp=datetime.now(TIMEZONE),
        )
        embed.add_field(name="Ник в игре", value=self.nickname.value, inline=False)
        embed.add_field(name="Даты отсутствия", value=dates_display, inline=False)
        embed.add_field(name="Причина", value=self.reason.value, inline=False)
        # Упоминание внутри embed — рендерится кликабельным, но НЕ пингует автора
        embed.add_field(name="Оставил(а) заявку", value=interaction.user.mention, inline=False)
        embed.set_footer(text="Сообщение удалится автоматически по окончании периода неявки")

        try:
            sent_message = await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),  # доп. гарантия от пинга
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "⚠️ У бота нет прав писать в канал заявок. Проверьте права бота на этом канале.",
                ephemeral=True,
            )
            return

        delete_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=TIMEZONE)
        state["pending_deletions"].append(
            {
                "message_id": sent_message.id,
                "channel_id": channel.id,
                "delete_at": delete_at.isoformat(),
            }
        )
        save_state()

        await interaction.response.send_message("✅ Заявка на неявку отправлена!", ephemeral=True)


# ─── Кнопка «Неявка» ──────────────────────────────────────────────────────────
class NeyavkaView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # вечная кнопка, переживает рестарт бота

    @discord.ui.button(
        label="Неявка",
        style=discord.ButtonStyle.danger,
        custom_id="stalzone_neyavka_button",
    )
    async def neyavka_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NeyavkaModal())


# ─── Удаление сообщений по истечении срока неявки ────────────────────────────
async def perform_cleanup() -> None:
    """
    Одна проверка всех отложенных удалений: находит просроченные записи,
    удаляет соответствующие сообщения и убирает их из state.

    Вызывается:
    1) один раз сразу при подключении бота (on_ready) — чтобы наверстать
       удаления, которые должны были произойти, пока бот был офлайн;
    2) затем регулярно каждые 30 секунд через cleanup_loop, пока бот работает.
    """
    now = datetime.now(TIMEZONE)
    still_pending = []
    changed = False

    for entry in state["pending_deletions"]:
        delete_at = datetime.fromisoformat(entry["delete_at"])
        if now < delete_at:
            still_pending.append(entry)
            continue

        changed = True
        channel = await resolve_channel(entry["channel_id"])
        if channel is not None:
            try:
                msg = await channel.fetch_message(entry["message_id"])
                await msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    if changed:
        state["pending_deletions"] = still_pending
        save_state()


@tasks.loop(seconds=30)
async def cleanup_loop():
    await perform_cleanup()


@cleanup_loop.before_loop
async def before_cleanup():
    await bot.wait_until_ready()


# ─── События и команды ────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    bot.add_view(NeyavkaView())

    # Наверстываем удаления, которые должны были произойти, пока бот был офлайн
    await perform_cleanup()

    if not cleanup_loop.is_running():
        cleanup_loop.start()

    print(f"✅ Бот запущен: {bot.user} (ID: {bot.user.id})")


@bot.command(name="button")
@commands.has_permissions(manage_guild=True)
async def button_cmd(ctx: commands.Context):
    embed = discord.Embed(
        title="📋 Отметка о неявке — STALZONE",
        description=(
            "Если вы не сможете присутствовать, нажмите кнопку **«Неявка»** ниже "
            "и заполните форму: ник, даты отсутствия и причину."
        ),
        color=discord.Color.red(),
    )
    await ctx.send(embed=embed, view=NeyavkaView())


@bot.command(name="here")
@commands.has_permissions(manage_guild=True)
async def here_cmd(ctx: commands.Context):
    state["channels"][str(ctx.guild.id)] = ctx.channel.id
    save_state()
    await ctx.send(f"✅ Заявки о неявке теперь будут приходить в канал {ctx.channel.mention}.")


@button_cmd.error
@here_cmd.error
async def setup_commands_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("⛔ У вас нет прав для использования этой команды (нужны права «Управление сервером»).")
    else:
        raise error


if __name__ == "__main__":
    if TOKEN == "ВАШ_ТОКЕН_СЮДА":
        raise SystemExit(
            "Укажите токен бота: переменная окружения DISCORD_TOKEN, "
            "либо впишите его напрямую в переменную TOKEN в этом файле."
        )
    bot.run(TOKEN)
