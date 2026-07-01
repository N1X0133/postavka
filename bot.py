import os
import asyncio
import traceback
import datetime
import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

# ------------------------------------------------------------
# ID сервера 1 (основной)
# ------------------------------------------------------------
GUILD_ID = 764090907657240586
ORDER_CHANNEL_ID = 1178807307921002578
ORDERER_ROLE_ID = 1178807389420527646
ARMY_ROLE_ID = 764091598983921674

# ------------------------------------------------------------
# PostgreSQL (НЕ ТРОГАТЬ)
# ------------------------------------------------------------
DB_DSN = (
    "postgresql://bothost_db_eb47576e4dad:"
    "VWNyYcmbXI4C7KW-YKqzgvjrwxrMH7RIqWOO3UQEb_4"
    "@node1.pghost.ru:15722/bothost_db_eb47576e4dad"
)


async def create_pool():
    print("[DB] Подключение к PostgreSQL...")
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
            person_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            message_id BIGINT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """)

    print("[DB] Готово")
    return pool


# ------------------------------------------------------------
# Bot
# ------------------------------------------------------------
class DeliveryBot(commands.Bot):
    def __init__(self, pool):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.pool = pool

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print("[BOT] Slash-команды синхронизированы")

    async def on_ready(self):
        print(f"[BOT] Запущен как {self.user}")

    async def on_application_command_error(self, interaction, error):
        traceback.print_exception(type(error), error, error.__traceback__)
        try:
            msg = f"❌ Ошибка: {error}"
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


# ------------------------------------------------------------
# Slash command /заказ
# ------------------------------------------------------------
bot = None


@app_commands.command(name="заказ", description="Создать заказ поставки")
@app_commands.describe(
    фракция="Фракция",
    время="ЧЧ:ММ",
    количество="Количество человек"
)
@app_commands.choices(фракция=[
    app_commands.Choice(name="ФСБ", value="ФСБ"),
    app_commands.Choice(name="Полиция", value="Полиция")
])
async def order(interaction: discord.Interaction,
                фракция: app_commands.Choice[str],
                время: str,
                количество: int):

    await interaction.response.defer(ephemeral=True)

    if interaction.channel_id != ORDER_CHANNEL_ID:
        ch = interaction.guild.get_channel(ORDER_CHANNEL_ID)
        await interaction.followup.send(
            f"❌ Используйте канал {ch.mention if ch else 'назначенный'}",
            ephemeral=True
        )
        return

    member = interaction.user
    if not isinstance(member, discord.Member):
        return

    role = interaction.guild.get_role(ORDERER_ROLE_ID)
    if role not in member.roles:
        await interaction.followup.send("❌ Нет роли заказчика", ephemeral=True)
        return

    if количество <= 0:
        await interaction.followup.send("❌ Количество должно быть > 0", ephemeral=True)
        return

    try:
        datetime.datetime.strptime(время, "%H:%M")
    except ValueError:
        await interaction.followup.send("❌ Формат времени ЧЧ:ММ", ephemeral=True)
        return

    async with bot.pool.acquire() as conn:
        order_id = await conn.fetchval("""
            INSERT INTO orders (
                guild_id, channel_id, author_id,
                author_name, faction, delivery_time, person_count
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            RETURNING id
        """, interaction.guild_id, interaction.channel_id,
             member.id, str(member), фракция.value, время, количество)

    embed = discord.Embed(
        title="Новый заказ",
        color=discord.Color.blue()
    )
    embed.add_field(name="Фракция", value=фракция.value)
    embed.add_field(name="Время", value=время)
    embed.add_field(name="Количество", value=str(количество))
    embed.add_field(name="Заказчик", value=member.mention)

    view = OrderApproveView(order_id)

    channel = interaction.guild.get_channel(ORDER_CHANNEL_ID)
    msg = await channel.send(embed=embed, view=view)

    async with bot.pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET message_id=$1 WHERE id=$2",
            msg.id, order_id
        )

    await interaction.followup.send(f"✅ Заказ #{order_id} создан", ephemeral=True)


# ------------------------------------------------------------
# Кнопки принятия
# ------------------------------------------------------------
class OrderApproveView(discord.ui.View):
    def __init__(self, order_id):
        super().__init__(timeout=None)
        self.order_id = order_id

    @discord.ui.button(label="Принять", style=discord.ButtonStyle.green)
    async def approve(self, interaction, button):
        await self._handle(interaction, "approved", "✅ Принято")

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.red)
    async def deny(self, interaction, button):
        await self._handle(interaction, "denied", "❌ Отклонено")

    async def _handle(self, interaction, status, title):
        role = interaction.guild.get_role(ARMY_ROLE_ID)
        if role not in interaction.user.roles:
            await interaction.response.send_message("Нет доступа", ephemeral=True)
            return

        async with bot.pool.acquire() as conn:
            await conn.execute(
                "UPDATE orders SET status=$1 WHERE id=$2",
                status, self.order_id
            )

        embed = interaction.message.embeds[0]
        embed.title = title
        embed.color = discord.Color.green() if status == "approved" else discord.Color.red()
        embed.add_field(name="Решение", value=interaction.user.mention, inline=False)

        for c in self.children:
            c.disabled = True

        await interaction.response.edit_message(embed=embed, view=self)


# ------------------------------------------------------------
# main
# ------------------------------------------------------------
async def main():
    global bot
    pool = await create_pool()
    bot = DeliveryBot(pool)

    bot.tree.add_command(order)

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN не задан")

    await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
