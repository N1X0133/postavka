import os
import asyncio
import traceback
import datetime
import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

# ------------------------------------------------------------
# ID сервера 1 (гос), каналов и ролей
# ------------------------------------------------------------
GUILD_ID = 764090907657240586
ORDER_CHANNEL_ID = 1178807307921002578
ORDERER_ROLE_ID = 1178807389420527646
ARMY_ROLE_ID = 764091598983921674

# ------------------------------------------------------------
# ID сервера 2 (криминал) и его параметры
# ------------------------------------------------------------
CRIMINAL_GUILD_ID = 767449572015341671
CRIMINAL_CHANNEL_ID = 1476295153525194817
CRIMINAL_ROLE_IDS = [767449572015341675, 767449572452335619]

# ------------------------------------------------------------
# Подключение к PostgreSQL (вшито)
# ------------------------------------------------------------
DB_DSN = (
    "postgresql://bothost_db_eb47576e4dad:"
    "VWNyYcmbXI4C7KW-YKqzgvjrwxrMH7RIqWOO3UQEb_4"
    "@node1.pghost.ru:15722/bothost_db_eb47576e4dad"
)

async def create_pool():
    print("[DB] Подключаюсь к PostgreSQL...")
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
                criminal_message_id BIGINT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        for col, coltype in [
            ("person_count", "INTEGER NOT NULL DEFAULT 0"),
            ("author_name", "TEXT NOT NULL DEFAULT ''"),
            ("criminal_message_id", "BIGINT")
        ]:
            exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='orders' AND column_name=$1)",
                col
            )
            if not exists:
                print(f"[DB] Добавляю колонку {col}...")
                await conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {coltype}")
        print("[DB] Таблица orders готова.")
    return pool

# ------------------------------------------------------------
# Класс бота
# ------------------------------------------------------------
class DeliveryBot(commands.Bot):
    def __init__(self, pool, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pool = pool

    async def setup_hook(self):
        print("[BOT] Синхронизация команд на сервере 1...")
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print(f"[BOT] Команды синхронизированы с сервером {GUILD_ID}")

    async def on_ready(self):
        print(f"[BOT] Бот {self.user} запущен и готов.")

    async def on_application_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        print(f"[ОШИБКА] /{interaction.command.name} от {interaction.user}:")
        traceback.print_exception(type(error), error, error.__traceback__)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ Внутренняя ошибка: {error}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ Внутренняя ошибка: {error}", ephemeral=True)
        except Exception:
            pass

intents = discord.Intents.default()
intents.message_content = True
bot = None

# ------------------------------------------------------------
# Слэш-команда /заказ (сервер 1)
# ------------------------------------------------------------
@discord.app_commands.command(
    name="заказ",
    description="Заказать поставку для своей фракции"
)
@app_commands.describe(
    фракция="Выберите фракцию",
    время="Время поставки (формат ЧЧ:ММ, например 15:00)",
    количество="Кол-во человек от вашей фракции"
)
@app_commands.choices(фракция=[
    app_commands.Choice(name="ФСБ", value="ФСБ"),
    app_commands.Choice(name="Полиция", value="Полиция")
])
async def order(
    interaction: discord.Interaction,
    фракция: app_commands.Choice[str],
    время: str,
    количество: int
):
    print(f"[CMD] /заказ вызван {interaction.user} (канал {interaction.channel_id})")
    await interaction.response.defer(ephemeral=True)

    if interaction.channel_id != ORDER_CHANNEL_ID:
        channel = interaction.guild.get_channel(ORDER_CHANNEL_ID)
        mention = channel.mention if channel else "указанный канал"
        await interaction.followup.send(f"❌ Эта команда доступна только в канале {mention}.", ephemeral=True)
        return

    member = interaction.user
    if not isinstance(member, discord.Member):
        await interaction.followup.send("Ошибка: не удалось определить участника.", ephemeral=True)
        return

    orderer_role = interaction.guild.get_role(ORDERER_ROLE_ID)
    if orderer_role is None or orderer_role not in member.roles:
        await interaction.followup.send("❌ У вас нет роли заказчика.", ephemeral=True)
        return

    if количество <= 0:
        await interaction.followup.send("❌ Количество человек должно быть больше нуля.", ephemeral=True)
        return

    # Валидация формата времени
    try:
        parsed_time = datetime.datetime.strptime(время, '%H:%M')
    except ValueError:
        await interaction.followup.send("❌ Неверный формат времени. Используйте ЧЧ:ММ (например, 15:00).", ephemeral=True)
        return

    # Запись в БД
    async with bot.pool.acquire() as conn:
        order_id = await conn.fetchval(
            "INSERT INTO orders (guild_id, channel_id, author_id, author_name, faction, delivery_time, person_count) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
            interaction.guild_id,
            interaction.channel_id,
            member.id,
            str(member),
            фракция.value,
            время,
            количество
        )

    # Создаём Embed с видимым упоминанием роли Армии
    army_mention = f"<@&{ARMY_ROLE_ID}>"
    embed = discord.Embed(
        title="🛒 Новый заказ поставки",
        color=discord.Color.blue()
    )
    embed.add_field(name="Фракция", value=фракция.value, inline=True)
    embed.add_field(name="Время поставки", value=время, inline=True)
    embed.add_field(name="Кол-во человек", value=str(количество), inline=True)
    embed.add_field(name="Заказчик", value=member.mention, inline=False)
    embed.add_field(name="Уведомление", value=army_mention, inline=False)
    embed.set_footer(text=f"ID заказа: {order_id} | by Ilya Vetrov")

    view = OrderApproveView(order_id)

    order_channel = interaction.guild.get_channel(ORDER_CHANNEL_ID)
    if order_channel is None:
        await interaction.followup.send("❌ Канал не найден.", ephemeral=True)
        return

    try:
        # Пинг роли (контент) + embed с упоминанием
        message = await order_channel.send(
            content=army_mention,
            embed=embed,
            view=view
        )
    except discord.Forbidden:
        await interaction.followup.send("❌ У бота нет прав отправлять сообщения.", ephemeral=True)
        return

    async with bot.pool.acquire() as conn:
        await conn.execute("UPDATE orders SET message_id=$1 WHERE id=$2", message.id, order_id)

    await interaction.followup.send(f"✅ Заказ №{order_id} создан и отправлен на одобрение.", ephemeral=True)

# ------------------------------------------------------------
# Кнопки «Принять» / «Отклонить» (сервер 1, роль Армия)
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
            await conn.execute("UPDATE orders SET status=$1 WHERE id=$2", new_status, self.order_id)

        embed = interaction.message.embeds[0]
        embed.title = title
        embed.color = color
        embed.set_footer(text=embed.footer.text + f" | Статус: {new_status}")
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)

        if new_status == "approved":
            async with bot.pool.acquire() as conn:
                row = await conn.fetchrow("SELECT delivery_time FROM orders WHERE id=$1", self.order_id)
            if row is None:
                return
            delivery_time = row['delivery_time']
            try:
                t = datetime.datetime.strptime(delivery_time, '%H:%M').time()
            except ValueError:
                await send_to_criminal_server(self.order_id)
                return

            safe_start = datetime.time(17, 0)
            safe_end = datetime.time(18, 0)
            if safe_start <= t < safe_end:
                print(f"[INFO] Заказ {self.order_id} - безопасная поставка (время {delivery_time}), уведомление криминалу не отправляется.")
            else:
                await send_to_criminal_server(self.order_id)

