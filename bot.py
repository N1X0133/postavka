import os
import sys
import asyncio
import traceback
import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

# ------------------------------------------------------------
# ID сервера, каналов и ролей
# ------------------------------------------------------------
GUILD_ID = 764090907657240586
ORDER_CHANNEL_ID = 1178807307921002578
ORDERER_ROLE_ID = 1178807389420527646
ARMY_ROLE_ID = 764091598983921674

# ------------------------------------------------------------
# Подключение к PostgreSQL
# ------------------------------------------------------------
DB_DSN = (
    "postgresql://bothost_db_eb47576e4dad:"
    "VWNyYcmbXI4C7KW-YKqzgvjrwxrMH7RIqWOO3UQEb_4"
    "@node1.pghost.ru:15722/bothost_db_eb47576e4dad"
)

async def create_pool():
    print("[DB] Пытаюсь подключиться к PostgreSQL...")
    try:
        pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=5)
        print("[DB] Пул соединений создан успешно.")
    except Exception as e:
        print(f"[DB] Ошибка подключения к PostgreSQL: {e}")
        raise
    async with pool.acquire() as conn:
        print("[DB] Создаю/проверяю таблицу orders...")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                author_id BIGINT NOT NULL,
                author_name TEXT NOT NULL,
                faction TEXT NOT NULL,
                delivery_time TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                message_id BIGINT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        print("[DB] Таблица готова.")
    return pool

# ------------------------------------------------------------
# Класс бота
# ------------------------------------------------------------
class DeliveryBot(commands.Bot):
    def __init__(self, pool, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pool = pool

    async def setup_hook(self):
        print("[BOT] Начинаю синхронизацию команд...")
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print(f"[BOT] Команды синхронизированы с сервером {GUILD_ID}")

    async def on_ready(self):
        print(f"[BOT] Бот {self.user} запущен и готов к работе.")

    async def on_application_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        print(f"[ОШИБКА] Команда /{interaction.command.name} от {interaction.user}:")
        traceback.print_exception(type(error), error, error.__traceback__)

        if interaction.response.is_done():
            await interaction.followup.send(
                f"❌ Произошла внутренняя ошибка: {error}", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"❌ Произошла внутренняя ошибка: {error}", ephemeral=True
            )

intents = discord.Intents.default()
intents.message_content = True
bot = None

# ------------------------------------------------------------
# Слэш-команда /заказ
# ------------------------------------------------------------
@discord.app_commands.command(
    name="заказ",
    description="Заказать поставку для своей фракции"
)
@app_commands.describe(
    фракция="Выберите фракцию",
    время="Время поставки (например, 15:00)"
)
@app_commands.choices(фракция=[
    app_commands.Choice(name="ФСБ", value="ФСБ"),
    app_commands.Choice(name="Полиция", value="Полиция")
])
async def order(interaction: discord.Interaction, фракция: app_commands.Choice[str], время: str):
    print(f"[CMD] /заказ вызван пользователем {interaction.user} в канале {interaction.channel_id}")
    await interaction.response.defer(ephemeral=True)
    print("[CMD] defer выполнен.")

    # Проверка канала
    if interaction.channel_id != ORDER_CHANNEL_ID:
        print(f"[CMD] Неверный канал: {interaction.channel_id} != {ORDER_CHANNEL_ID}")
        order_channel = interaction.guild.get_channel(ORDER_CHANNEL_ID)
        channel_mention = order_channel.mention if order_channel else "указанный канал"
        await interaction.followup.send(
            f"❌ Эта команда доступна только в канале {channel_mention}.",
            ephemeral=True
        )
        return
    print("[CMD] Канал правильный.")

    # Проверка роли
    member = interaction.user
    if not isinstance(member, discord.Member):
        print("[CMD] Не удалось получить member.")
        await interaction.followup.send("Ошибка: не удалось определить участника.", ephemeral=True)
        return

    orderer_role = interaction.guild.get_role(ORDERER_ROLE_ID)
    if orderer_role is None or orderer_role not in member.roles:
        print(f"[CMD] У пользователя {member} нет роли заказчика.")
        await interaction.followup.send(
            "❌ У вас нет роли заказчика. Обратитесь к командованию.",
            ephemeral=True
        )
        return
    print("[CMD] Роль заказчика подтверждена.")

    # Запись в БД
    try:
        print("[DB] Вставляю заказ в базу...")
        async with bot.pool.acquire() as conn:
            order_id = await conn.fetchval(
                "INSERT INTO orders (guild_id, channel_id, author_id, author_name, faction, delivery_time) "
                "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
                interaction.guild_id,
                interaction.channel_id,
                member.id,
                str(member),
                фракция.value,
                время
            )
        print(f"[DB] Заказ создан, ID={order_id}")
    except Exception as e:
        print(f"[DB] Ошибка вставки: {e}")
        await interaction.followup.send(f"❌ Ошибка базы данных: {e}", ephemeral=True)
        return

    # Embed и кнопки
    embed = discord.Embed(
        title="🛒 Новый заказ поставки",
        color=discord.Color.blue()
    )
    embed.add_field(name="Фракция", value=фракция.value, inline=True)
    embed.add_field(name="Время поставки", value=время, inline=True)
    embed.add_field(name="Заказчик", value=member.mention, inline=True)
    embed.set_footer(text=f"ID заказа: {order_id}")

    view = OrderApproveView(order_id)

    order_channel = interaction.guild.get_channel(ORDER_CHANNEL_ID)
    if order_channel is None:
        print("[CMD] order_channel is None!")
        await interaction.followup.send(
            "❌ Канал не найден. Обратитесь к администратору.",
            ephemeral=True
        )
        return

    try:
        print(f"[CMD] Отправляю сообщение в канал {order_channel}...")
        message = await order_channel.send(embed=embed, view=view)
        print(f"[CMD] Сообщение отправлено (ID={message.id})")
    except discord.Forbidden:
        print("[CMD] Нет прав на отправку сообщения!")
        await interaction.followup.send("❌ У бота нет прав отправлять сообщения в этот канал.", ephemeral=True)
        return
    except Exception as e:
        print(f"[CMD] Ошибка отправки сообщения: {e}")
        await interaction.followup.send(f"❌ Ошибка отправки сообщения: {e}", ephemeral=True)
        return

    async with bot.pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET message_id = $1 WHERE id = $2",
            message.id, order_id
        )
    print("[CMD] message_id сохранён.")

    await interaction.followup.send(
        f"✅ Заказ №{order_id} создан и отправлен на одобрение.",
        ephemeral=True
    )
    print("[CMD] Ответ пользователю отправлен.")

