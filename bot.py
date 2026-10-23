
import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import json
import os
import random
import re
from datetime import timedelta

# =========================
# CONFIGURATION
# =========================

TOKEN = os.getenv("DISCORD_TOKEN")

WARNINGS_FILE = "warnings.json"
LEVELS_FILE = "levels.json"
POINTS_FILE = "points.json"
SHOP_FILE = "shop.json"

# XP settings
XP_MIN = 5
XP_MAX = 15
XP_COOLDOWN = 60

# Points earned for every eligible message
POINTS_PER_MESSAGE = 3

# Discord dropdowns can contain at most 25 options
MAX_SHOP_ITEMS = 25


# =========================
# JSON HELPERS
# =========================

def load_json(filename):
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as file:
                data = json.load(file)
                if isinstance(data, dict):
                    return data
        except (json.JSONDecodeError, OSError):
            print(f"Could not load {filename}. Starting with empty data.")

    return {}


def save_json(filename, data):
    try:
        with open(filename, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4)
    except OSError as error:
        print(f"Could not save {filename}: {error}")


warnings_data = load_json(WARNINGS_FILE)
levels = load_json(LEVELS_FILE)
points = load_json(POINTS_FILE)
shop_items = load_json(SHOP_FILE)


# =========================
# BOT SETUP
# =========================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# Track when each user last earned XP.
# Key: (guild_id, user_id)
xp_cooldowns = {}


# =========================
# DATA HELPERS
# =========================

def get_warnings(guild_id, user_id):
    guild_key = str(guild_id)
    user_key = str(user_id)

    guild_warnings = warnings_data.setdefault(guild_key, {})
    return guild_warnings.setdefault(user_key, [])


def get_level_data(guild_id, user_id):
    guild_key = str(guild_id)
    user_key = str(user_id)

    guild_levels = levels.setdefault(guild_key, {})
    return guild_levels.setdefault(
        user_key,
        {"xp": 0, "level": 0}
    )


def get_points(guild_id, user_id):
    guild_key = str(guild_id)
    user_key = str(user_id)

    guild_points = points.setdefault(guild_key, {})
    return int(guild_points.get(user_key, 0))


def change_points(guild_id, user_id, amount):
    guild_key = str(guild_id)
    user_key = str(user_id)

    guild_points = points.setdefault(guild_key, {})
    current = int(guild_points.get(user_key, 0))
    guild_points[user_key] = max(0, current + amount)

    save_json(POINTS_FILE, points)
    return guild_points[user_key]


def get_shop_items(guild_id):
    return shop_items.setdefault(str(guild_id), [])


def xp_needed(level):
    return 100 + level * 50


def parse_duration(duration):
    """
    Supports values such as:
    10m = 10 minutes
    2h  = 2 hours
    1d  = 1 day
    1w  = 1 week

    Returns a timedelta or None if invalid.
    """
    match = re.fullmatch(
        r"\s*(\d+)\s*([mhdw])\s*",
        duration.lower()
    )

    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)

    if amount <= 0:
        return None

    units = {
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
        "w": timedelta(weeks=amount),
    }

    result = units[unit]

    # Avoid unreasonably long durations.
    if result > timedelta(days=365):
        return None

    return result


async def safe_reply(interaction, message, ephemeral=True):
    """Reply whether or not the interaction was already acknowledged."""
    if interaction.response.is_done():
        await interaction.followup.send(
            message,
            ephemeral=ephemeral
        )
    else:
        await interaction.response.send_message(
            message,
            ephemeral=ephemeral
        )


# =========================
# XP AND POINTS SYSTEM
# =========================

@bot.event
async def on_message(message):
    if message.author.bot or message.guild is None:
        return

    guild_id = message.guild.id
    user_id = message.author.id

    # Award points on EVERY eligible message.
    change_points(
        guild_id,
        user_id,
        POINTS_PER_MESSAGE
    )

    # Award XP only when the cooldown has passed.
    cooldown_key = (guild_id, user_id)
    now = asyncio.get_running_loop().time()
    last_xp_time = xp_cooldowns.get(cooldown_key, 0)

    if now - last_xp_time >= XP_COOLDOWN:
        xp_cooldowns[cooldown_key] = now

        data = get_level_data(guild_id, user_id)
        data["xp"] += random.randint(XP_MIN, XP_MAX)

        leveled_up = False

        while data["xp"] >= xp_needed(data["level"]):
            data["xp"] -= xp_needed(data["level"])
            data["level"] += 1
            leveled_up = True

        save_json(LEVELS_FILE, levels)

        if leveled_up:
            try:
                await message.channel.send(
                    f"🎉 {message.author.mention} reached "
                    f"**Level {data['level']}**!"
                )
            except discord.HTTPException:
                pass

    # Keep this call in case prefix commands are added later.
    await bot.process_commands(message)