async def send_to_criminal_server(order_id):
    async with bot.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)
        if row is None:
            print(f"[CRIMINAL] Заказ {order_id} не найден в БД")
            return

    faction = row['faction']
    delivery_time = row['delivery_time']
    role_mentions = " ".join(f"<@&{rid}>" for rid in CRIMINAL_ROLE_IDS)

    criminal_guild = bot.get_guild(CRIMINAL_GUILD_ID)
    if criminal_guild is None:
        print(f"[CRIMINAL] Сервер {CRIMINAL_GUILD_ID} не найден. Бот приглашён?")
        return
    channel = criminal_guild.get_channel(CRIMINAL_CHANNEL_ID)
    if channel is None:
        print(f"[CRIMINAL] Канал {CRIMINAL_CHANNEL_ID} не найден на сервере {CRIMINAL_GUILD_ID}.")
        return

    embed = discord.Embed(
        title="📦 Поставка для криминальных структур",
        color=discord.Color.orange()
    )
    embed.add_field(name="Фракция", value=faction, inline=True)
    embed.add_field(name="Время поставки", value=delivery_time, inline=True)
    embed.add_field(name="Уведомление", value=role_mentions, inline=False)
    embed.set_footer(text=f"ID заказа: {order_id} | by Ilya Vetrov")

    view = CollectView(order_id)

    try:
        msg = await channel.send(content=role_mentions, embed=embed, view=view)
        async with bot.pool.acquire() as conn:
            await conn.execute(
                "UPDATE orders SET criminal_message_id=$1 WHERE id=$2",
                msg.id, order_id
            )
        print(f"[CRIMINAL] Сообщение о поставке {order_id} отправлено в {channel.id}")
    except discord.Forbidden:
        print("[CRIMINAL] Нет прав на отправку сообщения.")
    except Exception as e:
        print(f"[CRIMINAL] Ошибка отправки: {e}")

# ------------------------------------------------------------
# Кнопка "Забрать поставку" (сервер 2, криминальные роли)
# ------------------------------------------------------------
class CollectView(discord.ui.View):
    def __init__(self, order_id):
        super().__init__(timeout=None)
        self.order_id = order_id

    @discord.ui.button(label="Забрать поставку", style=discord.ButtonStyle.primary, custom_id="collect_delivery")
    async def collect(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Ошибка.", ephemeral=True)
            return

        has_role = any(
            interaction.guild.get_role(rid) in interaction.user.roles
            for rid in CRIMINAL_ROLE_IDS
        )
        if not has_role:
            await interaction.response.send_message("❌ Только уполномоченные могут забрать поставку.", ephemeral=True)
            return

        async with bot.pool.acquire() as conn:
            await conn.execute("UPDATE orders SET status='collected' WHERE id=$1", self.order_id)

        embed = interaction.message.embeds[0]
        embed.title = "✅ Поставка забрана"
        embed.color = discord.Color.green()
        embed.set_footer(text=embed.footer.text + " | Статус: забрана")
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)

# ------------------------------------------------------------
# Запуск
# ------------------------------------------------------------
async def main():
    global bot
    print("[MAIN] Запуск...")
    pool = await create_pool()
    bot = DeliveryBot(pool, command_prefix="!", intents=intents)
    bot.tree.add_command(order)

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("[MAIN] DISCORD_TOKEN не задан!")
        raise ValueError("Переменная окружения DISCORD_TOKEN не задана")
    await bot.start(token)

if __name__ == "__main__":
    asyncio.run(main())
