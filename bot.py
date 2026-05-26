import os
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
ORDER_CHANNEL_ID = 767449572015341671          # Единый канал для заказа и одобрения
ORDERER_ROLE_ID = 1178807389420527646          # Роль заказчика
ARMY_ROLE_ID = 764091598983921674              # Роль Армии (одобрение)

# ------------------------------------------------------------
# Подключение к PostgreSQL (вшито)
# ------------------------------------------------------------
DB_DSN = (
    "postgresql://bothost_db_eb47576e4dad:"
    "VWNyYcmbXI4C7KW-YKqzgvjrwxrMH7RIqWOO3UQEb_4"
    "@node1.pghost.ru:15722/bothost_db_eb47576e4dad"
)

async def create_pool():
    pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=5)
    async with pool.acquire() as conn:
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
    return pool

# ------------------------------------------------------------
# Класс бота с обработчиком ошибок
# ------------------------------------------------------------
class DeliveryBot(commands.Bot):
    def __init__(self, pool, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pool = pool

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print(f"Слэш-команды синхронизированы с сервером {GUILD_ID}")

    # Глобальный обработчик ошибок для app_commands
    async def on_application_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        # Логируем полный трейсбек в консоль
        print(f"[ОШИБКА] Команда /{interaction.command.name} от {interaction.user}:")
        traceback.print_exception(type(error), error, error.__traceback__)

        # Отправляем пользователю сообщение об ошибке (ephemeral, если возможно)
        if interaction.response.is_done():
            # Уже был ответ defer или send, используем followup
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
    # Сразу откладываем ответ, чтобы Discord не ругался
    await interaction.response.defer(ephemeral=True)

    # Проверка канала
    if interaction.channel_id != ORDER_CHANNEL_ID:
        await interaction.followup.send(
            f"❌ Эта команда доступна только в канале <#{ORDER_CHANNEL_ID}>.",
            ephemeral=True
        )
        return

    # Проверка роли заказчика
    member = interaction.user
    if not isinstance(member, discord.Member):
        await interaction.followup.send("Ошибка: не удалось определить участника.", ephemeral=True)
        return

    orderer_role = interaction.guild.get_role(ORDERER_ROLE_ID)
    if orderer_role is None or orderer_role not in member.roles:
        await interaction.followup.send(
            "❌ У вас нет роли заказчика. Обратитесь к командованию.",
            ephemeral=True
        )
        return

    # Запись в БД (без try/except — теперь все ошибки уйдут в общий обработчик)
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

    # Создание Embed
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
        await interaction.followup.send(
            "❌ Канал не найден. Обратитесь к администратору.",
            ephemeral=True
        )
        return

    message = await order_channel.send(embed=embed, view=view)

    # Сохраняем message_id
    async with bot.pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET message_id = $1 WHERE id = $2",
            message.id, order_id
        )

    await interaction.followup.send(
        f"✅ Заказ №{order_id} создан и отправлен на одобрение.",
        ephemeral=True
    )

# ------------------------------------------------------------
# Кнопки «Принять» / «Отклонить» (только Армия)
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
    pool = await create_pool()
    bot = DeliveryBot(pool, command_prefix="!", intents=intents)
    bot.tree.add_command(order)

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("Переменная окружения DISCORD_TOKEN не задана")
    await bot.start(token)

if __name__ == "__main__":
    asyncio.run(main())