# =========================
# MODERATION COMMANDS
# =========================

@bot.tree.command(
    name="warn",
    description="Warn a member"
)
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.describe(
    member="The member to warn",
    reason="Why this member is being warned"
)
async def warn(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str
):
    if member.bot:
        await interaction.response.send_message(
            "You cannot warn a bot.",
            ephemeral=True
        )
        return

    if member.id == interaction.user.id:
        await interaction.response.send_message(
            "You cannot warn yourself.",
            ephemeral=True
        )
        return

    if member.id == interaction.guild.owner_id:
        await interaction.response.send_message(
            "You cannot warn the server owner.",
            ephemeral=True
        )
        return

    if member.top_role >= interaction.user.top_role:
        if interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message(
                "You cannot warn someone with an equal or higher role.",
                ephemeral=True
            )
            return

    entry = {
        "reason": reason,
        "moderator_id": interaction.user.id
    }

    user_warnings = get_warnings(
        interaction.guild_id,
        member.id
    )
    user_warnings.append(entry)
    save_json(WARNINGS_FILE, warnings_data)

    await interaction.response.send_message(
        f"⚠️ {member.mention} was warned.\n"
        f"**Reason:** {reason}\n"
        f"**Total warnings:** {len(user_warnings)}"
    )


@bot.tree.command(
    name="warnings",
    description="View a member's warnings"
)
@app_commands.checks.has_permissions(moderate_members=True)
async def warnings(
    interaction: discord.Interaction,
    member: discord.Member
):
    user_warnings = get_warnings(
        interaction.guild_id,
        member.id
    )

    if not user_warnings:
        await interaction.response.send_message(
            f"{member.mention} has no warnings.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title=f"Warnings for {member}",
        color=discord.Color.orange()
    )

    for index, entry in enumerate(user_warnings[-10:], start=1):
        moderator_id = entry.get("moderator_id", "Unknown")
        reason = entry.get("reason", "No reason supplied")

        embed.add_field(
            name=f"Warning {index}",
            value=(
                f"**Reason:** {reason}\n"
                f"**Moderator ID:** {moderator_id}"
            ),
            inline=False
        )

    embed.set_footer(
        text=f"Total warnings: {len(user_warnings)}"
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


@bot.tree.command(
    name="timeout",
    description="Timeout a member"
)
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.describe(
    member="The member to timeout",
    duration="Duration such as 10m, 2h, or 1d",
    reason="Reason for the timeout"
)
async def timeout(
    interaction: discord.Interaction,
    member: discord.Member,
    duration: str,
    reason: str = "No reason provided"
):
    parsed_duration = parse_duration(duration)

    if parsed_duration is None:
        await interaction.response.send_message(
            "Invalid duration. Use formats like `10m`, `2h`, `1d`, or `1w`.",
            ephemeral=True
        )
        return

    if member.id == interaction.user.id:
        await interaction.response.send_message(
            "You cannot timeout yourself.",
            ephemeral=True
        )
        return

    if member.id == interaction.guild.owner_id:
        await interaction.response.send_message(
            "You cannot timeout the server owner.",
            ephemeral=True
        )
        return

    if member.top_role >= interaction.user.top_role:
        if interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message(
                "You cannot timeout someone with an equal or higher role.",
                ephemeral=True
            )
            return

    bot_member = interaction.guild.me

    if bot_member is None or member.top_role >= bot_member.top_role:
        await interaction.response.send_message(
            "My highest role must be above that member's highest role.",
            ephemeral=True
        )
        return

    try:
        await member.timeout(
            parsed_duration,
            reason=reason
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to timeout that member.",
            ephemeral=True
        )
        return
    except discord.HTTPException:
        await interaction.response.send_message(
            "Discord couldn't apply the timeout.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"🔇 {member.mention} was timed out for **{duration}**.\n"
        f"**Reason:** {reason}"
    )


@bot.tree.command(
    name="kick",
    description="Kick a member"
)
@app_commands.checks.has_permissions(kick_members=True)
@app_commands.describe(
    member="The member to kick",
    reason="Reason for the kick"
)
async def kick(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided"
):
    if member.id == interaction.user.id:
        await interaction.response.send_message(
            "You cannot kick yourself.",
            ephemeral=True
        )
        return

    if member.id == interaction.guild.owner_id:
        await interaction.response.send_message(
            "You cannot kick the server owner.",
            ephemeral=True
        )
        return

    if member.top_role >= interaction.user.top_role:
        if interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message(
                "You cannot kick someone with an equal or higher role.",
                ephemeral=True
            )
            return

    bot_member = interaction.guild.me

    if bot_member is None or member.top_role >= bot_member.top_role:
        await interaction.response.send_message(
            "My highest role must be above that member's highest role.",
            ephemeral=True
        )
        return

    try:
        await member.kick(reason=reason)
    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to kick that member.",
            ephemeral=True
        )
        return
    except discord.HTTPException:
        await interaction.response.send_message(
            "Discord couldn't kick that member.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"👢 **{member}** was kicked.\n**Reason:** {reason}"
    )


