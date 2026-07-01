import os
import asyncio
import traceback
import datetime
import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------
GUILD_ID = 764090907657240586
ORDER_CHANNEL_ID = 1178807307921002578
ORDERER_ROLE_ID = 1178807389420527646
ARMY_ROLE_ID = 764091598983921674

# ------------------------------------------------------------
# DATABASE (НЕ ТРОГАТЬ)
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
            guild_id BIGINT,
            channel_id BIGINT,
            author_id BIGINT,
            author_name TEXT,
            faction TEXT,
            delivery_time TEXT,
            person_count INTEGER,
            status TEXT DEFAULT 'pending',
            message_id BIGINT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """)

    return pool


# ------------------------------------------------------------
# BOT
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

    async def on_ready(self):
        print(f"[BOT] Logged in as {self.user}")


bot = None


# ------------------------------------------------------------
# /заказ
# ------------------------------------------------------------
@app_commands.command(name="заказ", description="Создать заказ поставки")
@app_commands.describe(
    фракция="Фракция",
    время="ЧЧ:ММ",
    количество="Количество"
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
        return await interaction.followup.send(
            f"❌ Используйте канал {ch.mention if ch else 'назначенный'}",
            ephemeral=True
        )

    member = interaction.user

    role = interaction.guild.get_role(ORDERER_ROLE_ID)
    if role not in member.roles:
        return await interaction.followup.send("❌ Нет доступа", ephemeral=True)

    if количество <= 0:
        return await interaction.followup.send("❌ Количество неверное", ephemeral=True)

    try:
        datetime.datetime.strptime(время, "%H:%M")
    except ValueError:
        return await interaction.followup.send("❌ Время ЧЧ:ММ", ephemeral=True)

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

    # ------------------------------------------------------------
    # EMBED
    # ------------------------------------------------------------
    orderer_role = interaction.guild.get_role(ORDERER_ROLE_ID)
    army_role = interaction.guild.get_role(ARMY_ROLE_ID)

    embed = discord.Embed(
        title="📦 Новый заказ поставки",
        color=discord.Color.from_rgb(52, 152, 219),
        timestamp=datetime.datetime.utcnow()
    )

    embed.add_field(name="🏷️ Фракция", value=f"```{фракция.value}```", inline=True)
    embed.add_field(name="⏰ Время", value=f"```{время}```", inline=True)
    embed.add_field(name="👥 Количество", value=f"```{количество}```", inline=True)

    embed.add_field(name="👤 Заказчик", value=member.mention, inline=False)

    embed.add_field(
        name="👮 Роль заказчика",
        value=orderer_role.mention if orderer_role else "N/A",
        inline=True
    )

    embed.add_field(
        name="🪖 Армия (контроль)",
        value=army_role.mention if army_role else "N/A",
        inline=True
    )

    # статус (фиксированный индекс)
    embed.add_field(
        name="📌 Статус",
        value="🟡 Ожидает обработки",
        inline=False
    )

    embed.set_footer(text=f"Order #{order_id}")

    view = OrderApproveView(order_id)

    channel = interaction.guild.get_channel(ORDER_CHANNEL_ID)

    # 🪖 ПИНГ АРМИИ
    msg = await channel.send(
        content=army_role.mention if army_role else None,
        embed=embed,
        view=view
    )

    async with bot.pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET message_id=$1 WHERE id=$2",
            msg.id, order_id
        )

    await interaction.followup.send(f"✅ Заказ #{order_id} создан", ephemeral=True)


# ------------------------------------------------------------
# BUTTONS
# ------------------------------------------------------------
class OrderApproveView(discord.ui.View):
    def __init__(self, order_id):
        super().__init__(timeout=None)
        self.order_id = order_id

    @discord.ui.button(label="Принять", style=discord.ButtonStyle.green)
    async def approve(self, interaction, button):
        await self._handle(interaction, "approved", "Принято")

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.red)
    async def deny(self, interaction, button):
        await self._handle(interaction, "denied", "Отклонено")

    async def _handle(self, interaction, status, title):
        role = interaction.guild.get_role(ARMY_ROLE_ID)

        if role not in interaction.user.roles:
            return await interaction.response.send_message("❌ Нет доступа", ephemeral=True)

        async with bot.pool.acquire() as conn:
            await conn.execute(
                "UPDATE orders SET status=$1 WHERE id=$2",
                status, self.order_id
            )

        embed = interaction.message.embeds[0]

        # обновляем статус (7-й field)
        embed.set_field_at(
            6,
            name="📌 Статус",
            value="🟢 Принято" if status == "approved" else "🔴 Отклонено",
            inline=False
        )

        # 👤 кто обработал
        embed.add_field(
            name="👮 Обработал",
            value=interaction.user.mention,
            inline=False
        )

        embed.title = f"📦 Заказ #{self.order_id} — {title}"

        for c in self.children:
            c.disabled = True

        await interaction.response.edit_message(embed=embed, view=self)


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------
async def main():
    global bot
    pool = await create_pool()
    bot = DeliveryBot(pool)

    bot.tree.add_command(order)

    token = os.getenv("DISCORD_TOKEN")
    await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