# ------------------------------------------------------------
# Кнопки (без изменений)
# ------------------------------------------------------------
class OrderApproveView(discord.ui.View):
    def __init__(self, order_id):
        super().__init__(timeout=None)
        self.order_id = order_id

    @discord.ui.button(label="Принять", style=discord.ButtonStyle.green, custom_id="approve_order")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_decision(interaction, "approved", discord.Color.green(), "✅ Одобрено Армией")

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.red, custom_id="deny_order")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_decision(interaction, "denied", discord.Color.red(), "❌ Отклонено Армией")

    async def process_decision(self, interaction: discord.Interaction, new_status: str, color: discord.Color, title: str):
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Ошибка.", ephemeral=True)
            return
        army_role = interaction.guild.get_role(ARMY_ROLE_ID)
        if army_role is None or army_role not in interaction.user.roles:
            await interaction.response.send_message("Только Армия может принимать решение.", ephemeral=True)
            return

        async with bot.pool.acquire() as conn:
            await conn.execute(
                "UPDATE orders SET status = $1 WHERE id = $2",
                new_status, self.order_id
            )

        embed = interaction.message.embeds[0]
        embed.title = title
        embed.color = color
        embed.set_footer(text=embed.footer.text + f" | Статус: {new_status}")
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)

# ------------------------------------------------------------
# Запуск
# ------------------------------------------------------------
async def main():
    global bot
    print("[MAIN] Запуск бота...")
    pool = await create_pool()
    bot = DeliveryBot(pool, command_prefix="!", intents=intents)
    bot.tree.add_command(order)

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("[MAIN] ОШИБКА: DISCORD_TOKEN не задан!")
        raise ValueError("Переменная окружения DISCORD_TOKEN не задана")
    print(f"[MAIN] Токен загружен, первый символ: {token[0]}... длина {len(token)}")
    await bot.start(token)

if __name__ == "__main__":
    asyncio.run(main())
