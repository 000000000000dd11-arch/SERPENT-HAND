import discord
from discord.ext import commands, tasks
import json
import os
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TOKEN = os.getenv("DISCORD_TOKEN", "ВАШ_ТОКЕН_СЮДА")
TIMEZONE = ZoneInfo("Europe/Moscow")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data.json")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


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
    raw.setdefault("channels", {})
    raw.setdefault("pending_deletions", [])
    raw.setdefault("history", [])
    raw.setdefault("schedules", {})
    return raw


def save_state() -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


state = load_state()
print(f"📄 Файл данных: {DATA_FILE}")
print(f"📌 Загружено каналов для заявок: {len(state['channels'])}, "
      f"отложенных удалений: {len(state['pending_deletions'])}, "
      f"в очереди на отчёт: {len(state['history'])}")


DATE_RE = re.compile(
    r"^\s*(\d{2})\.(\d{2})\s*(?:-\s*(\d{2})\.(\d{2}))?\s*$"
)


def parse_date_range(raw: str):
    match = DATE_RE.match(raw)
    if not match:
        return None

    d1_s, m1_s, d2_s, m2_s = match.groups()
    day1, month1 = int(d1_s), int(m1_s)

    today = datetime.now(TIMEZONE).date()
    year = today.year

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


async def resolve_channel(channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def delete_invoking_message(ctx: commands.Context) -> None:
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        print("⚠️ Не удалось удалить сообщение с командой: боту не хватает права «Управление сообщениями».")
    except discord.HTTPException:
        pass


def exact_command_only(ctx: commands.Context) -> bool:
    return ctx.message.content.strip() == f"{ctx.prefix}{ctx.invoked_with}"


bot.add_check(exact_command_only)


DAY_NAMES = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
DAY_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def build_schedule_embed(days: list) -> discord.Embed:
    embed = discord.Embed(title="🗓️ Расписание недели", color=discord.Color.blurple())
    for name, is_green in zip(DAY_NAMES, days):
        embed.add_field(name=name, value="🟢" if is_green else "🔴", inline=True)
    return embed


def build_schedule_view(message_id: int, days: list) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for index, (short, is_green) in enumerate(zip(DAY_SHORT, days)):
        style = discord.ButtonStyle.success if is_green else discord.ButtonStyle.danger
        view.add_item(
            discord.ui.Button(
                label=short,
                style=style,
                custom_id=f"schedule:{message_id}:{index}",
            )
        )
    return view


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
                "Администратор должен выполнить команду `!канал-неявка` в нужном канале.",
                ephemeral=True,
            )
            return

        channel = await resolve_channel(channel_id)
        if channel is None:
            await interaction.response.send_message(
                "⚠️ Не удалось найти сохранённый канал. Настройте его заново через `!канал-неявка`.",
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
        embed.add_field(name="Оставил(а) заявку", value=interaction.user.mention, inline=False)
        embed.set_footer(text="Сообщение удалится автоматически по окончании периода неявки")

        try:
            sent_message = await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
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
                "guild_id": interaction.guild_id,
                "channel_id": channel.id,
                "message_id": sent_message.id,
                "delete_at": delete_at.isoformat(),
                "nickname": self.nickname.value,
                "dates_display": dates_display,
                "reason": self.reason.value,
                "author_id": interaction.user.id,
            }
        )
        save_state()

        await interaction.response.send_message("✅ Заявка на неявку отправлена!", ephemeral=True)


class NeyavkaView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Неявка",
        style=discord.ButtonStyle.danger,
        custom_id="stalzone_neyavka_button",
    )
    async def neyavka_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NeyavkaModal())


async def perform_cleanup() -> None:
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

        state["history"].append(
            {
                "guild_id": entry["guild_id"],
                "nickname": entry["nickname"],
                "dates_display": entry["dates_display"],
                "reason": entry["reason"],
                "author_id": entry["author_id"],
            }
        )

    if changed:
        state["pending_deletions"] = still_pending
        save_state()


@tasks.loop(seconds=30)
async def cleanup_loop():
    await perform_cleanup()


