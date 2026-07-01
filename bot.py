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
    print("[DB] Connecting...")

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

    print("[DB] Ready")
    return pool


# ------------------------------------------------------------
# BOT CLASS
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
        print("[BOT] Slash commands synced")

    async def on_ready(self):
        print(f"[BOT] Logged in as {self.user}")

    async def on_application_command_error(self, interaction, error):
        traceback.print_exception(type(error), error, error.__traceback__)
        try:
            msg = f"❌ Error: {error}"
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except:
            pass


bot = None


# ------------------------------------------------------------
# /заказ COMMAND
# ------------------------------------------------------------
@app_commands.command(name="заказ", description="Создать заказ")
@app_commands.describe(
    фракция="Фракция",
    время="Время (ЧЧ:ММ)",
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
        await interaction.followup.send("❌ Нет доступа", ephemeral=True)
        return

    if количество <= 0:
        await interaction.followup.send("❌ Некорректное количество", ephemeral=True)
        return

    try:
        datetime.datetime.strptime(время, "%H:%M")
    except ValueError:
        await interaction.followup.send("❌ Время должно быть ЧЧ:ММ", ephemeral=True)
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

    # ------------------------------------------------------------
    # TERMINAL STYLE EMBED + ROLES
    # ------------------------------------------------------------
    embed = discord.Embed(
        title="> SYSTEM :: NEW ORDER RECEIVED",
        color=discord.Color.dark_grey(),
        timestamp=datetime.datetime.utcnow()
    )

    embed.description = (
        "```"
        f"[INIT] ORDER_ID      :: #{order_id}\n"
        f"[INIT] FACTION       :: {фракция.value}\n"
        f"[INIT] DELIVERY     :: {время}\n"
        f"[INIT] PERSON_COUNT  :: {количество}\n"
        f"[INIT] USER         :: {member.name} ({member.id})\n"
        "```\n"
        "```diff\n"
        "- STATUS: PENDING APPROVAL\n"
        "+ ROUTING: DISPATCH QUEUE ACTIVE\n"
        "```"
    )

    embed.add_field(
        name="👮 ORDERER ROLE",
        value=f"```{interaction.guild.get_role(ORDERER_ROLE_ID).name}```",
        inline=True
    )

    embed.add_field(
        name="🪖 ARMY ROLE",
        value=f"```{interaction.guild.get_role(ARMY_ROLE_ID).name}```",
        inline=True
    )

    embed.add_field(
        name="📌 FACTION",
        value=f"```{фракция.value}```",
        inline=True
    )

    embed.add_field(
        name="👤 DISCORD ROLES",
        value=", ".join([r.name for r in member.roles if r != interaction.guild.default_role]),
        inline=False
    )

    embed.set_footer(
        text="secure-node://dispatch-system • verified session",
        icon_url=interaction.guild.icon.url if interaction.guild.icon else None
    )

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
# BUTTONS
# ------------------------------------------------------------
class OrderApproveView(discord.ui.View):
    def __init__(self, order_id):
        super().__init__(timeout=None)
        self.order_id = order_id

    @discord.ui.button(label="Принять", style=discord.ButtonStyle.green)
    async def approve(self, interaction, button):
        await self._handle(interaction, "approved", "APPROVED")

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.red)
    async def deny(self, interaction, button):
        await self._handle(interaction, "denied", "REJECTED")

    async def _handle(self, interaction, status, title):
        role = interaction.guild.get_role(ARMY_ROLE_ID)
        if role not in interaction.user.roles:
            await interaction.response.send_message("❌ Нет доступа", ephemeral=True)
            return

        async with bot.pool.acquire() as conn:
            await conn.execute(
                "UPDATE orders SET status=$1 WHERE id=$2",
                status, self.order_id
            )

        embed = interaction.message.embeds[0]
        embed.title = f"> SYSTEM :: ORDER {title}"
        embed.color = discord.Color.green() if status == "approved" else discord.Color.red()

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
    if not token:
        raise ValueError("DISCORD_TOKEN not set")

    await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