@bot.tree.command(
    name="ban",
    description="Ban a member, optionally for a set duration"
)
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.describe(
    member="The member to ban",
    reason="Reason for the ban",
    duration="Optional duration, such as 1d or 2h"
)
async def ban(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided",
    duration: str = ""
):
    if member.id == interaction.user.id:
        await interaction.response.send_message(
            "You cannot ban yourself.",
            ephemeral=True
        )
        return

    if member.id == interaction.guild.owner_id:
        await interaction.response.send_message(
            "You cannot ban the server owner.",
            ephemeral=True
        )
        return

    if member.top_role >= interaction.user.top_role:
        if interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message(
                "You cannot ban someone with an equal or higher role.",
                ephemeral=True
            )
            return

    bot_member = interaction.guild.me

    if bot_member is None or member.top_role >= bot_member.top_role:
        await interaction.response.send_message(
            "My highest role must be above that member's highest role.",
            ephemeral=True
        )
        return

    parsed_duration = None

    if duration:
        parsed_duration = parse_duration(duration)

        if parsed_duration is None:
            await interaction.response.send_message(
                "Invalid duration. Use formats like `10m`, `2h`, `1d`, or `1w`.",
                ephemeral=True
            )
            return

    try:
        await member.ban(reason=reason)
    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to ban that member.",
            ephemeral=True
        )
        return
    except discord.HTTPException:
        await interaction.response.send_message(
            "Discord couldn't ban that member.",
            ephemeral=True
        )
        return

    if parsed_duration is not None:
        await interaction.response.send_message(
            f"🔨 **{member}** was banned for **{duration}**.\n"
            f"**Reason:** {reason}\n"
            "The bot will attempt to unban them when the duration ends. "
            "This timer won't survive a bot restart."
        )

        async def unban_later():
            await asyncio.sleep(parsed_duration.total_seconds())

            try:
                await interaction.guild.unban(
                    member,
                    reason="Temporary ban expired"
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        asyncio.create_task(unban_later())
    else:
        await interaction.response.send_message(
            f"🔨 **{member}** was permanently banned.\n"
            f"**Reason:** {reason}"
        )


# =========================
# LEVEL COMMANDS
# =========================

@bot.tree.command(
    name="level",
    description="Check your level and XP"
)
@app_commands.describe(
    member="The member to check (optional)"
)
async def level(
    interaction: discord.Interaction,
    member: discord.Member = None
):
    member = member or interaction.user

    data = get_level_data(
        interaction.guild_id,
        member.id
    )

    needed = xp_needed(data["level"])

    embed = discord.Embed(
        title=f"{member.display_name}'s Level",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="Level",
        value=str(data["level"]),
        inline=True
    )
    embed.add_field(
        name="XP",
        value=f"{data['xp']} / {needed}",
        inline=True
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="leaderboard",
    description="Show the server's top members by level"
)
async def leaderboard(interaction: discord.Interaction):
    guild_levels = levels.get(str(interaction.guild_id), {})

    if not guild_levels:
        await interaction.response.send_message(
            "Nobody has earned XP yet!",
            ephemeral=True
        )
        return

    ranking = sorted(
        guild_levels.items(),
        key=lambda item: (
            item[1].get("level", 0),
            item[1].get("xp", 0)
        ),
        reverse=True
    )

    lines = []

    for position, (user_id, data) in enumerate(ranking[:10], start=1):
        member = interaction.guild.get_member(int(user_id))

        if member is not None:
            name = member.display_name
        else:
            name = f"User {user_id}"

        lines.append(
            f"**{position}.** {name} — "
            f"Level {data.get('level', 0)} "
            f"({data.get('xp', 0)} XP)"
        )

    embed = discord.Embed(
        title="🏆 Server Leaderboard",
        description="\n".join(lines),
        color=discord.Color.gold()
    )

    await interaction.response.send_message(embed=embed)


# =========================
# POINTS COMMANDS
# =========================

@bot.tree.command(
    name="balance",
    description="Check your points balance"
)
@app_commands.describe(
    member="The member whose balance you want to check"
)
async def balance(
    interaction: discord.Interaction,
    member: discord.Member = None
):
    member = member or interaction.user

    amount = get_points(
        interaction.guild_id,
        member.id
    )

    await interaction.response.send_message(
        f"🪙 **{member.display_name}** has **{amount} points**."
    )


# =========================
# ADD ROLES TO THE SHOP
# =========================

@bot.tree.command(
    name="add",
    description="Add a role to the points shop"
)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    role="The role members can buy",
    price="How many points the role costs"
)
async def add(
    interaction: discord.Interaction,
    role: discord.Role,
    price: app_commands.Range[int, 1, 1000000000]
):
    guild = interaction.guild

    if guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True
        )
        return

    if role.is_default() or role.managed:
        await interaction.response.send_message(
            "That role cannot be sold in the shop.",
            ephemeral=True
        )
        return

    bot_member = guild.me

    if bot_member is None or role >= bot_member.top_role:
        await interaction.response.send_message(
            "Move my highest role above the role you want to sell.",
            ephemeral=True
        )
        return

    items = get_shop_items(guild.id)

    for item in items:
        if int(item.get("role_id", 0)) == role.id:
            await interaction.response.send_message(
                f"{role.mention} is already in the shop. "
                "Remove its existing entry from shop.json before adding it again.",
                ephemeral=True
            )
            return

    if len(items) >= MAX_SHOP_ITEMS:
        await interaction.response.send_message(
            "The shop already has 25 roles, the Discord dropdown limit. "
            "Remove an entry from shop.json before adding another.",
            ephemeral=True
        )
        return

    items.append({
        "name": role.name,
        "role_id": role.id,
        "price": int(price)
    })

    save_json(SHOP_FILE, shop_items)

    await interaction.response.send_message(
        f"✅ Added {role.mention} to the shop for **{price} points**."
    )


