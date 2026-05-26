import os
import asyncio
import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

# ------------------------------------------------------------
# Константы (идентификаторы твоего сервера, каналов и ролей)
# ------------------------------------------------------------
GUILD_ID = 764090907657240586               # ID сервера, на котором работает бот
ORDER_COMMAND_CHANNEL_ID = 1178807307921002578  # Канал, где можно писать /заказ
ORDER_APPROVAL_CHANNEL_ID = 767449572015341671  # Канал, куда уходят заявки на одобрение
ARMY_ROLE_ID = 1508951142292521143          # Роль @Армия
POLICE_ROLE_ID = 1508951206683213885        # Роль @Полиция (МВД)
FSB_ROLE_ID = 1508951249444274307           # Роль @ФСБ

# ------------------------------------------------------------
# Подключение к PostgreSQL (строка вшита напрямую)
# ------------------------------------------------------------
DB_DSN = (
    "postgresql://bothost_db_eb47576e4dad:"
    "VWNyYcmbXI4C7KW-YKqzgvjrwxrMH7RIqWOO3UQEb_4"
    "@node1.pghost.ru:15722/bothost_db_eb47576e4dad"
    "?sslmode=require"
)

async def create_pool():
    """Создаёт пул соединений и инициализирует таблицу заказов."""
    pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                author_id BIGINT NOT NULL,
                faction TEXT NOT NULL,
                delivery_time TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                message_id BIGINT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    return pool

# ------------------------------------------------------------
# Класс бота
# ------------------------------------------------------------
class DeliveryBot(commands.Bot):
    def __init__(self, pool, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pool = pool

    async def setup_hook(self):
        """Синхронизация слэш-команд с сервером."""
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print(f"Слэш-команды синхронизированы с сервером {GUILD_ID}")

# ------------------------------------------------------------
# Инициализация
# ------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True  # на будущее
bot = None  # будет создан в main

# ------------------------------------------------------------
# Слэш-команда /заказ
# ------------------------------------------------------------
@discord.app_commands.command(
    name="заказ",
    description="Заказать поставку для своей фракции"
)
@app_commands.describe(
    фракция="Выберите вашу фракцию",
    время="Время поставки (например, 15:00)"
)
@app_commands.choices(фракция=[
    app_commands.Choice(name="ФСБ", value="ФСБ"),
    app_commands.Choice(name="Полиция", value="Полиция")
])
async def order(interaction: discord.Interaction, фракция: app_commands.Choice[str], время: str):
    # 1) Проверяем, что команда выполняется в правильном канале
    if interaction.channel_id != ORDER_COMMAND_CHANNEL_ID:
        await interaction.response.send_message(
            f"❌ Эта команда доступна только в канале <#{ORDER_COMMAND_CHANNEL_ID}>.",
            ephemeral=True
        )
        return

    # 2) Проверяем, что у участника есть нужная роль
    member = interaction.user
    if not isinstance(member, discord.Member):
        await interaction.response.send_message("Ошибка: не удалось определить участника.", ephemeral=True)
        return

    required_role_id = FSB_ROLE_ID if фракция.value == "ФСБ" else POLICE_ROLE_ID
    required_role = interaction.guild.get_role(required_role_id)
    if required_role is None or required_role not in member.roles:
        await interaction.response.send_message(
            f"❌ У вас нет роли {required_role.name if required_role else 'необходимой фракции'}.",
            ephemeral=True
        )
        return

    # 3) Записываем заказ в базу данных
    async with bot.pool.acquire() as conn:
        order_id = await conn.fetchval(
            "INSERT INTO orders (guild_id, channel_id, author_id, faction, delivery_time) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id",
            interaction.guild_id,
            interaction.channel_id,
            member.id,
            фракция.value,
            время
        )

    # 4) Создаём Embed для канала одобрения
    embed = discord.Embed(
        title="🛒 Новый заказ поставки",
        color=discord.Color.blue()
    )
    embed.add_field(name="Фракция", value=фракция.value, inline=True)
    embed.add_field(name="Время поставки", value=время, inline=True)
    embed.add_field(name="Заказчик", value=member.mention, inline=True)
    embed.set_footer(text=f"ID заказа: {order_id}")

    # 5) Прикрепляем кнопки
    view = OrderApproveView(order_id)

    order_channel = interaction.guild.get_channel(ORDER_APPROVAL_CHANNEL_ID)
    if order_channel is None:
        await interaction.response.send_message(
            "❌ Канал для одобрения не найден. Обратитесь к администратору.",
            ephemeral=True
        )
        return

    message = await order_channel.send(embed=embed, view=view)

    # 6) Сохраняем ID сообщения в БД
    async with bot.pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET message_id = $1 WHERE id = $2",
            message.id, order_id
        )

    await interaction.response.send_message(
        f"✅ Заказ №{order_id} создан и отправлен на одобрение в {order_channel.mention}.",
        ephemeral=True
    )

# ------------------------------------------------------------
# Кнопки «Принять» / «Отклонить» (доступны только Армии)
# ------------------------------------------------------------
class OrderApproveView(discord.ui.View):
    def __init__(self, order_id):
        super().__init__(timeout=None)  # Кнопки не исчезнут по тайм-ауту
        self.order_id = order_id

    @discord.ui.button(label="Принять", style=discord.ButtonStyle.green, custom_id="approve_order")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_decision(interaction, "approved", discord.Color.green(), "✅ Одобрено Армией")

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.red, custom_id="deny_order")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_decision(interaction, "denied", discord.Color.red(), "❌ Отклонено Армией")

    async def process_decision(self, interaction: discord.Interaction, new_status: str, color: discord.Color, title: str):
        # Только Армия может принимать решение
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Ошибка.", ephemeral=True)
            return
        army_role = interaction.guild.get_role(ARMY_ROLE_ID)
        if army_role is None or army_role not in interaction.user.roles:
            await interaction.response.send_message("Только Армия может принимать решение.", ephemeral=True)
            return

        # Обновляем статус в БД
        async with bot.pool.acquire() as conn:
            await conn.execute(
                "UPDATE orders SET status = $1 WHERE id = $2",
                new_status, self.order_id
            )

        # Редактируем исходное сообщение
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
    pool = await create_pool()
    bot = DeliveryBot(pool, command_prefix="!", intents=intents)
    bot.tree.add_command(order)

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("Переменная окружения DISCORD_TOKEN не задана")
    await bot.start(token)

if __name__ == "__main__":
    asyncio.run(main())