@cleanup_loop.before_loop
async def before_cleanup():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    bot.add_view(NeyavkaView())

    await perform_cleanup()

    if not cleanup_loop.is_running():
        cleanup_loop.start()

    print(f"✅ Бот запущен: {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return

    custom_id = (interaction.data or {}).get("custom_id", "")
    if not custom_id.startswith("schedule:"):
        return

    _, message_id_s, day_index_s = custom_id.split(":")
    schedule = state["schedules"].get(message_id_s)
    if schedule is None:
        await interaction.response.send_message("⚠️ Это расписание больше не найдено.", ephemeral=True)
        return

    day_index = int(day_index_s)
    schedule["days"][day_index] = not schedule["days"][day_index]
    save_state()

    embed = build_schedule_embed(schedule["days"])
    view = build_schedule_view(int(message_id_s), schedule["days"])
    await interaction.response.edit_message(embed=embed, view=view)


@bot.command(name="кнопка-неявка")
@commands.has_permissions(manage_guild=True)
async def knopka_cmd(ctx: commands.Context):
    embed = discord.Embed(
        title="📋 Отметка о неявке — STALZONE",
        description=(
            "Если вы не сможете присутствовать, нажмите кнопку **«Неявка»** ниже "
            "и заполните форму: ник, даты отсутствия и причину."
        ),
        color=discord.Color.red(),
    )
    await ctx.send(embed=embed, view=NeyavkaView())
    await delete_invoking_message(ctx)


@bot.command(name="канал-неявка")
@commands.has_permissions(manage_guild=True)
async def kanal_cmd(ctx: commands.Context):
    state["channels"][str(ctx.guild.id)] = ctx.channel.id
    save_state()
    await ctx.send(f"✅ Заявки о неявке теперь будут приходить в канал {ctx.channel.mention}.")
    await delete_invoking_message(ctx)


@bot.command(name="отчёт-неявка")
@commands.has_permissions(manage_guild=True)
async def report_cmd(ctx: commands.Context):
    guild_id = ctx.guild.id
    entries = [e for e in state["history"] if e["guild_id"] == guild_id]

    if not entries:
        await ctx.send("ℹ️ Нет новых завершившихся неявок для отчёта.")
        return

    CHUNK_SIZE = 25
    chunks = [entries[i:i + CHUNK_SIZE] for i in range(0, len(entries), CHUNK_SIZE)]

    for idx, chunk in enumerate(chunks, start=1):
        title = "📊 Отчёт по завершившимся неявкам"
        if len(chunks) > 1:
            title += f" ({idx}/{len(chunks)})"

        embed = discord.Embed(
            title=title,
            color=discord.Color.orange(),
            timestamp=datetime.now(TIMEZONE),
        )
        for entry in chunk:
            embed.add_field(
                name=f"{entry['nickname']} ({entry['dates_display']})",
                value=f"<@{entry['author_id']}> — {entry['reason']}",
                inline=False,
            )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    state["history"] = [e for e in state["history"] if e["guild_id"] != guild_id]
    save_state()


@bot.command(name="расписание")
@commands.has_permissions(manage_guild=True)
async def schedule_cmd(ctx: commands.Context):
    days = [True] * 7
    embed = build_schedule_embed(days)
    sent = await ctx.send(embed=embed)
    view = build_schedule_view(sent.id, days)
    await sent.edit(view=view)
    view.stop()

    state["schedules"][str(sent.id)] = {
        "guild_id": ctx.guild.id,
        "channel_id": ctx.channel.id,
        "days": days,
    }
    save_state()

    await delete_invoking_message(ctx)


@bot.command(name="помощь")
async def help_cmd(ctx: commands.Context):
    embed = discord.Embed(title="📖 Список команд", color=discord.Color.blurple())
    embed.add_field(
        name="!кнопка-неявка",
        value="Публикует кнопку «Неявка» — по ней открывается форма подачи заявки.",
        inline=False,
    )
    embed.add_field(
        name="!канал-неявка",
        value="Делает текущий канал каналом приёма заявок о неявке.",
        inline=False,
    )
    embed.add_field(
        name="!отчёт-неявка",
        value="Показывает список всех, кто был в неявке с момента прошлого отчёта.",
        inline=False,
    )
    embed.add_field(
        name="!расписание",
        value="Публикует расписание на неделю с кнопками-переключателями по дням.",
        inline=False,
    )
    embed.add_field(
        name="!помощь",
        value="Показывает этот список команд.",
        inline=False,
    )
    await ctx.send(embed=embed)


@knopka_cmd.error
@kanal_cmd.error
@report_cmd.error
@schedule_cmd.error
@help_cmd.error
async def setup_commands_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("⛔ У вас нет прав для использования этой команды (нужны права «Управление сервером»).")
    elif isinstance(error, commands.CheckFailure):
        pass
    else:
        raise error


if __name__ == "__main__":
    if TOKEN == "ВАШ_ТОКЕН_СЮДА":
        raise SystemExit(
            "Укажите токен бота: переменная окружения DISCORD_TOKEN, "
            "либо впишите его напрямую в переменную TOKEN в этом файле."
        )
    bot.run(TOKEN)