# =========================
# SHOP DROPDOWN
# =========================

class ShopSelect(discord.ui.Select):
    def __init__(self, items):
        options = []

        for item in items:
            options.append(
                discord.SelectOption(
                    label=item["name"][:100],
                    description=f"Cost: {item['price']} points",
                    value=str(item["role_id"])
                )
            )

        super().__init__(
            placeholder="Choose a role to buy...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view

        if view is None:
            return

        if interaction.guild_id != view.guild_id:
            await interaction.response.send_message(
                "This shop belongs to another server.",
                ephemeral=True
            )
            return

        role_id = int(self.values[0])
        items = get_shop_items(view.guild_id)

        selected_item = next(
            (
                item for item in items
                if int(item.get("role_id", 0)) == role_id
            ),
            None
        )

        if selected_item is None:
            await interaction.response.send_message(
                "That item is no longer available.",
                ephemeral=True
            )
            return

        view.selected_role_id = role_id

        await interaction.response.send_message(
            f"You selected **{selected_item['name']}** "
            f"for **{selected_item['price']} points**. "
            "Press **Buy Role** to complete your purchase.",
            ephemeral=True
        )


# =========================
# SHOP VIEW AND PURCHASES
# =========================

class ShopView(discord.ui.View):
    def __init__(self, owner_id, guild_id):
        super().__init__(timeout=180)

        self.owner_id = owner_id
        self.guild_id = guild_id
        self.selected_role_id = None

        items = get_shop_items(guild_id)
        self.add_item(ShopSelect(items))

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened this shop can use it.",
                ephemeral=True
            )
            return False

        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "This shop belongs to another server.",
                ephemeral=True
            )
            return False

        return True

    @discord.ui.button(
        label="Buy Role",
        style=discord.ButtonStyle.success,
        emoji="🛒"
    )
    async def buy_role(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if self.selected_role_id is None:
            await interaction.response.send_message(
                "Choose a role from the dropdown first.",
                ephemeral=True
            )
            return

        guild = interaction.guild

        if guild is None:
            await interaction.response.send_message(
                "This purchase must be made inside a server.",
                ephemeral=True
            )
            return

        items = get_shop_items(self.guild_id)

        item = next(
            (
                entry for entry in items
                if int(entry.get("role_id", 0)) == self.selected_role_id
            ),
            None
        )

        if item is None:
            await interaction.response.send_message(
                "That role is no longer in the shop.",
                ephemeral=True
            )
            return

        role = guild.get_role(self.selected_role_id)

        if role is None:
            await interaction.response.send_message(
                "That role no longer exists. Ask an admin to update the shop.",
                ephemeral=True
            )
            return

        if role.is_default() or role.managed:
            await interaction.response.send_message(
                "That role cannot be purchased.",
                ephemeral=True
            )
            return

        bot_member = guild.me

        if bot_member is None or role >= bot_member.top_role:
            await interaction.response.send_message(
                "The bot's highest role must be above the shop role. "
                "Ask an admin to fix the role order.",
                ephemeral=True
            )
            return

        member = interaction.user

        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "I couldn't identify your server membership.",
                ephemeral=True
            )
            return

        if role in member.roles:
            await interaction.response.send_message(
                "You already have that role!",
                ephemeral=True
            )
            return

        price = int(item["price"])
        current_balance = get_points(guild.id, member.id)

        if current_balance < price:
            await interaction.response.send_message(
                f"You need **{price} points**, but you only have "
                f"**{current_balance} points**.",
                ephemeral=True
            )
            return

        # Add the role first. Only charge the user if that succeeds.
        try:
            await member.add_roles(
                role,
                reason="Purchased from the points shop"
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to give that role. "
                "Check my role position and permissions.",
                ephemeral=True
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message(
                "Discord couldn't give you that role. Please try again.",
                ephemeral=True
            )
            return

        # Deduct points after the role is successfully assigned.
        new_balance = change_points(
            guild.id,
            member.id,
            -price
        )

        await interaction.response.send_message(
            f"🎉 You bought **{role.name}** for **{price} points**!\n"
            f"Your remaining balance is **{new_balance} points**.",
            ephemeral=True
        )


@bot.tree.command(
    name="shop",
    description="Browse and buy roles using points"
)
async def shop(interaction: discord.Interaction):
    items = get_shop_items(interaction.guild_id)

    if not items:
        await interaction.response.send_message(
            "🛍️ The shop is empty! An admin can add roles using `/add`.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="🛍️ Points Shop",
        description=(
            "Choose a role from the dropdown, then press **Buy Role**.\n\n"
            "You earn **3 points per message**."
        ),
        color=discord.Color.green()
    )

    for item in items:
        role = interaction.guild.get_role(
            int(item.get("role_id", 0))
        )

        role_name = role.name if role else item.get("name", "Unknown role")

        embed.add_field(
            name=role_name,
            value=f"🪙 **{item['price']} points**",
            inline=True
        )

    embed.set_footer(
        text=f"Your balance: {get_points(interaction.guild_id, interaction.user.id)} points"
    )

    view = ShopView(
        interaction.user.id,
        interaction.guild_id
    )

    await interaction.response.send_message(
        embed=embed,
        view=view,
        ephemeral=True
    )


# =========================
# ERROR HANDLING
# =========================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        message = "❌ You don't have permission to use this command."
    elif isinstance(error, app_commands.BotMissingPermissions):
        message = "❌ I'm missing a required permission for this command."
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = "⏳ Please wait before using this command again."
    else:
        print(f"Command error: {error}")
        message = "Something went wrong while running that command."

    await safe_reply(interaction, message)


# =========================
# STARTUP
# =========================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as error:
        print(f"Could not sync slash commands: {error}")


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(
            "Set the DISCORD_TOKEN environment variable before starting the bot."
        )

    bot.run(TOKEN)
